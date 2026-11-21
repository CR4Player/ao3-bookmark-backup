import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from unittest.mock import patch

import ao3_backup as backup
from ao3_diff import Difference, compare_exports, compare_snapshots, read_export, read_work_page
from ao3_store import (
    Archive, BackupSettings, VISIT_WINDOW_SECONDS, compute_next_runs, import_legacy, schedule_label,
)
from ao3_web import (
    PER_PAGE, article_html, backup_status, download_filename, home_url, overview_html, render_diff,
    submitted_settings, work_html,
)
from test_ao3_backup import FixtureClient, bookmark


def html_export(*, body="旧正文", start="", end="", css="", chapter="第一个标题"):
    notes = (f'<div class="meta" id="endnotes1"><p>Chapter End Notes</p>'
             f'<blockquote class="userstuff"><p>{end}</p></blockquote></div>' if end else "")
    jump = '<div class="endnote-link"><a href="#endnotes1">Go to notes</a></div>' if end else ""
    opening = (f'<p>Chapter Notes</p><blockquote class="userstuff"><p>{start}</p></blockquote>'
               if start else "")
    return (
        f'<html><head><title>示例作品</title><style>{css}</style></head><body>'
        '<div id="preface"><div class="meta"><h1>示例作品</h1></div></div>'
        f'<div id="chapters"><div class="meta group"><h2 class="heading">Chapter 1: {chapter}</h2>'
        f'{opening}{jump}</div><div class="userstuff"><p>{body}</p></div>{notes}</div></body></html>'
    ).encode()


def work_page(*, body="旧正文", start="", end="", chapter="第一个标题",
              summary="海港的来信。", count="1/?", tags=("悬疑",), single=False, tags_hidden=False):
    def note(classes, text, id_=""):
        return (f'<div class="{classes} module" id="{id_}"><h3 class="heading">Notes:</h3>'
                f'<blockquote class="userstuff"><p>{text}</p></blockquote></div>') if text else ""

    tag_links = "".join(f'<li><a href="/tags/test/works">{tag}</a></li>' for tag in tags)
    if tags_hidden:
        tag_links = '<li id="work_101_category_freeforms"><a href="/works/101/freeforms">Show additional tags</a></li>'
    prose = ('<div class="userstuff module" role="article">'
             f'<h3 class="landmark heading" id="work">Chapter Text</h3><p>{body}</p></div>')
    chapters = prose if single else (
        '<div class="chapter" id="chapter-1"><div class="chapter preface group">'
        f'<h3 class="title"><a href="/works/101/chapters/123">Chapter 1</a>: {chapter}</h3>'
        f'{note("summary", "章节简介测试")}{note("notes", start)}</div>{prose}'
        f'<div class="chapter preface group">{note("end notes", end, "chapter_1_endnotes")}</div></div>'
    )
    return (
        '<html><body><div id="main"><div class="work"><div class="wrapper">'
        '<dl class="work meta group"><dd class="rating tags"><ul><li>'
        '<a href="/tags/General/works">General Audiences</a></li></ul></dd>'
        '<dd class="fandom tags"><ul><li><a href="/tags/test/works">原创作品</a></li></ul></dd>'
        f'<dd class="freeform tags"><ul>{tag_links}</ul></dd>'
        '<dd class="series"><a class="previous" href="/works/100">Previous Work</a>'
        '<span class="position">Part 2 of <a href="/series/2">示例系列</a></span></dd>'
        '<dd class="stats"><dl class="stats"><dd class="words">10</dd>'
        f'<dd class="chapters">{count}</dd></dl></dd></dl></div>'
        '<div id="work-skin"><div id="workskin"><div class="preface group">'
        '<h2 class="title heading"><img alt="(Restricted)" src="/lock.png">示例作品</h2>'
        '<h3 class="byline heading"><a rel="author" href="/users/author">织字人</a></h3>'
        f'{note("summary", summary)}{note("notes", "全篇首注")}</div>'
        f'<div id="chapters">{chapters}</div>'
        f'<div class="afterword preface group">{note("end notes", "全篇尾注", "work_endnotes")}</div>'
        '</div></div></div><div id="comments"><div class="userstuff"><p>读者的评论</p></div></div></body></html>'
    ).encode()


class PageClient(FixtureClient):
    def __init__(self, blurb, html, page):
        super().__init__([blurb], {101: html})
        self.page = page

    def get_bytes(self, path):
        if path.startswith("/works/"):
            self.requests.append(path)
            return self.page
        return super().get_bytes(path)


class SQLiteArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.out = Path(temp.name)
        self.archive = Archive(self.out)
        self.archive.initialize()
        self.cfg = backup.Config("review", self.out)
        self.old = html_export()
        self.new = html_export(end="&lt;附注&gt;")

    def scan(self, stamp, html, cfg=None, *, page=None, **metadata):
        cfg = cfg or self.cfg
        if page is None:
            fields = {key: metadata[key] for key in ("summary", "tags") if key in metadata}
            page = work_page(end="&lt;附注&gt;" if html == self.new else "", **fields)
        client = PageClient(bookmark(101, "示例作品", stamp, **metadata), html, page)
        state = self.archive.scan_state(cfg.bookmark_user)
        _, ok = backup.run_once(cfg, client, state, threading.Event(), archive=self.archive)
        self.assertTrue(ok)
        return client.requests

    def detail(self, reader=None):
        if reader is None:
            reader, _ = self.archive.reader("")
        return self.archive.detail("101", reader)

    def test_notes_and_body_are_separate_differences(self):
        note_changes = compare_exports(self.old, self.new, {}, {})
        self.assertEqual([change.heading for change in note_changes],
                         ["第 1 章 · 新增章末作者附注"])
        different_text = html_export(body="新正文", start="章首的话", chapter="新章名")
        changes = compare_exports(self.new, different_text, {}, {})
        self.assertEqual({change.heading for change in changes}, {
            "第 1 章 · 修改正文", "第 1 章 · 新增章首作者附注",
            "第 1 章 · 删除章末作者附注", "第 1 章 · 修改章名",
        })

    def test_work_page_and_legacy_export_align(self):
        legacy = html_export(start="章首", end="章末")
        legacy = legacy.replace(
            "<h1>示例作品</h1>".encode(),
            ('<dl><dt>Rating:</dt><dd><a>General Audiences</a></dd>'
             '<dt>Fandom:</dt><dd><a>原创作品</a></dd><dt>Additional Tags:</dt><dd><a>悬疑</a></dd>'
             '<dt>Series:</dt><dd>Part 2 of <a href="/series/2">示例系列</a></dd>'
             '<dt>Stats:</dt><dd>Words: 10 Chapters: 1/?</dd></dl>'
             '<h1>示例作品</h1><div class="byline">by <a>织字人</a></div>'
             '<p>Summary:</p><blockquote class="userstuff"><p>海港的来信。</p></blockquote>'
             '<p>Notes:</p><blockquote class="userstuff"><p>全篇首注</p></blockquote>').encode(),
        ).replace(
            b'<p>Chapter Notes</p>',
            '<p>Chapter Summary</p><blockquote class="userstuff"><p>章节简介测试</p></blockquote>'
            '<p>Chapter Notes</p>'.encode(),
        ).replace(
            b'</body>', '<div id="afterword"><blockquote class="userstuff"><p>全篇尾注</p>'
            '</blockquote></div></body>'.encode(),
        )
        saved = read_export(legacy)
        direct = read_work_page(work_page(start="章首", end="章末"))
        self.assertEqual(compare_snapshots(saved, direct, {}, {}), [])
        self.assertEqual(direct.fields["系列"], ("示例系列",))
        self.assertNotIn("读者的评论", direct.to_markdown({}))
        one_chapter = read_work_page(work_page(single=True, count="1/1"))
        self.assertEqual(len(one_chapter.chapters), 1)
        self.assertNotIn("Chapter Text", one_chapter.to_markdown({}))

    def test_without_attachments_refresh_and_restore(self):
        cfg = replace(self.cfg, formats=())
        self.assertEqual(self.scan(100, self.old, cfg),
                         ["/works/101?view_full_work=true&view_adult=true"])
        reader, _ = self.archive.reader("", visit=True)
        self.assertEqual(self.scan(100, self.new, cfg), [])
        self.scan(100, self.new, replace(cfg, refresh_content=True))
        _, versions, events, _ = self.detail(reader)
        old, new = versions.values()
        self.assertEqual((old.files, new.files), ({}, {}))
        self.assertEqual([event["kind"] for event in events], ["updated", "new"])
        self.assertEqual([d.heading for d in self.archive.compare(old, new)],
                         ["第 1 章 · 新增章末作者附注"])
        reader, _ = self.archive.reader(reader["id"], visit=True)
        for view in ("since", "unread"):
            self.assertIn("第 1 章 · 新增章末作者附注",
                          overview_html(self.archive, reader, view, "", "event"))
        restored = Archive(self.out / "restored")
        self.archive.backup(restored.path)
        version, article = restored.read_article("101", new.id)
        self.assertIn("旧正文", article_html("101", version, article))
        self.assertIn("### 章末作者附注\n\n\\<附注\\>", article.to_markdown(version.metadata))
        with restored.connect(readonly=True) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM blobs").fetchone()[0], 0)

    def test_incomplete_work_keeps_last_good_version_and_retries(self):
        cfg = replace(self.cfg, formats=())
        self.scan(100, self.old, cfg)
        self.scan(200, self.new, cfg, page=work_page(count="2/?"))
        saved, versions, events, _ = self.detail()
        self.assertEqual(saved["updated_at"], 100)
        self.assertIn("章节数或正文不完整", saved["last_error"])
        self.assertEqual((len(versions), len(events)), (1, 1))
        self.scan(200, self.new, cfg)
        saved, versions, events, _ = self.detail()
        self.assertNotIn("last_error", saved)
        self.assertEqual((len(versions), len(events)), (2, 2))

    def test_hidden_tags_allow_capture_without_reporting_tag_removal(self):
        cfg = replace(self.cfg, formats=())
        self.scan(100, self.old, cfg)
        self.scan(200, self.old, cfg, page=work_page(tags_hidden=True))
        _, versions, events, _ = self.detail()
        self.assertEqual((len(versions), len(events)), (1, 1))

        self.scan(300, self.old, cfg, page=work_page(body="新正文", tags_hidden=True))
        _, versions, events, _ = self.detail()
        before, after = versions.values()
        _, article = self.archive.read_article("101", after.id)
        self.assertEqual(article.hidden_fields, ["附加标签"])
        self.assertEqual(article.fields["附加标签"], ("悬疑",))
        self.assertEqual([diff.heading for diff in self.archive.compare(before, after)], ["第 1 章 · 修改正文"])

        self.scan(400, self.old, cfg, page=work_page(body="新正文", tags=("新标签",)))
        _, versions, events, _ = self.detail()
        self.assertEqual((len(versions), len(events)), (2, 2))
        self.assertEqual(self.archive.read_latest_article("101")[1].fields["附加标签"], ("新标签",))
        self.assertFalse(self.archive.read_latest_article("101")[1].hidden_fields)

    def test_manual_retry_fills_missing_body_and_skips_healthy_works(self):
        cfg = replace(self.cfg, max_errors=1, formats=())
        self.scan(100, self.old, cfg, page=work_page(count="2/?"))
        self.archive.save_capture("102", {"updated_at": 100, "downloaded_at": backup.now_iso()},
                                  read_work_page(work_page()), {})
        self.assertEqual(self.scan(100, self.old, cfg), [])

        client = PageClient(bookmark(101, "示例作品", 100), self.old, work_page(tags_hidden=True))
        state = self.archive.scan_state(cfg.bookmark_user)
        stats, ok = backup.retry_works(cfg, client, self.archive, state, threading.Event())
        self.assertTrue(ok)
        self.assertEqual((stats["seen"], stats["downloaded"], stats["failed"]), (1, 1, 0))
        self.assertEqual(client.requests, ["/works/101?view_full_work=true&view_adult=true"])
        version, article, fallback = self.archive.read_latest_article("101")
        self.assertEqual(version.ao3_updated_at, 100)
        self.assertFalse(fallback)
        self.assertIn("旧正文", article.to_markdown(version.metadata))
        self.assertNotIn("last_error", self.detail()[0])

    def test_mystery_work_reports_unrevealed_body(self):
        with self.assertRaisesRegex(ValueError, "Mystery Work"):
            read_work_page(b"<title>Mystery Work | Archive of Our Own</title><p>No chapter is available.</p>")

    def test_backup_dashboard_upgrades_schema_and_recovers_interrupted_run(self):
        self.scan(100, self.old)
        before = self.archive.read_latest_article("101")
        with self.archive.write() as db:
            db.execute("DROP TABLE backup_runs")
            db.execute("PRAGMA user_version=3")
        self.archive.initialize()
        self.archive.initialize()
        self.assertEqual(before, self.archive.read_latest_article("101"))

        settings = BackupSettings(interval_seconds=3600, retries=2)
        self.archive.set_backup_settings(settings)
        self.assertTrue(self.archive.queue_backup("retry"))
        self.assertFalse(self.archive.queue_backup("retry"))
        first, action, _ = self.archive.claim_backup(None)
        self.assertEqual(action, "retry")
        self.assertTrue(self.archive.queue_backup("scan"))
        second, action, _ = self.archive.claim_backup(None)
        self.assertEqual(action, "scan")
        _, saved, active, last = self.archive.backup_dashboard()
        self.assertEqual(saved, settings)
        self.assertEqual((active[0]["id"], active[0]["status"]), (second, "running"))
        self.assertEqual((last["id"], last["status"]), (first, "interrupted"))
        self.archive.finish_backup(second, "success")
        self.assertIsNone(self.archive.claim_backup(None))

    def test_official_attachment_failure_does_not_block_article(self):
        with patch.object(backup, "download_work", return_value=(False, b"", "暂不可用")):
            self.scan(100, self.old)
        saved, versions, events, _ = self.detail()
        self.assertEqual(len(events), 1)
        self.assertIn("暂不可用", saved["last_error"])
        self.assertTrue(next(iter(versions.values())).document_digest)
        self.assertIn(".html?", self.scan(100, self.old)[0])
        saved, versions, events, _ = self.detail()
        self.assertNotIn("last_error", saved)
        self.assertEqual(len(events), 1)
        self.assertIn("html", next(iter(versions.values())).files)

    def test_refetch_identical_bytes_and_format_fill_do_not_create_events(self):
        self.scan(100, self.old)
        self.assertEqual(len(self.scan(200, self.old)), 2)
        self.assertEqual(self.scan(200, self.old), [])
        cfg = replace(self.cfg, formats=("html", "epub"))
        self.assertIn(".epub?", self.scan(200, self.old, cfg=cfg)[0])
        record, versions, events, _ = self.detail()
        self.assertEqual(record["updated_at"], 200)
        self.assertEqual(len(versions), 1)
        self.assertEqual(len(events), 1)
        version = next(iter(versions.values()))
        self.assertEqual(version.ao3_updated_at, 100)
        self.assertEqual(set(version.files), {"html", "epub"})

    def test_markup_and_metadata_are_reported_independently(self):
        self.scan(100, self.old)
        markup = html_export(css="p {color: black;}")
        self.scan(200, markup)
        self.assertEqual(self.scan(200, markup, note="私人备注"), [])
        self.assertEqual(len(self.scan(200, markup, summary="新简介")), 2)
        _, versions, events, _ = self.detail()
        a, b, c = versions.values()
        self.assertEqual([event["kind"] for event in events], ["metadata", "file", "new"])
        self.assertEqual(self.archive.compare(a, b), [])
        self.assertEqual([d.heading for d in self.archive.compare(a, c)], ["作品资料 · 修改作品简介"])
        self.assertEqual(c.files["html"].digest, b.files["html"].digest)

    def test_write_failure_rolls_back_version_blob_and_event(self):
        self.scan(100, self.old)
        with self.archive.write() as db:
            db.execute("""CREATE TRIGGER block_event BEFORE INSERT ON events
                          BEGIN SELECT RAISE(ABORT, 'test write failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test write failure"):
            self.scan(200, self.new)
        self.assertEqual(self.archive.scan_state("review")["works"]["101"]["updated_at"], 100)
        with self.archive.write() as db:
            for table in ("blobs", "versions", "events"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1)
            db.execute("DROP TRIGGER block_event")
        self.scan(200, self.new)
        self.assertEqual(len(self.detail()[1]), 2)

    def test_overview_refresh_and_older_tab_cannot_clear_unread(self):
        self.scan(100, self.old)
        reader, _ = self.archive.reader("", visit=True)
        self.scan(200, self.new)
        first_visit, _ = self.archive.reader(reader["id"], visit=True)
        first_event = self.archive.overview(first_visit)[0]["last_event"]
        self.scan(200, self.new, summary="更新简介")
        next_visit, _ = self.archive.reader(reader["id"], visit=True)
        refresh, _ = self.archive.reader(reader["id"], visit=True)
        self.assertEqual(refresh, next_visit)
        self.assertEqual(self.archive.overview(refresh)[0]["after_visit"], 1)
        status, page = work_html(self.archive, refresh, "101", {})
        self.assertEqual(status, 200)
        self.assertIn("上次读到的版本 ↔ 最新版", page)
        self.assertIn("&lt;附注&gt;", page)
        self.assertIn("第 1 章 · 新增章末作者附注",
                      overview_html(self.archive, refresh, "unread", "", "event"))
        self.archive.mark_read(reader["id"], "101", first_event)
        row = self.archive.overview(refresh)[0]
        self.assertGreater(row["last_event"], row["read_through"])
        with patch("ao3_store.time.time", return_value=refresh["overview_at"] + VISIT_WINDOW_SECONDS):
            later, _ = self.archive.reader(reader["id"], visit=True)
        self.assertEqual(self.archive.overview(later)[0]["after_visit"], 0)
        self.assertGreater(self.archive.overview(later)[0]["last_event"],
                           self.archive.overview(later)[0]["read_through"])
        self.archive.mark_read(reader["id"], "101", row["last_event"])
        self.archive.mark_read(reader["id"], "101", first_event)
        self.assertEqual(self.archive.overview(refresh)[0]["read_through"], row["last_event"])
        self.assertEqual(self.archive.overview(refresh)[0]["after_visit"], 1)

    def test_overview_links_filter_complete_authors_and_tags(self):
        article = read_work_page(work_page())
        tag = "角色 A & B"
        article.fields.update({"作者": ("织字人", "Quill, Jr."), "角色标签": (tag,)})
        self.archive.save_capture("101", {"title": "合著作品", "downloaded_at": "2026-01-01"}, article, {})
        self.archive.save_status("102", {"title": "同作者旧作", "author": "织字人, 另一位",
                                         "freeforms": [tag]})
        self.archive.save_status("103", {"title": "另一个作者", "author": "织字人二号",
                                         "freeforms": [tag + " 的同人"]})
        reader, _ = self.archive.reader("")
        listing = overview_html(self.archive, reader, "all", "", "title")
        self.assertIn('/works/101/latest?view=all&amp;q=&amp;sort=title', listing)
        self.assertIn(escape(home_url("all", sort="title", author="Quill, Jr.")), listing)
        self.assertIn(escape(home_url("all", sort="title", tag=tag)), listing)
        for filters in ({"author": "织字人"}, {"tag": tag}):
            filtered = overview_html(self.archive, reader, "", "", "title", **filters)
            self.assertIn("合著作品", filtered)
            self.assertIn("同作者旧作", filtered)
            self.assertNotIn("另一个作者", filtered)
            self.assertIn(escape(home_url("unread", sort="title", **filters)), filtered)
        self.assertIn("没有匹配的作品。", overview_html(self.archive, reader, "all", "", "title", tag="角色 A"))

    def test_failed_overview_renders_with_queued_backup(self):
        reader, _ = self.archive.reader("")
        self.assertTrue(self.archive.queue_backup("retry"))
        empty = overview_html(self.archive, reader, "failed", "", "event")
        self.assertIn("没有抓取问题。", empty)
        self.assertIn("排队中 · 重试失败或缺失项", empty)
        self.scan(100, self.old)
        self.archive.save_status("102", {"title": "等待抓取的作品", "error_count": 1})
        failed = overview_html(self.archive, reader, "failed", "", "event")
        self.assertIn("等待抓取的作品", failed)
        self.assertIn("全文抓取失败", failed)
        self.assertNotIn("示例作品", failed)
        self.assertIn("排队中 · 重试失败或缺失项", failed)

    def test_opened_article_marks_only_updates_through_displayed_version(self):
        self.scan(100, self.old)
        reader, _ = self.archive.reader("", visit=True)
        self.scan(200, self.new)
        opened, _, _ = self.archive.read_latest_article("101")
        opened_event = self.archive.overview(reader)[0]["last_event"]
        self.scan(300, self.old, page=work_page(body="更新的正文"))

        self.archive.mark_article_read(reader, "101", opened.id)
        row = self.archive.overview(reader)[0]
        self.assertEqual(row["read_through"], opened_event)
        self.assertGreater(row["last_event"], opened_event)

        latest, _, _ = self.archive.read_latest_article("101")
        self.archive.mark_article_read(reader, "101", latest.id)
        self.archive.mark_article_read(reader, "101", opened.id)
        row = self.archive.overview(reader)[0]
        self.assertEqual(row["last_event"], row["read_through"])
        self.assertEqual(row["after_visit"], 2)

    def test_latest_article_uses_saved_body_and_reports_fallback(self):
        self.scan(100, self.old)
        first, _, fallback = self.archive.read_latest_article("101")
        self.assertFalse(fallback)
        with self.archive.write() as db:
            self.archive._add_version(db, "101", 150, None, "legacy", {}, {}, parse_error="缺少章节")
        version, body, fallback = self.archive.read_latest_article("101")
        self.assertEqual(version.id, first.id)
        self.assertTrue(fallback)
        self.assertIn("留存版", article_html("101", version, body, fallback=fallback))
        self.scan(200, self.new)
        version, body, fallback = self.archive.read_latest_article("101")
        self.assertGreater(version.id, first.id)
        self.assertFalse(fallback)
        self.assertIn("&lt;附注&gt;", article_html("101", version, body))
        self.assertIsNone(self.archive.read_latest_article("999"))

    def test_import_is_idempotent_and_online_backup_restores(self):
        source = self.out / "file-archive"
        (source / "works").mkdir(parents=True)
        directory = source / "versions" / "101"
        directory.mkdir(parents=True)
        (directory / "100.html").write_bytes(self.old)
        (source / "works" / "current.html").write_bytes(self.new)
        state = {
            "user": "review",
            "works": {
                "101": {"file": "current.html", "updated_at": 200, "title": "示例作品"},
                "102": {"title": "下载失败", "error_count": 3, "last_error": "HTTP 404"},
            },
        }
        state_file = source / "state.json"
        state_file.write_text(json.dumps(state), encoding="utf-8")
        result = import_legacy(self.archive, source)
        self.assertEqual((result.works, result.files, result.missing), (2, 2, ()))
        self.assertTrue(import_legacy(self.archive, source).already_imported)
        record, versions, events, seen = self.detail()
        self.assertEqual(record["updated_at"], 200)
        self.assertEqual(len(versions), len(events))
        self.assertGreaterEqual(seen, events[0]["id"])
        old, new = versions.values()
        self.assertIsNone(old.captured_at)
        self.assertEqual(self.archive.read_file("101", old.id, "html")[1], self.old)

        restored = Archive(self.out / "restored")
        self.archive.backup(restored.path)
        self.assertEqual(restored.read_file("101", new.id, "html")[1], self.new)
        self.assertEqual(restored.scan_state("review")["works"]["102"]["error_count"], 3)
        self.assertEqual(state_file.read_text(encoding="utf-8"), json.dumps(state))
        self.assertEqual((directory / "100.html").read_bytes(), self.old)

    def test_download_menu_and_export_filename(self):
        self.scan(100, self.old)
        reader, _ = self.archive.reader("")
        _, detail = work_html(self.archive, reader, "101", {})
        self.assertIn('class="download-menu"', detail)
        self.assertIn("Markdown (.md)", detail)
        self.assertIn("JSON (.json)", detail)
        self.assertIn("原始 HTML (.html)", detail)
        version, article, _ = self.archive.read_latest_article("101")
        self.assertIn("html", version.files)
        reader_page = article_html("101", version, article)
        self.assertIn('class="download-menu"', reader_page)
        self.assertIn("原始 HTML (.html)", reader_page)
        header = download_filename("101", 1, "潮汐/笔记", "md")
        self.assertIn('filename="ao3-101-1.md"', header)
        self.assertIn("filename*=UTF-8''101_%E6%BD%AE%E6%B1%90_%E7%AC%94%E8%AE%B0.md", header)

    def test_overview_search_id_filters_sort_pagination_and_seen(self):
        reader, _ = self.archive.reader("", visit=True)
        many_tags = tuple(f"标签{i}" for i in range(13))
        article = read_work_page(work_page(tags=many_tags, count="1/?"))
        self.archive.save_capture("101", {
            "title": "连载星海", "author": "甲", "chapters": "1/?", "words": 12000,
            "summary": "海港的来信。",
            "downloaded_at": "2026-02-01T10:00:00", "bookmark_tags": ["必看"], "bookmark_notes": "很好看",
        }, article, {})
        self.archive.save_status("102", {"title": "完结短篇", "author": "乙", "chapters": "1/1", "words": 500,
                                         "error_count": 1, "last_error": "HTTP 404"})
        reader, _ = self.archive.reader(reader["id"], visit=True)

        by_id = overview_html(self.archive, reader, "all", "101", "words")
        self.assertIn("连载星海", by_id)
        self.assertNotIn("完结短篇", by_id)
        self.assertIn("+ 展开其余 3 个标签", by_id)
        self.assertIn("12,000 字", by_id)
        self.assertIn("书签备注", by_id)
        self.assertIn('class="work-body has-about"', by_id)
        self.assertIn('class="card-summary-wrap is-collapsed"', by_id)
        self.assertIn("展开简介", by_id)

        noted_only = overview_html(self.archive, reader, "all", "", "event", wip=True, noted=True)
        self.assertIn("连载星海", noted_only)
        self.assertNotIn("完结短篇", noted_only)

        failed_page = overview_html(self.archive, reader, "failed", "", "event")
        self.assertIn('href="/works/102?view=failed', failed_page)
        self.assertNotIn('/works/102/latest', failed_page)

        since_page = overview_html(self.archive, reader, "since", "", "event")
        self.assertIn("标为已查看", since_page)
        self.assertTrue(self.archive.mark_overview_seen(reader["id"]))
        reader, _ = self.archive.reader(reader["id"], visit=True)
        self.assertEqual(self.archive.overview(reader)[0]["after_visit"], 0)

        for idx in range(200, 200 + PER_PAGE + 2):
            self.archive.save_status(str(idx), {"title": f"作品{idx}", "words": idx})
        page1 = overview_html(self.archive, reader, "all", "", "words", page_num=1)
        page2 = overview_html(self.archive, reader, "all", "", "words", page_num=2)
        self.assertIn('aria-label="作品分页"', page1)
        self.assertIn("第 2/2 页", page2)

    def test_long_paragraph_diff_and_reader_controls(self):
        long_old = "甲" * 1000 + "旧片段" + "乙" * 1000
        long_new = "甲" * 1000 + "新内容" + "乙" * 1000
        diff_html = render_diff(Difference("第 1 章", "正文", (long_old,), (long_new,)))
        self.assertIn("<mark>旧片段</mark>", diff_html)
        self.assertIn("<mark>新内容</mark>", diff_html)
        self.assertIn("…", diff_html)

        self.scan(100, self.old)
        self.scan(200, self.new)
        _, versions, _, _ = self.detail()
        old_ver, new_ver = versions.values()
        _, old_article = self.archive.read_article("101", old_ver.id)
        ctx = self.archive.version_info("101", old_ver.id)
        page_html = article_html("101", old_ver, old_article, version_ctx=ctx)
        self.assertIn("正文阅读", page_html)
        self.assertIn("正在阅读历史存档（第 1 / 2 版）", page_html)
        self.assertIn('id="reader-font"', page_html)
        self.assertIn('id="reader-size"', page_html)
        self.assertIn('id="reader-width"', page_html)
        for theme_id in ("sage", "lavender", "crimson", "dusk", "midnight"):
            self.assertIn(f'value="{theme_id}"', page_html)
        self.assertNotIn("章节目录 · 1 章", page_html)

        two_ch = read_work_page(work_page(single=True, count="1/1"))
        two_ch.chapters.append(two_ch.chapters[0])
        multi_html = article_html("101", new_ver, two_ch)
        self.assertIn("章节目录 · 2 章", multi_html)
        self.assertIn("下一章 →", multi_html)

    def test_retry_works_preserves_bookmark_notes_and_tags(self):
        cfg = replace(self.cfg, formats=("html",))
        self.scan(100, self.old, cfg, note="保留的书签备注", tags=("保留标签",))
        state = self.archive.scan_state(cfg.bookmark_user)
        state["works"]["101"]["bookmark_tags"] = ["收藏夹标签"]
        self.archive.save_status("101", state["works"]["101"])
        blurb = {"object_id": 101, "title": "示例作品", "updated_at": 100}
        stats = backup.run_stats()
        client = PageClient(bookmark(101, "示例作品", 100), self.old, work_page())
        backup._archive_work(cfg, client, self.archive, state["works"], blurb, threading.Event(), stats, retry=True)
        saved = self.archive.scan_state(cfg.bookmark_user)["works"]["101"]
        self.assertEqual(saved["bookmark_notes"], "保留的书签备注")
        self.assertEqual(saved["bookmark_tags"], ["收藏夹标签"])

    def test_schedule_modes_validation_and_next_runs(self):
        tz = timezone(timedelta(hours=8))
        now = datetime(2026, 2, 28, 19, 20, tzinfo=tz)

        fixed_6h = submitted_settings({
            "schedule_mode": ["interval"],
            "interval_value": ["6"],
            "interval_unit": ["hour"],
            "anchor_mode": ["fixed"],
            "run_at": ["03:30"],
            "delay": ["1.5"],
            "retries": ["4"],
            "max_errors": ["5"],
        })
        self.assertEqual(
            compute_next_runs(fixed_6h, count=3, now=now),
            [
                datetime(2026, 2, 28, 21, 30, tzinfo=tz),
                datetime(2026, 3, 1, 3, 30, tzinfo=tz),
                datetime(2026, 3, 1, 9, 30, tzinfo=tz),
            ],
        )

        after_30m = submitted_settings({
            "schedule_mode": ["interval"],
            "interval_value": ["30"],
            "interval_unit": ["minute"],
            "anchor_mode": ["after"],
            "run_at": ["03:30"],
            "delay": ["1.5"],
            "retries": ["4"],
            "max_errors": ["5"],
        })
        self.assertEqual(compute_next_runs(after_30m, count=1, now=now)[0], now + timedelta(minutes=30))

        weekly = submitted_settings({
            "schedule_mode": ["weekly"],
            "weekdays": ["0", "5"],
            "run_at": ["04:15"],
            "delay": ["1.5"],
            "retries": ["4"],
            "max_errors": ["5"],
        })
        self.assertEqual(weekly.weekdays, (0, 5))
        self.assertEqual(
            compute_next_runs(weekly, count=3, now=now),
            [
                datetime(2026, 3, 2, 4, 15, tzinfo=tz),
                datetime(2026, 3, 7, 4, 15, tzinfo=tz),
                datetime(2026, 3, 9, 4, 15, tzinfo=tz),
            ],
        )

        monthly_eom = submitted_settings({
            "schedule_mode": ["monthly"],
            "month_day": ["31"],
            "run_at": ["02:00"],
            "delay": ["1.5"],
            "retries": ["4"],
            "max_errors": ["5"],
        })
        self.assertEqual(
            compute_next_runs(monthly_eom, count=3, now=datetime(2026, 2, 28, 1, 0, tzinfo=tz)),
            [
                datetime(2026, 2, 28, 2, 0, tzinfo=tz),
                datetime(2026, 3, 31, 2, 0, tzinfo=tz),
                datetime(2026, 4, 30, 2, 0, tzinfo=tz),
            ],
        )

        manual = submitted_settings({
            "schedule_mode": ["manual"],
            "run_at": ["03:30"],
            "delay": ["1.5"],
            "retries": ["4"],
            "max_errors": ["5"],
        })
        self.assertEqual(compute_next_runs(manual, count=3, now=now), [])
        self.assertIn("仅手动", schedule_label(manual))
        self.assertIn("已关闭自动定时", backup_status({}, [], None, manual)["upcoming"])

        self.archive.set_backup_settings(weekly)
        reader, _ = self.archive.reader("1234", visit=True)
        html = overview_html(self.archive, reader, "all", "", "event")
        self.assertIn('name="schedule_mode" value="manual"', html)
        self.assertIn('id="backup-interval-unit"', html)
        self.assertIn('id="backup-schedule-preview"', html)

        with self.assertRaisesRegex(ValueError, "至少选择一个星期"):
            submitted_settings({
                "schedule_mode": ["weekly"],
                "run_at": ["03:30"],
                "delay": ["1.5"],
                "retries": ["4"],
                "max_errors": ["5"],
            })

    def test_scheduler_respects_manual_mode_and_queued_jobs(self):
        manual = BackupSettings(schedule_mode="manual", run_at="03:30")
        self.archive.set_backup_settings(manual)
        self.archive.queue_backup("scan")
        cfg = replace(self.cfg, once=False, run_on_start=True)
        stop = threading.Event()
        calls: list[str] = []

        def fake_pass(pass_cfg, action):
            calls.append(action)
            threading.Timer(0.05, stop.set).start()
            return True, backup.run_stats()

        with patch.object(backup, "BACKUP_POLL_SECONDS", 0.01):
            backup.run_scheduler(cfg, self.archive, stop, fake_pass)

        self.assertEqual(calls, ["scan"])
        scheduler, saved, _, last = self.archive.backup_dashboard()
        self.assertEqual(saved, manual)
        self.assertEqual(scheduler["next_run"], 0)
        self.assertEqual((last["action"], last["source"], last["status"]), ("scan", "manual", "success"))


if __name__ == "__main__":
    unittest.main()
