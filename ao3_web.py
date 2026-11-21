"""Local web interface for the SQLite archive."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import zip_longest
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ao3_content import Fragment, reading_html
from ao3_diff import Difference, Export, chapter_heading, field_differences
from ao3_store import (
    ANCHOR_MODES, AO3_AUTH_MODES, BACKUP_ACTIONS, BACKUP_LOG, INTERVAL_UNITS,
    SCHEDULE_MODES, UNIT_SECONDS, WEEKDAY_LABELS, Archive, BackupSettings,
    DOWNLOAD_FORMATS, Version, compute_next_runs, missing_attachments,
    parse_duration, parse_hhmm, resolve_schedule, schedule_label,
)


LOG = logging.getLogger("ao3.web")
COOKIE = "ao3_archive_reader"
SESSION_COOKIE = "ao3_archive_session"
CSRF_COOKIE = "ao3_web_csrf"
PER_PAGE = 40
VIEWS = {"all": "全部作品", "since": "访问后更新", "unread": "未读更新", "failed": "抓取问题"}
SORTS = {
    "event": "最近变更",
    "ao3": "AO3 更新时间",
    "captured": "备份时间",
    "words": "字数最多",
    "title": "作品标题",
}
KINDS = {"new": "首次备份", "updated": "作品更新", "metadata": "资料变更", "file": "备份文件变化"}
TAG_GROUPS = {
    "原作": "fandoms", "关系标签": "relationships", "角色标签": "characters",
    "附加标签": "freeforms", "分级": "rating", "警告": "warnings", "分类": "categories",
}
TAG_SLUGS = {
    "原作": "fandoms", "关系标签": "relationships", "角色标签": "characters",
    "附加标签": "freeforms", "分级": "rating", "警告": "warnings", "分类": "categories",
}
MIMES = {
    "epub": "application/epub+zip", "pdf": "application/pdf",
    "mobi": "application/x-mobipocket-ebook", "azw3": "application/vnd.amazon.ebook",
}
PAGE_POLICY = "default-src 'none'; style-src 'self'; script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
READER_POLICY = PAGE_POLICY
DASHBOARD_POLICY = READER_POLICY + "; connect-src 'self'"
FILE_POLICY = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'; base-uri 'none'"
RUN_LABELS = {"startup": "启动抓取", "schedule": "定时抓取"}
RUN_RESULTS = {"success": "完成", "partial": "部分失败", "failed": "失败", "interrupted": "已中断"}
BACKUP_NOTICES = {
    "queued": "任务已提交。",
    "pending": "已有相同任务在排队或执行。",
    "saved": "抓取设置已保存。",
    "seen": "已将当前变更标为已查看。",
}


def _default_tz():
    if tz_name := os.environ.get("TZ", "").strip():
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo or timezone(timedelta(hours=8))


DISPLAY_TZ = _default_tz()


def date_label(value: int | str | None) -> str:
    if not value:
        return "未知"
    try:
        if isinstance(value, int):
            date = datetime.fromtimestamp(value, timezone.utc)
        else:
            date = datetime.fromisoformat(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=DISPLAY_TZ)
        return date.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return "未知"


def overview_query(
    view: str, q: str = "", sort: str = "event", *, author: str = "", tag: str = "",
    wip: bool | str = False, noted: bool | str = False, page: int | str = 1,
) -> dict[str, str]:
    query = {"view": view, "q": q, "sort": sort}
    if author:
        query["author"] = author
    if tag:
        query["tag"] = tag
    if wip is True or wip == "1":
        query["wip"] = "1"
    if noted is True or noted == "1":
        query["noted"] = "1"
    page_num = page if isinstance(page, int) else (int(page) if isinstance(page, str) and page.isdigit() else 1)
    if page_num > 1:
        query["page"] = str(page_num)
    return query


def home_url(
    view: str, q: str = "", sort: str = "event", *, author: str = "", tag: str = "",
    wip: bool | str = False, noted: bool | str = False, page: int | str = 1,
) -> str:
    return "/?" + urlencode(overview_query(
        view, q, sort, author=author, tag=tag, wip=wip, noted=noted, page=page,
    ))


def with_query(path: str, query: dict[str, str] | None) -> str:
    return path + ("?" + urlencode(query) if query else "")


def hidden_fields(query: dict[str, str]) -> str:
    return "".join(f'<input type="hidden" name="{key}" value="{escape(value)}">'
                   for key, value in query.items())


def _rating_badge(tags: dict[str, tuple[str, ...]], record: dict | None = None) -> str:
    items = tags.get("分级") or ()
    if not items and record:
        raw = record.get("rating")
        items = (raw,) if isinstance(raw, str) and raw else tuple(raw or ())
    if not items:
        return ""
    first = items[0]
    low = first.casefold()
    if "general" in low or "全年龄" in low or low == "g":
        short, cls = "G", "rating-g"
    elif "teen" in low or "青少年" in low or low == "t":
        short, cls = "T", "rating-t"
    elif "mature" in low or "成熟" in low or low == "m":
        short, cls = "M", "rating-m"
    elif "explicit" in low or "成人" in low or low == "e":
        short, cls = "E", "rating-e"
    else:
        short, cls = "NR", "rating-nr"
    return f'<span class="rating-badge {cls}" title="{escape(first)}">{short}</span>'


def page(
    title: str, contents: str, *, detail: bool = False, dashboard: bool = False,
    reading: bool = False, auth_mode: str = "app", active_nav: str = "",
    logout_proof: str = "",
) -> str:
    brand = home_url("all") if detail else "/"
    scripts = ['<script src="/reader.js"></script>']
    if dashboard:
        scripts.append('<script src="/backup.js" defer></script>')
    script_tags = "".join(scripts)
    body_class = ' class="reader-page"' if reading else ""
    theme_picker = """<label class="reader-theme" title="切换配色方案"><i class="swatch-dot" aria-hidden="true"></i><span class="sr-only">配色</span>
<select id="reader-theme"><option value="system">跟随系统</option>
<optgroup label="浅色方案"><option value="day">羊皮纸（日间）</option><option value="sepia">旧书页（暖棕）</option>
<option value="crimson">经典绯红（AO3）</option><option value="sage">鼠尾草（护眼绿）</option>
<option value="lavender">薰衣草（柔紫）</option></optgroup>
<optgroup label="深色方案"><option value="night">深林夜读（夜间）</option><option value="dusk">暮夜星辰（深蓝紫）</option>
<option value="midnight">暗夜森林（深墨绿）</option></optgroup></select></label>"""
    nav_items = (
        ("overview", "/", "总览"),
        ("schedule", "/schedule", "计划"),
        ("logs", "/logs", "日志"),
        ("account", "/account", "账号安全"),
    )
    nav_links = ""
    logout_btn = ""
    density_switch = ""
    confirm_modal = ""
    if auth_mode == "app":
        links = "".join(
            f'<a class="top-link" href="{href}"'
            f'{" aria-current=\"page\"" if active_nav == key else ""}>{label}</a>'
            for key, href, label in nav_items
        )
        nav_links = f'<nav class="top-nav" aria-label="主导航">{links}</nav>'
        if active_nav == "overview":
            density_switch = (
                '<div class="density-switch" role="group" aria-label="列表密度">'
                '<button type="button" data-density="compact" class="is-active">紧凑</button>'
                '<button type="button" data-density="cozy">舒适</button></div>'
            )
        if logout_proof:
            logout_btn = (
                f'<form id="site-logout-form" class="top-nav-logout" method="post" action="/logout">'
                f'{hidden_fields({"proof": logout_proof})}'
                '<button class="top-link is-logout" type="submit" title="退出当前站点"'
                ' data-confirm-trigger="site"'
                ' data-confirm-title="确认退出当前站点？"'
                ' data-confirm-body="退出后将锁定浏览界面，需重新输入站点访问密码才能进入。"'
                ' data-confirm-ok="确认退出站点">退出</button></form>'
            )
            confirm_modal = """<div id="confirm-popover" class="confirm-popover" role="dialog" aria-labelledby="confirm-title" hidden>
  <p id="confirm-title" class="confirm-title"></p>
  <p id="confirm-body" class="muted confirm-body"></p>
  <div class="confirm-actions">
    <button class="button small" type="button" data-confirm-cancel>取消</button>
    <button class="button small danger" type="button" data-confirm-accept>确认</button>
  </div>
</div>"""
    brand_href = escape(brand if auth_mode == "app" else "#")
    main_cls = "wrap reader-main" if reading else "wrap"
    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · AO3 书签备份</title>{script_tags}<link rel="stylesheet" href="/archive.css"></head>
<body{body_class}><a class="skip" href="#main">跳到内容</a>
<header class="top"><div class="wrap top-row">
<div class="brand-group"><a class="brand" href="{brand_href}"><span>AO3</span> 书签备份</a>{nav_links}</div>
<div class="top-actions">{density_switch}{theme_picker}{logout_btn}</div></div></header>
<main class="{main_cls}" id="main">{contents}</main>{confirm_modal}</body></html>"""


def work_title(wid: str, record: dict, saved_title: str = "") -> str:
    return str(record.get("title") or saved_title or f"AO3 作品 {wid}")


def file_url(wid: str, vid: int, ext: str, *, download: bool = False) -> str:
    return f"/files/{wid}/{vid}/{ext}" + ("?download=1" if download else "")


def article_url(wid: str, vid: int) -> str:
    return f"/works/{wid}/versions/{vid}"


def download_filename(wid: str, vid: int, title: str, ext: str) -> str:
    clean = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", title).strip(" .")
    clean = re.sub(r"\s+", " ", clean)[:80].rstrip(" .")
    ascii_fallback = f'filename="ao3-{wid}-{vid}.{ext}"'
    if not clean:
        return f"attachment; {ascii_fallback}"
    return f"attachment; {ascii_fallback}; filename*=UTF-8''" + quote(f"{wid}_{clean}.{ext}", safe="")


def download_options(
    wid: str, vid: int, *, has_article: bool, formats: Iterable[str],
) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    if has_article:
        base = article_url(wid, vid)
        items.append(("Markdown (.md)", f"{base}/md"))
        items.append(("JSON (.json)", f"{base}/json"))
    for ext in sorted(formats, key=lambda item: (item != "html", item)):
        label = "原始 HTML (.html)" if ext == "html" else f"{ext.upper()} (.{ext})"
        items.append((label, file_url(wid, vid, ext, download=True)))
    return items


def download_menu(
    wid: str, vid: int, *, has_article: bool, formats: Iterable[str], compact: bool = False,
) -> str:
    items = download_options(wid, vid, has_article=has_article, formats=formats)
    if not items:
        return ""
    if len(items) == 1:
        label, url = items[0]
        cls = "text-link" if compact else "button"
        return f'<a class="{cls}" href="{escape(url)}">下载 {escape(label)}</a>'
    links = "".join(
        f'<a href="{escape(url)}" role="menuitem">{escape(label)}</a>'
        for label, url in items
    )
    trigger_cls = "text-link download-trigger is-compact" if compact else "button download-trigger"
    return (f'<details class="download-menu">'
            f'<summary class="{trigger_cls}" aria-haspopup="menu">下载格式 <span aria-hidden="true">▾</span></summary>'
            f'<div class="download-list" role="menu">{links}</div></details>')


def names(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    values = (value,) if isinstance(value, str) else value or ()
    return tuple(dict.fromkeys(name.strip() for name in values if name.strip()))


def filter_link(name: str, field: str, *, sort: str = "event") -> str:
    url = escape(home_url("all", sort=sort, **{field: name}))
    return f'<a href="{url}">{escape(name)}</a>'


def render_tag_groups(
    tags: dict[str, tuple[str, ...]], *, sort: str = "event", collapse_limit: int = 0,
    compact_meta: bool = False,
) -> str:
    groups = []
    meta_items: list[tuple[str, str]] = []
    meta_labels = {"分级", "分类", "警告"} if compact_meta else set()
    for label, items in tags.items():
        if not items:
            continue
        if label in meta_labels:
            slug = TAG_SLUGS.get(label, "")
            meta_items.extend((slug, item) for item in items)
            continue
        slug = TAG_SLUGS.get(label, "")
        cls = f' class="tag-group-{slug}"' if slug else ""
        if collapse_limit and label == "附加标签" and len(items) > collapse_limit:
            head = "".join(f'<li>{filter_link(item, "tag", sort=sort)}</li>' for item in items[:collapse_limit])
            tail = "".join(f'<li>{filter_link(item, "tag", sort=sort)}</li>' for item in items[collapse_limit:])
            extra = len(items) - collapse_limit
            body = (f'<ul class="tag-links" role="list">{head}</ul>'
                    f'<details class="tag-more"><summary><span class="tag-more-closed">+ 展开其余 {extra} 个标签</span>'
                    f'<span class="tag-more-open">− 收起多余标签</span></summary>'
                    f'<ul class="tag-links" role="list">{tail}</ul></details>')
        else:
            links = "".join(f'<li>{filter_link(item, "tag", sort=sort)}</li>' for item in items)
            body = f'<ul class="tag-links" role="list">{links}</ul>'
        groups.append(f'<div{cls}><dt>{label}</dt><dd>{body}</dd></div>')
    if meta_items:
        links = "".join(
            f'<li class="tag-kind-{slug}">{filter_link(item, "tag", sort=sort)}</li>'
            for slug, item in meta_items
        )
        groups.append(f'<div class="tag-group-meta"><dt>属性</dt><dd><ul class="tag-links" role="list">{links}</ul></dd></div>')
    return f'<dl class="tag-groups">{"".join(groups)}</dl>' if groups else ""


def backup_proof(reader_id: str) -> str:
    return hmac.new(bytes.fromhex(reader_id), b"backup-control", "sha256").hexdigest()


def _field_first(fields: dict[str, object], key: str, default: str = "") -> str:
    value = fields.get(key, default)
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else default
    return str(value)


def _field_many(fields: dict[str, object], key: str) -> list[str]:
    value = fields.get(key, ())
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)] if value != "" else []


def submitted_settings(fields: dict[str, object], *, base: BackupSettings | None = None) -> BackupSettings:
    prior = base or BackupSettings()
    try:
        delay = float(_field_first(fields, "delay", ""))
        retries = int(_field_first(fields, "retries", ""))
        max_errors = int(_field_first(fields, "max_errors", ""))
    except ValueError as exc:
        raise ValueError("请检查请求间隔、尝试次数和连续失败上限") from exc
    if not math.isfinite(delay) or not 0.1 <= delay <= 600:
        raise ValueError("请求间隔应在 0.1 至 600 秒之间")
    if not 1 <= retries <= 20 or not 1 <= max_errors <= 100:
        raise ValueError("请求尝试次数应为 1 至 20，连续失败上限应为 1 至 100")

    bookmark_user = prior.bookmark_user
    if "bookmark_user" in fields:
        bookmark_user = _field_first(fields, "bookmark_user", "").strip()
        if not bookmark_user:
            raise ValueError("请填写目标书签用户名（BOOKMARK_USER）")

    formats = prior.formats
    formats_configured = prior.formats_configured
    if "formats_present" in fields or "formats" in fields:
        selected = [
            f.strip().lower() for f in _field_many(fields, "formats")
            if f.strip().lower() in DOWNLOAD_FORMATS
        ]
        formats = tuple(dict.fromkeys(selected))
        formats_configured = True

    run_on_start = prior.run_on_start
    prefer_ipv4_opt = prior.prefer_ipv4
    if "advanced_present" in fields:
        run_on_start = _field_first(fields, "run_on_start", "") == "1"
        prefer_ipv4_opt = _field_first(fields, "prefer_ipv4", "") == "1"

    stop_idle = prior.stop_after_idle_pages
    if "stop_after_idle_pages" in fields:
        try:
            stop_idle = int(_field_first(fields, "stop_after_idle_pages", "0"))
        except ValueError as exc:
            raise ValueError("连续无变化停止页数应为非负整数") from exc
        if not 0 <= stop_idle <= 500:
            raise ValueError("连续无变化停止页数应在 0 至 500 之间")

    max_pages = prior.max_pages
    if "max_pages" in fields:
        try:
            max_pages = int(_field_first(fields, "max_pages", "200"))
        except ValueError as exc:
            raise ValueError("最大翻页数应为整数") from exc
        if not 1 <= max_pages <= 5000:
            raise ValueError("最大翻页数应在 1 至 5000 之间")

    timeout = prior.timeout
    if "timeout" in fields:
        try:
            timeout = int(_field_first(fields, "timeout", "60"))
        except ValueError as exc:
            raise ValueError("请求超时时间应为整数") from exc
        if not 5 <= timeout <= 600:
            raise ValueError("请求超时时间应在 5 至 600 秒之间")

    if "schedule_mode" not in fields:
        hour, minute = parse_hhmm(_field_first(fields, "run_at", ""))
        interval = parse_duration(_field_first(fields, "interval", "").strip())
        seconds = interval.total_seconds() if interval is not None else 0
        if interval is not None and not 1 <= seconds <= 366 * 86400:
            raise ValueError("抓取间隔应在 1 秒至 366 天之间")
        return BackupSettings(
            run_at=f"{hour:02d}:{minute:02d}",
            interval_seconds=seconds,
            delay=delay,
            retries=retries,
            max_errors=max_errors,
            schedule_mode="interval" if seconds > 0 else "daily",
            bookmark_user=bookmark_user,
            formats=formats,
            formats_configured=formats_configured,
            run_on_start=run_on_start,
            stop_after_idle_pages=stop_idle,
            max_pages=max_pages,
            timeout=timeout,
            prefer_ipv4=prefer_ipv4_opt,
        )

    mode = _field_first(fields, "schedule_mode").strip()
    if mode not in SCHEDULE_MODES:
        raise ValueError("请选择有效的调度方式")

    raw_run_at = _field_first(fields, "run_at", "03:30")
    if mode == "manual":
        try:
            hour, minute = parse_hhmm(raw_run_at)
        except ValueError:
            hour, minute = 3, 30
    else:
        hour, minute = parse_hhmm(raw_run_at)
    run_at = f"{hour:02d}:{minute:02d}"

    unit = _field_first(fields, "interval_unit", "hour").strip()
    if unit not in INTERVAL_UNITS:
        if mode == "interval":
            raise ValueError("请选择有效的间隔单位")
        unit = "hour"
    anchor = _field_first(fields, "anchor_mode", "fixed").strip()
    if anchor not in ANCHOR_MODES:
        if mode == "interval":
            raise ValueError("请选择有效的时间基准")
        anchor = "fixed"

    seconds = 0.0
    interval_val = 6.0
    raw_interval = _field_first(fields, "interval_value", "").strip()
    if mode == "interval":
        try:
            interval_val = float(raw_interval)
        except ValueError as exc:
            raise ValueError("请输入有效的抓取间隔数字") from exc
        if not math.isfinite(interval_val) or interval_val <= 0:
            raise ValueError("抓取间隔数字必须大于 0")
        seconds = interval_val * UNIT_SECONDS[unit]
        if not 60 <= seconds <= 366 * 86400:
            raise ValueError("抓取间隔应在 1 分钟至 366 天之间")
    elif raw_interval:
        try:
            parsed_val = float(raw_interval)
            if math.isfinite(parsed_val) and parsed_val > 0:
                interval_val = parsed_val
        except ValueError:
            pass

    parsed_days: list[int] = []
    for item in _field_many(fields, "weekdays"):
        try:
            day = int(item)
        except ValueError:
            continue
        if 0 <= day <= 6 and day not in parsed_days:
            parsed_days.append(day)
    if mode == "weekly" and not parsed_days:
        raise ValueError("每周执行请至少选择一个星期")
    weekdays = tuple(sorted(parsed_days)) if parsed_days else (6,)

    month_day = 1
    raw_mday = _field_first(fields, "month_day", "1").strip()
    if mode == "monthly":
        try:
            month_day = int(raw_mday)
        except ValueError as exc:
            raise ValueError("每月执行日期应在 1 至 31 日之间") from exc
        if not 1 <= month_day <= 31:
            raise ValueError("每月执行日期应在 1 至 31 日之间")
    elif raw_mday:
        try:
            parsed_mday = int(raw_mday)
            if 1 <= parsed_mday <= 31:
                month_day = parsed_mday
        except ValueError:
            pass

    return BackupSettings(
        run_at=run_at,
        interval_seconds=seconds,
        delay=delay,
        retries=retries,
        max_errors=max_errors,
        schedule_mode=mode,
        interval_value=interval_val,
        interval_unit=unit,
        anchor_mode=anchor,
        weekdays=weekdays,
        month_day=month_day,
        bookmark_user=bookmark_user,
        formats=formats,
        formats_configured=formats_configured,
        run_on_start=run_on_start,
        stop_after_idle_pages=stop_idle,
        max_pages=max_pages,
        timeout=timeout,
        prefer_ipv4=prefer_ipv4_opt,
    )


def format_preview_runs(settings: BackupSettings, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    runs = compute_next_runs(settings, count=3, now=now)
    if not runs:
        return "当前已关闭自动定时，仅通过「立即刷新」或「重试失败或缺失项」手动触发。"
    today = now.date()
    tomorrow = today + timedelta(days=1)
    parts: list[str] = []
    for dt in runs:
        d = dt.date()
        if d == today:
            parts.append(f"今天 {dt:%H:%M}")
        elif d == tomorrow:
            parts.append(f"明天 {dt:%H:%M}")
        elif d.year == today.year:
            parts.append(f"{dt:%m-%d} {dt:%H:%M}")
        else:
            parts.append(f"{dt:%Y-%m-%d} {dt:%H:%M}")
    return f"按当前计划，预计接下来 {len(parts)} 次：{' · '.join(parts)}"


def backup_status(
    scheduler: dict, active: list[dict], last: dict | None, settings: BackupSettings | None = None,
) -> dict[str, str]:
    def label(job: dict) -> str:
        return RUN_LABELS.get(job["source"], BACKUP_ACTIONS[job["action"]])

    running = next((job for job in active if job["status"] == "running"), None)
    online = bool(scheduler) and time.time() - scheduler["seen_at"] < 45
    awaiting = bool(scheduler.get("awaiting_setup")) and not running
    tone = ("running" if running else
            "waiting" if awaiting else
            "online" if online else
            "offline" if scheduler else "waiting")
    state = (f"执行中 · {label(running)}（{date_label(running['started_at'])} 开始）" if running else
             "等待完成目标书签初始化" if awaiting else
             "备份进程空闲" if online and not active else "等待备份进程领取任务" if online else
             "备份进程暂未响应" if scheduler else "等待备份进程启动")
    queued = [label(job) for job in active if job["status"] == "queued"]
    if queued:
        state += " · 排队中 · " + "、".join(queued)
    previous = (f"上次运行 · {date_label(last['finished_at'])} · {label(last)}{RUN_RESULTS[last['status']]}"
                if last else "暂无运行记录")
    if last and last["summary"]:
        previous += " · " + last["summary"]
    is_manual = bool(settings and resolve_schedule(settings)["mode"] == "manual")
    due = scheduler.get("next_run", 0)
    if awaiting:
        upcoming = "下次计划 · 请先配置目标书签用户名"
    elif is_manual:
        upcoming = "下次计划 · 本轮结束后保持手动" if running else "下次计划 · 已关闭自动定时（仅手动）"
    else:
        upcoming = (f"下次计划 · {date_label(due)}" if (online or running) and due > time.time() else
                    "下次计划 · 本轮结束后更新" if running else "下次计划 · 等待调度")
    return {"state": state, "previous": previous, "upcoming": upcoming, "tone": tone}


def recent_backup_log(archive: Archive, *, limit: int = 200) -> str:
    path = archive.path.parent / BACKUP_LOG
    try:
        with path.open("rb") as log_file:
            size = log_file.seek(0, os.SEEK_END)
            read_bytes = 65536
            start = max(0, size - read_bytes)
            log_file.seek(start)
            data = log_file.read(read_bytes)
    except FileNotFoundError:
        return "等待备份进程输出日志。"
    except OSError:
        return "暂时无法读取备份日志。"
    if start and b"\n" in data:
        data = data.split(b"\n", 1)[1]
    lines = data.decode("utf-8", errors="replace").splitlines()[-limit:]
    log = "\n".join(lines)
    log = re.sub(r"(?i)(\b(?:https?|socks\w*)://)[^/\s@]+@", r"\1***@", log)
    return log or "等待备份进程输出日志。"


def _initial_formats(archive: Archive, settings: BackupSettings, scheduler: dict | None = None) -> set[str]:
    if settings.formats_configured:
        return {f for f in settings.formats if f in DOWNLOAD_FORMATS}
    sched_formats = (scheduler or {}).get("formats")
    if sched_formats is not None:
        return {str(f).lower() for f in sched_formats if str(f).lower() in DOWNLOAD_FORMATS}
    raw_env = os.environ.get("FORMATS", "").strip().lower()
    if raw_env and raw_env != "none":
        return {part.strip() for part in raw_env.split(",") if part.strip() in DOWNLOAD_FORMATS}
    return set()


def _initial_bookmark_user(archive: Archive, settings: BackupSettings) -> str:
    return (
        settings.bookmark_user
        or archive.stored_bookmark_user()
        or os.environ.get("BOOKMARK_USER", "").strip()
        or os.environ.get("AO3_USER", "").strip()
    )


def _schedule_form_html(
    archive: Archive, reader: dict, settings: BackupSettings, *,
    post_action: str = "/schedule", form_values: dict[str, object] | None = None,
    scheduler: dict | None = None,
) -> str:
    proof_field = hidden_fields({"proof": backup_proof(reader["id"])})
    vals = form_values or {}
    sched = resolve_schedule(settings)
    mode = _field_first(vals, "schedule_mode", sched["mode"])
    if mode not in SCHEDULE_MODES:
        mode = sched["mode"]
    raw_val = sched["value"]
    default_ival = str(int(raw_val)) if float(raw_val).is_integer() else f"{raw_val:g}"
    interval_val_str = _field_first(vals, "interval_value", default_ival)
    interval_unit = _field_first(vals, "interval_unit", sched["unit"])
    if interval_unit not in INTERVAL_UNITS:
        interval_unit = sched["unit"]
    anchor_mode = _field_first(vals, "anchor_mode", sched["anchor_mode"])
    if anchor_mode not in ANCHOR_MODES:
        anchor_mode = sched["anchor_mode"]
    run_at_str = _field_first(vals, "run_at", sched["run_at"])
    if "weekdays" in vals:
        selected_weekdays = {int(x) for x in _field_many(vals, "weekdays") if x.isdigit() and 0 <= int(x) <= 6}
    else:
        selected_weekdays = set(sched["weekdays"])
    try:
        month_day = min(31, max(1, int(_field_first(vals, "month_day", str(sched["month_day"])))))
    except ValueError:
        month_day = sched["month_day"]
    delay_str = _field_first(vals, "delay", str(settings.delay))
    retries_str = _field_first(vals, "retries", str(settings.retries))
    max_errors_str = _field_first(vals, "max_errors", str(settings.max_errors))

    bookmark_user_str = _field_first(vals, "bookmark_user", _initial_bookmark_user(archive, settings))
    if "formats_present" in vals or "formats" in vals:
        selected_formats = {f.strip().lower() for f in _field_many(vals, "formats") if f.strip().lower() in DOWNLOAD_FORMATS}
    else:
        selected_formats = _initial_formats(archive, settings, scheduler)
    if "advanced_present" in vals:
        run_on_start_checked = _field_first(vals, "run_on_start", "") == "1"
        prefer_ipv4_checked = _field_first(vals, "prefer_ipv4", "") == "1"
    else:
        run_on_start_checked = settings.run_on_start
        prefer_ipv4_checked = settings.prefer_ipv4
    stop_idle_str = _field_first(vals, "stop_after_idle_pages", str(settings.stop_after_idle_pages))
    max_pages_str = _field_first(vals, "max_pages", str(settings.max_pages))
    timeout_str = _field_first(vals, "timeout", str(settings.timeout))

    mode_buttons = "".join(
        f'<label class="schedule-mode-pill"><input type="radio" name="schedule_mode" value="{key}"'
        f'{" checked" if mode == key else ""}><span>{label}</span></label>'
        for key, label in (
            ("interval", "按固定间隔"),
            ("daily", "每天"),
            ("weekly", "每周"),
            ("monthly", "每月"),
            ("manual", "仅手动"),
        )
    )
    unit_options = "".join(
        f'<option value="{u}"{" selected" if interval_unit == u else ""}>{u_label}</option>'
        for u, u_label in (("minute", "分钟"), ("hour", "小时"), ("day", "天"))
    )
    anchor_options = "".join(
        f'<option value="{a}"{" selected" if anchor_mode == a else ""}>{a_label}</option>'
        for a, a_label in (("fixed", "对齐基准时刻（不漂移）"), ("after", "每次结束后顺延"))
    )
    weekday_chips = "".join(
        f'<label class="weekday-chip" aria-label="周{label}"><input type="checkbox" name="weekdays" value="{idx}"'
        f'{" checked" if idx in selected_weekdays else ""}><span>{label}</span></label>'
        for idx, label in enumerate(WEEKDAY_LABELS)
    )
    month_options = "".join(
        f'<option value="{d}"{" selected" if month_day == d else ""}>'
        f'{"31 日（每月最后一天）" if d == 31 else f"{d} 日（小月月末）" if d in (29, 30) else f"{d} 日"}'
        f"</option>"
        for d in range(1, 32)
    )
    format_chips = "".join(
        f'<label class="format-chip"><input type="checkbox" name="formats" value="{fmt}"'
        f'{" checked" if fmt in selected_formats else ""}><span>{fmt.upper()}</span></label>'
        for fmt in ("html", "epub", "pdf", "mobi", "azw3")
    )
    hide_run_at = mode == "manual" or (mode == "interval" and anchor_mode == "after")
    run_at_titles = {
        "interval": ("基准时刻", "固定按此时刻对齐间隔网格，避免越跑越晚"),
        "daily": ("每日执行时间", "每天固定在此时刻启动扫描"),
        "weekly": ("执行时间", "所选星期几的此时刻启动扫描"),
        "monthly": ("执行时间", "每月所选日期的此时刻启动扫描"),
        "manual": ("基准时刻", ""),
    }
    run_at_title, run_at_hint = run_at_titles[mode]
    anchor_hint = "以基准时刻切分固定周期，不受单轮耗时影响" if anchor_mode == "fixed" else "待上轮完成后再等待对应间隔，起始点逐次顺延"
    preview_settings = settings
    if form_values is None:
        preview_text = format_preview_runs(settings)
    else:
        try:
            preview_settings = submitted_settings(vals, base=settings)
        except ValueError:
            pass
        preview_text = format_preview_runs(preview_settings)

    return f"""<form class="backup-form" id="backup-settings-form" method="post" action="{escape(post_action)}">{proof_field}
  <input type="hidden" name="formats_present" value="1">
  <input type="hidden" name="advanced_present" value="1">
  <div class="settings-groups">
    <fieldset class="settings-group is-wide">
      <legend>目标与附件</legend>
      <div class="settings-fields is-target-row">
        <label><span class="field-label">目标书签用户名（BOOKMARK_USER）</span>
          <input name="bookmark_user" value="{escape(bookmark_user_str)}" placeholder="填写 AO3 个人主页中的用户名" required>
          <small>即 AO3 书签 URL 中的用户名；需要调整登录账号或站点密码请前往 <a class="text-link" href="/account">账号安全</a></small>
        </label>
        <div class="format-picker">
          <span class="field-label">额外下载官方附件格式</span>
          <div class="format-chips" role="group" aria-label="附件格式">{format_chips}</div>
          <small>全不选时仅保存可在线阅读、版本对比及导出 Markdown/JSON 的正文数据</small>
        </div>
      </div>
    </fieldset>
    <fieldset class="settings-group">
      <legend>调度计划</legend>
      <div class="schedule-stack">
        <div class="schedule-modes" role="radiogroup" aria-label="调度方式">{mode_buttons}</div>
        <div class="settings-fields schedule-sub" data-schedule-panel="interval"{' hidden' if mode != 'interval' else ''}>
          <label><span class="field-label">抓取间隔</span>
            <span class="interval-pair">
              <input type="number" name="interval_value" id="backup-interval-value" value="{escape(interval_val_str)}" min="1" max="527040" step="any">
              <select name="interval_unit" id="backup-interval-unit" aria-label="间隔单位">{unit_options}</select>
            </span>
            <small>支持 1 分钟至 366 天</small>
          </label>
          <label><span class="field-label">时间基准</span>
            <select name="anchor_mode" id="backup-anchor-mode">{anchor_options}</select>
            <small id="backup-anchor-hint">{escape(anchor_hint)}</small>
          </label>
        </div>
        <div class="schedule-sub" data-schedule-panel="weekly"{' hidden' if mode != 'weekly' else ''}>
          <span class="field-label">每周执行日</span>
          <div class="weekday-chips" role="group" aria-label="选择星期几">{weekday_chips}</div>
          <small>可多选，未勾选时无法保存每周计划</small>
        </div>
        <div class="settings-fields schedule-sub" data-schedule-panel="monthly"{' hidden' if mode != 'monthly' else ''}>
          <label><span class="field-label">每月执行日</span>
            <select name="month_day" id="backup-month-day">{month_options}</select>
            <small>29~31 日遇小月时自动在当月最后一天执行</small>
          </label>
        </div>
        <div class="settings-fields" id="backup-run-at-wrap"{' hidden' if hide_run_at else ''}>
          <label><span class="field-label" id="backup-run-at-title">{escape(run_at_title)}</span>
            <input type="time" name="run_at" id="backup-run-at" value="{escape(run_at_str)}">
            <small id="backup-run-at-hint">{escape(run_at_hint)}</small>
          </label>
        </div>
        <p class="schedule-note" data-schedule-panel="manual"{' hidden' if mode != 'manual' else ''}>已关闭自动定时扫描，仅在点击「立即刷新」或「重试失败或缺失项」时执行。</p>
        <p class="schedule-preview" id="backup-schedule-preview">{escape(preview_text)}</p>
      </div>
    </fieldset>
    <fieldset class="settings-group">
      <legend>请求与容错</legend>
      <div class="settings-fields is-three">
        <label><span class="field-label">请求间隔</span>
          <span class="input-unit"><input type="number" name="delay" value="{escape(delay_str)}" min="0.1" max="600" step="any" required><span>秒</span></span>
          <small>建议 ≥ 1.5 秒</small>
        </label>
        <label><span class="field-label">尝试次数</span>
          <span class="input-unit"><input type="number" name="retries" value="{escape(retries_str)}" min="1" max="20" required><span>次</span></span>
          <small>含首次请求</small>
        </label>
        <label><span class="field-label">连续失败上限</span>
          <span class="input-unit"><input type="number" name="max_errors" value="{escape(max_errors_str)}" min="1" max="100" required><span>次</span></span>
          <small>达上限暂停重试</small>
        </label>
      </div>
      <details class="advanced-settings">
        <summary>高级抓取策略</summary>
        <div class="settings-fields is-three">
          <label><span class="field-label">最大翻页数</span>
            <span class="input-unit"><input type="number" name="max_pages" value="{escape(max_pages_str)}" min="1" max="5000" required><span>页</span></span>
            <small>每页 20 条书签</small>
          </label>
          <label><span class="field-label">无更新提前停止</span>
            <span class="input-unit"><input type="number" name="stop_after_idle_pages" value="{escape(stop_idle_str)}" min="0" max="500" required><span>页</span></span>
            <small>0 为扫完全部书签</small>
          </label>
          <label><span class="field-label">单次请求超时</span>
            <span class="input-unit"><input type="number" name="timeout" value="{escape(timeout_str)}" min="5" max="600" required><span>秒</span></span>
            <small>默认 60 秒</small>
          </label>
        </div>
        <div class="advanced-checks">
          <label class="inline-check"><input type="checkbox" name="run_on_start" value="1"{' checked' if run_on_start_checked else ''}><span>容器启动后先执行一轮抓取</span></label>
          <label class="inline-check"><input type="checkbox" name="prefer_ipv4" value="1"{' checked' if prefer_ipv4_checked else ''}><span>优先 IPv4 解析（规避部分代理 IPv6 超时）</span></label>
        </div>
      </details>
    </fieldset>
  </div>
  <div class="backup-form-footer">
    <span class="muted">设置保存至数据库，将在调度器空闲时自动生效。</span>
    <button class="button primary" type="submit" name="action" value="settings">保存设置</button>
  </div>
</form>"""


def backup_panel(
    archive: Archive, reader: dict, context: dict[str, str], settings: BackupSettings,
    status: dict[str, str], notice: str, *, error: str = "", form_values: dict[str, object] | None = None,
    scheduler: dict | None = None,
) -> str:
    proof_field = hidden_fields({"proof": backup_proof(reader["id"])})
    post_url = escape(with_query("/backup", context))
    list_url = escape(home_url(**context))
    feedback = ""
    if error:
        feedback = f'<p class="warning notice" role="alert">{escape(error)}</p>'
    elif notice in BACKUP_NOTICES:
        feedback = f'<p class="notice is-info" role="status">{BACKUP_NOTICES[notice]}</p>'
    return f"""<section class="backup-panel is-strip" id="backup" aria-labelledby="backup-heading">
<div class="backup-heading">
  <div class="backup-title-wrap" role="status">
    <h2 class="sr-only" id="backup-heading">抓取管理</h2>
    <span class="backup-status-inline" id="backup-status" data-status="{escape(status['tone'])}">
      <i class="status-dot" aria-hidden="true"></i>
      <span id="backup-state">{escape(status['state'])}</span>
    </span>
    <span class="strip-sep" aria-hidden="true">·</span>
    <span class="muted" id="backup-previous">{escape(status['previous'])}</span>
    <span class="strip-sep" aria-hidden="true">·</span>
    <span class="muted" id="backup-upcoming">{escape(status['upcoming'])}</span>
    <a class="text-link refresh-hint" id="backup-refresh-hint" href="{list_url}" hidden>有新归档完成，点击刷新列表</a>
  </div>
  <form class="backup-actions" method="post" action="{post_url}">{proof_field}
    <button class="button small" type="submit" name="action" value="retry">重试失败或缺失项</button>
    <button class="button small primary" type="submit" name="action" value="scan">立即刷新</button>
    <a class="button small js-hide" id="backup-refresh-list" href="{list_url}">更新列表</a>
  </form>
</div>
{feedback}</section>"""


def schedule_html(
    archive: Archive, reader: dict, *, notice: str = "", error: str = "",
    form_values: dict[str, object] | None = None,
) -> str:
    scheduler, settings, active_jobs, last = archive.backup_dashboard()
    status = backup_status(scheduler, active_jobs, last, settings)
    feedback = ""
    if error:
        feedback = f'<p class="warning notice" role="alert">{escape(error)}</p>'
    elif notice == "saved":
        feedback = f'<p class="notice is-info" role="status">{BACKUP_NOTICES["saved"]}</p>'
    form_html = _schedule_form_html(
        archive, reader, settings, post_action="/schedule", form_values=form_values, scheduler=scheduler,
    )
    return page("抓取计划与设置", f"""
<div class="page-heading">
  <div class="backup-title-wrap">
    <h1>抓取计划与设置</h1>
    <span class="backup-status-inline" id="backup-status" data-status="{escape(status['tone'])}">
      <i class="status-dot" aria-hidden="true"></i>
      <span id="backup-state">{escape(status['state'])}</span>
    </span>
  </div>
</div>
{feedback}
<section class="backup-panel" aria-label="计划与状态摘要">
  <div class="backup-meta" role="status">
    <p class="muted" id="backup-previous">{escape(status['previous'])}</p>
    <p class="muted" id="backup-upcoming">{escape(status['upcoming'])}</p>
  </div>
  {form_html}
</section>""", dashboard=True, active_nav="schedule", logout_proof=backup_proof(reader["id"]))


def logs_html(archive: Archive, reader: dict, *, notice: str = "") -> str:
    scheduler, settings, active_jobs, last = archive.backup_dashboard()
    status = backup_status(scheduler, active_jobs, last, settings)
    proof_field = hidden_fields({"proof": backup_proof(reader["id"])})
    feedback = ""
    if notice in BACKUP_NOTICES:
        feedback = f'<p class="notice is-info" role="status">{BACKUP_NOTICES[notice]}</p>'
    return page("运行日志", f"""
<div class="page-heading">
  <div class="backup-title-wrap">
    <h1>运行日志</h1>
    <span class="backup-status-inline" id="backup-status" data-status="{escape(status['tone'])}">
      <i class="status-dot" aria-hidden="true"></i>
      <span id="backup-state">{escape(status['state'])}</span>
    </span>
  </div>
  <form class="backup-actions" method="post" action="/backup?from=logs">{proof_field}
    <button class="button primary" type="submit" name="action" value="scan">立即刷新</button>
    <button class="button" type="submit" name="action" value="retry">重试失败或缺失项</button>
  </form>
</div>
{feedback}
<section class="backup-panel" aria-label="日志实时终端">
  <div class="backup-meta" role="status">
    <p class="muted" id="backup-previous">{escape(status['previous'])}</p>
    <p class="muted" id="backup-upcoming">{escape(status['upcoming'])}</p>
  </div>
  <div class="backup-log-card is-page">
    <div class="backup-log-head">
      <span>最近 200 行输出</span>
      <span class="muted">自动轮询更新</span>
    </div>
    <pre id="backup-log" class="backup-log" tabindex="0">{escape(recent_backup_log(archive, limit=200))}</pre>
  </div>
</section>""", dashboard=True, active_nav="logs", logout_proof=backup_proof(reader["id"]))


def _pagination_html(context: dict[str, str], page_num: int, total_pages: int) -> str:
    if total_pages <= 1:
        return ""
    pages: list[int | None] = []
    for p in range(1, total_pages + 1):
        if p == 1 or p == total_pages or abs(p - page_num) <= 2:
            pages.append(p)
        elif pages and pages[-1] is not None:
            pages.append(None)

    def page_link(target_page: int, label: str, *, current: bool = False, disabled: bool = False) -> str:
        if disabled:
            return f'<span class="page-item is-disabled">{label}</span>'
        if current:
            return f'<span class="page-item is-current" aria-current="page">{label}</span>'
        url = escape(home_url(**{**context, "page": target_page}))
        return f'<a class="page-item" href="{url}">{label}</a>'

    items = [page_link(page_num - 1, "上一页", disabled=page_num <= 1)]
    for p in pages:
        items.append('<span class="page-ellipsis">…</span>' if p is None
                     else page_link(p, str(p), current=p == page_num))
    items.append(page_link(page_num + 1, "下一页", disabled=page_num >= total_pages))
    return f'<nav class="pagination" aria-label="作品分页">{"".join(items)}</nav>'


def overview_html(
    archive: Archive, reader: dict, view: str, q: str, sort: str, *, author: str = "", tag: str = "",
    wip: bool = False, noted: bool = False, page_num: int = 1,
    notice: str = "", backup_error: str = "", backup_values: dict[str, object] | None = None,
) -> str:
    scheduler, settings, active_jobs, last = archive.backup_dashboard()
    configured = scheduler.get("formats")
    rows = []
    words = q.casefold().split()
    author_key, tag_key = author.casefold(), tag.casefold()
    for row in archive.overview(reader):
        record, fields = row["record"], row["article_fields"]
        title = work_title(row["id"], record, " / ".join(names(fields.get("标题"))))
        authors = names(fields.get("作者") or str(record.get("author") or "").split(", "))
        tags = {label: names(fields.get(label) or record.get(key)) for label, key in TAG_GROUPS.items()}
        all_tags = [item for group in tags.values() for item in group]
        chapters_str = str(record.get("chapters") or "")
        is_wip = chapters_str.endswith("/?")
        bm_tags = names(record.get("bookmark_tags"))
        bm_notes = str(record.get("bookmark_notes") or "").strip()
        is_noted = bool(bm_tags or bm_notes)
        if author_key and (author_key == "anonymous" or all(author_key != name.casefold() for name in authors)):
            continue
        if tag_key and all(tag_key != item.casefold() for item in all_tags):
            continue
        if wip and not is_wip:
            continue
        if noted and not is_noted:
            continue
        if words:
            text = " ".join((row["id"], f"#{row['id']}", title, *authors, *all_tags,
                             *bm_tags, str(record.get("summary") or ""), bm_notes)).casefold()
            if not all(word in text for word in words):
                continue
        row.update(title=title, authors=authors, tags=tags, is_wip=is_wip, is_noted=is_noted,
                   bm_tags=bm_tags, bm_notes=bm_notes)
        row["unread"] = (row["last_event"] or 0) > row["read_through"]
        row["missing_attachments"] = (missing_attachments(record, configured, row["formats"]) if configured is not None else
                                      record.get("failed_formats") or record.get("missing_current_formats"))
        row["failed"] = bool(record.get("error_count") or row["missing_attachments"] or row["parse_error"]
                             or not row["readable_version"] or row["readable_version"] != row["latest_version"])
        rows.append(row)
    counts = {"all": len(rows), "since": sum(bool(r["after_visit"]) for r in rows),
              "unread": sum(r["unread"] for r in rows), "failed": sum(r["failed"] for r in rows)}
    if not view:
        if author or tag or q or wip or noted:
            view = "all"
        else:
            view = "since" if counts["since"] else "unread" if counts["unread"] else "all"
    context = overview_query(view, q, sort, author=author, tag=tag, wip=wip, noted=noted, page=page_num)
    tabs = []
    for key, label in VIEWS.items():
        current_tab = ' aria-current="page"' if key == view else ""
        url = escape(home_url(**{**context, "view": key, "page": 1}))
        tabs.append(f'<a href="{url}"{current_tab}>{label} <span>{counts[key]}</span></a>')
    view_key = {"since": "after_visit", "unread": "unread", "failed": "failed"}.get(view)
    view_rows = [row for row in rows if view_key is None or row[view_key]]
    wip_count = sum(r["is_wip"] for r in view_rows)
    noted_count = sum(r["is_noted"] for r in view_rows)
    wip_url = escape(home_url(**{**context, "wip": not wip, "page": 1}))
    noted_url = escape(home_url(**{**context, "noted": not noted, "page": 1}))
    extra_pills = (f'<div class="sub-filters" aria-label="快捷筛选">'
                   f'<a class="filter-chip{" is-active" if wip else ""}" href="{wip_url}">连载中 <span>{wip_count}</span></a>'
                   f'<a class="filter-chip{" is-active" if noted else ""}" href="{noted_url}">含书签备注 <span>{noted_count}</span></a>'
                   f'</div>')
    chip_parts = []
    if author:
        clear_url = escape(home_url(view, q, sort, tag=tag, wip=wip, noted=noted))
        chip_parts.append(f'<span class="active-chip">作者 · <strong>{escape(author)}</strong> <a href="{clear_url}" aria-label="清除作者筛选">×</a></span>')
    if tag:
        clear_url = escape(home_url(view, q, sort, author=author, wip=wip, noted=noted))
        chip_parts.append(f'<span class="active-chip">标签 · <strong>{escape(tag)}</strong> <a href="{clear_url}" aria-label="清除标签筛选">×</a></span>')
    if wip:
        chip_parts.append(f'<span class="active-chip">连载中 <a href="{wip_url}" aria-label="清除连载中筛选">×</a></span>')
    if noted:
        chip_parts.append(f'<span class="active-chip">含书签备注 <a href="{noted_url}" aria-label="清除书签备注筛选">×</a></span>')
    filters = ""
    if chip_parts:
        filters = (f'<div class="active-filters">{"".join(chip_parts)}'
                   f'<a class="text-link" href="{escape(home_url(view, q, sort))}">清除全部筛选</a></div>')

    matched = list(view_rows)
    if sort == "title":
        matched.sort(key=lambda row: row["title"].casefold())
    elif sort == "ao3":
        matched.sort(key=lambda row: row["record"].get("updated_at") or 0, reverse=True)
    elif sort == "words":
        matched.sort(key=lambda row: row["record"].get("words") or 0, reverse=True)
    elif sort == "captured":
        matched.sort(key=lambda row: str(row["record"].get("downloaded_at") or row["happened_at"] or ""), reverse=True)
    else:
        matched.sort(key=lambda row: row["last_event"] or 0, reverse=True)

    total_matched = len(matched)
    total_pages = max(1, math.ceil(total_matched / PER_PAGE))
    current_page = max(1, min(page_num, total_pages))
    page_rows = matched[(current_page - 1) * PER_PAGE : current_page * PER_PAGE]
    page_context = overview_query(view, q, sort, author=author, tag=tag, wip=wip, noted=noted, page=current_page)
    rendered = "".join(overview_row(row, page_context) for row in page_rows)
    if not rendered:
        empty = "没有匹配的作品。" if (author or tag or q or wip or noted) and not view_rows else {
            "all": "归档里还没有作品。", "since": "上次访问后没有新变更。",
            "unread": "没有未读更新。", "failed": "没有抓取问题。",
        }[view]
        rendered = f'<li class="empty">{empty}</li>'

    visit_bar = ""
    if counts["since"] > 0:
        seen_url = escape(with_query("/overview/seen", {k: v for k, v in page_context.items() if k != "page"}))
        proof_input = hidden_fields({"proof": backup_proof(reader["id"])})
        visit_bar = f"""<div class="visit-bar" role="region" aria-label="访问后更新提示">
  <span>上次访问后 <strong>{counts['since']}</strong> 篇变更</span>
  <form method="post" action="{seen_url}">{proof_input}
    <button class="button small primary" type="submit">✓ 标为已查看</button>
  </form>
</div>"""

    count_label = (f"第 {current_page}/{total_pages} 页 · 共 {total_matched} 篇"
                   if total_pages > 1 else f"共 {total_matched} 篇作品")
    options = "".join(f'<option value="{key}"{" selected" if key == sort else ""}>排序：{label}</option>'
                      for key, label in SORTS.items())
    hidden = hidden_fields({key: value for key, value in context.items() if key not in {"q", "sort", "page"}})
    toolbar = f"""<div class="controls">
  <div class="controls-top">
    <div class="controls-tabs-wrap"><nav class="tabs" aria-label="作品筛选">{''.join(tabs)}</nav>{extra_pills}</div>
    {visit_bar}
  </div>
  <form class="search" method="get" action="/">{hidden}
    <label class="sr-only" for="search">搜索作品</label>
    <input type="search" id="search" name="q" placeholder="搜索 ID、标题、作者、原作、CP、标签或书签备注（回车搜索）…" maxlength="200" value="{escape(q)}">
    <label class="sr-only" for="sort">排序方式</label><select id="sort" name="sort" data-auto-submit>{options}</select>
    <button class="button js-hide" type="submit">应用</button>
    <span class="result-count muted">{count_label}</span>
  </form>
</div>"""

    panel = backup_panel(archive, reader, page_context, settings, backup_status(scheduler, active_jobs, last, settings),
                         notice, error=backup_error, form_values=backup_values, scheduler=scheduler)
    creds = archive.ao3_credentials_status()
    env_creds = bool(os.environ.get("AO3_USER", "").strip() and os.environ.get("AO3_PASSWORD", ""))
    target_bookmark_user = _initial_bookmark_user(archive, settings)
    credential_banner = ""
    if not target_bookmark_user:
        credential_banner = (
            '<p class="notice is-info">站点密码已完成设置。请前往 <a class="text-link" href="/schedule">计划</a> 填写目标书签用户名（BOOKMARK_USER）以启动备份；'
            '如需同步私有书签或受限作品，可前往 <a class="text-link" href="/account">账号安全</a> 配置并在线校验 AO3 登录凭据。</p>'
        )
    elif creds["auth_mode"] == "cookie" and creds["cookie_expired"]:
        credential_banner = (
            '<p class="warning notice" role="alert">AO3 登录 Cookie 已过期（当前为仅 Cookie 模式），私有书签与受限作品已暂停抓取。'
            ' <a class="button small" href="/account">前往重新登录更新 Cookie</a></p>'
        )
    elif not creds["configured"] and env_creds:
        credential_banner = (
            '<p class="notice is-info">检测到环境变量仍存有明文 AO3 账号，建议前往 '
            '<a class="text-link" href="/account">账号安全</a> 在线校验并转为加密存储或仅保存 Cookie，随后可从 .env 移除。</p>'
        )
    pagination = _pagination_html(page_context, current_page, total_pages)
    return page("作品总览", f"""
<div class="page-heading sr-only"><h1>作品总览</h1><span class="muted">{count_label}</span></div>
{credential_banner}{panel}{toolbar}{filters}
<section class="results" aria-label="{VIEWS[view]}">
<ul class="work-list" role="list">{rendered}</ul>{pagination}</section>""", dashboard=True, active_nav="overview", logout_proof=backup_proof(reader["id"]))


def overview_row(row: dict, context: dict[str, str]) -> str:
    wid, record = row["id"], row["record"]
    view, sort = context["view"], context["sort"]
    versions_url = escape(with_query(f"/works/{wid}", context))
    readable, latest = row["readable_version"], row["latest_version"]
    title_target = with_query(f"/works/{wid}/latest", context) if readable else with_query(f"/works/{wid}", context)
    title_url = escape(title_target)
    title = escape(row["title"])
    rating_badge = _rating_badge(row["tags"], record)

    authors = "、".join(escape(name) if name.casefold() == "anonymous" else filter_link(name, "author", sort=sort)
                       for name in row["authors"])
    byline = f'<p class="byline"><span>by</span> {authors}</p>' if authors else ""
    tags = render_tag_groups(row["tags"], sort=sort, collapse_limit=10, compact_meta=True)

    summary_text = str(record.get("summary") or "").strip()
    bm_tags = row["bm_tags"]
    bm_notes = row["bm_notes"]
    about_sections = []
    if summary_text:
        about_sections.append(
            '<div class="card-summary-wrap is-collapsed">'
            f'<p class="card-summary">{escape(summary_text)}</p>'
            '<button type="button" class="summary-toggle" aria-expanded="false" aria-label="展开简介" title="展开简介" hidden>'
            '<svg class="icon-dots" viewBox="0 0 24 12" width="22" height="11" aria-hidden="true">'
            '<circle cx="5" cy="6" r="1.75" fill="currentColor"/>'
            '<circle cx="12" cy="6" r="1.75" fill="currentColor"/>'
            '<circle cx="19" cy="6" r="1.75" fill="currentColor"/></svg>'
            '<svg class="icon-collapse" viewBox="0 0 20 12" width="16" height="10" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<path d="M4 8l6-5 6 5"/></svg></button></div>'
        )
    if bm_tags or bm_notes:
        bm_label = '<span class="bm-label">书签备注</span>'
        bm_chips = ("".join(f'<span class="bm-tag">{escape(t)}</span>' for t in bm_tags)
                    if bm_tags else "")
        bm_note_html = f'<p class="card-bm-note">{escape(bm_notes)}</p>' if bm_notes else ""
        about_sections.append(f'<div class="card-bookmark">{bm_label}{bm_chips}{bm_note_html}</div>')
    card_about = f'<div class="card-about">{"".join(about_sections)}</div>' if about_sections else ""

    unread = '<span class="unread-label"><i></i>未读</span>' if row["unread"] else ""
    labels = row[{"since": "since_labels", "unread": "unread_labels"}.get(view, "labels")]
    changes = "".join(f"<li>{escape(label)}</li>" for label in labels[:2])
    if len(labels) > 2:
        changes += f'<li class="muted">另有 {len(labels) - 2} 项</li>'
    failure = ""
    if row["failed"]:
        label = ("正文提取失败" if row["parse_error"] else
                 "全文抓取失败" if record.get("error_count") else
                 "正文缺失" if not readable or readable != latest else
                 "附件缺失" if row["missing_attachments"] else "")
        if label:
            failure = f'<span class="warning">{label}</span>'
    count = row["after_visit"]
    badge = f'<span class="muted">{count} 次变化</span>' if view == "since" and count > 1 else ""
    kind = KINDS.get(row["kind"], "")
    kind_line = f'<span class="change-kind">{kind}</span>' if kind else ""
    reading_action = ""
    attachment = ""
    if not readable:
        reading_action = '<span class="muted">暂无可读正文</span>'
        if row["formats"] and latest:
            attachment = download_menu(wid, latest, has_article=False, formats=row["formats"], compact=True)
    elif readable != latest:
        reading_action = f'<a class="action-link" href="{title_url}">阅读留存版</a>'
    else:
        reading_action = f'<a class="action-link is-primary" href="{title_url}">阅读全文</a>'

    chapters = escape(str(record.get("chapters") or ""))
    chapter_cls = "stat-pill is-wip" if row["is_wip"] else "stat-pill"
    chapter_prefix = "连载 " if row["is_wip"] else ""
    chapter_count = f'<span class="{chapter_cls}">{chapter_prefix}{chapters} 章</span>' if chapters else ""
    words = record.get("words")
    word_count = f'<span class="stat-pill">{words:,} 字</span>' if isinstance(words, int) and words > 0 else ""
    downloaded_at = record.get("downloaded_at")
    backup_time = (f'<span>备份 <time class="stamp">{date_label(downloaded_at)}</time></span>'
                   if downloaded_at else "")
    ao3_id_link = (f'<a class="action-link stat-id" href="https://archiveofourown.org/works/{wid}" '
                   f'rel="noreferrer" target="_blank" title="在 AO3 打开原页">#{wid} ↗</a>')
    body_cls = "work-body has-about" if tags and card_about else "work-body"
    card_body = f'<div class="{body_cls}">{tags}{card_about}</div>' if (tags or card_about) else ""
    row_cls = "work-row" + (" is-unread" if row["unread"] else "") + (" is-failed" if row["failed"] else "")
    return f"""<li class="{row_cls}">
<div class="row-head">
  <div class="row-title-group">{rating_badge}<h2 class="row-title"><a href="{title_url}">{title}</a></h2>{byline}</div>
  <div class="row-head-right">{unread}{kind_line}{badge}{failure}{chapter_count}{word_count}</div>
</div>
{card_body}
<div class="work-footer">
  <div class="change-info"><ul class="change-labels">{changes}</ul></div>
  <div class="work-actions">
    <span class="work-stats muted"><span>AO3 <time class="stamp">{date_label(record.get('updated_at'))}</time></span>{backup_time}</span>
    {reading_action}{attachment}<a class="action-link" href="{versions_url}">版本与差异</a>{ao3_id_link}
  </div>
</div></li>"""


def version_name(version: Version, number: int) -> str:
    if version.origin == "metadata":
        return f"第 {number} 版 · 资料快照"
    return f"第 {number} 版 · AO3 {date_label(version.ao3_updated_at)}"


def version_files(wid: str, version: Version, back: dict[str, str], *, primary_read: bool = False) -> str:
    links = []
    if version.document_digest:
        url = escape(with_query(article_url(wid, version.id), back))
        read_cls = "button primary" if primary_read else "text-link"
        links.append(f'<a class="{read_cls}" href="{url}">阅读全文</a>')
    menu = download_menu(
        wid, version.id, has_article=bool(version.document_digest),
        formats=version.files.keys(), compact=not primary_read,
    )
    if menu:
        links.append(menu)
    return "<span class='file-links'>" + "".join(links) + "</span>"


def compare_url(wid: str, previous: int, current: int, back: dict[str, str]) -> str:
    return f"/works/{wid}?" + urlencode({**back, "from": previous, "to": current})


def positive_id(value: str) -> int:
    return int(value) if re.fullmatch(r"[0-9]{1,18}", value) else 0


def proof(reader_id: str, wid: str, seq: int) -> str:
    return hmac.new(reader_id.encode(), f"{wid}:{seq}".encode(), hashlib.sha256).hexdigest()


def _highlight_pair(old: str, new: str) -> tuple[str, str]:
    if len(old) <= 1600 and len(new) <= 1600:
        pairs = SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
        old_html = "".join(escape(old[i:j]) if tag == "equal" else f"<mark>{escape(old[i:j])}</mark>"
                           for tag, i, j, _, _ in pairs if i < j)
        new_html = "".join(escape(new[k:m]) if tag == "equal" else f"<mark>{escape(new[k:m])}</mark>"
                           for tag, _, _, k, m in pairs if k < m)
        return old_html, new_html
    limit = min(len(old), len(new))
    prefix = 0
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    max_suffix = limit - prefix
    while suffix < max_suffix and old[-1 - suffix] == new[-1 - suffix]:
        suffix += 1
    context = 80
    start = max(0, prefix - context)
    old_end = len(old) - suffix + context if suffix > context else len(old)
    new_end = len(new) - suffix + context if suffix > context else len(new)
    old_win, new_win = old[start:old_end], new[start:new_end]
    if len(old_win) <= 2400 and len(new_win) <= 2400:
        pairs = SequenceMatcher(None, old_win, new_win, autojunk=False).get_opcodes()
        old_html = "".join(escape(old_win[i:j]) if tag == "equal" else f"<mark>{escape(old_win[i:j])}</mark>"
                           for tag, i, j, _, _ in pairs if i < j)
        new_html = "".join(escape(new_win[k:m]) if tag == "equal" else f"<mark>{escape(new_win[k:m])}</mark>"
                           for tag, _, _, k, m in pairs if k < m)
    else:
        old_html = escape(old_win[:1600]) + ("…" if len(old_win) > 1600 else "")
        new_html = escape(new_win[:1600]) + ("…" if len(new_win) > 1600 else "")
    if start > 0:
        old_html = "…" + old_html
        new_html = "…" + new_html
    if old_end < len(old):
        old_html += "…"
    if new_end < len(new):
        new_html += "…"
    return old_html, new_html


def _format_numeric_diff(diff: Difference) -> tuple[str, str, str] | None:
    if diff.section != "作品资料" or len(diff.before) != 1 or len(diff.after) != 1:
        return None
    old_val, new_val = diff.before[0].strip(), diff.after[0].strip()
    old_num, new_num = old_val.replace(",", ""), new_val.replace(",", "")
    if diff.label == "字数" and old_num.isdigit() and new_num.isdigit():
        old_n, new_n = int(old_num), int(new_num)
        delta = new_n - old_n
        sign = f"+{delta:,}" if delta >= 0 else f"{delta:,}"
        return f"{old_n:,} 字", f"{new_n:,} 字", f"{sign} 字"
    if diff.label == "章节" and old_val and new_val:
        return f"{old_val} 章", f"{new_val} 章", ""
    return None


def render_diff(diff: Difference, *, open_by_default: bool = True) -> str:
    open_attr = " open" if open_by_default else ""
    num_fmt = _format_numeric_diff(diff)
    if num_fmt is not None:
        old_txt, new_txt, delta_txt = num_fmt
        badge = f' <span class="delta-badge">{escape(delta_txt)}</span>' if delta_txt else ""
        rows = (
            '<div class="diff-key"><span>旧版</span><span>新版</span></div>'
            f'<div class="diff-pair"><p class="deletion">{escape(old_txt)}</p>'
            f'<p class="addition"><mark>{escape(new_txt)}</mark>{badge}</p></div>'
        )
        return f'<details class="diff"{open_attr}><summary>{escape(diff.heading)}</summary><div class="diff-body">{rows}</div></details>'
    if not diff.before or not diff.after:
        added = bool(diff.after)
        paragraphs = diff.after if added else diff.before
        limit = 8 if diff.label in {"章节", "正文"} else len(paragraphs)
        side = "addition" if added else "deletion"
        rows = ("<div class='diff-single " + side + "'>" + "".join(
            f"<p>{escape(p)}</p>" for p in paragraphs[:limit]
        ) + "</div>")
        if len(paragraphs) > limit:
            rows += f'<p class="muted">另有 {len(paragraphs) - limit} 段，可打开全文阅读。</p>'
    else:
        rows = []
        remaining = 0
        has_pair = False
        add_tag = "新增段落" if diff.label == "正文" else "新增"
        del_tag = "删除段落" if diff.label == "正文" else "删除"
        for action, a, b, c, d in SequenceMatcher(None, diff.before, diff.after).get_opcodes():
            if action == "equal":
                continue
            for old, new in zip_longest(diff.before[a:b], diff.after[c:d], fillvalue=""):
                if len(rows) >= 16 and diff.label == "正文":
                    remaining += 1
                    continue
                if old and new:
                    has_pair = True
                    old_html, new_html = _highlight_pair(old, new)
                    rows.append(f'<div class="diff-pair"><p class="deletion">{old_html}</p>'
                                f'<p class="addition">{new_html}</p></div>')
                elif new:
                    rows.append(f'<div class="diff-pair is-single"><p class="addition"><span class="diff-side-tag">{add_tag}</span>{escape(new)}</p></div>')
                else:
                    rows.append(f'<div class="diff-pair is-single"><p class="deletion"><span class="diff-side-tag">{del_tag}</span>{escape(old)}</p></div>')
        tail = f'<p class="muted">另有 {remaining} 处改动，可打开全文查看。</p>' if remaining else ""
        header = '<div class="diff-key"><span>旧版</span><span>新版</span></div>' if has_pair else ""
        rows = header + "".join(rows) + tail
    return f'<details class="diff"{open_attr}><summary>{escape(diff.heading)}</summary><div class="diff-body">{rows}</div></details>'


def work_html(archive: Archive, reader: dict, wid: str, params: dict[str, str]) -> tuple[int, str]:
    l_proof = backup_proof(reader["id"])
    detail = archive.detail(wid, reader)
    if detail is None:
        return 404, page("找不到作品", '<p class="empty">找不到这篇作品。</p>', logout_proof=l_proof)
    record, versions, events, seen = detail
    title = work_title(wid, record)
    back = overview_query(
        params.get("view", "all"), params.get("q", ""), params.get("sort", "event"),
        author=params.get("author", ""), tag=params.get("tag", ""),
        wip=params.get("wip") == "1", noted=params.get("noted") == "1",
        page=positive_id(params.get("page", "")) or 1,
    )
    latest = next((event for event in events if event["kind"] != "file"), None)
    unread = latest is not None and latest["id"] > seen
    order = list(versions)
    newest = order[-1] if order else 0
    status = ""
    if unread:
        seq = latest["id"]
        action = escape(with_query(f"/works/{wid}/read", back))
        status = f"""<form method="post" action="{action}" class="read-form">
<input type="hidden" name="through" value="{seq}">
<input type="hidden" name="proof" value="{proof(reader['id'], wid, seq)}">
<button class="button tonal" type="submit">✓ 标为已读</button></form>"""

    readable = next((v for v in reversed(versions.values()) if v.document_digest), None)
    article_fields: dict[str, tuple[str, ...]] = {}
    if readable and (saved_article := archive.read_article(wid, readable.id)):
        article_fields = saved_article[1].fields

    authors_list = names(article_fields.get("作者") or str(record.get("author") or "").split(", "))
    authors_html = "、".join(escape(name) if name.casefold() == "anonymous"
                             else filter_link(name, "author", sort=back["sort"]) for name in authors_list)
    info = [
        " / ".join(record.get("fandoms") or article_fields.get("原作") or ()),
        f"{record['chapters']} 章" if record.get("chapters") else "",
        f"{record['words']:,} 字" if isinstance(record.get("words"), int) else "",
        f"共 {len(order)} 个存档版本" if order else "",
    ]
    info_tail = escape(" · ".join(filter(None, info)))
    byline_html = f'<p class="muted work-meta-line">{authors_html}{(" · " + info_tail) if authors_html and info_tail else info_tail}</p>'

    work_tags = {label: names(article_fields.get(label) or record.get(key)) for label, key in TAG_GROUPS.items()}
    rating_badge = _rating_badge(work_tags, record)
    tags_html = render_tag_groups(work_tags, sort=back["sort"])
    tags_box = (f'<details class="about dossier-pane" open><summary>作品标签</summary>{tags_html}</details>'
                if tags_html else "")

    description = "".join(f'<section><h3>{label}</h3><p>{escape(value)}</p></section>'
                          for label, value in (("简介", record.get("summary") or ""),
                                               ("书签标签", " · ".join(record.get("bookmark_tags") or [])),
                                               ("书签备注", record.get("bookmark_notes") or "")) if value)
    about = (f'<details class="about dossier-pane" open><summary>简介和书签</summary>{description}</details>'
             if description else "")
    error = record.get("last_error")
    if not error and record.get("failed_formats"):
        error = "等待重试 " + "、".join(ext.upper() for ext in record["failed_formats"])
    if not error and record.get("missing_current_formats"):
        error = "旧库缺失 " + "、".join(ext.upper() for ext in record["missing_current_formats"])
    error_html = f'<p class="warning notice">下载状态 · {escape(error)}</p>' if error else ""
    if newest and versions[newest].parse_error:
        error_html += f'<p class="warning notice">正文提取失败 · {escape(versions[newest].parse_error)}</p>'

    header_actions = []
    if newest and versions[newest].document_digest:
        header_actions.append(version_files(wid, versions[newest], back, primary_read=True))
    elif readable:
        read_url = escape(with_query(article_url(wid, readable.id), back))
        header_actions.append(f'<a class="button primary" href="{read_url}">阅读留存版</a>')
        if newest:
            menu = download_menu(wid, versions[newest].id, has_article=False, formats=versions[newest].files.keys())
            if menu:
                header_actions.append(menu)
        error_html += f'<p class="notice"><a class="text-link" href="{read_url}">当前版本缺少正文，点击阅读最近一份可用正文</a></p>'
    else:
        if newest:
            menu = download_menu(wid, versions[newest].id, has_article=False, formats=versions[newest].files.keys())
            if menu:
                header_actions.append(menu)
        if newest:
            error_html += '<p class="notice muted">暂无可读正文。</p>'
    if status:
        header_actions.append(status)
    header_actions.append(f'<a class="button" href="https://archiveofourown.org/works/{wid}" rel="noreferrer" target="_blank">AO3 原页 ↗</a>')

    unread_chip = '<span class="unread-label"><i></i>未读更新</span>' if unread else ""
    dossier_grid = f'<div class="dossier-grid">{tags_box}{about}</div>' if (tags_box or about) else ""
    heading = f"""<nav class="breadcrumbs"><a href="{escape(home_url(**back))}">← 作品总览</a>
<span aria-hidden="true">/</span><span>版本与差异</span></nav>
<section class="dossier-card">
  <div class="work-heading">
    <div class="work-title-block">
      <div class="work-headline">{rating_badge}<h1>{escape(title)}</h1>{unread_chip}</div>
      {byline_html}
    </div>
    <div class="work-header-links">{"".join(header_actions)}</div>
  </div>
  {dossier_grid}
  {error_html}
</section>"""

    if not order:
        return 200, page(title, heading + '<p class="empty">还没有成功保存的版本。</p>', detail=True, logout_proof=l_proof)

    seen_vid = next((e["version_id"] for e in events if e["kind"] != "file"
                     and e["id"] <= seen and e["version_id"] in versions), None)
    current_vid = latest["version_id"] if unread and latest["version_id"] in versions else newest
    recent_pair = (seen_vid, current_vid) if seen_vid and seen_vid < current_vid else None
    default_from = recent_pair[0] if unread and recent_pair else order[max(0, len(order) - 2)]
    old_id, new_id = positive_id(params.get("from", "")), positive_id(params.get("to", ""))
    old_id = old_id if old_id in versions else default_from
    new_id = new_id if new_id in versions else current_vid
    old_id, new_id = sorted((old_id, new_id))
    old, new = versions[old_id], versions[new_id]
    positions = {vid: i for i, vid in enumerate(order, 1)}

    quick = ""
    if recent_pair and unread:
        url = compare_url(wid, *recent_pair, back)
        quick = f'<a class="text-link" href="{escape(url)}">上次读到的版本 ↔ 最新版</a>'

    def options(selected: int) -> str:
        return "".join(f'<option value="{vid}"{" selected" if vid == selected else ""}>'
                       f'{escape(version_name(versions[vid], positions[vid]))}</option>'
                       for vid in reversed(order))

    hidden = hidden_fields(back)
    comparison = f"""<section class="compare" id="comparison" aria-label="版本比较">
<div class="section-heading"><h2>版本比较</h2>{quick}</div>
<form method="get" action="/works/{wid}#comparison" class="selectors">{hidden}
<label>旧版<select name="from">{options(old_id)}</select></label>
<span class="arrow" aria-hidden="true">→</span>
<label>新版<select name="to">{options(new_id)}</select></label>
<button class="button primary" type="submit">比较</button></form>"""
    if len(order) == 1:
        comparison = '<section class="compare"><h2>版本比较</h2><p class="muted">已保存一版。</p>'
    elif old_id == new_id:
        comparison += '<p class="muted">请选择两个不同的版本。</p>'
    else:
        old_info = f"抓取 {date_label(old.captured_at)}" if old.origin != "metadata" else "沿用上一版文件"
        new_info = f"抓取 {date_label(new.captured_at)}" if new.origin != "metadata" else "沿用上一版文件"
        comparison += f"""<div class="selected-files">
<div><span class="muted">旧版 · {old_info}</span>{version_files(wid, old, back)}</div>
<div><span class="muted">新版 · {new_info}</span>{version_files(wid, new, back)}</div></div>"""
        changes = archive.compare(old, new)
        adjacent = positions[new_id] - positions[old_id] == 1
        saved = next((json.loads(e["legacy_changes"]) for e in events if e["version_id"] == new_id), {})
        if adjacent and saved:
            labels = {c.label for c in changes}
            changes.extend(c for c in field_differences(saved) if c.label not in labels)
        missing_article = not (old.document_digest and new.document_digest)
        if missing_article:
            comparison += '<p class="notice muted">所选版本缺少可读正文。下方显示已留存的资料变更。</p>'
        if changes:
            delta_items = []
            for c in changes:
                nf = _format_numeric_diff(c)
                if nf:
                    o_t, n_t, d_t = nf
                    d_chip = f' <span class="delta-badge">{escape(d_t)}</span>' if d_t else ""
                    delta_items.append(f'<span class="delta-item">{escape(c.label)}：<strong>{escape(o_t)}</strong> → <strong>{escape(n_t)}</strong>{d_chip}</span>')
            delta_strip = f'<div class="delta-strip"><strong>数值变化：</strong>{" · ".join(delta_items)}</div>' if delta_items else ""
            open_default = len(changes) <= 6
            toggle_lbl = "收起全部差异" if open_default else "展开全部差异"
            diff_bar = (f'<div class="diffs-bar"><span class="muted">共 {len(changes)} 处差异</span>'
                        f'<button type="button" class="text-link diff-toggle-all" hidden>{toggle_lbl}</button></div>'
                        if len(changes) > 1 else "")
            comparison += delta_strip + diff_bar + '<div class="diffs">' + "".join(render_diff(change, open_by_default=open_default) for change in changes) + '</div>'
        elif not missing_article:
            changed_files = {k: f.digest for k, f in old.files.items()} != {k: f.digest for k, f in new.files.items()}
            desc = ("只检测到原始附件变化。" if changed_files else "文章内容和作品资料一致。")
            comparison += f'<p class="notice muted">{desc}</p>'
    comparison += "</section>"

    timeline = []
    anchored = {event["version_id"]: event for event in events if event["version_id"] is not None}
    for index in range(len(order) - 1, -1, -1):
        vid = order[index]
        version = versions[vid]
        event = anchored.get(vid)
        labels = json.loads(event["labels"]) if event else []
        kind = KINDS.get(event["kind"], "") if event else "留存版本"
        labels_html = "".join(f"<li>{escape(label)}</li>" for label in labels)
        is_unread = event and event["kind"] != "file" and event["id"] > seen
        mark = '<span class="unread-label"><i></i>未读</span>' if is_unread else ""
        issue = f'<p class="warning">{escape(version.parse_error)}</p>' if version.parse_error else ""
        pair = compare_url(wid, order[index - 1], vid, back) if index else ""
        link = f'<a class="text-link" href="{escape(pair)}#comparison">与上一版比较</a>' if pair else ""
        times = f"抓取 {date_label(version.captured_at)}" if version.origin != "metadata" else (
            f"记录 {date_label(event['happened_at'])}" if event else "资料快照")
        node_cls = "v-node" + (" is-latest" if index == len(order) - 1 else "") + (" is-unread" if is_unread else "")
        timeline.append(f"""<li class="{node_cls}"><div class="timeline-head"><div class="timeline-title"><strong>{escape(version_name(version, index + 1))}</strong>
{mark}<span class="muted">{kind}</span></div><div class="row-actions">{link}{version_files(wid, version, back)}</div></div>
<p class="muted timeline-stamp">{times}</p>{issue}<ul class="change-labels">{labels_html}</ul></li>""")
    historical = []
    for event in events:
        if event["version_id"] is None:
            changes = field_differences(json.loads(event["legacy_changes"]))
            history = "".join(render_diff(change) for change in changes)
            historical.append(f'<li class="v-node"><strong>{KINDS[event["kind"]]}</strong> · {date_label(event["happened_at"])}'
                              f'<p class="muted">旧库仅留存变更记录</p>{history}</li>')
    old_history = (f'<details><summary>旧库的资料变更记录</summary><ol class="timeline">{"".join(historical)}</ol></details>'
                   if historical else "")
    timeline_html = (f'<section class="history"><h2>版本历史 <span>{len(order)}</span></h2>'
                     f'<ol class="timeline">{"".join(timeline)}</ol>{old_history}</section>')
    return 200, page(title, heading + comparison + timeline_html, detail=True, logout_proof=l_proof)


def article_html(
    wid: str, version: Version, article: Export, *,
    back: dict[str, str] | None = None, fallback: bool = False,
    version_ctx: tuple[int, int, int | None] | None = None,
    logout_proof: str = "",
) -> str:
    values = article.values(version.metadata)
    title = " / ".join(values.pop("标题", ())) or work_title(wid, version.metadata)
    sort_mode = (back or {}).get("sort", "event")
    author_items = values.pop("作者", ())
    author = "、".join(escape(name) if name.casefold() == "anonymous"
                       else filter_link(name, "author", sort=sort_mode) for name in author_items)
    summary = article.sections.get("作品简介", list(values.pop("作品简介", ())))

    rating_badge = _rating_badge(values, version.metadata)
    quick_pills = []
    for grp, cls in (("原作", "tag-group-fandoms"), ("关系标签", "tag-group-relationships"), ("角色标签", "tag-group-characters")):
        for item in values.get(grp, ())[:3]:
            quick_pills.append(f'<span class="side-tag {cls}">{filter_link(item, "tag", sort=sort_mode)}</span>')
    quick_tags_html = f'<div class="side-quick-tags">{"".join(quick_pills)}</div>' if quick_pills else ""

    field_rows = []
    for label, items in values.items():
        if label in TAG_GROUPS:
            links = "".join(f'<li>{filter_link(item, "tag", sort=sort_mode)}</li>' for item in items)
            val_html = f'<ul class="tag-links" role="list">{links}</ul>'
        elif label == "字数" and len(items) == 1 and items[0].isdigit():
            val_html = f"{int(items[0]):,} 字"
        elif label == "章节" and len(items) == 1 and not items[0].endswith("章"):
            val_html = f"{escape(items[0])} 章"
        else:
            val_html = escape("、".join(items))
        field_rows.append(f"<dt>{label}</dt><dd>{val_html}</dd>")
    tags = "".join(field_rows)

    total_chapters = len(article.chapters)
    catalog = "".join(f'<li><a href="#chapter_{index}">{escape(chapter_heading(chapter, index))}</a></li>'
                      for index, chapter in enumerate(article.chapters, 1))
    toc_html = (f'<div class="side-card"><details class="about toc-details" id="toc" open>'
                f'<summary>章节目录 · {total_chapters} 章</summary><ol class="toc-list">{catalog}</ol></details></div>'
                if total_chapters > 1 else "")

    def note(label: str, nodes: Fragment, heading: str = "h3", *, overline: str = "") -> str:
        if not nodes:
            return ""
        sub = f'<span class="note-overline">{overline}</span>' if overline else ""
        return f'<section class="article-note">{sub}<{heading}>{label}</{heading}>{reading_html(nodes)}</section>'

    chapters = []
    for index, chapter in enumerate(article.chapters, 1):
        nav_links = []
        if total_chapters > 1:
            if index > 1:
                nav_links.append(f'<a class="text-link" href="#chapter_{index - 1}">← 上一章</a>')
            nav_links.append('<a class="text-link toc-jump" href="#toc">目录</a>')
            if index < total_chapters:
                nav_links.append(f'<a class="text-link" href="#chapter_{index + 1}">下一章 →</a>')
        chapter_nav = f'<nav class="chapter-nav" aria-label="章节切换">{"".join(nav_links)}</nav>' if nav_links else ""
        step_label = f'<small class="chapter-step">{index} / {total_chapters}</small>' if total_chapters > 1 else ""
        chapters.append(f"""<section class="article-chapter" id="chapter_{index}">
<h2 class="chapter-heading"><span>{escape(chapter_heading(chapter, index))}</span>{step_label}</h2>
{note("章节简介", chapter.summary, overline="CHAPTER SUMMARY")}{note("章首作者附注", chapter.start_notes, overline="CHAPTER NOTE")}
<div class="article-prose">{reading_html(chapter.body)}</div>
{note("章末作者附注", chapter.end_notes, overline="END NOTE")}{chapter_nav}</section>""")
    overview_url = escape(home_url(**back)) if back else "/"
    versions_url = escape(with_query(f"/works/{wid}", back))
    availability = ('<p class="notice">当前版本缺少可读正文，正在显示最近一份留存版。</p>'
                    if fallback else "")
    if not fallback and version_ctx:
        pos, total, latest_vid = version_ctx
        if latest_vid and version.id != latest_vid:
            latest_url = escape(with_query(f"/works/{wid}/latest", back))
            cmp_url = escape(compare_url(wid, version.id, latest_vid, back or {}))
            availability = (f'<p class="notice is-info">正在阅读历史存档（第 {pos} / {total} 版）。'
                            f' <a class="text-link" href="{latest_url}">阅读最新版</a>'
                            f' <a class="text-link" href="{cmp_url}#comparison">与最新版比较</a></p>')
    hidden = (f'<p class="muted">抓取时 AO3 隐藏了{escape("、".join(article.hidden_fields))}。</p>'
              if article.hidden_fields else "")
    dl_menu = download_menu(wid, version.id, has_article=True, formats=version.files.keys())
    toc_fab = '<button type="button" class="button small fab-toc" id="reader-toc-btn">目录</button>' if total_chapters > 1 else ""
    summary_block = note("作品简介", summary, "h2", overline="SUMMARY")
    start_note_block = note("作品首注", article.sections.get("作品首注", []), "h2", overline="AUTHOR'S NOTE")
    front_cls = "front-matter-grid is-pair" if (summary_block and start_note_block) else "front-matter-grid"
    front_matter = f'<div class="{front_cls}">{summary_block}{start_note_block}</div>' if (summary_block or start_note_block) else ""
    contents = f"""<div class="reading-progress" id="reading-progress" aria-hidden="true"></div>
<nav class="breadcrumbs reader-breadcrumbs">
  <div class="crumb-links"><a href="{overview_url}">← 作品总览</a><span aria-hidden="true">/</span>
  <a href="{versions_url}">版本与差异</a><span aria-hidden="true">/</span><span>正文阅读</span></div>
  <div class="crumb-meta"><span class="muted">抓取 {date_label(version.captured_at)}</span>
  <button type="button" class="button small sidebar-toggle" id="reader-sidebar-toggle" title="收起或展开左侧书案栏">⇤ 收起侧栏</button></div>
</nav>
<div class="desk-grid">
  <aside class="desk-sidebar" id="reader-sidebar" aria-label="阅读书案侧栏">
    <div class="side-card">
      <div class="side-work-head">{rating_badge}<strong class="side-work-title">{escape(title)}</strong></div>
      <p class="muted side-work-author">{author}</p>
      {quick_tags_html}
      <div class="reader-actions side-actions">{dl_menu}<a class="button" href="{versions_url}">版本与差异</a></div>
      <details class="about side-details"><summary>作品资料</summary>{hidden}<dl class="article-fields">{tags}</dl></details>
    </div>
    {toc_html}
    <div class="side-card">
      <div class="overline-label">TYPOGRAPHY · 阅读排版</div>
      <div class="reader-prefs">
        <label><span>字体</span><select id="reader-font">
          <option value="serif">优雅衬线</option><option value="sans">现代黑体</option>
          <option value="kaiti">温润楷体</option><option value="system">系统默认</option>
        </select></label>
        <label><span>字号</span><select id="reader-size">
          <option value="small">小</option><option value="medium">标准</option>
          <option value="large">大</option><option value="xlarge">特大</option>
        </select></label>
        <label><span>栏宽</span><select id="reader-width">
          <option value="narrow">适中</option><option value="medium">宽幅</option>
          <option value="wide">满屏</option>
        </select></label>
      </div>
    </div>
  </aside>
  <article class="article reader-sheet-wide">
    <header class="reader-head-compact">
      <div class="reader-title-row">
        <h1>{escape(title)}</h1>
        <p class="muted article-author">{author}</p>
      </div>
      <a class="text-link muted" href="https://archiveofourown.org/works/{wid}" rel="noreferrer" target="_blank">AO3 #{wid} ↗</a>
    </header>
    {availability}
    {front_matter}
    {''.join(chapters)}{note("作品尾注", article.sections.get("作品尾注", []), "h2", overline="AFTERWORD")}
  </article>
</div>
<div class="reader-fab" aria-label="阅读快捷导航">{toc_fab}<button type="button" class="button small" id="reader-top-btn">↑ 顶部</button></div>"""
    return page(title, contents, detail=True, reading=True, logout_proof=logout_proof)


def _safe_next(raw: str) -> str:
    val = (raw or "").strip()
    if (not val or not val.startswith("/") or val.startswith("//")
            or any(c in val for c in ("\r", "\n", "\\"))
            or val.split("?", 1)[0] in {"/login", "/setup", "/logout"}):
        return "/"
    return val


_AUTH_SLIDER_OPTIONS = (
    ("public", "仅公开"),
    ("encrypted", "加密保存"),
    ("cookie", "仅 Cookie"),
)


def _ao3_status_badge(creds_status: dict, env_creds: bool) -> tuple[str, str]:
    if creds_status["auth_mode"] == "encrypted":
        return "is-ok", f"加密托管中 · {creds_status['user']}"
    if creds_status["auth_mode"] == "cookie":
        if creds_status["cookie_expired"]:
            return "is-warn", f"Cookie 已过期 · 请重登 ({creds_status['user']})"
        return "is-ok", f"仅 Cookie 有效 · {creds_status['user']}"
    if env_creds:
        return "is-info", "回退环境变量账号"
    return "", "仅公开（未登录）"


def _is_ao3_logged_in(archive: Archive, creds_status: dict, env_creds: bool) -> bool:
    return (
        creds_status["auth_mode"] in ("encrypted", "cookie")
        or (not creds_status["configured"] and env_creds)
        or (archive.root / "cookies.txt").exists()
    )


def _ao3_cred_fields(
    *, auth_mode: str, ao3_user: str, has_saved_password: bool = False,
) -> str:
    mode_order = [k for k, _ in _AUTH_SLIDER_OPTIONS]
    active_index = mode_order.index(auth_mode) if auth_mode in mode_order else 0
    slider_opts = "".join(
        f'<label class="auth-slider-opt"><input type="radio" name="ao3_auth_mode" value="{k}"'
        f'{" checked" if auth_mode == k else ""}><span>{label}</span></label>'
        for k, label in _AUTH_SLIDER_OPTIONS
    )
    pw_placeholder = (
        "已加密保存（留空沿用原密码）" if (auth_mode == "encrypted" and has_saved_password)
        else "输入 AO3 登录密码"
    )
    return f"""<div class="ao3-auth-box" data-ao3-auth-scope>
  <span class="field-label">AO3 登录方式</span>
  <div class="auth-slider" role="radiogroup" aria-label="AO3 登录方式" data-active-index="{active_index}">
    <span class="auth-slider-thumb" aria-hidden="true"></span>{slider_opts}
  </div>
  <p class="muted auth-mode-desc" data-ao3-mode-hint></p>
  <div class="settings-fields" data-ao3-cred-fields{' hidden' if auth_mode == 'public' else ''}>
    <label><span class="field-label">AO3 用户名</span>
      <input name="ao3_user" value="{escape(ao3_user)}" autocomplete="username" placeholder="AO3 账号用户名">
    </label>
    <label><span class="field-label">AO3 密码</span>
      <input type="password" name="ao3_password" value="" autocomplete="current-password"
             placeholder="{escape(pw_placeholder)}" data-has-saved="{1 if has_saved_password else 0}">
      <small data-ao3-pw-hint></small>
    </label>
  </div>
</div>"""


def login_html(proof_token: str, *, next_url: str = "/", error: str = "") -> str:
    err_html = f'<p class="warning notice" role="alert">{escape(error)}</p>' if error else ""
    fields = hidden_fields({"proof": proof_token, "next": _safe_next(next_url)})
    return page("访问验证", f"""
<section class="auth-shell">
  <div class="auth-card">
    <h1>访问验证</h1>
    <p class="muted">此 AO3 书签归档已启用全站密码保护，请输入站点访问密码后继续。</p>
    {err_html}
    <form class="auth-form" method="post" action="/login">{fields}
      <label><span class="field-label">站点访问密码</span>
        <input type="password" name="password" required autofocus autocomplete="current-password" placeholder="输入站点访问密码">
      </label>
      <div class="auth-actions">
        <button class="button primary" type="submit">登录</button>
      </div>
    </form>
  </div>
</section>""", auth_mode="public")


def setup_html(proof_token: str, *, has_existing_data: bool = False, error: str = "") -> str:
    err_html = f'<p class="warning notice" role="alert">{escape(error)}</p>' if error else ""
    upgrade_hint = (
        '<p class="notice is-info">检测到已有归档数据或环境变量配置。请先设置站点访问密码完成锁定；原有配置进入后会自动保留并可继续调整。</p>'
        if has_existing_data else ""
    )
    fields = hidden_fields({"proof": proof_token})
    return page("初始化站点密码", f"""
<section class="auth-shell">
  <div class="auth-card">
    <h1>欢迎使用 AO3 书签备份</h1>
    <p class="muted">首次使用请先设置全站访问密码锁定站点。进入主页后，可在总览设置抓取用户名，并在「账号安全」中配置与在线校验 AO3 登录凭据。</p>
    {upgrade_hint}{err_html}
    <form class="auth-form" method="post" action="/setup">{fields}
      <label><span class="field-label">初始化口令（Setup Token）</span>
        <input name="setup_token" required autofocus autocomplete="off" placeholder="例：ABCD-1234-EF56">
        <small>请查看容器启动日志（[SETUP] 行）</small>
      </label>
      <label><span class="field-label">设置站点访问密码</span>
        <input type="password" name="web_password" minlength="6" required autocomplete="new-password" placeholder="至少 6 位字符">
        <small>用于保护整站阅读、导出与设置页面</small>
      </label>
      <label><span class="field-label">确认站点访问密码</span>
        <input type="password" name="web_password_confirm" minlength="6" required autocomplete="new-password" placeholder="再次输入站点访问密码">
      </label>
      <div class="auth-actions">
        <button class="button primary" type="submit">完成密码设置并进入主页</button>
      </div>
    </form>
  </div>
</section>""", auth_mode="public")


def _live_tone_class(tone: str) -> str:
    return {
        "warn": "is-warn",
        "error": "is-error",
        "ok": "is-ok",
    }.get(tone, "is-info")


def account_html(
    archive: Archive, reader: dict, *, creds_status: dict, notice: str = "", error: str = "",
    auth_mode: str = "", ao3_user: str = "", cred_job: dict | None = None,
) -> str:
    proof_val = backup_proof(reader["id"])
    proof_field = hidden_fields({"proof": proof_val})
    env_creds = bool(os.environ.get("AO3_USER", "").strip() and os.environ.get("AO3_PASSWORD", ""))
    default_mode = creds_status["auth_mode"] or ("encrypted" if env_creds else "public")
    effective_mode = auth_mode if auth_mode in AO3_AUTH_MODES else default_mode
    effective_user = ao3_user or creds_status["user"] or os.environ.get("AO3_USER", "").strip()

    badge_cls, badge_text = _ao3_status_badge(creds_status, env_creds)
    status_pill = f'<span id="ao3-cred-badge" class="cred-badge {badge_cls}".rstrip()>{escape(badge_text)}</span>'
    status_pill = status_pill.replace(' class="cred-badge "', ' class="cred-badge"')

    msg_html = ""
    if error:
        msg_html = f'<p id="account-flash-msg" class="warning notice" role="alert">{escape(error)}</p>'
    elif notice == "credentials_verified":
        msg_html = '<p id="account-flash-msg" class="notice is-info" role="status">AO3 登录凭据已通过在线校验并保存（Cookie 已同步更新）。</p>'
    elif notice == "credentials":
        msg_html = '<p id="account-flash-msg" class="notice is-info" role="status">AO3 登录凭据设置已更新。</p>'
    elif notice == "ao3_logout":
        msg_html = '<p id="account-flash-msg" class="notice is-info" role="status">已退出 AO3 账号，本地凭据与 Cookie 已清除（已切回仅公开模式）。</p>'
    elif notice == "password":
        msg_html = '<p id="account-flash-msg" class="notice is-info" role="status">站点访问密码已更新，其余设备会话已自动失效。</p>'

    env_tip = (
        '<p id="ao3-env-tip" class="notice is-info">提示：环境变量中仍检测到 AO3_USER / AO3_PASSWORD。保存此处设置后，可从 .env 删除明文账号。</p>'
        if env_creds and not creds_status["configured"] else ""
    )
    job = cred_job or {}
    job_state = str(job.get("state") or "idle")
    job_msg = str(job.get("message") or "")
    job_tone = str(job.get("tone") or "info")
    show_live = (not error and not notice and job_state == "running" and bool(job_msg))
    live_cls = f"cred-live-box notice {_live_tone_class(job_tone)}" + (" is-running" if job_state == "running" else "")
    live_attr = ' role="status" aria-live="polite"' if show_live else ' role="status" aria-live="polite" hidden'
    is_running = job_state == "running"
    ao3_logged_in = _is_ao3_logged_in(archive, creds_status, env_creds)
    cred_fields = _ao3_cred_fields(
        auth_mode=effective_mode,
        ao3_user=effective_user,
        has_saved_password=creds_status["has_password"],
    )

    return page("账号与安全", f"""
<div class="page-heading">
  <h1>账号与安全</h1>
</div>
{msg_html}{env_tip}
<div class="account-grid">
  <section class="account-card">
    <div class="account-card-head">
      <h2>AO3 抓取凭据</h2>
      {status_pill}
    </div>
    <p class="muted">仅公开模式无需登录；使用账号凭据时，保存前必须完成 AO3 在线校验（若触发 429 限流或超时会在下方实时显示重试进度）。</p>
    <form class="auth-form" method="post" action="/account" data-ao3-cred-form
          data-cred-job-state="{escape(job_state)}">{proof_field}
      <input type="hidden" name="action" value="credentials">
      {cred_fields}
      <div id="ao3-cred-live" class="{live_cls}"{live_attr}>
        <span class="cred-live-dot" aria-hidden="true"></span>
        <span data-cred-live-text>{escape(job_msg)}</span>
      </div>
      <div class="auth-actions is-split">
        <button class="button danger-outline" type="submit" form="ao3-logout-form"
                data-ao3-logout-btn{' hidden' if (not ao3_logged_in or is_running) else ''}
                data-confirm-trigger="ao3"
                data-confirm-title="确认退出 AO3 账号？"
                data-confirm-body="将清除已保存的 AO3 凭据与本地 Cookie 并切回仅公开模式；已备份的作品数据不会删除。"
                data-confirm-ok="确认退出 AO3">退出 AO3 账号</button>
        <div class="auth-actions-end">
          <button class="button" type="button" data-cred-cancel{' hidden' if not is_running else ''}>取消校验</button>
          <button class="button primary" type="submit" data-cred-submit{' disabled' if is_running else ''}>{'校验中…' if is_running else ('保存设置' if effective_mode == 'public' else '保存并校验')}</button>
        </div>
      </div>
    </form>
    <form id="ao3-logout-form" method="post" action="/account/ao3-logout" hidden>{proof_field}</form>
  </section>
  <section class="account-card">
    <div class="account-card-head">
      <h2>站点访问密码</h2>
    </div>
    <p class="muted">用于保护整个归档站点的阅读与管理界面。修改密码后，其他浏览器和设备需重新登录。</p>
    <form class="auth-form" method="post" action="/account">{proof_field}
      <input type="hidden" name="action" value="password">
      <label><span class="field-label">当前访问密码</span>
        <input type="password" name="current_password" required autocomplete="current-password" placeholder="输入当前密码">
      </label>
      <label><span class="field-label">新访问密码</span>
        <input type="password" name="new_password" minlength="6" required autocomplete="new-password" placeholder="至少 6 位字符">
      </label>
      <label><span class="field-label">确认新密码</span>
        <input type="password" name="confirm_password" minlength="6" required autocomplete="new-password" placeholder="再次输入新密码">
      </label>
      <div class="auth-actions">
        <button class="button primary" type="submit">更新站点密码</button>
      </div>
    </form>
  </section>
</div>""", active_nav="account", logout_proof=proof_val)


def serve(
    archive: Archive, host: str, port: int, *,
    ao3_login_fn: Callable[..., None] | None = None,
    on_bind: Callable[[ThreadingHTTPServer], None] | None = None,
) -> None:
    css = Path(__file__).with_name("ao3_web.css").read_bytes()
    backup_script = Path(__file__).with_name("ao3_web.js").read_bytes()
    reader_script = Path(__file__).with_name("ao3_reader.js").read_bytes()
    allowed_hosts = {"localhost", "127.0.0.1", "::1"} | {
        item.strip().casefold() for item in os.environ.get("WEB_ALLOWED_HOSTS", "").split(",") if item.strip()
    }
    secure = "; Secure" if os.environ.get("WEB_COOKIE_SECURE") == "1" else ""
    csrf_secret = secrets.token_bytes(32)

    def _default_login(
        out_dir: Path, user: str, password: str, *,
        timeout: int = 60, prefer_v4: bool = True, retries: int = 3,
        stop: threading.Event | None = None,
        status_cb: Callable[[str], None] | None = None,
    ) -> None:
        from ao3_backup import verify_login_and_save_cookies

        verify_login_and_save_cookies(
            out_dir, user, password,
            timeout=timeout, prefer_v4=prefer_v4, retries=retries,
            stop=stop, status_cb=status_cb,
        )

    login_checker = ao3_login_fn or _default_login
    _checker_params = set(inspect.signature(login_checker).parameters)

    def _invoke_login_checker(
        user: str, password: str, *, timeout: int, prefer_v4: bool,
        stop: threading.Event | None = None,
        status_cb: Callable[[str], None] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"timeout": timeout, "prefer_v4": prefer_v4}
        if "retries" in _checker_params:
            kwargs["retries"] = 3
        if "stop" in _checker_params:
            kwargs["stop"] = stop
        if "status_cb" in _checker_params:
            kwargs["status_cb"] = status_cb
        login_checker(archive.root, user, password, **kwargs)

    cred_lock = threading.Lock()
    cred_state: dict[str, Any] = {
        "id": "",
        "state": "idle",
        "mode": "",
        "user": "",
        "message": "",
        "tone": "info",
        "started_at": 0.0,
        "finished_at": 0.0,
        "stop": None,
    }

    def _cred_job_snapshot() -> dict[str, Any]:
        with cred_lock:
            return {k: v for k, v in cred_state.items() if k != "stop"}

    def _cred_json_payload() -> dict[str, Any]:
        snap = _cred_job_snapshot()
        creds = archive.ao3_credentials_status()
        env_creds = bool(os.environ.get("AO3_USER", "").strip() and os.environ.get("AO3_PASSWORD", ""))
        badge_cls, badge_text = _ao3_status_badge(creds, env_creds)
        return {
            **snap,
            "job_id": snap.get("id", ""),
            "configured": creds["configured"],
            "auth_mode": creds["auth_mode"],
            "ao3_user": creds["user"],
            "has_password": creds["has_password"],
            "cookie_expired": creds["cookie_expired"],
            "env_creds": env_creds,
            "status_class": badge_cls,
            "status_text": badge_text,
            "ao3_logged_in": _is_ao3_logged_in(archive, creds, env_creds),
        }

    def _validate_cred_inputs(fields: dict[str, str], creds_status: dict) -> tuple[str, str, str, str]:
        a_mode = fields.get("ao3_auth_mode", "").strip()
        a_user = fields.get("ao3_user", "").strip()
        a_pw = fields.get("ao3_password", "")
        if a_mode not in AO3_AUTH_MODES:
            raise ValueError("请选择有效的 AO3 登录方式")
        if a_mode == "public":
            return "public", "", "", ""
        if a_mode == "cookie":
            if not a_user or not a_pw:
                raise ValueError("仅 Cookie 模式需要填写 AO3 用户名和密码以完成一次在线登录")
            return "cookie", a_user, a_pw, a_pw
        if not a_user:
            raise ValueError("请填写 AO3 用户名")
        user_changed = bool(creds_status["user"] and a_user != creds_status["user"])
        if user_changed and not a_pw:
            raise ValueError("修改 AO3 用户名时请同时重新输入密码")
        pw_to_verify = a_pw
        if not pw_to_verify:
            prev_mode, _, prev_pw = archive.decrypted_ao3_credentials()
            pw_to_verify = prev_pw if prev_mode == "encrypted" else ""
        if not pw_to_verify:
            raise ValueError("首次启用加密保存时请填写 AO3 密码")
        return "encrypted", a_user, pw_to_verify, a_pw

    def _start_cred_job(a_mode: str, a_user: str, pw_to_verify: str, pw_to_store: str, cur_settings: BackupSettings) -> None:
        job_id = secrets.token_hex(8)
        stop_ev = threading.Event()
        with cred_lock:
            prev_stop = cred_state.get("stop")
            if isinstance(prev_stop, threading.Event):
                prev_stop.set()
            cred_state.update({
                "id": job_id,
                "state": "running",
                "mode": a_mode,
                "user": a_user,
                "message": "正在启动 AO3 在线校验…",
                "tone": "info",
                "started_at": time.time(),
                "finished_at": 0.0,
                "stop": stop_ev,
            })

        def _on_status(msg: str) -> None:
            with cred_lock:
                if cred_state["id"] != job_id or cred_state["state"] != "running":
                    return
                cred_state["message"] = msg
                cred_state["tone"] = (
                    "warn" if any(k in msg for k in ("429", "限流", "重试", "超时", "异常")) else "info"
                )

        def _worker() -> None:
            try:
                _invoke_login_checker(
                    a_user, pw_to_verify,
                    timeout=cur_settings.timeout, prefer_v4=cur_settings.prefer_ipv4,
                    stop=stop_ev, status_cb=_on_status,
                )
            except BaseException as exc:  # noqa: BLE001
                with cred_lock:
                    if cred_state["id"] == job_id:
                        cancelled = stop_ev.is_set() or isinstance(exc, InterruptedError)
                        cred_state.update({
                            "state": "cancelled" if cancelled else "failed",
                            "tone": "info" if cancelled else "error",
                            "message": (
                                "已取消 AO3 凭据在线校验，未改动原有设置。" if cancelled
                                else (str(exc) or "AO3 凭据在线校验失败")
                            ),
                            "finished_at": time.time(),
                        })
                return

            try:
                with cred_lock:
                    if stop_ev.is_set() or cred_state["id"] != job_id:
                        if cred_state["id"] == job_id:
                            cred_state.update({
                                "state": "cancelled",
                                "tone": "info",
                                "message": "已取消 AO3 凭据在线校验，未改动原有设置。",
                                "finished_at": time.time(),
                            })
                        return
                    archive.set_ao3_credentials(a_mode, a_user, pw_to_store, clear_cookie_file=False)
                    cred_state.update({
                        "state": "succeeded",
                        "tone": "ok",
                        "message": "AO3 登录凭据已通过在线校验并保存（Cookie 已同步更新）。",
                        "finished_at": time.time(),
                    })
            except sqlite3.Error:
                LOG.exception("保存 AO3 校验结果失败")
                with cred_lock:
                    if cred_state["id"] == job_id:
                        cred_state.update({
                            "state": "failed",
                            "tone": "error",
                            "message": "AO3 验证已通过，但写入数据库失败，请稍后重试。",
                            "finished_at": time.time(),
                        })

        threading.Thread(target=_worker, name="ao3-cred-check", daemon=True).start()

    def _cancel_cred_job() -> None:
        with cred_lock:
            prev_stop = cred_state.get("stop")
            if isinstance(prev_stop, threading.Event):
                prev_stop.set()
            if cred_state["state"] == "running":
                cred_state.update({
                    "state": "cancelled",
                    "tone": "info",
                    "message": "已取消 AO3 凭据在线校验，未改动原有设置。",
                    "finished_at": time.time(),
                })

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def allowed(self) -> bool:
            if len(self.headers.get_all("Host", [])) != 1:
                return False
            try:
                name = urlsplit("//" + self.headers["Host"]).hostname
                return name in allowed_hosts
            except ValueError:
                return False

        def _cookie_value(self, name: str, pattern: str) -> str:
            try:
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                value = cookies[name].value if name in cookies else ""
            except CookieError:
                return ""
            return value if re.fullmatch(pattern, value) else ""

        def cookie(self) -> str:
            return self._cookie_value(COOKIE, r"[0-9a-f]{32}")

        def session_cookie(self) -> str:
            return self._cookie_value(SESSION_COOKIE, r"[0-9]+\.[0-9]+\.[0-9a-f]{64}")

        def is_authenticated(self) -> bool:
            return archive.verify_session_token(self.session_cookie())

        def session_headers(self, token: str) -> dict[str, str]:
            return {
                "Set-Cookie": f"{SESSION_COOKIE}={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict{secure}",
            }

        def clear_session_headers(self) -> dict[str, str]:
            return {
                "Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}",
            }

        def pre_auth_csrf(self) -> tuple[str, str, dict[str, str]]:
            cid = self._cookie_value(CSRF_COOKIE, r"[0-9a-f]{32}")
            headers: dict[str, str] = {}
            if not cid:
                cid = secrets.token_hex(16)
                headers["Set-Cookie"] = f"{CSRF_COOKIE}={cid}; Path=/; HttpOnly; SameSite=Strict{secure}"
            token = hmac.new(csrf_secret, cid.encode("ascii"), "sha256").hexdigest()
            return cid, token, headers

        def verify_pre_auth_csrf(self, proof_val: str) -> bool:
            cid = self._cookie_value(CSRF_COOKIE, r"[0-9a-f]{32}")
            if not cid or not re.fullmatch(r"[0-9a-f]{64}", proof_val or ""):
                return False
            expected = hmac.new(csrf_secret, cid.encode("ascii"), "sha256").hexdigest()
            return hmac.compare_digest(proof_val.encode("utf-8"), expected.encode("utf-8"))

        def reader_context(self, *, visit: bool = False) -> tuple[dict, dict[str, str]]:
            reader, new = archive.reader(self.cookie(), visit=visit)
            headers = {"Set-Cookie": f"{COOKIE}={reader['id']}; Path=/; Max-Age=31536000;"
                                     f" HttpOnly; SameSite=Strict{secure}"} if new else {}
            return reader, headers

        def respond(self, status: int, body: str | bytes, mime: str = "text/html; charset=utf-8",
                    headers: dict[str, str] | list[tuple[str, str]] | None = None, *, policy: str = PAGE_POLICY) -> None:
            raw = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Content-Security-Policy", policy)
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            header_items = headers.items() if isinstance(headers, dict) else (headers or ())
            for key, value in header_items:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if not self.allowed():
                self.respond(403, "访问地址未获允许", "text/plain; charset=utf-8")
                return
            try:
                target = urlsplit(self.path)
                query = {key: entries[0] for key, entries in parse_qs(target.query).items()}
                self.get_route(target.path, query)
            except (BrokenPipeError, ConnectionResetError):
                return
            except sqlite3.Error:
                LOG.exception("读取 SQLite 归档失败")
                self.respond(503, "暂时无法读取归档", "text/plain; charset=utf-8")
            except Exception:
                LOG.exception("处理归档页面失败")
                self.respond(500, "暂时无法显示这个页面", "text/plain; charset=utf-8")

        def get_route(self, path, query):
            if path == "/health":
                self.respond(200, "ok\n", "text/plain; charset=utf-8")
                return
            if path == "/archive.css":
                self.respond(200, css, "text/css; charset=utf-8")
                return
            if path == "/reader.js":
                self.respond(200, reader_script, "text/javascript; charset=utf-8")
                return

            has_auth = archive.has_web_auth()
            if not has_auth:
                archive.ensure_setup_token()
                if path != "/setup":
                    if path in {"/backup/status", "/account/credentials/status"}:
                        self.respond(401, json.dumps({"error": "setup_required"}), "application/json; charset=utf-8")
                    else:
                        self.respond(303, "", headers={"Location": "/setup"})
                    return
                _, csrf_token, csrf_headers = self.pre_auth_csrf()
                _, cur_settings, _, _ = archive.backup_dashboard()
                has_existing = bool(_initial_bookmark_user(archive, cur_settings) or archive.has_works())
                self.respond(200, setup_html(csrf_token, has_existing_data=has_existing), headers=csrf_headers)
                return

            if path == "/setup":
                dest = "/" if self.is_authenticated() else "/login"
                self.respond(303, "", headers={"Location": dest})
                return

            if path == "/login":
                if self.is_authenticated():
                    self.respond(303, "", headers={"Location": _safe_next(query.get("next", "/"))})
                    return
                _, csrf_token, csrf_headers = self.pre_auth_csrf()
                self.respond(200, login_html(csrf_token, next_url=query.get("next", "/")), headers=csrf_headers)
                return

            if not self.is_authenticated():
                if path in {"/backup/status", "/account/credentials/status"}:
                    self.respond(401, json.dumps({"error": "unauthorized"}), "application/json; charset=utf-8")
                    return
                next_dest = _safe_next(self.path)
                login_loc = with_query("/login", {"next": next_dest}) if next_dest != "/" else "/login"
                self.respond(303, "", headers={"Location": login_loc})
                return

            if path == "/backup.js":
                self.respond(200, backup_script, "text/javascript; charset=utf-8")
                return
            if path == "/backup/status":
                scheduler, current_settings, active, last = archive.backup_dashboard()
                status = backup_status(scheduler, active, last, current_settings)
                if query.get("logs") == "1":
                    status["log"] = recent_backup_log(archive)
                self.respond(200, json.dumps(status, ensure_ascii=False), "application/json; charset=utf-8")
                return
            if path == "/account/credentials/status":
                self.respond(200, json.dumps(_cred_json_payload(), ensure_ascii=False), "application/json; charset=utf-8")
                return
            if path == "/schedule":
                reader, headers = self.reader_context()
                self.respond(
                    200,
                    schedule_html(archive, reader, notice="saved" if query.get("saved") == "1" else ""),
                    headers=headers,
                    policy=DASHBOARD_POLICY,
                )
                return
            if path == "/logs":
                reader, headers = self.reader_context()
                self.respond(
                    200,
                    logs_html(archive, reader, notice=query.get("backup", "")),
                    headers=headers,
                    policy=DASHBOARD_POLICY,
                )
                return
            if path == "/account":
                reader, headers = self.reader_context()
                creds = archive.ao3_credentials_status()
                self.respond(
                    200,
                    account_html(
                        archive, reader, creds_status=creds,
                        notice=query.get("saved", ""), cred_job=_cred_job_snapshot(),
                    ),
                    headers=headers,
                    policy=DASHBOARD_POLICY,
                )
                return
            view, sort = query.get("view", ""), query.get("sort", "")
            q = query.get("q", "")[:200].strip()
            view = view if view in VIEWS else ""
            sort = sort if sort in SORTS else "event"
            author, tag = query.get("author", "").strip(), query.get("tag", "").strip()
            wip, noted = query.get("wip") == "1", query.get("noted") == "1"
            page_num = positive_id(query.get("page", "")) or 1
            back = overview_query(view or "all", q, sort, author=author, tag=tag, wip=wip, noted=noted, page=page_num)
            latest_match = re.fullmatch(r"/works/([0-9]+)/latest", path)
            if latest_match:
                wid = latest_match[1]
                # A speculative navigation can request the body before anyone opens it.
                purpose = self.headers.get("Sec-Purpose", "") + " " + self.headers.get("Purpose", "")
                reader, headers = ({}, {}) if "prefetch" in purpose.lower() else self.reader_context()
                if saved := archive.read_latest_article(wid):
                    version, article, fallback = saved
                    html = article_html(
                        wid, version, article, back=back, fallback=fallback,
                        logout_proof=backup_proof(reader["id"]) if reader else "",
                    )
                    if reader:
                        try:
                            archive.mark_article_read(reader, wid, version.id)
                        except sqlite3.Error:
                            LOG.exception("记录已读状态失败")
                    self.respond(200, html, headers=headers, policy=READER_POLICY)
                else:
                    self.respond(303, "", headers={**headers, "Location": with_query(f"/works/{wid}", back)})
                return
            article_match = re.fullmatch(r"/works/([0-9]+)/versions/([0-9]{1,18})(?:/(md|json))?", path)
            if article_match:
                wid, vid, ext = article_match.groups()
                if saved := archive.read_article(wid, int(vid)):
                    version, article = saved
                    if ext:
                        if ext == "md":
                            body, mime = article.to_markdown(version.metadata), "text/markdown; charset=utf-8"
                        else:
                            body = json.dumps({
                                "schema_version": 1, "work_id": wid, "version_id": version.id,
                                "ao3_updated_at": version.ao3_updated_at, "captured_at": version.captured_at,
                                "metadata": version.metadata, "article": asdict(article),
                            }, ensure_ascii=False, indent=2) + "\n"
                            mime = "application/json; charset=utf-8"
                        export_title = " / ".join(article.fields.get("标题", ())) or work_title(wid, version.metadata)
                        self.respond(200, body, mime, {
                            "Content-Disposition": download_filename(wid, int(vid), export_title, ext),
                        })
                    else:
                        reader, headers = self.reader_context()
                        v_ctx = archive.version_info(wid, int(vid))
                        self.respond(
                            200,
                            article_html(
                                wid, version, article, back=back if query else None,
                                version_ctx=v_ctx, logout_proof=backup_proof(reader["id"]),
                            ),
                            headers=headers,
                            policy=READER_POLICY,
                        )
                    return
            file_match = re.fullmatch(r"/files/([0-9]+)/([0-9]{1,18})/([a-z0-9]+)", path)
            if file_match:
                wid, vid, ext = file_match.groups()
                file = archive.read_file(wid, int(vid), ext) if ext in DOWNLOAD_FORMATS else None
                if file:
                    name, raw = file
                    if ext == "html" and query.get("download") != "1":
                        self.respond(200, raw, policy=FILE_POLICY)
                    else:
                        disposition = "attachment; filename*=UTF-8''" + quote(name, safe="")
                        self.respond(200, raw, MIMES.get(ext, "application/octet-stream"),
                                     {"Content-Disposition": disposition}, policy=FILE_POLICY)
                    return
            work_match = re.fullmatch(r"/works/([0-9]+)", path)
            if path != "/" and not work_match:
                self.respond(404, page("未找到页面", '<p class="empty">未找到页面或文件。</p>'))
                return
            visit = path == "/" and not (view or q or author or tag or wip or noted or query.get("page"))
            reader, headers = self.reader_context(visit=visit)
            if work_match:
                status, html = work_html(archive, reader, work_match[1], {**query, **back})
                self.respond(status, html, headers=headers)
            else:
                html = overview_html(archive, reader, view, q, sort, author=author, tag=tag,
                                     wip=wip, noted=noted, page_num=page_num, notice=query.get("backup", ""))
                self.respond(200, html, headers=headers, policy=DASHBOARD_POLICY)

        def _is_fetch(self) -> bool:
            return self.headers.get("X-Requested-With", "").casefold() == "fetch"

        def respond_json(self, status: int, payload: dict[str, Any]) -> None:
            self.respond(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

        def do_POST(self):
            is_fetch = self._is_fetch()
            if not self.allowed():
                self.respond(403, "访问地址未获允许", "text/plain; charset=utf-8")
                return
            try:
                target = urlsplit(self.path)
                match = re.fullmatch(r"/works/([0-9]+)/read", target.path)
                length = positive_id(self.headers.get("Content-Length", ""))
                allowed_paths = {
                    "/setup", "/login", "/logout", "/account",
                    "/account/ao3-logout", "/account/credentials/cancel",
                    "/schedule", "/backup", "/overview/seen",
                }
                if not (match or target.path in allowed_paths) or not 0 < length <= 4096:
                    if is_fetch:
                        self.respond_json(400, {"state": "failed", "tone": "error", "message": "请求参数无效"})
                    else:
                        self.respond(400, "请求无效", "text/plain; charset=utf-8")
                    return
                try:
                    fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"),
                                      keep_blank_values=True, max_num_fields=48)
                except ValueError:
                    if is_fetch:
                        self.respond_json(400, {"state": "failed", "tone": "error", "message": "请求体解析失败"})
                    else:
                        self.respond(400, "请求无效", "text/plain; charset=utf-8")
                    return
                flat_fields = {key: values[0] for key, values in fields.items()}

                if target.path == "/setup":
                    self.post_setup(fields)
                    return
                if target.path == "/login":
                    self.post_login(flat_fields)
                    return
                if not archive.has_web_auth():
                    if is_fetch:
                        self.respond_json(401, {"state": "failed", "tone": "error", "message": "尚未完成初始化设置"})
                    else:
                        self.respond(303, "", headers={"Location": "/setup"})
                    return
                if not self.is_authenticated():
                    if is_fetch:
                        self.respond_json(401, {"state": "failed", "tone": "error", "message": "登录会话已失效，请刷新页面重新登录"})
                    else:
                        self.respond(303, "", headers={"Location": "/login"})
                    return

                rid = self.cookie()
                if not rid:
                    if is_fetch:
                        self.respond_json(400, {"state": "failed", "tone": "error", "message": "未检测到会话 Cookie，请刷新页面重试"})
                    else:
                        self.respond(400, page("需要会话 Cookie", (
                            '<p class="warning notice">未检测到浏览器会话 Cookie。请确认未禁用 Cookie'
                            '（局域网 HTTP 访问请设置 WEB_COOKIE_SECURE=0），然后返回总览重试。</p>'
                            '<p><a class="button" href="/">返回总览</a></p>'
                        )))
                    return

                if target.path == "/logout":
                    token = flat_fields.get("proof", "")
                    if not re.fullmatch("[0-9a-f]{64}", token) or not hmac.compare_digest(
                        token.encode("utf-8"), backup_proof(rid).encode("utf-8")
                    ):
                        self.respond(403, "请求来源验证失败", "text/plain; charset=utf-8")
                        return
                    self.respond(303, "", headers={**self.clear_session_headers(), "Location": "/login"})
                    return
                if target.path == "/account/ao3-logout":
                    token = flat_fields.get("proof", "")
                    if not re.fullmatch("[0-9a-f]{64}", token) or not hmac.compare_digest(
                        token.encode("utf-8"), backup_proof(rid).encode("utf-8")
                    ):
                        if is_fetch:
                            self.respond_json(403, {"state": "failed", "tone": "error", "message": "请求来源校验失败，请刷新页面重试"})
                        else:
                            self.respond(403, "请从账号安全页提交退出 AO3 操作", "text/plain; charset=utf-8")
                        return
                    _cancel_cred_job()
                    archive.set_ao3_credentials("public", "", "", clear_cookie_file=True)
                    with cred_lock:
                        cred_state.update({
                            "id": secrets.token_hex(8),
                            "state": "succeeded",
                            "mode": "public",
                            "user": "",
                            "message": "已退出 AO3 账号，本地凭据与 Cookie 已清除（已切回仅公开模式）。",
                            "tone": "ok",
                            "started_at": time.time(),
                            "finished_at": time.time(),
                            "stop": None,
                        })
                    if is_fetch:
                        self.respond_json(200, _cred_json_payload())
                    else:
                        self.respond(303, "", headers={"Location": "/account?saved=ao3_logout"})
                    return
                if target.path == "/account/credentials/cancel":
                    token = flat_fields.get("proof", "")
                    if not re.fullmatch("[0-9a-f]{64}", token) or not hmac.compare_digest(
                        token.encode("utf-8"), backup_proof(rid).encode("utf-8")
                    ):
                        if is_fetch:
                            self.respond_json(403, {"state": "failed", "tone": "error", "message": "请求来源校验失败，请刷新页面重试"})
                        else:
                            self.respond(403, "请从账号安全页提交取消操作", "text/plain; charset=utf-8")
                        return
                    _cancel_cred_job()
                    if is_fetch:
                        self.respond_json(200, _cred_json_payload())
                    else:
                        self.respond(303, "", headers={"Location": "/account"})
                    return
                if target.path == "/account":
                    self.post_account(rid, flat_fields)
                    return
                if target.path == "/schedule":
                    self.post_schedule(rid, fields)
                    return
                if target.path == "/backup":
                    self.post_backup(rid, target.query, fields)
                    return
                if target.path == "/overview/seen":
                    self.post_overview_seen(rid, target.query, flat_fields)
                    return
                seq = positive_id(fields.get("through", [""])[0])
                token = fields.get("proof", [""])[0]
                if not seq or not re.fullmatch("[0-9a-f]{64}", token) or not hmac.compare_digest(
                    token.encode("utf-8"), proof(rid, match[1], seq).encode("utf-8")
                ):
                    self.respond(403, "请从作品页提交已读状态", "text/plain; charset=utf-8")
                    return
                if archive.mark_read(rid, match[1], seq):
                    query = {key: values[0] for key, values in parse_qs(target.query).items()
                             if key in {"view", "q", "sort", "author", "tag", "wip", "noted", "page"}}
                    self.respond(303, "", headers={"Location": with_query(f"/works/{match[1]}", query)})
                else:
                    self.respond(404, "找不到更新记录", "text/plain; charset=utf-8")
            except (BrokenPipeError, ConnectionResetError):
                return
            except sqlite3.Error:
                LOG.exception("写入 SQLite 归档失败")
                if is_fetch:
                    self.respond_json(503, {"state": "failed", "tone": "error", "message": "暂时无法写入数据库，请稍后重试"})
                else:
                    self.respond(503, "暂时无法保存提交请求", "text/plain; charset=utf-8")
            except Exception:
                LOG.exception("处理归档提交失败")
                if is_fetch:
                    self.respond_json(500, {"state": "failed", "tone": "error", "message": "服务器内部错误，请稍后重试"})
                else:
                    self.respond(500, "暂时无法处理提交请求", "text/plain; charset=utf-8")

        def post_setup(self, fields: dict[str, list[str]]) -> None:
            if archive.has_web_auth():
                self.respond(303, "", headers={"Location": "/"})
                return
            proof_val = _field_first(fields, "proof", "")
            if not self.verify_pre_auth_csrf(proof_val):
                _, csrf_token, csrf_headers = self.pre_auth_csrf()
                self.respond(403, setup_html(csrf_token, error="页面表单已过期，请重新提交"), headers=csrf_headers)
                return

            setup_token = _field_first(fields, "setup_token", "").strip()
            web_pw = _field_first(fields, "web_password", "")
            web_pw_confirm = _field_first(fields, "web_password_confirm", "")
            _, cur_settings, _, _ = archive.backup_dashboard()

            def _fail(msg: str) -> None:
                _, csrf_token, csrf_headers = self.pre_auth_csrf()
                has_existing = bool(_initial_bookmark_user(archive, cur_settings) or archive.has_works())
                self.respond(400, setup_html(csrf_token, has_existing_data=has_existing, error=msg), headers=csrf_headers)

            if not archive.verify_setup_token(setup_token):
                _fail("初始化口令不正确，请从容器启动日志查看最新 [SETUP] 口令")
                return
            if len(web_pw) < 6:
                _fail("网页访问密码至少需要 6 个字符")
                return
            if web_pw != web_pw_confirm:
                _fail("两次输入的网页访问密码不一致")
                return
            try:
                session_token = archive.setup_web_auth(web_pw)
            except (ValueError, RuntimeError) as exc:
                _fail(str(exc))
                return
            except sqlite3.Error:
                LOG.exception("保存初始化配置失败")
                self.respond(503, "暂时无法保存初始化配置", "text/plain; charset=utf-8")
                return

            _, reader_headers = self.reader_context()
            resp_headers = [("Location", "/"), ("Set-Cookie", self.session_headers(session_token)["Set-Cookie"])]
            if "Set-Cookie" in reader_headers:
                resp_headers.append(("Set-Cookie", reader_headers["Set-Cookie"]))
            self.respond(303, "", headers=resp_headers)

        def post_login(self, fields: dict[str, str]) -> None:
            next_url = _safe_next(fields.get("next", "/"))
            if not self.verify_pre_auth_csrf(fields.get("proof", "")):
                _, csrf_token, csrf_headers = self.pre_auth_csrf()
                self.respond(403, login_html(csrf_token, next_url=next_url, error="页面表单已过期，请刷新重试"), headers=csrf_headers)
                return
            if not archive.verify_web_password(fields.get("password", "")):
                _, csrf_token, csrf_headers = self.pre_auth_csrf()
                self.respond(401, login_html(csrf_token, next_url=next_url, error="站点访问密码不正确"), headers=csrf_headers)
                return
            token = archive.issue_session_token()
            _, reader_headers = self.reader_context()
            resp_headers = [("Location", next_url), ("Set-Cookie", self.session_headers(token)["Set-Cookie"])]
            if "Set-Cookie" in reader_headers:
                resp_headers.append(("Set-Cookie", reader_headers["Set-Cookie"]))
            self.respond(303, "", headers=resp_headers)

        def post_account(self, rid: str, fields: dict[str, str]) -> None:
            is_fetch = self._is_fetch()
            token = fields.get("proof", "")
            if not re.fullmatch("[0-9a-f]{64}", token) or not hmac.compare_digest(
                token.encode("utf-8"), backup_proof(rid).encode("utf-8")
            ):
                if is_fetch:
                    self.respond_json(403, {"state": "failed", "tone": "error", "message": "页面验证令牌已过期，请刷新后重试"})
                else:
                    self.respond(403, "请从账号安全页提交修改", "text/plain; charset=utf-8")
                return
            reader, _ = archive.reader(rid, visit=False)
            creds_status = archive.ao3_credentials_status()
            action = fields.get("action", "")
            try:
                if action == "credentials":
                    a_mode, a_user, pw_to_verify, pw_to_store = _validate_cred_inputs(fields, creds_status)
                    _, cur_settings, _, _ = archive.backup_dashboard()
                    if is_fetch:
                        if a_mode == "public":
                            _cancel_cred_job()
                            archive.set_ao3_credentials("public", "", "", clear_cookie_file=True)
                            with cred_lock:
                                cred_state.update({
                                    "id": secrets.token_hex(8),
                                    "state": "succeeded",
                                    "mode": "public",
                                    "user": "",
                                    "message": "已切换为仅公开书签模式（未登录）。",
                                    "tone": "ok",
                                    "started_at": time.time(),
                                    "finished_at": time.time(),
                                    "stop": None,
                                })
                            self.respond_json(200, _cred_json_payload())
                            return
                        _start_cred_job(a_mode, a_user, pw_to_verify, pw_to_store, cur_settings)
                        self.respond_json(202, _cred_json_payload())
                        return

                    if a_mode == "public":
                        _cancel_cred_job()
                        archive.set_ao3_credentials("public", "", "", clear_cookie_file=True)
                        self.respond(303, "", headers={"Location": "/account?saved=credentials"})
                        return
                    _invoke_login_checker(
                        a_user, pw_to_verify,
                        timeout=cur_settings.timeout, prefer_v4=cur_settings.prefer_ipv4,
                    )
                    archive.set_ao3_credentials(a_mode, a_user, pw_to_store, clear_cookie_file=False)
                    self.respond(303, "", headers={"Location": "/account?saved=credentials_verified"})
                    return
                if action == "password":
                    cur_pw = fields.get("current_password", "")
                    new_pw = fields.get("new_password", "")
                    confirm_pw = fields.get("confirm_password", "")
                    if new_pw != confirm_pw:
                        raise ValueError("两次输入的新访问密码不一致")
                    new_session = archive.change_web_password(cur_pw, new_pw)
                    self.respond(303, "", headers={**self.session_headers(new_session), "Location": "/account?saved=password"})
                    return
                raise ValueError("无效的账户操作")
            except (ValueError, RuntimeError) as exc:
                if is_fetch:
                    self.respond_json(400, {"state": "failed", "tone": "error", "message": str(exc)})
                    return
                self.respond(
                    400,
                    account_html(
                        archive, reader, creds_status=creds_status, error=str(exc),
                        auth_mode=fields.get("ao3_auth_mode", ""), ao3_user=fields.get("ao3_user", ""),
                        cred_job=_cred_job_snapshot(),
                    ),
                    policy=DASHBOARD_POLICY,
                )
            except sqlite3.Error:
                LOG.exception("保存账户设置失败")
                if is_fetch:
                    self.respond_json(503, {"state": "failed", "tone": "error", "message": "暂时无法保存账户设置，请稍后重试"})
                else:
                    self.respond(503, "暂时无法保存账户设置", "text/plain; charset=utf-8")

        def _overview_back(self, query: str) -> dict[str, str]:
            return {key: values[0] for key, values in parse_qs(query).items()
                    if key in {"view", "q", "sort", "author", "tag", "wip", "noted", "page"}}

        def post_overview_seen(self, rid: str, query: str, fields: dict[str, str]) -> None:
            token = fields.get("proof", "")
            if not re.fullmatch("[0-9a-f]{64}", token) or not hmac.compare_digest(token, backup_proof(rid)):
                self.respond(403, "请从总览页提交已查看操作", "text/plain; charset=utf-8")
                return
            try:
                archive.mark_overview_seen(rid)
            except sqlite3.Error:
                LOG.exception("更新查看状态失败")
                self.respond(503, "暂时无法更新查看状态", "text/plain; charset=utf-8")
                return
            back = self._overview_back(query)
            back.pop("page", None)
            if back.get("view") == "since":
                back["view"] = "all"
            self.respond(303, "", headers={"Location": with_query("/", {**back, "backup": "seen"})})

        def post_schedule(self, rid: str, fields: dict[str, list[str]]) -> None:
            token = _field_first(fields, "proof", "")
            if not re.fullmatch("[0-9a-f]{64}", token) or not hmac.compare_digest(
                token.encode("utf-8"), backup_proof(rid).encode("utf-8")
            ):
                self.respond(403, "请从计划页提交抓取设置", "text/plain; charset=utf-8")
                return
            try:
                base_settings = archive.backup_settings() or archive.backup_dashboard()[1]
                archive.set_backup_settings(submitted_settings(fields, base=base_settings))
            except ValueError as exc:
                reader, _ = archive.reader(rid, visit=False)
                self.respond(
                    400,
                    schedule_html(archive, reader, error=str(exc), form_values=fields),
                    policy=DASHBOARD_POLICY,
                )
                return
            except sqlite3.Error:
                LOG.exception("保存抓取计划失败")
                self.respond(503, "暂时无法保存抓取设置", "text/plain; charset=utf-8")
                return
            self.respond(303, "", headers={"Location": "/schedule?saved=1"})

        def post_backup(self, rid: str, query: str, fields: dict[str, list[str]]) -> None:
            token = _field_first(fields, "proof", "")
            if not re.fullmatch("[0-9a-f]{64}", token) or not hmac.compare_digest(
                token.encode("utf-8"), backup_proof(rid).encode("utf-8")
            ):
                self.respond(403, "请从总览页提交抓取操作", "text/plain; charset=utf-8")
                return
            back = self._overview_back(query)
            from_logs = parse_qs(query).get("from", [""])[0] == "logs"
            action = _field_first(fields, "action", "")
            try:
                if action == "settings":
                    base_settings = archive.backup_settings() or archive.backup_dashboard()[1]
                    archive.set_backup_settings(submitted_settings(fields, base=base_settings))
                    notice = "saved"
                else:
                    notice = "queued" if archive.queue_backup(action) else "pending"
            except ValueError as exc:
                reader, _ = archive.reader(rid, visit=False)
                if action == "settings":
                    self.respond(
                        400,
                        schedule_html(archive, reader, error=str(exc), form_values=fields),
                        policy=DASHBOARD_POLICY,
                    )
                    return
                view = back.get("view", "all") if back.get("view") in VIEWS else "all"
                sort = back.get("sort", "event") if back.get("sort") in SORTS else "event"
                html = overview_html(
                    archive, reader, view, back.get("q", ""), sort,
                    author=back.get("author", ""), tag=back.get("tag", ""),
                    wip=back.get("wip") == "1", noted=back.get("noted") == "1",
                    page_num=positive_id(back.get("page", "")) or 1,
                    backup_error=str(exc), backup_values=fields,
                )
                self.respond(400, html, policy=DASHBOARD_POLICY)
                return
            except sqlite3.Error:
                LOG.exception("保存抓取操作失败")
                self.respond(503, "暂时无法保存抓取操作", "text/plain; charset=utf-8")
                return
            if from_logs:
                self.respond(303, "", headers={"Location": with_query("/logs", {"backup": notice})})
                return
            self.respond(303, "", headers={"Location": with_query("/", {**back, "backup": notice}) + "#backup"})

    with ThreadingHTTPServer((host, port), Handler) as server:
        if on_bind is not None:
            on_bind(server)
        print(f"归档总览已启动 http://{host}:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
