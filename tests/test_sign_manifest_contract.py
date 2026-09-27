"""Contract between scripts/sign_manifest.py and app/core/update_manager.py.

The signer and the client agree on a JSON shape that no schema enforces. Every
optional field is read with .get() and defaults to empty, so a disagreement is
silent: the update still verifies and installs, it just loses whatever the client
could not find.

That already happened. The signer emitted `release_notes` and `release_url`; the
client read `notes` and `html_url`. Nobody noticed because the existing tests build
manifests by hand using the client's names, which cannot detect the signer drifting.

These tests drive the signer's own payload builder through the real envelope
signing and the real verifier, so the two sides are checked against each other
rather than against a fixture.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402

from app.core.update_manager import UpdateManager, UpdateState  # noqa: E402
from scripts.sign_manifest import build_envelope, build_manifest_payload  # noqa: E402

NOTES = "## What's new\n\n- A visible release note"
URL = "https://github.com/AbdoslamB/InkDoc/releases/tag/v9.9.9"


class TestSignerClientContract(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key = ed25519.Ed25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()
        self.mgr = UpdateManager(
            manifest_url_override="https://github.com/AbdoslamB/InkDoc/releases/latest/download/inkdoc-update-manifest.json",
            test_public_keys=[self.public_key],
        )

    def _sign(self, payload: dict) -> bytes:
        return json.dumps(build_envelope(payload, self.private_key)).encode("utf-8")

    def _asset(self) -> dict:
        return {
            self.mgr._platform_key: {
                "filename": "inkdoc-setup.exe",
                "url": "https://github.com/AbdoslamB/InkDoc/releases/download/v9.9.9/inkdoc-setup.exe",
                "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "size_bytes": 1024,
                "install_method": "inno_silent",
            }
        }

    def test_signed_payload_surfaces_notes_and_release_url(self) -> None:
        """What the signer writes must be what the client displays."""
        payload = build_manifest_payload(
            version="9.9.9",
            issued_at="2026-09-21T00:00:00Z",
            release_url=URL,
            release_notes=NOTES,
            assets=self._asset(),
        )
        with patch.object(self.mgr, "_fetch_manifest_bytes", return_value=self._sign(payload)):
            status = self.mgr.check_for_updates(force=True)

        self.assertEqual(status["state"], UpdateState.AVAILABLE.value)
        self.assertEqual(status["latest_version"], "9.9.9")
        self.assertEqual(
            status["release_notes"], NOTES,
            "release notes were lost between signer and client: the update panel "
            "would render an empty notes section",
        )
        self.assertEqual(
            status["release_url"], URL,
            "release URL was lost between signer and client: the update panel "
            "would offer no link to the release",
        )

    def test_signed_payload_passes_envelope_validation(self) -> None:
        """The signer's own output must satisfy the verifier's required fields."""
        from app.core.update_verifier import verify_envelope_bytes

        payload = build_manifest_payload(
            version="9.9.9",
            issued_at="2026-09-21T00:00:00Z",
            release_url=URL,
            release_notes="",
            assets=self._asset(),
        )
        manifest = verify_envelope_bytes(
            self._sign(payload),
            installed_version="1.0.0",
            trusted_public_keys=[self.public_key],
        )
        self.assertEqual(manifest["version"], "9.9.9")

    def test_legacy_field_names_still_render(self) -> None:
        """A manifest from the previous signer must not silently lose both fields."""
        payload = build_manifest_payload(
            version="9.9.9",
            issued_at="2026-09-21T00:00:00Z",
            release_url="",
            release_notes="",
            assets=self._asset(),
        )
        payload.pop("notes")
        payload.pop("html_url")
        payload["release_notes"] = NOTES
        payload["release_url"] = URL

        with patch.object(self.mgr, "_fetch_manifest_bytes", return_value=self._sign(payload)):
            status = self.mgr.check_for_updates(force=True)

        self.assertEqual(status["release_notes"], NOTES)
        self.assertEqual(status["release_url"], URL)


class TestSilentUpdateRelaunch(unittest.TestCase):
    """The installer must be asked to relaunch, and must know how to.

    Inno skips [Run] entries flagged postinstall under /VERYSILENT, so the app
    installed an update and never came back while reporting that it was restarting.
    """

    def test_apply_passes_restartapp_flag(self) -> None:
        iss = (REPO_ROOT / "installer.iss").read_text(encoding="utf-8")
        mgr_src = (REPO_ROOT / "app" / "core" / "update_manager.py").read_text(encoding="utf-8")

        self.assertIn(
            '"/RESTARTAPP=1"', mgr_src,
            "apply_update must pass /RESTARTAPP=1, or a silent update never relaunches",
        )
        self.assertIn(
            "RESTARTAPP", iss,
            "installer.iss must read the RESTARTAPP parameter the updater passes",
        )
        self.assertIn(
            "Check: ShouldRelaunchAfterSilentUpdate", iss,
            "installer.iss needs a [Run] entry without the postinstall flag, gated on "
            "the updater's request; postinstall entries never execute in silent mode",
        )
        self.assertIn(
            "WizardSilent()", iss,
            "the relaunch must be restricted to silent installs so an unattended "
            "deployment, and the release smoke test, do not spawn a GUI",
        )


if __name__ == "__main__":
    unittest.main()
