import json
import re
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import ao3_backup as backup


def bookmark(wid, title, stamp, *, words=10, chapters="1/?",
             summary="海港的来信。", tags=("悬疑",), note=""):
    labels = "".join(f'<a class="tag">{tag}</a>' for tag in tags)
    return (
        f'<li id="bookmark_{wid}" class="bookmark blurb work-{wid}">'
        f'<h4 class="heading"><a href="/works/{wid}">{title}</a> '
        '<a rel="author">织字人</a></h4>'
        '<h5 class="fandoms heading"><a class="tag">原创作品</a></h5>'
        f'<!-- updated_at={stamp} -->'
        f'<dd class="words">{words}</dd><dd class="chapters">{chapters}</dd>'
        f'<li class="freeforms">{labels}</li>'
        f'<blockquote class="userstuff summary">{summary}</blockquote>'
        '<div class="user module group">'
        f'<blockquote class="userstuff notes">{note}</blockquote></div></li>'
    )


def export(*chapters, notes=""):
    body = "".join(
        f'<div class="meta group"><h2 class="heading">{title}</h2></div>'
        f'<div class="userstuff">{paragraphs}</div>'
        for title, paragraphs in chapters
    )
    return (
        '<html><head><title>备份测试</title></head>'
        f'<body><div id="preface">{notes}</div><div id="chapters">{body}</div></body></html>'
    ).encode()


class FixtureClient:
    logged_in = False

    def __init__(self, blurbs, works):
        self.blurbs = blurbs
        self.works = works
        self.requests = []

    def has_credentials(self):
        return False

    def get_text(self, path):
        return "<ol>" + "".join(self.blurbs) + "</ol>"

    def get_bytes(self, path):
        self.requests.append(path)
        if ".epub?" in path:
            return b"PK\x03\x04"
        return self.works[int(path.split("/")[2])]


class BackupHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cfg = backup.Config("review", Path(directory.name))
        self.state = backup.load_state(self.cfg, backup.log)
        self.old_html = export(("Chapter 1: 渡口", "<p>下了一夜雨。</p>"))
        self.new_html = export(
            ("Chapter 1: 渡口", "<p>落了一夜雪。</p>"),
            ("Chapter 2: 归帆", "<p>帆船在涨潮时回来。</p>"),
        )

    def scan(self, stamp, html, *, cfg=None, **metadata):
        client = FixtureClient([bookmark(101, "潮汐笔记", stamp, **metadata)], {101: html})
        stats, ok = backup.run_once(cfg or self.cfg, client, self.state, threading.Event())
        self.assertTrue(ok)
        return stats, client.requests

    def publish(self):
        backup.save_state(self.cfg, self.state)
        backup.write_index(self.cfg, self.state)
        backup.prune_retired_files(self.cfg, self.state)
        self.state = backup.load_state(self.cfg, backup.log)

    def test_updates_survive_empty_runs_and_keep_diffs(self):
        self.scan(100, self.old_html)
        self.publish()
        previous = self.cfg.works_dir / self.state["works"]["101"]["file"]
        self.scan(200, self.new_html, words=25, chapters="2/?")
        self.assertTrue(previous.is_file())
        self.publish()
        self.assertFalse(previous.exists())
        self.scan(200, self.new_html, words=25, chapters="2/?")
        self.publish()

        rec = self.state["works"]["101"]
        self.assertNotIn("flag", rec)
        self.assertEqual(self.state["change_seq"], 2)
        self.assertEqual(rec["history"][-1]["changes"]["chapters"], ["1/?", "2/?"])
        self.assertEqual((self.cfg.works_dir / rec["file"]).read_bytes(), self.new_html)
        history = self.cfg.versions_dir / "101"
        self.assertEqual(next(history.glob("100-*.html")).read_bytes(), self.old_html)
        report = next(history.glob("compare-*.html")).read_text(encoding="utf-8")
        self.assertIn("<mark>雨</mark>", report)
        self.assertIn("<mark>雪</mark>", report)
        report = re.sub(r"</?mark>", "", report)
        for text in ("下了一夜雨", "落了一夜雪", "归帆", "新增章节"):
            self.assertIn(text, report)
        self.assertIn("data-change='2'", (self.cfg.out_dir / "index.html").read_text(encoding="utf-8"))

    def test_same_revision_format_and_bookmark_edits_do_not_count(self):
        self.scan(100, self.old_html)
        cfg = replace(self.cfg, formats=("html", "epub"))
        _, requests = self.scan(100, self.old_html, cfg=cfg, note="个人备注")
        self.assertEqual(len(requests), 1)
        self.assertIn(".epub?", requests[0])
        self.assertEqual(self.state["change_seq"], 1)
        self.assertEqual(self.state["works"]["101"]["bookmark_notes"], "个人备注")

        _, requests = self.scan(100, self.old_html, cfg=cfg, summary="", tags=())
        self.assertEqual(requests, [])
        event = self.state["works"]["101"]["history"][-1]
        self.assertEqual(event["kind"], "metadata")
        self.assertEqual(event["changes"]["freeforms"], (["悬疑"], []))
        self.assertEqual(event["changes"]["summary"], ("海港的来信。", ""))

    def test_overview_retains_content_update_when_its_metadata_changes_later(self):
        self.scan(100, self.old_html)
        self.scan(200, self.new_html)
        self.scan(200, self.new_html, summary="补充了简介。")
        self.publish()
        index = (self.cfg.out_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn("data-change='3' data-added='1' data-updated='2'", index)

    def test_failed_archive_write_preserves_published_backup_and_allows_retry(self):
        self.scan(100, self.old_html)
        self.publish()
        original = self.state["works"]["101"]["file"]
        write = backup.atomic_write

        def fail_archive(path, data):
            if isinstance(data, Path):
                raise OSError("simulated archive failure")
            write(path, data)

        with patch.object(backup, "atomic_write", fail_archive):
            stats, _ = self.scan(200, self.new_html, words=25, chapters="2/?")
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(self.state["change_seq"], 1)
        self.assertEqual(list(self.cfg.works_dir.iterdir()), [self.cfg.works_dir / original])
        self.assertEqual((self.cfg.works_dir / original).read_bytes(), self.old_html)
        self.scan(200, self.new_html, words=25, chapters="2/?")
        self.publish()
        self.assertEqual(self.state["change_seq"], 2)

    def test_legacy_archives_are_linked_without_inventing_changes(self):
        current_name = "101_潮汐笔记.html"
        backup.atomic_write(self.cfg.works_dir / current_name, self.new_html)
        archive = self.cfg.versions_dir / "101" / "100.html"
        backup.atomic_write(archive, self.old_html)
        backup.atomic_write(self.cfg.state_file, json.dumps({
            "version": 2, "works": {"101": {"file": current_name, "updated_at": 200}},
        }))
        self.state = backup.load_state(self.cfg, backup.log)
        self.publish()
        history = (archive.parent / "history.html").read_text(encoding="utf-8")
        self.assertIn("href='100.html'", history)
        self.assertIn("compare-100-current.html", history)
        self.assertEqual(archive.read_bytes(), self.old_html)
        self.assertNotIn("history", self.state["works"]["101"])

    def test_retry_after_unpublished_update_keeps_revision_order(self):
        self.scan(100, self.old_html)
        self.publish()
        old_name = self.state["works"]["101"]["file"]
        self.scan(200, self.new_html)
        self.state = backup.load_state(self.cfg, backup.log)
        self.assertEqual(self.state["works"]["101"]["file"], old_name)
        self.assertEqual((self.cfg.works_dir / old_name).read_bytes(), self.old_html)
        self.scan(200, self.new_html)
        self.publish()
        self.scan(150, self.old_html)
        self.publish()
        rec = self.state["works"]["101"]
        self.assertEqual([event["seq"] for event in rec["history"]], [1, 2, 3])
        directory = self.cfg.versions_dir / "101"
        self.assertEqual(len(list(directory.glob("1[0-9]0-*.html"))), 1)
        self.assertEqual(len(list(directory.glob("200-*.html"))), 1)
        history = (directory / "history.html").read_text(encoding="utf-8")
        self.assertEqual(history.count("查看正文差异"), 2)

    def test_keep_versions_off_keeps_metadata_without_copying_old_text(self):
        self.scan(100, self.old_html)
        self.scan(200, self.new_html, cfg=replace(self.cfg, keep_versions=False), words=25)
        self.publish()
        history_dir = self.cfg.versions_dir / "101"
        self.assertEqual([p.name for p in history_dir.iterdir()], ["history.html"])
        history = (history_dir / "history.html").read_text(encoding="utf-8")
        self.assertIn("版本文件未保留", history)
        self.assertIn("字数", history)
        self.assertNotIn("下了一夜雨", history)

    def test_single_chapter_comparison_ignores_preface_and_escapes_text(self):
        before, after = backup.ChapterParser(), backup.ChapterParser()
        template = '<div id="chapters"><h2 class="toc-heading">标题</h2><div class="userstuff">{}</div></div>'
        before.feed(template.format("<p>岸边的灯。</p>"))
        after.feed("<div id='preface'>忽略这段</div>" + template.format("<p>&lt;晚潮&gt;</p>"))
        report = backup.chapter_diff(before, after)
        self.assertIn("岸边的灯", re.sub(r"</?mark>", "", report))
        self.assertIn("&lt;晚潮&gt;", re.sub(r"</?mark>", "", report))
        self.assertNotIn("忽略这段", report)


if __name__ == "__main__":
    unittest.main()
