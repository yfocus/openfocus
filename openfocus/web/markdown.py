# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import html
import re
from urllib.parse import urlparse

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_FENCE_RE = re.compile(r"^\s*```")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_UL_RE = re.compile(r"^\s*[-+*]\s+(.+?)\s*$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)")
_SAFE_SCHEMES = {"http", "https", "mailto"}


def _safe_href(value: str) -> str | None:
    href = html.unescape(str(value or "")).strip()
    if not href or any(ord(ch) < 32 for ch in href):
        return None
    parsed = urlparse(href)
    if parsed.scheme:
        if parsed.scheme.lower() not in _SAFE_SCHEMES:
            return None
    elif not href.startswith(("/", "#")):
        return None
    return html.escape(href, quote=True)


def _render_inline(text: str) -> str:
    escaped = html.escape(str(text or ""), quote=True)
    code_spans: list[str] = []

    def stash_code(match: re.Match[str]) -> str:
        code_spans.append(f"<code>{match.group(1)}</code>")
        return f"\x00CODE{len(code_spans) - 1}\x00"

    escaped = _CODE_SPAN_RE.sub(stash_code, escaped)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"__([^_\n]+)__", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<em>\1</em>", escaped)

    def render_link(match: re.Match[str]) -> str:
        label = match.group(1)
        href = _safe_href(match.group(2))
        if href is None:
            return label
        return f'<a href="{href}" rel="nofollow noopener noreferrer">{label}</a>'

    escaped = _LINK_RE.sub(render_link, escaped)
    for idx, code in enumerate(code_spans):
        escaped = escaped.replace(f"\x00CODE{idx}\x00", code)
    return escaped


def _paragraph(lines: list[str]) -> str:
    rendered = "<br>".join(_render_inline(line.strip()) for line in lines)
    return f"<p>{rendered}</p>"


def render_dashboard_markdown(value: str | None) -> Markup:
    """Render Goal/Task Basic Markdown without allowing raw HTML injection."""

    text = str(value or "").strip()
    if not text:
        return Markup("&mdash;")

    html_parts: list[str] = []
    paragraph: list[str] = []
    list_kind: str | None = None
    in_fence = False
    fence_lines: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            html_parts.append(_paragraph(paragraph))
            paragraph = []

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            html_parts.append(f"</{list_kind}>")
            list_kind = None

    def start_list(kind: str) -> None:
        nonlocal list_kind
        if list_kind != kind:
            close_list()
            html_parts.append(f"<{kind}>")
            list_kind = kind

    for raw_line in text.splitlines():
        if _FENCE_RE.match(raw_line):
            if in_fence:
                html_parts.append(
                    "<pre><code>"
                    + html.escape("\n".join(fence_lines), quote=False)
                    + "</code></pre>"
                )
                fence_lines = []
                in_fence = False
            else:
                flush_paragraph()
                close_list()
                in_fence = True
            continue

        if in_fence:
            fence_lines.append(raw_line)
            continue

        if not raw_line.strip():
            flush_paragraph()
            close_list()
            continue

        heading = _HEADING_RE.match(raw_line)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            html_parts.append(f"<h{level}>{_render_inline(heading.group(2))}</h{level}>")
            continue

        unordered = _UL_RE.match(raw_line)
        if unordered:
            flush_paragraph()
            start_list("ul")
            html_parts.append(f"<li>{_render_inline(unordered.group(1))}</li>")
            continue

        ordered = _OL_RE.match(raw_line)
        if ordered:
            flush_paragraph()
            start_list("ol")
            html_parts.append(f"<li>{_render_inline(ordered.group(1))}</li>")
            continue

        close_list()
        paragraph.append(raw_line)

    if in_fence:
        html_parts.append(
            "<pre><code>" + html.escape("\n".join(fence_lines), quote=False) + "</code></pre>"
        )
    flush_paragraph()
    close_list()
    return Markup("\n".join(html_parts))


def register_dashboard_markdown_filter(templates: Jinja2Templates) -> None:
    templates.env.filters["dashboard_markdown"] = render_dashboard_markdown
