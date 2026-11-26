"""Orthogonal domain repositories backed by ao3_db.Database (Layer 2)."""

from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from ao3_db import (
    ANCHOR_MODES, AO3_AUTH_MODES, BACKUP_ACTIONS, CREDENTIAL_KEY_FILE,
    DOWNLOAD_FORMATS, INTERVAL_UNITS, PBKDF2_ITERATIONS, SCHEDULE_MODES,
    SETUP_ALPHABET, SETUP_TOKEN_FILE, UNIT_SECONDS, VISIT_WINDOW_SECONDS,
    WEEKDAY_LABELS, ArchiveFile, Database, Version, encode, parse_hhmm,
    timestamp,
)
from ao3_diff import Export, WORK_FIELDS, read_export


def _fernet_class():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError("缺少 cryptography 依赖，请执行 pip install -r requirements.txt") from exc
    return Fernet


def _open_private_exclusive(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(path), flags, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return True


def _credential_fernet(root: Path, *, create: bool = False):
    Fernet = _fernet_class()
    master = os.environ.get("AO3_MASTER_KEY", "").strip()
    if master:
        derived = base64.urlsafe_b64encode(hashlib.sha256(master.encode("utf-8")).digest())
        return Fernet(derived)
    key_path = root / CREDENTIAL_KEY_FILE
    if not key_path.exists():
        if not create:
            return None
        key_bytes = Fernet.generate_key().decode("ascii")
        _open_private_exclusive(key_path, key_bytes + "\n")
    raw = key_path.read_text(encoding="ascii").strip()
    try:
        return Fernet(raw.encode("ascii"))
    except ValueError as exc:
        raise RuntimeError("凭据密钥文件格式无效（.credentials.key）") from exc


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    raw_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), raw_salt, PBKDF2_ITERATIONS)
    return raw_salt.hex(), digest.hex()


def snapshot(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in (*WORK_FIELDS, "all_freeforms", "hidden_fields") if key in record}


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
    bookmark_user: str = ""
    formats: tuple[str, ...] = ()
    formats_configured: bool = False
    run_on_start: bool = True
    stop_after_idle_pages: int = 0
    max_pages: int = 200
    timeout: int = 60
    prefer_ipv4: bool = True

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
        if "formats" in cleaned:
            raw_formats = cleaned["formats"]
            if isinstance(raw_formats, (list, tuple)):
                cleaned["formats"] = tuple(dict.fromkeys(
                    str(f).strip().lower() for f in raw_formats
                    if str(f).strip().lower() in DOWNLOAD_FORMATS
                ))
            else:
                cleaned["formats"] = ()
        if "bookmark_user" in cleaned:
            cleaned["bookmark_user"] = str(cleaned["bookmark_user"] or "").strip()
        if "stop_after_idle_pages" in cleaned:
            try:
                cleaned["stop_after_idle_pages"] = max(0, int(cleaned["stop_after_idle_pages"]))
            except (TypeError, ValueError):
                cleaned["stop_after_idle_pages"] = 0
        if "max_pages" in cleaned:
            try:
                cleaned["max_pages"] = min(5000, max(1, int(cleaned["max_pages"])))
            except (TypeError, ValueError):
                cleaned["max_pages"] = 200
        if "timeout" in cleaned:
            try:
                cleaned["timeout"] = min(600, max(5, int(cleaned["timeout"])))
            except (TypeError, ValueError):
                cleaned["timeout"] = 60
        for flag in ("formats_configured", "run_on_start", "prefer_ipv4"):
            if flag in cleaned:
                cleaned[flag] = bool(cleaned[flag])
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
    import math

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
    import math

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


class AuthCredentialRepo:
    """Repository managing web site authentication and encrypted AO3 credentials."""

    def __init__(self, db: Database, *, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self._clock = clock

    def has_web_auth(self) -> bool:
        with self.db.connect(readonly=True) as db:
            auth = self.db.get_setting(db, "web_auth")
            return bool(auth and auth.get("hash") and auth.get("salt") and auth.get("session_secret"))

    def ensure_setup_token(self) -> str:
        if self.has_web_auth():
            return ""
        token_path = self.db.root / SETUP_TOKEN_FILE
        if not token_path.exists():
            raw = "".join(secrets.choice(SETUP_ALPHABET) for _ in range(12))
            formatted = f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}"
            _open_private_exclusive(token_path, formatted + "\n")
        return token_path.read_text(encoding="utf-8").strip()

    def verify_setup_token(self, token: str) -> bool:
        if self.has_web_auth():
            return False
        token_path = self.db.root / SETUP_TOKEN_FILE
        if not token_path.exists():
            return False
        normalize_re = r"[\s\-\u2010-\u2015\u2212\uff0d]+"
        expected = re.sub(normalize_re, "", token_path.read_text(encoding="utf-8").upper())
        supplied = re.sub(normalize_re, "", (token or "").upper())
        return bool(expected) and hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))

    def setup_web_auth(self, password: str) -> str:
        if len(password) < 6:
            raise ValueError("网页访问密码至少需要 6 个字符")
        if self.has_web_auth():
            raise ValueError("网页访问密码已初始化")
        salt_hex, digest_hex = _hash_password(password)
        auth = {
            "salt": salt_hex,
            "hash": digest_hex,
            "session_secret": secrets.token_hex(32),
            "auth_version": 1,
            "updated_at": timestamp(),
        }
        with self.db.write() as db:
            self.db.set_setting(db, "web_auth", auth)
        (self.db.root / SETUP_TOKEN_FILE).unlink(missing_ok=True)
        return self._sign_session(auth)

    def reset_web_auth(self) -> str:
        with self.db.write() as db:
            db.execute("DELETE FROM settings WHERE key='web_auth'")
        (self.db.root / SETUP_TOKEN_FILE).unlink(missing_ok=True)
        return self.ensure_setup_token()

    def verify_web_password(self, password: str) -> bool:
        with self.db.connect(readonly=True) as db:
            auth = self.db.get_setting(db, "web_auth")
        if not auth or not password:
            return False
        try:
            salt = bytes.fromhex(auth["salt"])
            expected = auth["hash"]
        except (KeyError, ValueError):
            return False
        _, actual = _hash_password(password, salt)
        return hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8"))

    def change_web_password(self, current_password: str, new_password: str) -> str:
        if not self.verify_web_password(current_password):
            raise ValueError("当前访问密码不正确")
        if len(new_password) < 6:
            raise ValueError("新访问密码至少需要 6 个字符")
        with self.db.connect(readonly=True) as db:
            prev = self.db.get_setting(db, "web_auth") or {}
        salt_hex, digest_hex = _hash_password(new_password)
        auth = {
            "salt": salt_hex,
            "hash": digest_hex,
            "session_secret": secrets.token_hex(32),
            "auth_version": int(prev.get("auth_version", 1)) + 1,
            "updated_at": timestamp(),
        }
        with self.db.write() as db:
            self.db.set_setting(db, "web_auth", auth)
        return self._sign_session(auth)

    def _sign_session(self, auth: dict[str, Any], days: int = 30) -> str:
        expires = int(self._clock()) + days * 86400
        version = int(auth.get("auth_version", 1))
        payload = f"{version}:{expires}"
        sig = hmac.new(bytes.fromhex(auth["session_secret"]), payload.encode("ascii"), "sha256").hexdigest()
        return f"{version}.{expires}.{sig}"

    def issue_session_token(self, days: int = 30) -> str:
        with self.db.connect(readonly=True) as db:
            auth = self.db.get_setting(db, "web_auth")
        if not auth:
            return ""
        return self._sign_session(auth, days=days)

    def verify_session_token(self, token: str) -> bool:
        parts = (token or "").split(".")
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            return False
        version, expires, sig = int(parts[0]), int(parts[1]), parts[2]
        if expires < int(self._clock()) or not re.fullmatch(r"[0-9a-f]{64}", sig):
            return False
        with self.db.connect(readonly=True) as db:
            auth = self.db.get_setting(db, "web_auth")
        if not auth or version != int(auth.get("auth_version", 1)):
            return False
        payload = f"{version}:{expires}"
        expected = hmac.new(bytes.fromhex(auth["session_secret"]), payload.encode("ascii"), "sha256").hexdigest()
        return hmac.compare_digest(sig.encode("utf-8"), expected.encode("utf-8"))

    def ao3_credentials_status(self) -> dict[str, Any]:
        with self.db.connect(readonly=True) as db:
            raw = self.db.get_setting(db, "ao3_credentials")
        if not raw:
            return {
                "configured": False, "auth_mode": "", "user": "", "has_password": False,
                "updated_at": "", "cookie_saved_at": "", "cookie_expired": False,
            }
        mode = raw.get("auth_mode") if raw.get("auth_mode") in AO3_AUTH_MODES else "public"
        return {
            "configured": True,
            "auth_mode": mode,
            "user": str(raw.get("user") or ""),
            "has_password": bool(raw.get("password_enc")),
            "updated_at": str(raw.get("updated_at") or ""),
            "cookie_saved_at": str(raw.get("cookie_saved_at") or ""),
            "cookie_expired": bool(raw.get("cookie_expired", False)),
        }

    def decrypted_ao3_credentials(self) -> tuple[str, str, str]:
        with self.db.connect(readonly=True) as db:
            raw = self.db.get_setting(db, "ao3_credentials")
        if not raw:
            return "", "", ""
        mode = raw.get("auth_mode") if raw.get("auth_mode") in AO3_AUTH_MODES else "public"
        user = str(raw.get("user") or "").strip()
        if mode == "public":
            return "public", "", ""
        if mode == "cookie":
            return "cookie", user, ""
        token = str(raw.get("password_enc") or "")
        if not token:
            return "encrypted", user, ""
        fernet = _credential_fernet(self.db.root, create=False)
        if fernet is None:
            raise RuntimeError("找不到凭据密钥文件 .credentials.key，无法解密 AO3 密码")
        from cryptography.fernet import InvalidToken

        try:
            plain = fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("AO3 凭据解密失败，请检查 .credentials.key 或 AO3_MASTER_KEY") from exc
        return "encrypted", user, plain

    def set_ao3_credentials(
        self, auth_mode: str, user: str = "", password: str = "", *,
        on_clear_cookie: Callable[[], None] | None = None,
    ) -> None:
        if auth_mode not in AO3_AUTH_MODES:
            raise ValueError("不支持的 AO3 认证方式")
        user = user.strip()
        if auth_mode == "public":
            record = {
                "auth_mode": "public", "user": "", "password_enc": "",
                "updated_at": timestamp(), "cookie_saved_at": "", "cookie_expired": False,
            }
            if on_clear_cookie is not None:
                on_clear_cookie()
        elif auth_mode == "cookie":
            if not user:
                raise ValueError("请填写 AO3 用户名")
            record = {
                "auth_mode": "cookie", "user": user, "password_enc": "",
                "updated_at": timestamp(), "cookie_saved_at": timestamp(), "cookie_expired": False,
            }
        else:
            if not user:
                raise ValueError("请填写 AO3 用户名")
            with self.db.connect(readonly=True) as db:
                prev = self.db.get_setting(db, "ao3_credentials") or {}
            prev_enc = str(prev.get("password_enc") or "") if prev.get("auth_mode") == "encrypted" else ""
            if password:
                fernet = _credential_fernet(self.db.root, create=True)
                password_enc = fernet.encrypt(password.encode("utf-8")).decode("ascii")
            elif prev_enc:
                password_enc = prev_enc
            else:
                raise ValueError("首次启用加密保存时请填写 AO3 密码")
            record = {
                "auth_mode": "encrypted", "user": user, "password_enc": password_enc,
                "updated_at": timestamp(), "cookie_saved_at": "", "cookie_expired": False,
            }
        with self.db.write() as db:
            self.db.set_setting(db, "ao3_credentials", record)

    def mark_ao3_cookie_expired(self, expired: bool) -> None:
        with self.db.write() as db:
            raw = self.db.get_setting(db, "ao3_credentials")
            if not raw or raw.get("auth_mode") != "cookie":
                return
            raw["cookie_expired"] = bool(expired)
            if not expired:
                raw["cookie_saved_at"] = timestamp()
            self.db.set_setting(db, "ao3_credentials", raw)


class SchedulerQueueRepo:
    """Repository managing backup schedule settings, scheduler heartbeat, and task queue."""

    def __init__(self, db: Database, *, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self._clock = clock

    def backup_settings(self) -> BackupSettings | None:
        with self.db.connect(readonly=True) as db:
            saved = self.db.get_setting(db, "backup_settings")
            return BackupSettings.from_dict(saved) if saved is not None else None

    def set_backup_settings(self, settings: BackupSettings) -> None:
        with self.db.write() as db:
            self.db.set_setting(db, "backup_settings", asdict(settings))

    def publish_scheduler(
        self, defaults: BackupSettings, formats: tuple[str, ...], next_run: int,
        *, awaiting_setup: bool = False,
    ) -> None:
        with self.db.write() as db:
            state = {
                "defaults": asdict(defaults),
                "formats": formats,
                "next_run": next_run,
                "awaiting_setup": awaiting_setup,
                "seen_at": int(self._clock()),
            }
            self.db.set_setting(db, "backup_scheduler", state)

    def backup_dashboard(self) -> tuple[dict, BackupSettings, list[dict], dict | None]:
        with self.db.connect(readonly=True) as db:
            db.execute("BEGIN")
            scheduler = self.db.get_setting(db, "backup_scheduler") or {}
            settings = self.db.get_setting(db, "backup_settings") or scheduler.get("defaults", {})
            active = db.execute("SELECT * FROM backup_runs WHERE status IN ('queued', 'running') ORDER BY id")
            last = db.execute(
                "SELECT * FROM backup_runs WHERE finished_at IS NOT NULL ORDER BY finished_at DESC, id DESC LIMIT 1"
            ).fetchone()
            return scheduler, BackupSettings.from_dict(settings), [dict(row) for row in active], dict(last) if last else None

    def queue_backup(self, action: str) -> bool:
        if action not in BACKUP_ACTIONS:
            raise ValueError("无效的抓取操作")
        with self.db.write() as db:
            if db.execute(
                "SELECT 1 FROM backup_runs WHERE action=? AND status IN ('queued', 'running')",
                (action,),
            ).fetchone():
                return False
            db.execute(
                "INSERT INTO backup_runs (action, source, status, requested_at) VALUES (?, 'manual', 'queued', ?)",
                (action, timestamp()),
            )
            return True

    def has_queued_backup(self) -> bool:
        with self.db.connect(readonly=True) as db:
            return db.execute("SELECT 1 FROM backup_runs WHERE status='queued' LIMIT 1").fetchone() is not None

    def claim_backup(self, source: str | None) -> tuple[int, str, str] | None:
        """Called under the backup process lock, including when recovering after a restart."""
        now = timestamp()
        with self.db.write() as db:
            db.execute(
                """UPDATE backup_runs SET status='interrupted', finished_at=?, summary='备份进程已重启'
                   WHERE status='running'""",
                (now,),
            )
            job = db.execute(
                "SELECT id, action, source FROM backup_runs WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if job:
                db.execute("UPDATE backup_runs SET status='running', started_at=? WHERE id=?", (now, job["id"]))
                return job["id"], job["action"], job["source"]
            if source:
                cursor = db.execute(
                    """INSERT INTO backup_runs (action, source, status, requested_at, started_at)
                       VALUES ('scan', ?, 'running', ?, ?)""",
                    (source, now, now),
                )
                return cursor.lastrowid, "scan", source
            return None

    def finish_backup(self, job_id: int, status: str, summary: str = "") -> None:
        with self.db.write() as db:
            db.execute(
                "UPDATE backup_runs SET status=?, finished_at=?, summary=? WHERE id=? AND status='running'",
                (status, timestamp(), summary, job_id),
            )
            db.execute(
                """DELETE FROM backup_runs WHERE id IN (
                   SELECT id FROM backup_runs WHERE finished_at IS NOT NULL
                   ORDER BY finished_at DESC, id DESC LIMIT -1 OFFSET 100)"""
            )


class ReaderStateRepo:
    """Repository managing per-browser reader sessions, overview queries, and read pointers."""

    def __init__(self, db: Database, *, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self._clock = clock

    def reader(self, cookie: str, *, visit: bool = False) -> tuple[dict[str, Any], bool]:
        now = int(self._clock())
        if cookie:
            with self.db.connect(readonly=True) as db:
                db.execute("BEGIN")
                row = db.execute("SELECT * FROM readers WHERE id=?", (cookie,)).fetchone()
                if row and (not visit or (
                    now - row["overview_at"] < VISIT_WINDOW_SECONDS and row["last_overview"] >=
                    db.execute("SELECT coalesce(max(id), 0) FROM events WHERE kind!='file'").fetchone()[0]
                )):
                    return dict(row), False
        with self.db.write() as db:
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
                db.execute(
                    "UPDATE readers SET since=?, last_overview=?, overview_at=? WHERE id=?",
                    (reader["since"], latest, now, cookie),
                )
            return reader, False

    def overview(self, reader: dict[str, Any]) -> list[dict[str, Any]]:
        with self.db.connect(readonly=True) as db:
            db.execute("BEGIN")
            rows = db.execute(
                """
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
                """,
                (reader["baseline"], reader["since"], reader["id"]),
            )
            works = {
                r["id"]: {
                    **dict(r),
                    "record": json.loads(r["record"]),
                    "article_fields": json.loads(r["article_fields"] or "{}"),
                    "labels": json.loads(r["labels"] or "[]"),
                    "formats": [],
                    "since_labels": [],
                    "unread_labels": [],
                }
                for r in rows
            }
            for row in db.execute(
                """
                SELECT e.* FROM works w
                LEFT JOIN work_reads r ON r.work_id=w.id AND r.reader_id=?
                JOIN events e ON e.work_id=w.id AND e.kind!='file' AND e.id>min(?, coalesce(r.through, ?))
                ORDER BY e.id DESC
                """,
                (reader["id"], reader["since"], reader["baseline"]),
            ):
                work = works[row["work_id"]]
                labels = json.loads(row["labels"])
                for active, key in ((row["id"] > reader["since"], "since_labels"),
                                    (row["id"] > work["read_through"], "unread_labels")):
                    if active and len(work[key]) < 6:
                        work[key] = list(dict.fromkeys([*work[key], *labels]))[:6]
            for row in db.execute(
                """SELECT w.id, f.format FROM works w
                   JOIN version_files f ON f.version_id=w.latest_version
                   ORDER BY f.format"""
            ):
                works[row["id"]]["formats"].append(row["format"])
            return list(works.values())

    def detail(self, wid: str, reader: dict[str, Any]):
        with self.db.connect(readonly=True) as db:
            db.execute("BEGIN")
            work = db.execute("SELECT * FROM works WHERE id=?", (wid,)).fetchone()
            if not work:
                return None
            versions = {
                r["id"]: Version(
                    r["id"], r["ao3_updated_at"], r["captured_at"],
                    r["origin"], json.loads(r["metadata"]),
                    r["document_digest"], r["parse_error"],
                )
                for r in db.execute("SELECT * FROM versions WHERE work_id=? ORDER BY id", (wid,))
            }
            for r in db.execute(
                """SELECT f.*, b.size FROM version_files f JOIN blobs b USING(digest)
                   JOIN versions v ON v.id=f.version_id WHERE v.work_id=?""",
                (wid,),
            ):
                versions[r["version_id"]].files[r["format"]] = ArchiveFile(r["name"], r["digest"], r["size"])
            events = [dict(r) for r in db.execute(
                "SELECT * FROM events WHERE work_id=? ORDER BY id DESC", (wid,),
            )]
            seen = db.execute(
                "SELECT through FROM work_reads WHERE reader_id=? AND work_id=?",
                (reader["id"], wid),
            ).fetchone()
            return json.loads(work["record"]), versions, events, seen[0] if seen else reader["baseline"]

    def mark_overview_seen(self, rid: str) -> bool:
        now = int(self._clock())
        with self.db.write() as db:
            cursor = db.execute(
                "UPDATE readers SET since=last_overview, overview_at=? WHERE id=?",
                (now, rid),
            )
            return cursor.rowcount > 0

    def mark_article_read(self, reader: dict[str, Any], wid: str, vid: int) -> None:
        with self.db.connect(readonly=True) as db:
            seq = db.execute(
                """
                SELECT max(id) FROM events
                WHERE work_id=? AND kind!='file' AND version_id<=?
                    AND id>coalesce((SELECT through FROM work_reads WHERE reader_id=? AND work_id=?), ?)
                """,
                (wid, vid, reader["id"], wid, reader["baseline"]),
            ).fetchone()[0]
        if seq is not None:
            self.mark_read(reader["id"], wid, seq)

    def mark_read(self, rid: str, wid: str, seq: int) -> bool:
        with self.db.write() as db:
            if db.execute("SELECT 1 FROM events WHERE work_id=? AND id=?", (wid, seq)).fetchone() is None:
                return False
            db.execute(
                """INSERT INTO work_reads VALUES (?, ?, ?)
                   ON CONFLICT(reader_id, work_id) DO UPDATE
                   SET through=max(work_reads.through, excluded.through)""",
                (rid, wid, seq),
            )
            return True


class WorkArchiveRepo:
    """Repository managing works, versions, content-addressed blobs, documents, and events."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def migrate_v1_articles(self, db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE documents (digest TEXT PRIMARY KEY, body TEXT NOT NULL)")
        db.execute("ALTER TABLE versions ADD COLUMN document_digest TEXT REFERENCES documents(digest)")
        db.execute("ALTER TABLE versions ADD COLUMN parse_error TEXT")
        parsed: dict[str, tuple[str | None, str | None]] = {}
        for vid, digest in db.execute("SELECT version_id, digest FROM version_files WHERE format='html'"):
            if digest not in parsed:
                raw = db.execute("SELECT data FROM blobs WHERE digest=?", (digest,)).fetchone()[0]
                parsed[digest] = self._save_article(db, raw)
            db.execute("UPDATE versions SET document_digest=?, parse_error=? WHERE id=?", (*parsed[digest], vid))

    @staticmethod
    def _save_article(db: sqlite3.Connection, raw: bytes) -> tuple[str | None, str | None]:
        try:
            article = read_export(raw)
        except UnicodeDecodeError:
            return None, "HTML 不是有效的 UTF-8"
        except (ValueError, RecursionError):
            return None, "HTML 缺少可完整读取的章节"
        return WorkArchiveRepo._store_document(db, article), None

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
        return Version(
            row["id"], row["ao3_updated_at"], row["captured_at"],
            row["origin"], json.loads(row["metadata"]),
            row["document_digest"], row["parse_error"],
        )

    @staticmethod
    def _files(db: sqlite3.Connection, vid: int) -> dict[str, ArchiveFile]:
        return {
            row["format"]: ArchiveFile(row["name"], row["digest"], row["size"])
            for row in db.execute(
                """SELECT f.*, b.size FROM version_files f JOIN blobs b USING(digest)
                   WHERE version_id=? ORDER BY format""",
                (vid,),
            )
        }

    @staticmethod
    def _set_record(db: sqlite3.Connection, wid: str, record: dict[str, Any]) -> None:
        saved = {
            k: v for k, v in record.items()
            if k not in {"file", "files", "bytes", "history", "revision", "flag", "work_updated_iso", "article_saved"}
        }
        db.execute(
            """INSERT INTO works (id, record) VALUES (?, ?)
               ON CONFLICT(id) DO UPDATE SET record=excluded.record""",
            (wid, encode(saved)),
        )

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
        db.executemany(
            "INSERT INTO version_files VALUES (?, ?, ?, ?)",
            [(vid, ext, file.name, file.digest) for ext, file in files.items()],
        )

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
        changes: Iterable[Any], legacy: dict[str, Any] | None = None,
    ) -> None:
        labels = list(dict.fromkeys(getattr(change, "heading", str(change)) for change in changes))
        if len(labels) > 6:
            labels = labels[:6] + [f"另有 {len(labels) - 6} 处变更"]
        db.execute(
            """INSERT INTO events (work_id, version_id, kind, happened_at, labels, legacy_changes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (wid, vid, kind, happened, encode(labels), encode(legacy or {})),
        )

    def scan_state(self, bookmark_user: str) -> dict[str, Any]:
        state: dict[str, Any] = {"user": bookmark_user, "works": {}}
        if not self.db.path.is_file():
            return state
        with self.db.connect(readonly=True) as db:
            db.execute("BEGIN")
            user = db.execute("SELECT value FROM settings WHERE key='bookmark_user'").fetchone()
            if user and user[0].casefold() != bookmark_user.casefold():
                raise ValueError(f"数据库对应的书签账号为 {user[0]}，请检查 BOOKMARK_USER")
            works = {row["id"]: self._record(row) for row in db.execute("SELECT * FROM works")}
            # Dry runs can inspect a v2 archive before its next writable startup upgrades it.
            if db.execute("PRAGMA user_version").fetchone()[0] >= 3:
                for row in db.execute(
                    """SELECT w.id FROM works w JOIN versions v ON v.id=w.latest_version
                       WHERE v.document_digest IS NOT NULL"""
                ):
                    works[row["id"]]["article_saved"] = True
            for row in db.execute(
                """SELECT w.id, f.format, f.name FROM works w
                   JOIN version_files f ON f.version_id=w.latest_version"""
            ):
                works[row["id"]].setdefault("files", {})[row["format"]] = row["name"]
        state["works"] = works
        return state

    def bind_user(self, user: str) -> None:
        with self.db.write() as db:
            db.execute("INSERT OR IGNORE INTO settings VALUES ('bookmark_user', ?)", (user,))

    def stored_bookmark_user(self) -> str:
        with self.db.connect(readonly=True) as db:
            row = db.execute("SELECT value FROM settings WHERE key='bookmark_user'").fetchone()
            return str(row[0]).strip() if row and row[0] else ""

    def has_works(self) -> bool:
        with self.db.connect(readonly=True) as db:
            return db.execute("SELECT 1 FROM works LIMIT 1").fetchone() is not None

    def save_status(self, wid: str, record: dict[str, Any]) -> None:
        with self.db.write() as db:
            self._set_record(db, wid, record)

    def save_capture(
        self, wid: str, record: dict[str, Any], article: Export | None,
        files: dict[str, tuple[str, bytes]],
        *,
        differ: Callable[[Export | None, Export | None, dict[str, Any], dict[str, Any]], list[Any]] | None = None,
    ) -> tuple[dict[str, Any], str]:
        with self.db.write() as db:
            previous = db.execute(
                """SELECT v.* FROM works w JOIN versions v ON v.id=w.latest_version
                   WHERE w.id=?""",
                (wid,),
            ).fetchone()
            old_id = previous["id"] if previous else None
            digest = previous["document_digest"] if previous else None
            if article is None and not digest:
                raise ValueError("补齐附件需要已经归档的正文")
            old_files = self._files(db, old_id) if old_id else {}
            incoming = self._blobs(db, files)
            new_meta = snapshot(record)
            baseline = previous
            if article is not None and old_id and not digest:
                baseline = db.execute(
                    """SELECT * FROM versions WHERE work_id=? AND document_digest IS NOT NULL
                       ORDER BY id DESC LIMIT 1""",
                    (wid,),
                ).fetchone() or previous
            old_article = self._article(db, baseline["document_digest"]) if baseline and article else None
            if old_article and article.hidden_fields:
                carried = {
                    label: old_article.fields[label]
                    for label in article.hidden_fields
                    if label in old_article.fields and label not in article.fields
                }
                article = replace(article, fields={**article.fields, **carried})
            changes: list[Any] = []
            if article is not None and baseline and differ is not None:
                changes = differ(old_article, article, json.loads(baseline["metadata"]), new_meta)
            raw_change = any(
                old_files[ext].digest != incoming[ext].digest
                for ext in old_files.keys() & incoming.keys()
            )
            kind = (
                "new" if not old_id else
                "updated" if any(
                    getattr(c, "section", "") != "作品资料"
                    or getattr(c, "label", "") in {"作品首注", "作品尾注"}
                    for c in changes
                ) else
                "metadata" if changes else
                "file" if raw_change else ""
            )
            current = incoming if changes else {**old_files, **incoming}
            self._set_record(db, wid, record)
            if kind or not digest:
                if article is not None:
                    digest = self._store_document(db, article)
                vid = self._add_version(
                    db, wid, record.get("updated_at"), record["downloaded_at"],
                    "capture", new_meta, current, digest,
                )
                if kind:
                    self._event(db, wid, vid, kind, record["downloaded_at"], changes)
            else:
                self._attach(db, old_id, {ext: file for ext, file in current.items() if ext not in old_files})
                if old_article is not None:
                    recovered = set(old_article.hidden_fields) - set(article.hidden_fields)
                    if recovered:
                        fields_map = {
                            label: values
                            for label, values in old_article.fields.items()
                            if label not in recovered
                        }
                        fields_map.update({
                            label: article.fields[label]
                            for label in recovered
                            if label in article.fields
                        })
                        enriched = replace(
                            old_article,
                            fields=fields_map,
                            hidden_fields=[label for label in old_article.hidden_fields if label not in recovered],
                        )
                        metadata = json.loads(previous["metadata"])
                        metadata["hidden_fields"] = enriched.hidden_fields
                        if "附加标签" in recovered:
                            metadata.update(freeforms=list(fields_map.get("附加标签", ())), all_freeforms=True)
                        db.execute(
                            "UPDATE versions SET document_digest=?, metadata=? WHERE id=?",
                            (self._store_document(db, enriched), encode(metadata), old_id),
                        )
            return {**record, "files": {ext: file.name for ext, file in current.items()}}, kind

    def read_article_by_digest(self, digest: str | None) -> Export | None:
        if digest is None:
            return None
        with self.db.connect(readonly=True) as db:
            return self._article(db, digest)

    def read_article(self, wid: str, vid: int) -> tuple[Version, Export] | None:
        with self.db.connect(readonly=True) as db:
            row = db.execute("SELECT * FROM versions WHERE work_id=? AND id=?", (wid, vid)).fetchone()
            if row and (article := self._article(db, row["document_digest"])):
                version = self._version(row)
                version.files = self._files(db, row["id"])
                return version, article
        return None

    def read_latest_article(self, wid: str) -> tuple[Version, Export, bool] | None:
        with self.db.connect(readonly=True) as db:
            db.execute("BEGIN")
            row = db.execute(
                """
                SELECT v.*, w.latest_version
                FROM works w JOIN versions v ON v.work_id=w.id
                WHERE w.id=? AND v.document_digest IS NOT NULL
                ORDER BY v.id DESC LIMIT 1
                """,
                (wid,),
            ).fetchone()
            if row and (article := self._article(db, row["document_digest"])):
                version = self._version(row)
                version.files = self._files(db, row["id"])
                return version, article, row["id"] != row["latest_version"]
        return None

    def version_info(self, wid: str, vid: int) -> tuple[int, int, int | None] | None:
        with self.db.connect(readonly=True) as db:
            db.execute("BEGIN")
            ids = [row[0] for row in db.execute("SELECT id FROM versions WHERE work_id=? ORDER BY id", (wid,))]
            if vid not in ids:
                return None
            work = db.execute("SELECT latest_version FROM works WHERE id=?", (wid,)).fetchone()
            latest = work[0] if work else ids[-1]
            return ids.index(vid) + 1, len(ids), latest

    def read_file(self, wid: str, vid: int, ext: str) -> tuple[str, bytes] | None:
        with self.db.connect(readonly=True) as db:
            row = db.execute(
                """SELECT f.name, b.data FROM version_files f JOIN blobs b USING(digest)
                   JOIN versions v ON v.id=f.version_id
                   WHERE v.work_id=? AND v.id=? AND f.format=?""",
                (wid, vid, ext),
            ).fetchone()
            return (row[0], row[1]) if row else None
