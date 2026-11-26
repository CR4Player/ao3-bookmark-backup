"""Bookmark scraping, work fetching, and incremental archive execution (Layer 4)."""

from __future__ import annotations

import contextlib
import html as html_mod
import logging
import os
import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from ao3_content import paragraphs
from ao3_diff import Export, TAG_FIELDS, WORK_FIELDS, metadata_changes, read_work_page
from ao3_http import (
    COOKIE_FILE_NAME,
    AO3Client,
    CookieExpiredError,
    atomic_write,
    is_logged_in_html,
    touch_marker,
)
from ao3_legacy import (
    now_iso,
    record_change as _legacy_record_change,
    revision_key,
    save_state,
    slugify,
    strip_html_paragraphs,
    strip_tags,
    ts_iso,
)
from ao3_store import (
    BACKUP_LOG,
    Archive,
    BackupSettings,
    compute_next_runs,
    missing_attachments,
    resolve_schedule,
    snapshot,
    work_files,
)


__version__ = "2.1.1"
log = logging.getLogger("ao3")


# --------------------------------------------------------------------------
# 环境变量 & 运行配置
# --------------------------------------------------------------------------
def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    v = env(name, "").lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = env(name, "")
    if not v:
        return default
    try:
        return int(v)
    except ValueError as exc:
        raise SystemExit(f"环境变量 {name}={v!r} 不是有效整数") from exc


def env_float(name: str, default: float) -> float:
    v = env(name, "")
    if not v:
        return default
    try:
        return float(v)
    except ValueError as exc:
        raise SystemExit(f"环境变量 {name}={v!r} 不是有效数字") from exc


@dataclass
class Config:
    bookmark_user: str
    out_dir: Path
    user: str = ""
    password: str = ""
    delay: float = 1.5
    timeout: int = 60
    retries: int = 4
    max_pages: int = 200
    max_downloads: int = 0
    stop_after_idle_pages: int = 0
    max_errors: int = 5
    keep_versions: bool = True
    run_at: str = "03:30"
    interval: Optional[timedelta] = None
    once: bool = False
    dry_run: bool = False
    run_on_start: bool = True
    prefer_ipv4: bool = True
    formats: tuple[str, ...] = ("html",)
    refresh_content: bool = False
    auth_mode: str = ""

    @property
    def cookie_file(self) -> Path:
        return self.out_dir / COOKIE_FILE_NAME

    @property
    def setup_pending_file(self) -> Path:
        return self.out_dir / ".setup_pending"

    @property
    def works_dir(self) -> Path:
        return self.out_dir / "works"

    @property
    def versions_dir(self) -> Path:
        return self.out_dir / "versions"

    @property
    def state_file(self) -> Path:
        return self.out_dir / "state.json"

    @property
    def log_file(self) -> Path:
        return self.out_dir / BACKUP_LOG

    @property
    def running_file(self) -> Path:
        return self.out_dir / ".running"

    @property
    def last_run_file(self) -> Path:
        return self.out_dir / ".last_run"

    @property
    def lock_file(self) -> Path:
        return self.out_dir / ".backup.lock"


# --------------------------------------------------------------------------
# 解析书签页
# --------------------------------------------------------------------------
BLURB_TAG = re.compile(r'<li\b[^>]*\bid="bookmark_(\d+)"[^>]*>', re.S)
LIST_END = re.compile(
    r'<!--\s*/content\s*-->|<ol\b[^>]*\bclass="[^"]*\bpagination\b|<div\b[^>]*\bid="dynamic-bookmark',
    re.S,
)
KIND_ID = re.compile(r"\b(work|series|external_work)-(\d+)")
UPDATED = re.compile(r"<!--\s*updated_at=(\d+)\s*-->")
HEADING = re.compile(r'<h4 class="heading">(.*?)</h4>', re.S)
WORK_TITLE = re.compile(r'<a href="/works/\d+">(.*?)</a>', re.S)
ANY_TITLE = re.compile(r'<a href="/(?:works|series|external_works)/\d+">(.*?)</a>', re.S)
AUTHOR_LINK = re.compile(r'<a\b[^>]*\brel="author"[^>]*>(.*?)</a>', re.S)
FANDOMS_BLOCK = re.compile(r'<h5 class="fandoms heading">(.*?)</h5>', re.S)
TAG_LINK = re.compile(r'<a\b[^>]*\bclass="tag"[^>]*>(.*?)</a>', re.S)
REL_LI = re.compile(
    r'<li\b[^>]*\bclass=["\'][^"\']*\brelationships\b[^"\']*["\'][^>]*>(.*?)</li>', re.S
)
FREEFORM_LI = re.compile(
    r'<li\b[^>]*\bclass=["\'][^"\']*\bfreeforms\b[^"\']*["\'][^>]*>(.*?)</li>', re.S
)
WORDS_DD = re.compile(r'<dd class="words">(.*?)</dd>', re.S)
CHAPTERS_DD = re.compile(r'<dd class="chapters">(.*?)</dd>', re.S)
SUMMARY_BQ = re.compile(r'<blockquote class="userstuff summary">(.*?)</blockquote>', re.S)
DATETIME = re.compile(r'<p class="datetime">(.*?)</p>', re.S)
# 登录查看自己的书签时 AO3 会渲染为 class="own user module group"
USER_MOD = re.compile(r'<div class="(?:[^"]*\s)?user module group">', re.S)
BM_TAGS_UL = re.compile(r'<ul class="meta tags commas">(.*?)</ul>', re.S)
BM_NOTES_BQ = re.compile(r'<blockquote class="userstuff notes">(.*?)</blockquote>', re.S)
PAGINATION_NEXT_LINK = re.compile(
    r'<li\b[^>]*\bclass=["\'][^"\']*\bnext\b[^"\']*["\'][^>]*>\s*<a\b|'
    r'<a\b[^>]*\bclass=["\'][^"\']*\bnext\b[^"\']*["\']|'
    r'<a\b[^>]*\brel=["\']?next\b|'
    r'<a\b[^>]*\bhref=["\'][^"\']*[?&]page=\d+[^"\']*>[^<]*Next',
    re.S | re.I,
)
PAGINATION_NEXT_DISABLED = re.compile(
    r'<li\b[^>]*\bclass=["\'][^"\']*\bnext\b[^"\']*["\'][^>]*>\s*<span\b|'
    r'<li\b[^>]*\bclass=["\'][^"\']*\bdisabled\b[^"\']*\bnext\b|'
    r'<li\b[^>]*\bclass=["\'][^"\']*\bnext\b[^"\']*\bdisabled\b',
    re.S | re.I,
)


def has_next_page(page: str, total_li: int, current_page: int = 1) -> bool:
    """综合判断 AO3 书签页是否还有下一页。"""
    if total_li == 0:
        return False

    if PAGINATION_NEXT_DISABLED.search(page):
        return False

    m_next = re.search(
        r'<li\b[^>]*\bclass=["\'][^"\']*\bnext\b[^"\']*["\'][^>]*>\s*'
        r'<a\b[^>]*\bhref=["\'][^"\']*[?&]page=(\d+)',
        page,
        re.I,
    )
    if m_next:
        return int(m_next.group(1)) > current_page

    if re.search(r'href=["\'][^"\']*[?&]page=' + str(current_page + 1) + r'\b', page):
        return True

    if PAGINATION_NEXT_LINK.search(page):
        return True

    if total_li < 20:
        return False

    return True


def _extract_tags(html_fragment: str) -> list[str]:
    return [t for raw in TAG_LINK.findall(html_fragment) if (t := strip_tags(raw))]


def parse_page(page: str, current_page: int = 1) -> tuple[list[dict[str, Any]], int, bool]:
    """解析单页书签列表，返回 (可识别条目列表, 页面总书签 li 数, 是否有下一页)。"""
    tags = list(BLURB_TAG.finditer(page))
    has_next = has_next_page(page, len(tags), current_page)
    out: list[dict[str, Any]] = []

    for idx, tag in enumerate(tags):
        if idx + 1 < len(tags):
            end = tags[idx + 1].start()
        else:
            m_end = LIST_END.search(page, tag.end())
            end = m_end.start() if m_end else len(page)
        seg = page[tag.end(): end]

        kind_match = KIND_ID.search(tag.group(0))
        if not kind_match:
            continue
        kind, obj_id = kind_match.group(1), int(kind_match.group(2))

        user_mod = USER_MOD.search(seg)
        head, tail = (seg[: user_mod.start()], seg[user_mod.end():]) if user_mod else (seg, "")

        work_date = ""
        m = DATETIME.search(head)
        if m:
            work_date = strip_tags(m.group(1))

        bookmark_date = ""
        m = DATETIME.search(tail)
        if m:
            bookmark_date = strip_tags(m.group(1))

        title = ""
        author = ""
        m_head = HEADING.search(head)
        if m_head:
            heading_html = m_head.group(1)
            m_title = WORK_TITLE.search(heading_html) or ANY_TITLE.search(heading_html)
            if m_title:
                title = strip_tags(m_title.group(1))
            authors = [a for raw in AUTHOR_LINK.findall(heading_html) if (a := strip_tags(raw))]
            if authors:
                author = ", ".join(authors)
            elif "Anonymous" in strip_tags(heading_html):
                author = "Anonymous"

        fandoms: list[str] = []
        m = FANDOMS_BLOCK.search(head)
        if m:
            fandoms = _extract_tags(m.group(1))

        relationships: list[str] = []
        for blk in REL_LI.findall(head):
            relationships.extend(_extract_tags(blk))

        freeforms: list[str] = []
        for blk in FREEFORM_LI.findall(head):
            freeforms.extend(_extract_tags(blk))

        words = 0
        m = WORDS_DD.search(head)
        if m:
            digits = re.sub(r"\D", "", strip_tags(m.group(1)))
            words = int(digits) if digits else 0

        chapters = ""
        m = CHAPTERS_DD.search(head)
        if m:
            chapters = strip_tags(m.group(1))

        summary = ""
        m = SUMMARY_BQ.search(head)
        if m:
            summary = strip_html_paragraphs(m.group(1))

        bookmark_tags: list[str] = []
        m = BM_TAGS_UL.search(tail)
        if m:
            bookmark_tags = _extract_tags(m.group(1))

        bookmark_notes = ""
        m = BM_NOTES_BQ.search(tail)
        if m:
            bookmark_notes = strip_html_paragraphs(m.group(1))

        updated = None
        m = UPDATED.search(head)
        if m:
            updated = int(m.group(1))

        out.append(
            {
                "bookmark_id": int(tag.group(1)),
                "kind": kind,
                "object_id": obj_id,
                "title": title,
                "author": author,
                "fandoms": fandoms,
                "relationships": relationships,
                "freeforms": freeforms,
                "words": words,
                "chapters": chapters,
                "summary": summary,
                "work_date": work_date,
                "bookmark_date": bookmark_date,
                "bookmark_tags": bookmark_tags,
                "bookmark_notes": bookmark_notes,
                "updated_at": updated,
            }
        )
    return out, len(tags), has_next


def parse_blurbs(page: str) -> list[dict[str, Any]]:
    """向后兼容辅助函数：仅返回解析出的书签条目列表。"""
    blurbs, _, _ = parse_page(page)
    return blurbs


# --------------------------------------------------------------------------
# 下载与正文抽取
# --------------------------------------------------------------------------
def looks_like_work(raw: bytes, ext: str = "html") -> bool:
    """确认拿到的确实是作品本体，而不是 Cloudflare 错误页 / 登录重定向页。"""
    if not raw:
        return False
    if ext == "html":
        head = raw[:6000].lower()
        if b"<html" not in head or b"<title>" not in head:
            return False
        if b"ssl handshake failed" in head or b"just a moment" in head:
            return False
        text = raw.decode("utf-8", "replace")
        return 'id="preface"' in text or 'class="meta"' in text
    if ext == "epub":
        return raw[:4] == b"PK\x03\x04"
    if ext == "pdf":
        return raw[:5] == b"%PDF-"
    if ext in ("mobi", "azw3"):
        return b"BOOKMOBI" in raw[:200] or b"TPZ0" in raw[:200]
    return len(raw) > 512


def looks_like_login_redirect(raw: bytes) -> bool:
    """检测响应是否被重定向到了 AO3 登录页（如仅登录可见作品在 Cookie 失效时）。"""
    head = raw[:12000].decode("utf-8", "replace")
    return 'action="/users/login"' in head and not is_logged_in_html(head)


def fetch_article(client: AO3Client, work_id: int) -> tuple[Export, int]:
    path = f"/works/{work_id}?view_full_work=true&view_adult=true"
    raw = client.get_bytes(path)
    if (client.has_credentials() or getattr(getattr(client, "cfg", None), "auth_mode", "") == "cookie") and (
        looks_like_login_redirect(raw) or (client.logged_in and b"/users/logout" not in raw)
    ):
        if not client.has_credentials():
            raise CookieExpiredError("AO3 登录 Cookie 已失效（仅 Cookie 模式），请在网页「账号与安全」重新登录")
        if not client.login(force=True):
            raise ValueError("AO3 登录已失效，重新登录失败")
        raw = client.get_bytes(path)
        if b"/users/logout" not in raw:
            raise ValueError("重新登录后作品页仍未处于登录状态")
    try:
        return read_work_page(raw), len(raw)
    except UnicodeDecodeError as exc:
        raise ValueError("作品页不是有效的 UTF-8") from exc
    except RecursionError as exc:
        raise ValueError("作品页内容嵌套过深") from exc


def resolve_download_path(
    client: AO3Client, work_id: int, ext: str = "html"
) -> Optional[str]:
    """兜底：抓作品页，从下载下拉框里拿真实链接。"""
    try:
        page = client.get_text(f"/works/{work_id}?view_adult=true")
    except (HTTPError, RuntimeError, URLError, OSError) as exc:
        log.warning("取作品页失败 %s: %s", work_id, exc)
        return None
    m = re.search(r'href="(/downloads/[^"]+\.' + re.escape(ext) + r'[^"]*)"', page)
    return html_mod.unescape(m.group(1)) if m else None


def download_work(
    client: AO3Client, cfg: Config, work: dict[str, Any], ext: str = "html"
) -> tuple[bool, bytes, str]:
    work_id = int(work["object_id"])
    slug = slugify(work.get("title") or f"work_{work_id}")
    url_slug = quote(slug, safe="_")
    stamp = work.get("updated_at") or 0
    path = f"/downloads/{work_id}/{url_slug}.{ext}?updated_at={stamp}"

    try:
        raw = client.get_bytes(path)
    except HTTPError as exc:
        if exc.code == 404:
            try:
                alt = resolve_download_path(client, work_id, ext)
                if alt:
                    raw = client.get_bytes(alt)
                else:
                    return False, b"", "404（作品已删除、受限或需登录）"
            except (HTTPError, RuntimeError, URLError, OSError) as exc2:
                return False, b"", f"404 → 兜底也失败: {exc2}"
        else:
            return False, b"", f"HTTP {exc.code}"
    except (RuntimeError, URLError, OSError) as exc:
        return False, b"", str(exc)

    if not looks_like_work(raw, ext) and client.has_credentials() and looks_like_login_redirect(raw):
        log.info("  检测到受限作品需要重新登录，正在刷新登录态…")
        if client.login(force=True):
            try:
                raw = client.get_bytes(path)
            except (HTTPError, RuntimeError, URLError, OSError) as exc:
                return False, b"", f"重登后下载仍失败: {exc}"

    if not looks_like_work(raw, ext):
        alt = resolve_download_path(client, work_id, ext)
        if alt:
            try:
                raw = client.get_bytes(alt)
            except (HTTPError, RuntimeError, URLError, OSError) as exc:
                return False, b"", f"内容异常，兜底失败: {exc}"
    if not looks_like_work(raw, ext):
        return False, b"", "返回的不是作品正文（可能被限制或页面结构变化）"
    return True, raw, ""


# --------------------------------------------------------------------------
# 元数据合并 & 增量抓取流程
# --------------------------------------------------------------------------
def merge_blurb_metadata(
    rec: dict[str, Any], blurb: dict[str, Any], *, work_fields: bool = True
) -> None:
    """用书签页上现成的元数据补全本地记录。"""
    if work_fields:
        for key in (*WORK_FIELDS, "work_date"):
            value = blurb.get(key)
            can_clear = blurb.get("title") and (key in TAG_FIELDS or key == "summary")
            if value or (can_clear and value is not None):
                rec[key] = value
        if "freeforms" in blurb and blurb.get("title"):
            rec["all_freeforms"] = True
    if blurb.get("bookmark_date"):
        rec["bookmark_date"] = blurb["bookmark_date"]
    rec["bookmark_tags"] = list(blurb.get("bookmark_tags") or [])
    rec["bookmark_notes"] = str(blurb.get("bookmark_notes") or "")


def record_change(
    state: dict[str, Any], rec: dict[str, Any], kind: str,
    changes: dict[str, tuple[Any, Any]], *, previous: str = "",
) -> None:
    _legacy_record_change(state, rec, kind, changes, previous=previous)


def sync_metadata(
    state: dict[str, Any], rec: dict[str, Any], blurb: dict[str, Any]
) -> None:
    before = {key: rec.get(key) for key in (*WORK_FIELDS, "all_freeforms")}
    merge_blurb_metadata(rec, blurb)
    changes = metadata_changes(before, rec)
    if changes and work_files(rec):
        record_change(state, rec, "metadata", changes)
        rec["flag"] = "metadata"


def run_stats() -> dict[str, int]:
    return dict(
        pages=0,
        seen=0,
        new=0,
        updated=0,
        unchanged=0,
        skipped=0,
        skipped_errors=0,
        failed=0,
        downloaded=0,
        bytes=0,
    )


def _archive_work(
    cfg: Config, client: AO3Client, archive: Archive, works: dict[str, Any],
    blurb: dict[str, Any], stop: threading.Event, stats: dict[str, int], *,
    retry: bool = False,
    downloader: Callable[[AO3Client, Config, dict[str, Any], str], tuple[bool, bytes, str]] | None = None,
) -> bool:
    download_fn = downloader or download_work
    work_id = blurb["object_id"]
    wid = str(work_id)
    prior = works.get(wid, {})
    record = dict(prior)
    stamp = blurb["updated_at"] or prior.get("updated_at") or 0
    previous_bookmark = prior.get("bookmark_snapshot", snapshot(prior))
    current_bookmark = previous_bookmark if retry else snapshot(blurb)
    capture = (
        (cfg.refresh_content and not retry) or not prior.get("article_saved") or prior.get("error_count", 0) > 0
        or stamp != prior.get("updated_at")
        or bool(metadata_changes(previous_bookmark, current_bookmark))
    )
    existing_files = work_files(prior)
    missing = set(prior.get("missing_current_formats", ())) & set(cfg.formats)
    failed_formats = {ext: num for ext, num in (prior.get("failed_formats") or {}).items() if ext in cfg.formats}
    old_parser_error = "AO3 账号隐藏了警告或附加标签" in (prior.get("last_error") or "")
    if (cfg.refresh_content or old_parser_error or stamp != prior.get("failed_at_stamp")
            or (client.logged_in and not prior.get("failed_logged_in", True))):
        record["error_count"] = 0
        failed_formats.clear()
    if capture and not retry and record.get("error_count", 0) >= cfg.max_errors:
        stats["skipped_errors"] += 1
        return False

    targets = cfg.formats if capture else tuple(
        ext for ext in cfg.formats if (ext not in existing_files or ext in missing or ext in failed_formats)
        and (retry or failed_formats.get(ext, 0) < cfg.max_errors)
    )
    if not capture and not targets:
        if not cfg.dry_run:
            if not retry:
                merge_blurb_metadata(record, blurb, work_fields=False)
            if not failed_formats and not record.get("error_count"):
                for key in ("last_error", "failed_at_stamp", "failed_logged_in"):
                    record.pop(key, None)
            if "failed_formats" in record:
                record["failed_formats"] = failed_formats
            if "missing_current_formats" in record:
                record["missing_current_formats"] = sorted(missing)
            if record != prior:
                archive.save_status(wid, record)
                works[wid] = record
        if failed_formats:
            stats["skipped_errors"] += 1
        else:
            stats["unchanged"] += 1
        return False

    action = "核对全文" if capture else "补齐附件"
    log.info("  %s [%s] %s", action, wid, blurb["title"] or "(无标题)")
    if cfg.dry_run:
        stats["updated" if prior.get("article_saved") or existing_files else "new"] += 1
        return True

    checked = now_iso()
    if not retry:
        merge_blurb_metadata(record, blurb, work_fields=not (prior.get("article_saved") or existing_files))
    article = None
    size = 0
    if capture:
        try:
            article, size = fetch_article(client, work_id)
        except (InterruptedError, CookieExpiredError):
            raise
        except (ValueError, RuntimeError, URLError, OSError) as exc:
            message = f"抓取全文失败 · {exc}"
            log.warning("  [%s] %s", wid, message)
            record.update(
                error_count=record.get("error_count", 0) + 1, failed_at_stamp=stamp,
                failed_logged_in=client.logged_in, last_error=message, last_checked=checked,
                failed_formats=failed_formats,
            )
            archive.save_status(wid, record)
            works[wid] = record
            stats["failed"] += 1
            return False

        for key, label in WORK_FIELDS.items():
            if label in article.hidden_fields:
                continue
            values = article.fields.get(label, ())
            if key in TAG_FIELDS:
                record[key] = list(values)
            elif key == "summary":
                record[key] = "\n\n".join(paragraphs(article.sections.get(label, [])))
            elif key == "words":
                record[key] = int(values[0])
            else:
                record[key] = ", ".join(values)
        record.update(
            article_saved=True, bookmark_snapshot=current_bookmark,
            all_freeforms="附加标签" not in article.hidden_fields or bool(prior.get("all_freeforms")),
            hidden_fields=article.hidden_fields,
            updated_at=stamp, downloaded_at=checked, error_count=0,
        )
        if blurb.get("work_date"):
            record["work_date"] = blurb["work_date"]

    files: dict[str, tuple[str, bytes]] = {}
    errors = []
    base = f"{wid}_{slugify(record.get('title') or 'work')}"
    for ext in targets:
        if stop.is_set():
            raise InterruptedError
        try:
            ok, raw, error = download_fn(client, cfg, blurb, ext)
        except InterruptedError:
            raise
        except (RuntimeError, URLError, OSError) as exc:
            ok, raw, error = False, b"", str(exc)
        if ok:
            files[ext] = (f"{base}.{ext}", raw)
            size += len(raw)
            failed_formats.pop(ext, None)
            missing.discard(ext)
        else:
            failed_formats[ext] = failed_formats.get(ext, 0) + 1
            errors.append(f"{ext}: {error}")
            log.warning("  [%s] %s 附件下载失败 · %s", wid, ext, error)
    if stop.is_set():
        raise InterruptedError

    record.update(last_checked=checked, failed_formats=failed_formats)
    record.pop("missing_current_formats", None)
    if missing and not capture:
        record["missing_current_formats"] = sorted(missing)
    if errors:
        record.update(last_error="；".join(errors), failed_at_stamp=stamp, failed_logged_in=client.logged_in)
    elif not failed_formats:
        for key in ("last_error", "failed_at_stamp", "failed_logged_in"):
            record.pop(key, None)
    if article is None and not files:
        archive.save_status(wid, record)
        works[wid] = record
        stats["failed"] += 1
        return False

    record, kind = archive.save_capture(wid, record, article, files)
    works[wid] = record
    stats[{"new": "new", "updated": "updated", "metadata": "updated"}.get(kind, "unchanged")] += 1
    stats["downloaded"] += 1
    stats["bytes"] += size
    touch_marker(cfg.running_file)
    return True


def _scan_bookmark_pages(
    cfg: Config,
    client: AO3Client,
    state: dict[str, Any],
    stop: threading.Event,
    stats: dict[str, int],
    seen_ids: set[str],
    *,
    archive: Archive | None = None,
    sort_by_updated: bool = False,
    clear_flags_on_first_page: bool = False,
    downloader: Callable[[AO3Client, Config, dict[str, Any], str], tuple[bool, bytes, str]] | None = None,
    writer: Callable[[Path, bytes | str | Path], None] | None = None,
) -> tuple[bool, bool]:
    """扫描书签分页列表。"""
    download_fn = downloader or download_work
    write_fn = writer or atomic_write
    works = state["works"]
    idle_pages = 0
    user_quoted = quote(cfg.bookmark_user, safe="")
    mode_label = "按更新时间补扫" if sort_by_updated else "书签页"

    for page in range(1, cfg.max_pages + 1):
        if stop.is_set():
            log.warning("收到停止信号，本轮提前结束")
            return False, False

        if sort_by_updated:
            path = (
                f"/users/{user_quoted}/bookmarks"
                f"?bookmark_search%5Bsort_column%5D=bookmarkable_date&page={page}"
            )
        else:
            path = f"/users/{user_quoted}/bookmarks?page={page}"

        try:
            html = client.get_text(path)
        except InterruptedError:
            log.warning("收到停止信号，本轮提前结束")
            return False, False
        except HTTPError as exc:
            if exc.code == 404:
                log.error("书签页 404：用户 %s 不存在，或该用户书签不公开", cfg.bookmark_user)
            else:
                log.error("%s第 %d 页抓取失败: %s", mode_label, page, exc)
            stats["failed"] += 1
            return False, False
        except (RuntimeError, URLError, TimeoutError, OSError) as exc:
            log.error("%s第 %d 页抓取异常: %s", mode_label, page, exc)
            stats["failed"] += 1
            return False, False

        if page == 1 and not sort_by_updated:
            client.logged_in = is_logged_in_html(html)
            if not client.logged_in:
                if client.has_credentials():
                    log.info("检测到登录态已过期或未登录，正在重新登录…")
                    try:
                        ok = client.login(force=True)
                    except (InterruptedError, HTTPError, RuntimeError, URLError, OSError) as exc:
                        log.error("重新登录异常，中止本轮备份: %s", exc)
                        stats["failed"] += 1
                        return False, False
                    if not ok:
                        log.error("重新登录失败，为避免漏掉私有书签与受限作品，本轮中止")
                        stats["failed"] += 1
                        return False, False
                    try:
                        html = client.get_text(path)
                    except (InterruptedError, HTTPError, RuntimeError, URLError, OSError) as exc:
                        log.error("重登后抓取第 1 页失败: %s", exc)
                        stats["failed"] += 1
                        return False, False
                elif cfg.auth_mode == "cookie":
                    raise CookieExpiredError("AO3 登录 Cookie 已过期（仅 Cookie 模式），请在网页「账号与安全」重新登录")
                else:
                    log.warning("当前是未登录状态，私有书签不会出现；如需备份私有书签请配置账号密码")

            if clear_flags_on_first_page and not cfg.dry_run:
                for rec in works.values():
                    rec.pop("flag", None)

        blurbs, total_li, has_next = parse_page(html, current_page=page)
        if total_li == 0:
            log.info("%s第 %d 页没有书签，翻页结束", mode_label, page)
            return True, False

        unparsed_count = max(0, total_li - len(blurbs))
        if unparsed_count and not sort_by_updated:
            stats["seen"] += unparsed_count
            stats["skipped"] += unparsed_count
            log.debug("第 %d 页有 %d 条已删除或隐藏挑战书签，已跳过", page, unparsed_count)

        stats["pages"] += 1
        log.info("%s第 %d 页：%d 条书签", mode_label, page, total_li)

        page_changed = 0
        for b in blurbs:
            if stop.is_set():
                log.warning("收到停止信号，正在保存进度并退出…")
                return False, False

            b_key = f"{b['kind']}:{b['object_id']}"
            if b_key in seen_ids:
                continue
            seen_ids.add(b_key)

            stats["seen"] += 1
            if b["kind"] != "work":
                stats["skipped"] += 1
                log.debug("  跳过非单篇书签 [%s/%s] %s", b["kind"], b["object_id"], b["title"])
                continue

            if archive is not None:
                try:
                    page_changed += _archive_work(
                        cfg, client, archive, works, b, stop, stats, downloader=download_fn,
                    )
                except CookieExpiredError:
                    raise
                except InterruptedError:
                    log.warning("收到停止信号，正在保存已完成进度并退出…")
                    return False, False
                processed = stats["new"] + stats["updated"] if cfg.dry_run else stats["downloaded"]
                if cfg.max_downloads and processed >= cfg.max_downloads:
                    log.info("已达本次处理上限 %d，停止", cfg.max_downloads)
                    return True, False
                continue

            wid = str(b["object_id"])
            rec = works.get(wid)
            stamp = b["updated_at"] or (rec or {}).get("updated_at") or 0

            existing_files = work_files(rec or {})
            same_stamp = bool(rec and rec.get("updated_at") == stamp)
            failed_formats: dict[str, int] = (
                dict(rec.get("failed_formats") or {}) if (rec and same_stamp) else {}
            )

            if rec and (rec.get("error_count", 0) > 0 or failed_formats):
                stamp_changed = bool(stamp and rec.get("failed_at_stamp") != stamp)
                auth_upgraded = bool(client.logged_in and not rec.get("failed_logged_in", True))
                if stamp_changed or auth_upgraded:
                    rec["error_count"] = 0
                    failed_formats.clear()

            if same_stamp and rec is not None:
                missing_exts = [
                    ext
                    for ext in cfg.formats
                    if (ext not in existing_files or ext in rec.get("missing_current_formats", ()) or
                        not (cfg.works_dir / existing_files[ext]).is_file())
                    and failed_formats.get(ext, 0) < cfg.max_errors
                ]
                if not missing_exts:
                    if not cfg.dry_run:
                        sync_metadata(state, rec, b)
                    stats["unchanged"] += 1
                    continue
                target_exts = tuple(missing_exts)
            else:
                target_exts = cfg.formats

            if rec and rec.get("error_count", 0) >= cfg.max_errors:
                stats["skipped_errors"] += 1
                log.debug(
                    "  跳过连续失败达上限 (%d) 的作品 [%s] %s",
                    rec.get("error_count", 0),
                    wid,
                    b["title"] or "(无标题)",
                )
                continue

            had_backup = bool(existing_files)
            is_update = bool(had_backup and not same_stamp)
            action_label = "补齐格式" if (had_backup and same_stamp) else ("更新" if is_update else "新增")
            log.info(
                "  %s [%s] %s · %s",
                action_label,
                wid,
                b["title"] or "(无标题)",
                b["author"] or "?",
            )

            if cfg.dry_run:
                stats["updated" if (is_update or (had_backup and same_stamp)) else "new"] += 1
                page_changed += 1
                if cfg.max_downloads and (stats["new"] + stats["updated"]) >= cfg.max_downloads:
                    log.info("已达本次干跑上限 %d，停止", cfg.max_downloads)
                    return True, False
                continue

            got: dict[str, bytes] = {}
            last_err = ""
            try:
                for ext in target_exts:
                    ok, raw, err = download_fn(client, cfg, b, ext)
                    if ok:
                        got[ext] = raw
                        failed_formats.pop(ext, None)
                    else:
                        failed_formats[ext] = failed_formats.get(ext, 0) + 1
                        last_err = f"{ext}: {err}"
                        log.warning("  %s 格式下载失败: %s", ext, err)
            except InterruptedError:
                log.warning("收到停止信号，正在保存已完成进度并退出…")
                return False, False

            if not got:
                log.error("  下载失败 %s: %s", wid, last_err)
                rec = dict(rec) if rec else {}
                if same_stamp:
                    sync_metadata(state, rec, b)
                else:
                    merge_blurb_metadata(rec, b, work_fields=same_stamp or not had_backup)
                rec["error_count"] = int(rec.get("error_count", 0)) + 1
                rec["failed_at_stamp"] = stamp
                rec["failed_logged_in"] = client.logged_in
                rec["failed_formats"] = failed_formats
                rec["last_error"] = last_err
                rec["last_checked"] = now_iso()
                works[wid] = rec
                stats["failed"] += 1
                continue

            downloaded_bytes = sum(len(v) for v in got.values())
            base = f"{wid}_{slugify(b['title'] or 'work')}"
            seq = state.get("change_seq", 0) + 1
            revision = revision_key(state, stamp, seq)
            previous = ""
            if is_update:
                previous = rec.get("revision") or revision_key(state, rec.get("updated_at"), 0)
                base = f"{base}_{revision}"
            elif had_backup and same_stamp:
                base = Path(next(iter(existing_files.values()))).stem
            new_files = {ext: f"{base}.{ext}" for ext in got}
            merged_files = {
                ext: name for ext, name in existing_files.items()
                if same_stamp and (cfg.works_dir / name).is_file()
            }
            merged_files.update(new_files)

            try:
                for ext, raw in got.items():
                    write_fn(cfg.works_dir / new_files[ext], raw)
                if previous and cfg.keep_versions:
                    for ext, name in existing_files.items():
                        old = cfg.works_dir / name
                        if old.is_file():
                            write_fn(cfg.versions_dir / wid / f"{previous}.{ext}", old)
            except OSError as exc:
                log.error("  保存版本失败 %s，下轮重试: %s", wid, exc)
                stats["failed"] += 1
                if is_update:
                    for name in new_files.values():
                        with contextlib.suppress(OSError):
                            (cfg.works_dir / name).unlink(missing_ok=True)
                continue

            total_disk_bytes = 0
            for fname in merged_files.values():
                with contextlib.suppress(OSError):
                    total_disk_bytes += (cfg.works_dir / fname).stat().st_size

            new_rec: dict[str, Any] = dict(rec) if rec else {}
            if had_backup and same_stamp:
                sync_metadata(state, new_rec, b)
            else:
                merge_blurb_metadata(new_rec, b)
                new_rec["revision"] = revision
                changes = metadata_changes(rec or {}, new_rec) if is_update else {}
                record_change(
                    state, new_rec, "updated" if is_update else "new", changes,
                    previous=previous,
                )
            new_rec.update(
                {
                    "updated_at": stamp,
                    "work_updated_iso": ts_iso(stamp),
                    "file": merged_files.get("html") or next(iter(merged_files.values())),
                    "files": merged_files,
                    "bytes": total_disk_bytes or downloaded_bytes,
                    "downloaded_at": now_iso(),
                    "last_checked": now_iso(),
                    "error_count": 0,
                    "failed_formats": failed_formats,
                    "flag": "updated" if is_update else ("format" if had_backup else "new"),
                }
            )
            new_rec.pop("last_error", None)
            new_rec.pop("failed_at_stamp", None)
            new_rec.pop("failed_logged_in", None)
            works[wid] = new_rec
            if is_update:
                state.setdefault("retired_files", []).extend(existing_files.values())

            stats["updated" if (is_update or (had_backup and same_stamp)) else "new"] += 1
            stats["downloaded"] += 1
            stats["bytes"] += downloaded_bytes
            page_changed += 1

            if stats["downloaded"] % 10 == 0:
                save_state(cfg, state, writer=write_fn)
                touch_marker(cfg.running_file)

            if cfg.max_downloads and stats["downloaded"] >= cfg.max_downloads:
                log.info("已达本次下载上限 %d，停止", cfg.max_downloads)
                return True, False

        if not has_next:
            log.info("%s已到最后一页（第 %d 页）", mode_label, page)
            return True, False

        idle_pages = 0 if page_changed else idle_pages + 1
        if cfg.stop_after_idle_pages and idle_pages >= cfg.stop_after_idle_pages:
            log.info("%s连续 %d 页无变化，提前结束翻页", mode_label, idle_pages)
            return True, True

    return True, False


def retry_works(
    cfg: Config, client: AO3Client, archive: Archive, state: dict[str, Any], stop: threading.Event,
    *,
    downloader: Callable[[AO3Client, Config, dict[str, Any], str], tuple[bool, bytes, str]] | None = None,
) -> tuple[dict[str, int], bool]:
    works = state["works"]
    targets = [
        wid for wid, record in works.items()
        if not record.get("article_saved") or record.get("error_count")
        or missing_attachments(record, cfg.formats, work_files(record))
    ]
    stats = run_stats()
    log.info("待补抓 %d 篇作品", len(targets))
    for wid in targets:
        if stop.is_set():
            return stats, False
        record = works[wid]
        blurb = {
            "object_id": int(wid),
            "title": record.get("title", ""),
            "updated_at": record.get("failed_at_stamp") or record.get("updated_at"),
        }
        stats["seen"] += 1
        touch_marker(cfg.running_file)
        _archive_work(cfg, client, archive, works, blurb, stop, stats, retry=True, downloader=downloader)
        if cfg.max_downloads and stats["downloaded"] >= cfg.max_downloads:
            break
    return stats, True


def run_once(
    cfg: Config, client: AO3Client, state: dict[str, Any], stop: threading.Event,
    *,
    archive: Archive | None = None,
    downloader: Callable[[AO3Client, Config, dict[str, Any], str], tuple[bool, bytes, str]] | None = None,
    writer: Callable[[Path, bytes | str | Path], None] | None = None,
) -> tuple[dict[str, int], bool]:
    stats = run_stats()
    seen_ids: set[str] = set()

    page_ok, stopped_early = _scan_bookmark_pages(
        cfg,
        client,
        state,
        stop,
        stats,
        seen_ids,
        archive=archive,
        sort_by_updated=False,
        clear_flags_on_first_page=True,
        downloader=downloader,
        writer=writer,
    )

    if (
        page_ok
        and stopped_early
        and cfg.stop_after_idle_pages > 0
        and not stop.is_set()
        and not (cfg.max_downloads and stats["downloaded"] >= cfg.max_downloads)
    ):
        log.info("补扫按「作品更新时间」排序的书签页，检查早期收藏的连载更新…")
        ok2, _ = _scan_bookmark_pages(
            cfg,
            client,
            state,
            stop,
            stats,
            seen_ids,
            archive=archive,
            sort_by_updated=True,
            clear_flags_on_first_page=False,
            downloader=downloader,
            writer=writer,
        )
        page_ok = page_ok and ok2

    return stats, page_ok


def settings_for_config(cfg: Config) -> BackupSettings:
    return BackupSettings(
        run_at=cfg.run_at,
        interval_seconds=cfg.interval.total_seconds() if cfg.interval else 0,
        delay=cfg.delay,
        retries=cfg.retries,
        max_errors=cfg.max_errors,
        bookmark_user=cfg.bookmark_user,
        formats=cfg.formats,
        formats_configured=True,
        run_on_start=cfg.run_on_start,
        stop_after_idle_pages=cfg.stop_after_idle_pages,
        max_pages=cfg.max_pages,
        timeout=cfg.timeout,
        prefer_ipv4=cfg.prefer_ipv4,
    )


def next_run_at(source: Config | BackupSettings, now: Optional[datetime] = None) -> Optional[datetime]:
    settings = source if isinstance(source, BackupSettings) else settings_for_config(source)
    runs = compute_next_runs(settings, count=1, now=now)
    return runs[0] if runs else None


def configured_backup(
    cfg: Config, settings: BackupSettings, creds: tuple[str, str, str] | None = None,
) -> Config:
    sched = resolve_schedule(settings)
    interval = timedelta(seconds=sched["seconds"]) if sched["mode"] == "interval" else None
    bookmark_user = settings.bookmark_user or cfg.bookmark_user
    formats = settings.formats if settings.formats_configured else cfg.formats
    if creds and creds[0]:
        auth_mode, user, password = creds
    else:
        auth_mode = "encrypted" if (cfg.user and cfg.password) else "public"
        user, password = cfg.user, cfg.password
    return replace(
        cfg,
        bookmark_user=bookmark_user,
        user=user,
        password=password,
        auth_mode=auth_mode,
        formats=formats,
        run_at=sched["run_at"],
        interval=interval,
        delay=settings.delay,
        retries=settings.retries,
        max_errors=settings.max_errors,
        run_on_start=settings.run_on_start,
        stop_after_idle_pages=settings.stop_after_idle_pages,
        max_pages=settings.max_pages,
        timeout=settings.timeout,
        prefer_ipv4=settings.prefer_ipv4,
    )
