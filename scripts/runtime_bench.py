"""Compare Python interpreters on InkDoc's own workloads (the bundled-Python upgrade check).

Development-only. Run it with each interpreter under test, each in its own
virtual environment with the same dependency versions (pip's -c constraints),
then compare. Nothing is written to ~/Downloads: conversions run in-process or
against a headless server whose home folder is a temporary sandbox. Results
go to --out (default: scratch/runtime-bench, which is gitignored).

    python scripts/runtime_bench.py corpus                  # once, any interpreter
    python scripts/runtime_bench.py matrix [LABEL=]PY_A ...  # alternates them, --rounds times
    python scripts/runtime_bench.py report                  # medians, ratios, output equality

`run` (what `matrix` calls) measures, in the interpreter running it:
  convert:*     MarkItDown's conversion of each corpus file (run_markitdown, in-process)
  tables:*      the table finder on the ruled-table PDF (lines, and deep)
  polish        text_polish.restore_typography (InkDoc's own pure Python)
  magika        magika's file-type check of every corpus file
  native        pdfium extracting the text of every page of the 100-page PDF: a few
                ctypes calls per page, the rest native code. The control: it should not
                move between interpreters; if it does, the machine was noisy.
  probe         pdf_probe.probe_pdf: pdfium too, but one ctypes call per page object,
                so it also measures the interpreter's ctypes overhead.
  worker_start  MarkItDown worker spawn -> ready
  server_start  `main.py --headless` spawn -> /health 200
  http:*        POST /convert/file against that server (Fast, and Auto at Balanced)
  peak_mb       peak memory of a process converting the 100-page PDF and finding its tables
and the SHA-256 of every conversion's Markdown, so `report` can show that each
interpreter produced the same output.

The corpus is generated (tests/_pdf_fixtures.py, tests/_table_fixtures.py,
python-docx, openpyxl, python-pptx), never anyone's documents. python-docx is
not an InkDoc dependency: install it in the benchmark environments only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_OUT = REPO_ROOT / "scratch" / "runtime-bench"
CONVERT_FILES = ("prose.pdf", "tables.pdf", "mixed.pdf", "fixtures.pdf",
                 "report.docx", "sheet.xlsx", "deck.pptx", "page.html")
VERSIONED = ("markitdown", "pdfminer.six", "pdfplumber", "pypdfium2", "magika", "onnxruntime",
             "numpy", "pandas", "lxml", "fastapi", "uvicorn", "pydantic", "pythonnet", "pywebview")


# ─── Corpus ──────────────────────────────────────────────────────────────────

def _table_cells(rows: int, cols: int, seed: int) -> list[list[str | None]]:
    head = ["Region", "Quarter", "Revenue", "Cost", "Margin", "Units", "Growth", "Share"][:cols]
    body = [[f"R{seed}-{r}"] + [str((seed * 131 + r * 17 + c * 7) % 9973) for c in range(1, cols)]
            for r in range(rows - 1)]
    return [head, *body]


def _table_page(i: int, rows: int, top: float, lines: int) -> dict:
    from tests._pdf_fixtures import drawn_page, prose_page
    from tests._table_fixtures import grid_table

    ops = grid_table(40, top, _table_cells(rows, 6, i), [88] * 6)
    return drawn_page(ops, lines=prose_page(lines * 10, offset=i)["lines"])


MIXED_PAGES = 100


def make_mixed_pdf(path: Path) -> Path:
    """MIXED_PAGES pages, prose and ruled tables in turn (also scripts/perf_baseline.py's workload)."""
    from tests._pdf_fixtures import prose_page, write_pdf

    return write_pdf(path, [
        prose_page(400, offset=i) if i % 2 else _table_page(i, 10, 520, 20) for i in range(MIXED_PAGES)
    ])


def make_corpus(out: Path) -> Path:
    from tests._pdf_fixtures import prose_page, words, write_pdf
    from tests._table_fixtures import FIXTURES

    folder = out / "corpus"
    folder.mkdir(parents=True, exist_ok=True)

    write_pdf(folder / "prose.pdf", [prose_page(400, offset=i * 7) for i in range(50)])
    write_pdf(folder / "tables.pdf", [_table_page(i, 16, 690, 5) for i in range(30)])
    make_mixed_pdf(folder / "mixed.pdf")
    fixture_pages: list[dict] = []
    for build in FIXTURES.values():
        fixture_pages.extend(build()[0])
    write_pdf(folder / "fixtures.pdf", fixture_pages)

    import docx  # python-docx, benchmark environments only

    document = docx.Document()
    for i in range(50):
        document.add_heading(f"Section {i + 1}", level=1)
        for j in range(6):
            document.add_paragraph(words(60, offset=i * 6 + j))
        table = document.add_table(rows=6, cols=4)
        for r, row in enumerate(_table_cells(6, 4, i)):
            for c, text in enumerate(row):
                table.cell(r, c).text = text or ""
        document.add_page_break()
    document.save(folder / "report.docx")

    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    for row in _table_cells(2000, 8, 3):  # 2,000 rows x 8 columns: 16,000 cells
        sheet.append(row)
    for n in range(2):
        extra = book.create_sheet(f"Sheet{n + 2}")
        for row in _table_cells(250, 8, n):
            extra.append(row)
    book.save(folder / "sheet.xlsx")

    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    for i in range(30):
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = f"Slide {i + 1}: {words(5, offset=i)}"
        box = slide.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(9), Inches(1.5))
        box.text_frame.text = words(40, offset=i)
        rows = _table_cells(6, 5, i)
        shape = slide.shapes.add_table(6, 5, Inches(0.5), Inches(3.2), Inches(9), Inches(3))
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                shape.table.cell(r, c).text = text or ""
    deck.save(folder / "deck.pptx")

    parts = ["<!doctype html><html><head><title>Bench page</title></head><body>"]
    i = 0
    while sum(len(p) for p in parts) < 1_000_000:
        parts.append(f"<h2>Part {i}</h2><p>{words(80, offset=i)}</p><ul>"
                     + "".join(f"<li><a href='#x{i}-{k}'>{words(6, offset=k)}</a></li>" for k in range(5))
                     + "</ul><table><tr>" + "".join(f"<th>{h}</th>" for h in _table_cells(1, 5, i)[0])
                     + "</tr>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
                                         for row in _table_cells(6, 5, i)[1:]) + "</table>")
        i += 1
    parts.append("</body></html>")
    (folder / "page.html").write_text("".join(parts), encoding="utf-8")

    digests = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())}
    (out / "corpus.json").write_text(json.dumps(digests, indent=2), encoding="utf-8")
    return folder


# ─── Measuring ───────────────────────────────────────────────────────────────

def _timed(fn, repeat: int) -> tuple[list[float], Any]:
    result = fn()  # warm-up, also the result whose digest is kept
    times = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        times.append(time.perf_counter() - start)
    return times, result


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def peak_memory_mb() -> float:
    """Peak memory of this process so far, in MiB."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        return counters.PeakWorkingSetSize / 2**20
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 2**20 if sys.platform == "darwin" else peak / 2**10  # bytes on macOS, KiB on Linux


def _in_process(corpus: Path, repeat: int) -> tuple[dict, dict]:
    from app.core.converter import ConversionOptions, run_markitdown
    from app.core.pdf_probe import probe_pdf
    from app.core.table_finder import find_tables
    from app.core.text_polish import restore_typography

    workloads: dict[str, dict] = {}
    digests: dict[str, str] = {}
    options = ConversionOptions()
    for name in CONVERT_FILES:
        path = str(corpus / name)
        times, text = _timed(lambda p=path: run_markitdown(p, options, {}, None), repeat)
        workloads[f"convert:{name}"] = {"times": times}
        digests[f"convert:{name}"] = _digest(text)

    tables_pdf = str(corpus / "tables.pdf")
    pages = list(range(1, 31))
    for label, deep in (("lines", False), ("deep", True)):
        times, found = _timed(lambda d=deep: find_tables(tables_pdf, pages, deep=d, time_budget_s=600), repeat)
        found.pop("elapsed_ms", None)
        workloads[f"tables:{label}"] = {"times": times}
        digests[f"tables:{label}"] = _digest(json.dumps(found, sort_keys=True))

    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(corpus / "prose.pdf"))
    try:
        page_texts = [page.get_textpage().get_text_range() for page in document]
    finally:
        document.close()
    prose_md = run_markitdown(str(corpus / "prose.pdf"), options, {}, None)
    times, polished = _timed(lambda: restore_typography(prose_md, page_texts), repeat)
    workloads["polish"] = {"times": times}
    digests["polish"] = _digest(polished[0])

    from magika import Magika

    magika = Magika()
    paths = [corpus / name for name in CONVERT_FILES]
    times, labels = _timed(lambda: [str(magika.identify_path(p).output.label) for p in paths], repeat)
    workloads["magika"] = {"times": times}
    digests["magika"] = _digest(",".join(labels))

    times, _ = _timed(lambda: probe_pdf(str(corpus / "mixed.pdf")), repeat)
    workloads["probe"] = {"times": times}

    def extract_text() -> str:
        document = pdfium.PdfDocument(str(corpus / "mixed.pdf"))
        try:
            return "".join(page.get_textpage().get_text_range() for page in document)
        finally:
            document.close()

    times, _ = _timed(extract_text, repeat)
    workloads["native"] = {"times": times}
    return workloads, digests


def _worker_starts(starts: int) -> list[float]:
    times = []
    for _ in range(starts):
        start = time.perf_counter()
        proc = subprocess.Popen([sys.executable, str(REPO_ROOT / "main.py"), "--markitdown-worker"],
                                cwd=REPO_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        try:
            for line in proc.stdout:
                if json.loads(line).get("status") == "ready":
                    times.append(time.perf_counter() - start)
                    break
            else:
                raise RuntimeError("the MarkItDown worker exited before it was ready")
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return times


def _upload(port: int, path: Path, query: str) -> tuple[int, dict]:
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() \
        + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/convert/file?{query}", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(request, timeout=600) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as err:
        return err.code, {"error": err.read().decode("utf-8", "replace")[:300]}


def _server(sandbox: Path) -> tuple[subprocess.Popen, int, float]:
    """Start `main.py --headless` with its home in `sandbox`; return it, its port and the time to /health."""
    home = sandbox / "home"
    (home / "Downloads").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
               INKDOC_ENGINES_DIR=str(sandbox / "engines"))
    env.pop("INKDOC_SESSION_TOKEN", None)
    port = _free_port()
    start = time.perf_counter()
    proc = subprocess.Popen([sys.executable, str(REPO_ROOT / "main.py"), "--headless", "--port", str(port)],
                            cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = start + 120
    while time.perf_counter() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"main.py --headless exited with {proc.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as res:
                if res.status == 200:
                    return proc, port, time.perf_counter() - start
        except OSError:
            pass
        time.sleep(0.02)
    proc.kill()
    raise RuntimeError("main.py --headless did not answer /health within 120 s")


def _stop(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":  # the MarkItDown worker is in the server's Job Object
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _over_http(corpus: Path, starts: int, repeat: int) -> tuple[dict, dict]:
    workloads: dict[str, dict] = {"server_start": {"times": []}}
    digests: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="inkdoc-bench-") as tmp:
        for n in range(starts):
            proc, port, elapsed = _server(Path(tmp) / f"s{n}")
            workloads["server_start"]["times"].append(elapsed)
            if n < starts - 1:
                _stop(proc)
        try:  # the last server stays up for the conversions
            cases = {"http:fast:tables.pdf": ("tables.pdf", "engine=markitdown"),
                     "http:balanced:tables.pdf": ("tables.pdf", "engine=auto&auto_quality=balanced"),
                     "http:fast:report.docx": ("report.docx", "engine=markitdown")}
            for key, (name, query) in cases.items():
                full = f"{query}&response_format=json&save_to_downloads=false"
                times, (status, payload) = _timed(lambda n=name, q=full: _upload(port, corpus / n, q), repeat)
                if status != 200:
                    raise RuntimeError(f"{key}: HTTP {status}: {payload}")
                workloads[key] = {"times": times}
                digests[key] = _digest(payload.get("markdown", ""))
        finally:
            _stop(proc)
    return workloads, digests


def run(out: Path, label: str, repeat: int, starts: int) -> Path:
    import importlib.metadata as metadata

    corpus = out / "corpus"
    if not (out / "corpus.json").is_file():
        raise SystemExit(f"No corpus in {out}: run `corpus` first.")
    expected = json.loads((out / "corpus.json").read_text(encoding="utf-8"))
    for name, digest in expected.items():
        if hashlib.sha256((corpus / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"{name} differs from corpus.json; regenerate the corpus.")

    workloads, digests = _in_process(corpus, repeat)
    workloads["worker_start"] = {"times": _worker_starts(starts)}
    http_workloads, http_digests = _over_http(corpus, starts, repeat)
    workloads.update(http_workloads)
    digests.update(http_digests)
    peak = subprocess.run([sys.executable, __file__, "_peak", str(corpus / "mixed.pdf")],
                          capture_output=True, text=True, check=True, cwd=REPO_ROOT)
    versions = {}
    for name in VERSIONED:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    result = {
        "label": label, "python": sys.version, "implementation": platform.python_implementation(),
        "platform": platform.platform(), "versions": versions, "peak_mb": float(peak.stdout.strip()),
        "workloads": workloads, "digests": digests,
    }
    folder = out / "results"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    return path


def _peak(pdf: str) -> None:
    from app.core.converter import ConversionOptions, run_markitdown
    from app.core.table_finder import find_tables

    run_markitdown(pdf, ConversionOptions(), {}, None)
    find_tables(pdf, list(range(1, MIXED_PAGES + 1)), deep=True, time_budget_s=600)
    print(f"{peak_memory_mb():.1f}")


# ─── Reporting ───────────────────────────────────────────────────────────────

def report(out: Path) -> str:
    runs: dict[str, list[dict]] = {}
    for path in sorted((out / "results").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        runs.setdefault(data["label"], []).append(data)
    if not runs:
        raise SystemExit(f"No results in {out / 'results'}.")
    labels = list(runs)
    base = labels[0]
    names = list(runs[base][0]["workloads"])

    def pooled(label: str, name: str) -> list[float]:
        return [t for r in runs[label] for t in r["workloads"].get(name, {}).get("times", [])]

    lines = ["Rounds: " +", ".join(f"{label} x{len(runs[label])}" for label in labels),
             *(f"  {label}: {runs[label][0]['python'].split()[0]}" for label in labels), ""]
    head = f"{'workload':28s}" + "".join(f"{label + ' ms':>22s}" for label in labels)
    lines += [head + "".join(f"{label + '/' + base:>14s}" for label in labels[1:]), "-" * len(head)]
    for name in names:
        row, medians = f"{name:28s}", {}
        for label in labels:
            times = pooled(label, name)
            if not times:
                row += f"{'-':>22s}"
                continue
            q = statistics.quantiles(times, n=4) if len(times) > 1 else [times[0]] * 3
            medians[label] = q[1]
            row += f"{q[1] * 1000:>10.1f} ({(q[2] - q[0]) * 1000:>6.1f} IQR)"
        for label in labels[1:]:
            if label in medians and base in medians:
                row += f"{medians[label] / medians[base]:>14.3f}"
        lines.append(row)
    peaks = "  ".join(f"{label}: {statistics.median(r['peak_mb'] for r in runs[label]):.0f} MiB"
                      for label in labels)
    lines += ["", f"Peak memory (100-page PDF): {peaks}", ""]

    keys = sorted({k for label in labels for r in runs[label] for k in r["digests"]})
    differing = []
    for key in keys:
        seen = {r["digests"].get(key) for label in labels for r in runs[label]}
        if len(seen) > 1:
            per = {label: sorted({(r["digests"].get(key) or "-")[:12] for r in runs[label]}) for label in labels}
            differing.append(f"  {key}: {per}")
    lines.append("Output: identical across every run" if not differing
                 else "Output DIFFERS:\n" + "\n".join(differing))
    version_rows = []
    for name in VERSIONED:
        seen = {label: runs[label][0]["versions"].get(name) for label in labels}
        if len(set(seen.values())) > 1:
            version_rows.append(f"  {name}: {seen}")
    lines.append("Dependency versions: identical" if not version_rows
                 else "Dependency versions that differ:\n" + "\n".join(version_rows))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("corpus", help="generate the documents")
    run_p = sub.add_parser("run", help="measure in this interpreter")
    run_p.add_argument("--label", default=f"py{sys.version_info.major}{sys.version_info.minor}")
    run_p.add_argument("--repeat", type=int, default=5, help="timed runs per in-process workload")
    run_p.add_argument("--starts", type=int, default=5, help="worker and server starts per round")
    matrix_p = sub.add_parser("matrix", help="alternate several interpreters, round by round")
    matrix_p.add_argument("pythons", nargs="+", metavar="[LABEL=]PYTHON",
                          help="interpreters; LABEL= names one (default: pyXY)")
    matrix_p.add_argument("--rounds", type=int, default=3)
    matrix_p.add_argument("--repeat", type=int, default=5)
    matrix_p.add_argument("--starts", type=int, default=5)
    sub.add_parser("report", help="compare the results")
    peak_p = sub.add_parser("_peak")
    peak_p.add_argument("pdf")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(errors="backslashreplace")

    if args.command == "corpus":
        print(f"Corpus in {make_corpus(args.out)}")
    elif args.command == "run":
        print(f"Results in {run(args.out, args.label, args.repeat, args.starts)}")
    elif args.command == "matrix":
        for n in range(args.rounds):
            for spec in args.pythons:
                label, _, python = spec.rpartition("=")
                print(f"round {n + 1}/{args.rounds}: {label or python}", flush=True)
                subprocess.run([python, __file__, "--out", str(args.out), "run",
                                "--repeat", str(args.repeat), "--starts", str(args.starts),
                                *(["--label", label] if label else [])],
                               check=True, cwd=REPO_ROOT)
        print(report(args.out))
    elif args.command == "report":
        print(report(args.out))
    elif args.command == "_peak":
        _peak(args.pdf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
