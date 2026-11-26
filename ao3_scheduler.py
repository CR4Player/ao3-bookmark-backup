"""Backup scheduler loop, logging setup, and container healthcheck (Layer 5)."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

from ao3_http import acquire_lock, prefer_ipv4, touch_marker
from ao3_scraper import (
    Config,
    configured_backup,
    next_run_at,
    settings_for_config,
)
from ao3_store import (
    BACKUP_ACTIONS,
    Archive,
    resolve_schedule,
    schedule_label,
)


RUNNING_MAX_IDLE_SECONDS = 7200  # .running 超过 2 小时无心跳视为卡死
BACKUP_POLL_SECONDS = 3

log = logging.getLogger("ao3")


def run_scheduler(
    cfg: Config, archive: Archive, stop: threading.Event,
    execute_pass: Callable[[Config, str], tuple[bool, dict[str, int]]],
    *,
    poll_seconds: float | None = None,
) -> None:
    poll = BACKUP_POLL_SECONDS if poll_seconds is None else poll_seconds
    defaults = settings_for_config(cfg)
    startup_pending = True
    target: Optional[float] = None
    source = "schedule"
    schedule = None
    heartbeat_at = 0.0
    checking_previous_run = True
    waiting_logged = False
    setup_logged = False
    last_ipv4: Optional[bool] = None

    while not stop.is_set():
        try:
            if not archive.has_web_auth():
                token = archive.ensure_setup_token()
                if not setup_logged and token:
                    log.info("[SETUP] 首次网页初始化口令（Setup Token）：%s", token)
                    log.info("[SETUP] 请打开 Web 页面完成设置（口令在初始化完成后自动清除）")
                    setup_logged = True
            else:
                setup_logged = False

            settings = archive.backup_settings() or defaults
            try:
                creds = archive.decrypted_ao3_credentials()
            except RuntimeError as exc:
                creds = ("", "", "")
                if not waiting_logged:
                    log.warning("读取 AO3 加密凭据异常：%s", exc)
            pass_cfg = configured_backup(cfg, settings, creds)
            if not pass_cfg.bookmark_user:
                cfg.setup_pending_file.parent.mkdir(parents=True, exist_ok=True)
                touch_marker(cfg.setup_pending_file)
                if not waiting_logged:
                    log.info("等待通过网页完成初始化设置（配置目标书签用户名）…")
                    waiting_logged = True
                if time.monotonic() >= heartbeat_at:
                    archive.publish_scheduler(defaults, pass_cfg.formats, 0, awaiting_setup=True)
                    heartbeat_at = time.monotonic() + 15
                stop.wait(poll)
                continue

            if waiting_logged or cfg.setup_pending_file.exists():
                waiting_logged = False
                heartbeat_at = 0.0
                with contextlib.suppress(OSError):
                    cfg.setup_pending_file.unlink()

            if last_ipv4 is None or pass_cfg.prefer_ipv4 != last_ipv4:
                prefer_ipv4(pass_cfg.prefer_ipv4)
                last_ipv4 = pass_cfg.prefer_ipv4

            sched = resolve_schedule(settings)
            current_schedule = sched["key"]
            if startup_pending:
                startup_pending = False
                if pass_cfg.run_on_start and sched["mode"] != "manual":
                    target, source = time.time(), "startup"
                else:
                    target, source = None, "schedule"
                    if sched["mode"] == "manual":
                        log.info("定时计划：已关闭自动定时（仅手动）")
                    heartbeat_at = 0.0
            if sched["mode"] == "manual":
                if schedule is not None and current_schedule != schedule:
                    target, source = None, "schedule"
                    log.info("下次计划抓取：已关闭自动定时（仅手动）")
                    heartbeat_at = 0.0
            elif target is None or (schedule is not None and current_schedule != schedule):
                next_run = next_run_at(settings)
                target = next_run.timestamp() if next_run else None
                source = "schedule"
                if next_run:
                    log.info("下次计划抓取：%s（%s）", next_run.isoformat(timespec="minutes"), schedule_label(settings))
                heartbeat_at = 0.0
            schedule = current_schedule
            if time.monotonic() >= heartbeat_at:
                archive.publish_scheduler(defaults, pass_cfg.formats, int(target or 0), awaiting_setup=False)
                heartbeat_at = time.monotonic() + 15

            due = source if target is not None and time.time() >= target else None
            if checking_previous_run or due or archive.has_queued_backup():
                with acquire_lock(cfg.lock_file):
                    job = archive.claim_backup(due)
                    checking_previous_run = False
                    if job:
                        run_scheduled_pass(job, pass_cfg, archive, stop, execute_pass)
                if job:
                    if job[1] == "scan":
                        target, source = None, "schedule"
                    heartbeat_at = 0.0
                    continue
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            log.warning("调度暂未完成，稍后继续：%s", exc)
        stop.wait(poll)


def run_scheduled_pass(
    job: tuple[int, str, str], cfg: Config, archive: Archive, stop: threading.Event,
    execute_pass: Callable[[Config, str], tuple[bool, dict[str, int]]],
) -> None:
    job_id, action, source = job
    label = {"startup": "启动抓取", "schedule": "定时抓取"}.get(source, BACKUP_ACTIONS[action])
    log.info("开始%s（任务 %d）", label, job_id)
    try:
        ok, stats = execute_pass(cfg, action)
    except InterruptedError:
        log.warning("%s已中断", label)
        archive.finish_backup(job_id, "interrupted")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("%s失败：%s", label, exc)
        archive.finish_backup(job_id, "interrupted" if stop.is_set() else "failed")
        return
    status = ("interrupted" if stop.is_set() else "failed" if not ok else
              "partial" if stats["failed"] or stats["skipped_errors"] else "success")
    summary = f"检查 {stats['seen']} 条，成功归档 {stats['downloaded']} 篇，失败 {stats['failed']} 项"
    if stats["skipped_errors"]:
        summary += f"，已达失败上限 {stats['skipped_errors']} 篇"
    archive.finish_backup(job_id, status, summary)


def setup_logging(cfg: Config, verbose: bool) -> None:
    log.setLevel(logging.DEBUG)
    for h in list(log.handlers):
        log.removeHandler(h)
        with contextlib.suppress(OSError):
            h.close()

    # Windows 控制台默认 GBK，中文日志会乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    if cfg.dry_run:
        return

    try:
        cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            cfg.log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as exc:
        log.warning("日志文件不可写: %s", exc)


def healthcheck(out_dir: Path, max_age_hours: float) -> int:
    """容器健康检查：正在活跃运行或最近一次成功运行足够新即视为健康。"""
    running = out_dir / ".running"
    if running.exists():
        try:
            running_age = time.time() - running.stat().st_mtime
            if running_age <= RUNNING_MAX_IDLE_SECONDS:
                print(f"HEALTHY: 备份任务正在运行中（最近心跳 {running_age / 60:.1f} 分钟前）")
                return 0
        except OSError:
            pass

    if (out_dir / ".setup_pending").exists():
        print("HEALTHY: 等待通过网页完成初始化配置")
        return 0

    marker = out_dir / ".last_run"
    if not marker.exists():
        print(f"UNHEALTHY: {marker} 不存在（还没成功跑过一轮）")
        return 1
    if max_age_hours <= 0:
        print("HEALTHY: 已禁用时效检查")
        return 0
    age_h = (time.time() - marker.stat().st_mtime) / 3600
    if age_h > max_age_hours:
        print(f"UNHEALTHY: 上次成功运行在 {age_h:.1f} 小时前，超过 {max_age_hours:g} 小时")
        return 1
    print(f"HEALTHY: 上次成功运行在 {age_h:.1f} 小时前")
    return 0
