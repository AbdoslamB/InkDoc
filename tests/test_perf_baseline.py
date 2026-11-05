"""The performance baseline (scripts/perf_baseline.py): limits, comparison and which baseline applies."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.perf_baseline as pb  # noqa: E402

BASE = {"python": "3.14", "import_mb": 50.0, "convert_peak_mb": 160.0, "import_s": 1.0, "server_start_s": 2.0,
        "worker_ready_s": 4.0, "convert_s": 10.0, "tables_s": 6.0, "outputs": {"markdown": "a", "tables": "b"}}


def test_the_same_numbers_pass():
    lines, over = pb.compare(dict(BASE), BASE)
    assert over == [] and len(lines) == len(pb.MEMORY_METRICS) + len(pb.TIME_METRICS)


def test_memory_over_its_margin_fails_and_time_has_room():
    current = dict(BASE, convert_peak_mb=160.0 * pb.MEMORY_MARGIN + 1, convert_s=10.0 * pb.TIME_MARGIN)
    _lines, over = pb.compare(current, BASE)
    assert over == ["convert_peak_mb"]
    current = dict(BASE, convert_s=10.0 * pb.TIME_MARGIN + pb.TIME_SLACK_S + 0.1)
    assert pb.compare(current, BASE)[1] == ["convert_s"]


def test_no_baseline_or_another_python_only_measures():
    assert pb.compare(dict(BASE), None)[1] == []
    lines, over = pb.compare(dict(BASE, python="3.15", import_mb=999.0), BASE)
    assert over == [] and "Python 3.14" in lines[0]


def test_a_change_of_output_is_reported_but_not_a_failure():
    lines, over = pb.compare(dict(BASE, outputs={"markdown": "c", "tables": "b"}), BASE)
    assert over == [] and any("outputs" in line for line in lines)


def test_ci_and_local_machines_have_their_own_baselines(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_OS", "macOS")
    assert pb.machine() == "ci-macos"
    monkeypatch.delenv("GITHUB_ACTIONS")
    assert pb.machine() == f"local-{sys.platform}"


def test_the_ci_baseline_file_is_valid_and_holds_only_ci_machines():
    data = json.loads(pb.BASELINE.read_text(encoding="utf-8"))
    assert all(key.startswith("ci-") for key in data["machines"])
    for entry in data["machines"].values():
        assert set(pb.MEMORY_METRICS + pb.TIME_METRICS) <= set(entry)


def test_the_generated_pdf_has_its_pages(tmp_path):
    from app.core.pdf_probe import probe_pdf
    from scripts.runtime_bench import MIXED_PAGES, make_mixed_pdf

    probe = probe_pdf(str(make_mixed_pdf(tmp_path / "mixed.pdf")))
    assert probe.page_count == MIXED_PAGES and probe.table_pages
