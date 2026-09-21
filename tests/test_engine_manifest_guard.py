"""Guards on app/core/manifest.json, the file whose emptiness broke Docling in v1.0.2.

The bundled manifest is the sole source of truth for which platforms can install the
Docling engine pack. An empty one makes get_platform_pack() return None for every
host, so is_platform_supported() is False everywhere and the UI reports
"NOT AVAILABLE" regardless of what the user's machine can actually run.

v1.0.2 shipped exactly that, past two release guards that both reported success.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.engine_manifest import EngineManifest, load_engine_manifest  # noqa: E402
from scripts.generate_engine_manifest import validate_manifest  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "app" / "core" / "manifest.json"

# Every platform the release workflow builds an app for and the README advertises a
# Docling pack for. A manifest missing any of these reintroduces "NOT AVAILABLE" for
# that platform's users without any other symptom.
EXPECTED_PLATFORMS = {"windows-x86_64", "linux-x86_64", "macos-arm64"}


class TestBundledManifest(unittest.TestCase):
    def test_manifest_covers_every_shipped_platform(self) -> None:
        manifest: EngineManifest = load_engine_manifest(MANIFEST_PATH)
        self.assertEqual(
            set(manifest.supported_platforms), EXPECTED_PLATFORMS,
            "app/core/manifest.json must cover exactly the platforms InkDoc ships for; "
            "a missing key makes Docling report UNSUPPORTED on that platform",
        )

    def test_every_platform_entry_is_installable(self) -> None:
        manifest = load_engine_manifest(MANIFEST_PATH)
        for key, pack in manifest.supported_platforms.items():
            with self.subTest(platform=key):
                # install_engine() fails closed on a bad hash, so catch it here instead.
                self.assertRegex(pack.sha256, r"^[0-9a-f]{64}$")
                self.assertGreater(pack.size_bytes, 0)
                self.assertGreater(pack.uncompressed_size_bytes, 0)
                self.assertTrue(pack.url.startswith("https://github.com/"))

                # is_pack_installed() checks these two paths exist after extraction;
                # if they are not in the archive index the pack can never register as
                # installed, and the UI offers Install forever.
                self.assertIn(pack.worker_script, pack.sha256_files)
                self.assertIn(pack.interpreter_path, pack.sha256_files)

                # The models the worker loads offline. docling-pack-v1 had none, so
                # every conversion failed after a successful install.
                self.assertTrue(
                    any(f.startswith("models/") for f in pack.sha256_files),
                    f"{key} bundles no models/ directory; conversions will fail offline",
                )

    def test_pack_stays_within_install_time_limits(self) -> None:
        """Limits enforced during download and extraction, not at build time.

        Exceeding any of these produces a pack that builds and publishes fine and
        then fails on every user's machine.
        """
        from app.core.download_utils import (
            DEFAULT_MAX_COMPRESSION_RATIO,
            DEFAULT_MAX_FILE_COUNT,
            DEFAULT_MAX_UNCOMPRESSED_BYTES,
        )

        manifest = load_engine_manifest(MANIFEST_PATH)
        for key, pack in manifest.supported_platforms.items():
            with self.subTest(platform=key):
                self.assertLessEqual(pack.uncompressed_size_bytes, DEFAULT_MAX_UNCOMPRESSED_BYTES)
                self.assertLessEqual(len(pack.sha256_files), DEFAULT_MAX_FILE_COUNT)
                self.assertLessEqual(
                    pack.uncompressed_size_bytes / pack.size_bytes,
                    DEFAULT_MAX_COMPRESSION_RATIO,
                )
                # engine_manager passes max_size_cap=2000 MiB to stream_download.
                self.assertLessEqual(pack.size_bytes, 2000 * 1024 * 1024)


class TestManifestGuardRejectsEmpty(unittest.TestCase):
    """The guard CI runs must fail on an empty manifest, not pass vacuously."""

    def _write(self, payload: dict) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="inkdoc_manifest_guard_")) / "manifest.json"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        return tmp

    def test_empty_supported_platforms_is_rejected(self) -> None:
        # Byte-for-byte what v1.0.2 shipped and what CI passed.
        path = self._write({
            "manifest_version": "1.0.0",
            "pack_version": "1.0.0",
            "min_app_version": "1.0.0",
            "supported_platforms": {},
        })
        valid, errors = validate_manifest(path)
        self.assertFalse(valid, "an empty manifest was accepted by the release guard")
        self.assertTrue(any("no supported_platforms" in e for e in errors), errors)

    def test_missing_supported_platforms_key_is_rejected(self) -> None:
        path = self._write({"manifest_version": "1.0.0", "pack_version": "1.0.0"})
        valid, _ = validate_manifest(path)
        self.assertFalse(valid)

    def test_real_bundled_manifest_passes(self) -> None:
        valid, errors = validate_manifest(MANIFEST_PATH)
        self.assertTrue(valid, f"the shipped manifest fails its own guard: {errors[:5]}")


if __name__ == "__main__":
    unittest.main()
