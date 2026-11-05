"""AGENTS.md and the guides in guides/ stay complete and consistent with the repository.

AGENTS.md is read on every task; each guide only before work in its own area, found through
AGENTS.md's routing table. That works only while the table covers every file, every path the
documents name still exists, and every guide can be reached. These tests check that structure.
They can't check that a description is still true: whoever changes the code updates its guide.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS = REPO_ROOT / "AGENTS.md"
GUIDES_DIR = REPO_ROOT / "guides"
GUIDES_README = GUIDES_DIR / "README.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Every file under these must have a guide.
ROUTED = ("app/", "main.py")
# A code span that is a path under one of these must exist in the repository.
CHECKED_PREFIXES = ("app/", "tests/", "scripts/", "examples/", "docs/", "guides/", "LICENSES/",
                    ".github/")
GUIDE_SECTIONS = ["Rules", "How it works", "Tests", "Lessons"]
LESSON_FIELDS = ["When", "Lesson", "Evidence"]

FENCE = re.compile(r"^```.*?^```[^\n]*$", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
PATH_SPAN = re.compile(r"[A-Za-z0-9_./-]+")


def repo_files() -> list[str]:
    """Tracked files plus new ones not ignored, as posix paths; a plain walk without git."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=REPO_ROOT, capture_output=True, check=True,
        ).stdout.decode("utf-8")
        files = [f for f in out.split("\0") if f]
        # A file deleted in the work tree but not yet staged is still listed.
        return [f for f in files if (REPO_ROOT / f).exists()]
    except (OSError, subprocess.CalledProcessError):
        files = []
        for root, dirs, names in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__"}]
            for name in names:
                if not name.endswith(".pyc"):
                    files.append((Path(root) / name).relative_to(REPO_ROOT).as_posix())
        return files


def prose(path: Path) -> str:
    """A Markdown file without its fenced code blocks (examples, not content)."""
    return FENCE.sub("", path.read_text(encoding="utf-8"))


def guide_files() -> list[Path]:
    return sorted(p for p in GUIDES_DIR.glob("*.md") if p.name != "README.md")


def section(text: str, heading: str, level: int = 2) -> str:
    """The body of the first `## heading` section, up to the next heading of that level."""
    marks = "#" * level
    match = re.search(rf"^{marks} {re.escape(heading)}\s*$(.*?)(?=^{marks} |\Z)", text,
                      re.MULTILINE | re.DOTALL)
    assert match, f"no '{marks} {heading}' section"
    return match.group(1)


def routing_table() -> dict[str, list[str]]:
    """AGENTS.md's routing table: guide path -> the file patterns it covers."""
    routes: dict[str, list[str]] = {}
    for line in section(prose(AGENTS), "Guides").splitlines():
        if not line.startswith("|"):
            continue
        cells = line.strip().strip("|").split("|")
        target = re.search(r"\]\((guides/[\w-]+\.md)\)", cells[0])
        if target and len(cells) >= 2:
            routes[target.group(1)] = CODE_SPAN.findall(cells[1])
    assert routes, "AGENTS.md's Guides table has no rows"
    return routes


def covers(pattern: str, path: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return fnmatch.fnmatchcase(path, pattern)


def heading_anchors(path: Path) -> set[str]:
    """GitHub's anchors for a Markdown file's headings."""
    anchors = set()
    for title in re.findall(r"^#{1,6} (.+?)\s*#*$", prose(path), re.MULTILINE):
        slug = re.sub(r"[^\w\- ]", "", title.strip().lower()).replace(" ", "-")
        anchors.add(slug)
    return anchors


def documents() -> list[Path]:
    return [AGENTS, GUIDES_README, *guide_files()]


def test_every_file_under_app_has_a_guide():
    patterns = [p for ps in routing_table().values() for p in ps]
    unrouted = [f for f in repo_files()
                if f.startswith(ROUTED) and not any(covers(p, f) for p in patterns)]
    assert not unrouted, (
        "No guide covers these files. Add each to a row of AGENTS.md's Guides table and "
        f"describe it in that guide: {unrouted}"
    )


def test_every_route_still_matches_a_file():
    files = repo_files()
    stale = [p for ps in routing_table().values() for p in ps
             if not any(covers(p, f) for f in files)]
    assert not stale, f"These routes in AGENTS.md's Guides table match no file: {stale}"


def test_every_guide_is_routed_and_listed():
    routed = set(routing_table())
    listed = {f"guides/{t}" for t in LINK.findall(prose(GUIDES_README)) if t.endswith(".md")}
    for guide in guide_files():
        name = f"guides/{guide.name}"
        assert name in routed, f"{name} is not in AGENTS.md's Guides table, so no one reads it"
        assert name in listed, f"{name} is not listed in guides/README.md"
    for name in routed:
        assert (REPO_ROOT / name).is_file(), f"AGENTS.md routes to a missing guide: {name}"


@pytest.mark.parametrize("doc", documents(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_named_paths_exist(doc):
    paths = [span for span in CODE_SPAN.findall(prose(doc))
             if span.startswith(CHECKED_PREFIXES) and PATH_SPAN.fullmatch(span)]
    missing = [p for p in paths if not (REPO_ROOT / p).exists()]
    assert not missing, f"{doc.name} names paths that don't exist: {missing}"


@pytest.mark.parametrize("doc", documents(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_links_resolve(doc):
    broken = []
    for target in LINK.findall(prose(doc)):
        if re.match(r"[a-z]+:", target):
            continue  # http(s), mailto: not checked here
        file_part, _, anchor = target.partition("#")
        dest = (doc.parent / file_part).resolve() if file_part else doc
        anchor_missing = anchor and dest.suffix == ".md" and anchor not in heading_anchors(dest)
        if not dest.exists() or anchor_missing:
            broken.append(target)
    assert not broken, f"{doc.name} has broken links: {broken}"


@pytest.mark.parametrize("guide", guide_files(), ids=lambda p: p.name)
def test_guide_has_the_standard_sections(guide):
    headings = re.findall(r"^## (.+?)\s*$", prose(guide), re.MULTILINE)
    assert headings == GUIDE_SECTIONS, (
        f"{guide.name} must have exactly these sections, in order: {GUIDE_SECTIONS}; "
        f"it has {headings}"
    )


@pytest.mark.parametrize("guide", guide_files(), ids=lambda p: p.name)
def test_lessons_are_complete(guide):
    lessons = re.split(r"^### ", section(prose(guide), "Lessons"), flags=re.MULTILINE)[1:]
    for lesson in lessons:
        title = lesson.splitlines()[0].strip()
        for field in LESSON_FIELDS:
            match = re.search(rf"^- \*\*{field}:\*\*\s*(\S.*)$", lesson, re.MULTILINE)
            assert match, f"{guide.name}, lesson '{title}': no '- **{field}:**' line with text"


def test_ci_runs_every_node_test():
    in_ci = set(re.findall(r"node tests/([\w-]+\.test\.js)", CI_WORKFLOW.read_text("utf-8")))
    on_disk = {p.name for p in (REPO_ROOT / "tests").glob("*.test.js")}
    assert on_disk - in_ci == set(), f"ci.yml doesn't run these Node tests: {on_disk - in_ci}"
    assert in_ci - on_disk == set(), f"ci.yml runs Node tests that don't exist: {in_ci - on_disk}"
