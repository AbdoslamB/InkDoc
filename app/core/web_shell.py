"""Web pages that build their content with JavaScript ("app shells").

InkDoc fetches a page once and runs none of its scripts. A page that draws
everything in the browser arrives as an empty shell: its body shows no text,
only a <noscript> notice such as "You need to enable JavaScript to run this
app." Many of these pages still carry their content for search engines, as
schema.org JSON-LD (a JobPosting, an Article, a Product, an Event).

inspect_page() looks at a fetched HTML page before any engine sees it:

- a page with any visible text is left alone. The scan stops at the first
  word, so a normal page costs next to nothing;
- a shell that carries JSON-LD content gets a small HTML page rebuilt from it,
  which the engine converts instead (ShellPage, with a note for the response);
- a shell without, whose <noscript> asks for JavaScript, raises
  JavaScriptPageError, which says what to do, instead of the conversion saving
  a file that holds only that notice.

Anything else (no text and no such notice, a page that is only a picture, say)
goes to the engine as before. Hosts that MarkItDown reads from their raw HTML
(YouTube, Wikipedia, Bing) are never touched, nor is a page over
MAX_PAGE_BYTES or one without a <body> tag.

Pure: standard library only, no network, nothing on the page is run. Never
raises anything but JavaScriptPageError: a bug here leaves the page as fetched.
"""
from __future__ import annotations

import codecs
import html
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.core.converter import UserFacingConversionError

logger = logging.getLogger(__name__)

# Shells are small (the example that prompted this is 68 KB); a page larger
# than this is not inspected at all.
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_JSON_LD_CHARS = 2 * 1024 * 1024  # one <script type="application/ld+json">
MAX_JSON_LD_BLOCKS = 32
# The least text a JSON-LD body must hold to stand in for the page.
MIN_BODY_CHARS = 200
# A <noscript> longer than this is content of its own, which the engine keeps.
MAX_NOSCRIPT_WORDS = 40
# MarkItDown has converters that read these sites' raw HTML themselves.
SKIP_HOSTS = ("youtube.com", "youtu.be", "wikipedia.org", "bing.com")

JAVASCRIPT_PAGE_MESSAGE = (
    "This page builds its content with JavaScript, which InkDoc doesn't run, and it "
    "includes no copy InkDoc can read. Open the page in your browser and print it to "
    "PDF (or save it as \"Webpage, Complete\"), then drop that file into InkDoc."
)


class JavaScriptPageError(UserFacingConversionError):
    """The page shows nothing without JavaScript and carries no readable copy."""


@dataclass
class ShellPage:
    """The HTML rebuilt from a shell's JSON-LD, which the engine converts instead."""

    html: bytes
    kind: str   # the schema.org type it came from, e.g. "JobPosting"
    note: str   # one sentence for the response's warnings


# ─── Is it a shell? ──────────────────────────────────────────────────────────

_BODY = re.compile(r"<body\b", re.I)
_TOKENS = re.compile(
    r"<!--.*?(?:-->|\Z)"
    r"|<(script|style|noscript|template)\b[^>]*>(.*?)(?:</\1\s*>|\Z)"
    r"|<[a-zA-Z/!?][^>]*>?"
    r"|[^<]+|<",
    re.S | re.I,
)
_VISIBLE = re.compile(r"[^\W_]")
_TAG = re.compile(r"<[^>]*>")


def _strip_tags(fragment: str) -> str:
    return html.unescape(_TAG.sub(" ", fragment))


def _scan_body(page: str) -> tuple[bool, list[str]] | None:
    """(the body shows any text, the texts of its <noscript>s); None without a <body> tag."""
    start = _BODY.search(page)
    if start is None:
        return None
    noscripts: list[str] = []
    for token in _TOKENS.finditer(page, start.start()):
        hidden = token.group(1)
        if hidden:
            if hidden.lower() == "noscript":
                noscripts.append(_strip_tags(token.group(2) or ""))
            continue
        text = token.group(0)
        if len(text) > 1 and text.startswith("<"):
            continue  # a tag or a comment
        if _VISIBLE.search(html.unescape(text)):
            return True, noscripts
    return False, noscripts


def _decode(raw: bytes, charset: str | None) -> str:
    name = charset
    if not name:
        meta = re.search(rb"<meta[^>]+charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", raw[:4096], re.I)
        name = meta.group(1).decode("ascii") if meta else "utf-8"
    try:
        codecs.lookup(name)
    except LookupError:
        name = "utf-8"
    return raw.decode(name, errors="replace")


# ─── What the page carries for search engines ────────────────────────────────

_JSON_LD = re.compile(
    r"<script\b[^>]*?\btype\s*=\s*([\"']?)application/ld\+json\1[^>]*>(.*?)</script\s*>",
    re.S | re.I,
)
_ARTICLE_TYPES = {
    "Article", "NewsArticle", "BlogPosting", "TechArticle", "ScholarlyArticle", "Report",
    "AnalysisNewsArticle", "OpinionNewsArticle", "ReportageNewsArticle", "LiveBlogPosting",
}
_PRODUCT_TYPES = {"Product", "ProductGroup"}


def _json_ld_blocks(page: str) -> Iterator[Any]:
    for count, match in enumerate(_JSON_LD.finditer(page)):
        if count >= MAX_JSON_LD_BLOCKS:
            return
        text = match.group(2).strip()
        if not text or len(text) > MAX_JSON_LD_CHARS:
            continue
        text = re.sub(r"^\s*(?:<!--|//\s*<!\[CDATA\[)", "", text)
        text = re.sub(r"(?:-->|//\s*\]\]>)\s*$", "", text)
        try:
            yield json.loads(text)
        except (ValueError, RecursionError):
            continue


def _nodes(data: Any, depth: int = 0) -> Iterator[dict]:
    """Every typed object in a JSON-LD value: top level, @graph and mainEntity."""
    if depth > 6:
        return
    if isinstance(data, list):
        for entry in data[:200]:
            yield from _nodes(entry, depth + 1)
    elif isinstance(data, dict):
        if "@type" in data:
            yield data
        for key in ("@graph", "mainEntity"):
            if isinstance(data.get(key), (dict, list)):
                yield from _nodes(data[key], depth + 1)


def _types(node: dict) -> set[str]:
    value = node.get("@type")
    values = value if isinstance(value, list) else [value]
    return {v.rsplit("/", 1)[-1] for v in values if isinstance(v, str)}


def _text(value: Any) -> str:
    """A JSON-LD value as one line of plain text."""
    if isinstance(value, str):
        return " ".join(_strip_tags(value).split())
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return f"{value:g}" if isinstance(value, float) else str(value)
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("@value") or "")
    if isinstance(value, list):
        return ", ".join(dict.fromkeys(t for t in (_text(v) for v in value[:10]) if t))
    return ""


def _date(value: Any) -> str:
    text = _text(value)
    match = re.match(r"\d{4}-\d{2}-\d{2}", text)
    return match.group(0) if match else text


def _place(value: Any) -> str:
    places = []
    for entry in (value if isinstance(value, list) else [value])[:10]:
        if isinstance(entry, str):
            places.append(_text(entry))
            continue
        if not isinstance(entry, dict):
            continue
        address = entry.get("address", entry)
        if isinstance(address, dict):
            parts = [_text(address.get(k)) for k in
                     ("streetAddress", "addressLocality", "addressRegion", "addressCountry")]
            where = ", ".join(p for p in parts if p)
        else:
            where = _text(address)
        name = _text(entry.get("name")) if entry is not address else ""
        places.append(", ".join(p for p in (name, where) if p))
    return "; ".join(dict.fromkeys(p for p in places if p))


def _salary(value: Any) -> str:
    if not isinstance(value, dict):
        return _text(value)
    currency = _text(value.get("currency"))
    amount_value = value.get("value")
    unit = ""
    if isinstance(amount_value, dict):
        unit = _text(amount_value.get("unitText"))
        amount = _text(amount_value.get("value")) or "–".join(
            t for t in (_text(amount_value.get("minValue")), _text(amount_value.get("maxValue"))) if t
        )
    else:
        amount = _text(amount_value)
    if not amount:
        return ""
    return " ".join(p for p in (currency, amount, f"per {unit.lower()}" if unit else "") if p)


def _offer(value: Any) -> tuple[str, str]:
    """(price, availability) of a Product's offers."""
    offer = value[0] if isinstance(value, list) and value else value
    if not isinstance(offer, dict):
        return "", ""
    currency = _text(offer.get("priceCurrency"))
    price = _text(offer.get("price")) or "–".join(
        t for t in (_text(offer.get("lowPrice")), _text(offer.get("highPrice"))) if t
    )
    availability = _text(offer.get("availability")).rsplit("/", 1)[-1]
    availability = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", availability).capitalize()
    return (" ".join(p for p in (currency, price) if p) if price else ""), availability


_HTML_HINT = re.compile(r"<(?:p|br|div|ul|ol|li|h[1-6]|strong|em|b|i|a|table|span|section)\b", re.I)
_NOT_CONTENT = re.compile(
    r"<(script|style|iframe|object|embed|form|template)\b.*?(?:</\1\s*>|\Z)", re.S | re.I
)


def _body(value: Any) -> tuple[str, int]:
    """(an HTML fragment, how much text it holds) for a description or articleBody."""
    text = value if isinstance(value, str) else ""
    if not text.strip():
        return "", 0
    if "<" not in text and "&lt;" in text:
        text = html.unescape(text)  # HTML escaped once more for the JSON
    if _HTML_HINT.search(text):
        fragment = _NOT_CONTENT.sub("", text)
        return fragment, len(" ".join(_strip_tags(fragment).split()))
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    fragment = "".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paragraphs)
    return fragment, len(" ".join(text.split()))


def _readable(node: dict) -> tuple[str, str, list[tuple[str, str]], Any] | None:
    """(label, title, facts, body value) for the types InkDoc can stand in for a page."""
    types = _types(node)
    if "JobPosting" in types:
        employment = _text(node.get("employmentType")).replace("_", " ").capitalize()
        remote = "Yes" if "TELECOMMUTE" in _text(node.get("jobLocationType")).upper() else ""
        return "job posting", _text(node.get("title") or node.get("name")), [
            ("Company", _text(node.get("hiringOrganization"))),
            ("Location", _place(node.get("jobLocation"))),
            ("Remote", remote),
            ("Employment type", employment),
            ("Salary", _salary(node.get("baseSalary"))),
            ("Posted", _date(node.get("datePosted"))),
            ("Apply by", _date(node.get("validThrough"))),
        ], node.get("description")
    if types & _ARTICLE_TYPES:
        body = node.get("articleBody") or node.get("text") or node.get("description")
        return "article", _text(node.get("headline") or node.get("name")), [
            ("Author", _text(node.get("author"))),
            ("Published", _date(node.get("datePublished"))),
            ("Updated", _date(node.get("dateModified"))),
            ("Publisher", _text(node.get("publisher"))),
        ], body
    if types & _PRODUCT_TYPES:
        price, availability = _offer(node.get("offers"))
        return "product details", _text(node.get("name")), [
            ("Brand", _text(node.get("brand"))),
            ("SKU", _text(node.get("sku"))),
            ("Price", price),
            ("Availability", availability),
        ], node.get("description")
    if any(t == "Event" or t.endswith("Event") for t in types):
        return "event details", _text(node.get("name")), [
            ("Starts", _text(node.get("startDate"))),
            ("Ends", _text(node.get("endDate"))),
            ("Location", _place(node.get("location"))),
            ("Organizer", _text(node.get("organizer"))),
        ], node.get("description")
    return None


def _rebuilt_page(title: str, facts: list[tuple[str, str]], body: str) -> bytes:
    parts = ['<!DOCTYPE html><html><head><meta charset="utf-8">']
    if title:
        parts.append(f"<title>{html.escape(title)}</title>")
    parts.append("</head><body>")
    if title:
        parts.append(f"<h1>{html.escape(title)}</h1>")
    rows = [(k, v) for k, v in facts if v]
    if rows:
        parts.append("<ul>" + "".join(
            f"<li><strong>{html.escape(k)}:</strong> {html.escape(v)}</li>" for k, v in rows
        ) + "</ul>")
    parts.append(body)
    parts.append("</body></html>")
    return "".join(parts).encode("utf-8")


def _from_json_ld(page: str) -> ShellPage | None:
    """The page's main JSON-LD object as HTML: the readable one holding the most text."""
    best: tuple[int, str, str, str, list[tuple[str, str]], str] | None = None
    for data in _json_ld_blocks(page):
        for node in _nodes(data):
            readable = _readable(node)
            if readable is None:
                continue
            label, title, facts, body_value = readable
            fragment, size = _body(body_value)
            if size >= MIN_BODY_CHARS and (best is None or size > best[0]):
                kind = sorted(_types(node))[0]
                best = (size, kind, label, title, facts, fragment)
    if best is None:
        return None
    _size, kind, label, title, facts, fragment = best
    return ShellPage(
        html=_rebuilt_page(title, facts, fragment),
        kind=kind,
        note=(
            "This page builds its content with JavaScript, which InkDoc doesn't run, so InkDoc "
            f"converted the {label} the page includes for search engines. Parts of the page that "
            "aren't in it, such as buttons and forms, are left out."
        ),
    )


# ─── Entry point ─────────────────────────────────────────────────────────────

def inspect_page(path: str | Path, *, url: str, charset: str | None = None) -> ShellPage | None:
    """What to convert for the fetched HTML page at `path` (see the module docstring).

    None: convert the page as fetched. A ShellPage: convert its HTML instead.
    Raises JavaScriptPageError for a shell that can't be read without JavaScript.
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
            return None
        with open(path, "rb") as f:
            raw = f.read(MAX_PAGE_BYTES + 1)
        if len(raw) > MAX_PAGE_BYTES:
            return None
        page = _decode(raw, charset)
        scan = _scan_body(page)
        if scan is None or scan[0]:
            return None
        rebuilt = _from_json_ld(page)
        if rebuilt is not None:
            return rebuilt
        notice = " ".join(" ".join(scan[1]).split())
        if re.search(r"javascript", notice, re.I) and len(notice.split()) <= MAX_NOSCRIPT_WORDS:
            raise JavaScriptPageError(JAVASCRIPT_PAGE_MESSAGE)
        return None
    except JavaScriptPageError:
        raise
    except Exception as exc:  # a bug here must never cost the conversion
        logger.debug("Web page check skipped: %s", exc)
        return None
