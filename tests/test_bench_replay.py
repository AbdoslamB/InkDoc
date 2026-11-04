"""Record and replay (scripts/bench_replay.py): keys, misses, and Auto replayed byte for byte."""
from __future__ import annotations

import dataclasses
import importlib
import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.bench_replay as br  # noqa: E402
from app.core.queue_model import EngineKind  # noqa: E402


def test_every_conversion_option_is_classified():
    from app.core.converter import ConversionOptions

    fields = {f.name for f in dataclasses.fields(ConversionOptions)}
    named = [*br.ENGINE_INPUTS, *br.ENGINE_RESULTS, *br.NOT_ENGINE]
    assert len(named) == len(set(named))
    assert set(named) == fields, "decide whether a new option is an engine input, a result or neither"


def test_every_replayed_call_exists():
    for module, name in br.CALLS:
        assert callable(getattr(importlib.import_module(module), name)), f"{module}.{name}"


def test_a_part_is_keyed_by_its_document_and_pages(tmp_path):
    doc, part = tmp_path / "doc.pdf", tmp_path / "part.pdf"
    doc.write_bytes(b"%PDF document")
    part.write_bytes(b"%PDF part, different bytes every time")
    rec = br.Recorder(tmp_path / "rec", "replay")
    rec.start(doc)
    rec.part_written(str(doc), [13, 14], str(part))
    key = rec.key("docling", path=str(part))
    assert key["file"] == br.file_sha(doc) and key["pages"] == [13, 14]
    assert rec.key("glm_ocr", path=str(doc), pages=[3])["pages"] == [3]
    assert rec.key("docling", path=str(doc))["pages"] == "all"


def test_only_options_off_their_defaults_are_in_a_key(tmp_path):
    from app.core.converter import ConversionOptions

    rec = br.Recorder(tmp_path, "replay")
    rec.file = "f" * 64
    assert "options" not in rec.key("docling", options=ConversionOptions())
    key = rec.key("docling", options=ConversionOptions(docling_formula_enrichment=True, page_subset=[2]))
    assert key["options"] == {"docling_formula_enrichment": True}


def test_this_machines_paths_are_not_recorded():
    text = f"failed reading {Path(tempfile.gettempdir()) / 'inkdoc-split-x' / 'pages-1-12.pdf'} from {Path.home()}"
    clean = br.sanitize({"warnings": [text]})["warnings"][0]
    assert "<tmp>" in clean and "<home>" in clean and str(Path.home()) not in clean


def test_a_recorded_failure_comes_back_as_itself(tmp_path):
    rec = br.Recorder(tmp_path, "record")
    rec.file = "a" * 64
    key = rec.key("docling")

    def fail():
        raise TimeoutError("Docling took longer than 180 s")

    with pytest.raises(TimeoutError):
        rec.call(key, fail)
    replay = br.Recorder(tmp_path, "replay")
    with pytest.raises(TimeoutError, match="180 s"):
        replay.call(key, lambda: pytest.fail("replay must not run the engine"))


def test_the_corpus_leaves_out_truth_files(tmp_path):
    folder = tmp_path / "corpus" / "01_tables_single"
    folder.mkdir(parents=True)
    for name in ("a.pdf", "a.tables.json", "a.expected.md", ".hidden.pdf"):
        (folder / name).write_bytes(b"%PDF")
    assert [p.name for p in br.corpus(tmp_path)] == ["a.pdf"]


# ─── Auto, recorded with stand-in engines, then replayed ─────────────────────
@pytest.fixture
def engines(monkeypatch):
    """Stand-in engines and machine checks: what they answer, and every call they get."""
    calls: list[tuple] = []
    state = {"ocr": [EngineKind.DOCLING]}
    eng = "app.core.engines"
    auto = "app.core.auto_engine"

    def text_of(path):
        from app.core.pdf_probe import probe_pdf

        return "\n\n".join(p.text.replace("\r\n", " ") for p in probe_pdf(path).pages) + "\n"

    def docling(item, options):
        calls.append(("docling", item.source))
        return text_of(item.source)

    def markitdown(source, options, stream_info_kwargs, url):
        calls.append(("markitdown", source))
        return text_of(source)

    for module, name, fn in [
        (f"{eng}.docling_engine", "convert_with_docling", docling),
        (f"{eng}.markitdown_worker_client", "convert_with_markitdown", markitdown),
        (auto, "ocr_engines_usable", lambda: list(state["ocr"])),
        (auto, "ocr_install_target", lambda: None),
        (auto, "layout_model_usable", lambda: False),
        (auto, "_enrichment_usable", lambda: True),
        (auto, "glm_estimate_seconds", lambda path, probe, pdf: None),
        (auto, "glm_estimate_for_pages", lambda pages: None),
    ]:
        monkeypatch.setattr(importlib.import_module(module), name, fn)
    # The finder in this process, as the table bench runs it, rather than in the worker.
    from app.core.table_finder import find_tables

    monkeypatch.setattr("app.core.table_stage._find_in_worker",
                        lambda p, pages, _job, **kwargs: find_tables(p, pages, **kwargs))
    return calls, state


def _long_pdf(path, pages=14):
    from tests._pdf_fixtures import prose_page, write_pdf

    return write_pdf(path, [prose_page(60, offset=i) for i in range(pages)])


def test_auto_replays_byte_for_byte_and_never_calls_an_engine(tmp_path, engines):
    calls, _ = engines
    pdf = _long_pdf(tmp_path / "long.pdf")
    folder = tmp_path / "recordings"
    rec = br.Recorder(folder, "record")
    with br.patched(rec):
        rec.start(pdf)
        recorded = br.run_case(pdf, "thorough")
    # Docling read the long PDF in two parts (auto_engine.docling_chunks).
    assert [c[0] for c in calls] == ["docling", "docling"]
    assert recorded[1]["decision"]["routed_to"] == "docling"
    calls.clear()
    rec = br.Recorder(folder, "replay")
    with br.patched(rec):
        rec.start(pdf)
        assert br.run_case(pdf, "thorough") == recorded
    assert calls == [] and rec.misses == []
    keys = [json.loads(p.read_text(encoding="utf-8"))["key"] for p in folder.rglob("docling-*.json")]
    assert sorted(k["pages"] for k in keys) == [list(range(1, 13)), [13, 14]]


def test_new_page_boundaries_are_a_miss_naming_the_recorded_ones(tmp_path, engines, monkeypatch):
    pdf = _long_pdf(tmp_path / "long.pdf")
    folder = tmp_path / "recordings"
    rec = br.Recorder(folder, "record")
    with br.patched(rec):
        rec.start(pdf)
        br.run_case(pdf, "thorough")
    monkeypatch.setattr("app.core.auto_engine.THOROUGH_DOCLING_PAGES", 10)
    rec = br.Recorder(folder, "replay")
    with br.patched(rec), pytest.raises(br.ReplayMiss) as miss:
        rec.start(pdf)
        br.run_case(pdf, "thorough")
    assert "docling, pages 1-10" in str(miss.value)
    assert "recorded: docling, pages 1-12; docling, pages 13-14" in str(miss.value)


def test_a_different_machine_replays_as_the_recording_one(tmp_path, engines):
    _calls, state = engines
    pdf = _long_pdf(tmp_path / "short.pdf", pages=2)
    folder = tmp_path / "recordings"
    rec = br.Recorder(folder, "record")
    with br.patched(rec):
        rec.start(pdf)
        recorded = br.run_case(pdf, "thorough")
    state["ocr"] = []  # nothing installed here
    rec = br.Recorder(folder, "replay")
    with br.patched(rec):
        rec.start(pdf)
        assert br.run_case(pdf, "thorough") == recorded


def test_replay_locks_then_flags_a_change(tmp_path, engines, capsys):
    calls, _ = engines
    from tests._pdf_fixtures import write_pdf
    from tests._table_fixtures import side_by_side

    root = tmp_path / "bench"
    (root / "corpus" / "01_tables_single").mkdir(parents=True)
    pdf = write_pdf(root / "corpus" / "01_tables_single" / "doc.pdf", side_by_side()[0])
    assert br.record(root, ["fast"], "", fill=False) == 0
    out = tmp_path / "out"
    assert br.replay(root, ["fast"], "", out, update=False) == 1          # new: no lock yet
    assert br.replay(root, ["fast"], "", out, update=True) == 0
    assert br.replay(root, ["fast"], "", out, update=False) == 0          # same
    lock = br.load_locks(root)["01_tables_single/doc.pdf@fast"]
    assert lock["telemetry"]["decision"]["routed_to"] == "markitdown"
    base = root / "baselines" / "01_tables_single" / "doc.pdf.fast.md"
    assert "East 143 150" in base.read_text(encoding="utf-8")

    # The reply changes (as if InkDoc's own code wrote something else): flagged, with the numbers kept.
    reply = next((root / "recordings").rglob("markitdown-*.json"))
    data = json.loads(reply.read_text(encoding="utf-8"))
    data["reply"] = data["reply"].replace("East 143 150", "East 143")
    reply.write_text(json.dumps(data), encoding="utf-8")
    capsys.readouterr()
    assert br.replay(root, ["fast"], "", out, update=False) == 1
    printed = capsys.readouterr().out
    assert "CHANGED  01_tables_single/doc.pdf@fast: +1 -1 lines" in printed
    assert "numbers 100.0% -> 93.3% found" in printed
    assert calls == [("markitdown", str(pdf))]


def test_a_recorded_error_never_imports_a_module_it_names():
    import sys

    err = br._rebuild({"type": "antigravity:Thing", "message": "boom"})
    assert type(err) is RuntimeError and str(err) == "boom" and "antigravity" not in sys.modules
    assert type(br._rebuild({"type": "builtins:ValueError", "message": "x"})) is ValueError
    from app.core.jobs import ConversionCancelledError

    assert type(br._rebuild({"type": "app.core.jobs:ConversionCancelledError", "message": "c"})) is ConversionCancelledError
