"""Automated verification of MIT Axiomatic Design decoupling and runtime safety."""

from __future__ import annotations

import ast
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ao3_db import Database
from ao3_http import (
    AO3Client,
    AO3HttpConfig,
    _IPv4FirstHTTPConnection,
    _IPv4FirstHTTPHandler,
    _IPv4FirstHTTPSConnection,
    _IPv4FirstHTTPSHandler,
    clear_cookies_file,
    cookie_lock,
    prefer_ipv4,
)
from ao3_repos import (
    AuthCredentialRepo,
    BackupSettings,
    ReaderStateRepo,
    SchedulerQueueRepo,
    WorkArchiveRepo,
)


ROOT = Path(__file__).resolve().parent
LAYER_ORDER = [
    "ao3_content",
    "ao3_db",
    "ao3_http",
    "ao3_diff",
    "ao3_repos",
    "ao3_store",
    "ao3_legacy",
    "ao3_scraper",
    "ao3_web",
    "ao3_scheduler",
    "ao3_cli",
    "ao3_backup",
]


def _extract_internal_imports(path: Path, known: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".", 1)[0]
                if mod in known:
                    found.add(mod)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mod = node.module.split(".", 1)[0]
            if mod in known:
                found.add(mod)
    return found


class AxiomaticArchitectureTests(unittest.TestCase):
    def test_strict_lower_triangular_import_matrix_and_dag(self) -> None:
        disk_modules = sorted(p.stem for p in ROOT.glob("ao3_*.py"))
        self.assertEqual(sorted(LAYER_ORDER), disk_modules)

        known = set(LAYER_ORDER)
        index = {name: idx for idx, name in enumerate(LAYER_ORDER)}
        graph: dict[str, set[str]] = {}

        for mod in LAYER_ORDER:
            deps = _extract_internal_imports(ROOT / f"{mod}.py", known)
            graph[mod] = deps
            for dep in deps:
                self.assertLess(
                    index[dep],
                    index[mod],
                    f"Upper-triangular/diagonal dependency violation: {mod} -> {dep}",
                )

        # Layer 0 modules must have zero internal dependencies.
        for base_mod in ("ao3_content", "ao3_db", "ao3_http"):
            self.assertEqual(graph[base_mod], set(), f"{base_mod} must have zero internal imports")

        # Web UI must not depend on backup, scraper, scheduler, legacy, or CLI.
        self.assertEqual(
            graph["ao3_web"] & {"ao3_backup", "ao3_scraper", "ao3_scheduler", "ao3_legacy", "ao3_cli"},
            set(),
        )

        # Tarjan SCC check: confirm zero strongly connected components of size > 1 (pure DAG).
        sccs: list[list[str]] = []
        indices: dict[str, int] = {}
        lowlink: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        counter = 0

        def strongconnect(node: str) -> None:
            nonlocal counter
            indices[node] = counter
            lowlink[node] = counter
            counter += 1
            stack.append(node)
            on_stack.add(node)
            for nxt in graph[node]:
                if nxt not in indices:
                    strongconnect(nxt)
                    lowlink[node] = min(lowlink[node], lowlink[nxt])
                elif nxt in on_stack:
                    lowlink[node] = min(lowlink[node], indices[nxt])
            if lowlink[node] == indices[node]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.remove(w)
                    comp.append(w)
                    if w == node:
                        break
                sccs.append(comp)

        for mod in LAYER_ORDER:
            if mod not in indices:
                strongconnect(mod)

        cycles = [comp for comp in sccs if len(comp) > 1]
        self.assertEqual(cycles, [], f"Circular module dependencies detected: {cycles}")

    def test_no_global_socket_getaddrinfo_monkey_patch(self) -> None:
        orig = socket.getaddrinfo
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefer_ipv4(True)
            self.assertIs(socket.getaddrinfo, orig)

            cfg_v4 = AO3HttpConfig(out_dir=root, prefer_ipv4=True)
            client = AO3Client(cfg_v4)
            self.assertIs(socket.getaddrinfo, orig)
            self.assertTrue(any(isinstance(h, _IPv4FirstHTTPHandler) for h in client.opener.handlers))
            self.assertTrue(any(isinstance(h, _IPv4FirstHTTPSHandler) for h in client.opener.handlers))

            cfg_default = AO3HttpConfig(out_dir=root, prefer_ipv4=False)
            client.configure(cfg_default)
            self.assertIs(socket.getaddrinfo, orig)
            self.assertFalse(any(isinstance(h, _IPv4FirstHTTPHandler) for h in client.opener.handlers))
            self.assertFalse(any(isinstance(h, _IPv4FirstHTTPSHandler) for h in client.opener.handlers))

    def test_ipv4_first_handlers_invoke_do_open_without_attribute_error(self) -> None:
        from unittest.mock import patch
        from urllib.request import Request

        req = Request("https://archiveofourown.org/")
        http_handler = _IPv4FirstHTTPHandler()
        https_handler = _IPv4FirstHTTPSHandler()

        with patch.object(http_handler, "do_open", return_value="http-ok") as m_http:
            self.assertEqual(http_handler.http_open(req), "http-ok")
            m_http.assert_called_once_with(_IPv4FirstHTTPConnection, req)

        with patch.object(https_handler, "do_open", return_value="https-ok") as m_https:
            self.assertEqual(https_handler.https_open(req), "https-ok")
            m_https.assert_called_once_with(
                _IPv4FirstHTTPSConnection, req, context=https_handler._context,
            )

    def test_cookie_lock_serializes_access_and_clears_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cookies_path = root / "cookies.txt"
            cookies_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

            entered = threading.Event()
            second_started = threading.Event()
            second_acquired = threading.Event()

            def holder() -> None:
                with cookie_lock(root, timeout=5.0):
                    entered.set()
                    time.sleep(0.25)

            def waiter() -> None:
                entered.wait(timeout=2.0)
                second_started.set()
                with cookie_lock(root, timeout=5.0):
                    second_acquired.set()

            t1 = threading.Thread(target=holder)
            t2 = threading.Thread(target=waiter)
            t1.start()
            t2.start()

            self.assertTrue(second_started.wait(timeout=2.0))
            time.sleep(0.08)
            self.assertFalse(second_acquired.is_set(), "Second thread acquired cookie_lock before holder exited")

            t1.join(timeout=2.0)
            t2.join(timeout=2.0)
            self.assertTrue(second_acquired.is_set())
            self.assertTrue((root / ".cookies.lock").exists())

            clear_cookies_file(root)
            self.assertFalse(cookies_path.exists())

    def test_orthogonal_repositories_operate_independently(self) -> None:
        self.assertFalse(hasattr(WorkArchiveRepo, "compare"))

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp))
            db.initialize()

            auth = AuthCredentialRepo(db)
            scheduler = SchedulerQueueRepo(db)
            readers = ReaderStateRepo(db)
            works = WorkArchiveRepo(db)

            token = auth.setup_web_auth("secret123")
            self.assertTrue(auth.verify_session_token(token))
            self.assertTrue(auth.verify_web_password("secret123"))

            settings = BackupSettings(bookmark_user="tester", schedule_mode="manual")
            scheduler.set_backup_settings(settings)
            self.assertEqual(scheduler.backup_settings(), settings)
            self.assertTrue(scheduler.queue_backup("scan"))
            job = scheduler.claim_backup(None)
            self.assertIsNotNone(job)
            scheduler.finish_backup(job[0], "success", "ok")

            reader, created = readers.reader("", visit=True)
            self.assertTrue(created)
            self.assertEqual(readers.overview(reader), [])

            works.bind_user("tester")
            self.assertEqual(works.stored_bookmark_user(), "tester")
            self.assertFalse(works.has_works())

    def test_dockerfile_copies_all_python_modules(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        copied: set[str] = set()
        for line in dockerfile.splitlines():
            if line.strip().startswith("COPY ") and "--from=" not in line:
                copied.update(re.findall(r"\bao3_[a-z0-9_]+\.py\b", line))
        disk = {p.name for p in ROOT.glob("ao3_*.py")}
        self.assertEqual(copied, disk)


if __name__ == "__main__":
    unittest.main()
