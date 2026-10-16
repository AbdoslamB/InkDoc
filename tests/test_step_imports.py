"""What a step of Auto quality may import: the core-or-add-on rule (AGENTS.md).

A step is core code, switchable under Settings > General > Auto quality > Steps.
Anything that needs model weights or a native runtime, adds size or startup
time, needs builds per platform, has a non-permissive license or uses the
network is a download instead (an engine or add-on the step names in
needs_all / needs_any). This test holds every module a step names in
app/core/thorough_passes.py (PassInfo.modules) to that. Such a module may import:

- the standard library, except the modules that reach the network or run
  native code;
- InkDoc's own code;
- the PDF libraries InkDoc already ships (SHIPPED_PDF_LIBRARIES), inside a
  function only, so importing a step module stays cheap. Each must be in the
  bundled-package list, LICENSES/python-packages-desktop.txt, under a license
  that is not copyleft.

The source is read, not imported: an import inside a function counts too.
"""
from __future__ import annotations

import ast
import re
import sys
import textwrap
from pathlib import Path

import pytest

from app.core.thorough_passes import PASSES

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_LIST = REPO_ROOT / "LICENSES" / "python-packages-desktop.txt"

# The libraries a step may use besides the standard library: import name ->
# distribution in the bundled-package list.
SHIPPED_PDF_LIBRARIES = {
    "pypdfium2": "pypdfium2",
    "pdfplumber": "pdfplumber",
    "pdfminer": "pdfminer.six",
}

# Standard-library modules a step must not use, and why: what needs them is an add-on.
NOT_IN_A_STEP = {
    "socket": "network",
    "ssl": "network",
    "http": "network",
    "urllib.request": "network",
    "ftplib": "network",
    "imaplib": "network",
    "poplib": "network",
    "smtplib": "network",
    "xmlrpc": "network",
    "webbrowser": "network",
    "ctypes": "native code",
    "subprocess": "native code",
}

COPYLEFT = re.compile(
    r"\b[AL]?GPL|General Public License|\bMPL\b|Mozilla Public|\bEUPL\b|\bCDDL\b|\bEPL\b|"
    r"Eclipse Public|\bSSPL\b|Commons Clause|Non-?Commercial",
    re.IGNORECASE,
)


def _imports(source: str) -> list[tuple[str, int, bool]]:
    """(dotted name, line, inside a function) for every import in `source`.

    `from a import b` gives "a.b", so `from urllib import request` is caught
    like `import urllib.request`. A relative import is InkDoc's own code.
    """
    found: list[tuple[str, int, bool]] = []

    def visit(node: ast.AST, in_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                found.extend((alias.name, child.lineno, in_function) for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                module = "app" if child.level else child.module
                found.extend((f"{module}.{alias.name}", child.lineno, in_function) for alias in child.names)
            visit(child, in_function or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))

    visit(ast.parse(source), False)
    return found


def problems_in(source: str) -> list[tuple[int, str, str]]:
    """(line, import, why) for every import the rule doesn't allow in a step module."""
    problems = []
    for name, line, in_function in _imports(source):
        top = name.split(".")[0]
        denied = next((d for d in NOT_IN_A_STEP if name == d or name.startswith(d + ".")), None)
        if denied:
            problems.append((line, name, f"{NOT_IN_A_STEP[denied]}: an add-on"))
        elif top == "app" or top in sys.stdlib_module_names:
            continue
        elif top not in SHIPPED_PDF_LIBRARIES:
            problems.append((line, name, "not a library InkDoc ships for steps: an add-on"))
        elif not in_function:
            problems.append((line, name, "at module level: import it in the function that uses it"))
    return problems


def _path(module: str) -> Path:
    return (REPO_ROOT / Path(*module.split("."))).with_suffix(".py")


def _normalise(distribution: str) -> str:
    return re.sub(r"[-_.]+", "-", distribution).lower()


def _bundled_packages() -> dict[str, str]:
    """{distribution: license} from the "Packages (N):" list at the top of BUNDLED_LIST."""
    lines = BUNDLED_LIST.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Packages ("))
    packages = {}
    for line in lines[start + 1:]:
        match = re.match(r"^  (\S+) \S+ -- (.+)$", line)
        if not match:
            break
        packages[_normalise(match.group(1))] = match.group(2)
    return packages


STEP_MODULES = sorted({module for step in PASSES for module in step.modules})


def test_every_step_names_the_modules_that_do_its_work():
    for step in PASSES:
        assert step.modules, f"{step.name}: PassInfo.modules is empty"
        for module in step.modules:
            assert module.startswith("app.") and _path(module).is_file(), f"{step.name}: {module}"


@pytest.mark.parametrize("module", STEP_MODULES)
def test_step_modules_import_only_what_the_rule_allows(module):
    assert problems_in(_path(module).read_text(encoding="utf-8")) == [], module


def test_the_libraries_steps_may_use_are_shipped_and_permissive():
    bundled = _bundled_packages()
    assert len(bundled) > 20, "the package list was read"
    for distribution in SHIPPED_PDF_LIBRARIES.values():
        license_ = bundled.get(_normalise(distribution))
        assert license_, f"{distribution} is not in {BUNDLED_LIST.name}: InkDoc doesn't ship it"
        assert not COPYLEFT.search(license_), f"{distribution}: {license_}"


def test_the_check_catches_what_the_rule_forbids():
    forbidden = textwrap.dedent("""
        import torch
        import pdfplumber
        from urllib import request
        import http.client
        import ctypes

        def convert():
            import requests
            import subprocess
    """)
    assert [name for _line, name, _why in problems_in(forbidden)] == [
        "torch", "pdfplumber", "urllib.request", "http.client", "ctypes", "requests", "subprocess",
    ]
    allowed = textwrap.dedent("""
        from __future__ import annotations
        import re
        from collections.abc import Collection
        from urllib.parse import quote
        from app.core.markdown_tables import find_table_blocks
        from . import sibling

        def read(path):
            import pdfplumber
            import pypdfium2 as pdfium
    """)
    assert problems_in(allowed) == []


@pytest.mark.parametrize("license_, copyleft", [
    ("MIT License", False),
    ("BSD-3-Clause, Apache-2.0, dependency licenses", False),
    ("MIT-CMU", False),
    ("GPL-2.0-or-later", True),
    ("LGPL-3.0-only", True),
    ("GNU Affero General Public License v3", True),
    ("MPL-2.0", True),
    ("SSPL-1.0", True),
    ("PolyForm-Noncommercial-1.0.0", True),
])
def test_copyleft_and_restricted_licenses_are_recognised(license_, copyleft):
    assert bool(COPYLEFT.search(license_)) == copyleft
