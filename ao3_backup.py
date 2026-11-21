#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ao3_backup.py — AO3 书签增量备份器
==================================

定期读取 AO3 书签页，抓取新增或更新的作品全文并存入 SQLite。
可选保存 AO3 官方附件，也支持旧版静态文件模式。

核心事实（已对真实页面验证）
--------------------------
1. 书签页 /users/<user>/bookmarks?page=N，每页 20 条。
2. 每条书签的 HTML 里有注释 `<!-- updated_at=<unix> -->`，
   它等于该作品的 updated_at —— 判断「是否更新过」就靠它。
3. 官方下载地址可以由 work id 直接拼出：
       /downloads/<work_id>/<URL安全文件名>.<ext>?updated_at=<unix>
   文件名只是装饰，服务端按 id 取作品。返回的是自带样式的
   XHTML（含 id="preface" / .meta）或对应电子书文件，可直接离线阅读。

用法
----
    python ao3_backup.py --once                 # 跑一轮就退出
    python ao3_backup.py                        # 常驻，按计划循环
    python ao3_backup.py --once --dry-run       # 只读干跑，不修改任何文件

配置见 README.md / .env.example。
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import html as html_mod
import json
import logging
import os
import re
import shutil
import signal
import socket
import sqlite3
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.cookiejar import MozillaCookieJar
from itertools import zip_longest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterator, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

from ao3_content import paragraphs
from ao3_diff import Export, TAG_FIELDS, WORK_FIELDS, metadata_changes, read_work_page
from ao3_store import (
    BACKUP_ACTIONS, BACKUP_LOG, Archive, BackupSettings, DOWNLOAD_FORMATS,
    REVISION_NAME, compute_next_runs, import_legacy, missing_attachments,
    parse_duration, parse_hhmm, resolve_schedule, schedule_label, snapshot, work_files,
)

__version__ = "2.1.1"
BASE = "https://archiveofourown.org"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}
RETRY_CODES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526}
RUNNING_MAX_IDLE_SECONDS = 7200  # .running 超过 2 小时无心跳视为卡死
BACKUP_POLL_SECONDS = 3

log = logging.getLogger("ao3")


# --------------------------------------------------------------------------
# 网络：IPv4 优先 & URL 安全编码
# --------------------------------------------------------------------------
_ORIG_GETADDRINFO = socket.getaddrinfo
_PREFER_IPV4 = False


def _v4_first_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
    infos = _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
    if _PREFER_IPV4 and family in (0, socket.AF_UNSPEC):
        infos = sorted(infos, key=lambda i: 0 if i[0] == socket.AF_INET else 1)
    return infos


def prefer_ipv4(enabled: bool = True) -> None:
    """让 getaddrinfo 把 IPv4 地址排在前面。

    某些网络环境（代理的 fake-IP 模式、IPv6 黑洞）下 DNS 会先返回一个不可达的
    IPv6 地址，socket.create_connection 会先卡满 connect 超时才回落到 IPv4，
    单次请求凭空多出 20~40 秒。这里只调整顺序，IPv6 仍作为后备，
    纯 IPv6 环境不受影响。
    """
    global _PREFER_IPV4
    _PREFER_IPV4 = bool(enabled)
    if _PREFER_IPV4:
        if socket.getaddrinfo is not _v4_first_getaddrinfo:
            socket.getaddrinfo = _v4_first_getaddrinfo  # type: ignore[assignment]
        log.debug("已启用 IPv4 优先解析")
    else:
        if socket.getaddrinfo is _v4_first_getaddrinfo:
            socket.getaddrinfo = _ORIG_GETADDRINFO  # type: ignore[assignment]


def normalize_url(url: str) -> str:
    """确保 URL 仅含 ASCII 字符，防止 http.client 因中日韩字符抛 UnicodeEncodeError。"""
    parts = urlsplit(url)
    # safe 中保留 '%' 以避免对已经百分号编码的片段二次编码
    safe_path = quote(parts.path, safe="/:@!$&'()*+,;=-._~%")
    safe_query = quote(parts.query, safe="=&/:@!$'()*+,;-._~?%")
    return urlunsplit((parts.scheme, parts.netloc, safe_path, safe_query, ""))


def parse_retry_after(val: Optional[str]) -> Optional[float]:
    """解析 HTTP Retry-After 响应头（秒数或 HTTP 日期）。"""
    if not val:
        return None
    val = val.strip()
    if val.isdigit():
        return float(val)
    try:
        dt = parsedate_to_datetime(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html_mod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def strip_html_paragraphs(s: str, maxlen: int = 2000) -> str:
    """把 HTML 段落转为保留换行的纯文本（用于简介与书签备注）。"""
    if not s:
        return ""
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", s)
    text = re.sub(r"(?i)</\s*(?:p|div|li|blockquote|h[1-6])\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.splitlines()]
    cleaned = "\n".join(ln for ln in lines if ln)
    if len(cleaned) > maxlen:
        return cleaned[: maxlen - 1].rstrip() + "…"
    return cleaned


def slugify(text: str, maxlen: int = 60) -> str:
    """把标题变成安全的文件名片段，保留中日韩字符。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[^\w\s.-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s.\-]+", "_", text).strip("_")
    return text[:maxlen].rstrip("_") or "work"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ts_iso(ts: Optional[int]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def human_size(n: int) -> str:
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024 or unit == "GB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{val:.1f} GB"


def human_words(n: int) -> str:
    if n <= 0:
        return "-"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def atomic_write(path: Path, data: bytes | str | Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        if isinstance(data, str):
            tmp.write_text(data, encoding="utf-8")
        elif isinstance(data, Path):
            shutil.copyfile(data, tmp)
        else:
            tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def touch_marker(path: Path) -> None:
    with contextlib.suppress(OSError):
        atomic_write(path, now_iso())


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


@contextlib.contextmanager
def acquire_lock(lock_path: Path) -> Iterator[None]:
    """跨平台非阻塞单实例文件锁，防止手动 --once 与后台守护进程并发写坏 state.json。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+", encoding="utf-8")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(" ")
                fh.flush()
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError as exc:
                raise RuntimeError(
                    f"输出目录已被另一备份进程锁定（{lock_path}）。"
                    "请等待当前任务完成，或加 --dry-run 只读预览。"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                raise RuntimeError(
                    f"输出目录已被另一备份进程锁定（{lock_path}）。"
                    "请等待当前任务完成，或加 --dry-run 只读预览。"
                ) from exc

        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()} {now_iso()}\n")
        fh.flush()
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
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
    def cookie_file(self) -> Path:
        return self.out_dir / "cookies.txt"

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
# HTTP 与登录态管理
# --------------------------------------------------------------------------
def is_logged_in_html(page: str) -> bool:
    """判断 AO3 返回的 HTML 是否处于已登录状态。"""
    return "/users/logout" in page


def extract_csrf_token(page: str) -> Optional[str]:
    """从 AO3 登录页提取 authenticity_token，兼容不同属性顺序与 meta csrf-token。"""
    form_match = re.search(
        r'<form\b[^>]*action=["\']/users/login["\'][^>]*>(.*?)</form>',
        page,
        re.S | re.I,
    )
    scopes = [form_match.group(1), page] if form_match else [page]
    input_patterns = (
        r'<input\b[^>]*\bname=["\']authenticity_token["\'][^>]*\bvalue=["\']([^"\']+)["\']',
        r'<input\b[^>]*\bvalue=["\']([^"\']+)["\'][^>]*\bname=["\']authenticity_token["\']',
    )
    for scope in scopes:
        for pat in input_patterns:
            m = re.search(pat, scope, re.S | re.I)
            if m:
                return html_mod.unescape(m.group(1))

    for pat in (
        r'<meta\b[^>]*\bname=["\']csrf-token["\'][^>]*\bcontent=["\']([^"\']+)["\']',
        r'<meta\b[^>]*\bcontent=["\']([^"\']+)["\'][^>]*\bname=["\']csrf-token["\']',
    ):
        m = re.search(pat, page, re.S | re.I)
        if m:
            return html_mod.unescape(m.group(1))
    return None


class CookieExpiredError(Exception):
    """仅 Cookie 模式下现有登录态失效，需要通过网页重新登录。"""


def _format_net_error(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        reason = f"（{exc.reason}）" if getattr(exc, "reason", "") else ""
        return f"HTTP {exc.code}{reason}"
    if isinstance(exc, TimeoutError):
        return f"请求超时（{exc}）" if str(exc) else "请求超时"
    if isinstance(exc, URLError):
        return f"网络连接异常（{exc.reason}）"
    return str(exc) or type(exc).__name__


def _diagnose_login_failure(page: str, *, missing_csrf: bool = False) -> str:
    lower = page.casefold()
    if any(k in lower for k in ("retry later", "too many requests", "rate limit")):
        return "AO3 触发访问限流（Retry later），请稍候几分钟后再试"
    if any(k in lower for k in (
        "captcha", "recaptcha", "hcaptcha", "turnstile", "verify you are human", "just a moment",
    )):
        return "AO3 登录触发了人机校验 / 防护拦截，暂时无法自动登录，请先在浏览器核实"
    if missing_csrf:
        return "AO3 登录页未返回安全令牌（authenticity_token），可能被防护或代理拦截"
    if any(k in lower for k in (
        "doesn't match our records",
        "please check your user name and password",
        "invalid email or password",
        "incorrect password",
    )):
        return "AO3 用户名或密码不正确，请核对后重新输入"
    if any(k in lower for k in ("account has been", "suspended", "locked")):
        return "AO3 账号状态异常，请先前往 AO3 官网确认账号状态"
    return "AO3 登录失败：请核对用户名/密码，或确认未被验证码拦截"


class AO3Client:
    def __init__(
        self, cfg: Config, stop: Optional[threading.Event] = None,
        *, status_cb: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.cfg = cfg
        self.stop = stop
        self.status_cb = status_cb
        self.jar = MozillaCookieJar(str(cfg.cookie_file))
        self._cookie_mtime = 0.0
        self.reload_cookies(force=True)
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self._last = 0.0
        self.logged_in = False
        self.last_login_error = ""

    def _report(self, message: str) -> None:
        if self.status_cb is not None and message:
            with contextlib.suppress(Exception):
                self.status_cb(message)

    def reload_cookies(self, *, force: bool = False) -> None:
        if self.cfg.auth_mode == "public":
            self.jar.clear()
            self._cookie_mtime = 0.0
            self.logged_in = False
            return
        if not self.cfg.cookie_file.exists():
            if self._cookie_mtime:
                self.jar.clear()
                self._cookie_mtime = 0.0
                self.logged_in = False
            return
        try:
            mtime = self.cfg.cookie_file.stat().st_mtime
        except OSError:
            return
        if not force and mtime == self._cookie_mtime:
            return
        self.jar.clear()
        try:
            self.jar.load(ignore_discard=True, ignore_expires=True)
            self._cookie_mtime = mtime
            self.logged_in = False
            log.debug("已载入 cookie: %s", self.cfg.cookie_file)
        except Exception as exc:  # noqa: BLE001
            self._cookie_mtime = 0.0
            self.logged_in = False
            log.warning("cookie 读取失败，将重新登录: %s", exc)

    def _sleep(self, seconds: float, *, label: str = "") -> None:
        if seconds <= 0:
            return
        if self.status_cb is not None and label and seconds > 1.2:
            deadline = time.monotonic() + seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._report(f"{label}（剩余 {max(1, int(round(remaining)))} 秒）")
                step = min(1.0, remaining)
                if self.stop is not None:
                    if self.stop.wait(step):
                        raise InterruptedError("收到停止信号")
                else:
                    time.sleep(step)
            return
        if self.stop is not None:
            if self.stop.wait(seconds):
                raise InterruptedError("收到停止信号")
        else:
            time.sleep(seconds)

    def _check_stop(self) -> None:
        if self.stop is not None and self.stop.is_set():
            raise InterruptedError("收到停止信号")

    # -- 限速 -------------------------------------------------------------
    def _throttle(self) -> None:
        self._check_stop()
        gap = self.cfg.delay - (time.monotonic() - self._last)
        if gap > 0:
            self._sleep(gap)
        self._last = time.monotonic()

    # -- 底层请求 ---------------------------------------------------------
    def _request(self, path: str, data: Optional[dict[str, str]] = None) -> bytes:
        raw_url = path if path.startswith("http") else BASE + path
        url = normalize_url(raw_url)
        body = urlencode(data).encode("utf-8") if data else None
        last: Exception | None = None

        for attempt in range(1, self.cfg.retries + 1):
            self._throttle()
            self._report("正在向 AO3 提交登录凭据…" if body else "正在连接 AO3 获取登录安全令牌…")
            req = Request(url, data=body, method="POST" if body else "GET")
            for k, v in HEADERS.items():
                req.add_header(k, v)
            if body:
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
                req.add_header("Referer", BASE + "/users/login")

            try:
                with self.opener.open(req, timeout=self.cfg.timeout) as resp:
                    return resp.read()
            except HTTPError as exc:
                last = exc
                if exc.code in RETRY_CODES and attempt < self.cfg.retries:
                    if exc.code == 429:
                        ra = parse_retry_after(
                            exc.headers.get("Retry-After") if exc.headers else None
                        )
                        back = (
                            min(600.0, max(15.0, ra + 2.0))
                            if ra is not None
                            else float(min(300, 60 * 2 ** (attempt - 1)))
                        )
                        wait_label = f"AO3 触发限流（HTTP 429），等待第 {attempt + 1}/{self.cfg.retries} 次重试"
                    else:
                        back = float(min(60, 3 * 2 ** (attempt - 1)))
                        wait_label = f"AO3 返回 HTTP {exc.code}，等待第 {attempt + 1}/{self.cfg.retries} 次重试"
                    log.warning(
                        "HTTP %s，%.0fs 后重试（%d/%d）：%s",
                        exc.code,
                        back,
                        attempt,
                        self.cfg.retries,
                        url,
                    )
                    self._sleep(back, label=wait_label)
                    continue
                if exc.code == 403:
                    raise RuntimeError(
                        "HTTP 403：被 Cloudflare 拦截。请调大 DELAY（如 5），或稍后重试。"
                    ) from exc
                raise
            except (URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt < self.cfg.retries:
                    back = float(min(60, 3 * 2 ** (attempt - 1)))
                    log.warning(
                        "网络错误 %s，%.0fs 后重试（%d/%d）",
                        exc,
                        back,
                        attempt,
                        self.cfg.retries,
                    )
                    wait_label = f"连接 AO3 异常（{_format_net_error(exc)}），等待第 {attempt + 1}/{self.cfg.retries} 次重试"
                    self._sleep(back, label=wait_label)
                    continue
                raise

        raise RuntimeError(f"请求失败：{url}（{last}）")

    def get_text(self, path: str) -> str:
        return self._request(path).decode("utf-8", "replace")

    def get_bytes(self, path: str) -> bytes:
        return self._request(path)

    # -- 登录 -------------------------------------------------------------
    def has_credentials(self) -> bool:
        return bool(self.cfg.user and self.cfg.password)

    def verify_cookie_session(self) -> bool:
        page = self.get_text("/users/login")
        if is_logged_in_html(page):
            self.logged_in = True
            if not self.cfg.dry_run:
                self.save_cookies()
            log.info("已验证有效登录态：%s", self.cfg.user or "Cookie 会话")
            return True
        self.logged_in = False
        return False

    def login(self, force: bool = False) -> bool:
        self.last_login_error = ""
        if not self.has_credentials():
            return False

        if force:
            self.jar.clear()

        page = self.get_text("/users/login")
        if is_logged_in_html(page):
            self.logged_in = True
            if not self.cfg.dry_run:
                self.save_cookies()
            log.info("已复用有效登录态：%s", self.cfg.user)
            return True

        token = extract_csrf_token(page)
        if not token:
            log.error("登录页未找到 authenticity_token，AO3 页面结构可能变了或被拦截")
            self.logged_in = False
            self.last_login_error = _diagnose_login_failure(page, missing_csrf=True)
            return False

        raw = self._request(
            "/users/login",
            data={
                "authenticity_token": token,
                "user[login]": self.cfg.user,
                "user[password]": self.cfg.password,
                "user[remember_me]": "1",
                "commit": "Log In",
            },
        )
        text = raw.decode("utf-8", "replace")
        if is_logged_in_html(text):
            self.logged_in = True
            if not self.cfg.dry_run:
                self.save_cookies()
            log.info("登录成功：%s", self.cfg.user)
            return True

        self.logged_in = False
        self.last_login_error = _diagnose_login_failure(text)
        log.error("登录失败：%s", self.last_login_error)
        return False

    def save_cookies(self) -> None:
        if self.cfg.dry_run:
            return
        try:
            self.cfg.cookie_file.parent.mkdir(parents=True, exist_ok=True)
            self.jar.save(str(self.cfg.cookie_file), ignore_discard=True, ignore_expires=True)
            if os.name != "nt":
                with contextlib.suppress(OSError):
                    os.chmod(self.cfg.cookie_file, 0o600)
            with contextlib.suppress(OSError):
                self._cookie_mtime = self.cfg.cookie_file.stat().st_mtime
        except Exception as exc:  # noqa: BLE001
            log.warning("cookie 保存失败: %s", exc)


def verify_login_and_save_cookies(
    out_dir: Path, user: str, password: str, *,
    timeout: int = 60, prefer_v4: bool = True, retries: int = 3,
    stop: Optional[threading.Event] = None,
    status_cb: Optional[Callable[[str], None]] = None,
) -> None:
    user = user.strip()
    if not user or not password:
        raise ValueError("请填写完整的 AO3 用户名和密码")
    prefer_ipv4(prefer_v4)
    attempts = max(1, min(retries, 5))
    tmp_cfg = Config(
        bookmark_user=user,
        out_dir=out_dir,
        user=user,
        password=password,
        delay=0.5,
        timeout=max(5, timeout),
        retries=attempts,
        prefer_ipv4=prefer_v4,
        formats=(),
        auth_mode="cookie",
    )
    client = AO3Client(tmp_cfg, stop=stop, status_cb=status_cb)
    try:
        ok = client.login(force=True)
    except InterruptedError as exc:
        raise ValueError("已取消 AO3 凭据在线校验") from exc
    except HTTPError as exc:
        if exc.code == 429:
            raise ValueError(f"AO3 请求过于频繁（HTTP 429 限流），自动重试 {attempts} 次后仍受限，请稍候几分钟后再试") from exc
        raise ValueError(f"连接 AO3 验证登录失败：{_format_net_error(exc)}") from exc
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError(f"连接 AO3 超时或网络失败（已尝试 {attempts} 次）：{_format_net_error(exc)}") from exc
    if not ok:
        raise ValueError(client.last_login_error or "AO3 登录失败：请核对用户名/密码，或确认未被验证码拦截")


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

    # 1. 页面明确存在禁用态 Next（已在最后一页）
    if PAGINATION_NEXT_DISABLED.search(page):
        return False

    # 2. 从 next 容器中精确提取目标页码（AO3 典型结构：<li class="next"><a href="...?page=2">Next →</a></li>）
    m_next = re.search(
        r'<li\b[^>]*\bclass=["\'][^"\']*\bnext\b[^"\']*["\'][^>]*>\s*'
        r'<a\b[^>]*\bhref=["\'][^"\']*[?&]page=(\d+)',
        page,
        re.I,
    )
    if m_next:
        return int(m_next.group(1)) > current_page

    # 3. 动态检查是否存在明确指向下一页的链接（如 page=current_page+1）
    if re.search(r'href=["\'][^"\']*[?&]page=' + str(current_page + 1) + r'\b', page):
        return True

    # 4. 其他包含 next 语义的有效链接（如 rel="next" 等）
    if PAGINATION_NEXT_LINK.search(page):
        return True

    # 5. 若无任何下一页链接：AO3 每页固定 20 条，不满 20 条必定已是最后一页
    if total_li < 20:
        return False

    # 6. 当前页满 20 条且无禁用标识，兜底认为可能有下一页（下一页为空时自动退出）
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
        seg = page[tag.end() : end]

        kind_match = KIND_ID.search(tag.group(0))
        if not kind_match:
            # 已被作者删除的作品或隐藏挑战（Mystery Work）在 li 上没有 work-<id>
            continue
        kind, obj_id = kind_match.group(1), int(kind_match.group(2))

        user_mod = USER_MOD.search(seg)
        head, tail = (seg[: user_mod.start()], seg[user_mod.end() :]) if user_mod else (seg, "")

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
# 下载
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

    # 若运行途中登录态恰好失效导致锁文跳回登录页，自动重登再试一次
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
# 状态
# --------------------------------------------------------------------------
def load_state(cfg: Config, log_: logging.Logger) -> dict[str, Any]:
    if not cfg.state_file.exists():
        return {"version": 3, "user": cfg.bookmark_user, "works": {}}
    try:
        data = json.loads(cfg.state_file.read_text(encoding="utf-8"))
        data.setdefault("works", {})
        return data
    except Exception as exc:  # noqa: BLE001
        log_.warning("state.json 损坏，先备份再重建: %s", exc)
        if not cfg.dry_run:
            backup = cfg.state_file.with_suffix(f".corrupt-{int(time.time())}.json")
            with contextlib.suppress(OSError):
                cfg.state_file.replace(backup)
        return {"version": 3, "user": cfg.bookmark_user, "works": {}}


def save_state(cfg: Config, state: dict[str, Any]) -> None:
    if cfg.dry_run:
        return
    state["version"] = 3
    state.setdefault("library_id", uuid4().hex)
    state["user"] = cfg.bookmark_user
    state["last_run"] = now_iso()
    atomic_write(
        cfg.state_file, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    )


CHANGE_LABELS = {"new": "新增备份", "updated": "作品更新", "metadata": "作品资料变更"}


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
    seq = state.get("change_seq", 0) + 1
    state["change_seq"] = seq
    event = {"seq": seq, "kind": kind, "at": now_iso(), "changes": changes}
    if kind in ("new", "updated"):
        event["revision"] = rec["revision"]
    if previous:
        event["previous"] = previous
    rec["history"] = [*rec.get("history", []), event]


def sync_metadata(
    state: dict[str, Any], rec: dict[str, Any], blurb: dict[str, Any]
) -> None:
    before = {key: rec.get(key) for key in (*WORK_FIELDS, "all_freeforms")}
    merge_blurb_metadata(rec, blurb)
    changes = metadata_changes(before, rec)
    if changes and work_files(rec):
        record_change(state, rec, "metadata", changes)
        rec["flag"] = "metadata"


def revision_key(state: dict[str, Any], stamp: int | None, seq: int) -> str:
    library_id = state.setdefault("library_id", uuid4().hex)
    return f"{stamp or 'old'}-{library_id[:8]}-{seq}"


def prune_retired_files(cfg: Config, state: dict[str, Any]) -> None:
    """当前索引发布后再清理旧路径，且允许中断后安全重试。"""
    retired = state.get("retired_files", [])
    if not retired:
        return
    active = {name for rec in state["works"].values() for name in work_files(rec).values()}
    remaining = []
    for name in dict.fromkeys(retired):
        if name in active or Path(name).name != name:
            continue
        try:
            (cfg.works_dir / name).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("  旧备份路径清理失败 %s: %s", name, exc)
            remaining.append(name)
    if remaining != retired:
        state["retired_files"] = remaining
        save_state(cfg, state)


# --------------------------------------------------------------------------
# 索引与清单
# --------------------------------------------------------------------------
PARAGRAPH_TAGS = {"p", "div", "section", "li", "blockquote", "pre", "br", "hr",
                  "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


class ChapterParser(HTMLParser):
    """从 AO3 导出页提取章节正文，跳过封面、简介和章节备注。"""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.title = ""
        self.heading: list[str] | None = None
        self.text: list[str] = []
        self.paragraphs: list[str] | None = None
        self.chapters: list[tuple[str, tuple[str, ...]]] = []

    def finish_paragraph(self) -> None:
        text = " ".join("".join(self.text).split())
        if text and self.paragraphs is not None:
            self.paragraphs.append(text)
        self.text.clear()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.depth and tag != "div":
            return
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if self.paragraphs is not None and tag in PARAGRAPH_TAGS:
            self.finish_paragraph()
        if tag == "div":
            if self.depth == 1 and "userstuff" in classes:
                self.paragraphs = []
            if self.depth or attr.get("id") == "chapters":
                self.depth += 1
        if tag == "h2" and self.depth == 2 and self.paragraphs is None and "heading" in classes:
            self.heading = []

    def handle_endtag(self, tag: str) -> None:
        if self.paragraphs is not None and tag in PARAGRAPH_TAGS:
            self.finish_paragraph()
        if tag == "h2" and self.heading is not None:
            self.title = " ".join("".join(self.heading).split())
            self.heading = None
        if tag == "div" and self.depth:
            if self.depth == 2 and self.paragraphs is not None:
                self.chapters.append((self.title, tuple(self.paragraphs)))
                self.title = ""
                self.paragraphs = None
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.paragraphs is not None:
            self.text.append(data)
        elif self.heading is not None:
            self.heading.append(data)


def paragraph_pair(previous: str, current: str) -> str:
    start = max(0, len(os.path.commonprefix((previous, current))) - 48)
    old, new = previous[start:start + 420], current[start:start + 420]
    if old and new:
        sides: tuple[list[str], list[str]] = ([], [])
        for action, a, b, c, d in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            for text, side in ((old[a:b], sides[0]), (new[c:d], sides[1])):
                escaped = html_mod.escape(text)
                if escaped:
                    side.append(escaped if action == "equal" else f"<mark>{escaped}</mark>")
        excerpts = ("".join(sides[0]), "".join(sides[1]))
    else:
        excerpts = (html_mod.escape(old), html_mod.escape(new))
    rendered = []
    for original, excerpt, tag in zip((previous, current), excerpts, ("del", "ins")):
        if not original:
            rendered.append("<span class='no-paragraph'>—</span>")
            continue
        if start:
            excerpt = "…" + excerpt
        if start + 420 < len(original):
            excerpt += "…"
        rendered.append(f"<{tag}>{excerpt}</{tag}>")
    return f"<div class='compare-pair'>{''.join(rendered)}</div>"


def chapter_diff(before: ChapterParser, after: ChapterParser) -> str:
    old, new = before.chapters, after.chapters
    if not old or not new:
        return "<p>无法识别其中一版的 AO3 章节正文。</p>"
    parts = []
    excerpts_left = 16
    matcher = SequenceMatcher(None, [body for _, body in old], [body for _, body in new])
    for _, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        for old_chapter, new_chapter in zip_longest(
            old[old_start:old_end], new[new_start:new_end]
        ):
            old_title, old_text = old_chapter or ("", ())
            new_title, new_text = new_chapter or ("", ())
            old_number = old_start + 1
            new_number = new_start + 1
            old_start += old_chapter is not None
            new_start += new_chapter is not None
            renamed = bool(old_title and new_title and old_title != new_title)
            if old_chapter and new_chapter and old_text == new_text and not renamed:
                continue
            label = new_title or old_title or f"第 {new_number if new_chapter else old_number} 章"
            if not old_chapter:
                label += " · 新增章节"
            if not new_chapter:
                label += " · 删除章节"
            parts.append(f"<section class='chapter-diff'><h2>{html_mod.escape(label)}</h2>")
            if renamed:
                parts.append(f"<p>章名 <del>{html_mod.escape(old_title)}</del> → "
                             f"<ins>{html_mod.escape(new_title)}</ins></p>")
            removed = added = total_pairs = 0
            snippets = []
            for action, a, b, c, d in SequenceMatcher(None, old_text, new_text).get_opcodes():
                if action == "equal":
                    continue
                removed += b - a
                added += d - c
                total_pairs += max(b - a, d - c)
                for previous, current in zip_longest(old_text[a:b], new_text[c:d], fillvalue=""):
                    if not excerpts_left:
                        break
                    snippets.append(paragraph_pair(previous, current))
                    excerpts_left -= 1
            if removed or added:
                parts.append(f"<p class='sub'>删除 {removed} 段 · 新增 {added} 段</p>")
            parts.extend(snippets)
            if total_pairs > len(snippets):
                parts.append("<p class='sub'>打开版本文件查看其余段落。</p>")
            parts.append("</section>")
    return "".join(parts) or "<p>两版的正文文字和章名一致。</p>"


def history_document(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html_mod.escape(title)}</title><style>{CSS}</style></head>"
        f"<body><main class='history-page'>{body}</main></body></html>"
    )


def compare_versions(
    directory: Path, older: dict[str, Path], newer: dict[str, Path], name: str,
) -> bool:
    old_html, new_html = older.get("html"), newer.get("html")
    if not old_html or not new_html:
        return False
    report = directory / name
    try:
        latest = max(old_html.stat().st_mtime_ns, new_html.stat().st_mtime_ns)
        if report.is_file() and report.stat().st_mtime_ns >= latest:
            return True
        before, after = ChapterParser(), ChapterParser()
        for parser, path in ((before, old_html), (after, new_html)):
            parser.feed(path.read_text(encoding="utf-8", errors="replace"))
            parser.close()
        old_href = quote(Path(os.path.relpath(old_html, directory)).as_posix())
        new_href = quote(Path(os.path.relpath(new_html, directory)).as_posix())
        atomic_write(report, history_document(
            "正文差异",
            "<nav><a href='history.html'>‹ 版本历史</a> · <a href='../../index.html'>总览</a></nav>"
            "<h1>正文差异</h1><div class='compare-pair legend'>"
            f"<span><a href='{old_href}'>旧版全文</a></span>"
            f"<span><a href='{new_href}'>新版全文</a></span></div>{chapter_diff(before, after)}",
        ))
        return True
    except OSError as exc:
        log.warning("  正文对比失败 %s: %s", report, exc)
        return False


def render_metadata(changes: dict[str, tuple[Any, Any]]) -> str:
    parts = []
    for key in WORK_FIELDS:
        if key not in changes:
            continue
        before, after = changes[key]
        if key in TAG_FIELDS:
            before = " · ".join(f"− {tag}" for tag in before)
            after = " · ".join(f"+ {tag}" for tag in after)
        elif key == "words":
            before = f"{before:,}" if isinstance(before, int) else before
            after = f"{after:,}" if isinstance(after, int) else after
        parts.append(
            f"<div class='field-change'><strong>{WORK_FIELDS[key]}</strong>"
            f"<div class='compare-pair'><del aria-label='修改前'>{html_mod.escape(str(before) or '—')}</del>"
            f"<ins aria-label='修改后'>{html_mod.escape(str(after) or '—')}</ins></div></div>"
        )
    return "".join(parts)


def write_history(cfg: Config, wid: str, rec: dict[str, Any]) -> str:
    if not re.fullmatch(r"[0-9]+", wid):
        return ""
    directory = cfg.versions_dir / wid
    archives: dict[str, dict[str, Path]] = {}
    if directory.is_dir():
        for path in directory.iterdir():
            ext = path.suffix.lstrip(".")
            if ext in DOWNLOAD_FORMATS and REVISION_NAME.fullmatch(path.stem) and path.is_file():
                archives.setdefault(path.stem, {})[ext] = path
    events = rec.get("history", [])
    revisions = {
        event["revision"]: event for event in events if event.get("revision")
    }
    legacy = sorted(
        (key for key in archives if "-" not in key),
        key=lambda key: int(key) if key.isdigit() else 0,
    )
    previous = [
        key for event in events
        if (key := event.get("previous")) and REVISION_NAME.fullmatch(key)
    ]
    metadata_events = [event for event in events if event["kind"] == "metadata"]
    if not (legacy or previous or metadata_events):
        return ""
    current = rec.get("revision", "")
    if not REVISION_NAME.fullmatch(current):
        current = "current"
    archives[current] = {
        ext: cfg.works_dir / name for ext, name in work_files(rec).items()
        if (cfg.works_dir / name).is_file()
    }
    order = [*legacy, *previous, current]
    items = []
    for index, revision in enumerate(order):
        files = archives.get(revision, {})
        stamp = (str(rec.get("updated_at") or 0) if revision == current else revision.split("-")[0])
        stamp_iso = ts_iso(int(stamp)) if stamp.isdigit() else ""
        stamp_label = stamp_iso[:19].replace("T", " ")
        label = stamp_label or "日期未知"
        if revision == current:
            label = f"当前版本 · {label}"
        links = []
        for ext, path in sorted(files.items()):
            relative = quote(path.name)
            if revision == current:
                relative = f"../../works/{relative}"
            links.append(f"<a class='tag fmt' href='{relative}'>{html_mod.escape(ext.upper())}</a>")
        event = revisions.get(revision)
        details = render_metadata(event["changes"]) if event else ""
        at = event["at"][:19].replace("T", " ") if event else ""
        comparison = ""
        if index:
            older = order[index - 1]
            name = f"compare-{older}-{revision}.html"
            if compare_versions(directory, archives.get(older, {}), files, name):
                comparison = f"<a class='compare-link' href='{name}'>查看正文差异</a>"
            else:
                comparison = "<span class='sub'>正文对比需要两版 HTML</span>"
        items.append(
            f"<li class='version-item'><div class='version-top'><strong>{label}</strong>"
            f"<span class='sub'>{f'备份于 {html_mod.escape(at)}' if at else ''}</span></div>"
            f"<div class='version-links'>{''.join(links) or '版本文件未保留'} {comparison}</div>"
            f"{details}</li>"
        )
    metadata_html = "".join(
        f"<li class='version-item'><strong>{html_mod.escape(event['at'][:19].replace('T', ' '))}</strong>"
        f"{render_metadata(event['changes'])}</li>" for event in reversed(metadata_events)
    )
    title = rec.get("title") or f"work {wid}"
    body = (
        "<nav><a href='../../index.html'>‹ 备份总览</a></nav>"
        f"<h1>{html_mod.escape(title)}</h1><h2>版本历史</h2>"
        f"<ol class='version-list'>{''.join(reversed(items))}</ol>"
    )
    if metadata_html:
        body += f"<h2>作品资料变更</h2><ol class='version-list'>{metadata_html}</ol>"
    atomic_write(directory / "history.html", history_document(title, body))
    return f"versions/{wid}/history.html"


CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font:15px/1.6 -apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
     margin:0;padding:28px 32px;background:#f6f5f3;color:#1b1b1b}
h1{font-size:22px;margin:0 0 4px}
.sub{color:#666;margin:0 0 18px;font-size:13px}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:0 0 16px}
.search{flex:1 1 260px;min-width:200px;padding:7px 12px;border:1px solid #d8d5d0;
        border-radius:6px;background:#fff;color:inherit;font:inherit;font-size:14px}
.select{padding:7px 10px;border:1px solid #d8d5d0;border-radius:6px;background:#fff;
        color:inherit;font:inherit;font-size:13px}
.pills{display:flex;flex-wrap:wrap;gap:6px}
.pill{padding:5px 11px;border:1px solid #d8d5d0;border-radius:999px;background:#fff;
      color:#444;font-size:12px;cursor:pointer;user-select:none}
.pill.active{background:#8b1e1e;border-color:#8b1e1e;color:#fff}
.count{font-size:12px;color:#666;margin-left:auto;white-space:nowrap}
table{border-collapse:collapse;width:100%;background:#fff;
      box-shadow:0 1px 3px rgba(0,0,0,.08);border-radius:6px;overflow:hidden}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #eee;vertical-align:top}
th{background:#faf9f8;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#555;white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fcfbfa}
a{color:#8b1e1e;text-decoration:none}
a:hover{text-decoration:underline}
.title-line{font-weight:600;line-height:1.45}
.meta-line{margin-top:4px;display:flex;flex-wrap:wrap;gap:5px;align-items:center}
.fandom{font-size:12px;color:#555;margin-top:3px}
.chips{font-size:12px;color:#777;margin-top:2px}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;color:#555}
.nowrap{white-space:nowrap;font-variant-numeric:tabular-nums;color:#555;font-size:13px}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;background:#eee;color:#444;font-weight:400}
a.tag:hover{text-decoration:none;filter:brightness(0.95)}
.fmt{background:#f0ebe6;color:#6d2828;font-weight:500}
.new{background:#d8f0d8;color:#1c6b1c}
.upd{background:#fde9c8;color:#8a5300}
.wip{background:#e3eefc;color:#1d4f91}
details{margin-top:5px;font-size:13px;color:#444}
summary{cursor:pointer;color:#777;font-size:12px;user-select:none}
.detail-box{margin-top:5px;padding:8px 10px;background:#f9f8f6;border-left:3px solid #d8cfc8;
            border-radius:0 4px 4px 0;white-space:pre-line;line-height:1.55}
.bm-note{margin-top:6px;padding-top:6px;border-top:1px dashed #ddd;color:#5c3b1e}
.empty{padding:32px;text-align:center;color:#888;display:none}
[hidden]{display:none!important}
.sub time{white-space:nowrap}
.visit-summary{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;
               gap:6px 16px;padding:10px 0;margin-bottom:12px;border-block:1px solid #d8d5d0}
.visit-summary strong{font-size:14px}
.visit-summary button{cursor:pointer;font:inherit;font-size:13px}
.visit-status{font-size:13px;color:#666}
.new-since-visit td:first-child{box-shadow:inset 3px 0 #8b1e1e}
.new-since-visit .run-badge{display:none}
.history-link,.compare-link{font-weight:600}
.change-preview{display:none;font-size:13px;margin-top:4px;color:#7a3939}
.new-since-visit .change-preview:not(:empty){display:block}
.author-sm{display:none}
.history-page{max-width:960px;margin-inline:auto;overflow-wrap:anywhere}
.history-page h1{margin-top:12px}
.history-page h2{font-size:16px;margin:20px 0 6px}
.version-list{list-style:none;padding:0;margin:0}
.version-item,.chapter-diff{padding:12px 0;border-top:1px solid #d8d5d0}
.version-top,.version-links{display:flex;gap:4px 12px;align-items:baseline;flex-wrap:wrap}
.version-item .sub,.chapter-diff .sub{margin:0}
.field-change{margin-top:10px;font-size:13px}
.compare-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:6px}
.compare-pair > *{padding:6px 9px;border-radius:4px;white-space:pre-wrap;overflow-wrap:anywhere}
.compare-pair .no-paragraph,.legend span{background:#eee;color:#555}
del{background:#f9e1e2;color:#76232d;text-decoration:none}
ins{background:#dceee0;color:#20552b;text-decoration:none}
.compare-pair mark{color:inherit;font-weight:700}
.compare-pair del mark{background:#edbfc3}
.compare-pair ins mark{background:#b7dcbc}
.chapter-diff h2{margin-top:0}
@media (max-width:780px){
  body{padding:16px}
  .hide-sm{display:none}
  .author-sm{display:block;color:#666;font-size:13px}
  td,th{padding:8px;overflow-wrap:anywhere}
  .compare-pair{gap:4px}
}
@media (prefers-color-scheme:dark){
  body{background:#17181a;color:#e6e6e6}
  .search,.select,.pill{background:#1f2023;border-color:#34363c;color:#ddd}
  .pill.active{background:#b63a3a;border-color:#b63a3a;color:#fff}
  table{background:#1f2023;box-shadow:none}
  th{background:#242528;color:#aaa}
  td{border-bottom-color:#2c2d31}
  tr:hover td{background:#26272b}
  a{color:#f0a1a1}
  .tag{background:#2e2f34;color:#bbb}
  .fmt{background:#382626;color:#f2b6b6}
  .new{background:#1e3d24;color:#8ee09a}
  .upd{background:#423015;color:#f7c97e}
  .wip{background:#1c314d;color:#9bc2f7}
  .fandom{color:#aaa}
  .chips,.count,summary{color:#8e9299}
  details{color:#ccc}
  .detail-box{background:#18191c;border-left-color:#523c3c}
  .bm-note{border-top-color:#333;color:#e0be99}
  .visit-summary,.version-item,.chapter-diff{border-color:#34363c}
  .visit-status,.sub,.author-sm,.num,.nowrap{color:#aeb2b9}
  .change-preview{color:#f0a1a1}
  .compare-pair .no-paragraph,.legend span{background:#292b30;color:#b7bbc1}
  del{background:#42282c;color:#f4b9bf}
  ins{background:#223b2b;color:#a9e0b8}
  .compare-pair del mark{background:#6f313a}
  .compare-pair ins mark{background:#31563c}
}
"""

INDEX_JS = """
(() => {
  const q = document.getElementById('q');
  const sortSel = document.getElementById('sort');
  const pills = document.querySelectorAll('.pill[data-filter]');
  const tbody = document.querySelector('#works-table tbody');
  const countEl = document.getElementById('count');
  const emptyEl = document.getElementById('empty');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const visitSummary = document.getElementById('visit-summary');
  const visitCount = document.getElementById('visit-count');
  const visitStatus = document.getElementById('visit-status');
  const markRead = document.getElementById('mark-read');
  const visitFilter = document.querySelector('[data-filter="visit"]');
  const revision = Number(visitSummary.dataset.revision);
  const storageKey = 'ao3-bookmarks:visit:' + visitSummary.dataset.library;
  const total = rows.length;
  const nowSec = Math.floor(Date.now() / 1000);
  let activeFilter = 'all';
  let appliedSort = '';
  let baseline = revision;

  function readVisit() {
    const raw = localStorage.getItem(storageKey);
    let saved;
    try {
      saved = JSON.parse(raw || 'null');
    } catch {
      return null;
    }
    return saved && Number.isSafeInteger(saved.seen) && Number.isSafeInteger(saved.since) &&
      saved.since >= 0 && saved.since <= saved.seen ? saved : null;
  }

  function storeVisit() {
    const saved = readVisit();
    if (!saved || saved.seen <= revision) {
      localStorage.setItem(storageKey, JSON.stringify({seen: revision, since: baseline}));
    }
  }

  function showVisitChanges() {
    let added = 0;
    let updated = 0;
    for (const tr of rows) {
      const changed = Number(tr.dataset.change) > baseline;
      tr.classList.toggle('new-since-visit', changed);
      const badge = tr.querySelector('.visit-badge');
      badge.hidden = !changed;
      const isNew = Number(tr.dataset.added) > baseline;
      badge.classList.toggle('new', isNew);
      badge.classList.toggle('upd', !isNew);
      if (changed && isNew) {
        badge.textContent = '新备份';
        added++;
      } else if (changed) {
        badge.textContent = Number(tr.dataset.updated) > baseline ? '作品更新' : '资料变更';
        updated++;
      }
    }
    const count = added + updated;
    visitCount.textContent = count ? '新增 ' + added + ' 篇 · 更新 ' + updated + ' 篇' : '暂无变化';
    visitFilter.textContent = '访问后变化' + (count ? ' (' + count + ')' : '');
    markRead.hidden = !count;
    return count;
  }

  function selectFilter(filter) {
    activeFilter = filter;
    for (const pill of pills) {
      const selected = pill.dataset.filter === filter;
      pill.classList.toggle('active', selected);
      pill.setAttribute('aria-pressed', String(selected));
    }
  }

  function apply() {
    const term = (q.value || '').trim().toLowerCase();
    const terms = term ? term.split(/\\s+/) : [];
    let shown = 0;

    for (const tr of rows) {
      let ok = true;
      if (activeFilter === 'visit') ok = Number(tr.dataset.change) > baseline;
      else if (activeFilter === 'changed') ok = tr.dataset.changed === '1';
      else if (activeFilter === 'recent') ok = (nowSec - Number(tr.dataset.dl || 0)) <= 7 * 86400;
      else if (activeFilter === 'wip') ok = tr.dataset.wip === '1';
      else if (activeFilter === 'noted') ok = tr.dataset.noted === '1';

      if (ok && terms.length) {
        const hay = tr.dataset.search || '';
        for (const t of terms) {
          if (!hay.includes(t)) { ok = false; break; }
        }
      }
      tr.hidden = !ok;
      if (ok) shown++;
    }

    const mode = sortSel.value;
    if (mode !== appliedSort) {
      rows.sort((a, b) => {
        if (mode === 'title') return (a.dataset.title || '').localeCompare(b.dataset.title || '', 'zh-CN');
        if (mode === 'words') return Number(b.dataset.words || 0) - Number(a.dataset.words || 0);
        if (mode === 'dl') return Number(b.dataset.dl || 0) - Number(a.dataset.dl || 0);
        if (mode === 'change') return Number(b.dataset.change) - Number(a.dataset.change);
        return Number(b.dataset.upd || 0) - Number(a.dataset.upd || 0);
      });
      for (const tr of rows) tbody.appendChild(tr);
      appliedSort = mode;
    }

    countEl.textContent = shown === total ? `共 ${total} 篇` : `显示 ${shown} / ${total} 篇`;
    emptyEl.style.display = shown === 0 && total > 0 ? 'block' : 'none';
  }

  q.addEventListener('input', apply);
  sortSel.addEventListener('change', apply);
  for (const p of pills) {
    p.addEventListener('click', () => {
      selectFilter(p.dataset.filter);
      apply();
    });
  }
  markRead.addEventListener('click', () => {
    baseline = revision;
    try {
      storeVisit();
      visitStatus.textContent = '已标为查看';
    } catch {
      visitStatus.textContent = '浏览器无法保存访问记录';
    }
    showVisitChanges();
    if (activeFilter === 'visit') selectFilter('all');
    apply();
  });
  try {
    const saved = readVisit();
    if (!saved) {
      visitStatus.textContent = '已记录首次访问';
    } else if (saved.seen > revision) {
      visitStatus.textContent = '当前打开的是较早的总览';
    } else {
      baseline = saved.seen === revision ? saved.since : saved.seen;
    }
    storeVisit();
    visitFilter.hidden = Boolean(saved && saved.seen > revision);
    if (showVisitChanges()) {
      selectFilter('visit');
      sortSel.value = 'change';
    }
  } catch {
    baseline = revision;
    visitStatus.textContent = '浏览器无法保存访问记录';
    visitCount.textContent = '无法判断';
    visitFilter.hidden = true;
  }
  apply();
})();
"""


def _iso_to_epoch(iso_str: str) -> int:
    if not iso_str:
        return 0
    try:
        return int(datetime.fromisoformat(iso_str).timestamp())
    except ValueError:
        return 0


def change_preview(event: dict[str, Any]) -> str:
    changes = event.get("changes", {})
    labels = []
    for key, (before, after) in changes.items():
        if key == "chapters" and before and after:
            labels.append(f"章节 {before} → {after}")
        elif key == "words" and isinstance(before, int) and isinstance(after, int):
            labels.append(f"字数 {after - before:+,}")
        else:
            labels.append(WORK_FIELDS[key])
    return " · ".join(labels)


def write_index(cfg: Config, state: dict[str, Any]) -> None:
    if cfg.dry_run:
        return
    works = {
        wid: rec
        for wid, rec in state.get("works", {}).items()
        if rec.get("file") or rec.get("files")
    }
    rows = sorted(
        works.items(), key=lambda kv: kv[1].get("updated_at") or 0, reverse=True
    )
    library_id = html_mod.escape(state.setdefault("library_id", uuid4().hex))
    revision = state.get("change_seq", 0)
    generated = now_iso()
    parts: list[str] = [
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>AO3 书签备份</title><style>",
        CSS,
        "</style></head><body>",
        "<h1>AO3 书签备份</h1>",
        f"<p class='sub'>用户 <b>{html_mod.escape(cfg.bookmark_user)}</b> · "
        f"共 {len(rows)} 篇 · 生成于 <time datetime='{generated}'>"
        f"{generated[:16].replace('T', ' ')}</time></p>",
        f"<div id='visit-summary' class='visit-summary' data-library='{library_id}' "
        f"data-revision='{revision}'>"
        "<div><strong>上次访问后</strong> · <span id='visit-count' aria-live='polite'>—</span></div>"
        "<span id='visit-status' class='visit-status' role='status'></span>"
        "<button type='button' id='mark-read' class='pill' hidden>标为已查看</button></div>",
        "<div class='toolbar'>",
        "<input id='q' class='search' type='search' aria-label='搜索作品' "
        "placeholder='搜索标题、作者、标签、简介或备注…'>",
        "<div class='pills'>",
        "<button type='button' class='pill active' data-filter='all' aria-pressed='true'>全部</button>",
        "<button type='button' class='pill' data-filter='visit' hidden>访问后变化</button>",
        "<button type='button' class='pill' data-filter='changed'>本轮变更</button>",
        "<button type='button' class='pill' data-filter='recent'>7天内备份</button>",
        "<button type='button' class='pill' data-filter='wip'>连载中</button>",
        "<button type='button' class='pill' data-filter='noted'>含书签备注</button>",
        "</div>",
        "<select id='sort' class='select' aria-label='排序方式'>",
        "<option value='upd'>按作品更新</option>",
        "<option value='change'>按最近变化</option>",
        "<option value='dl'>按备份时间</option>",
        "<option value='words'>按字数</option>",
        "<option value='title'>按标题</option>",
        "</select>",
        f"<span id='count' class='count'>共 {len(rows)} 篇</span>",
        "</div>",
        "<table id='works-table'><thead><tr><th>作品</th><th class='hide-sm'>作者</th>"
        "<th class='hide-sm'>作品更新</th>"
        "<th class='hide-sm'>备份时间</th><th class='num'>字数 / 大小</th></tr></thead><tbody>",
    ]

    for wid, rec in rows:
        files_map = work_files(rec)
        fname = rec.get("file") or (next(iter(files_map.values())) if files_map else "")
        title_raw = rec.get("title") or f"work {wid}"
        title = html_mod.escape(title_raw)
        author = html_mod.escape(rec.get("author") or "")
        href = html_mod.escape(f"works/{quote(fname)}") if fname else ""
        link = f"<a href='{href}'>{title}</a>" if href else title

        badges: list[str] = [
            f"<a class='tag' href='{html_mod.escape(BASE + '/works/' + str(wid))}' "
            f"target='_blank' rel='noopener' title='在 AO3 打开原帖'>#{html_mod.escape(str(wid))}</a>"
        ]
        flag = rec.get("flag")
        if flag == "new":
            badges.append("<span class='tag new run-badge'>本轮新增</span>")
        elif flag == "updated":
            badges.append("<span class='tag upd run-badge'>本轮更新</span>")
        elif flag == "format":
            badges.append("<span class='tag'>格式补齐</span>")
        events = rec.get("history", [])
        latest = events[-1] if events else {}
        added_seq = events[0]["seq"] if events and events[0]["kind"] == "new" else 0
        updated_seq = next(
            (event["seq"] for event in reversed(events) if event["kind"] == "updated"), 0
        )
        badges.append("<span class='tag visit-badge' hidden></span>")
        history_href = write_history(cfg, wid, rec)
        if history_href:
            badges.append(f"<a class='history-link' href='{history_href}'>版本历史</a>")
        preview = html_mod.escape(change_preview(latest))

        chapters = str(rec.get("chapters") or "")
        is_wip = chapters.endswith("/?")
        if chapters:
            cls = "tag wip" if is_wip else "tag"
            badges.append(f"<span class='{cls}'>{html_mod.escape(chapters)}</span>")

        if len(files_map) > 1:
            for ext, ext_file in files_map.items():
                ext_href = html_mod.escape(f"works/{quote(ext_file)}")
                badges.append(
                    f"<a class='tag fmt' href='{ext_href}'>{html_mod.escape(ext.upper())}</a>"
                )

        fandoms = rec.get("fandoms") or []
        rels = rec.get("relationships") or []
        freeforms = rec.get("freeforms") or []
        bm_tags = rec.get("bookmark_tags") or []
        bm_notes = str(rec.get("bookmark_notes") or "")
        summary = str(rec.get("summary") or "")

        fandom_html = (
            f"<div class='fandom'>{html_mod.escape(' · '.join(fandoms))}</div>"
            if fandoms
            else ""
        )
        tag_preview = rels + freeforms[:6]
        chips_html = (
            f"<div class='chips'>{html_mod.escape(' · '.join(tag_preview))}</div>"
            if tag_preview
            else ""
        )

        details_html = ""
        if summary or bm_notes or bm_tags:
            label_parts = []
            if summary:
                label_parts.append("简介")
            if bm_notes or bm_tags:
                label_parts.append("书签备注")
            inner: list[str] = []
            if summary:
                inner.append(html_mod.escape(summary))
            if bm_tags or bm_notes:
                bm_piece = ""
                if bm_tags:
                    bm_piece += "🏷️ " + html_mod.escape(", ".join(bm_tags))
                if bm_notes:
                    bm_piece += ("\n" if bm_piece else "") + html_mod.escape(bm_notes)
                inner.append(f"<div class='bm-note'>{bm_piece}</div>")
            details_html = (
                f"<details><summary>{' / '.join(label_parts)}</summary>"
                f"<div class='detail-box'>{''.join(inner)}</div></details>"
            )

        words = int(rec.get("words") or 0)
        size_str = human_size(int(rec.get("bytes") or 0))
        words_size = f"{human_words(words)}<br><span style='font-size:11px;opacity:.75'>{size_str}</span>" if words else size_str

        dl_iso = str(rec.get("downloaded_at") or "")
        dl_epoch = _iso_to_epoch(dl_iso)
        upd_epoch = int(rec.get("updated_at") or 0)

        search_blob = " ".join(
            [
                str(wid),
                title_raw,
                str(rec.get("author") or ""),
                " ".join(fandoms),
                " ".join(rels),
                " ".join(freeforms),
                " ".join(bm_tags),
                summary,
                bm_notes,
            ]
        ).lower()

        parts.append(
            "<tr data-upd='{upd}' data-dl='{dl}' data-words='{words}' "
            "data-changed='{chg}' data-wip='{wip}' data-noted='{noted}' "
            "data-change='{change}' data-added='{added}' data-updated='{updated}' "
            "data-title='{dtitle}' data-search='{dsearch}'>"
            "<td><div class='title-line'>{link}</div>"
            "<div class='author-sm'>{author}</div><div class='meta-line'>{badges}</div>"
            "<div class='change-preview'>{preview}</div>{fandom}{chips}{details}</td>"
            "<td class='hide-sm'>{author}</td>"
            "<td class='nowrap hide-sm'>{work_upd}</td>"
            "<td class='nowrap hide-sm'>{dl_disp}</td>"
            "<td class='num'>{words_size}</td></tr>".format(
                upd=upd_epoch,
                dl=dl_epoch,
                words=words,
                chg="1" if flag in (*CHANGE_LABELS, "format") else "0",
                wip="1" if is_wip else "0",
                noted="1" if (bm_notes or bm_tags) else "0",
                change=latest.get("seq", 0),
                added=added_seq,
                updated=updated_seq,
                dtitle=html_mod.escape(title_raw, quote=True),
                dsearch=html_mod.escape(search_blob, quote=True),
                link=link,
                badges="".join(badges),
                preview=preview,
                fandom=fandom_html,
                chips=chips_html,
                details=details_html,
                author=author,
                work_upd=html_mod.escape(
                    (rec.get("work_updated_iso") or rec.get("work_date") or "")[:10]
                ),
                dl_disp=html_mod.escape(dl_iso[:19].replace("T", " ")),
                words_size=words_size,
            )
        )

    parts.append(
        "</tbody></table>"
        "<div id='empty' class='empty'>没有匹配当前筛选条件的作品</div>"
        f"<script>{INDEX_JS}</script></body></html>"
    )
    atomic_write(cfg.out_dir / "index.html", "".join(parts))


def write_manifest(cfg: Config, state: dict[str, Any]) -> None:
    if cfg.dry_run:
        return
    works = {
        wid: rec
        for wid, rec in state.get("works", {}).items()
        if rec.get("file") or rec.get("files")
    }
    rows = sorted(
        works.items(), key=lambda kv: kv[1].get("updated_at") or 0, reverse=True
    )
    path = cfg.out_dir / "manifest.csv"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(
                [
                    "work_id",
                    "title",
                    "author",
                    "work_updated",
                    "downloaded_at",
                    "bytes",
                    "file",
                    "url",
                    "words",
                    "chapters",
                    "fandoms",
                    "relationships",
                    "bookmark_tags",
                    "bookmark_notes",
                ]
            )
            for wid, rec in rows:
                w.writerow(
                    [
                        wid,
                        rec.get("title", ""),
                        rec.get("author", ""),
                        rec.get("work_updated_iso", ""),
                        rec.get("downloaded_at", ""),
                        rec.get("bytes", 0),
                        rec.get("file", ""),
                        f"{BASE}/works/{wid}",
                        rec.get("words", 0),
                        rec.get("chapters", ""),
                        "; ".join(rec.get("fandoms") or []),
                        "; ".join(rec.get("relationships") or []),
                        "; ".join(rec.get("bookmark_tags") or []),
                        rec.get("bookmark_notes", ""),
                    ]
                )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _archive_work(
    cfg: Config, client: AO3Client, archive: Archive, works: dict[str, Any],
    blurb: dict[str, Any], stop: threading.Event, stats: dict[str, int], *, retry: bool = False,
) -> bool:
    work_id = blurb["object_id"]
    wid = str(work_id)
    prior = works.get(wid, {})
    record = dict(prior)
    stamp = blurb["updated_at"] or prior.get("updated_at") or 0
    previous_bookmark = prior.get("bookmark_snapshot", snapshot(prior))
    current_bookmark = previous_bookmark if retry else snapshot(blurb)
    capture = ((cfg.refresh_content and not retry) or not prior.get("article_saved") or prior.get("error_count", 0) > 0
               or stamp != prior.get("updated_at")
               or bool(metadata_changes(previous_bookmark, current_bookmark)))
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
            record.update(error_count=record.get("error_count", 0) + 1, failed_at_stamp=stamp,
                          failed_logged_in=client.logged_in, last_error=message, last_checked=checked,
                          failed_formats=failed_formats)
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
        record.update(article_saved=True, bookmark_snapshot=current_bookmark,
                      all_freeforms="附加标签" not in article.hidden_fields or bool(prior.get("all_freeforms")),
                      hidden_fields=article.hidden_fields,
                      updated_at=stamp, downloaded_at=checked, error_count=0)
        if blurb.get("work_date"):
            record["work_date"] = blurb["work_date"]

    files: dict[str, tuple[str, bytes]] = {}
    errors = []
    base = f"{wid}_{slugify(record.get('title') or 'work')}"
    for ext in targets:
        if stop.is_set():
            raise InterruptedError
        try:
            ok, raw, error = download_work(client, cfg, blurb, ext)
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
    stats["failed"] += bool(errors)
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
) -> tuple[bool, bool]:
    """扫描书签分页列表。

    返回 (page_ok, stopped_early_by_idle_or_limit)：
    - page_ok: 分页抓取是否正常完成（未发生 404/403/5xx/网络中断）
    - stopped_early: 是否因 STOP_AFTER_IDLE_PAGES 提前结束翻页
    """
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

        # 第 1 页检查登录状态：若配了账密但 Session 已过期，立刻重登并重抓第 1 页
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

            # 第 1 页成功拿到后，清理上一轮遗留的 NEW/UPDATED 标记
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
                    page_changed += _archive_work(cfg, client, archive, works, b, stop, stats)
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

            # 若作品本身有了新更新，或此前在未登录状态下失败而现在已登录，自动解除错误熔断
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
                    ok, raw, err = download_work(client, cfg, b, ext)
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

            # 状态和索引仍可能引用旧路径，提交之前保留旧文件。
            try:
                for ext, raw in got.items():
                    atomic_write(cfg.works_dir / new_files[ext], raw)
                if previous and cfg.keep_versions:
                    for ext, name in existing_files.items():
                        old = cfg.works_dir / name
                        if old.is_file():
                            atomic_write(cfg.versions_dir / wid / f"{previous}.{ext}", old)
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
                save_state(cfg, state)
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


def retry_works(
    cfg: Config, client: AO3Client, archive: Archive, state: dict[str, Any], stop: threading.Event,
) -> tuple[dict[str, int], bool]:
    works = state["works"]
    targets = [wid for wid, record in works.items()
               if not record.get("article_saved") or record.get("error_count")
               or missing_attachments(record, cfg.formats, work_files(record))]
    stats = run_stats()
    log.info("待补抓 %d 篇作品", len(targets))
    for wid in targets:
        if stop.is_set():
            return stats, False
        record = works[wid]
        blurb = {"object_id": int(wid), "title": record.get("title", ""),
                 "updated_at": record.get("failed_at_stamp") or record.get("updated_at")}
        stats["seen"] += 1
        touch_marker(cfg.running_file)
        _archive_work(cfg, client, archive, works, blurb, stop, stats, retry=True)
        if cfg.max_downloads and stats["downloaded"] >= cfg.max_downloads:
            break
    return stats, True


def run_once(
    cfg: Config, client: AO3Client, state: dict[str, Any], stop: threading.Event,
    *, archive: Archive | None = None,
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
    )

    # AO3 默认按收藏时间排序；若启用了提前结束翻页，补扫按作品更新时间排序的前几页，避免漏掉老书签连载更新
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


def run_scheduler(
    cfg: Config, archive: Archive, stop: threading.Event,
    execute_pass: Callable[[Config, str], tuple[bool, dict[str, int]]],
) -> None:
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
                stop.wait(BACKUP_POLL_SECONDS)
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
        stop.wait(BACKUP_POLL_SECONDS)


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
            source = args.import_legacy.expanduser()
            with acquire_lock(out / ".backup.lock"):
                result = import_legacy(archive, source)
            label = "已核对现有迁移" if result.already_imported else "迁移完成"
            print(f"{label}：{result.works} 条作品记录，{result.files} 份文件，{result.size:,} 字节")
            for path in result.missing:
                print(f"旧库缺失文件：{path}", file=sys.stderr)
            return 0
        if args.backup_db:
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
            from ao3_web import serve

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
            client.cfg = cfg
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

                # 仅在分页正常扫完、未被信号中断、且并非全军覆没时才刷新健康检查时间戳
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


if __name__ == "__main__":
    sys.exit(main())
