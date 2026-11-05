"""Web pages that build their content with JavaScript (app/core/web_shell.py).

A page that only shows "You need to enable JavaScript to run this app." used to
convert to that one line, and Auto-save saved it. Such a page is now converted
from the copy it carries for search engines (JSON-LD), or refused with a
sentence saying what to do. Every other page must come out exactly as before.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import web_shell as ws
from app.core.converter import UserFacingConversionError

NOTICE = "<noscript>You need to enable JavaScript to run this app.</noscript>"
DESCRIPTION = (
    "<h2><strong>About us</strong></h2><p>We build software for private markets, "
    "and we are hiring a business systems analyst to own our lead-to-cash process.</p>"
    "<ul><li>Own Salesforce and NetSuite integrations</li><li>Work with finance and sales</li></ul>"
    "<p>You have five years of experience with quote-to-cash systems and enjoy clear writing.</p>"
)


def job_posting(**extra) -> dict:
    return {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": "Lead Business Systems Analyst",
        "hiringOrganization": {"@type": "Organization", "name": "Juniper Square"},
        "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "addressCountry": "United States"}},
        "jobLocationType": "TELECOMMUTE",
        "employmentType": "FULL_TIME",
        "datePosted": "2026-10-15T00:00:00.000Z",
        "description": DESCRIPTION,
        **extra,
    }


def _json(block) -> str:
    # As pages write it: "</" escaped, so a "</script>" in a value can't end the block.
    return json.dumps(block).replace("</", r"<\/")


def shell(*json_ld, body: str = NOTICE + '<div id="root"></div><script src="/app.js"></script>') -> str:
    blocks = "".join(
        f'<script type="application/ld+json">{block if isinstance(block, str) else _json(block)}</script>'
        for block in json_ld
    )
    return f"<!DOCTYPE html><html><head><title>Jobs</title>{blocks}</head><body>{body}</body></html>"


def inspect(tmp_path: Path, page: str, url: str = "https://jobs.example.com/acme/123", charset: str | None = "utf-8"):
    path = tmp_path / "page.html"
    path.write_text(page, encoding="utf-8")
    return ws.inspect_page(path, url=url, charset=charset)


def to_markdown(html_bytes: bytes) -> str:
    import io

    from markitdown import MarkItDown, StreamInfo

    info = StreamInfo(extension=".html", mimetype="text/html", charset="utf-8")
    return MarkItDown().convert_stream(io.BytesIO(html_bytes), stream_info=info).text_content


# ─── Pages that must stay exactly as they are ────────────────────────────────

@pytest.mark.parametrize("page", [
    "<html><body><h1>Hello</h1><p>World</p></body></html>",
    # A notice and real content: the content is converted, notice and all, as before.
    f"<html><body>{NOTICE}<article><p>The full story.</p></article></body></html>",
    # Text before the scripts, text after them, text in a table cell or a link.
    "<html><body><script>var a = '<p>x</p>';</script><div><a href='/'>Home</a></div></body></html>",
    "<html><body><table><tr><td>42</td></tr></table></body></html>",
    # A page that is only a picture: nothing to say about it, the engine decides.
    "<html><body><img src='cat.png' alt='A cat'></body></html>",
    # Empty, with no notice: unchanged (the page already reports "no text").
    '<html><body><div id="root"></div><script src="/app.js"></script></body></html>',
    # No <body> tag at all: never guessed at.
    "<html><head><title>x</title></head>" + NOTICE,
    # A long <noscript> is content of its own.
    "<html><body><noscript>" + "JavaScript is off, so here is the whole article. " * 20 + "</noscript></body></html>",
])
def test_ordinary_pages_are_left_alone(tmp_path, page):
    assert inspect(tmp_path, page) is None


def test_a_normal_page_with_json_ld_is_left_alone(tmp_path):
    page = shell(job_posting(), body="<h1>Lead Business Systems Analyst</h1><p>Apply below.</p>")
    assert inspect(tmp_path, page) is None


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=abc", "https://youtu.be/abc",
    "https://en.wikipedia.org/wiki/Markdown", "https://www.bing.com/search?q=x",
])
def test_sites_markitdown_reads_itself_are_never_touched(tmp_path, url):
    assert inspect(tmp_path, shell(job_posting()), url=url) is None
    assert inspect(tmp_path, shell(), url=url) is None


def test_a_page_over_the_size_limit_is_not_inspected(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "MAX_PAGE_BYTES", 200)
    assert inspect(tmp_path, shell(job_posting())) is None


def test_the_check_is_cheap_on_a_normal_page(tmp_path):
    page = ("<html><head><script>" + "var a=1;" * 400_000 + "</script></head><body><p>"
            + "word " * 150_000 + "</p></body></html>")
    path = tmp_path / "page.html"
    path.write_text(page, encoding="utf-8")
    ws.inspect_page(path, url="https://example.com/")  # the first read of a new file can wait on an antivirus scan
    start = time.perf_counter()
    assert ws.inspect_page(path, url="https://example.com/") is None
    assert time.perf_counter() - start < 0.5


# ─── Shells with a copy for search engines ───────────────────────────────────

def test_a_job_posting_shell_is_rebuilt_from_its_json_ld(tmp_path):
    rebuilt = inspect(tmp_path, shell(job_posting()))
    assert isinstance(rebuilt, ws.ShellPage)
    assert rebuilt.kind == "JobPosting"
    assert "job posting" in rebuilt.note and "JavaScript" in rebuilt.note
    md = to_markdown(rebuilt.html)
    assert md.startswith("# Lead Business Systems Analyst")
    for line in ("**Company:** Juniper Square", "**Location:** United States", "**Remote:** Yes",
                 "**Employment type:** Full time", "**Posted:** 2026-10-15"):
        assert line in md
    assert "About us" in md and "* Own Salesforce and NetSuite integrations" in md
    assert "enable JavaScript" not in md


def test_salary_and_closing_date_when_the_posting_has_them(tmp_path):
    posting = job_posting(
        baseSalary={"@type": "MonetaryAmount", "currency": "USD",
                    "value": {"@type": "QuantitativeValue", "minValue": 150000, "maxValue": 180000, "unitText": "YEAR"}},
        validThrough="2026-12-01",
    )
    md = to_markdown(inspect(tmp_path, shell(posting)).html)
    assert "**Salary:** USD 150000–180000 per year" in md
    assert "**Apply by:** 2026-12-01" in md


def test_an_article_with_plain_text_body(tmp_path):
    article = {"@context": "https://schema.org", "@type": "NewsArticle", "headline": "Rates hold steady",
               "author": [{"@type": "Person", "name": "A. Writer"}], "datePublished": "2026-10-01",
               "articleBody": "First paragraph of the story, long enough to count as content. " * 3
               + "\n\nSecond paragraph of the story, also long enough. " * 2}
    rebuilt = inspect(tmp_path, shell(article))
    assert rebuilt.kind == "NewsArticle" and "article" in rebuilt.note
    md = to_markdown(rebuilt.html)
    assert md.startswith("# Rates hold steady")
    assert "**Author:** A. Writer" in md and "Second paragraph" in md


def test_json_ld_in_a_graph_a_list_and_after_a_broken_block(tmp_path):
    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "Organization", "name": "Acme", "description": "An organisation. " * 30},
        job_posting(),
    ]}
    for blocks in ([graph], [[job_posting()]], ["{not json", job_posting()]):
        rebuilt = inspect(tmp_path, shell(*blocks))
        assert rebuilt is not None and rebuilt.kind == "JobPosting", blocks


def test_types_inkdoc_cannot_stand_in_for_the_page_are_not_used(tmp_path):
    organisation = {"@type": "Organization", "name": "Acme", "description": "Who we are. " * 40}
    with pytest.raises(ws.JavaScriptPageError):
        inspect(tmp_path, shell(organisation))
    # A posting too short to stand in for the page is not used either.
    with pytest.raises(ws.JavaScriptPageError):
        inspect(tmp_path, shell(job_posting(description="<p>Apply now.</p>")))


def test_the_rebuilt_page_holds_no_script_and_escapes_every_value(tmp_path):
    posting = job_posting(
        title='<img src=x onerror="alert(1)">Analyst',
        hiringOrganization={"name": "<script>alert(2)</script>Acme"},
        description=DESCRIPTION + '<script>alert(3)</script><iframe src="https://evil.example"></iframe>'
        + '<form action="https://evil.example"><input name="q"></form>',
    )
    html_text = inspect(tmp_path, shell(posting)).html.decode("utf-8")
    for bad in ("<script", "<iframe", "<form", "<img", "onerror", "alert(3)"):
        assert bad not in html_text, bad


def test_html_escaped_once_more_in_the_json(tmp_path):
    import html

    md = to_markdown(inspect(tmp_path, shell(job_posting(description=html.escape(DESCRIPTION)))).html)
    assert "* Own Salesforce and NetSuite integrations" in md and "&lt;" not in md


def test_charset_from_the_page_when_the_server_gives_none(tmp_path):
    page = shell(job_posting(title="Ingénieur logiciel")).replace("<head>", '<head><meta charset="windows-1252">')
    path = tmp_path / "page.html"
    path.write_bytes(page.encode("windows-1252"))
    md = to_markdown(ws.inspect_page(path, url="https://jobs.example.com/x", charset=None).html)
    assert md.startswith("# Ingénieur logiciel")


# ─── Shells with nothing to read ─────────────────────────────────────────────

def test_a_shell_with_nothing_to_read_says_what_to_do(tmp_path):
    with pytest.raises(ws.JavaScriptPageError) as caught:
        inspect(tmp_path, shell())
    assert isinstance(caught.value, UserFacingConversionError)  # shown to the user as worded
    assert "print it to PDF" in str(caught.value)


def test_a_bug_never_costs_the_conversion(tmp_path, monkeypatch):
    def broken(_page):
        raise RuntimeError("bug")

    monkeypatch.setattr(ws, "_from_json_ld", broken)
    assert inspect(tmp_path, shell(job_posting())) is None
    assert ws.inspect_page(tmp_path / "missing.html", url="https://example.com/") is None


# ─── Through the URL route ───────────────────────────────────────────────────

def _fake_fetch(page: str, content_type: str = "text/html"):
    import tempfile

    from app.core.url_fetcher import FetchedUrlResult

    made: list[Path] = []

    def fetch(url, **_kwargs):
        with tempfile.NamedTemporaryFile("wb", prefix="inkdoc_fetch_", suffix=".html", delete=False) as f:
            f.write(page.encode("utf-8"))
        made.append(Path(f.name))
        return FetchedUrlResult(final_url=url, temp_file_path=Path(f.name), content_bytes=None,
                                content_type=content_type, charset="utf-8", filename="123")

    return fetch, made


@pytest.fixture
def api():
    from fastapi.testclient import TestClient

    from app.server.server import app

    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
        yield TestClient(app)


def test_url_route_converts_the_rebuilt_page_and_says_so(api):
    fetch, made = _fake_fetch(shell(job_posting()))
    with patch("app.core.url_fetcher.fetch_url_safely", side_effect=fetch):
        resp = api.post("/convert/url?response_format=json", json={"url": "https://jobs.example.com/acme/123"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["markdown"].startswith("# Lead Business Systems Analyst")
    assert any("job posting" in w for w in data["warnings"])
    assert made and not made[0].exists(), "the fetched file was left behind"


def test_url_route_refuses_a_shell_with_nothing_to_read(api):
    fetch, made = _fake_fetch(shell())
    with patch("app.core.url_fetcher.fetch_url_safely", side_effect=fetch):
        resp = api.post("/convert/url?response_format=json&job_id=js-shell-0001",
                        json={"url": "https://jobs.example.com/acme/123", "job_id": "js-shell-0001"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == ws.JAVASCRIPT_PAGE_MESSAGE
    assert api.get("/convert/progress/js-shell-0001").json()["status"] == "error"
    assert made and not made[0].exists(), "the fetched file was left behind"


def test_url_route_leaves_an_ordinary_page_as_before(api):
    page = "<html><body><h1>Hello</h1><p>An ordinary page.</p></body></html>"
    fetch, _made = _fake_fetch(page)
    with patch("app.core.url_fetcher.fetch_url_safely", side_effect=fetch):
        resp = api.post("/convert/url?response_format=json", json={"url": "https://example.com/page"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["markdown"].strip() == "# Hello\n\nAn ordinary page."
    assert resp.json()["warnings"] == []


def test_an_explicit_type_override_is_respected(tmp_path):
    from app.core.converter import open_url_source
    from app.core.queue_model import QueueItem, SourceKind

    fetch, _made = _fake_fetch(shell(job_posting()))
    item = QueueItem(source="https://jobs.example.com/acme/123", kind=SourceKind.URL, display_name="x",
                     extension_override=".txt")
    with patch("app.core.url_fetcher.fetch_url_safely", side_effect=fetch):
        fetched = open_url_source(item)
    try:
        assert fetched.notes == []
        assert b"enable JavaScript" in fetched.path.read_bytes()  # as fetched
    finally:
        fetched.cleanup()
