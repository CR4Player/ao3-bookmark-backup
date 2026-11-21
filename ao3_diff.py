"""Extract structured articles from AO3 pages and exports and compare saved versions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html.parser import HTMLParser
from itertools import zip_longest
from typing import Any

from ao3_content import (
    HIDDEN, VOID, Fragment, element, links, markdown, markdown_text, normalize, paragraphs,
)


WORK_FIELDS = {
    "title": "标题", "author": "作者", "chapters": "章节", "words": "字数",
    "summary": "作品简介", "fandoms": "原作", "relationships": "关系标签",
    "freeforms": "附加标签",
}
TAG_FIELDS = {"fandoms", "relationships", "freeforms"}
EXPORT_FIELDS = {
    "rating": "分级", "archive warning": "警告", "archive warnings": "警告",
    "category": "分类", "fandom": "原作", "fandoms": "原作",
    "relationship": "关系标签", "relationships": "关系标签",
    "character": "角色标签", "characters": "角色标签", "additional tags": "附加标签",
    "language": "语言", "series": "系列",
}
RICH_FIELDS = {"作品简介", "作品首注", "作品尾注"}
PAGE_TAG_FIELDS = {
    "rating": "分级", "warning": "警告", "archive_warning": "警告",
    "category": "分类", "fandom": "原作", "relationship": "关系标签",
    "character": "角色标签", "freeform": "附加标签", "language": "语言", "series": "系列",
}


def metadata_changes(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, tuple[Any, Any]]:
    changes = {}
    for key in WORK_FIELDS:
        if any(WORK_FIELDS[key] in record.get("hidden_fields", ()) for record in (before, after)):
            continue
        old, new = before.get(key), after.get(key)
        if key in TAG_FIELDS:
            old, new = old or [], new or []
            if key == "freeforms" and len(old) == 15 and not before.get("all_freeforms"):
                new = new[:15]
            removed = [tag for tag in old if tag not in new]
            added = [tag for tag in new if tag not in old]
            if removed or added:
                changes[key] = (removed, added)
        elif (old or "") != (new or ""):
            changes[key] = (old or "", new or "")
    return changes


@dataclass
class Chapter:
    title: str = ""
    body: Fragment = field(default_factory=list)
    summary: Fragment = field(default_factory=list)
    start_notes: Fragment = field(default_factory=list)
    end_notes: Fragment = field(default_factory=list)


@dataclass
class Export:
    fields: dict[str, tuple[str, ...]] = field(default_factory=dict)
    sections: dict[str, Fragment] = field(default_factory=dict)
    chapters: list[Chapter] = field(default_factory=list)
    hidden_fields: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Export:
        return cls({key: tuple(values) for key, values in data["fields"].items()},
                   data["sections"], [Chapter(**chapter) for chapter in data["chapters"]],
                   data.get("hidden_fields", []))

    def values(self, metadata: dict[str, Any]) -> dict[str, tuple[str, ...]]:
        values = self.fields.copy()
        for key, label in WORK_FIELDS.items():
            if label in values or label in self.sections or not (value := metadata.get(key)):
                continue
            values[label] = tuple(value) if isinstance(value, list) else (str(value),)
        return values

    def to_markdown(self, metadata: dict[str, Any]) -> str:
        values = self.values(metadata)
        title = " / ".join(values.pop("标题", ())) or "AO3"
        author = "、".join(values.pop("作者", ()))
        output = [f"# {markdown_text(title)}"]
        if author:
            output.append(markdown_text(author))
        output.extend(f"**{label}** · {markdown_text('、'.join(values[label]))}"
                      for label in values if label != "作品简介")

        def add(label: str, nodes: Fragment, level: str = "##") -> None:
            if nodes:
                output.append(f"{level} {label}\n\n{markdown(nodes).strip()}")

        add("作品简介", self.sections.get("作品简介", list(values.get("作品简介", ()))))
        add("作品首注", self.sections.get("作品首注", []))
        for index, chapter in enumerate(self.chapters, 1):
            output.append(f"## {markdown_text(chapter_heading(chapter, index))}")
            add("章节简介", chapter.summary, "###")
            add("章首作者附注", chapter.start_notes, "###")
            output.append(markdown(chapter.body).strip())
            add("章末作者附注", chapter.end_notes, "###")
        add("作品尾注", self.sections.get("作品尾注", []))
        return "\n\n".join(output) + "\n"


@dataclass
class Capture:
    field: str
    depth: int
    chapter: Chapter | None
    nodes: Fragment = field(default_factory=list)


@dataclass
class Frame:
    tag: str
    id: str
    classes: set[str]
    target: Fragment | None = None


class ExportParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.export = Export()
        self.stack: list[Frame] = []
        self.capture: Capture | None = None
        self.chapter: Chapter | None = None
        self.caption = ""
        self.tag_name = ""
        self.chapters_closed = False

    def start_capture(self, name: str, chapter: Chapter | None = None) -> None:
        self.capture = Capture(name, len(self.stack), chapter)
        self.stack[-1].target = self.capture.nodes

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        id_ = attr.get("id") or ""
        classes = set((attr.get("class") or "").split())
        frame = Frame(tag, id_, classes)
        if self.capture is not None:
            frame.id = ""
            target = self.stack[-1].target
            title_image = tag == "img" and self.capture.field == "标题"
            if target is not None and tag not in HIDDEN and not title_image and self.keep_element(tag, attr, classes):
                node = element(tag, attr)
                if node is not None:
                    target.append(node)
                    target = node["children"]
                frame.target = target
            if tag not in VOID:
                self.stack.append(frame)
            return

        if tag in VOID:
            return
        self.stack.append(frame)
        self.open_section(frame)

    def keep_element(self, tag: str, attrs: dict[str, str | None], classes: set[str]) -> bool:
        return True

    def open_section(self, frame: Frame) -> None:
        tag, classes = frame.tag, frame.classes
        ancestors = self.stack[:-1]
        zone = next((item.id for item in ancestors if item.id in {
            "preface", "chapters", "afterword",
        }), "")
        if zone == "chapters":
            meta = any("meta" in item.classes for item in ancestors)
            if tag == "h2" and "heading" in classes and "userstuff" not in classes:
                self.chapter = Chapter()
                self.export.chapters.append(self.chapter)
                self.caption = ""
                self.start_capture("title", self.chapter)
            elif tag == "div" and "userstuff" in classes and not meta:
                if self.chapter is None:
                    self.chapter = Chapter()
                    self.export.chapters.append(self.chapter)
                self.start_capture("body", self.chapter)
            elif tag in {"p", "h3"} and meta:
                self.start_capture("_caption")
            elif tag == "blockquote" and "userstuff" in classes and self.chapter:
                endnotes = any(item.id.startswith(("endnotes", "chapter_endnotes")) for item in ancestors)
                name = ("end_notes" if endnotes or "end note" in self.caption else
                        "summary" if "summary" in self.caption or "summary" in classes else
                        "start_notes")
                self.start_capture(name, self.chapter)
        elif zone == "preface":
            if tag == "h1":
                self.start_capture("标题")
            elif tag == "div" and "byline" in classes:
                self.start_capture("作者")
            elif tag in {"p", "h3"} and "message" not in classes:
                self.start_capture("_caption")
            elif tag == "dt":
                self.start_capture("_tag")
            elif tag == "dd":
                if label := EXPORT_FIELDS.get(self.tag_name):
                    self.start_capture(label)
                elif self.tag_name == "stats":
                    self.start_capture("_stats")
            elif tag == "blockquote" and "userstuff" in classes:
                name = "作品首注" if "note" in self.caption or "notes" in classes else "作品简介"
                self.start_capture(name)
        elif zone == "afterword" and tag == "blockquote" and "userstuff" in classes:
            self.start_capture("作品尾注")

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                break
        else:
            return
        capture = self.capture
        if capture and index < capture.depth:
            nodes = normalize(capture.nodes)
            name = capture.field
            if capture.chapter:
                if name == "title":
                    capture.chapter.title = " ".join(paragraphs(nodes))
                else:
                    getattr(capture.chapter, name).extend(nodes)
            elif name in RICH_FIELDS:
                self.export.sections.setdefault(name, []).extend(nodes)
            elif name == "_caption":
                self.caption = " ".join(paragraphs(nodes)).casefold()
            elif name == "_tag":
                self.tag_name = " ".join(paragraphs(nodes)).rstrip(":").casefold()
            elif name == "_stats":
                stats = " ".join(paragraphs(nodes))
                for label, pattern in (("字数", r"Words:\s*([\d,]+)"),
                                       ("章节", r"Chapters:\s*([\d,]+/(?:[\d,]+|\?))")):
                    if match := re.search(pattern, stats, re.I):
                        self.export.fields[label] = (match[1].replace(",", ""),)
            else:
                values = self.field_values(name, nodes)
                if values:
                    self.export.fields[name] = self.export.fields.get(name, ()) + values
            self.capture = None
        if self.stack[index].id == "chapters":
            self.chapters_closed = True
        self.close_section(self.stack[index])
        del self.stack[index:]

    def close_section(self, frame: Frame) -> None:
        pass

    def field_values(self, name: str, nodes: Fragment) -> tuple[str, ...]:
        values = (links(nodes) if name != "标题" else ()) or paragraphs(nodes)
        if name == "作者" and values == ("by Anonymous",):
            return ("Anonymous",)
        return values

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.capture is not None and (target := self.stack[-1].target) is not None:
            target.append(data)


class WorkPageParser(ExportParser):
    """Read the public-facing work and chapter modules outside comments and navigation."""

    def __init__(self) -> None:
        super().__init__()
        self.positions: list[int] = []
        self.work_closed = False

    def open_section(self, frame: Frame) -> None:
        if len(self.stack) < 2:
            return
        tag, classes = frame.tag, frame.classes
        ancestors = self.stack[:-1]
        parent = ancestors[-1]
        if not any(node.id == "main" for node in ancestors):
            return

        if any(node.tag == "dl" and {"work", "meta"} <= node.classes for node in ancestors):
            if tag == "dd":
                if "words" in classes:
                    self.start_capture("字数")
                elif "chapters" in classes:
                    self.start_capture("章节")
                else:
                    for key, label in PAGE_TAG_FIELDS.items():
                        if key in classes or key + "s" in classes:
                            self.start_capture(label)
                            break
            return

        skin = next((i for i, node in enumerate(ancestors) if node.id == "workskin"), None)
        if skin is None:
            return
        path = ancestors[skin + 1:]
        if not path:
            return
        if path[0].id == "chapters":
            if len(path) == 1:
                position = re.fullmatch(r"chapter-(\d+)", frame.id)
                if tag == "div" and position and "chapter" in classes and "draft" not in classes:
                    self.positions.append(int(position[1]))
                    self.chapter = Chapter()
                    self.export.chapters.append(self.chapter)
                elif tag == "div" and "userstuff" in classes:
                    self.chapter = Chapter()
                    self.export.chapters.append(self.chapter)
                    self.start_capture("body", self.chapter)
                return
            if "draft" in path[1].classes or not self.chapter or not re.fullmatch(r"chapter-\d+", path[1].id):
                return
            if len(path) == 2 and tag == "div" and "userstuff" in classes:
                self.start_capture("body", self.chapter)
            elif len(path) == 3 and tag == "h3" and "title" in classes and "preface" in parent.classes:
                self.start_capture("title", self.chapter)
            elif tag == "blockquote" and "userstuff" in classes and len(path) == 4:
                for key, name in (("end", "end_notes"), ("summary", "summary"), ("notes", "start_notes")):
                    if key in parent.classes:
                        self.start_capture(name, self.chapter)
                        break
        elif "preface" in path[0].classes and "afterword" not in path[0].classes:
            if len(path) == 1 and tag == "h2" and "title" in classes:
                self.start_capture("标题")
            elif len(path) == 1 and tag == "h3" and "byline" in classes:
                self.start_capture("作者")
            elif len(path) == 2 and tag == "blockquote" and "userstuff" in classes:
                if "summary" in parent.classes:
                    self.start_capture("作品简介")
                elif "notes" in parent.classes:
                    self.start_capture("作品首注")
        elif (len(path) == 2 and "afterword" in path[0].classes and parent.id == "work_endnotes"
              and tag == "blockquote" and "userstuff" in classes):
            self.start_capture("作品尾注")

    def keep_element(self, tag: str, attrs: dict[str, str | None], classes: set[str]) -> bool:
        capture = self.capture
        if capture.field == "body" and tag == "h3" and len(self.stack) == capture.depth:
            return attrs.get("id") != "work" or "landmark" not in classes
        if capture.field == "系列" and tag == "a":
            return not ({"previous", "next"} & classes)
        if capture.field in {"警告", "附加标签"} and tag == "li":
            if re.fullmatch(r"work_\d+_category_(warnings|freeforms)", attrs.get("id") or ""):
                if capture.field not in self.export.hidden_fields:
                    self.export.hidden_fields.append(capture.field)
                return False
        return True

    def close_section(self, frame: Frame) -> None:
        if frame.id == "workskin":
            self.work_closed = True

    def field_values(self, name: str, nodes: Fragment) -> tuple[str, ...]:
        if name == "标题":
            return (" ".join(paragraphs(nodes)),)
        if name in {"字数", "章节"}:
            return (re.sub(r"[,\s]", "", "".join(paragraphs(nodes))),)
        return super().field_values(name, nodes)


def read_work_page(raw: bytes) -> Export:
    parser = WorkPageParser()
    parser.feed(raw.decode("utf-8-sig"))
    parser.close()
    article = parser.export
    if not (parser.work_closed and parser.chapters_closed):
        if re.search(rb"<title>\s*Mystery Work\s*\|", raw, re.I):
            raise ValueError("AO3 将该作品标为 Mystery Work，正文尚未公开")
        raise ValueError("作品页缺少完整正文，可能需要登录或页面结构已变化")
    if not all(len(article.fields.get(label, ())) == 1 and article.fields[label][0]
               for label in ("标题", "字数", "章节")) or not article.fields.get("作者"):
        raise ValueError("作品页缺少标题、作者或作品统计")
    count = re.fullmatch(r"([0-9]+)/([0-9]+|\?)", article.fields["章节"][0])
    if not count or not re.fullmatch(r"[0-9]+", article.fields["字数"][0]):
        raise ValueError("作品页的章节数或字数无法识别")
    if (not article.chapters or len(article.chapters) != int(count[1])
            or (parser.positions and (len(parser.positions) != len(article.chapters)
                                      or parser.positions != sorted(set(parser.positions))))
            or any(not paragraphs(ch.body) for ch in article.chapters)):
        raise ValueError("作品页章节数或正文不完整")
    return article


def read_export(raw: bytes) -> Export:
    parser = ExportParser()
    parser.feed(raw.decode("utf-8-sig"))
    parser.close()
    chapters = parser.export.chapters
    count = parser.export.fields.get("章节", ("0",))[0].split("/")[0]
    if not parser.chapters_closed or not chapters or len(chapters) < int(count):
        raise ValueError("HTML 里缺少完整的章节")
    if any(not paragraphs(chapter.body) for chapter in chapters):
        raise ValueError("至少一章没有提取到正文")
    return parser.export


@dataclass(frozen=True)
class Difference:
    section: str
    label: str
    before: tuple[str, ...]
    after: tuple[str, ...]

    @property
    def heading(self) -> str:
        verb = "新增" if not self.before else "删除" if not self.after else "修改"
        return f"{self.section} · {verb}{self.label}"


def chapter_title(title: str) -> str:
    return re.sub(r"^chapter\s+\d+\s*(?::\s*|$)", "", title, flags=re.I)


def chapter_heading(chapter: Chapter, index: int) -> str:
    title = chapter_title(chapter.title)
    return f"第 {index} 章 · {title}" if title else f"第 {index} 章"


def compare_fragments(a: Fragment, b: Fragment) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if a == b:
        return (), ()
    before, after = paragraphs(a), paragraphs(b)
    if before == after:
        before, after = (tuple(markdown(nodes).strip().split("\n\n")) if nodes else ()
                         for nodes in (a, b))
    return before, after


def compare_chapters(before: Export, after: Export) -> list[Difference]:
    old, new = before.chapters, after.chapters

    def keys(chapters: list[Chapter]) -> list[tuple[str, tuple[str, ...]]]:
        return [(chapter_title(ch.title), () if chapter_title(ch.title) else paragraphs(ch.body))
                for ch in chapters]

    changes = []
    for _, i, j, k, m in SequenceMatcher(None, keys(old), keys(new), autojunk=False).get_opcodes():
        for offset, (a, b) in enumerate(zip_longest(old[i:j], new[k:m])):
            index = k + offset + 1 if b is not None else i + offset + 1
            section = f"第 {index} 章"
            if a is None or b is None:
                def contents(chapter: Chapter | None) -> tuple[str, ...]:
                    if chapter is None:
                        return ()
                    return (chapter.title or section,) + sum(
                        (paragraphs(nodes) for nodes in
                         (chapter.summary, chapter.start_notes, chapter.body, chapter.end_notes)), ())

                changes.append(Difference(section, "章节", contents(a), contents(b)))
                continue
            fields = [("章名", (chapter_title(a.title),), (chapter_title(b.title),))]
            fields.extend((label, *compare_fragments(getattr(a, name), getattr(b, name)))
                          for label, name in (("章节简介", "summary"), ("章首作者附注", "start_notes"),
                                              ("正文", "body"), ("章末作者附注", "end_notes")))
            for label, previous, current in fields:
                if previous != current:
                    changes.append(Difference(section, label, previous, current))
    return changes


def field_differences(changes: dict[str, tuple[Any, Any]]) -> list[Difference]:
    def paragraphs(value: Any) -> tuple[str, ...]:
        if isinstance(value, (list, tuple)):
            return tuple(str(item) for item in value)
        return (str(value),) if value != "" and value is not None else ()

    return [Difference("作品资料", WORK_FIELDS.get(key, key), paragraphs(a), paragraphs(b))
            for key, (a, b) in changes.items()]


def compare_snapshots(
    old: Export | None, new: Export | None,
    before: dict[str, Any], after: dict[str, Any],
) -> list[Difference]:
    if old is None or new is None:
        return field_differences(metadata_changes(before, after)) if before and after else []
    old_fields, new_fields = old.values(before), new.values(after)
    article_fields = []
    hidden = set(old.hidden_fields) | set(new.hidden_fields)
    for label in dict.fromkeys([*old_fields, *new_fields]):
        if label in RICH_FIELDS or label in hidden:
            continue
        previous, current = old_fields.get(label, ()), new_fields.get(label, ())
        if label == "章节":
            previous, current = (tuple(re.sub(r"[,\s]", "", x) for x in values)
                                 for values in (previous, current))
        if label in EXPORT_FIELDS.values() and set(previous) == set(current):
            continue
        if previous != current:
            article_fields.append(Difference("作品资料", label, previous, current))
    for label in ("作品简介", "作品首注", "作品尾注"):
        previous, current = compare_fragments(old.sections.get(label, list(old_fields.get(label, ()))),
                                              new.sections.get(label, list(new_fields.get(label, ()))))
        if previous != current:
            article_fields.append(Difference("作品资料", label, previous, current))
    return (compare_chapters(old, new) if old.chapters != new.chapters else []) + article_fields


def compare_exports(
    old_html: bytes | None, new_html: bytes | None,
    before: dict[str, Any], after: dict[str, Any],
) -> list[Difference]:
    return compare_snapshots(read_export(old_html) if old_html else None,
                             read_export(new_html) if new_html else None, before, after)
