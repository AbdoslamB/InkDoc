"""The Settings "API Documentation" link must not point at a 404.

FastAPI's interactive docs are deliberately disabled in packaged builds
(app/server/server.py sets docs_url=None when sys.frozen), so /docs returns 404 in
the installed app. The Settings panel linked to it unconditionally, and GET /
advertised "docs_url": "/docs" whether or not anything was served there.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.server.server import app  # noqa: E402

client = TestClient(app)

INDEX_HTML = (REPO_ROOT / "app" / "ui" / "index.html").read_text(encoding="utf-8")
APP_JS = (REPO_ROOT / "app" / "ui" / "app.js").read_text(encoding="utf-8")


class TestDocsAvailabilityIsReportedHonestly(unittest.TestCase):
    def test_health_reports_whether_docs_are_served(self) -> None:
        body = client.get("/health").json()
        self.assertIn(
            "docs_enabled", body,
            "the UI needs this to decide whether to show the API Documentation link",
        )
        self.assertIsInstance(body["docs_enabled"], bool)

    def test_docs_enabled_matches_whether_docs_actually_exist(self) -> None:
        """The flag has to track reality, or the UI hides or shows the wrong thing."""
        enabled = client.get("/health").json()["docs_enabled"]
        status = client.get("/docs").status_code
        if enabled:
            self.assertNotEqual(status, 404, "docs_enabled is true but /docs is missing")
        else:
            self.assertEqual(status, 404, "docs_enabled is false but /docs is served")

    def test_root_does_not_advertise_docs_that_are_not_served(self) -> None:
        root = client.get("/").json()
        enabled = client.get("/health").json()["docs_enabled"]
        if enabled:
            self.assertEqual(root["docs_url"], "/docs")
        else:
            self.assertIsNone(
                root["docs_url"],
                'GET / advertised "docs_url": "/docs" while /docs returns 404',
            )


class TestSettingsLinkIsHiddenByDefault(unittest.TestCase):
    """Fail closed: if the health call never resolves, no dead link is shown."""

    def test_markup_hides_the_link_until_told_otherwise(self) -> None:
        for element_id in ("apiDocsSection", "apiDocsDivider"):
            with self.subTest(element=element_id):
                match = re.search(rf'id="{element_id}"[^>]*>', INDEX_HTML)
                self.assertIsNotNone(match, f"{element_id} is missing from index.html")
                self.assertIn(
                    "hidden", match.group(0),
                    f"{element_id} is visible before the server confirms /docs exists",
                )

    def test_the_link_is_revealed_from_the_health_response(self) -> None:
        self.assertIn("docs_enabled", APP_JS)
        self.assertIn("apiDocsSection", APP_JS)


if __name__ == "__main__":
    unittest.main()
