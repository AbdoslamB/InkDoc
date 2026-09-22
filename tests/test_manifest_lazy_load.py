"""The bundled engine manifest must not be parsed before something needs it.

app/core/manifest.json carries a SHA-256 for every file in every platform pack --
roughly 93,000 entries and 14 MB. Parsing it costs about 140 ms and retains ~18 MB.

The desktop runner reads user settings before opening the window
(app/desktop/runner.py), and settings do not need the manifest. Parsing it in
EngineManager.__init__ therefore delayed the window by that much for no reason.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.engine_manager import EngineManager  # noqa: E402
from app.core.engine_manifest import EngineManifest, load_engine_manifest  # noqa: E402


class TestManifestIsLoadedLazily(unittest.TestCase):
    def test_constructing_the_manager_does_not_parse_the_manifest(self) -> None:
        with patch("app.core.engine_manager.load_engine_manifest") as loader:
            EngineManager()
            loader.assert_not_called()

    def test_reading_settings_does_not_parse_the_manifest(self) -> None:
        """The exact call app/desktop/runner.py makes before opening the window."""
        with patch("app.core.engine_manager.load_engine_manifest") as loader:
            mgr = EngineManager()
            mgr.get_settings()
            loader.assert_not_called()

    def test_pack_metadata_triggers_exactly_one_parse(self) -> None:
        real = load_engine_manifest()
        with patch("app.core.engine_manager.load_engine_manifest", return_value=real) as loader:
            mgr = EngineManager()
            mgr.get_platform_pack_info("docling")
            mgr.get_supported_platform_keys()
            mgr.is_platform_supported("docling")
            self.assertEqual(
                loader.call_count, 1,
                "the manifest should be parsed once and cached, not on every access",
            )

    def test_an_injected_manifest_is_never_replaced_by_a_parse(self) -> None:
        """Tests and scripts/verify_engine_pack_install.py pass a manifest directly."""
        injected = EngineManifest(
            manifest_version="1.0.0", pack_version="9.9.9", min_app_version="1.0.0"
        )
        with patch("app.core.engine_manager.load_engine_manifest") as loader:
            mgr = EngineManager(manifest=injected)
            self.assertEqual(mgr.manifest.pack_version, "9.9.9")
            loader.assert_not_called()

    def test_manifest_remains_assignable(self) -> None:
        """Existing callers set .manifest directly; the property must allow it."""
        mgr = EngineManager(manifest=EngineManifest("1.0.0", "1.0.0", "1.0.0"))
        replacement = EngineManifest("1.0.0", "2.2.2", "1.0.0")
        mgr.manifest = replacement
        self.assertIs(mgr.manifest, replacement)

    def test_manifest_can_be_patched_and_restored(self) -> None:
        """unittest.mock.patch.object restores by deleting, so the property needs a
        deleter. Several existing tests patch .manifest and fail without it."""
        mgr = EngineManager(manifest=EngineManifest("1.0.0", "1.1.1", "1.0.0"))
        replacement = EngineManifest("1.0.0", "3.3.3", "1.0.0")
        with patch.object(mgr, "manifest", replacement):
            self.assertEqual(mgr.manifest.pack_version, "3.3.3")
        # Restored: the deleter cleared the cache, so this re-reads from disk.
        self.assertIsInstance(mgr.manifest, EngineManifest)

    def test_lazy_manifest_still_returns_real_pack_data(self) -> None:
        mgr = EngineManager()
        pack = mgr.get_platform_pack_info("docling")
        if pack is None:
            self.skipTest("no pack for this platform in the bundled manifest")
        self.assertTrue(pack.sha256_files, "lazy load returned an empty manifest")
        self.assertIn(pack.interpreter_path, pack.sha256_files)


if __name__ == "__main__":
    unittest.main()
