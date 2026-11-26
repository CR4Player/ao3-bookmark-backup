"""Isolated legacy static-file archive, HTML diff, and index renderer (Layer 3)."""

from __future__ import annotations

import contextlib
import csv
import html as html_mod
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from html.parser import HTMLParser
from itertools import zip_longest
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote
from uuid import uuid4

from ao3_db import DOWNLOAD_FORMATS, REVISION_NAME, work_files
from ao3_diff import TAG_FIELDS, WORK_FIELDS
from ao3_http import BASE, atomic_write


log = logging.getLogger("ao3")

CHANGE_LABELS = {"new": "新增备份", "updated": "作品更新", "metadata": "作品资料变更"}
PARAGRAPH_TAGS = {
    "p", "div", "section", "li", "blockquote", "pre", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6", "tr",
}


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


def load_state(cfg: Any, log_: logging.Logger) -> dict[str, Any]:
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


def save_state(
    cfg: Any,
    state: dict[str, Any],
    *,
    writer: Callable[[Path, bytes | str | Path], None] | None = None,
) -> None:
    if cfg.dry_run:
        return
    write_fn = writer or atomic_write
    state["version"] = 3
    state.setdefault("library_id", uuid4().hex)
    state["user"] = cfg.bookmark_user
    state["last_run"] = now_iso()
    write_fn(
        cfg.state_file, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    )


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


def revision_key(state: dict[str, Any], stamp: int | None, seq: int) -> str:
    library_id = state.setdefault("library_id", uuid4().hex)
    return f"{stamp or 'old'}-{library_id[:8]}-{seq}"


def prune_retired_files(
    cfg: Any,
    state: dict[str, Any],
    *,
    writer: Callable[[Path, bytes | str | Path], None] | None = None,
) -> None:
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
        save_state(cfg, state, writer=writer)


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
                parts.append(
                    f"<p>章名 <del>{html_mod.escape(old_title)}</del> → "
                    f"<ins>{html_mod.escape(new_title)}</ins></p>"
                )
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
    *, writer: Callable[[Path, bytes | str | Path], None] | None = None,
) -> bool:
    write_fn = writer or atomic_write
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
        write_fn(report, history_document(
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


def write_history(
    cfg: Any, wid: str, rec: dict[str, Any],
    *, writer: Callable[[Path, bytes | str | Path], None] | None = None,
) -> str:
    if not re.fullmatch(r"[0-9]+", wid):
        return ""
    write_fn = writer or atomic_write
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
            if compare_versions(directory, archives.get(older, {}), files, name, writer=write_fn):
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
    write_fn(directory / "history.html", history_document(title, body))
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


def write_index(
    cfg: Any,
    state: dict[str, Any],
    *,
    writer: Callable[[Path, bytes | str | Path], None] | None = None,
) -> None:
    if cfg.dry_run:
        return
    write_fn = writer or atomic_write
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
        history_href = write_history(cfg, wid, rec, writer=write_fn)
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
        words_size = (
            f"{human_words(words)}<br><span style='font-size:11px;opacity:.75'>{size_str}</span>"
            if words
            else size_str
        )

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
    write_fn(cfg.out_dir / "index.html", "".join(parts))


def write_manifest(cfg: Any, state: dict[str, Any]) -> None:
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
