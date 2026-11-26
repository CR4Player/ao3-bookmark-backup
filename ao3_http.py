"""HTTP transport, AO3 authentication, and cross-process file locking (Layer 0)."""

from __future__ import annotations

import contextlib
import html as html_mod
import http.client
import logging
import os
import re
import shutil
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPHandler, HTTPCookieProcessor, HTTPSHandler, Request, build_opener


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
COOKIE_FILE_NAME = "cookies.txt"
COOKIE_LOCK_NAME = ".cookies.lock"

log = logging.getLogger("ao3")


# --------------------------------------------------------------------------
# 网络：实例级 IPv4 优先（不修改全局 socket.getaddrinfo）& URL 安全编码
# --------------------------------------------------------------------------
def _create_connection_v4_first(
    address: tuple[str, int],
    timeout: float | None = socket._GLOBAL_DEFAULT_TIMEOUT,  # type: ignore[attr-defined]
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Resolve address with AF_INET sorted before AF_INET6 for a single connection."""
    host, port = address
    err: OSError | None = None
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    infos = sorted(infos, key=lambda info: 0 if info[0] == socket.AF_INET else 1)
    for af, socktype, proto, _canonname, sa in infos:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(af, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:  # type: ignore[attr-defined]
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            err = None
            return sock
        except OSError as exc:
            err = exc
            if sock is not None:
                sock.close()
    if err is not None:
        raise err
    raise OSError("getaddrinfo returns an empty list")


class _IPv4FirstHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection_v4_first


class _IPv4FirstHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection_v4_first


class _IPv4FirstHTTPHandler(HTTPHandler):
    def http_open(self, req: Request):
        return self.do_open(_IPv4FirstHTTPConnection, req)


class _IPv4FirstHTTPSHandler(HTTPSHandler):
    def https_open(self, req: Request):
        return self.do_open(_IPv4FirstHTTPSConnection, req, context=self._context)


def prefer_ipv4(enabled: bool = True) -> None:
    """Compatibility helper retained for callers; IPv4 preference is now scoped per AO3Client."""
    if enabled:
        log.debug("已启用实例级 IPv4 优先解析")


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
# 文件原子写与跨进程锁
# --------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write(path: Path, data: bytes | str | Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
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
        atomic_write(path, _now_iso())


@contextlib.contextmanager
def acquire_lock(lock_path: Path, *, blocking: bool = False, timeout: float = 0.0) -> Iterator[None]:
    """跨平台文件锁。

    - blocking=False（默认）：非阻塞单实例锁，冲突时立即抛 RuntimeError。
    - blocking=True：在 timeout 秒内轮询等待获取锁，适用于 cookies.txt 等短临界区。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+", encoding="utf-8")
    locked = False
    deadline = time.monotonic() + max(0.0, timeout)

    def _try_lock() -> bool:
        if os.name == "nt":
            import msvcrt

            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(" ")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True

    try:
        while True:
            try:
                locked = _try_lock()
                break
            except OSError as exc:
                if not blocking or time.monotonic() >= deadline:
                    if blocking:
                        raise TimeoutError(f"等待文件锁超时：{lock_path}") from exc
                    raise RuntimeError(
                        f"输出目录已被另一备份进程锁定（{lock_path}）。"
                        "请等待当前任务完成，或加 --dry-run 只读预览。"
                    ) from exc
                time.sleep(0.05)

        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()} {_now_iso()}\n")
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


@contextlib.contextmanager
def cookie_lock(out_dir: Path, timeout: float = 30.0) -> Iterator[None]:
    """Cross-process lock serializing reads/writes/removals of cookies.txt."""
    with acquire_lock(out_dir / COOKIE_LOCK_NAME, blocking=True, timeout=timeout):
        yield


def clear_cookies_file(out_dir: Path) -> None:
    """Safely delete cookies.txt under the cross-process cookie lock."""
    with cookie_lock(out_dir):
        (out_dir / COOKIE_FILE_NAME).unlink(missing_ok=True)


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


@dataclass
class AO3HttpConfig:
    """Minimal transport and authentication configuration required by AO3Client."""

    out_dir: Path
    user: str = ""
    password: str = ""
    delay: float = 1.5
    timeout: int = 60
    retries: int = 4
    dry_run: bool = False
    prefer_ipv4: bool = True
    auth_mode: str = ""

    @property
    def cookie_file(self) -> Path:
        return self.out_dir / COOKIE_FILE_NAME


class AO3Client:
    def __init__(
        self, cfg: AO3HttpConfig, stop: Optional[threading.Event] = None,
        *, status_cb: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.cfg = cfg
        self.stop = stop
        self.status_cb = status_cb
        self.jar = MozillaCookieJar(str(cfg.cookie_file))
        self._cookie_mtime = 0.0
        self.reload_cookies(force=True)
        self.opener = self._build_opener(bool(getattr(cfg, "prefer_ipv4", True)))
        self._last = 0.0
        self.logged_in = False
        self.last_login_error = ""

    def _build_opener(self, use_ipv4_first: bool):
        handlers: list[object] = [HTTPCookieProcessor(self.jar)]
        if use_ipv4_first:
            handlers.extend([_IPv4FirstHTTPHandler(), _IPv4FirstHTTPSHandler()])
        return build_opener(*handlers)

    def configure(self, cfg: AO3HttpConfig) -> None:
        """Update client config and rebuild opener if prefer_ipv4 changed."""
        prev_v4 = bool(getattr(self.cfg, "prefer_ipv4", True))
        self.cfg = cfg
        new_v4 = bool(getattr(cfg, "prefer_ipv4", True))
        if new_v4 != prev_v4:
            self.opener = self._build_opener(new_v4)

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
            with cookie_lock(self.cfg.out_dir):
                if not self.cfg.cookie_file.exists():
                    if self._cookie_mtime:
                        self.jar.clear()
                        self._cookie_mtime = 0.0
                        self.logged_in = False
                    return
                mtime = self.cfg.cookie_file.stat().st_mtime
                if not force and mtime == self._cookie_mtime:
                    return
                self.jar.clear()
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
            with cookie_lock(self.cfg.out_dir):
                self.cfg.cookie_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.cfg.cookie_file.with_name(
                    f"{self.cfg.cookie_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
                )
                try:
                    self.jar.save(str(tmp), ignore_discard=True, ignore_expires=True)
                    if os.name != "nt":
                        with contextlib.suppress(OSError):
                            os.chmod(tmp, 0o600)
                    os.replace(tmp, self.cfg.cookie_file)
                finally:
                    if tmp.exists():
                        with contextlib.suppress(OSError):
                            tmp.unlink()
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
    """Verify AO3 credentials online and persist session cookies atomically."""
    user = user.strip()
    if not user or not password:
        raise ValueError("请填写完整的 AO3 用户名和密码")
    attempts = max(1, min(retries, 5))
    tmp_cfg = AO3HttpConfig(
        out_dir=out_dir,
        user=user,
        password=password,
        delay=0.5,
        timeout=max(5, timeout),
        retries=attempts,
        prefer_ipv4=prefer_v4,
        auth_mode="cookie",
    )
    client = AO3Client(tmp_cfg, stop=stop, status_cb=status_cb)
    try:
        ok = client.login(force=True)
    except InterruptedError as exc:
        raise ValueError("已取消 AO3 凭据在线校验") from exc
    except HTTPError as exc:
        if exc.code == 429:
            raise ValueError(
                f"AO3 请求过于频繁（HTTP 429 限流），自动重试 {attempts} 次后仍受限，请稍候几分钟后再试"
            ) from exc
        raise ValueError(f"连接 AO3 验证登录失败：{_format_net_error(exc)}") from exc
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError(f"连接 AO3 超时或网络失败（已尝试 {attempts} 次）：{_format_net_error(exc)}") from exc
    if not ok:
        raise ValueError(client.last_login_error or "AO3 登录失败：请核对用户名/密码，或确认未被验证码拦截")
