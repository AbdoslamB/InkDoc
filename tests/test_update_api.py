import os
import sys
import unittest
from pathlib import Path

# Configure thread pool limits before scientific/native libraries load
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

from app.server.server import (
    SESSION_TOKEN,
    app,
    get_update_manager,
    is_update_applying,
    set_update_applying,
    track_active_conversion,
)


class TestUpdateAPI(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.headers = {
            "Host": "127.0.0.1:13118",
            "Origin": "http://127.0.0.1:13118",
            "X-InkDoc-Token": SESSION_TOKEN,
        }

    def test_get_update_status(self) -> None:
        resp = self.client.get("/InkDoc/update/status", headers={"Host": "127.0.0.1:13118"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("current_version", data)
        self.assertIn("state", data)
        self.assertIn("platform_key", data)

    def test_update_check_requires_session_token(self) -> None:
        # Request without session token must be refused with 403
        resp = self.client.post(
            "/InkDoc/update/check",
            json={"force": True},
            headers={"Host": "127.0.0.1:13118", "Origin": "http://127.0.0.1:13118"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_update_check_rejects_query_token(self) -> None:
        """Requirement C-2: ?token= in query string must be rejected with 403."""
        resp = self.client.post(
            f"/InkDoc/update/check?token={SESSION_TOKEN}",
            json={"force": True},
            headers={"Host": "127.0.0.1:13118", "Origin": "http://127.0.0.1:13118"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("X-InkDoc-Token header required", resp.json()["detail"])

    def test_update_download_requires_session_token(self) -> None:
        resp = self.client.post(
            "/InkDoc/update/download",
            headers={"Host": "127.0.0.1:13118", "Origin": "http://127.0.0.1:13118"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_update_apply_requires_session_token(self) -> None:
        resp = self.client.post(
            "/InkDoc/update/apply",
            headers={"Host": "127.0.0.1:13118", "Origin": "http://127.0.0.1:13118"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_update_apply_refused_when_not_desktop_runner(self) -> None:
        """Requirement D: apply refused unless launched by desktop runner."""
        mgr = get_update_manager()
        mgr.set_desktop_runner(False)

        resp = self.client.post("/InkDoc/update/apply", headers=self.headers)
        # Should be 400 (no update ready) or 403 (desktop runner required)
        self.assertIn(resp.status_code, (400, 403))

    def test_update_apply_refused_when_conversion_active(self) -> None:
        """Requirement D: apply refused with 409 if document conversion is running."""
        mgr = get_update_manager()
        # Mock ready state
        from app.core.update_manager import UpdateState
        mgr._status.state = UpdateState.READY_TO_INSTALL.value
        mgr._status.can_apply = True

        with track_active_conversion():
            resp = self.client.post("/InkDoc/update/apply", headers=self.headers)
            self.assertEqual(resp.status_code, 409)
            self.assertIn("conversions are in progress", resp.json()["detail"])
        self.assertFalse(is_update_applying())

    def test_update_apply_sets_update_applying_flag(self) -> None:
        """Requirement H-9: successful apply sets update_applying flag to True."""
        mgr = get_update_manager()
        from unittest.mock import patch

        try:
            with patch.object(
                mgr, "apply_update", return_value={"status": "applying", "message": "restarting"}
            ):
                resp = self.client.post("/InkDoc/update/apply", headers=self.headers)
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(is_update_applying())
        finally:
            set_update_applying(False)


if __name__ == "__main__":
    unittest.main()
