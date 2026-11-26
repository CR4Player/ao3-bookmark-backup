"""SQLite archive facade, diff service composition, and legacy migration (Layer 3)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from ao3_db import (
    ANCHOR_MODES, AO3_AUTH_MODES, BACKUP_ACTIONS, BACKUP_LOG, BACKUP_RUNS,
    CREDENTIAL_KEY_FILE, DATABASE_NAME, DOWNLOAD_FORMATS, INTERVAL_UNITS,
    PBKDF2_ITERATIONS, REVISION_NAME, SCHEDULE_MODES, SCHEMA, SETUP_ALPHABET,
    SETUP_TOKEN_FILE, UNIT_SECONDS, VISIT_WINDOW_SECONDS, WEEKDAY_LABELS,
    ArchiveFile, Database, ImportResult, Version, encode, missing_attachments,
    parse_duration, parse_hhmm, timestamp, work_files,
)
from ao3_diff import Difference, Export, WORK_FIELDS, compare_snapshots, field_differences, read_export
from ao3_http import clear_cookies_file
from ao3_repos import (
    AuthCredentialRepo, BackupSettings, ReaderStateRepo, SchedulerQueueRepo,
    WorkArchiveRepo, _credential_fernet, _fernet_class, _hash_password,
    _open_private_exclusive, compute_next_runs, resolve_interval_display,
    resolve_schedule, schedule_label, snapshot,
)


class Archive:
    """Backward-compatible facade composing four orthogonal domain repositories."""

    def __init__(self, directory: Path) -> None:
        self.db = Database(directory)
        self.path = self.db.path
        clock = lambda: time.time()  # noqa: E731 — keeps patch("ao3_store.time.time") working
        self.works = WorkArchiveRepo(self.db)
        self.auth = AuthCredentialRepo(self.db, clock=clock)
        self.scheduler = SchedulerQueueRepo(self.db, clock=clock)
        self.readers = ReaderStateRepo(self.db, clock=clock)

    @property
    def root(self) -> Path:
        return self.db.root

    def connect(self, *, readonly: bool = False):
        return self.db.connect(readonly=readonly)

    def write(self):
        return self.db.write()

    def initialize(self) -> None:
        self.db.initialize(migrate_v1_articles=self.works.migrate_v1_articles)

    def _migrate_articles(self, db: sqlite3.Connection) -> None:
        self.works.migrate_v1_articles(db)

    @staticmethod
    def _save_article(db: sqlite3.Connection, raw: bytes) -> tuple[str | None, str | None]:
        return WorkArchiveRepo._save_article(db, raw)

    @staticmethod
    def _store_document(db: sqlite3.Connection, article: Export) -> str:
        return WorkArchiveRepo._store_document(db, article)

    @staticmethod
    def _article(db: sqlite3.Connection, digest: str | None) -> Export | None:
        return WorkArchiveRepo._article(db, digest)

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return WorkArchiveRepo._record(row)

    @staticmethod
    def _version(row: sqlite3.Row) -> Version:
        return WorkArchiveRepo._version(row)

    @staticmethod
    def _files(db: sqlite3.Connection, vid: int) -> dict[str, ArchiveFile]:
        return WorkArchiveRepo._files(db, vid)

    @staticmethod
    def _set_record(db: sqlite3.Connection, wid: str, record: dict[str, Any]) -> None:
        WorkArchiveRepo._set_record(db, wid, record)

    @staticmethod
    def _blobs(db: sqlite3.Connection, files: dict[str, tuple[str, bytes]]) -> dict[str, ArchiveFile]:
        return WorkArchiveRepo._blobs(db, files)

    @staticmethod
    def _attach(db: sqlite3.Connection, vid: int, files: dict[str, ArchiveFile]) -> None:
        WorkArchiveRepo._attach(db, vid, files)

    def _add_version(
        self, db: sqlite3.Connection, wid: str, stamp: int | None, captured: str | None,
        origin: str, metadata: dict[str, Any], files: dict[str, ArchiveFile],
        document_digest: str | None = None, parse_error: str | None = None,
    ) -> int:
        return self.works._add_version(
            db, wid, stamp, captured, origin, metadata, files, document_digest, parse_error,
        )

    @staticmethod
    def _event(
        db: sqlite3.Connection, wid: str, vid: int | None, kind: str, happened: str | None,
        changes: list[Difference], legacy: dict[str, Any] | None = None,
    ) -> None:
        WorkArchiveRepo._event(db, wid, vid, kind, happened, changes, legacy)

    @staticmethod
    def _setting(db: sqlite3.Connection, key: str) -> Any:
        return Database.get_setting(db, key)

    # -- Works & captures --------------------------------------------------
    def scan_state(self, bookmark_user: str) -> dict[str, Any]:
        return self.works.scan_state(bookmark_user)

    def bind_user(self, user: str) -> None:
        self.works.bind_user(user)

    def stored_bookmark_user(self) -> str:
        return self.works.stored_bookmark_user()

    def has_works(self) -> bool:
        return self.works.has_works()

    def save_status(self, wid: str, record: dict[str, Any]) -> None:
        self.works.save_status(wid, record)

    def save_capture(
        self, wid: str, record: dict[str, Any], article: Export | None,
        files: dict[str, tuple[str, bytes]],
    ) -> tuple[dict[str, Any], str]:
        return self.works.save_capture(wid, record, article, files, differ=compare_snapshots)

    def read_article(self, wid: str, vid: int) -> tuple[Version, Export] | None:
        return self.works.read_article(wid, vid)

    def read_latest_article(self, wid: str) -> tuple[Version, Export, bool] | None:
        return self.works.read_latest_article(wid)

    def version_info(self, wid: str, vid: int) -> tuple[int, int, int | None] | None:
        return self.works.version_info(wid, vid)

    def read_file(self, wid: str, vid: int, ext: str) -> tuple[str, bytes] | None:
        return self.works.read_file(wid, vid, ext)

    # -- Diff service composition -----------------------------------------
    def compare(self, old: Version, new: Version) -> list[Difference]:
        if old.document_digest == new.document_digest and old.metadata == new.metadata:
            return []
        old_article = new_article = None
        if old.document_digest and new.document_digest:
            with self.db.connect(readonly=True) as db:
                old_article = self.works._article(db, old.document_digest)
                new_article = (
                    old_article
                    if old.document_digest == new.document_digest
                    else self.works._article(db, new.document_digest)
                )
        return compare_snapshots(old_article, new_article, old.metadata, new.metadata)

    # -- Web auth & AO3 credentials ---------------------------------------
    def has_web_auth(self) -> bool:
        return self.auth.has_web_auth()

    def ensure_setup_token(self) -> str:
        return self.auth.ensure_setup_token()

    def verify_setup_token(self, token: str) -> bool:
        return self.auth.verify_setup_token(token)

    def setup_web_auth(self, password: str) -> str:
        return self.auth.setup_web_auth(password)

    def reset_web_auth(self) -> str:
        return self.auth.reset_web_auth()

    def verify_web_password(self, password: str) -> bool:
        return self.auth.verify_web_password(password)

    def change_web_password(self, current_password: str, new_password: str) -> str:
        return self.auth.change_web_password(current_password, new_password)

    def _sign_session(self, auth: dict[str, Any], days: int = 30) -> str:
        return self.auth._sign_session(auth, days=days)

    def issue_session_token(self, days: int = 30) -> str:
        return self.auth.issue_session_token(days=days)

    def verify_session_token(self, token: str) -> bool:
        return self.auth.verify_session_token(token)

    def ao3_credentials_status(self) -> dict[str, Any]:
        return self.auth.ao3_credentials_status()

    def decrypted_ao3_credentials(self) -> tuple[str, str, str]:
        return self.auth.decrypted_ao3_credentials()

    def set_ao3_credentials(
        self, auth_mode: str, user: str = "", password: str = "", *, clear_cookie_file: bool = False,
    ) -> None:
        self.auth.set_ao3_credentials(
            auth_mode,
            user,
            password,
            on_clear_cookie=(lambda: clear_cookies_file(self.root)) if clear_cookie_file else None,
        )

    def mark_ao3_cookie_expired(self, expired: bool) -> None:
        self.auth.mark_ao3_cookie_expired(expired)

    # -- Scheduler & task queue -------------------------------------------
    def backup_settings(self) -> BackupSettings | None:
        return self.scheduler.backup_settings()

    def set_backup_settings(self, settings: BackupSettings) -> None:
        self.scheduler.set_backup_settings(settings)

    def publish_scheduler(
        self, defaults: BackupSettings, formats: tuple[str, ...], next_run: int,
        *, awaiting_setup: bool = False,
    ) -> None:
        self.scheduler.publish_scheduler(defaults, formats, next_run, awaiting_setup=awaiting_setup)

    def backup_dashboard(self) -> tuple[dict, BackupSettings, list[dict], dict | None]:
        return self.scheduler.backup_dashboard()

    def queue_backup(self, action: str) -> bool:
        return self.scheduler.queue_backup(action)

    def has_queued_backup(self) -> bool:
        return self.scheduler.has_queued_backup()

    def claim_backup(self, source: str | None) -> tuple[int, str, str] | None:
        return self.scheduler.claim_backup(source)

    def finish_backup(self, job_id: int, status: str, summary: str = "") -> None:
        self.scheduler.finish_backup(job_id, status, summary)

    # -- Reader state -----------------------------------------------------
    def reader(self, cookie: str, *, visit: bool = False) -> tuple[dict[str, Any], bool]:
        return self.readers.reader(cookie, visit=visit)

    def overview(self, reader: dict[str, Any]) -> list[dict[str, Any]]:
        return self.readers.overview(reader)

    def detail(self, wid: str, reader: dict[str, Any]):
        return self.readers.detail(wid, reader)

    def mark_overview_seen(self, rid: str) -> bool:
        return self.readers.mark_overview_seen(rid)

    def mark_article_read(self, reader: dict[str, Any], wid: str, vid: int) -> None:
        self.readers.mark_article_read(reader, wid, vid)

    def mark_read(self, rid: str, wid: str, seq: int) -> bool:
        return self.readers.mark_read(rid, wid, seq)

    # -- Online DB snapshot backup ----------------------------------------
    def backup(self, target: Path) -> None:
        self.db.backup(target)


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
            last = db.execute("SELECT coalesce(max(id), 0) FROM events WHERE kind!='file'").fetchone()[0]
            db.execute("UPDATE readers SET baseline=?, last_overview=?, since=?", (last, last, last))
    return ImportResult(len(works), captured_files, captured_bytes, imported is not None, tuple(missing))
