"""Measure InkDoc's startup, memory and conversion cost, and fail when one gets worse.

Run from source by CI on every push (`check`), so a change that makes InkDoc
slower to start, heavier at rest or hungrier while converting is caught when
it is made, not a release later. What it measures, in the interpreter running
it (helpers shared with scripts/runtime_bench.py):

  import_mb, import_s     InkDoc's own process: the server's modules loaded
                          (app.server.server, uvicorn), peak memory and time
  server_start_s          `main.py --headless` spawned -> /health answers (median of 3)
  worker_ready_s          MarkItDown worker spawned -> ready (median of 3)
  convert_s, tables_s     a generated 100-page PDF (prose and ruled tables) through
                          MarkItDown, then the table finder on every page (deep)
  convert_peak_mb         peak memory of the process doing that

and the SHA-256 of what that conversion and table search produced, so two
environments can be shown to give the same output (scripts/optimize_audit.py
uses it to try package upgrades). The output digests are reported, never
checked here: a deliberate change of output is the tests' business.

    python scripts/perf_baseline.py check             # measure, compare with the baseline
    python scripts/perf_baseline.py measure [--json FILE]
    python scripts/perf_baseline.py update            # measure and store as the baseline

The baseline is kept per kind of machine: CI's runners ("ci-linux",
"ci-windows", "ci-macos") in scripts/perf_baseline.json, a developer's own
("local-win32", ...) in scratch/perf_baseline.local.json, which stays local. A machine with no baseline yet, or one measured under a
different Python minor version, is measured and reported but not checked; CI
prints the entry to add. Margins are generous (CI runners are noisy, guides/
auto-quality.md, "Give timing assertions room on CI"): memory may grow by
MEMORY_MARGIN, times by TIME_MARGIN plus TIME_SLACK_S. Nothing is written to
~/Downloads: servers run with their home folder in a temporary sandbox.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

BASELINE = REPO_ROOT / "scripts" / "perf_baseline.json"            # CI's runners
LOCAL_BASELINE = REPO_ROOT / "scratch" / "perf_baseline.local.json"  # a developer's own (gitignored)
STARTS = 3
MEMORY_MARGIN = 1.25    # memory may grow by a quarter
TIME_MARGIN = 1.75      # times may grow by three quarters...
TIME_SLACK_S = 1.0      # ...plus a second
MEMORY_METRICS = ("import_mb", "convert_peak_mb")
TIME_METRICS = ("import_s", "server_start_s", "worker_ready_s", "convert_s", "tables_s")


# ─── The workload (in a child process, so its peak memory is its own) ────────
def _child_imports() -> None:
    from scripts.runtime_bench import peak_memory_mb

    started = time.perf_counter()
    import uvicorn  # noqa: F401  # what the server runs on

    import app.server.server  # noqa: F401
    print(json.dumps({"import_s": time.perf_counter() - started, "import_mb": peak_memory_mb()}))


def _child_convert(pdf: str) -> None:
    from app.core.converter import ConversionOptions, run_markitdown
    from app.core.table_finder import find_tables
    from scripts.runtime_bench import MIXED_PAGES, _digest, peak_memory_mb

    started = time.perf_counter()
    markdown = run_markitdown(pdf, ConversionOptions(), {}, None)
    convert_s = time.perf_counter() - started
    started = time.perf_counter()
    found = find_tables(pdf, list(range(1, MIXED_PAGES + 1)), deep=True, time_budget_s=600)
    tables_s = time.perf_counter() - started
    found.pop("elapsed_ms", None)
    print(json.dumps({
        "convert_s": convert_s, "tables_s": tables_s, "convert_peak_mb": peak_memory_mb(),
        "outputs": {"markdown": _digest(markdown), "tables": _digest(json.dumps(found, sort_keys=True))},
    }))


def _child(*args: str) -> dict[str, Any]:
    res = subprocess.run([sys.executable, __file__, *args], capture_output=True, text=True, cwd=REPO_ROOT,
                         encoding="utf-8", timeout=900)
    if res.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {res.stderr.strip()[-2000:]}")
    return json.loads(res.stdout.strip().splitlines()[-1])


# ─── Measuring ───────────────────────────────────────────────────────────────
def machine() -> str:
    """The baseline this machine is compared with."""
    runner = os.environ.get("RUNNER_OS") if os.environ.get("GITHUB_ACTIONS") == "true" else None
    return f"ci-{runner.lower()}" if runner else f"local-{sys.platform}"


def measure() -> dict[str, Any]:
    from scripts.runtime_bench import _server, _stop, _worker_starts, make_mixed_pdf

    result: dict[str, Any] = {"python": f"{sys.version_info.major}.{sys.version_info.minor}"}
    result.update(_child("_imports"))
    starts = []
    with tempfile.TemporaryDirectory(prefix="inkdoc-perf-") as tmp:
        for n in range(STARTS):
            proc, _port, elapsed = _server(Path(tmp) / f"s{n}")
            _stop(proc)
            starts.append(elapsed)
        result["server_start_s"] = statistics.median(starts)
        result["worker_ready_s"] = statistics.median(_worker_starts(STARTS))
        pdf = make_mixed_pdf(Path(tmp) / "mixed.pdf")
        result.update(_child("_convert", str(pdf)))
    for key in (*MEMORY_METRICS, *TIME_METRICS):
        result[key] = round(result[key], 1 if key.endswith("_mb") else 2)
    return result


# ─── Comparing (pure; tests/test_perf_baseline.py) ───────────────────────────
def limit(metric: str, base: float) -> float:
    if metric in MEMORY_METRICS:
        return round(base * MEMORY_MARGIN, 1)
    return round(base * TIME_MARGIN + TIME_SLACK_S, 2)


def compare(current: dict[str, Any], base: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """(lines of the report, the metrics over their limit)."""
    if not base:
        return ["no baseline for this machine yet: measured only"], []
    if base.get("python") != current.get("python"):
        return [f"the baseline was measured with Python {base.get('python')}, this is "
                f"{current.get('python')}: measured only"], []
    lines, over = [], []
    for metric in (*MEMORY_METRICS, *TIME_METRICS):
        if metric not in base:
            lines.append(f"{metric:16} {current[metric]:>9}   (no baseline)")
            continue
        top = limit(metric, base[metric])
        bad = current[metric] > top
        change = (current[metric] - base[metric]) / base[metric] * 100 if base[metric] else 0.0
        lines.append(f"{metric:16} {current[metric]:>9}   baseline {base[metric]:>9}  ({change:+.0f}%)  "
                     f"limit {top}{'   OVER' if bad else ''}")
        if bad:
            over.append(metric)
    if base.get("outputs") and base["outputs"] != current.get("outputs"):
        lines.append("outputs          differ from the baseline's (expected after a change of output; "
                     "update the baseline with it)")
    return lines, over


def load(path: Path = BASELINE) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"machines": {}}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_imports"]:
        _child_imports()
        return 0
    if argv[:1] == ["_convert"]:
        _child_convert(argv[1])
        return 0
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("check", "measure", "update"))
    parser.add_argument("--json", type=Path, help="measure: also write the measurement here")
    parser.add_argument("--baseline", type=Path, help="the baseline file (default: by machine, see above)")
    args = parser.parse_args(argv)

    current = measure()
    key = machine()
    if args.baseline is None:
        args.baseline = BASELINE if key.startswith("ci-") else LOCAL_BASELINE
    if args.action == "measure":
        text = json.dumps(current, indent=2)
        print(text)
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(text, encoding="utf-8")
        return 0
    data = load(args.baseline)
    if args.action == "update":
        data.setdefault("machines", {})[key] = current
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"Baseline for {key} written to {args.baseline}")
        return 0
    lines, over = compare(current, data.get("machines", {}).get(key))
    print(f"Performance against the {key} baseline")
    for line in lines:
        print(f"  {line}")
    if key not in data.get("machines", {}) or over:
        print(f"\nTo make this the {key} baseline, put this entry under \"machines\" in "
              f"{args.baseline.relative_to(REPO_ROOT) if args.baseline.is_relative_to(REPO_ROOT) else args.baseline}:")
        print(json.dumps({key: current}, indent=2))
    if over:
        print(f"\nOver the limit: {', '.join(over)}. Find out why (scripts/optimize_audit.py profile), "
              "or update the baseline if the cost is meant.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
