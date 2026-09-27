"""The pack build's check of the enrichment add-on's worker contract.

scripts/build_pack.py:check_enrichment_worker_contract runs inside the post-build
smoke test of an engine pack -- a one-hour job across three runners. A mistake in
it would surface only there, so it is driven here against stub workers that
answer the same line-delimited JSON protocol as app/core/engines/worker.py, with
each stub breaking the contract in one specific way.

The passing case against a real pack interpreter is not reproducible in CI (it
needs a built pack); it was run against the installed docling-pack-v5 interpreter
with the repository's worker.py when this check was written.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.build_addon import pinned_docling_version  # noqa: E402
from scripts.build_pack import (  # noqa: E402
    MINIMAL_PDF_BYTES,
    check_enrichment_worker_contract,
)

PINNED = pinned_docling_version()
MODEL_DIR = json.loads(
    (REPO_ROOT / "app" / "core" / "addons.json").read_text(encoding="utf-8")
)["addons"]["code_enrichment"]["model_dir"]

# A worker that honours the contract unless told otherwise through its config
# file. Written out per test because the smoke test launches workers with -I,
# which isolates them from this process's sys.path.
STUB_WORKER = textwrap.dedent('''
    import json, sys
    from pathlib import Path

    cfg = json.loads(Path(__file__).with_suffix(".json").read_text())
    log = Path(__file__).with_suffix(".log")

    for line in sys.stdin:
        req = json.loads(line)
        with log.open("a") as fh:
            fh.write(req.get("action", "") + "\\n")

        if req["action"] == "capabilities":
            if cfg.get("caps_error"):
                resp = {"status": "error", "error": "Docling is not importable"}
            else:
                resp = {
                    "status": "ok",
                    "docling_version": cfg["docling_version"],
                    "artifacts_path": cfg["artifacts_path"],
                    "hf_offline": True,
                    "code_formula": {
                        "model_dir": cfg["model_dir"],
                        "present": cfg["loadable"],
                        "loadable": cfg["loadable"],
                        "detail": "",
                    },
                }
        elif req["action"] == "convert":
            if cfg.get("write_output"):
                Path(req["output_file"]).write_text("# converted anyway")
            if cfg.get("convert_anyway"):
                resp = {"status": "ok", "job_id": req["job_id"]}
            else:
                resp = {
                    "status": "error",
                    "job_id": req["job_id"],
                    "error_code": "enrichment_model_unavailable",
                    "error": "model missing",
                }
        else:
            resp = {"status": "error", "error": "unknown action"}
        print(json.dumps(resp), flush=True)
''')


def run_contract(tmp_path: Path, **overrides):
    models = tmp_path / "models"
    models.mkdir()
    cfg = {
        "docling_version": PINNED,
        "artifacts_path": str(models),
        "model_dir": MODEL_DIR,
        "loadable": False,
    }
    cfg.update(overrides)

    worker = tmp_path / "stub_worker.py"
    worker.write_text(STUB_WORKER, encoding="utf-8")
    worker.with_suffix(".json").write_text(json.dumps(cfg), encoding="utf-8")

    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(MINIMAL_PDF_BYTES)

    # The inherited environment, as the real smoke test passes: an empty one
    # stops Python starting on Windows (no SYSTEMROOT).
    caps = check_enrichment_worker_contract(
        [sys.executable, "-I", str(worker)], dict(os.environ), models, pdf, tmp_path
    )
    log = worker.with_suffix(".log")
    actions = log.read_text(encoding="utf-8").split() if log.exists() else []
    return caps, actions


def test_a_worker_honouring_the_contract_passes(tmp_path):
    caps, actions = run_contract(tmp_path)
    assert caps["docling_version"] == PINNED
    assert actions == ["capabilities", "convert"], "refusal must actually be exercised"


def test_capability_rpc_failure_fails_the_build(tmp_path):
    with pytest.raises(RuntimeError, match="Capability RPC failed"):
        run_contract(tmp_path, caps_error=True)


def test_unpinned_docling_fails_the_build(tmp_path):
    """The constraints file was not applied, or was changed without a rebuild."""
    with pytest.raises(RuntimeError, match="constraints were not applied"):
        run_contract(tmp_path, docling_version="9.9.9")


def test_renamed_model_directory_fails_the_build(tmp_path):
    """The failure this check exists for: Docling looks somewhere the add-on
    does not install to, so every install would download 640 MB for nothing."""
    with pytest.raises(RuntimeError, match="fail to load it"):
        run_contract(tmp_path, model_dir="docling-project--CodeFormulaV3")


def test_wrong_artifacts_path_fails_the_build(tmp_path):
    with pytest.raises(RuntimeError, match="artifacts_path"):
        run_contract(tmp_path, artifacts_path=str(tmp_path / "elsewhere"))


def test_converting_instead_of_refusing_fails_the_build(tmp_path):
    """A worker that silently drops enrichment must never ship."""
    with pytest.raises(RuntimeError, match="not refused"):
        run_contract(tmp_path, convert_anyway=True)


def test_refusing_but_writing_output_fails_the_build(tmp_path):
    with pytest.raises(RuntimeError, match="still wrote output"):
        run_contract(tmp_path, write_output=True)


def test_refusal_check_is_skipped_when_the_model_is_bundled(tmp_path):
    """With the model present, enrichment is legitimate and must not be refused."""
    _caps, actions = run_contract(tmp_path, loadable=True)
    assert actions == ["capabilities"]
