"""Every list of release assets names the same files.

release.yml's publish-release job requires them, its size caps measure them,
scripts/sign_manifest.py signs them for the updater (all but inkdoc.exe, which has no
update path), and the local scripts/release.py checks them on the draft. A rename that
misses one of them (as the macOS zip -> disk image could have) ships a release the
updater can't find or a draft the publish job refuses.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from scripts.sign_manifest import MIN_GLIBC, TARGET_ASSETS

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
RELEASE_PY = REPO_ROOT / "scripts" / "release.py"
NOT_IN_MANIFEST = {"inkdoc.exe"}


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def required_assets() -> list[str]:
    match = re.search(r"for required in ([^;\n]+); do", workflow_text())
    assert match, "publish-release's required asset loop was not found in release.yml"
    return match.group(1).split()


def test_the_signer_covers_every_required_asset_but_the_onefile_exe():
    assert set(required_assets()) - NOT_IN_MANIFEST == set(TARGET_ASSETS)


def test_each_platform_key_is_signed_from_one_asset():
    assert len(set(TARGET_ASSETS.values())) == len(TARGET_ASSETS)
    assert set(MIN_GLIBC) <= set(TARGET_ASSETS.values())


def test_every_required_asset_has_a_size_cap():
    caps = re.findall(r'"([^"]+)" = @\{ Baseline = \d+; Cap = \d+ \}', workflow_text())
    assert sorted(caps) == sorted(required_assets())


def test_every_required_asset_is_smoke_tested():
    text = workflow_text()
    for name in required_assets():
        assert re.search(rf'(--artifact |=")release/{re.escape(name)}\b', text), name


def test_release_py_requires_the_same_assets():
    if not RELEASE_PY.is_file():
        pytest.skip("scripts/release.py is a local tool, not in the repository")
    tree = ast.parse(RELEASE_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "REQUIRED_ASSETS" for t in node.targets
        ):
            assert sorted(ast.literal_eval(node.value)) == sorted(required_assets())
            return
    pytest.fail("REQUIRED_ASSETS not found in scripts/release.py")
