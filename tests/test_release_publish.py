"""Publishing an app release so installed copies can update (scripts/sign_manifest.py
--publish, scripts/check_release.py).

v1.3.0 went out as Latest without its update manifest: the signer could not start under
an embeddable Python (`No module named 'changelog_notes'`), and nothing stopped the draft
from being published first. Every installed copy then said "No signed update information
has been published". These tests pin the order --publish keeps (provenance, sign, upload,
read back, then publish, then wait for the updater's address), that the signer starts
under `python -I`, and that the release check fails a release installed copies can't use.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from scripts import check_release, sign_manifest

REPO = Path(__file__).resolve().parent.parent
VERSION = "9.9.9"
TAG = f"v{VERSION}"


@pytest.fixture
def key(monkeypatch):
    """A signing key the app's verifier trusts, as it trusts the embedded ones."""
    private = ed25519.Ed25519PrivateKey.generate()
    monkeypatch.setattr("app.core.update_verifier.get_official_public_keys", lambda: [private.public_key()])
    return private


def _envelope(private, version=VERSION, assets=None) -> bytes:
    assets = assets if assets is not None else {
        key: {"filename": name, "url": f"https://github.com/AbdoslamB/InkDoc/releases/download/v{version}/{name}",
              "sha256": "0" * 64, "size_bytes": 10, "install_method": "download_reveal"}
        for name, key in sign_manifest.TARGET_ASSETS.items()
    }
    payload = sign_manifest.build_manifest_payload(version, "2026-11-01T00:00:00+00:00",
                                                   f"https://github.com/AbdoslamB/InkDoc/releases/tag/v{version}",
                                                   "notes", assets)
    return json.dumps(sign_manifest.build_envelope(payload, private), indent=2).encode("utf-8")


# ─── Starting the scripts ────────────────────────────────────────────────────

@pytest.mark.parametrize("script", ["scripts/sign_manifest.py", "scripts/check_release.py",
                                    "scripts/smoke_test_artifact.py", "scripts/sign_glm_ocr_catalogue.py"])
def test_the_scripts_start_without_the_script_folder_on_the_path(script):
    # -I leaves the script's folder off sys.path, as an embeddable Python's ._pth file does.
    res = subprocess.run([sys.executable, "-I", script, "--help"], cwd=REPO, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


# ─── The envelope, checked as the app checks it ──────────────────────────────

def test_a_good_envelope_passes(key):
    assert sign_manifest.check_envelope(_envelope(key), VERSION)["version"] == VERSION


def test_a_key_the_app_does_not_embed_is_refused(key):
    other = ed25519.Ed25519PrivateKey.generate()
    with pytest.raises(Exception, match="(?i)signature"):
        sign_manifest.check_envelope(_envelope(other), VERSION)


def test_the_wrong_version_is_refused(key):
    with pytest.raises(ValueError, match="not 9.9.9"):
        sign_manifest.check_envelope(_envelope(key, version="9.9.8"), VERSION)


def test_a_missing_platform_is_refused(key):
    with pytest.raises(ValueError, match="macos-arm64"):
        sign_manifest.check_envelope(_envelope(key, assets={}), VERSION)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def test_provenance_stops_at_the_first_file_that_fails(tmp_path):
    calls = []

    def run(cmd, **kwargs):
        calls.append(Path(cmd[3]).name)
        return subprocess.CompletedProcess(cmd, 1 if cmd[3].endswith("b") else 0, "", "no attestation")

    with pytest.raises(RuntimeError, match="b"):
        sign_manifest.verify_attestations([tmp_path / "a", tmp_path / "b", tmp_path / "c"], "o/r", run=run)
    assert calls == ["a", "b"]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


def test_waiting_ends_when_the_address_serves_the_manifest():
    served = iter([None, b"old", b"new"])
    clock = Clock()
    assert sign_manifest.wait_for_latest(b"new", "u", timeout=60, fetch=lambda u: next(served),
                                         sleep=clock.sleep, clock=clock)


def test_waiting_gives_up_at_the_deadline():
    clock = Clock()
    assert not sign_manifest.wait_for_latest(b"new", "u", timeout=30, fetch=lambda u: None,
                                             sleep=clock.sleep, clock=clock)


# ─── --publish, end to end against a fake gh ─────────────────────────────────

class FakeGh:
    """Answers the gh commands sign_manifest.py runs, and records them in order."""

    def __init__(self, draft=True, attested=True):
        self.draft, self.attested = draft, attested
        self.calls: list[str] = []
        self.uploaded: bytes | None = None

    def run(self, cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, f"## [{VERSION}]\n\n- notes\n".encode(), b"")
        verb = " ".join(cmd[1:3])
        self.calls.append(verb)
        out = ""
        if verb == "release view":
            out = json.dumps({"isDraft": self.draft, "body": "", "url": "u",
                              "assets": [{"name": n} for n in (*sign_manifest.TARGET_ASSETS, "SHA256SUMS-x.txt")]})
        elif verb == "release download":
            folder = Path(cmd[cmd.index("--dir") + 1])
            names = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--pattern"]
            for name in names:
                if name == sign_manifest.MANIFEST_ASSET:
                    (folder / name).write_bytes(self.uploaded or b"")
                elif name.startswith("SHA256SUMS"):
                    (folder / name).write_text("")
                else:
                    (folder / name).write_bytes(b"0123456789")
        elif verb == "release upload":
            path = Path(cmd[4])
            assert path.name == sign_manifest.MANIFEST_ASSET
            self.uploaded = path.read_bytes()
        elif verb == "attestation verify" and not self.attested:
            return subprocess.CompletedProcess(cmd, 1, "", "no attestation")
        return subprocess.CompletedProcess(cmd, 0, out, "")


def _publish(key, monkeypatch, gh, wait=True, extra=()):
    monkeypatch.setattr(sys, "argv", ["sign_manifest.py", "--tag", TAG, "--publish", *extra])
    monkeypatch.setattr(sign_manifest, "load_private_key", lambda path: key)
    with patch.object(sign_manifest.subprocess, "run", gh.run), \
            patch.object(sign_manifest.shutil, "which", return_value="gh"), \
            patch.object(sign_manifest, "wait_for_latest", return_value=wait) as waited:
        code = sign_manifest.main()
    return code, waited


def test_publish_signs_uploads_reads_back_then_publishes(key, monkeypatch):
    gh = FakeGh()
    code, waited = _publish(key, monkeypatch, gh)
    assert code == 0
    order = [c for c in gh.calls if c != "attestation verify"]
    assert order == ["release view", "release download", "release upload", "release download", "release edit"]
    first_upload = gh.calls.index("release upload")
    assert gh.calls.count("attestation verify") == len(sign_manifest.TARGET_ASSETS) + 1
    assert max(i for i, c in enumerate(gh.calls) if c == "attestation verify") < first_upload
    waited.assert_called_once()
    assert waited.call_args.args[0] == gh.uploaded
    sign_manifest.check_envelope(gh.uploaded, VERSION)


def test_publish_marks_the_release_latest(key, monkeypatch):
    gh = FakeGh()
    edits = []
    original = gh.run

    def run(cmd, **kwargs):
        if cmd[1:3] == ["release", "edit"]:
            edits.append(cmd)
        return original(cmd, **kwargs)

    gh.run = run
    _publish(key, monkeypatch, gh)
    assert len(edits) == 1 and "--draft=false" in edits[0] and "--latest" in edits[0]


def test_publish_refuses_a_release_that_is_already_published(key, monkeypatch):
    gh = FakeGh(draft=False)
    code, _ = _publish(key, monkeypatch, gh)
    assert code == 1 and gh.calls == ["release view"]


def test_publish_stops_before_signing_when_provenance_fails(key, monkeypatch):
    gh = FakeGh(attested=False)
    code, _ = _publish(key, monkeypatch, gh)
    assert code == 1 and "release upload" not in gh.calls and "release edit" not in gh.calls


def test_publish_stops_before_uploading_a_manifest_the_app_would_refuse(key, monkeypatch):
    monkeypatch.setattr("app.core.update_verifier.get_official_public_keys",
                        lambda: [ed25519.Ed25519PrivateKey.generate().public_key()])
    gh = FakeGh()
    code, _ = _publish(key, monkeypatch, gh)
    assert code == 1 and "release upload" not in gh.calls


def test_publish_reports_an_address_that_never_serves_it(key, monkeypatch):
    code, _ = _publish(key, monkeypatch, FakeGh(), wait=False)
    assert code == 1


# ─── The release check ───────────────────────────────────────────────────────

def test_the_check_skips_tags_that_are_not_app_releases(capsys):
    assert check_release.main(["--tag", "docling-pack-v2"]) == 0
    assert "not an app release" in capsys.readouterr().out


def test_the_check_waits_for_a_manifest_uploaded_late(key):
    served = iter([None, _envelope(key, version="9.9.8"), _envelope(key)])
    clock = Clock()
    manifest = check_release.latest_manifest("u", VERSION, timeout=120, fetch=lambda u: next(served),
                                             sleep=clock.sleep, clock=clock)
    assert manifest["version"] == VERSION


def test_the_check_fails_when_the_manifest_never_comes():
    clock = Clock()
    with pytest.raises(ValueError, match="serves no manifest"):
        check_release.latest_manifest("u", VERSION, timeout=60, fetch=lambda u: None,
                                      sleep=clock.sleep, clock=clock)


def test_the_check_reports_a_file_of_the_wrong_size(key):
    manifest = sign_manifest.check_envelope(_envelope(key), VERSION)
    sizes = {a["url"]: 10 for a in manifest["assets"].values()}
    broken = next(iter(sizes))
    sizes[broken] = 9
    problems = check_release.check_assets(manifest, size=lambda url: sizes[url])
    assert len(problems) == 1 and broken in problems[0]


def test_the_workflow_runs_the_check_on_every_published_release():
    text = (REPO / ".github" / "workflows" / "release-check.yml").read_text(encoding="utf-8")
    assert "types: [published]" in text
    assert "python scripts/check_release.py --tag" in text
    assert "permissions:\n  contents: read" in text
