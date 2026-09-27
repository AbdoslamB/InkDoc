"""scripts/build_addon.py's pre-publish checks, and the shared Docling pin.

Add-on assets are immutable once published, so every check that stops a bad one
has to run before the upload. They only run inside build-addon.yml, which needs a
640 MB download and a model load, so their failure branches are exercised here
with the Docling-dependent parts substituted.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.build_addon as ba  # noqa: E402

CONSTRAINTS = REPO_ROOT / "scripts" / "docling-constraints.txt"


# ─── The shared pin ──────────────────────────────────────────────────────────

def constraint_lines() -> list[str]:
    return [
        line.split("#", 1)[0].strip()
        for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    ]


def test_every_constraint_is_an_exact_pin():
    """A range would let the pack and the add-on resolve different versions."""
    lines = constraint_lines()
    assert lines, "constraints file has no entries"
    for line in lines:
        assert re.fullmatch(r"[A-Za-z0-9._-]+==[0-9][^\s=]*", line), (
            f"'{line}' is not an exact name==version pin"
        )


def test_constraint_pins_carry_no_local_version_label():
    """`torch==2.14.0+cpu` would not resolve on macOS, where the wheel has no
    +cpu label. An unlabelled pin matches both (PEP 440 ignores the local label)."""
    for line in constraint_lines():
        assert "+" not in line, f"'{line}' pins a local version label"


def test_both_builds_install_with_the_constraints():
    """The pin only works if both artifacts are built with it."""
    build_pack = (REPO_ROOT / "scripts" / "build_pack.py").read_text(encoding="utf-8")
    # torch and docling are separate pip invocations; both need it, or the
    # second can resolve a torch the first did not pick.
    assert build_pack.count('"-c",\n        str(DOCLING_CONSTRAINTS),') == 2, (
        "build_pack.py must pass -c DOCLING_CONSTRAINTS to both pip installs"
    )
    assert 'DOCLING_CONSTRAINTS = REPO_ROOT / "scripts" / "docling-constraints.txt"' in build_pack

    workflow = (REPO_ROOT / ".github" / "workflows" / "build-addon.yml").read_text(
        encoding="utf-8"
    )
    assert "-c scripts/docling-constraints.txt" in workflow, (
        "build-addon.yml must install docling with the shared constraints"
    )


def test_pinned_docling_version_reads_the_real_file():
    assert re.fullmatch(r"\d+\.\d+\.\d+", ba.pinned_docling_version())


def test_pinned_docling_version_ignores_siblings_and_comments(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text(
        "# docling==0.0.1 in a comment\n"
        "docling-core==2.98.0\n"
        "docling_slim==2.130.0\n"
        "  docling == 2.130.0  # the real pin\n",
        encoding="utf-8",
    )
    assert ba.pinned_docling_version(f) == "2.130.0"


def test_missing_docling_pin_is_fatal(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text("docling-core==2.98.0\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="does not pin docling"):
        ba.pinned_docling_version(f)


# ─── Pack / add-on Docling consistency ───────────────────────────────────────

def test_pack_docling_versions_reads_the_real_manifest():
    shipped = ba.pack_docling_versions()
    assert shipped, "manifest lists no platforms"
    assert all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in shipped.values()), shipped


def test_pack_docling_versions_is_not_fooled_by_docling_core(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"supported_platforms": {
        "linux-x86_64": {"sha256_files": {
            "python/lib/site-packages/docling_core-2.98.0.dist-info/RECORD": "x",
            "python/lib/site-packages/docling-2.130.0.dist-info/RECORD": "x",
        }},
        "macos-arm64": {"sha256_files": {
            "python/lib/site-packages/docling_core-2.98.0.dist-info/RECORD": "x",
        }},
    }}), encoding="utf-8")
    assert ba.pack_docling_versions(manifest) == {
        "linux-x86_64": "2.130.0",
        "macos-arm64": "",
    }


def test_the_shipped_pack_matches_the_pin():
    """Guards the pin file against a typo against what docling-pack-v5 shipped.

    Deliberately a statement about today's files, not a rule for all time: when
    the pin is bumped this fails until the new pack's manifest lands, which is
    the release order build_addon.py enforces anyway.
    """
    pinned = ba.pinned_docling_version()
    assert set(ba.pack_docling_versions().values()) == {pinned}


def test_consistency_refuses_a_pack_on_another_docling(monkeypatch):
    monkeypatch.setattr(ba, "pinned_docling_version", lambda: "2.140.0")
    monkeypatch.setattr(ba, "pack_docling_versions", lambda: {
        "windows-x86_64": "2.140.0",
        "linux-x86_64": "2.130.0",
    })
    with pytest.raises(SystemExit, match="Build and publish the pack first"):
        ba.check_docling_consistency(downloading=False)


def test_consistency_refuses_an_unconstrained_install(monkeypatch):
    import importlib.metadata

    monkeypatch.setattr(ba, "pinned_docling_version", lambda: "2.130.0")
    monkeypatch.setattr(ba, "pack_docling_versions", lambda: {"linux-x86_64": "2.130.0"})
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "2.140.0")
    with pytest.raises(SystemExit, match="Installed docling is 2.140.0"):
        ba.check_docling_consistency(downloading=True)


def test_consistency_ignores_the_local_install_for_model_dir(monkeypatch):
    """With --model-dir the weights came from elsewhere; the local Docling is moot."""
    import importlib.metadata

    monkeypatch.setattr(ba, "pinned_docling_version", lambda: "2.130.0")
    monkeypatch.setattr(ba, "pack_docling_versions", lambda: {"linux-x86_64": "2.130.0"})

    def must_not_be_called(name):
        raise AssertionError("local docling version consulted for --model-dir")

    monkeypatch.setattr(importlib.metadata, "version", must_not_be_called)
    assert ba.check_docling_consistency(downloading=False) == "2.130.0"


# ─── Pre-publish model load ──────────────────────────────────────────────────

class _StageSpy:
    """Stands in for Docling's CodeFormulaModel and records how it was built."""

    _model_repo_folder = "docling-project--CodeFormulaV2"
    calls: list[dict] = []
    fail_with: Exception | None = None

    def __init__(self, **kwargs):
        if _StageSpy.fail_with is not None:
            raise _StageSpy.fail_with
        _StageSpy.calls.append(kwargs)


@pytest.fixture
def fake_stage(monkeypatch):
    _StageSpy.calls = []
    _StageSpy.fail_with = None
    cpu = object()
    monkeypatch.setattr(ba, "_code_formula_stage", lambda: (_StageSpy, dict, cpu))
    # verify_model_loads forces offline mode; register the vars so the test
    # environment is restored afterwards.
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
    return cpu


def test_model_is_loaded_from_exactly_the_directory_being_archived(tmp_path, fake_stage):
    model = tmp_path / "docling-project--CodeFormulaV2"
    model.mkdir()
    ba.verify_model_loads(model)

    (call,) = _StageSpy.calls
    assert call["enabled"] is True
    # Docling appends _model_repo_folder to artifacts_path itself.
    assert call["artifacts_path"] == tmp_path
    assert call["accelerator_options"] is fake_stage


def test_model_load_is_offline(tmp_path, fake_stage):
    """Otherwise the loader could fetch a fresh copy and check that instead."""
    import os

    model = tmp_path / "docling-project--CodeFormulaV2"
    model.mkdir()
    ba.verify_model_loads(model)
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_misnamed_model_directory_is_refused(tmp_path, fake_stage):
    model = tmp_path / "CodeFormulaV2"
    model.mkdir()
    with pytest.raises(SystemExit, match="would never find the installed model"):
        ba.verify_model_loads(model)
    assert _StageSpy.calls == [], "must refuse before attempting a load"


def test_unloadable_model_is_refused(tmp_path, fake_stage):
    """The upstream-drift case: weights Docling's pinned stack cannot load."""
    _StageSpy.fail_with = ValueError("Unrecognized model type 'codeformula_v3'")
    model = tmp_path / "docling-project--CodeFormulaV2"
    model.mkdir()
    with pytest.raises(SystemExit, match="does not load under the pinned Docling stack"):
        ba.verify_model_loads(model)


def test_build_checks_consistency_and_loadability_before_writing(tmp_path, monkeypatch):
    """Ordering matters: nothing may reach the output directory ahead of the checks."""
    order: list[str] = []
    monkeypatch.setattr(ba, "check_docling_consistency",
                        lambda downloading: order.append(f"consistency:{downloading}"))

    def refuse(path):
        order.append("load")
        raise SystemExit("[FAIL] does not load")

    monkeypatch.setattr(ba, "verify_model_loads", refuse)
    model = tmp_path / "docling-project--CodeFormulaV2"
    model.mkdir()
    (model / "config.json").write_text("{}", encoding="utf-8")
    out = tmp_path / "out"

    with pytest.raises(SystemExit):
        ba.build(out, model, "docling-addon-v1")
    assert order == ["consistency:False", "load"]
    assert not any(out.glob("*")), "an unloadable model must produce no artifact"


# ─── Provenance ──────────────────────────────────────────────────────────────

def write_metadata(model: Path, files: dict[str, str]) -> None:
    meta = model / ".cache" / "huggingface" / "download"
    meta.mkdir(parents=True)
    for name, commit in files.items():
        (meta / f"{name}.metadata").write_text(f"{commit}\netag\n1790461307.9\n", encoding="utf-8")


def test_source_revision_is_the_snapshot_commit(tmp_path):
    commit = "ecedbe111d15c2dc60bfd4a823cbe80127b58af4"
    write_metadata(tmp_path, {"config.json": commit, "model.safetensors": commit})
    assert ba.source_revision(tmp_path) == commit


def test_mixed_snapshot_is_fatal(tmp_path):
    """Files from two commits mean an interrupted or mixed download."""
    write_metadata(tmp_path, {
        "config.json": "a" * 40,
        "model.safetensors": "b" * 40,
    })
    with pytest.raises(SystemExit, match="different commits"):
        ba.source_revision(tmp_path)


def test_unknown_revision_without_metadata(tmp_path):
    assert ba.source_revision(tmp_path) == ""


def test_malformed_commit_is_fatal(tmp_path):
    write_metadata(tmp_path, {"config.json": "not-a-commit"})
    with pytest.raises(SystemExit, match="Unexpected commit id"):
        ba.source_revision(tmp_path)
