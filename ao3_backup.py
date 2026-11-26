#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ao3_backup.py — AO3 书签增量备份器（向后兼容入口与门面，Layer 7）
=================================================================

保持命令行入口、Docker ENTRYPOINT 与既有测试所需的公共符号，内部按
MIT 公理设计拆分为严格下三角依赖的无环分层模块。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from ao3_cli import build_config, main
from ao3_http import (
    BASE,
    HEADERS,
    RETRY_CODES,
    UA,
    AO3Client,
    AO3HttpConfig,
    CookieExpiredError,
    _diagnose_login_failure,
    _format_net_error,
    acquire_lock,
    atomic_write,
    clear_cookies_file,
    cookie_lock,
    extract_csrf_token,
    is_logged_in_html,
    normalize_url,
    parse_retry_after,
    prefer_ipv4,
    touch_marker,
    verify_login_and_save_cookies,
)
from ao3_legacy import (
    CHANGE_LABELS,
    CSS,
    INDEX_JS,
    PARAGRAPH_TAGS,
    ChapterParser,
    _iso_to_epoch,
    change_preview,
    chapter_diff,
    compare_versions,
    history_document,
    human_size,
    human_words,
    load_state,
    now_iso,
    paragraph_pair,
    prune_retired_files,
    render_metadata,
    revision_key,
    save_state,
    slugify,
    strip_html_paragraphs,
    strip_tags,
    ts_iso,
    write_history,
    write_index,
    write_manifest,
)
from ao3_scheduler import (
    BACKUP_POLL_SECONDS,
    RUNNING_MAX_IDLE_SECONDS,
    healthcheck,
    run_scheduled_pass,
    run_scheduler as _run_scheduler,
    setup_logging,
)
from ao3_scraper import (
    ANY_TITLE,
    AUTHOR_LINK,
    BLURB_TAG,
    BM_NOTES_BQ,
    BM_TAGS_UL,
    CHAPTERS_DD,
    DATETIME,
    FANDOMS_BLOCK,
    FREEFORM_LI,
    HEADING,
    KIND_ID,
    LIST_END,
    PAGINATION_NEXT_DISABLED,
    PAGINATION_NEXT_LINK,
    REL_LI,
    SUMMARY_BQ,
    TAG_LINK,
    UPDATED,
    USER_MOD,
    WORDS_DD,
    WORK_TITLE,
    Config,
    __version__,
    _archive_work,
    _extract_tags,
    _scan_bookmark_pages,
    configured_backup,
    download_work,
    env,
    env_bool,
    env_float,
    env_int,
    fetch_article,
    has_next_page,
    log,
    looks_like_login_redirect,
    looks_like_work,
    merge_blurb_metadata,
    next_run_at,
    parse_blurbs,
    parse_page,
    record_change,
    resolve_download_path,
    retry_works as _retry_works,
    run_once as _run_once,
    run_stats,
    settings_for_config,
    sync_metadata,
)
from ao3_store import (
    BACKUP_ACTIONS,
    BACKUP_LOG,
    DOWNLOAD_FORMATS,
    REVISION_NAME,
    Archive,
    BackupSettings,
    compute_next_runs,
    import_legacy,
    missing_attachments,
    parse_duration,
    parse_hhmm,
    resolve_schedule,
    schedule_label,
    snapshot,
    work_files,
)


def run_once(
    cfg: Config,
    client: AO3Client,
    state: dict[str, Any],
    stop: threading.Event,
    *,
    archive: Archive | None = None,
    downloader: Optional[Callable[[AO3Client, Config, dict[str, Any], str], tuple[bool, bytes, str]]] = None,
    writer: Optional[Callable[[Path, bytes | str | Path], None]] = None,
) -> tuple[dict[str, int], bool]:
    """Forward to ao3_scraper.run_once while respecting facade monkeypatches."""
    return _run_once(
        cfg,
        client,
        state,
        stop,
        archive=archive,
        downloader=download_work if downloader is None else downloader,
        writer=atomic_write if writer is None else writer,
    )


def retry_works(
    cfg: Config,
    client: AO3Client,
    archive: Archive,
    state: dict[str, Any],
    stop: threading.Event,
    *,
    downloader: Optional[Callable[[AO3Client, Config, dict[str, Any], str], tuple[bool, bytes, str]]] = None,
) -> tuple[dict[str, int], bool]:
    """Forward to ao3_scraper.retry_works while respecting facade monkeypatches."""
    return _retry_works(
        cfg,
        client,
        archive,
        state,
        stop,
        downloader=download_work if downloader is None else downloader,
    )


def run_scheduler(
    cfg: Config,
    archive: Archive,
    stop: threading.Event,
    execute_pass: Callable[[Config, str], tuple[bool, dict[str, int]]],
    *,
    poll_seconds: float | None = None,
) -> None:
    """Forward to ao3_scheduler.run_scheduler while respecting BACKUP_POLL_SECONDS patches."""
    _run_scheduler(
        cfg,
        archive,
        stop,
        execute_pass,
        poll_seconds=BACKUP_POLL_SECONDS if poll_seconds is None else poll_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
