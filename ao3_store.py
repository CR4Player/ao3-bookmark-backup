"""SQLite archive, reading positions, and migration from the file archive."""

from __future__ import annotations

import calendar
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from ao3_diff import Difference, Export, WORK_FIELDS, compare_snapshots, field_differences, read_export


DOWNLOAD_FORMATS = {"html", "epub", "pdf", "mobi", "azw3"}
REVISION_NAME = re.compile(r"(?:[0-9]+|old)(?:-[a-f0-9]{8}-[0-9]+)?")
DATABASE_NAME = "archive.sqlite3"
BACKUP_LOG = Path("logs/ao3_backup.log")
BACKUP_ACTIONS = {"scan": "立即刷新", "retry": "重试失败或缺失项"}
VISIT_WINDOW_SECONDS = 30 * 60
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


def snapshot(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in (*WORK_FIELDS, "all_freeforms", "hidden_fields") if key in record}


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


SCHEDULE_MODES = ("interval", "daily", "weekly", "monthly", "manual")
INTERVAL_UNITS = ("minute", "hour", "day")
ANCHOR_MODES = ("fixed", "after")
UNIT_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}
WEEKDAY_LABELS = ("一", "二", "三", "四", "五", "六", "日")


@dataclass(frozen=True)
class BackupSettings:
    run_at: str = "03:30"
    interval_seconds: float = 0
    delay: float = 1.5
    retries: int = 4
    max_errors: int = 5
    schedule_mode: str = ""
    interval_value: float = 0
    interval_unit: str = "hour"
    anchor_mode: str = "fixed"
    weekdays: tuple[int, ...] = (6,)
    month_day: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> BackupSettings:
        if not data:
            return cls()
        allowed = {f.name for f in fields(cls)}
        cleaned = {k: v for k, v in data.items() if k in allowed}
        if "weekdays" in cleaned:
            raw_days = cleaned["weekdays"]
            parsed: list[int] = []
            if isinstance(raw_days, (list, tuple)):
                for item in raw_days:
                    try:
                        day = int(item)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= day <= 6 and day not in parsed:
                        parsed.append(day)
            cleaned["weekdays"] = tuple(parsed) or (6,)
        if "month_day" in cleaned:
            try:
                cleaned["month_day"] = min(31, max(1, int(cleaned["month_day"])))
            except (TypeError, ValueError):
                cleaned["month_day"] = 1
        return cls(**cleaned)


def resolve_interval_display(seconds: float) -> tuple[float, str]:
    if seconds <= 0:
        return 6.0, "hour"
    for unit, scale in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= scale and seconds % scale == 0:
            return float(int(seconds // scale)), unit
    if seconds >= 86400:
        return round(seconds / 86400, 2), "day"
    if seconds >= 3600:
        return round(seconds / 3600, 2), "hour"
    return max(0.1, round(seconds / 60, 2)), "minute"


def resolve_schedule(settings: BackupSettings) -> dict[str, Any]:
    try:
        hh, mm = parse_hhmm(settings.run_at)
    except ValueError:
        hh, mm = 3, 30
    run_at = f"{hh:02d}:{mm:02d}"
    mode = (settings.schedule_mode if settings.schedule_mode in SCHEDULE_MODES
            else ("interval" if settings.interval_seconds > 0 else "daily"))
    unit = settings.interval_unit if settings.interval_unit in INTERVAL_UNITS else "hour"
    seconds = 0.0
    value = 6.0
    if mode == "interval":
        if settings.interval_seconds > 0:
            seconds = float(settings.interval_seconds)
        elif settings.interval_value > 0:
            seconds = float(settings.interval_value) * UNIT_SECONDS[unit]
        else:
            seconds = 3600.0
        if (settings.interval_value > 0 and settings.interval_unit in INTERVAL_UNITS
                and math.isclose(settings.interval_value * UNIT_SECONDS[unit], seconds, rel_tol=1e-6)):
            value = float(settings.interval_value)
        else:
            value, unit = resolve_interval_display(seconds)
    elif settings.interval_value > 0 and settings.interval_unit in INTERVAL_UNITS:
        value, unit = float(settings.interval_value), settings.interval_unit
    anchor = settings.anchor_mode if settings.anchor_mode in ANCHOR_MODES else "fixed"
    weekdays = tuple(sorted({int(d) for d in settings.weekdays if 0 <= int(d) <= 6})) or (6,)
    month_day = min(31, max(1, int(settings.month_day)))
    key = (mode, round(seconds, 3), anchor, run_at, weekdays, month_day)
    return {
        "mode": mode, "seconds": seconds, "value": value, "unit": unit,
        "anchor_mode": anchor, "run_at": run_at, "hour": hh, "minute": mm,
        "weekdays": weekdays, "month_day": month_day, "key": key,
    }


def compute_next_runs(settings: BackupSettings, count: int = 3, now: datetime | None = None) -> list[datetime]:
    if count <= 0:
        return []
    now = now or datetime.now().astimezone()
    sched = resolve_schedule(settings)
    mode = sched["mode"]
    hh, mm = sched["hour"], sched["minute"]
    if mode == "manual":
        return []
    if mode == "daily":
        runs: list[datetime] = []
        day = now.date()
        while len(runs) < count:
            cand = datetime(day.year, day.month, day.day, hh, mm, tzinfo=now.tzinfo)
            if cand > now:
                runs.append(cand)
            day += timedelta(days=1)
        return runs
    if mode == "weekly":
        targets = set(sched["weekdays"])
        runs = []
        day = now.date()
        while len(runs) < count:
            if day.weekday() in targets:
                cand = datetime(day.year, day.month, day.day, hh, mm, tzinfo=now.tzinfo)
                if cand > now:
                    runs.append(cand)
            day += timedelta(days=1)
        return runs
    if mode == "monthly":
        month_day = sched["month_day"]
        runs = []
        year, month = now.year, now.month
        while len(runs) < count:
            day = min(month_day, calendar.monthrange(year, month)[1])
            cand = datetime(year, month, day, hh, mm, tzinfo=now.tzinfo)
            if cand > now:
                runs.append(cand)
            month += 1
            if month > 12:
                year, month = year + 1, 1
        return runs

    seconds = sched["seconds"]
    step = timedelta(seconds=seconds)
    if sched["anchor_mode"] == "after":
        return [now + step * i for i in range(1, count + 1)]
    if sched["unit"] == "day" and float(sched["value"]).is_integer() and int(round(seconds)) % 86400 == 0:
        step_days = max(1, int(sched["value"]))
        origin_date = date(2026, 1, 1)
        k = max(0, (now.date() - origin_date).days // step_days)
        runs = []
        while len(runs) < count:
            d = origin_date + timedelta(days=k * step_days)
            cand = datetime(d.year, d.month, d.day, hh, mm, tzinfo=now.tzinfo)
            if cand > now:
                runs.append(cand)
            k += 1
        return runs
    if seconds <= 86400 and float(seconds).is_integer() and 86400 % int(seconds) == 0:
        base = datetime(now.year, now.month, now.day, hh, mm, tzinfo=now.tzinfo)
    else:
        base = datetime(2026, 1, 1, hh, mm, tzinfo=now.tzinfo)
    k = math.floor((now - base).total_seconds() / seconds) + 1
    cand = base + timedelta(seconds=k * seconds)
    while cand <= now:
        k += 1
        cand = base + timedelta(seconds=k * seconds)
    return [base + timedelta(seconds=(k + i) * seconds) for i in range(count)]


def schedule_label(settings: BackupSettings) -> str:
    sched = resolve_schedule(settings)
    mode = sched["mode"]
    if mode == "manual":
        return "已关闭自动定时（仅手动）"
    if mode == "daily":
        return f"每天 {sched['run_at']}"
    if mode == "weekly":
        days = "、".join(f"周{WEEKDAY_LABELS[d]}" for d in sched["weekdays"])
        return f"每周 {days} {sched['run_at']}"
    if mode == "monthly":
        day_txt = "最后一天" if sched["month_day"] == 31 else f"{sched['month_day']} 日"
        return f"每月{day_txt} {sched['run_at']}"
    val = sched["value"]
    num_txt = str(int(val)) if float(val).is_integer() else f"{val:g}"
    unit_cn = {"minute": "分钟", "hour": "小时", "day": "天"}[sched["unit"]]
    suffix = f"基准 {sched['run_at']}" if sched["anchor_mode"] == "fixed" else "完成后顺延"
    return f"每 {num_txt}{unit_cn}（{suffix}）"


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


class Archive:
    def __init__(self, directory: Path) -> None:
        self.path = directory / DATABASE_NAME

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

    def initialize(self) -> None:
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
                self._migrate_articles(db)
            if version < 4:
                db.execute(BACKUP_RUNS)
                db.execute("PRAGMA user_version=4")

    def _migrate_articles(self, db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE documents (digest TEXT PRIMARY KEY, body TEXT NOT NULL)")
        db.execute("ALTER TABLE versions ADD COLUMN document_digest TEXT REFERENCES documents(digest)")
        db.execute("ALTER TABLE versions ADD COLUMN parse_error TEXT")
        parsed: dict[str, tuple[str | None, str | None]] = {}
        for vid, digest in db.execute("SELECT version_id, digest FROM version_files WHERE format='html'"):
            if digest not in parsed:
                raw = db.execute("SELECT data FROM blobs WHERE digest=?", (digest,)).fetchone()[0]
                parsed[digest] = self._save_article(db, raw)
            db.execute("UPDATE versions SET document_digest=?, parse_error=? WHERE id=?",
                       (*parsed[digest], vid))

    @staticmethod
    def _save_article(db: sqlite3.Connection, raw: bytes) -> tuple[str | None, str | None]:
        try:
            article = read_export(raw)
        except UnicodeDecodeError:
            return None, "HTML 不是有效的 UTF-8"
        except (ValueError, RecursionError):
            return None, "HTML 缺少可完整读取的章节"
        return Archive._store_document(db, article), None

    @staticmethod
    def _store_document(db: sqlite3.Connection, article: Export) -> str:
        data = asdict(article)
        if not data["hidden_fields"]:
            del data["hidden_fields"]
        body = encode(data)
        digest = hashlib.sha256(body.encode()).hexdigest()
        db.execute("INSERT OR IGNORE INTO documents VALUES (?, ?)", (digest, body))
        return digest

    @staticmethod
    def _article(db: sqlite3.Connection, digest: str | None) -> Export | None:
        if digest is None:
            return None
        body = db.execute("SELECT body FROM documents WHERE digest=?", (digest,)).fetchone()[0]
        return Export.from_dict(json.loads(body))

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["record"])

    @staticmethod
    def _version(row: sqlite3.Row) -> Version:
        return Version(row["id"], row["ao3_updated_at"], row["captured_at"],
                       row["origin"], json.loads(row["metadata"]),
                       row["document_digest"], row["parse_error"])

    @staticmethod
    def _files(db: sqlite3.Connection, vid: int) -> dict[str, ArchiveFile]:
        return {row["format"]: ArchiveFile(row["name"], row["digest"], row["size"]) for row in
                db.execute("""SELECT f.*, b.size FROM version_files f JOIN blobs b USING(digest)
                              WHERE version_id=? ORDER BY format""", (vid,))}

    @staticmethod
    def _set_record(db: sqlite3.Connection, wid: str, record: dict[str, Any]) -> None:
        saved = {k: v for k, v in record.items() if k not in {
            "file", "files", "bytes", "history", "revision", "flag", "work_updated_iso", "article_saved",
        }}
        db.execute("""INSERT INTO works (id, record) VALUES (?, ?)
                      ON CONFLICT(id) DO UPDATE SET record=excluded.record""", (wid, encode(saved)))

    @staticmethod
    def _blobs(db: sqlite3.Connection, files: dict[str, tuple[str, bytes]]) -> dict[str, ArchiveFile]:
        result = {}
        for ext, (name, data) in files.items():
            digest = hashlib.sha256(data).hexdigest()
            db.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?, ?)", (digest, len(data), data))
            result[ext] = ArchiveFile(name, digest, len(data))
        return result

    @staticmethod
    def _attach(db: sqlite3.Connection, vid: int, files: dict[str, ArchiveFile]) -> None:
        db.executemany("INSERT INTO version_files VALUES (?, ?, ?, ?)",
                       [(vid, ext, file.name, file.digest) for ext, file in files.items()])

    def _add_version(
        self, db: sqlite3.Connection, wid: str, stamp: int | None, captured: str | None,
        origin: str, metadata: dict[str, Any], files: dict[str, ArchiveFile],
        document_digest: str | None = None, parse_error: str | None = None,
    ) -> int:
        vid = db.execute(
            """INSERT INTO versions (work_id, ao3_updated_at, captured_at, origin, metadata,
                                     document_digest, parse_error)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (wid, stamp or None, captured, origin, encode(metadata), document_digest, parse_error),
        ).lastrowid
        self._attach(db, vid, files)
        db.execute("UPDATE works SET latest_version=? WHERE id=?", (vid, wid))
        return vid

    @staticmethod
    def _event(
        db: sqlite3.Connection, wid: str, vid: int | None, kind: str, happened: str | None,
        changes: list[Difference], legacy: dict[str, Any] | None = None,
    ) -> None:
        labels = list(dict.fromkeys(change.heading for change in changes))
        if len(labels) > 6:
            labels = labels[:6] + [f"另有 {len(labels) - 6} 处变更"]
        db.execute("""INSERT INTO events (work_id, version_id, kind, happened_at, labels, legacy_changes)
                      VALUES (?, ?, ?, ?, ?, ?)""", (wid, vid, kind, happened, encode(labels),
                                                     encode(legacy or {})))

    def scan_state(self, bookmark_user: str) -> dict[str, Any]:
        state: dict[str, Any] = {"user": bookmark_user, "works": {}}
        if not self.path.is_file():
            return state
        with self.connect(readonly=True) as db:
            db.execute("BEGIN")
            user = db.execute("SELECT value FROM settings WHERE key='bookmark_user'").fetchone()
            if user and user[0].casefold() != bookmark_user.casefold():
                raise ValueError(f"数据库对应的书签账号为 {user[0]}，请检查 BOOKMARK_USER")
            works = {row["id"]: self._record(row) for row in db.execute("SELECT * FROM works")}
            # Dry runs can inspect a v2 archive before its next writable startup upgrades it.
            if db.execute("PRAGMA user_version").fetchone()[0] >= 3:
                for row in db.execute("""SELECT w.id FROM works w JOIN versions v ON v.id=w.latest_version
                                         WHERE v.document_digest IS NOT NULL"""):
                    works[row["id"]]["article_saved"] = True
            for row in db.execute("""SELECT w.id, f.format, f.name FROM works w
                                     JOIN version_files f ON f.version_id=w.latest_version"""):
                works[row["id"]].setdefault("files", {})[row["format"]] = row["name"]
        state["works"] = works
        return state

    def bind_user(self, user: str) -> None:
        with self.write() as db:
            db.execute("INSERT OR IGNORE INTO settings VALUES ('bookmark_user', ?)", (user,))

    @staticmethod
    def _setting(db: sqlite3.Connection, key: str) -> Any:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def backup_settings(self) -> BackupSettings | None:
        with self.connect(readonly=True) as db:
            saved = self._setting(db, "backup_settings")
            return BackupSettings.from_dict(saved) if saved is not None else None

    def set_backup_settings(self, settings: BackupSettings) -> None:
        with self.write() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('backup_settings', ?)", (encode(asdict(settings)),))

    def publish_scheduler(self, defaults: BackupSettings, formats: tuple[str, ...], next_run: int) -> None:
        with self.write() as db:
            state = {"defaults": asdict(defaults), "formats": formats, "next_run": next_run,
                     "seen_at": int(time.time())}
            db.execute("INSERT OR REPLACE INTO settings VALUES ('backup_scheduler', ?)", (encode(state),))

    def backup_dashboard(self) -> tuple[dict, BackupSettings, list[dict], dict | None]:
        with self.connect(readonly=True) as db:
            db.execute("BEGIN")
            scheduler = self._setting(db, "backup_scheduler") or {}
            settings = self._setting(db, "backup_settings") or scheduler.get("defaults", {})
            active = db.execute("SELECT * FROM backup_runs WHERE status IN ('queued', 'running') ORDER BY id")
            last = db.execute("SELECT * FROM backup_runs WHERE finished_at IS NOT NULL ORDER BY finished_at DESC, id DESC LIMIT 1").fetchone()
            return scheduler, BackupSettings.from_dict(settings), [dict(row) for row in active], dict(last) if last else None

    def queue_backup(self, action: str) -> bool:
        if action not in BACKUP_ACTIONS:
            raise ValueError("无效的抓取操作")
        with self.write() as db:
            if db.execute("SELECT 1 FROM backup_runs WHERE action=? AND status IN ('queued', 'running')",
                          (action,)).fetchone():
                return False
            db.execute("INSERT INTO backup_runs (action, source, status, requested_at) VALUES (?, 'manual', 'queued', ?)",
                       (action, timestamp()))
            return True

    def has_queued_backup(self) -> bool:
        with self.connect(readonly=True) as db:
            return db.execute("SELECT 1 FROM backup_runs WHERE status='queued' LIMIT 1").fetchone() is not None

    def claim_backup(self, source: str | None) -> tuple[int, str, str] | None:
        """Called under the backup process lock, including when recovering after a restart."""
        now = timestamp()
        with self.write() as db:
            db.execute("""UPDATE backup_runs SET status='interrupted', finished_at=?, summary='备份进程已重启'
                          WHERE status='running'""", (now,))
            job = db.execute("SELECT id, action, source FROM backup_runs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if job:
                db.execute("UPDATE backup_runs SET status='running', started_at=? WHERE id=?", (now, job["id"]))
                return job["id"], job["action"], job["source"]
            if source:
                cursor = db.execute("""INSERT INTO backup_runs (action, source, status, requested_at, started_at)
                                       VALUES ('scan', ?, 'running', ?, ?)""", (source, now, now))
                return cursor.lastrowid, "scan", source
            return None

    def finish_backup(self, job_id: int, status: str, summary: str = "") -> None:
        with self.write() as db:
            db.execute("UPDATE backup_runs SET status=?, finished_at=?, summary=? WHERE id=? AND status='running'",
                       (status, timestamp(), summary, job_id))
            db.execute("""DELETE FROM backup_runs WHERE id IN (
                          SELECT id FROM backup_runs WHERE finished_at IS NOT NULL
                          ORDER BY finished_at DESC, id DESC LIMIT -1 OFFSET 100)""")

    def save_status(self, wid: str, record: dict[str, Any]) -> None:
        with self.write() as db:
            self._set_record(db, wid, record)

    def save_capture(
        self, wid: str, record: dict[str, Any], article: Export | None,
        files: dict[str, tuple[str, bytes]],
    ) -> tuple[dict[str, Any], str]:
        with self.write() as db:
            previous = db.execute("""SELECT v.* FROM works w JOIN versions v ON v.id=w.latest_version
                                     WHERE w.id=?""", (wid,)).fetchone()
            old_id = previous["id"] if previous else None
            digest = previous["document_digest"] if previous else None
            if article is None and not digest:
                raise ValueError("补齐附件需要已经归档的正文")
            old_files = self._files(db, old_id) if old_id else {}
            incoming = self._blobs(db, files)
            new_meta = snapshot(record)
            baseline = previous
            if article is not None and old_id and not digest:
                baseline = db.execute("""SELECT * FROM versions WHERE work_id=? AND document_digest IS NOT NULL
                                         ORDER BY id DESC LIMIT 1""", (wid,)).fetchone() or previous
            old_article = self._article(db, baseline["document_digest"]) if baseline and article else None
            if old_article and article.hidden_fields:
                carried = {label: old_article.fields[label] for label in article.hidden_fields
                           if label in old_article.fields and label not in article.fields}
                article = replace(article, fields={**article.fields, **carried})
            changes = []
            if article is not None and baseline:
                changes = compare_snapshots(old_article, article,
                                            json.loads(baseline["metadata"]), new_meta)
            raw_change = any(old_files[ext].digest != incoming[ext].digest
                             for ext in old_files.keys() & incoming.keys())
            kind = ("new" if not old_id else
                    "updated" if any(c.section != "作品资料" or c.label in {"作品首注", "作品尾注"}
                                     for c in changes) else
                    "metadata" if changes else
                    "file" if raw_change else "")
            current = incoming if changes else {**old_files, **incoming}
            self._set_record(db, wid, record)
            if kind or not digest:
                if article is not None:
                    digest = self._store_document(db, article)
                vid = self._add_version(db, wid, record.get("updated_at"), record["downloaded_at"],
                                        "capture", new_meta, current, digest)
                if kind:
                    self._event(db, wid, vid, kind, record["downloaded_at"], changes)
            else:
                self._attach(db, old_id, {ext: file for ext, file in current.items()
                                          if ext not in old_files})
                if old_article is not None:
                    recovered = set(old_article.hidden_fields) - set(article.hidden_fields)
                    if recovered:
                        fields = {label: values for label, values in old_article.fields.items() if label not in recovered}
                        fields.update({label: article.fields[label] for label in recovered if label in article.fields})
                        enriched = replace(old_article, fields=fields,
                                           hidden_fields=[label for label in old_article.hidden_fields if label not in recovered])
                        metadata = json.loads(previous["metadata"])
                        metadata["hidden_fields"] = enriched.hidden_fields
                        if "附加标签" in recovered:
                            metadata.update(freeforms=list(fields.get("附加标签", ())), all_freeforms=True)
                        db.execute("UPDATE versions SET document_digest=?, metadata=? WHERE id=?",
                                   (self._store_document(db, enriched), encode(metadata), old_id))
            return {**record, "files": {ext: file.name for ext, file in current.items()}}, kind

    def reader(self, cookie: str, *, visit: bool = False) -> tuple[dict[str, Any], bool]:
        now = int(time.time())
        if cookie:
            with self.connect(readonly=True) as db:
                db.execute("BEGIN")
                row = db.execute("SELECT * FROM readers WHERE id=?", (cookie,)).fetchone()
                if row and (not visit or (
                    now - row["overview_at"] < VISIT_WINDOW_SECONDS and row["last_overview"] >=
                    db.execute("SELECT coalesce(max(id), 0) FROM events WHERE kind!='file'").fetchone()[0]
                )):
                    return dict(row), False
        with self.write() as db:
            row = db.execute("SELECT * FROM readers WHERE id=?", (cookie,)).fetchone() if cookie else None
            latest = db.execute("SELECT coalesce(max(id), 0) FROM events WHERE kind!='file'").fetchone()[0]
            if row is None:
                rid = uuid4().hex
                db.execute("INSERT INTO readers VALUES (?, ?, ?, ?, ?)", (rid, latest, latest, latest, now))
                return {"id": rid, "baseline": latest, "last_overview": latest,
                        "since": latest, "overview_at": now}, True
            reader = dict(row)
            if visit and (latest > reader["last_overview"] or
                          now - reader["overview_at"] >= VISIT_WINDOW_SECONDS):
                reader["since"], reader["last_overview"] = reader["last_overview"], latest
                reader["overview_at"] = now
                db.execute("UPDATE readers SET since=?, last_overview=?, overview_at=? WHERE id=?",
                           (reader["since"], latest, now, cookie))
            return reader, False

    def overview(self, reader: dict[str, Any]) -> list[dict[str, Any]]:
        with self.connect(readonly=True) as db:
            db.execute("BEGIN")
            rows = db.execute("""
                WITH latest AS (SELECT work_id, max(id) AS last_event FROM events WHERE kind!='file' GROUP BY work_id)
                SELECT w.*, e.id AS last_event, e.kind, e.labels, e.happened_at,
                       coalesce(r.through, ?) AS read_through,
                       (SELECT count(*) FROM events x WHERE x.work_id=w.id AND x.kind!='file' AND x.id>?) AS after_visit,
                       (SELECT max(id) FROM versions v WHERE v.work_id=w.id AND v.document_digest IS NOT NULL) AS readable_version,
                       v.parse_error, json_extract(d.body, '$.fields') AS article_fields
                FROM works w LEFT JOIN latest l ON l.work_id=w.id
                LEFT JOIN events e ON e.id=l.last_event
                LEFT JOIN versions v ON v.id=w.latest_version
                LEFT JOIN documents d ON d.digest=v.document_digest
                LEFT JOIN work_reads r ON r.work_id=w.id AND r.reader_id=?
                """, (reader["baseline"], reader["since"], reader["id"]))
            works = {r["id"]: {**dict(r), "record": self._record(r),
                               "article_fields": json.loads(r["article_fields"] or "{}"),
                               "labels": json.loads(r["labels"] or "[]"), "formats": [],
                               "since_labels": [], "unread_labels": []}
                     for r in rows}
            for row in db.execute("""
                SELECT e.* FROM works w
                LEFT JOIN work_reads r ON r.work_id=w.id AND r.reader_id=?
                JOIN events e ON e.work_id=w.id AND e.kind!='file' AND e.id>min(?, coalesce(r.through, ?))
                ORDER BY e.id DESC
                """, (reader["id"], reader["since"], reader["baseline"])):
                work = works[row["work_id"]]
                labels = json.loads(row["labels"])
                for active, key in ((row["id"] > reader["since"], "since_labels"),
                                    (row["id"] > work["read_through"], "unread_labels")):
                    if active and len(work[key]) < 6:
                        work[key] = list(dict.fromkeys([*work[key], *labels]))[:6]
            for row in db.execute("""SELECT w.id, f.format FROM works w
                                     JOIN version_files f ON f.version_id=w.latest_version
                                     ORDER BY f.format"""):
                works[row["id"]]["formats"].append(row["format"])
            return list(works.values())

    def detail(self, wid: str, reader: dict[str, Any]):
        with self.connect(readonly=True) as db:
            db.execute("BEGIN")
            work = db.execute("SELECT * FROM works WHERE id=?", (wid,)).fetchone()
            if not work:
                return None
            versions = {r["id"]: self._version(r) for r in db.execute(
                "SELECT * FROM versions WHERE work_id=? ORDER BY id", (wid,))}
            for r in db.execute("""SELECT f.*, b.size FROM version_files f JOIN blobs b USING(digest)
                                   JOIN versions v ON v.id=f.version_id WHERE v.work_id=?""", (wid,)):
                versions[r["version_id"]].files[r["format"]] = ArchiveFile(r["name"], r["digest"], r["size"])
            events = [dict(r) for r in db.execute(
                "SELECT * FROM events WHERE work_id=? ORDER BY id DESC", (wid,))]
            seen = db.execute("SELECT through FROM work_reads WHERE reader_id=? AND work_id=?",
                              (reader["id"], wid)).fetchone()
            return self._record(work), versions, events, seen[0] if seen else reader["baseline"]

    def compare(self, old: Version, new: Version) -> list[Difference]:
        if old.document_digest == new.document_digest and old.metadata == new.metadata:
            return []
        old_article = new_article = None
        if old.document_digest and new.document_digest:
            with self.connect(readonly=True) as db:
                old_article = self._article(db, old.document_digest)
                new_article = (old_article if old.document_digest == new.document_digest else
                               self._article(db, new.document_digest))
        return compare_snapshots(old_article, new_article, old.metadata, new.metadata)

    def read_article(self, wid: str, vid: int) -> tuple[Version, Export] | None:
        with self.connect(readonly=True) as db:
            row = db.execute("SELECT * FROM versions WHERE work_id=? AND id=?", (wid, vid)).fetchone()
            if row and (article := self._article(db, row["document_digest"])):
                version = self._version(row)
                version.files = self._files(db, row["id"])
                return version, article
        return None

    def read_latest_article(self, wid: str) -> tuple[Version, Export, bool] | None:
        with self.connect(readonly=True) as db:
            db.execute("BEGIN")
            row = db.execute("""
                SELECT v.*, w.latest_version
                FROM works w JOIN versions v ON v.work_id=w.id
                WHERE w.id=? AND v.document_digest IS NOT NULL
                ORDER BY v.id DESC LIMIT 1
                """, (wid,)).fetchone()
            if row and (article := self._article(db, row["document_digest"])):
                version = self._version(row)
                version.files = self._files(db, row["id"])
                return version, article, row["id"] != row["latest_version"]
        return None

    def version_info(self, wid: str, vid: int) -> tuple[int, int, int | None] | None:
        with self.connect(readonly=True) as db:
            db.execute("BEGIN")
            ids = [row[0] for row in db.execute(
                "SELECT id FROM versions WHERE work_id=? ORDER BY id", (wid,)
            )]
            if vid not in ids:
                return None
            work = db.execute("SELECT latest_version FROM works WHERE id=?", (wid,)).fetchone()
            latest = work[0] if work else ids[-1]
            return ids.index(vid) + 1, len(ids), latest

    def mark_overview_seen(self, rid: str) -> bool:
        now = int(time.time())
        with self.write() as db:
            cursor = db.execute(
                "UPDATE readers SET since=last_overview, overview_at=? WHERE id=?",
                (now, rid),
            )
            return cursor.rowcount > 0

    def read_file(self, wid: str, vid: int, ext: str) -> tuple[str, bytes] | None:
        with self.connect(readonly=True) as db:
            row = db.execute("""SELECT f.name, b.data FROM version_files f JOIN blobs b USING(digest)
                               JOIN versions v ON v.id=f.version_id
                               WHERE v.work_id=? AND v.id=? AND f.format=?""", (wid, vid, ext)).fetchone()
            return (row[0], row[1]) if row else None

    def mark_article_read(self, reader: dict[str, Any], wid: str, vid: int) -> None:
        with self.connect(readonly=True) as db:
            seq = db.execute("""
                SELECT max(id) FROM events
                WHERE work_id=? AND kind!='file' AND version_id<=?
                    AND id>coalesce((SELECT through FROM work_reads WHERE reader_id=? AND work_id=?), ?)
                """, (wid, vid, reader["id"], wid, reader["baseline"])).fetchone()[0]
        if seq is not None:
            self.mark_read(reader["id"], wid, seq)

    def mark_read(self, rid: str, wid: str, seq: int) -> bool:
        with self.write() as db:
            if db.execute("SELECT 1 FROM events WHERE work_id=? AND id=?", (wid, seq)).fetchone() is None:
                return False
            db.execute("""INSERT INTO work_reads VALUES (?, ?, ?)
                          ON CONFLICT(reader_id, work_id) DO UPDATE
                          SET through=max(work_reads.through, excluded.through)""", (rid, wid, seq))
            return True

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


def _legacy_stamp(key: str) -> int | None:
    head = key.split("-", 1)[0]
    return int(head) if head.isdecimal() and int(head) > 0 else None


def import_legacy(archive: Archive, source: Path) -> ImportResult:
    source = source.resolve()
    if source == archive.path.parent.resolve() or source in archive.path.resolve().parents:
        raise ValueError("迁移目标须使用独立目录")
    state_file = source / "state.json"
    raw_state = state_file.read_bytes()
    state = json.loads(raw_state)
    works = state["works"]
    fingerprint = hashlib.sha256(raw_state)
    missing = []
    captured_files = captured_bytes = 0

    def get_files(paths: dict[str, Path]) -> dict[str, tuple[str, bytes]]:
        nonlocal captured_bytes, captured_files
        result = {}
        for ext, path in sorted(paths.items()):
            if ext not in DOWNLOAD_FORMATS or not path.resolve().is_relative_to(source):
                raise ValueError(f"旧库存在意外文件路径：{path}")
            relative = path.relative_to(source).as_posix()
            fingerprint.update(encode(relative).encode())
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                missing.append(relative)
                fingerprint.update(b"missing")
                continue
            fingerprint.update(hashlib.sha256(data).digest())
            result[ext] = (path.name, data)
            captured_files += 1
            captured_bytes += len(data)
        return result

    archive.initialize()
    with archive.write() as db:
        imported = db.execute("SELECT value FROM settings WHERE key='legacy_fingerprint'").fetchone()
        if not imported and db.execute("SELECT 1 FROM works LIMIT 1").fetchone():
            raise ValueError("请将旧库导入一个空的 SQLite 归档")
        if not imported and (user := state.get("user")):
            bound = db.execute("SELECT value FROM settings WHERE key='bookmark_user'").fetchone()
            if bound and bound[0].casefold() != user.casefold():
                raise ValueError("旧库的书签账号与目标归档不一致")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('bookmark_user', ?)", (user,))

        for wid, record in sorted(works.items()):
            if not re.fullmatch(r"[0-9]+", wid):
                raise ValueError(f"旧库作品编号无效：{wid}")
            events = sorted(record.get("history") or [], key=lambda e: e.get("seq", 0))
            known = {e["revision"]: e for e in events if e.get("revision")}
            versions_dir = source / "versions" / wid
            groups: dict[str, dict[str, Path]] = {}
            if versions_dir.is_dir():
                for path in sorted(versions_dir.iterdir()):
                    ext = path.suffix.lower().lstrip(".")
                    if ext in DOWNLOAD_FORMATS and REVISION_NAME.fullmatch(path.stem):
                        groups.setdefault(path.stem, {})[ext] = path
            for ext, name in work_files(record).items():
                if Path(name).name != name or "/" in name or "\\" in name:
                    raise ValueError(f"旧库当前文件名无效：{name}")
            if not imported:
                archive._set_record(db, wid, record)
            ordered = sorted(groups, key=lambda key: (
                key in known, known.get(key, {}).get("seq", _legacy_stamp(key) or 0), key
            ))
            current = record.get("revision") or "current"
            paths = [(key, groups[key]) for key in ordered]
            current_paths = {ext: source / "works" / name for ext, name in work_files(record).items()}
            if current_paths:
                paths.append((current, current_paths))
            previous_digest = None
            previous_files: dict[str, ArchiveFile] = {}
            vids = {}
            for index, (key, version_paths) in enumerate(paths):
                files = get_files(version_paths)
                if imported or not files:
                    continue
                is_current = bool(current_paths) and index == len(paths) - 1
                event = known.get(key, {})
                stamp = record.get("updated_at") if is_current else _legacy_stamp(key)
                captured_at = record.get("downloaded_at") if is_current else event.get("at")
                stored = archive._blobs(db, files)
                digest, error = archive._save_article(db, files["html"][1]) if "html" in files else (None, None)
                vid = archive._add_version(db, wid, stamp, captured_at, "legacy",
                                           snapshot(record) if is_current else {}, stored, digest, error)
                vids[key] = vid
                changes = compare_snapshots(archive._article(db, previous_digest),
                                            archive._article(db, digest), {}, {})
                if event.get("changes"):
                    change_labels = {change.label for change in changes}
                    changes.extend(c for c in field_differences(event["changes"])
                                   if c.label not in change_labels)
                digests = {ext: file.digest for ext, file in stored.items()}
                old_digests = {ext: file.digest for ext, file in previous_files.items()}
                if not previous_files or changes or digests != old_digests:
                    kind = "new" if not previous_files else "updated" if changes else "file"
                    archive._event(db, wid, vid, kind, captured_at, changes, event.get("changes"))
                if digest:
                    previous_digest = digest
                previous_files = stored
                if is_current and (absent := current_paths.keys() - files.keys()):
                    archive._set_record(db, wid, {**record, "missing_current_formats": sorted(absent)})

            if not imported:
                if current_paths and current not in vids:
                    archive._set_record(db, wid, {**record, "missing_current_formats": sorted(current_paths)})
                for event in events:
                    if event.get("revision") in vids:
                        continue
                    changes = event.get("changes") or {}
                    kind = event["kind"] if event.get("kind") in {"new", "updated", "metadata"} else "file"
                    archive._event(db, wid, None, kind, event.get("at"), field_differences(changes), changes)

        if state_file.read_bytes() != raw_state:
            raise ValueError("导入期间旧库状态发生变化，请在抓取空闲时重试")
        digest = fingerprint.hexdigest()
        if imported and imported[0] != digest:
            raise ValueError("本库已完成迁移，旧库后来发生了变化。需要重新迁移时请使用新的空目录")
        if not imported:
            db.execute("INSERT INTO settings VALUES ('legacy_fingerprint', ?)", (digest,))
            # Existing readers can have opened the empty UI while migration was pending.
            last = db.execute("SELECT coalesce(max(id), 0) FROM events WHERE kind!='file'").fetchone()[0]
            db.execute("UPDATE readers SET baseline=?, last_overview=?, since=?", (last, last, last))
    return ImportResult(len(works), captured_files, captured_bytes, imported is not None, tuple(missing))
