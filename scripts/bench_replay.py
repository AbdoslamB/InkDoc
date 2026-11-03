"""Record each engine's replies once, then replay Auto over them: a byte-for-byte check of InkDoc's own code.

Development-only. Engines and models are not deterministic across versions,
threads and machines, so a lock on their live output would fail for reasons
that have nothing to do with InkDoc. Here every call that leaves InkDoc's own
code is recorded once and replayed after:

  engines       convert_with_markitdown / _docling / _glm_ocr / _markit
  model reads   GLM-OCR on one area (read_table_region, read_text_region,
                read_formula_region), the layout
                model on some pages (find_layout_in_worker)
  this machine  which OCR engines and add-ons are installed, GLM-OCR's time
                estimates, whether Docling's enrichment model is there

Everything else runs live: the probe, routing, the missing-text check, the
table finder and every "Auto quality" step. A change to any of them shows up
as a changed Markdown or report; nothing else can.

A reply is keyed by (file, call, pages or crop) plus the options the engine
reads and any argument that changes its answer (the layout model's digest).
The file is the document's SHA-256; a part of it written for Docling
(pdf_pages.write_page_subset) is keyed by the original and its pages. A call
with no recording is a miss: replay stops that case and names the recordings
it does have for that file and call, so a router change that moves page
boundaries is flagged, never answered with stale pages.

    python scripts/bench_replay.py record [--root benchmarks] [--levels fast,balanced,thorough]
        [--only TEXT] [--fill]
    python scripts/bench_replay.py replay [--root benchmarks] [--levels ...] [--only TEXT]
        [--update-locks] [--out scratch/bench-replay]

A root holds corpus/ (the documents, in category folders), recordings/ (one
JSON per reply, in a folder per document) and baselines/ (each case's
Markdown, and locks.json with its hash and report). benchmarks/ is committed;
benchmarks/local/ is the same layout for private documents and is gitignored
(benchmarks/README.md).

record runs the real engines (they must be installed) and writes replies;
--fill keeps existing replies and records only the missing ones. replay
compares each case (document x level) with its lock: same, changed (the
Markdown diff and report diff go to --out, with the numbers kept before and
after, scripts/numeric_recall.py), new (no lock yet), miss or error. It exits
1 unless every case is the same. --update-locks writes the changed and new
cases' locks, never a miss's or an error's; a person reviews the diff of
baselines/ like any other change.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_ROOT = REPO_ROOT / "benchmarks"
LEVELS = ("fast", "balanced", "thorough")
# Next to a document: its truth (NAME.tables.json, NAME.expected.md), not a case.
NOT_CASES = (".json", ".md")

# ConversionOptions, field by field: what an engine reads (part of a reply's key),
# what a conversion writes back (recorded with the reply and put back on replay),
# and the rest, which no engine sees. tests/test_bench_replay.py fails when a
# new field is in none of them, so each one gets a decision.
ENGINE_INPUTS = (
    "enable_plugins", "keep_data_uris", "docintel_enabled", "docintel_endpoint", "cu_enabled", "cu_endpoint",
    "cu_analyzer_id", "docling_ocr", "docling_table_structure", "docling_code_enrichment",
    "docling_formula_enrichment", "allow_engine_fallback", "page_subset",
)
ENGINE_RESULTS = ("engine_used", "fallback_occurred", "fallback_reason", "engine_warnings")
NOT_ENGINE = (
    "engine", "quality_check", "auto_quality", "passes_off", "auto_decision", "quality_report", "table_report",
    "polish_counts", "save_fallback", "job",
)


# Every call that leaves InkDoc's own code, as (module, name). Each is looked up on
# its module when called (Auto imports them inside its functions), so replacing
# the module attribute is enough. write_page_subset is not answered, only
# watched: it says which pages of which document a part holds.
CALLS = (
    ("app.core.engines.markitdown_worker_client", "convert_with_markitdown"),
    ("app.core.engines.docling_engine", "convert_with_docling"),
    ("app.core.engines.glm_ocr_engine", "convert_with_glm_ocr"),
    ("app.core.engines.markit_engine", "convert_with_markit"),
    ("app.core.engines.glm_ocr_engine", "read_table_region"),
    ("app.core.engines.glm_ocr_engine", "read_text_region"),
    ("app.core.engines.glm_ocr_engine", "read_formula_region"),
    ("app.core.engines.markitdown_worker_client", "find_layout_in_worker"),
    ("app.core.auto_engine", "ocr_engines_usable"),
    ("app.core.auto_engine", "ocr_install_target"),
    ("app.core.auto_engine", "layout_model_usable"),
    ("app.core.auto_engine", "layout_model_location"),
    ("app.core.auto_engine", "_enrichment_usable"),
    ("app.core.auto_engine", "glm_estimate_for_pages"),
    ("app.core.auto_engine", "glm_estimate_seconds"),
    ("app.core.pdf_pages", "write_page_subset"),
)


class ReplayMiss(BaseException):
    """A call with no recording. A BaseException, so Auto's own fallbacks (`except Exception`)
    can't turn it into a quiet MarkItDown result."""


# ─── Keys and recordings ─────────────────────────────────────────────────────
def file_sha(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def option_inputs(options: Any) -> dict[str, Any]:
    """The engine inputs that differ from their defaults (a new option with a default changes no key)."""
    from app.core.converter import ConversionOptions

    default = ConversionOptions()
    return {name: getattr(options, name) for name in ENGINE_INPUTS
            if name != "page_subset" and getattr(options, name) != getattr(default, name)}


def _results(options: Any) -> dict[str, Any]:
    used = options.engine_used
    return {"engine_used": used.value if used is not None else None, "fallback_occurred": options.fallback_occurred,
            "fallback_reason": options.fallback_reason, "engine_warnings": list(options.engine_warnings)}


def _put_results(options: Any, changed: dict[str, Any]) -> None:
    from app.core.queue_model import EngineKind

    for name, value in changed.items():
        if name == "engine_used":
            value = EngineKind(value) if value is not None else None
        setattr(options, name, value)


def _placeholders() -> list[tuple[re.Pattern, str]]:
    """Machine paths in recorded messages, longest first: the temp folder, the repository, the home folder."""
    found = []
    for path, name in ((tempfile.gettempdir(), "<tmp>"), (str(REPO_ROOT), "<repo>"), (str(Path.home()), "<home>")):
        for variant in {path, os.path.realpath(path)}:
            for form in {variant, variant.replace("\\", "/")}:
                found.append((re.compile(re.escape(form), re.IGNORECASE if os.name == "nt" else 0), name))
    return sorted(found, key=lambda p: -len(p[0].pattern))


_PLACEHOLDERS = _placeholders()


def sanitize(value: Any) -> Any:
    """`value` with this machine's paths replaced: recordings are committed for public documents."""
    if isinstance(value, str):
        for pattern, name in _PLACEHOLDERS:
            value = pattern.sub(name, value)
        return value
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    return value


def _error_record(exc: BaseException) -> dict[str, str]:
    return {"type": f"{type(exc).__module__}:{type(exc).__qualname__}", "message": sanitize(str(exc))}


def _rebuild(error: dict[str, str]) -> Exception:
    """The recorded exception, of its own class when that class can be built from a message."""
    module, _, qualname = error["type"].partition(":")
    cls: Any = RuntimeError
    try:
        found: Any = importlib.import_module(module)
        for part in qualname.split("."):
            found = getattr(found, part)
        if isinstance(found, type) and issubclass(found, Exception):
            cls = found
    except Exception:
        pass
    try:
        return cls(error["message"])
    except Exception:
        return RuntimeError(error["message"])


def describe(key: dict[str, Any]) -> str:
    """'docling, pages 1-12 (docling_formula_enrichment=True)' for a reply's key."""
    from app.core.pdf_pages import page_runs

    text = key["call"]
    pages = key.get("pages")
    if isinstance(pages, list):
        text += f", page{'s' if len(pages) != 1 else ''} {page_runs(pages)}"
    if key.get("crop"):
        text += f", page {key['crop']['page']} area {key['crop']['bbox']}"
    extra = {**key.get("options", {}), **key.get("args", {})}
    if extra:
        text += " (" + ", ".join(f"{k}={v}" for k, v in sorted(extra.items())) + ")"
    return text


class Recorder:
    """Answers the patched calls from recordings (replay), the real code (record), or both (fill)."""

    def __init__(self, folder: Path, mode: str) -> None:
        assert mode in ("record", "fill", "replay")
        self.folder = folder
        self.mode = mode
        self.file = ""
        self.paths: dict[str, tuple[str, list[int] | None]] = {}
        self.misses: list[str] = []
        self.recorded = 0

    def start(self, path: str | Path) -> None:
        """A new case: calls are now about this document."""
        self.file = file_sha(path)
        self.paths = {_norm(path): (self.file, None)}
        self.misses = []

    def part_written(self, source: str, pages: list[int], part: str) -> None:
        """pdf_pages.write_page_subset wrote `part`, the given pages of `source`."""
        sha, _ = self.source(source)
        self.paths[_norm(part)] = (sha, list(pages))

    def source(self, path: str | Path) -> tuple[str, list[int] | None]:
        """(the document's SHA-256, its pages when `path` is a part of it)."""
        known = self.paths.get(_norm(path))
        if known is None:
            known = (file_sha(path), None)
            self.paths[_norm(path)] = known
        return known

    def key(self, call: str, *, path: str | None = None, pages: list[int] | None = None,
            crop: dict | None = None, options: Any = None, args: dict | None = None) -> dict[str, Any]:
        sha, part_pages = self.source(path) if path is not None else (self.file, None)
        key: dict[str, Any] = {"file": sha, "call": call, "pages": list(pages or part_pages or []) or "all"}
        if crop is not None:
            key["crop"] = crop
        if options is not None and option_inputs(options):
            key["options"] = option_inputs(options)
        if args:
            key["args"] = args
        return key

    def path_of(self, key: dict[str, Any]) -> Path:
        digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]
        return self.folder / key["file"][:16] / f"{key['call']}-{digest}.json"

    def near(self, key: dict[str, Any]) -> list[str]:
        """The recordings this document does have for the same call."""
        folder = self.folder / key["file"][:16]
        found = []
        for path in sorted(folder.glob(f"{key['call']}-*.json")) if folder.is_dir() else []:
            try:
                other = json.loads(path.read_text(encoding="utf-8"))["key"]
            except (OSError, ValueError, KeyError):
                continue
            if other.get("call") == key["call"]:
                # Options and arguments only where they differ from the call asked.
                shown = {k: v for k, v in other.items() if k not in ("options", "args") or v != key.get(k)}
                found.append(describe(shown))
        return found

    def call(self, key: dict[str, Any], live: Callable[[], Any], *, options: Any = None,
             encode: Callable[[Any], Any] = lambda v: v, decode: Callable[[Any], Any] = lambda v: v) -> Any:
        path = self.path_of(key)
        if self.mode != "record" and path.is_file():
            rec = json.loads(path.read_text(encoding="utf-8"))
            if options is not None:
                _put_results(options, rec.get("options_after", {}))
            if "error" in rec:
                raise _rebuild(rec["error"])
            return decode(rec["reply"])
        if self.mode == "replay":
            near = self.near(key)
            what = f"no recording for {describe(key)} of {key['file'][:16]}"
            what += f"; recorded: {'; '.join(near)}" if near else "; nothing recorded for that call"
            self.misses.append(what)
            raise ReplayMiss(what)
        before = _results(options) if options is not None else {}
        rec: dict[str, Any] = {"key": key, "meta": {"inkdoc": _version(), "recorded": date.today().isoformat()}}
        error: Exception | None = None
        try:
            value = live()
        except Exception as exc:
            error = exc
        if options is not None:
            after = _results(options)
            rec["options_after"] = sanitize({k: v for k, v in after.items() if v != before[k]})
        if error is not None:
            rec["error"] = _error_record(error)
        else:
            rec["reply"] = encode(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        self.recorded += 1
        if error is not None:
            raise error
        return value


def _version() -> str:
    from app._version import __version__

    return __version__


# ─── The calls that leave InkDoc's own code ──────────────────────────────────
def stand_ins(rec: Recorder, real: dict[str, Callable]) -> dict[str, Callable]:
    """name -> the stand-in answering it through `rec`, for every name in CALLS."""
    from app.core.queue_model import EngineKind

    def conversion(call: str, name: str) -> Callable:
        def run(file_item: Any, options: Any) -> str:
            pages = options.page_subset if call == "glm_ocr" else None
            key = rec.key(call, path=file_item.source, pages=pages, options=options)
            return rec.call(key, lambda: real[name](file_item, options), options=options)
        return run

    def markitdown(source: str, options: Any, stream_info_kwargs: dict, url: str | None) -> str:
        args = {"stream_info": dict(stream_info_kwargs or {}), "url": url} if (stream_info_kwargs or url) else None
        key = rec.key("markitdown", path=source, options=options, args=args)
        return rec.call(key, lambda: real["convert_with_markitdown"](source, options, stream_info_kwargs, url),
                        options=options)

    def table_region(path: str, page: int, bbox: list[float]) -> Any:
        crop = {"page": page, "bbox": [round(float(v), 1) for v in bbox]}
        key = rec.key("glm_ocr.table", path=path, crop=crop)
        return rec.call(key, lambda: real["read_table_region"](path, page, bbox))

    def region(kind: str, name: str) -> Callable:
        def run(path: str, page: int, bbox: list[float]) -> Any:
            crop = {"page": page, "bbox": [round(float(v), 1) for v in bbox]}
            return rec.call(rec.key(f"glm_ocr.{kind}", path=path, crop=crop), lambda: real[name](path, page, bbox))
        return run

    def layout(source: str, numbers: list[int], folder: str, digest: str, job: Any) -> dict:
        key = rec.key("layout_model", path=source, pages=list(numbers), args={"digest": digest})
        return rec.call(key, lambda: real["find_layout_in_worker"](source, numbers, folder, digest, job))

    def env(name: str, encode: Callable = lambda v: v, decode: Callable = lambda v: v) -> Callable:
        def run(*args: Any) -> Any:
            return rec.call(rec.key(f"env.{name}"), lambda: real[name](*args), encode=encode, decode=decode)
        return run

    def estimate_for_pages(pages: int) -> float | None:
        key = rec.key("env.glm_estimate_for_pages", args={"pages": pages})
        return rec.call(key, lambda: real["glm_estimate_for_pages"](pages))

    def estimate_seconds(path: str, probe: Any, pdf: bool) -> float | None:
        return rec.call(rec.key("env.glm_estimate_seconds", path=path),
                        lambda: real["glm_estimate_seconds"](path, probe, pdf))

    def location_out(v: Any) -> Any:
        # Only the digests matter (they key the layout model's replies); the folder is this machine's.
        return None if v is None else {"folder": "<layout-model>", "digests": dict(v[1])}

    def location_in(v: Any) -> Any:
        return None if v is None else (Path(v["folder"]), dict(v["digests"]))

    def write_page_subset(source: str, pages: list[int], part: str) -> Any:
        result = real["write_page_subset"](source, pages, part)
        rec.part_written(source, pages, part)
        return result

    return {
        "convert_with_markitdown": markitdown,
        "convert_with_docling": conversion("docling", "convert_with_docling"),
        "convert_with_glm_ocr": conversion("glm_ocr", "convert_with_glm_ocr"),
        "convert_with_markit": conversion("markit", "convert_with_markit"),
        "read_table_region": table_region,
        "read_text_region": region("text", "read_text_region"),
        "read_formula_region": region("formula", "read_formula_region"),
        "find_layout_in_worker": layout,
        "ocr_engines_usable": env("ocr_engines_usable", lambda v: [e.value for e in v],
                                  lambda v: [EngineKind(e) for e in v]),
        "ocr_install_target": env("ocr_install_target"),
        "layout_model_usable": env("layout_model_usable"),
        "layout_model_location": env("layout_model_location", location_out, location_in),
        "_enrichment_usable": env("_enrichment_usable"),
        "glm_estimate_for_pages": estimate_for_pages,
        "glm_estimate_seconds": estimate_seconds,
        "write_page_subset": write_page_subset,
    }


@contextmanager
def patched(rec: Recorder) -> Iterator[None]:
    """Every call in CALLS answered by `rec` while inside; the real ones put back after."""
    real = {name: getattr(importlib.import_module(module), name) for module, name in CALLS}
    answer = stand_ins(rec, real)
    try:
        for module, name in CALLS:
            setattr(importlib.import_module(module), name, answer[name])
        yield
    finally:
        for module, name in CALLS:
            setattr(importlib.import_module(module), name, real[name])


# ─── Cases ───────────────────────────────────────────────────────────────────
def corpus(root: Path, only: str = "") -> list[Path]:
    """The documents under root/corpus, in a stable order."""
    from app.core.converter import is_supported_file

    folder = root / "corpus"
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        if (path.is_file() and not any(part.startswith(".") for part in path.relative_to(folder).parts)
                and not path.name.lower().endswith(NOT_CASES) and is_supported_file(str(path))
                and only.lower() in rel.lower()):
            found.append(path)
    return found


def telemetry(options: Any) -> dict[str, Any]:
    """What Auto reports about one conversion, as the response's metadata carries it."""
    used = options.engine_used
    return sanitize({
        "engine_used": used.value if used is not None else None,
        "decision": options.auto_decision.to_dict(used) if options.auto_decision is not None else None,
        "fallback": options.fallback_reason or None,
        "warnings": list(options.engine_warnings),
        "quality": options.quality_report.to_dict() if options.quality_report is not None else None,
        "tables": options.table_report.to_dict() if options.table_report is not None else None,
    })


def run_case(path: Path, level: str) -> tuple[str, dict[str, Any]]:
    """Auto at `level` on `path`: (Markdown, telemetry)."""
    from app.core.converter import ConversionOptions, convert_item
    from app.core.queue_model import EngineKind, QueueItem, SourceKind

    options = ConversionOptions(engine=EngineKind.AUTO, auto_quality=level)
    item = QueueItem(source=str(path), kind=SourceKind.FILE, display_name=path.name, engine=EngineKind.AUTO)
    markdown = convert_item(item, options)
    return markdown, telemetry(options)


def case_id(root: Path, path: Path, level: str) -> str:
    return f"{path.relative_to(root / 'corpus').as_posix()}@{level}"


def _baseline_md(root: Path, cid: str) -> Path:
    rel, level = cid.rsplit("@", 1)
    return root / "baselines" / f"{rel}.{level}.md"


def load_locks(root: Path) -> dict[str, Any]:
    path = root / "baselines" / "locks.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save_locks(root: Path, locks: dict[str, Any]) -> None:
    path = root / "baselines" / "locks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(locks.items())), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def changed_paths(a: Any, b: Any, prefix: str = "") -> list[str]:
    """Where two reports differ: 'decision.reason', 'tables.passes'."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += changed_paths(a.get(k), b.get(k), f"{prefix}.{k}" if prefix else k)
        return out
    return [] if a == b else [prefix or "(all)"]


def numbers_delta(path: Path, before: str, after: str) -> str:
    """'numbers 95.2% -> 97.0% found, 3 -> 1 extra' (text-layer reference), or '' for a non-PDF."""
    from app.core.pdf_probe import is_pdf
    from scripts.numeric_recall import compare, markdown_numbers, pdf_numbers

    if not is_pdf(str(path)):
        return ""
    ref = pdf_numbers(path)
    a, b = compare(ref, markdown_numbers(before)), compare(ref, markdown_numbers(after))
    if not a["numbers"]:
        return ""
    return (f"numbers {100 * a['recall']:.1f}% -> {100 * b['recall']:.1f}% found, "
            f"{a['extra']} -> {b['extra']} extra (text-layer reference)")


def replay(root: Path, levels: list[str], only: str, out: Path, update: bool) -> int:
    rec = Recorder(root / "recordings", "replay")
    locks = load_locks(root)
    docs = corpus(root, only)
    if not docs:
        print(f"No documents under {root / 'corpus'}.")
        return 0
    counts: dict[str, int] = {}
    with patched(rec):
        for path in docs:
            for level in levels:
                cid = case_id(root, path, level)
                rec.start(path)
                error = ""
                try:
                    markdown, report = run_case(path, level)
                    status = "miss" if rec.misses else None
                except ReplayMiss:
                    status = "miss"
                except Exception as exc:
                    status, error = "error", f"{type(exc).__name__}: {exc}"
                if status == "miss":
                    print(f"  MISS     {cid}")
                    for what in dict.fromkeys(rec.misses):
                        print(f"           {what}")
                elif status == "error":
                    print(f"  ERROR    {cid}: {error}")
                else:
                    lock = locks.get(cid)
                    if lock is None:
                        status = "new"
                    elif lock["markdown_sha256"] == _sha(markdown) and lock["telemetry"] == report:
                        status = "same"
                    else:
                        status = "changed"
                    if status == "changed":
                        base = _baseline_md(root, cid)
                        old = base.read_text(encoding="utf-8") if base.is_file() else ""
                        diff = list(difflib.unified_diff(old.splitlines(), markdown.splitlines(), "locked", "now",
                                                         lineterm=""))
                        where = changed_paths(lock["telemetry"], report)
                        target = out / f"{cid.replace('/', '__').replace('@', '.')}.diff"
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text("\n".join(diff) + "\n\n# report\n" + json.dumps(
                            {"locked": lock["telemetry"], "now": report}, indent=1, ensure_ascii=False) + "\n",
                            encoding="utf-8")
                        added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
                        removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
                        print(f"  CHANGED  {cid}: +{added} -{removed} lines"
                              + (f"; report: {', '.join(where)}" if where else ""))
                        delta = numbers_delta(path, old, markdown)
                        if delta:
                            print(f"           {delta}")
                        print(f"           {target}")
                    elif status == "new":
                        print(f"  NEW      {cid}")
                    if update and status in ("changed", "new"):
                        base = _baseline_md(root, cid)
                        base.parent.mkdir(parents=True, exist_ok=True)
                        base.write_text(markdown, encoding="utf-8", newline="\n")
                        locks[cid] = {"markdown_sha256": _sha(markdown), "telemetry": report}
                counts[status] = counts.get(status, 0) + 1
    if update:
        save_locks(root, locks)
    print("\n" + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    if update and (counts.get("changed") or counts.get("new")):
        print("Locks updated: review the diff of baselines/ before committing it.")
        return 0 if not (counts.get("miss") or counts.get("error")) else 1
    return 0 if set(counts) <= {"same"} else 1


def record(root: Path, levels: list[str], only: str, fill: bool) -> int:
    rec = Recorder(root / "recordings", "fill" if fill else "record")
    docs = corpus(root, only)
    if not docs:
        print(f"No documents under {root / 'corpus'}.")
        return 0
    failed = 0
    with patched(rec):
        for path in docs:
            for level in levels:
                cid = case_id(root, path, level)
                rec.start(path)
                before = rec.recorded
                try:
                    run_case(path, level)
                    print(f"  {cid}: {rec.recorded - before} replies recorded")
                except Exception as exc:
                    failed += 1
                    print(f"  {cid}: failed: {type(exc).__name__}: {exc}")
    print(f"\n{rec.recorded} replies written to {root / 'recordings'}. "
          "Run `replay --update-locks` to lock the cases, then review baselines/.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "replay"):
        p = sub.add_parser(name)
        p.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="benchmarks/ or benchmarks/local/")
        p.add_argument("--levels", default=",".join(LEVELS))
        p.add_argument("--only", default="", help="only documents whose path under corpus/ contains this")
        if name == "record":
            p.add_argument("--fill", action="store_true", help="keep existing replies; record only the missing")
        else:
            p.add_argument("--update-locks", action="store_true", help="lock the changed and new cases")
            p.add_argument("--out", type=Path, default=REPO_ROOT / "scratch" / "bench-replay")
    args = parser.parse_args(argv)
    levels = [lv.strip() for lv in args.levels.split(",") if lv.strip()]
    unknown = [lv for lv in levels if lv not in LEVELS]
    if unknown:
        parser.error(f"unknown level(s): {', '.join(unknown)}")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        if args.command == "record":
            return record(args.root, levels, args.only, args.fill)
        return replay(args.root, levels, args.only, args.out, args.update_locks)
    finally:
        try:
            from app.core.engines.markitdown_worker_client import MarkItDownWorker

            MarkItDownWorker.get_instance().shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
