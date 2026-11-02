"""The optimization audit (scripts/optimize_audit.py): what it reads and what it suggests. No network."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.optimize_audit as oa  # noqa: E402

REQS = """\
# Pinned exactly: the bridge
# depends on it.
pywebview==6.2.1

markitdown[all]==0.1.8
fastapi>=0.110.0
"""


# ─── Packages ────────────────────────────────────────────────────────────────
def test_requirements_keep_their_pins_and_reasons():
    reqs = {r["name"]: r for r in oa.parse_requirements(REQS)}
    assert reqs["pywebview"]["pinned"] and reqs["pywebview"]["why"] == "Pinned exactly: the bridge depends on it."
    assert reqs["markitdown"]["extras"] == "[all]" and reqs["markitdown"]["why"] == ""
    assert not reqs["fastapi"]["pinned"]


def test_a_trial_installs_the_test_tools_ci_installs():
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    installs = [line for line in ci.splitlines() if "pip install" in line and "pytest==" in line]
    assert installs and all(tool in line.split() for line in installs for tool in oa.TEST_TOOLS)


def test_the_real_requirements_parse():
    names = {r["name"] for r in oa.parse_requirements(oa.REQUIREMENTS.read_text(encoding="utf-8"))}
    assert {"markitdown", "pywebview", "fastapi"} <= names


def test_dependabot_major_ignores_are_read():
    text = (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert {"pywebview", "fastapi", "uvicorn"} <= oa.dependabot_major_ignores(text)


def test_a_pinned_major_upgrade_carries_its_reasons():
    outdated = [{"name": "pywebview", "version": "6.2.1", "latest_version": "7.0"},
                {"name": "fastapi", "version": "0.115.0", "latest_version": "0.116.0"},
                {"name": "pdfminer.six", "version": "1", "latest_version": "2"}]
    found = oa.package_candidates(oa.parse_requirements(REQS), outdated, {"pywebview"})
    webview = next(f for f in found if f["title"].startswith("pywebview"))
    assert any("the bridge depends on it" in r for r in webview["rules"])
    assert any("Dependabot leaves these to a person" in r for r in webview["rules"])
    fastapi = next(f for f in found if f["title"].startswith("fastapi"))
    assert not any("major" in r for r in fastapi["rules"])
    assert found[-1]["kind"] == "info" and "pdfminer-six" in found[-1]["evidence"]


def test_a_pin_this_environment_does_not_have_is_said():
    outdated = [{"name": "markitdown", "version": "0.1.7", "latest_version": "0.1.9"}]
    found = oa.package_candidates(oa.parse_requirements(REQS), outdated, set())
    assert found[0]["title"] == "markitdown 0.1.8 -> 0.1.9"
    assert "this environment has 0.1.7, not the pinned 0.1.8" in found[0]["evidence"]


def test_a_trial_replaces_the_pinned_line_and_keeps_its_extras():
    text = oa.trial_requirements(REQS, ["markitdown==0.2.0", "pywebview", "numpy==2.3.0"])
    lines = text.splitlines()
    assert "markitdown[all]==0.2.0" in lines and "pywebview" in lines and "numpy==2.3.0" in lines
    assert "markitdown[all]==0.1.8" not in lines and "pywebview==6.2.1" not in lines


def test_a_trial_compares_numbers_and_outputs():
    base = {"import_mb": 50.0, "convert_s": 10.0, "outputs": {"m": "a"}}
    numbers, same = oa.compare_trial(base, {"import_mb": 45.0, "convert_s": 10.0, "outputs": {"m": "a"}})
    assert same and "import_mb 50.0 -> 45.0 (-10%)" in numbers
    assert not oa.compare_trial(base, dict(base, outputs={"m": "b"}))[1]


# ─── Profile ─────────────────────────────────────────────────────────────────
def test_profiled_code_is_grouped_by_owner():
    root = Path("/repo")
    assert oa.group_of("/repo/app/core/table_finder.py", root) == "inkdoc"
    assert oa.group_of("/x/lib/site-packages/pdfminer/layout.py", root) == "lib:pdfminer"
    assert oa.group_of("~", root) == "builtin"
    assert oa.group_of("/usr/lib/python3.14/re/__init__.py", root) == "stdlib"
    assert oa.module_of("/repo/app/core/table_finder.py", root) == "app.core.table_finder"


def test_a_step_module_and_a_worker_module_carry_their_rules():
    assert oa.STEP_RULE in oa.module_rules("app.core.text_polish")
    assert oa.WORKER_RULE in oa.module_rules("app.core.table_finder")
    assert oa.LIGHT_RULE in oa.module_rules("app.core.converter")


def _profile(share: float) -> dict:
    return {"total_s": 10.0, "groups": {"inkdoc": 2.0, "lib:pdfminer": 6.0, "stdlib": 2.0},
            "functions": [{"module": "app.core.text_polish", "function": "swap", "line": 84, "calls": 9,
                           "own_s": share * 10, "share": share}],
            "stages": [{"name": "markitdown", "seconds": 6.0, "peak_mb": 90.0,
                        "allocations": [{"where": "x.py:1", "mb": 12.0}]},
                       {"name": "typography", "seconds": 1.0, "peak_mb": 5.0, "allocations": []}]}


def test_only_a_function_over_the_threshold_is_a_candidate():
    found = oa.profile_candidates(_profile(oa.PROFILE_MIN_SHARE))
    hot = [f for f in found if f["kind"] == "candidate"]
    assert len(hot) == 1 and hot[0]["where"] == ["app/core/text_polish.py:84"] and oa.STEP_RULE in hot[0]["rules"]
    assert not [f for f in oa.profile_candidates(_profile(oa.PROFILE_MIN_SHARE / 2)) if f["kind"] == "candidate"]


def test_the_language_question_is_answered_with_inkdocs_share():
    first = oa.profile_candidates(_profile(0.01))[0]
    assert first["title"] == "InkDoc's own Python is 20% of the time" and oa.NATIVE_RULE in first["rules"]
    assert "pdfminer 60%" in first["evidence"]
    assert any("allocations peak in markitdown at 90 MB" in f["title"] for f in oa.profile_candidates(_profile(0.01)))


def test_a_real_profile_runs(tmp_path):
    from tests._pdf_fixtures import prose_page, write_pdf
    from tests._table_fixtures import side_by_side

    pdf = write_pdf(tmp_path / "small.pdf", side_by_side()[0] + [prose_page(200)])
    found = oa.audit_profile(tmp_path, pdf)
    assert found[0]["kind"] == "info" and "InkDoc's own Python" in found[0]["title"], found


# ─── Size ────────────────────────────────────────────────────────────────────
def test_a_bundle_is_grouped_by_its_parts(tmp_path):
    internal = tmp_path / "inkdoc" / "_internal"
    (internal / "numpy" / "core").mkdir(parents=True)
    (internal / "numpy" / "core" / "a.pyd").write_bytes(b"x" * 3000)
    (internal / "numpy-2.3.0.dist-info").mkdir()
    (internal / "numpy-2.3.0.dist-info" / "RECORD").write_bytes(b"x" * 10)
    (internal / "python314.dll").write_bytes(b"x" * 500)
    (tmp_path / "inkdoc" / "inkdoc.exe").write_bytes(b"x" * 100)
    assert oa.size_groups(tmp_path / "inkdoc") == {"numpy": 3000, "numpy (metadata)": 10, "python314.dll": 500,
                                                   "inkdoc.exe": 100}


def test_a_release_zip_is_grouped_like_the_folder_it_holds(tmp_path):
    import zipfile

    archive = tmp_path / "inkdoc-windows.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("inkdoc/_internal/numpy/core/a.pyd", b"x" * 3000)
        z.writestr("inkdoc/_internal/numpy-2.3.0.dist-info/RECORD", b"x" * 10)
        z.writestr("inkdoc/inkdoc.exe", b"x" * 100)
    assert oa.size_groups(archive) == {"numpy": 3000, "numpy (metadata)": 10, "inkdoc.exe": 100}


def test_large_parts_and_growth_are_candidates():
    mb = 2**20
    groups = {"torch": int(oa.SIZE_MIN_MB * mb), "small": mb // 2}
    found = oa.size_candidates(groups, {"torch": int((oa.SIZE_MIN_MB - oa.GROWTH_MIN_MB) * mb), "small": mb // 2})
    titles = [f["title"] for f in found if f["kind"] == "candidate"]
    assert titles[0].startswith("torch:") and titles[1].startswith("since the last audit: +1.0 MB")


def test_a_large_part_that_is_a_small_share_is_only_listed():
    mb = 2**20
    groups = {"big": int(200 * mb), "medium": int(oa.SIZE_MIN_MB * mb)}  # medium is under SIZE_MIN_SHARE
    titles = [f["title"] for f in oa.size_candidates(groups, None) if f["kind"] == "candidate"]
    assert len(titles) == 1 and titles[0].startswith("big:")


def test_a_local_build_is_marked_as_such(tmp_path):
    (tmp_path / "b" / "_internal").mkdir(parents=True)
    (tmp_path / "b" / "inkdoc.exe").write_bytes(b"x")
    found = oa.audit_size(tmp_path, tmp_path / "b", None)
    assert any(f["title"] == "this is a local build" for f in found)


# ─── Runtime ─────────────────────────────────────────────────────────────────
def test_the_bundled_python_and_the_floor_are_read_from_the_workflows():
    bundled, floor = oa.python_versions(
        (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"),
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert bundled and floor and tuple(map(int, floor.split("."))) <= tuple(map(int, bundled.split(".")))


CYCLES = [{"cycle": "3.15", "releaseDate": "2027-10-01", "eol": "2032-10-31"},
          {"cycle": "3.14", "releaseDate": "2025-10-07", "eol": "2030-10-31"},
          {"cycle": "3.13", "releaseDate": "2024-10-07", "eol": "2029-10-31"},
          {"cycle": "3.10", "releaseDate": "2021-10-04", "eol": "2026-10-31"}]


def test_a_floor_past_its_end_of_life_is_a_candidate():
    found = oa.python_candidates(CYCLES, "3.14", "3.10", date(2026, 11, 1))
    assert [f["title"] for f in found] == ["Python 3.10 (source floor) reached its end of life on 2026-10-31"]
    assert oa.FLOOR_RULE in found[0]["rules"]


def test_pythons_own_calendar_is_read_with_month_end_dates():
    data = {"3.15": {"status": "bugfix", "first_release": "2026-10-09", "end_of_life": "2031-10"},
            "3.10": {"status": "security", "first_release": "2021-10-04", "end_of_life": "2026-10"}}
    cycles = oa.release_cycles(data)
    assert {"cycle": "3.10", "releaseDate": "2021-10-04", "eol": "2026-10-31"} in cycles
    found = oa.python_candidates(cycles, "3.14", "3.10", date(2026, 11, 2))
    assert [f["title"] for f in found] == ["Python 3.10 (source floor) reached its end of life on 2026-10-31",
                                           "Python 3.15 is out (InkDoc ships 3.14)"]
    assert oa.release_cycles(CYCLES) is CYCLES  # endoflife.date's list is used as it is


def test_a_newer_python_counts_only_once_it_is_released():
    assert not oa.python_candidates(CYCLES, "3.14", "3.13", date(2027, 9, 1))
    found = oa.python_candidates(CYCLES, "3.14", "3.13", date(2027, 11, 1))
    assert [f["title"] for f in found] == ["Python 3.15 is out (InkDoc ships 3.14)"]


def test_the_report_marks_every_kind():
    found = [oa.candidate("size", "big", "1 MB", rules=["r"], where=["w"]),
             oa.candidate("size", "ctx", "e", kind="info"), oa.candidate("runtime", "x", "y", kind="error")]
    text = oa.render(found)
    assert "**Candidate: big**" in text and "Must respect: r" in text and "**Context: ctx**" in text
    assert "**Couldn't check: x**" in text and text.index("## Size") < text.index("## Runtime")
