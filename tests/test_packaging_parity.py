"""Every PyInstaller entry point must bundle the same data files.

InkDoc is packaged from several places that each list their data files by hand:
two PyInstaller invocations in .github/workflows/release.yml (the folder bundle
and the single-file .exe), and scripts/build_local_exe.ps1, which
build-local.bat runs. inkdoc.spec is a further one, but it is gitignored
(`*.spec`) and nothing builds from it, so it is checked only where it exists.

Nothing kept them in step, and app/core/addons.json was added to the source tree
without being added to any of them. The packaged app then found no add-on
catalogue, AddonManager swallowed the FileNotFoundError by design, and the
enrichment card in a shipped build read "Unknown add-on" -- a failure no test run
from source could see, because from source the file is simply there.

These tests make that class of mistake fail in CI instead:

  * every non-Python file under app/core is read at runtime relative to its
    module, so every one of them must be bundled by every entry point; and
  * the entry points must bundle exactly the same set, so a data file added to
    one of them is caught even if it lives outside app/core.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"
SPEC = REPO_ROOT / "inkdoc.spec"
LOCAL_PS1 = REPO_ROOT / "scripts" / "build_local_exe.ps1"

Entry = tuple[str, str]  # (source relative to the repo root, destination in bundle)


def _normalise(source: str, dest: str) -> Entry:
    return (
        source.replace("\\", "/").strip("/"),
        dest.replace("\\", "/").strip("/"),
    )


def release_yml_blocks() -> list[set[Entry]]:
    """Each PyInstaller invocation in release.yml, as its own set of entries.

    Blocks are separated on the `pyinstaller` command itself rather than counted
    by line number, so reordering steps does not break the test.
    """
    text = RELEASE_YML.read_text(encoding="utf-8")
    blocks: list[set[Entry]] = []
    for chunk in re.split(r"\bpyinstaller\b", text)[1:]:
        # A block ends at the next step; only its own arguments count.
        chunk = chunk.split("\n      - name:", 1)[0]
        entries = {
            _normalise(src, dest)
            for src, dest in re.findall(
                r'--add-data\s+"([^"]+?)(?:\$\{SEP\}|;|:)([^"]+)"', chunk
            )
        }
        if entries:
            blocks.append(entries)
    return blocks


def spec_entries() -> set[Entry] | None:
    """The literal `datas = [...]` list in inkdoc.spec, or None when absent.

    inkdoc.spec is gitignored, so a CI checkout never has it; there the tracked
    entry points are the whole story. Where a developer does have one, it is held
    to the same standard, since `pyinstaller inkdoc.spec` would otherwise produce
    a build that silently lacks a data file.

    Parsed with ast rather than a regex so comments and line breaks inside the
    list -- which the addons.json entry introduced -- do not matter. The
    collect_all() additions that follow are package data PyInstaller finds on
    its own, not project data files, so they are deliberately excluded.
    """
    if not SPEC.is_file():
        return None
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "datas" for t in node.targets)
            and isinstance(node.value, ast.List)
        ):
            return {_normalise(src, dest) for src, dest in ast.literal_eval(node.value)}
    raise AssertionError("inkdoc.spec has no literal `datas = [...]` assignment")


def local_ps1_entries() -> set[Entry]:
    """--add-data arguments in build_local_exe.ps1, with $xxxSrc variables resolved.

    The script builds absolute sources from $RepoRoot because --specpath makes
    PyInstaller resolve relative sources against the spec directory. Each
    variable is resolved back to its repo-relative path for comparison.
    """
    text = LOCAL_PS1.read_text(encoding="utf-8")
    variables = dict(
        re.findall(r'\$(\w+)\s*=\s*Join-Path\s+\$RepoRoot\s+"([^"]+)"', text)
    )

    entries: set[Entry] = set()
    for src, dest in re.findall(r'"--add-data",\s*"\$\{(\w+)\};([^"]+)"', text):
        assert src in variables, f"build_local_exe.ps1 uses ${{{src}}} but never defines it"
        entries.add(_normalise(variables[src], dest))
    return entries


def runtime_data_files() -> list[Entry]:
    """Every non-Python file under app/core, with the destination it needs.

    These are read at runtime relative to their module's __file__ (see
    ADDONS_CATALOGUE_PATH and engine_manifest.py), so each must land in the same
    directory inside the bundle as it occupies in the source tree.
    """
    core = REPO_ROOT / "app" / "core"
    files = []
    for path in sorted(core.rglob("*")):
        if not path.is_file() or path.suffix in (".py", ".pyc"):
            continue
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        files.append((rel, path.parent.relative_to(REPO_ROOT).as_posix()))
    return files


# ─── Tests ───────────────────────────────────────────────────────────────────

def test_release_yml_has_both_pyinstaller_invocations():
    """Guards the parser: if it finds fewer blocks, the parity checks prove nothing."""
    blocks = release_yml_blocks()
    assert len(blocks) == 2, (
        f"expected the folder-bundle and single-file PyInstaller invocations in "
        f"release.yml, found {len(blocks)}"
    )


def test_every_entry_point_parses_to_a_nonempty_set():
    assert local_ps1_entries(), "build_local_exe.ps1 --add-data parsed empty"
    spec = spec_entries()
    if spec is not None:
        assert spec, "inkdoc.spec exists but its datas parsed empty"


def entry_points() -> dict[str, set[Entry]]:
    blocks = release_yml_blocks()
    points = {f"release.yml PyInstaller #{i + 1}": b for i, b in enumerate(blocks)}
    points["scripts/build_local_exe.ps1"] = local_ps1_entries()
    spec = spec_entries()
    if spec is not None:
        points["inkdoc.spec (local, untracked)"] = spec
    return points


@pytest.mark.parametrize("data_file", runtime_data_files(), ids=lambda e: e[0])
def test_runtime_data_file_is_bundled_everywhere(data_file):
    missing = [name for name, entries in entry_points().items() if data_file not in entries]
    assert not missing, (
        f"{data_file[0]} is read at runtime but not bundled by: {', '.join(missing)}. "
        f"Add it with destination '{data_file[1]}' to each, or a packaged build "
        "will not find it."
    )


def test_all_entry_points_bundle_the_same_files():
    points = entry_points()
    reference_name, reference = next(iter(points.items()))
    for name, entries in points.items():
        assert entries == reference, (
            f"{name} differs from {reference_name}:\n"
            f"  only in {name}: {sorted(entries - reference)}\n"
            f"  only in {reference_name}: {sorted(reference - entries)}"
        )


def test_bundled_sources_exist():
    """A typo in a source path would make PyInstaller abort the release build."""
    for name, entries in entry_points().items():
        for source, _dest in entries:
            assert (REPO_ROOT / source).exists(), f"{name} bundles missing path {source}"
