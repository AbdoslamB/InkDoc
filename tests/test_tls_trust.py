"""HTTPS certificates in the packaged app (app/core/tls_trust.py), and what the updater says
when a connection fails.

1.3.0's macOS app looked for certificates where the build machine's python.org framework
keeps them, and its AppImage where Debian and Ubuntu do: on a Mac without that Python, and
on Fedora, Arch and openSUSE, every HTTPS request failed. 1.2.0 on Windows meets GitHub's
newer certificate chain with "unable to get issuer certificate". These tests pin which file
each system gets, that Windows and runs from source are left alone, and that the update
error says what to do.
"""
from __future__ import annotations

import ssl
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import linux_host, tls_trust
from app.core.update_manager import NO_MANIFEST_YET, UpdateManager, UpdateState, connection_error
from app.core.update_verifier import DowngradeError

REPO = Path(__file__).resolve().parent.parent
CERTIFI = "/bundle/certifi/cacert.pem"


def usable(*paths: str):
    return lambda path: path in paths


# ─── Which file ──────────────────────────────────────────────────────────────

def test_macos_uses_the_system_file():
    assert tls_trust.pick_ca_file("darwin", False, CERTIFI, usable("/etc/ssl/cert.pem", CERTIFI)) == "/etc/ssl/cert.pem"


def test_macos_falls_back_to_the_bundled_certifi():
    assert tls_trust.pick_ca_file("darwin", False, CERTIFI, usable(CERTIFI)) == CERTIFI


def test_macos_ignores_the_build_machines_location_even_when_it_exists():
    # A Mac with python.org's Python has the build-time folder; the app still uses the system's file.
    assert tls_trust.pick_ca_file("darwin", True, CERTIFI, usable("/etc/ssl/cert.pem")) == "/etc/ssl/cert.pem"


def test_debian_and_ubuntu_keep_openssls_own_location():
    every_file = usable(*tls_trust.LINUX_FILES, CERTIFI)
    assert tls_trust.pick_ca_file("linux", True, CERTIFI, every_file) is None


@pytest.mark.parametrize("found", [
    "/etc/pki/tls/certs/ca-bundle.crt",                     # Fedora, RHEL
    "/etc/ssl/certs/ca-certificates.crt",                   # Arch
    "/etc/ssl/ca-bundle.pem",                               # openSUSE
])
def test_other_distributions_get_their_own_file(found):
    assert tls_trust.pick_ca_file("linux", False, CERTIFI, usable(found, CERTIFI)) == found


def test_linux_without_any_system_file_uses_the_bundled_certifi():
    assert tls_trust.pick_ca_file("linux", False, CERTIFI, usable(CERTIFI)) == CERTIFI


def test_nothing_usable_changes_nothing():
    assert tls_trust.pick_ca_file("linux", False, CERTIFI, usable()) is None
    assert tls_trust.pick_ca_file("darwin", False, None, usable()) is None


def test_windows_keeps_its_certificate_store():
    assert tls_trust.pick_ca_file("win32", False, CERTIFI, usable(CERTIFI)) is None


# ─── Setting it ──────────────────────────────────────────────────────────────

@pytest.fixture
def fedora(tmp_path, monkeypatch):
    """A Linux system whose certificates are in a file OpenSSL's build doesn't look at."""
    system = tmp_path / "ca-bundle.crt"
    system.write_text("certificates")
    monkeypatch.setattr(tls_trust, "LINUX_FILES", (str(tmp_path / "absent.crt"), str(system)))
    monkeypatch.setattr(tls_trust, "openssl_default_works", lambda: False)
    return str(system)


def test_the_packaged_app_points_openssl_at_the_system_file(fedora):
    env: dict[str, str] = {}
    assert tls_trust.use_system_certificates(env, "linux", True, "/nowhere") == fedora
    assert env == {"SSL_CERT_FILE": fedora, tls_trust.CHOSEN_BY_INKDOC: fedora}


def test_bundled_certifi_is_found_inside_the_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(tls_trust, "MACOS_FILES", (str(tmp_path / "absent.pem"),))
    (tmp_path / "certifi").mkdir()
    (tmp_path / "certifi" / "cacert.pem").write_text("certificates")
    env: dict[str, str] = {}
    chosen = tls_trust.use_system_certificates(env, "darwin", True, str(tmp_path))
    assert chosen == str(tmp_path / "certifi" / "cacert.pem") == env["SSL_CERT_FILE"]


def test_runs_from_source_are_left_alone(fedora):
    env: dict[str, str] = {}
    assert tls_trust.use_system_certificates(env, "linux", False, "/nowhere") is None
    assert env == {}


@pytest.mark.parametrize("name", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
def test_the_users_own_setting_wins(fedora, name):
    env = {name: "/my/corporate/ca.pem"}
    assert tls_trust.use_system_certificates(env, "linux", True, "/nowhere") is None
    assert env == {name: "/my/corporate/ca.pem"}


def test_a_failure_never_stops_the_app(monkeypatch):
    def broken() -> bool:
        raise OSError("no access")

    monkeypatch.setattr(tls_trust, "openssl_default_works", broken)
    env: dict[str, str] = {}
    assert tls_trust.use_system_certificates(env, "linux", True, "/nowhere") is None
    assert env == {}


def test_describe_says_where_certificates_come_from():
    assert tls_trust.describe({"SSL_CERT_FILE": "/a", tls_trust.CHOSEN_BY_INKDOC: "/a"}, "linux") == \
        "SSL_CERT_FILE=/a (set by InkDoc)"
    assert "set by the environment" in tls_trust.describe({"SSL_CERT_FILE": "/a"}, "linux")
    assert tls_trust.describe({}, "win32") == "the Windows certificate store"


# ─── Programs started for the user don't get InkDoc's choice ─────────────────

def test_host_programs_get_back_the_users_environment():
    env = linux_host.host_env({"SSL_CERT_FILE": "/etc/pki/tls/certs/ca-bundle.crt",
                               tls_trust.CHOSEN_BY_INKDOC: "/etc/pki/tls/certs/ca-bundle.crt"})
    assert "SSL_CERT_FILE" not in env and tls_trust.CHOSEN_BY_INKDOC not in env


def test_host_programs_keep_a_setting_the_user_made():
    env = linux_host.host_env({"SSL_CERT_FILE": "/my/ca.pem"})
    assert env["SSL_CERT_FILE"] == "/my/ca.pem"


# ─── Wiring ──────────────────────────────────────────────────────────────────

def test_main_sets_certificates_before_the_workers_start():
    source = (REPO / "main.py").read_text(encoding="utf-8")
    assert 0 < source.index("use_system_certificates()") < source.index('["--markitdown-worker"]')


def test_every_packaged_build_carries_certifi():
    release = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    blocks = release.split("pyinstaller --noconfirm")[1:]
    assert len(blocks) == 2 and all("--collect-data certifi" in b.split("main.py", 1)[0] for b in blocks)
    local = (REPO / "scripts" / "build_local_exe.ps1").read_text(encoding="utf-8")
    assert '"--collect-data", "certifi",' in local


def test_the_release_smoke_test_runs_the_https_check():
    smoke = (REPO / "scripts" / "smoke_test_artifact.py").read_text(encoding="utf-8")
    assert '"--selftest-https"' in smoke
    import inspect

    from app.desktop import runner

    assert "--selftest-https" in inspect.getsource(runner.main)


# ─── The self-test ───────────────────────────────────────────────────────────

class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _selftest(fetch, open_url=lambda *a, **k: FakeResponse()):
    with patch.object(UpdateManager, "_fetch_manifest_bytes", fetch), \
            patch("app.core.update_verifier.verify_envelope_bytes", return_value={"version": "1.3.1"}), \
            patch("urllib.request.urlopen", open_url):
        return tls_trust.https_selftest(attempts=3, wait_s=0)


def test_selftest_passes_when_both_connections_verify(capsys):
    assert _selftest(lambda self: b"{}") == 0
    assert "[PASS] GitHub: update manifest for 1.3.1, signature verified" in capsys.readouterr().out


def test_selftest_fails_at_once_on_a_certificate_error():
    calls = []

    def fetch(self):
        calls.append(1)
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "unable to get local issuer certificate"))

    assert _selftest(fetch) == 1
    assert len(calls) == 1, "a certificate error is not tried again"


def test_selftest_tries_a_network_error_again():
    calls = []

    def fetch(self):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError("temporary failure in name resolution")
        return b"{}"

    assert _selftest(fetch) == 0
    assert len(calls) == 3


def test_selftest_counts_an_http_error_as_a_verified_connection():
    def fetch(self):
        raise urllib.error.HTTPError("https://github.com/x", 404, "Not Found", None, None)

    assert _selftest(fetch) == 0


# ─── What the updater says ───────────────────────────────────────────────────

def test_a_certificate_error_says_what_to_do():
    message = connection_error(ssl.SSLCertVerificationError(1, "x"))
    assert "couldn't verify the update server's security certificate" in message
    assert "Download the newest version from the website" in message


def test_other_network_errors_keep_their_reason():
    assert connection_error("timed out") == "Update check failed: network connection error (timed out)."


@pytest.mark.parametrize("error, text", [
    (urllib.error.URLError(ssl.SSLCertVerificationError(1, "unable to get issuer certificate")), "certificate"),
    (urllib.error.HTTPError("https://github.com/x", 404, "Not Found", None, None), NO_MANIFEST_YET),
    (urllib.error.URLError("timed out"), "timed out"),
])
def test_a_failed_check_offers_the_website(error, text):
    mgr = UpdateManager()
    with patch.object(mgr, "_fetch_manifest_bytes", side_effect=error):
        status = mgr.check_for_updates(force=True)
    assert status["state"] == UpdateState.ERROR.value
    assert text in status["error"]
    assert status["offer_download"] is True


def test_the_offer_is_cleared_by_the_next_check():
    mgr = UpdateManager()
    with patch.object(mgr, "_fetch_manifest_bytes", side_effect=urllib.error.URLError("timed out")):
        assert mgr.check_for_updates(force=True)["offer_download"] is True
    with patch.object(mgr, "_fetch_manifest_bytes", return_value=b"{}"), \
            patch("app.core.update_manager.verify_envelope_bytes", side_effect=DowngradeError("not newer")):
        status = mgr.check_for_updates(force=True)
    assert status["state"] == UpdateState.UP_TO_DATE.value
    assert status["offer_download"] is False
