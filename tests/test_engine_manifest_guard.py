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
from scripts.verify_engine_manifest_guard import check_pack_is_usable, verify_guard  # noqa: E402

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


class TestShippedManifestDescribesARelocatablePack(unittest.TestCase):
    """The pack the manifest points at must be able to start on a user's machine.

    docling-pack-v2 shipped a virtualenv. `python -m venv` never copies the standard
    library, so the stdlib and python311.dll stayed in the build runner's toolcache
    and the interpreter died during initialisation on every other machine, surfacing
    as "Docling worker exited prematurely with exit code 103".

    Every check at the time passed because every check ran on the build machine.
    These assertions read the manifest itself, so they hold regardless of where they
    run and regardless of whether anything is installed.
    """

    def setUp(self) -> None:
        self.platforms = load_engine_manifest(MANIFEST_PATH).supported_platforms

    def test_no_platform_ships_a_virtualenv(self) -> None:
        for key, pack in self.platforms.items():
            with self.subTest(platform=key):
                strays = [f for f in pack.sha256_files if f.endswith("pyvenv.cfg")]
                self.assertEqual(
                    strays, [],
                    f"{key} ships {strays}: the interpreter would resolve its standard "
                    "library against the machine that built the pack and fail to start",
                )

    def test_every_platform_bundles_its_standard_library(self) -> None:
        for key, pack in self.platforms.items():
            with self.subTest(platform=key):
                encodings = [
                    f for f in pack.sha256_files if f.endswith("/encodings/__init__.py")
                ]
                self.assertTrue(
                    encodings,
                    f"{key} bundles no encodings module; CPython cannot initialise "
                    "without it, so the worker dies before running any code",
                )
                if key.startswith("windows"):
                    self.assertIn(
                        "python/python311.dll", pack.sha256_files,
                        f"{key} bundles no python311.dll; python.exe cannot start",
                    )

    def test_interpreter_lives_inside_the_pack(self) -> None:
        """A path under env/ means the old virtualenv layout came back."""
        for key, pack in self.platforms.items():
            with self.subTest(platform=key):
                self.assertFalse(
                    pack.interpreter_path.startswith("env/"),
                    f"{key} interpreter_path is {pack.interpreter_path!r}, the "
                    "virtualenv layout replaced by the relocatable CPython build",
                )
                self.assertIn(pack.interpreter_path, pack.sha256_files)


class TestReleaseGateRejectsUnusablePacks(unittest.TestCase):
    """The release guard must block a tag whose manifest points at a dead pack.

    v1.0.3-rc2 was tagged one commit before the manifest was repointed at
    docling-pack-v3. It therefore built, published and reached a user still pointing
    at v2, whose interpreter could not start outside the build runner. Nothing in the
    pipeline looked at what the manifest described, only that its hashes were well
    formed, so the whole release completed successfully around a broken payload.

    These assert on properties rather than a list of known-bad tags, so re-pointing
    the manifest at any non-relocatable pack fails no matter what it is called.
    """

    def _pack(self, **overrides: object) -> dict:
        files = {
            "worker.py": "a" * 64,
            "python/python.exe": "b" * 64,
            "python/python311.dll": "c" * 64,
            "python/Lib/encodings/__init__.py": "d" * 64,
            "models/layout/model.safetensors": "e" * 64,
        }
        pack = {
            "url": "https://github.com/AbdoslamB/InkDoc/releases/download/docling-pack-v3/x.zip",
            "archive_format": "zip",
            "sha256": "f" * 64,
            "size_bytes": 1,
            "uncompressed_size_bytes": 2,
            "interpreter_path": "python/python.exe",
            "worker_script": "worker.py",
            "sha256_files": files,
        }
        pack.update(overrides)
        return pack

    def test_accepts_a_relocatable_pack(self) -> None:
        self.assertEqual(check_pack_is_usable("windows-x86_64", self._pack()), [])

    def test_rejects_a_virtualenv_pack(self) -> None:
        """Exactly the shape docling-pack-v2 shipped."""
        files = {
            "worker.py": "a" * 64,
            "env/pyvenv.cfg": "b" * 64,
            "env/Scripts/python.exe": "c" * 64,
            "models/layout/model.safetensors": "d" * 64,
        }
        errors = check_pack_is_usable(
            "windows-x86_64",
            self._pack(sha256_files=files, interpreter_path="env/Scripts/python.exe"),
        )
        joined = " ".join(errors)
        self.assertIn("pyvenv.cfg", joined)
        self.assertIn("encodings", joined)
        self.assertIn("python311.dll", joined)
        self.assertIn("virtualenv layout", joined)

    def test_rejects_pack_without_models(self) -> None:
        """docling-pack-v1: installs cleanly, then every conversion fails offline."""
        files = {k: v for k, v in self._pack()["sha256_files"].items() if not k.startswith("models/")}
        errors = check_pack_is_usable("windows-x86_64", self._pack(sha256_files=files))
        self.assertTrue(any("models/" in e for e in errors), errors)

    def test_rejects_unindexed_interpreter(self) -> None:
        """is_pack_installed() would never become true; the UI offers Install forever."""
        errors = check_pack_is_usable(
            "windows-x86_64", self._pack(interpreter_path="python/not-indexed.exe")
        )
        self.assertTrue(any("not in sha256_files" in e for e in errors), errors)

    def test_blocks_the_exact_manifest_rc2_shipped(self) -> None:
        """Regression: run the whole guard over a v2-shaped manifest."""
        payload = {
            "manifest_version": "1.0.0",
            "pack_version": "1.0.0",
            "min_app_version": "1.0.0",
            "supported_platforms": {
                "windows-x86_64": self._pack(
                    sha256_files={
                        "worker.py": "a" * 64,
                        "env/pyvenv.cfg": "b" * 64,
                        "env/Scripts/python.exe": "c" * 64,
                        "models/m.bin": "d" * 64,
                    },
                    interpreter_path="env/Scripts/python.exe",
                ),
            },
        }
        tmp = Path(tempfile.mkdtemp(prefix="inkdoc_release_gate_")) / "manifest.json"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        valid, errors = verify_guard(manifest_path=tmp, skip_network=True)
        self.assertFalse(valid, "the release guard accepted a virtualenv-based pack")
        self.assertTrue(any("pyvenv.cfg" in e for e in errors), errors)

    def test_shipped_manifest_passes_the_release_gate(self) -> None:
        valid, errors = verify_guard(manifest_path=MANIFEST_PATH, skip_network=True)
        self.assertTrue(valid, f"the shipped manifest would block a release: {errors[:6]}")


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
