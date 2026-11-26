"""Low-level SQLite connection manager, schema, value types, and shared helpers (Layer 0)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


DOWNLOAD_FORMATS = {"html", "epub", "pdf", "mobi", "azw3"}
REVISION_NAME = re.compile(r"(?:[0-9]+|old)(?:-[a-f0-9]{8}-[0-9]+)?")
DATABASE_NAME = "archive.sqlite3"
BACKUP_LOG = Path("logs/ao3_backup.log")
BACKUP_ACTIONS = {"scan": "立即刷新", "retry": "重试失败或缺失项"}
VISIT_WINDOW_SECONDS = 30 * 60

SCHEDULE_MODES = ("interval", "daily", "weekly", "monthly", "manual")
INTERVAL_UNITS = ("minute", "hour", "day")
ANCHOR_MODES = ("fixed", "after")
UNIT_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}
WEEKDAY_LABELS = ("一", "二", "三", "四", "五", "六", "日")
AO3_AUTH_MODES = ("public", "encrypted", "cookie")
SETUP_TOKEN_FILE = ".setup_token"
CREDENTIAL_KEY_FILE = ".credentials.key"
SETUP_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
PBKDF2_ITERATIONS = 200_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS works (
    id TEXT PRIMARY KEY,
    record TEXT NOT NULL,
    latest_version INTEGER REFERENCES versions(id)
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id),
    ao3_updated_at INTEGER,
    captured_at TEXT,
    origin TEXT NOT NULL,
    metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS versions_by_work ON versions(work_id, id);
CREATE TABLE IF NOT EXISTS blobs (
    digest TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS version_files (
    version_id INTEGER NOT NULL REFERENCES versions(id),
    format TEXT NOT NULL,
    name TEXT NOT NULL,
    digest TEXT NOT NULL REFERENCES blobs(digest),
    PRIMARY KEY (version_id, format)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id),
    version_id INTEGER REFERENCES versions(id),
    kind TEXT NOT NULL,
    happened_at TEXT,
    labels TEXT NOT NULL,
    legacy_changes TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_by_work ON events(work_id, id);
CREATE TABLE IF NOT EXISTS readers (
    id TEXT PRIMARY KEY,
    baseline INTEGER NOT NULL,
    last_overview INTEGER NOT NULL,
    since INTEGER NOT NULL,
    overview_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS work_reads (
    reader_id TEXT NOT NULL REFERENCES readers(id),
    work_id TEXT NOT NULL REFERENCES works(id),
    through INTEGER NOT NULL REFERENCES events(id),
    PRIMARY KEY (reader_id, work_id)
);
"""

BACKUP_RUNS = """
CREATE TABLE backup_runs (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL CHECK(action IN ('scan', 'retry')),
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    summary TEXT NOT NULL DEFAULT ''
)
"""


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def work_files(record: dict[str, Any]) -> dict[str, str]:
    files = dict(record.get("files") or {})
    if not files and (name := record.get("file")):
        files[Path(name).suffix.lower().lstrip(".") or "html"] = name
    return files


def missing_attachments(record: dict[str, Any], configured: Iterable[str], available: Iterable[str]) -> set[str]:
    missing = set(record.get("missing_current_formats") or ()) | set(record.get("failed_formats") or ())
    available = set(available)
    return {ext for ext in configured if ext in missing or ext not in available}


def parse_duration(value: str) -> timedelta | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*", value.lower())
    if not match:
        raise ValueError("间隔格式应为 30m、6h 或 1d")
    seconds = float(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
    try:
        return timedelta(seconds=seconds)
    except OverflowError as exc:
        raise ValueError("间隔超出允许范围") from exc


def parse_hhmm(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
    if not match or not (0 <= int(match[1]) <= 23 and 0 <= int(match[2]) <= 59):
        raise ValueError("每日抓取时间应为 HH:MM，例如 03:30")
    return int(match[1]), int(match[2])


@dataclass(frozen=True)
class ArchiveFile:
    name: str
    digest: str
    size: int


@dataclass
class Version:
    id: int
    ao3_updated_at: int | None
    captured_at: str | None
    origin: str
    metadata: dict[str, Any]
    document_digest: str | None = None
    parse_error: str | None = None
    files: dict[str, ArchiveFile] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportResult:
    works: int
    files: int
    size: int
    already_imported: bool
    missing: tuple[str, ...]


class Database:
    """Thin SQLite WAL connection and schema lifecycle manager."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / DATABASE_NAME

    @property
    def root(self) -> Path:
        return self.path.parent

    @contextmanager
    def connect(self, *, readonly: bool = False):
        path = self.path.resolve()
        with closing(sqlite3.connect(
            path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=30,
        )) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            yield db

    @contextmanager
    def write(self):
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def initialize(
        self,
        *,
        migrate_v1_articles: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4):
                raise ValueError(f"数据库结构版本 {version} 需要更新版的程序")
            if version == 0:
                for statement in SCHEMA.strip().rstrip(";").split(";"):
                    db.execute(statement)
            elif version == 1:
                db.execute("ALTER TABLE readers ADD COLUMN overview_at INTEGER NOT NULL DEFAULT 0")
            if version < 3:
                if migrate_v1_articles is not None:
                    migrate_v1_articles(db)
                else:
                    db.execute("CREATE TABLE documents (digest TEXT PRIMARY KEY, body TEXT NOT NULL)")
                    db.execute("ALTER TABLE versions ADD COLUMN document_digest TEXT REFERENCES documents(digest)")
                    db.execute("ALTER TABLE versions ADD COLUMN parse_error TEXT")
            if version < 4:
                db.execute(BACKUP_RUNS)
                db.execute("PRAGMA user_version=4")

    @staticmethod
    def get_setting(db: sqlite3.Connection, key: str) -> Any:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def set_setting(db: sqlite3.Connection, key: str, value: Any) -> None:
        db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, encode(value)))

    def backup(self, target: Path) -> None:
        target = target.resolve()
        active = self.path.resolve()
        if target.exists() or target in {active, Path(f"{active}-wal"), Path(f"{active}-shm")}:
            raise FileExistsError(f"备份目标已存在：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="archive-", suffix=".tmp", dir=target.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            with self.connect(readonly=True) as db, closing(sqlite3.connect(temporary)) as destination:
                db.backup(destination)
                destination.execute("PRAGMA journal_mode=DELETE")
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("备份文件未通过 SQLite 完整性校验")
            if target.exists():
                raise FileExistsError(f"备份目标已存在：{target}")
            temporary.rename(target)
        finally:
            temporary.unlink(missing_ok=True)
