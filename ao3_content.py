"""Serializable article fragments and their reading and export renderers."""

from __future__ import annotations

import re
from html import escape
from typing import Any
from urllib.parse import quote, urljoin, urlsplit


Node = str | dict[str, Any]
Fragment = list[Node]
BLOCKS = {"p", "blockquote", "pre", "ul", "ol", "li", "hr", "div",
          "h1", "h2", "h3", "h4", "h5", "h6"}
INLINE = {"a", "strong", "em", "del", "u", "sub", "sup", "code", "br", "img", "span"}
ALIASES = {"b": "strong", "i": "em", "s": "del", "strike": "del", "section": "div"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}
HIDDEN = {"script", "style", "template", "iframe", "object"}
AO3 = "https://archiveofourown.org"


def safe_url(value: str) -> str:
    value = value.strip()
    if not value or "\\" in value or any(ord(char) < 32 for char in value):
        return ""
    if value.startswith("#"):
        return value
    try:
        url = urljoin(AO3 + "/", value)
        parts = urlsplit(url)
        return url if parts.scheme in {"http", "https"} and parts.hostname else ""
    except ValueError:
        return ""


def element(tag: str, attrs: dict[str, str | None]) -> dict[str, Any] | None:
    tag = ALIASES.get(tag, tag)
    anchor = attrs.get("id") or (attrs.get("name") if tag == "a" else "") or ""
    if tag not in BLOCKS | INLINE or (tag == "span" and not anchor):
        return None
    node: dict[str, Any] = {"tag": tag, "children": []}
    if anchor:
        node["id"] = anchor
    if tag in {"a", "img"}:
        if url := safe_url(attrs.get("href" if tag == "a" else "src") or ""):
            node["url"] = url
        if tag == "img":
            node["alt"] = attrs.get("alt") or ""
    if tag == "ol" and re.fullmatch(r"-?[0-9]{1,8}", attrs.get("start") or ""):
        start = int(attrs["start"])
        if start != 1:
            node["start"] = start
    return node


def normalize(nodes: Fragment, *, trim: bool = True, pre: bool = False) -> Fragment:
    """Discard indentation at block boundaries and preserve spaces in prose."""
    result: Fragment = []
    for node in nodes:
        if isinstance(node, str):
            if result and isinstance(result[-1], str):
                result[-1] += node
            else:
                result.append(node)
        else:
            tag = node["tag"]
            node["children"] = normalize(node["children"], trim=tag in BLOCKS, pre=pre or tag == "pre")
            result.append(node)
    if pre:
        return result
    for i, node in enumerate(result):
        if not isinstance(node, str):
            continue
        value = re.sub(r"[ \t\n\r\f]+", " ", node)
        previous = result[i - 1] if i else None
        following = result[i + 1] if i + 1 < len(result) else None
        if (trim and previous is None) or (isinstance(previous, dict) and previous["tag"] in BLOCKS):
            value = value.lstrip(" ")
        if (trim and following is None) or (isinstance(following, dict) and following["tag"] in BLOCKS):
            value = value.rstrip(" ")
        result[i] = value
    return [node for node in result if node != ""]


def paragraphs(nodes: Fragment) -> tuple[str, ...]:
    texts: list[str] = []
    parts: list[str] = []

    def flush() -> None:
        if text := " ".join("".join(parts).split()):
            texts.append(text)
        parts.clear()

    def visit(fragment: Fragment) -> None:
        for node in fragment:
            if isinstance(node, str):
                parts.append(node)
                continue
            tag = node["tag"]
            boundary = tag in BLOCKS or tag == "br"
            if boundary:
                flush()
            if tag == "img":
                parts.append(f" [图片 {node['alt'] or node.get('url', '')}] ")
            visit(node["children"])
            if boundary:
                flush()

    visit(nodes)
    flush()
    return tuple(texts)


def links(nodes: Fragment) -> tuple[str, ...]:
    return tuple(text for node in nodes if isinstance(node, dict)
                 for text in ((" ".join(paragraphs(node["children"])),) if node["tag"] == "a"
                              else links(node["children"])) if text)


def reading_html(nodes: Fragment) -> str:
    result = []
    for node in nodes:
        if isinstance(node, str):
            result.append(escape(node))
            continue
        tag = node["tag"]
        body = reading_html(node["children"])
        anchor = f' id="{escape(node["id"])}"' if node.get("id") else ""
        url = safe_url(node.get("url", ""))
        if tag == "img":
            text = escape(node["alt"] or "插图")
            result.append(f'<a{anchor} href="{escape(url)}" rel="noreferrer">{text} ↗</a>' if url else text)
        elif tag == "a":
            result.append(f'<a{anchor} href="{escape(url)}" rel="noreferrer">{body}</a>'
                          if url else f"<span{anchor}>{body}</span>")
        elif tag in {"hr", "br"}:
            result.append(f"<{tag}{anchor}>")
        elif tag in BLOCKS | INLINE:
            start = f' start="{int(node["start"])}"' if tag == "ol" and "start" in node else ""
            result.append(f"<{tag}{anchor}{start}>{body}</{tag}>")
        else:
            result.append(body)
    return "".join(result)


def markdown_text(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>|~#])", r"\\\1", value)


def markdown(nodes: Fragment, *, literal: bool = False) -> str:
    result = []
    for node in nodes:
        if isinstance(node, str):
            result.append(node if literal else markdown_text(node))
            continue
        tag = node["tag"]
        body = markdown(node["children"], literal=literal or tag in {"pre", "code"})
        url = quote(safe_url(node.get("url", "")), safe=":/#?&=+@!$'*,;%~_-.[ ]".replace(" ", ""))
        if tag in {"a", "img"}:
            label = body if tag == "a" else markdown_text(node["alt"] or "插图")
            result.append(f"[{label}](<{url}>)" if url else label)
        elif tag == "blockquote":
            result.append("\n\n" + "\n".join("> " + line for line in body.strip().splitlines()) + "\n\n")
        elif tag in {"ol", "ul"}:
            items = [item for item in node["children"] if isinstance(item, dict) and item["tag"] == "li"]
            lines = []
            for i, item in enumerate(items, node.get("start", 1)):
                prefix = f"{i}. " if tag == "ol" else "- "
                text = markdown(item["children"]).strip().replace("\n", "\n" + " " * len(prefix))
                lines.append(prefix + text)
            result.append("\n\n" + "\n".join(lines) + "\n\n" if lines else body)
        elif tag == "pre":
            fence = "`" * (1 + max((len(run) for run in re.findall(r"`+", body)), default=2))
            result.append(f"\n\n{fence}\n{body.rstrip(chr(10))}\n{fence}\n\n")
        elif tag == "code":
            fence = "`" * (1 + max((len(run) for run in re.findall(r"`+", body)), default=0))
            pad = " " if body.startswith("`") or body.endswith("`") else ""
            result.append(f"{fence}{pad}{body}{pad}{fence}")
        elif tag in {"em", "strong", "del"}:
            mark = {"em": "*", "strong": "**", "del": "~~"}[tag]
            result.append(f"{mark}{body}{mark}" if body else "")
        elif tag in {"sub", "sup", "u"}:
            result.append(f"<{tag}>{body}</{tag}>")
        elif tag == "br":
            result.append("  \n")
        elif tag == "hr":
            result.append("\n\n---\n\n")
        elif tag in BLOCKS:
            heading = "#" * int(tag[1]) + " " if tag.startswith("h") else ""
            result.append(f"\n\n{heading}{body.strip()}\n\n")
        else:
            result.append(body)
    return "".join(result)
