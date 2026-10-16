"""The table check through the HTTP API: the setting, the override, the metadata.

Explicit engines are only checked (the Markdown is exactly the engine's);
Auto replaces the tables the engine got wrong. Fast, the default, changes
nothing at all.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.server.server as server_module
from app.core.markdown_tables import find_table_blocks
from app.server.server import app
from tests._pdf_fixtures import build_pdf
from tests._table_fixtures import side_by_side

client = TestClient(app)
TOKEN = {"X-InkDoc-Token": server_module.SESSION_TOKEN}


@pytest.fixture(autouse=True, scope="module")
def _stop_worker_after():
    yield
    from app.core.engines.markitdown_worker_client import MarkItDownWorker

    MarkItDownWorker.get_instance().shutdown()


@pytest.fixture(autouse=True)
def _no_ocr_engines(monkeypatch):
    import app.core.auto_engine as auto_engine

    monkeypatch.setattr(auto_engine, "ocr_engines_usable", lambda: [])


def _pair() -> tuple[bytes, list[list[list[str]]]]:
    pages, truth = side_by_side(True)
    return build_pdf(pages), [t["rows"] for t in truth]


def _convert(engine: str, *, level: str | None = None, fmt: str = "json"):
    pdf, _ = _pair()
    query = f"engine={engine}&response_format={fmt}" + (f"&auto_quality={level}" if level else "")
    return client.post(f"/convert/file?{query}", files={"file": ("pair.pdf", pdf, "application/pdf")})


def test_fast_is_the_default_and_adds_nothing():
    resp = _convert("auto")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["tables"] is None
    assert "inkdoc:table" not in data["markdown"]


def test_explicit_engine_is_checked_never_changed():
    resp = _convert("markitdown", level="balanced")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    tables = data["tables"]
    assert tables["checked"] and tables["tables_found"] == 2
    assert tables["warning"] is True and tables["repaired"] == []
    assert tables["message"] == "Page 1: 2 side-by-side tables came out as one."
    assert tables["passes"] == [], "Auto's steps: an explicit engine runs none"
    assert len(find_table_blocks(data["markdown"].split("\n"))) == 1  # MarkItDown's own, untouched


def test_auto_rebuilds_the_tables_and_says_so():
    _, truth = _pair()
    data = _convert("auto", level="balanced").json()
    assert [b.rows for b in find_table_blocks(data["markdown"].split("\n"))] == truth
    tables = data["tables"]
    assert tables["warning"] is False
    assert tables["repaired"] == [{"page": 1, "tables": 2, "issue": "merged", "source": "pdf_lines"}]
    assert any("rebuilt 2 tables" in line for line in tables["details"])
    assert data["engine_used"] == "markitdown"
    assert tables["passes"] == [{"name": "table_check", "ran": True, "changed": 2, "skipped": ""}]


def test_text_responses_carry_table_headers():
    resp = _convert("auto", level="balanced", fmt="text")
    assert resp.headers["X-Tables-Rebuilt"] == "2"
    assert resp.headers["X-Tables-Warning"] == "false"


def test_the_setting_applies_when_no_override_is_sent():
    assert client.post("/settings", json={"auto_quality": "balanced"}, headers=TOKEN).status_code == 200
    assert client.get("/settings").json()["auto_quality"] == "balanced"
    data = _convert("auto").json()
    assert data["tables"]["repaired"]
    # The per-request override wins over the setting.
    assert _convert("auto", level="fast").json()["tables"] is None


def test_only_known_levels_are_accepted():
    assert client.post("/settings", json={"auto_quality": "max"}, headers=TOKEN).status_code == 422
    assert _convert("auto", level="max").status_code == 422
    assert client.post("/settings", json={"auto_quality": "thorough"}).status_code in (401, 403)


# ─── Steps switched off (Settings: thorough_passes_off) ──────────────────────
def _off(value, headers=TOKEN):
    return client.post("/settings", json={"thorough_passes_off": value}, headers=headers)


def test_steps_switched_off_are_checked_against_the_registry():
    from app.core.engine_manager import EngineManager
    from app.core.thorough_passes import PASS_NAMES, catalogue

    body = client.get("/settings").json()
    assert body["thorough_passes_off"] == [], "every step on by default"
    assert body["thorough_passes"] == catalogue(), "the page's switches come from the registry"
    assert _off(["polish", "digital_layout", "polish"]).status_code == 200
    stored = lambda: EngineManager.get_instance().get_settings()["thorough_passes_off"]  # noqa: E731
    assert stored() == ["digital_layout", "polish"], "once each, in run order"
    for bad in (["nope"], "polish", [3], [*PASS_NAMES, "polish"]):
        assert _off(bad).status_code == 422, bad
    assert stored() == ["digital_layout", "polish"], "a refused change changes nothing"
    assert _off([], headers={}).status_code == 403
    assert _off([]).status_code == 200 and stored() == []


def test_a_step_switched_off_is_not_run():
    assert _off(["table_check"]).status_code == 200
    data = _convert("auto", level="balanced").json()
    assert len(find_table_blocks(data["markdown"].split("\n"))) == 1, "MarkItDown's own table, untouched"
    tables = data["tables"]
    assert tables["checked"] is False and tables["skipped_reason"] == "switched_off"
    assert tables["passes"] == [{"name": "table_check", "ran": False, "changed": 0, "skipped": "switched off"}]
    assert _convert("markitdown", level="balanced").json()["tables"] is None, "explicit engines skip it too"


def test_a_hand_edited_list_never_breaks_a_conversion():
    from app.core.engine_manager import EngineManager

    EngineManager.get_instance().update_settings({"thorough_passes_off": "table_check"})
    data = _convert("auto", level="balanced").json()
    assert data["tables"]["repaired"], "not a list: nothing is off"


def test_restore_defaults_puts_fast_back():
    client.post("/settings", json={"auto_quality": "thorough"}, headers=TOKEN)
    assert client.post("/settings/reset", headers=TOKEN).json()["auto_quality"] == "fast"
