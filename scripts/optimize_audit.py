"""Look for ways to make InkDoc lighter or faster, with measured evidence. Changes nothing.

Development-only, suggestions only: every finding is a candidate for a person
to weigh, with the numbers it rests on and the project rules any change must
respect. Nothing here edits the repository or the environment it runs in;
trials happen in throwaway virtual environments under --out.

    python scripts/optimize_audit.py packages [--trial NAME[==VERSION] ...] [--tests]
    python scripts/optimize_audit.py profile [FILE.pdf]
    python scripts/optimize_audit.py size [--bundle DIR|RELEASE.zip] [--compare size.json]
    python scripts/optimize_audit.py runtime
    python scripts/optimize_audit.py all

packages  Direct requirements with a newer release, and known advisories
          (pip-audit, when installed). --trial installs requirements.txt in one
          fresh environment and the same with NAME upgraded in another, then
          compares scripts/perf_baseline.py's measurements and output digests
          (and with --tests the whole suite, as CI runs it). Needs the network.
profile   Times and traces one conversion's stages (probe, MarkItDown, missing-
          text check, table stage, typography) in InkDoc's own code, the
          libraries and native code; names InkDoc functions taking at least
          PROFILE_MIN_SHARE of the time. The default document is a generated
          100-page PDF; a file of your own stays on this machine.
size      The largest parts of a release .zip or a bundle folder (default
          dist-local/onedir/inkdoc, a local build, which also packs whatever this
          environment has installed), what grew since a previous audit's
          size.json, and the UI's largest files.
runtime   Python's support dates for the bundled version and the source floor
          (python.org's release calendar, else endoflife.date), and the newest
          MarkItDown, Docling and pywebview on PyPI against what InkDoc pins.
          Needs the network.

A candidate is never a verdict: a change it suggests keeps every feature only
if the full test suite passes and perf_baseline's output digests stay the same.
The report goes to --out/<time>/ (default scratch/optimize-audit, gitignored):
report.md to read, report.json to compare.
"""
from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import os
import pstats
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_OUT = REPO_ROOT / "scratch" / "optimize-audit"
DEFAULT_BUNDLE = REPO_ROOT / "dist-local" / "onedir" / "inkdoc"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
# The test tools CI installs next to requirements.txt (.github/workflows/ci.yml).
TEST_TOOLS = ("pytest==9.1.1", "httpx==0.28.1", "httpx2==2.13.0")

PROFILE_MIN_SHARE = 0.05   # an InkDoc function worth a look takes this share of the time
SIZE_MIN_MB = 5.0          # a bundle part worth a look is this large...
SIZE_MIN_SHARE = 0.05      # ...and this share of the bundle (the rest are listed, not suggested)
GROWTH_MIN_MB = 1.0        # ...or grew this much since the last audit
UI_MIN_KB = 200            # a UI file worth a look
EOL_SOON_DAYS = 365        # a Python version this close to its end of life needs a plan

# What a change must respect (AGENTS.md and the tests that hold it).
STEP_RULE = ("a step module: only the standard library and the PDF libraries InkDoc ships, imported "
             "inside a function (tests/test_step_imports.py)")
LIGHT_RULE = "InkDoc's own process never imports markitdown, pdfplumber or onnxruntime (tests/test_startup_imports.py)"
WORKER_RULE = "runs in the MarkItDown worker, where pdfplumber and onnxruntime live"
NATIVE_RULE = ("compiled code needs a build per platform, which makes it a download, not core "
               "(AGENTS.md, Core or add-on)")
FLOOR_RULE = "source must keep running on the source floor's Python (CI's source-floor job)"
SAME_OUTPUT_RULE = "the full test suite passes and scripts/perf_baseline.py's output digests stay the same"
WORKER_MODULES = ("app.core.table_finder", "app.core.engines.layout_model", "app.core.engines.markitdown_worker")

PYPI = "https://pypi.org/pypi/{}/json"
# Python's own release calendar first (python.org), endoflife.date if it can't be read.
RELEASE_CYCLE = "https://peps.python.org/api/release-cycle.json"
ENDOFLIFE = "https://endoflife.date/api/python.json"


def candidate(audit: str, title: str, evidence: str, *, rules: list[str] | tuple[str, ...] = (),
              where: list[str] | tuple[str, ...] = (), kind: str = "candidate") -> dict[str, Any]:
    """One finding. kind: "candidate" (worth trying), "info" (context), "error" (the audit couldn't look)."""
    return {"audit": audit, "kind": kind, "title": title, "evidence": evidence, "rules": list(rules),
            "where": list(where)}


def _fetch_json(url: str, timeout: float = 20.0) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "InkDoc optimize_audit (development)"})
    with urllib.request.urlopen(request, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def _major(version: str) -> str:
    return version.split(".", 1)[0]


# ─── Packages ────────────────────────────────────────────────────────────────
def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(text: str) -> list[dict[str, Any]]:
    """Each requirement: its name, the spec, the version it is pinned to ("" when not exact), and the
    comment above it."""
    out, comment = [], []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            comment.append(stripped.lstrip("# ").strip())
            continue
        if not stripped:
            comment = []
            continue
        match = re.match(r"([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(.*)", stripped)
        if match:
            spec = match.group(3).strip()
            out.append({"name": _canonical(match.group(1)), "line": stripped, "extras": match.group(2) or "",
                        "spec": spec, "pinned": spec[2:] if spec.startswith("==") else "", "why": " ".join(comment)})
        comment = []
    return out


def dependabot_major_ignores(text: str) -> set[str]:
    """Packages whose major updates .github/dependabot.yml leaves to a person."""
    ignored, current = set(), None
    for line in text.splitlines():
        name = re.search(r'dependency-name:\s*"?([^"\s]+)', line)
        if name:
            current = _canonical(name.group(1))
        elif current and "semver-major" in line:
            ignored.add(current)
    return ignored


def package_candidates(reqs: list[dict], outdated: list[dict], major_ignored: set[str]) -> list[dict]:
    direct = {r["name"]: r for r in reqs}
    out, indirect = [], []
    for pkg in sorted(outdated, key=lambda p: _canonical(p["name"])):
        name = _canonical(pkg["name"])
        have, latest = pkg["version"], pkg["latest_version"]
        req = direct.get(name)
        if req is None:
            indirect.append(f"{name} {have} -> {latest}")
            continue
        pinned = req["pinned"]
        current = pinned or have
        rules = [SAME_OUTPUT_RULE]
        if pinned:
            rules.insert(0, f"pinned exactly in requirements.txt: {req['why'] or 'no reason given'}")
        if _major(current) != _major(latest):
            rules.append("a major version: read its changelog for removed behaviour"
                         + ("; Dependabot leaves these to a person" if name in major_ignored else ""))
        evidence = f"requirements.txt: {req['line']}"
        if pinned and have != pinned:
            # What this environment measures is then another version than the release ships.
            evidence += f"; this environment has {have}, not the pinned {pinned}"
        out.append(candidate("packages", f"{name} {current} -> {latest}", evidence, rules=rules,
                             where=["requirements.txt"]))
    if indirect:
        out.append(candidate("packages", f"{len(indirect)} packages that come with others are behind",
                             "; ".join(indirect[:15]) + ("; ..." if len(indirect) > 15 else ""),
                             rules=["moved by upgrading the package that needs them, not on their own"], kind="info"))
    return out


def _outdated(python: str) -> list[dict]:
    res = subprocess.run([python, "-m", "pip", "list", "--outdated", "--format=json", "--disable-pip-version-check"],
                         capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[-500:] or "pip list failed")
    return json.loads(res.stdout or "[]")


def _advisories(python: str) -> list[dict] | None:
    """Known vulnerabilities in the installed packages (pip-audit), or None when it isn't installed."""
    probe = subprocess.run([python, "-c", "import pip_audit"], capture_output=True)
    if probe.returncode != 0:
        return None
    res = subprocess.run([python, "-m", "pip_audit", "--format", "json", "--progress-spinner", "off"],
                         capture_output=True, text=True, timeout=900)
    data = json.loads(res.stdout or "{}")
    deps = data.get("dependencies", data if isinstance(data, list) else [])
    return [d for d in deps if d.get("vulns")]


def trial_requirements(text: str, upgrades: list[str]) -> str:
    """requirements.txt with each NAME[==VERSION] upgrade in place of its line (or added)."""
    lines = text.splitlines()
    for spec in upgrades:
        name = _canonical(re.split(r"[=<>!~ ]", spec, maxsplit=1)[0])
        _name, _eq, version = spec.partition("==")
        for i, line in enumerate(lines):
            req = parse_requirements(line)
            if req and req[0]["name"] == name:
                lines[i] = f"{name}{req[0]['extras']}{'==' + version if version else ''}"
                break
        else:
            lines.append(f"{name}=={version}" if version else name)
    return "\n".join(lines) + "\n"


def _venv(folder: Path, requirements: str, with_tests: bool) -> str:
    """A fresh environment with `requirements` installed; returns its interpreter. Reused when unchanged."""
    python = folder / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    marker = folder / "inkdoc-requirements.txt"
    wanted = requirements + ("\n# tests\n" + "\n".join(TEST_TOOLS) if with_tests else "")
    if python.is_file() and marker.is_file() and marker.read_text(encoding="utf-8") == wanted:
        return str(python)
    subprocess.run([sys.executable, "-m", "venv", "--clear", str(folder)], check=True)
    req_file = folder / "requirements.txt"
    req_file.write_text(requirements, encoding="utf-8")
    subprocess.run([str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", str(req_file),
                    *(TEST_TOOLS if with_tests else ())], check=True, timeout=3600)
    marker.write_text(wanted, encoding="utf-8")
    return str(python)


def _measure_in(python: str, out: Path) -> dict[str, Any]:
    subprocess.run([python, str(REPO_ROOT / "scripts" / "perf_baseline.py"), "measure", "--json", str(out)],
                   cwd=REPO_ROOT, check=True, capture_output=True, timeout=1800)
    return json.loads(out.read_text(encoding="utf-8"))


def _tests_in(python: str) -> tuple[bool, str]:
    res = subprocess.run([python, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"], cwd=REPO_ROOT,
                         capture_output=True, text=True, timeout=3600)
    return res.returncode == 0, (res.stdout.strip().splitlines() or [""])[-1]


def compare_trial(base: dict[str, Any], trial: dict[str, Any]) -> tuple[str, bool]:
    """(one line per measurement, whether the output digests are the same)."""
    from scripts.perf_baseline import MEMORY_METRICS, TIME_METRICS

    parts = []
    for metric in (*MEMORY_METRICS, *TIME_METRICS):
        a, b = base.get(metric), trial.get(metric)
        if a is None or b is None:
            continue
        change = (b - a) / a * 100 if a else 0.0
        parts.append(f"{metric} {a} -> {b} ({change:+.0f}%)")
    return "; ".join(parts), base.get("outputs") == trial.get("outputs")


def run_trial(upgrades: list[str], out: Path, with_tests: bool) -> dict[str, Any]:
    text = REQUIREMENTS.read_text(encoding="utf-8")
    folder = out.parent / "venvs"
    tag = hashlib.sha256(" ".join(sorted(upgrades)).encode()).hexdigest()[:8]
    print(f"  installing requirements.txt, and again with {', '.join(upgrades)}, in fresh environments ...",
          flush=True)
    # Two independent installs, mostly waiting on the network: side by side. The measuring
    # below stays one at a time, so neither run slows the other.
    with ThreadPoolExecutor(max_workers=2) as pool:
        base_job = pool.submit(_venv, folder / "base", text, with_tests)
        trial_job = pool.submit(_venv, folder / f"trial-{tag}", trial_requirements(text, upgrades), with_tests)
        base_py, trial_py = base_job.result(), trial_job.result()
    print("  measuring both ...", flush=True)
    base = _measure_in(base_py, out / "trial-base.json")
    trial = _measure_in(trial_py, out / "trial.json")
    numbers, same = compare_trial(base, trial)
    tests = ""
    if with_tests:
        print("  running the test suite in both ...", flush=True)
        ok_base, tail_base = _tests_in(base_py)
        ok_trial, tail_trial = _tests_in(trial_py)
        tests = f"; tests: before {tail_base or ('passed' if ok_base else 'failed')}, after " \
                f"{tail_trial or ('passed' if ok_trial else 'failed')}"
    evidence = numbers + f"; output {'identical' if same else 'DIFFERENT'}" + tests
    return candidate("packages", f"trial: {', '.join(upgrades)}", evidence,
                     rules=[SAME_OUTPUT_RULE] + ([] if same else ["the output changed: find out why before adopting"]),
                     where=["requirements.txt"], kind="candidate" if same else "info")


def audit_packages(out: Path, trials: list[str], with_tests: bool) -> list[dict]:
    found: list[dict] = []
    reqs = parse_requirements(REQUIREMENTS.read_text(encoding="utf-8"))
    dependabot = REPO_ROOT / ".github" / "dependabot.yml"
    ignores = dependabot_major_ignores(dependabot.read_text(encoding="utf-8")) if dependabot.is_file() else set()
    try:
        found += package_candidates(reqs, _outdated(sys.executable), ignores)
    except Exception as exc:
        found.append(candidate("packages", "couldn't list newer releases", str(exc), kind="error"))
    advisories = _advisories(sys.executable)
    if advisories is None:
        found.append(candidate("packages", "advisories not checked", "pip-audit isn't installed here: "
                               "python -m pip install pip-audit", kind="info"))
    for dep in advisories or []:
        for vuln in dep["vulns"]:
            fixed = ", ".join(vuln.get("fix_versions") or []) or "no fixed version yet"
            found.append(candidate("packages", f"advisory {vuln.get('id')}: {dep['name']} {dep['version']}",
                                   f"fixed in {fixed}", rules=[SAME_OUTPUT_RULE], where=["requirements.txt"]))
    if trials:
        try:
            found.append(run_trial(trials, out, with_tests))
        except Exception as exc:
            found.append(candidate("packages", f"trial of {', '.join(trials)} failed", str(exc)[-800:], kind="error"))
    return found


# ─── Profile ─────────────────────────────────────────────────────────────────
def _paths(filename: str, repo: Path) -> tuple[str, str]:
    """(a profiled file's path, the repository's), both with forward slashes; the repository's ends in /."""
    return filename.replace("\\", "/"), str(repo).replace("\\", "/").rstrip("/") + "/"


def group_of(filename: str, repo: Path = REPO_ROOT) -> str:
    """Whose code a profiled function is: InkDoc's, a library's (by package) or Python's."""
    path, root = _paths(filename, repo)
    if path.startswith("~") or path.startswith("<"):
        return "builtin"
    if "/site-packages/" in path:
        return "lib:" + path.split("/site-packages/", 1)[1].split("/", 1)[0].split(".")[0]
    if path.startswith(root + "app/"):
        return "inkdoc"
    return "stdlib"


def module_of(filename: str, repo: Path = REPO_ROOT) -> str:
    """InkDoc's module name, a library's path inside site-packages, or the path as it is."""
    path, root = _paths(filename, repo)
    if path.startswith(root):
        return path[len(root):].removesuffix(".py").replace("/", ".")
    return path.split("/site-packages/", 1)[1] if "/site-packages/" in path else path


def module_rules(module: str) -> list[str]:
    from app.core.thorough_passes import PASSES

    rules = []
    if any(module in p.modules for p in PASSES):
        rules.append(STEP_RULE)
    rules.append(WORKER_RULE if module in WORKER_MODULES else LIGHT_RULE)
    return rules + [SAME_OUTPUT_RULE]


def profile_candidates(profile: dict[str, Any]) -> list[dict]:
    """Candidates from a profile's numbers (pure; tests/test_optimize_audit.py)."""
    total = profile["total_s"] or 1e-9
    groups = profile["groups"]
    ours = groups.get("inkdoc", 0.0) / total
    libs = sorted(((g[4:], t) for g, t in groups.items() if g.startswith("lib:")), key=lambda x: -x[1])[:5]
    stages = ", ".join(f"{s['name']} {s['seconds']:.2f} s / {s['peak_mb']:.0f} MB" for s in profile["stages"])
    out = [candidate("profile", f"InkDoc's own Python is {ours:.0%} of the time",
                     f"{total:.2f} s in all under the profiler, which slows everything: the shares are what "
                     f"count; stages (time / Python's own allocations): {stages}; libraries: "
                     + ", ".join(f"{n} {t / total:.0%}" for n, t in libs)
                     + f"; Python and builtins {(groups.get('stdlib', 0) + groups.get('builtin', 0)) / total:.0%}",
                     rules=[f"another language could save at most this {ours:.0%}", NATIVE_RULE], kind="info")]
    for fn in profile["functions"]:
        if fn["share"] >= PROFILE_MIN_SHARE:
            out.append(candidate(
                "profile", f"{fn['module']}.{fn['function']} takes {fn['share']:.0%} of the time",
                f"{fn['own_s']:.2f} s in the function itself, {fn['calls']} calls; line {fn['line']}",
                rules=module_rules(fn["module"]), where=[f"{fn['module'].replace('.', '/')}.py:{fn['line']}"]))
    biggest = max(profile["stages"], key=lambda s: s["peak_mb"], default=None)
    if biggest:
        top = "; ".join(f"{a['where']} {a['mb']:.1f} MB" for a in biggest.get("allocations", [])[:5])
        out.append(candidate("profile", f"Python's own allocations peak in {biggest['name']} at "
                             f"{biggest['peak_mb']:.0f} MB",
                             f"held at its end: {top or 'nothing large'}; native libraries (pdfium, onnxruntime) "
                             "allocate outside Python: the process's whole peak is perf_baseline's convert_peak_mb",
                             kind="info"))
    return out


def _profile_child(pdf: str, out_json: str) -> None:
    """Run each stage twice: once under cProfile (time), once under tracemalloc (memory)."""
    import tracemalloc

    from app.core.converter import ConversionOptions, run_markitdown
    from app.core.pdf_probe import probe_pdf
    from app.core.quality_check import assess
    from app.core.table_finder import find_tables
    from app.core.table_stage import run_table_stage
    from app.core.text_polish import restore_typography

    def find(path, pages, _job, **kwargs):
        return find_tables(path, pages, **kwargs)

    state: dict[str, Any] = {}

    def probe():
        state["probe"] = probe_pdf(pdf)

    def markitdown():
        state["md"] = run_markitdown(pdf, ConversionOptions(), {}, None)

    def quality():
        assess(pdf, state["md"], "markitdown", probe=state["probe"])

    def tables():
        run_table_stage(pdf, state["md"], state["probe"], repair_allowed=True, find=find, deep=True)

    def polish():
        restore_typography(state["md"], [p.text for p in state["probe"].pages])

    stages = [("probe", probe), ("markitdown", markitdown), ("missing-text check", quality),
              ("table stage", tables), ("typography", polish)]
    profiler = cProfile.Profile()
    timings = []
    for name, fn in stages:
        started = time.perf_counter()
        profiler.enable()
        fn()
        profiler.disable()
        timings.append((name, time.perf_counter() - started))
    stats = pstats.Stats(profiler).stats  # type: ignore[attr-defined]
    total = sum(t for _, t in timings)
    groups: dict[str, float] = {}
    functions = []
    for (filename, line, func), (_cc, calls, own, _cum, _callers) in stats.items():
        group = group_of(filename)
        groups[group] = groups.get(group, 0.0) + own
        if group == "inkdoc":
            functions.append({"module": module_of(filename), "function": func, "line": line, "calls": calls,
                              "own_s": own, "share": own / total if total else 0.0})
    functions.sort(key=lambda f: -f["own_s"])

    memory = []
    tracemalloc.start()
    for _name, fn in stages:
        tracemalloc.reset_peak()
        fn()
        _now, peak = tracemalloc.get_traced_memory()
        snap = tracemalloc.take_snapshot().statistics("lineno")[:8]
        memory.append({"peak_mb": peak / 2**20, "allocations": [
            {"where": f"{module_of(s.traceback[0].filename)}:{s.traceback[0].lineno}", "mb": s.size / 2**20}
            for s in snap]})
    tracemalloc.stop()
    result = {
        "file": Path(pdf).name, "total_s": total, "groups": groups, "functions": functions[:25],
        "stages": [{"name": n, "seconds": t, **m} for (n, t), m in zip(timings, memory, strict=True)],
    }
    Path(out_json).write_text(json.dumps(result, indent=1), encoding="utf-8")


def audit_profile(out: Path, pdf: Path | None) -> list[dict]:
    if pdf is None:
        from scripts.runtime_bench import make_mixed_pdf

        pdf = make_mixed_pdf(out / "profile-100-pages.pdf")
    result = out / "profile.json"
    res = subprocess.run([sys.executable, __file__, "_profile", str(pdf), str(result)], cwd=REPO_ROOT,
                         capture_output=True, text=True, timeout=3600)
    if res.returncode != 0:
        return [candidate("profile", "the profile run failed", res.stderr.strip()[-800:], kind="error")]
    return profile_candidates(json.loads(result.read_text(encoding="utf-8")))


# ─── Size ────────────────────────────────────────────────────────────────────
def _group_key(parts: tuple[str, ...]) -> str:
    """The part of a bundle a file belongs to: its top-level folder or file under _internal (or the root)."""
    if parts and parts[0] == "_internal" and len(parts) > 1:
        parts = parts[1:]
    key = parts[0]
    return key.split("-")[0] + " (metadata)" if key.endswith(".dist-info") else key


def size_groups(bundle: Path) -> dict[str, int]:
    """Bytes per part of a PyInstaller bundle: a folder, or a release zip (sizes unpacked)."""
    groups: dict[str, int] = {}
    if bundle.is_file():
        import zipfile

        with zipfile.ZipFile(bundle) as archive:
            entries = [(Path(i.filename).parts, i.file_size) for i in archive.infolist() if not i.is_dir()]
        tops = {parts[0] for parts, _size in entries if len(parts) > 1}
        strip = len(tops) == 1 and all(len(parts) > 1 for parts, _size in entries)  # one folder holding it all
        for parts, size in entries:
            key = _group_key(parts[1:] if strip else parts)
            groups[key] = groups.get(key, 0) + size
        return groups
    for dirpath, _dirs, files in os.walk(bundle):
        rel = Path(dirpath).relative_to(bundle).parts
        for name in files:
            key = _group_key((*rel, name))
            try:
                groups[key] = groups.get(key, 0) + (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
    return groups


def size_candidates(groups: dict[str, int], previous: dict[str, int] | None) -> list[dict]:
    mb = 2**20
    total = sum(groups.values()) or 1
    out = []
    largest = sorted(groups.items(), key=lambda kv: -kv[1])
    out.append(candidate("size", f"the bundle is {total / mb:.0f} MB unpacked, in {len(groups)} parts",
                         "; ".join(f"{k} {v / mb:.1f} MB" for k, v in largest[:15]),
                         rules=["the release files' limits are in release.yml (Enforce artifact size limits)"],
                         kind="info"))
    big = [(k, v) for k, v in largest if v / mb >= SIZE_MIN_MB and v / total >= SIZE_MIN_SHARE]
    for key, size in big:
        out.append(candidate("size", f"{key}: {size / mb:.1f} MB ({size / total:.0%})",
                             "find out what needs it and whether every part is used: PyInstaller's "
                             "--exclude-module and --collect-* options in release.yml decide what goes in",
                             rules=[SAME_OUTPUT_RULE, "every frozen artifact's smoke test passes"],
                             where=[".github/workflows/release.yml", "scripts/build_local_exe.ps1"]))
    if previous is not None:
        grown = sorted(((k, v - previous.get(k, 0)) for k, v in groups.items()), key=lambda kv: -kv[1])
        grown = [(k, d) for k, d in grown if d / mb >= GROWTH_MIN_MB]
        before = sum(previous.values())
        out.append(candidate("size", f"since the last audit: {(total - before) / mb:+.1f} MB",
                             "; ".join(f"{k} {d / mb:+.1f} MB" for k, d in grown[:10]) or "no part grew by "
                             f"{GROWTH_MIN_MB:.0f} MB or more", kind="candidate" if grown else "info"))
    return out


def ui_candidates(ui: Path = REPO_ROOT / "app" / "ui") -> list[dict]:
    big = sorted(((p, p.stat().st_size) for p in ui.rglob("*") if p.is_file() and p.stat().st_size >= UI_MIN_KB * 1024),
                 key=lambda x: -x[1])
    if not big:
        return []
    return [candidate("size", f"{len(big)} UI files of {UI_MIN_KB} KB or more",
                      "; ".join(f"{p.relative_to(REPO_ROOT).as_posix()} {s / 1024:.0f} KB" for p, s in big[:10]),
                      rules=["UI libraries are bundled and pinned, never fetched at runtime (guides/ui.md)"],
                      kind="info")]


def audit_size(out: Path, bundle: Path | None, compare: Path | None) -> list[dict]:
    found: list[dict] = []
    path = bundle or DEFAULT_BUNDLE
    if path.is_dir() or path.suffix.lower() == ".zip":
        groups = size_groups(path)
        (out / "size.json").write_text(json.dumps(groups, indent=1, sort_keys=True), encoding="utf-8")
        previous = json.loads(compare.read_text(encoding="utf-8")) if compare else None
        found += size_candidates(groups, previous)
        if path.is_dir():
            # A local build packs what this environment has installed (Docling and torch, say);
            # the release files are built in a clean one from requirements.txt.
            found.append(candidate("size", "this is a local build", f"{path}: it holds whatever this environment "
                                   "has installed; for what users download, audit a release file "
                                   "(--bundle inkdoc-windows.zip)", kind="info"))
    elif path.is_file():
        found.append(candidate("size", "can't read this kind of file", f"{path}: give a bundle folder or a release "
                               ".zip (the AppImage and the disk image are not read)", kind="error"))
    else:
        found.append(candidate("size", "no bundle to measure", f"{path} doesn't exist: download a release .zip, "
                               "build one with scripts/build_local_exe.ps1, or pass --bundle", kind="info"))
    return found + ui_candidates()


# ─── Runtime ─────────────────────────────────────────────────────────────────
def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split(".") if x.isdigit())


def python_versions(release_yml: str, ci_yml: str) -> tuple[str | None, str | None]:
    """(the bundled Python, the source floor) as release.yml and ci.yml name them."""
    bundled = re.findall(r'python-version:\s*"(\d+\.\d+)"', release_yml)
    every = re.findall(r'python-version:\s*"(\d+\.\d+)"', ci_yml)
    floor = min(every, key=_version_key) if every else None
    return (bundled[0] if bundled else None), floor


def _end_of_month(value: str) -> str:
    """'2030-10' -> '2030-10-31'; a full date is kept."""
    if re.fullmatch(r"\d{4}-\d{2}", value):
        import calendar

        year, month = map(int, value.split("-"))
        return f"{value}-{calendar.monthrange(year, month)[1]:02d}"
    return value


def release_cycles(data: Any) -> list[dict]:
    """python.org's release calendar ({"3.14": {"first_release", "end_of_life", ...}}) or endoflife.date's
    list, as [{"cycle", "releaseDate", "eol"}]."""
    if isinstance(data, list):
        return data
    return [{"cycle": cycle, "releaseDate": _end_of_month(info.get("first_release") or "") or None,
             "eol": _end_of_month(info.get("end_of_life") or "") or None}
            for cycle, info in data.items()]


def python_candidates(cycles: list[dict], bundled: str | None, floor: str | None, today: date) -> list[dict]:
    by = {c["cycle"]: c for c in cycles}
    out = []
    for label, version, where in (("bundled", bundled, ".github/workflows/release.yml"),
                                  ("source floor", floor, ".github/workflows/ci.yml")):
        info = by.get(version or "")
        if not info or not isinstance(info.get("eol"), str):
            continue
        eol = date.fromisoformat(info["eol"])
        days = (eol - today).days
        rules = ["guides/release.md, Interpreters"] if label == "bundled" else [
            FLOOR_RULE, "raising it changes what the README promises; ruff's target-version moves with it"]
        if days < 0:
            out.append(candidate("runtime", f"Python {version} ({label}) reached its end of life on {eol}",
                                 f"no security fixes for {-days} days", rules=rules, where=[where]))
        elif days <= EOL_SOON_DAYS:
            out.append(candidate("runtime", f"Python {version} ({label}) ends on {eol}", f"{days} days left",
                                 rules=rules, where=[where]))
    released = [c["cycle"] for c in cycles if re.fullmatch(r"\d+\.\d+", str(c.get("cycle", "")))
                and c.get("releaseDate") and date.fromisoformat(c["releaseDate"]) <= today]
    newest = max(released, key=_version_key, default=None)
    if bundled and newest and _version_key(newest) > _version_key(bundled):
        out.append(candidate("runtime", f"Python {newest} is out (InkDoc ships {bundled})",
                             "move in autumn, after scripts/runtime_bench.py shows no regression",
                             rules=["guides/release.md, Interpreters", SAME_OUTPUT_RULE],
                             where=[".github/workflows/release.yml", ".github/workflows/ci.yml"]))
    return out


def audit_runtime() -> list[dict]:
    found: list[dict] = []
    bundled, floor = python_versions(
        (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"),
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    errors = []
    for url in (RELEASE_CYCLE, ENDOFLIFE):
        try:
            cycles = release_cycles(_fetch_json(url))
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue
        found += python_candidates(cycles, bundled, floor, date.today())
        break
    else:
        found.append(candidate("runtime", "couldn't read Python's support dates", "; ".join(errors), kind="error"))

    reqs = {r["name"]: r for r in parse_requirements(REQUIREMENTS.read_text(encoding="utf-8"))}
    pins = {name: reqs[name]["pinned"] for name in ("markitdown", "pywebview") if reqs.get(name, {}).get("pinned")}
    try:
        from scripts.build_addon import pack_docling_versions, pinned_docling_version

        pins["docling"] = pinned_docling_version()
        shipped = sorted({v for v in pack_docling_versions().values() if v})
    except Exception as exc:
        shipped = []
        found.append(candidate("runtime", "couldn't read Docling's pin", str(exc), kind="error"))
    rules = {
        "markitdown": [SAME_OUTPUT_RULE, "every conversion goes through its public API (AGENTS.md)"],
        "pywebview": ["move it only together with app/desktop/webview_bridge.py (tests/test_webview_bridge.py)",
                      "run inkdoc --selftest-bridge on every platform"],
        "docling": ["a new engine pack: scripts/build_pack.py, a docling-pack-vN tag, then manifest.json "
                    "(guides/engines.md)"],
    }
    for name, pinned in pins.items():
        try:
            latest = _fetch_json(PYPI.format(name))["info"]["version"]
        except Exception as exc:
            found.append(candidate("runtime", f"couldn't ask PyPI about {name}", str(exc), kind="error"))
            continue
        extra = f"; the published pack runs {', '.join(shipped)}" if name == "docling" and shipped else ""
        if latest != pinned:
            found.append(candidate("runtime", f"{name} {latest} is out (InkDoc pins {pinned})",
                                   f"https://pypi.org/project/{name}/{extra}", rules=rules[name],
                                   where=["requirements.txt" if name != "docling" else "scripts/docling-constraints.txt"]))
    found.append(candidate("runtime", "GLM-OCR is tracked by its own workflow",
                           ".github/workflows/glm-ocr-track.yml checks upstream and opens a PR", kind="info"))
    return found


# ─── The report ──────────────────────────────────────────────────────────────
TITLES = {"packages": "Packages", "profile": "Profile", "size": "Size", "runtime": "Runtime"}
KINDS = {"candidate": (0, "Candidate"), "error": (1, "Couldn't check"), "info": (2, "Context")}  # order, label


def render(found: list[dict]) -> str:
    lines = ["# Optimization audit", "", f"{date.today()} - suggestions only; nothing was changed. A candidate keeps "
             f"every feature only if {SAME_OUTPUT_RULE}.", ""]
    for audit, title in TITLES.items():
        items = [f for f in found if f["audit"] == audit]
        if not items:
            continue
        lines += [f"## {title}", ""]
        for f in sorted(items, key=lambda f: KINDS[f["kind"]][0]):
            lines.append(f"- **{KINDS[f['kind']][1]}: {f['title']}**  ")
            lines.append(f"  {f['evidence']}")
            if f["where"]:
                lines.append(f"  Where: {', '.join(f['where'])}")
            for rule in f["rules"]:
                lines.append(f"  Must respect: {rule}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_profile"]:
        _profile_child(argv[1], argv[2])
        return 0
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("audit", choices=("packages", "profile", "size", "runtime", "all"))
    parser.add_argument("file", nargs="?", type=Path, help="profile: the PDF to profile (default: a generated one)")
    parser.add_argument("--trial", action="append", default=[], metavar="NAME[==VERSION]",
                        help="packages: try this upgrade in a throwaway environment (repeatable)")
    parser.add_argument("--tests", action="store_true", help="packages --trial: also run the whole test suite")
    parser.add_argument("--bundle", type=Path, help=f"size: a bundle folder or a release .zip (default {DEFAULT_BUNDLE.relative_to(REPO_ROOT)})")
    parser.add_argument("--compare", type=Path, help="size: a previous audit's size.json")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    if args.file is not None and not args.file.is_file():
        parser.error(f"{args.file} doesn't exist")
    if args.compare is not None and not args.compare.is_file():
        parser.error(f"{args.compare} doesn't exist")

    out = args.out / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    found: list[dict] = []
    wanted = ("packages", "profile", "size", "runtime") if args.audit == "all" else (args.audit,)
    for audit in wanted:
        print(f"{audit} ...", flush=True)
        if audit == "packages":
            found += audit_packages(out, args.trial, args.tests)
        elif audit == "profile":
            found += audit_profile(out, args.file)
        elif audit == "size":
            found += audit_size(out, args.bundle, args.compare)
        else:
            found += audit_runtime()
    (out / "report.json").write_text(json.dumps(found, indent=1), encoding="utf-8")
    text = render(found)
    (out / "report.md").write_text(text, encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    print()
    print(text)
    print(f"Report: {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
