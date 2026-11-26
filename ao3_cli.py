"""Top-level CLI dispatcher wiring web, scraper, legacy, and scheduler modules (Layer 6)."""

from __future__ import annotations

import argparse
import contextlib
import logging
import signal
import sqlite3
import sys
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from ao3_http import AO3Client, CookieExpiredError, acquire_lock, prefer_ipv4, touch_marker
from ao3_legacy import (
    human_size,
    load_state,
    prune_retired_files,
    save_state,
    write_index,
    write_manifest,
)
from ao3_scheduler import healthcheck, run_scheduler, setup_logging
from ao3_scraper import (
    Config,
    __version__,
    configured_backup,
    env,
    env_bool,
    env_float,
    env_int,
    next_run_at,
    retry_works,
    run_once,
    settings_for_config,
)
from ao3_store import (
    DOWNLOAD_FORMATS,
    Archive,
    import_legacy,
    parse_duration,
    parse_hhmm,
    schedule_label,
)
from ao3_web import serve


log = logging.getLogger("ao3")


def build_config(args: argparse.Namespace, archive: Archive | None = None) -> Config:
    out = Path(args.out or env("OUT_DIR", "./data")).expanduser().resolve()
    bookmark_user = (args.bookmark_user or env("BOOKMARK_USER") or env("AO3_USER")).strip()
    if not bookmark_user and archive is not None:
        saved = archive.backup_settings()
        bookmark_user = (saved.bookmark_user if saved else "") or archive.stored_bookmark_user()
    require_user = args.once or args.dry_run or args.legacy_files
    if require_user and not bookmark_user:
        raise SystemExit(
            "缺少书签所属用户名：请通过网页初始化设置，或使用 --bookmark-user / BOOKMARK_USER 指定。"
        )

    default_format = "html" if args.legacy_files else "none"
    formats = tuple(f.strip().lower() for f in env("FORMATS", default_format).split(",") if f.strip())
    if "none" in formats and (args.legacy_files or formats != ("none",)):
        raise SystemExit("FORMATS=none 只能在 SQLite 模式中单独使用")
    allowed = DOWNLOAD_FORMATS
    unknown = [f for f in formats if f not in allowed and f != "none"]
    if unknown:
        print(
            f"警告：忽略不支持的格式 {', '.join(unknown)}（可选 {', '.join(sorted(allowed))}）",
            file=sys.stderr,
        )
    formats = tuple(dict.fromkeys(f for f in formats if f in allowed))
    if args.legacy_files and not formats:
        formats = ("html",)

    run_at = env("RUN_AT", "03:30")
    try:
        interval = parse_duration(env("INTERVAL", ""))
        try:
            hour, minute = parse_hhmm(run_at)
        except ValueError:
            if not interval:
                raise
            hour, minute = 3, 30
        run_at = f"{hour:02d}:{minute:02d}"
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    delay = args.delay if args.delay is not None else env_float("DELAY", 1.5)
    max_pages = args.max_pages if args.max_pages else env_int("MAX_PAGES", 200)
    max_downloads = args.max_downloads if args.max_downloads else env_int("MAX_DOWNLOADS", 0)

    return Config(
        bookmark_user=bookmark_user,
        out_dir=out,
        user=(args.user or env("AO3_USER")).strip(),
        password=args.password if args.password is not None else env("AO3_PASSWORD"),
        delay=max(0.1, delay),
        timeout=max(5, env_int("TIMEOUT", 60)),
        retries=max(1, env_int("RETRIES", 4)),
        max_pages=max(1, max_pages),
        max_downloads=max(0, max_downloads),
        stop_after_idle_pages=max(0, env_int("STOP_AFTER_IDLE_PAGES", 0)),
        max_errors=max(1, env_int("MAX_ERRORS", 5)),
        keep_versions=env_bool("KEEP_VERSIONS", True),
        run_at=run_at,
        interval=interval,
        once=args.once,
        dry_run=args.dry_run,
        run_on_start=env_bool("RUN_ON_START", True),
        prefer_ipv4=env_bool("PREFER_IPV4", True),
        formats=formats,
        refresh_content=args.refresh_content,
    )


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="定期把 AO3 书签里的新作品 / 更新作品增量备份到本地",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出")
    ap.add_argument("--dry-run", action="store_true", help="只读干跑报告，不修改本地文件与状态")
    ap.add_argument("--refresh-content", action="store_true", help="重新核对全文，包含更新时间相同的作品（SQLite 模式）")
    ap.add_argument("--bookmark-user", help="要备份谁的书签（默认取 BOOKMARK_USER）")
    ap.add_argument("--user", help="登录账号（备份私有书签/受限作品时需要）")
    ap.add_argument("--password", help="登录密码")
    ap.add_argument("--out", help="输出目录（默认 ./data）")
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument("--serve", action="store_true", help="启动 SQLite 归档的 Web 总览")
    modes.add_argument("--import-legacy", type=Path, metavar="DIR", help="只读导入旧数据目录")
    modes.add_argument("--backup-db", type=Path, metavar="FILE", help="生成可独立恢复的数据库备份")
    modes.add_argument("--legacy-files", action="store_true", help="以旧模式继续生成静态文件与索引")
    ap.add_argument("--host", default=env("WEB_HOST", "127.0.0.1"), help="Web 监听地址")
    ap.add_argument("--port", type=int, default=env_int("WEB_PORT", 8765), help="Web 监听端口")
    ap.add_argument("--max-downloads", type=int, default=0, help="本次最多下载多少篇（调试用）")
    ap.add_argument("--max-pages", type=int, default=0, help="最多翻多少页书签（调试用，0=取 MAX_PAGES）")
    ap.add_argument("--delay", type=float, help="每次请求间隔秒数（默认 1.5，优先于 DELAY 环境变量）")
    ap.add_argument(
        "--healthcheck",
        action="store_true",
        help="容器健康检查：正在活跃运行或最近一次成功运行未超时即返回 0",
    )
    ap.add_argument(
        "--reset-web-auth",
        action="store_true",
        help="重置网页访问密码并生成新的首次初始化口令（忘记密码时使用）",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    ap.add_argument("--version", action="version", version=f"ao3_backup {__version__}")
    args = ap.parse_args(list(argv) if argv is not None else None)
    if args.refresh_content and args.legacy_files:
        ap.error("--refresh-content 仅适用于 SQLite 模式")

    out = Path(args.out or env("OUT_DIR", "./data")).expanduser().resolve()
    if args.healthcheck:
        return healthcheck(out, env_float("HEALTHCHECK_MAX_AGE_HOURS", 48.0))

    archive = None if args.legacy_files else Archive(out)
    if archive and (out / "state.json").exists():
        ap.error("输出目录含旧版 state.json。请用 --import-legacy 指向旧目录，并用 --out 指向独立目录")
    try:
        if args.import_legacy:
            assert archive is not None
            source = args.import_legacy.expanduser()
            with acquire_lock(out / ".backup.lock"):
                result = import_legacy(archive, source)
            label = "已核对现有迁移" if result.already_imported else "迁移完成"
            print(f"{label}：{result.works} 条作品记录，{result.files} 份文件，{result.size:,} 字节")
            for path in result.missing:
                print(f"旧库缺失文件：{path}", file=sys.stderr)
            return 0
        if args.backup_db:
            assert archive is not None
            archive.backup(args.backup_db.expanduser())
            print(f"数据库备份已生成：{args.backup_db}")
            return 0
        if args.reset_web_auth:
            if archive is None:
                ap.error("--reset-web-auth 仅适用于 SQLite 模式")
            archive.initialize()
            token = archive.reset_web_auth()
            print(f"已重置网页访问密码。请使用新的一次性初始化口令登录 /setup：{token}")
            return 0
        if args.serve:
            assert archive is not None
            archive.initialize()
            if not archive.has_web_auth():
                token = archive.ensure_setup_token()
                print(f"[SETUP] 首次网页初始化口令（Setup Token）：{token}", flush=True)
            serve(archive, args.host, args.port)
            return 0
        if archive and not args.dry_run:
            archive.initialize()
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        ap.exit(1, f"{exc}\n")

    cfg = build_config(args, archive)
    if archive is not None and (args.once or args.dry_run):
        saved = archive.backup_settings()
        if saved is not None:
            try:
                creds = archive.decrypted_ao3_credentials()
            except RuntimeError as exc:
                ap.exit(1, f"{exc}\n")
            cfg = configured_backup(cfg, saved, creds)
        elif archive.ao3_credentials_status()["configured"]:
            creds = archive.decrypted_ao3_credentials()
            cfg = replace(cfg, auth_mode=creds[0], user=creds[1], password=creds[2])
    setup_logging(cfg, args.verbose)
    prefer_ipv4(cfg.prefer_ipv4)

    log.info("=" * 62)
    log.info("AO3 书签备份器 v%s%s", __version__, " [DRY-RUN]" if cfg.dry_run else "")
    log.info("书签用户: %s", cfg.bookmark_user or "（等待网页初始化配置）")
    log.info("输出目录: %s", cfg.out_dir)
    log.info("附件格式: %s", ", ".join(cfg.formats) or "关闭")
    if not cfg.once and (archive is None or cfg.dry_run):
        log.info("计划: %s", schedule_label(settings_for_config(cfg)))
    log.info("=" * 62)

    if not cfg.dry_run and archive is None:
        for d in (cfg.out_dir, cfg.works_dir, cfg.versions_dir, cfg.out_dir / "logs"):
            d.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()

    def _handle(signum, _frame):  # noqa: ANN001
        log.info("收到信号 %s，准备保存进度并退出…", signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _handle)

    client = AO3Client(cfg, stop=stop)

    def execute_pass(cfg: Config, action: str = "scan", *, lock_held: bool = False) -> tuple[bool, dict[str, int]]:
        lock_ctx = contextlib.nullcontext() if cfg.dry_run or lock_held else acquire_lock(cfg.lock_file)
        with lock_ctx:
            prev_user, prev_mode = client.cfg.user, client.cfg.auth_mode
            client.configure(cfg)
            if cfg.user != prev_user or cfg.auth_mode != prev_mode:
                client.reload_cookies(force=True)
            else:
                client.reload_cookies(force=False)
            state = archive.scan_state(cfg.bookmark_user) if archive else load_state(cfg, log)
            if archive and not cfg.dry_run:
                archive.bind_user(cfg.bookmark_user)
            if not cfg.dry_run:
                touch_marker(cfg.running_file)
            try:
                if client.has_credentials() and not client.logged_in:
                    if not client.login():
                        raise RuntimeError("AO3 登录失败，为避免漏掉私有书签与受限作品，本轮中止")
                elif cfg.auth_mode == "cookie":
                    if not client.verify_cookie_session():
                        if archive is not None:
                            archive.mark_ao3_cookie_expired(True)
                        raise CookieExpiredError("AO3 登录 Cookie 已过期（仅 Cookie 模式），请在网页「账号与安全」重新登录")
                    if archive is not None:
                        archive.mark_ao3_cookie_expired(False)

                try:
                    if action == "retry":
                        assert archive is not None
                        stats, page_ok = retry_works(cfg, client, archive, state, stop)
                    else:
                        stats, page_ok = run_once(cfg, client, state, stop, archive=archive)
                except CookieExpiredError:
                    if archive is not None:
                        archive.mark_ao3_cookie_expired(True)
                    raise

                if cfg.dry_run:
                    log.info(
                        "[DRY-RUN] 干跑完成（未修改任何文件）：预计新增 %d，更新/补齐 %d，"
                        "未变 %d，跳过 %d（含历史失败跳过 %d），错误 %d（扫描 %d 页 / %d 条）",
                        stats["new"],
                        stats["updated"],
                        stats["unchanged"],
                        stats["skipped"],
                        stats["skipped_errors"],
                        stats["failed"],
                        stats["pages"],
                        stats["seen"],
                    )
                    return page_ok and not stop.is_set(), stats

                if archive is None:
                    save_state(cfg, state)
                    write_index(cfg, state)
                    write_manifest(cfg, state)
                    prune_retired_files(cfg, state)
                if (client.has_credentials() or cfg.auth_mode == "cookie") and client.logged_in:
                    client.save_cookies()

                all_downloads_failed = (
                    stats["downloaded"] == 0
                    and stats["unchanged"] == 0
                    and stats["failed"] > 0
                )
                run_succeeded = page_ok and not stop.is_set() and not all_downloads_failed
                if run_succeeded and action == "scan":
                    touch_marker(cfg.last_run_file)
                elif not run_succeeded:
                    log.warning("本轮未完整成功，保留旧的 .last_run 时间戳以便健康检查告警")

                extra_skip = (
                    f"（另有连续失败达上限跳过 {stats['skipped_errors']}）"
                    if stats["skipped_errors"]
                    else ""
                )
                log.info(
                    "本轮完成：新增 %d，更新 %d，未变 %d，跳过 %d%s，失败 %d，"
                    "共下载 %d 篇 / %s（扫描 %d 页 / %d 条）",
                    stats["new"],
                    stats["updated"],
                    stats["unchanged"],
                    stats["skipped"],
                    extra_skip,
                    stats["failed"],
                    stats["downloaded"],
                    human_size(stats["bytes"]),
                    stats["pages"],
                    stats["seen"],
                )
                return run_succeeded, stats
            finally:
                if not cfg.dry_run and cfg.running_file.exists():
                    with contextlib.suppress(OSError):
                        cfg.running_file.unlink()

    if cfg.once:
        try:
            ok, _ = execute_pass(cfg)
            return 0 if ok else 1
        except InterruptedError:
            log.warning("操作已中断")
            return 130
        except Exception as exc:  # noqa: BLE001
            log.error("运行失败: %s", exc)
            return 1
        finally:
            for h in list(log.handlers):
                log.removeHandler(h)
                with contextlib.suppress(OSError):
                    h.close()

    if archive and not cfg.dry_run:
        run_scheduler(cfg, archive, stop, lambda settings, action: execute_pass(settings, action, lock_held=True))
        log.info("已退出")
        return 0

    if cfg.run_on_start:
        log.info("启动后先跑一轮（RUN_ON_START=1，设 0 可关闭）")
        try:
            execute_pass(cfg)
        except InterruptedError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("启动首轮失败，稍后按计划重试: %s", exc)

    while not stop.is_set():
        target = next_run_at(cfg)
        if target is None:
            stop.wait(30)
            continue
        log.info("下次运行：%s", target.isoformat(timespec="minutes"))
        while not stop.is_set():
            remaining = (target - datetime.now().astimezone()).total_seconds()
            if remaining <= 0:
                break
            stop.wait(min(remaining, 30))
        if stop.is_set():
            break
        try:
            execute_pass(cfg)
        except InterruptedError:
            break
        except Exception as exc:  # noqa: BLE001
            log.exception("本轮异常，60s 后继续等待下一轮: %s", exc)
            stop.wait(60)

    log.info("已退出")
    return 0
