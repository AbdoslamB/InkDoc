"""Which certificates the packaged app trusts for HTTPS.

Python checks a server's certificate against the file and folder its OpenSSL was built to
look in, and in the packaged app those are the build machine's: on macOS the python.org
framework's (/Library/Frameworks/Python.framework/Versions/3.14/etc/openssl), which a Mac
without that Python doesn't have, and on Linux /usr/lib/ssl, which Debian and Ubuntu have
and Fedora, Arch and openSUSE don't. With nothing to check against, every HTTPS request
failed with "unable to get local issuer certificate" (1.3.0): update checks, engine and
add-on downloads, URL conversion.

use_system_certificates() runs first thing in main.py. In the packaged app on macOS, and on
Linux when OpenSSL's own location has no certificates, it points OpenSSL at this system's
certificate file, else at the certifi bundle shipped with the app, through SSL_CERT_FILE,
which InkDoc's workers inherit. Windows (its system store), runs from source (their Python's
own setup) and a SSL_CERT_FILE or SSL_CERT_DIR the user set are left alone.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable, MutableMapping

# The system's certificate file, by distribution: Debian, Ubuntu and Arch; Fedora and RHEL
# (two names for one file); openSUSE; Alpine, and Arch's other name.
LINUX_FILES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/cert.pem",
)
# Kept up to date by macOS itself.
MACOS_FILES = ("/etc/ssl/cert.pem",)
# Set beside SSL_CERT_FILE when InkDoc chose it, so linux_host.host_env() can leave it out of
# the programs InkDoc starts for the user (a browser through xdg-open).
CHOSEN_BY_INKDOC = "INKDOC_SSL_CERT_FILE"


def _usable_file(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def openssl_default_works() -> bool:
    """True when OpenSSL's built-in file or folder has certificates on this system."""
    import ssl

    paths = ssl.get_default_verify_paths()
    if paths.openssl_cafile and _usable_file(paths.openssl_cafile):
        return True
    try:
        return bool(paths.openssl_capath) and any(os.scandir(paths.openssl_capath))
    except OSError:
        return False


def bundled_certifi(bundle_dir: str | None) -> str | None:
    """certifi's file inside the packaged app (release.yml collects it)."""
    return os.path.join(bundle_dir, "certifi", "cacert.pem") if bundle_dir else None


def pick_ca_file(
    platform: str,
    default_works: bool,
    bundled: str | None,
    usable: Callable[[str], bool] = _usable_file,
) -> str | None:
    """The certificate file to use, or None to keep what OpenSSL does on its own."""
    if platform == "darwin":
        candidates = MACOS_FILES
    elif platform.startswith("linux"):
        if default_works:
            return None
        candidates = LINUX_FILES
    else:
        return None
    for path in candidates:
        if usable(path):
            return path
    if bundled and usable(bundled):
        return bundled
    return None


def use_system_certificates(
    environ: MutableMapping[str, str] | None = None,
    platform: str | None = None,
    frozen: bool | None = None,
    bundle_dir: str | None = None,
) -> str | None:
    """Point OpenSSL at this system's certificates in the packaged app; the file chosen.

    Never raises: without a choice, HTTPS behaves as it did before.
    """
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not frozen or environ.get("SSL_CERT_FILE") or environ.get("SSL_CERT_DIR"):
        return None
    try:
        default_works = platform.startswith("linux") and openssl_default_works()
        bundle_dir = bundle_dir if bundle_dir is not None else getattr(sys, "_MEIPASS", None)
        chosen = pick_ca_file(platform, default_works, bundled_certifi(bundle_dir))
    except Exception:
        return None
    if chosen:
        environ["SSL_CERT_FILE"] = chosen
        environ[CHOSEN_BY_INKDOC] = chosen
    return chosen


def describe(environ: MutableMapping[str, str] | None = None, platform: str | None = None) -> str:
    """Where HTTPS certificates come from in this process, in one line."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    chosen = environ.get("SSL_CERT_FILE")
    if chosen:
        by = "InkDoc" if environ.get(CHOSEN_BY_INKDOC) == chosen else "the environment"
        return f"SSL_CERT_FILE={chosen} (set by {by})"
    if environ.get("SSL_CERT_DIR"):
        return f"SSL_CERT_DIR={environ['SSL_CERT_DIR']} (set by the environment)"
    if platform == "win32":
        return "the Windows certificate store"
    import ssl

    paths = ssl.get_default_verify_paths()
    return f"OpenSSL's own location ({paths.openssl_cafile}, {paths.openssl_capath})"


def https_selftest(attempts: int = 3, wait_s: float = 5.0) -> int:
    """Check HTTPS the way the app uses it; 0 when it works.

    Fetches the real update manifest through the updater's own code and checks its
    signature, then opens a connection to Hugging Face (GLM-OCR's downloads). An HTTP
    error still proves the certificate was accepted; a certificate error fails at once,
    other network errors are tried again. `inkdoc --selftest-https`, run for every release
    artifact by scripts/smoke_test_artifact.py.
    """
    import ssl
    import time
    import urllib.error
    import urllib.request

    from app.core.update_manager import UpdateManager
    from app.core.update_verifier import verify_envelope_bytes

    print(f"[*] HTTPS certificates: {describe()}")

    def manifest() -> str:
        raw = UpdateManager()._fetch_manifest_bytes()
        version = verify_envelope_bytes(raw)["version"]
        return f"update manifest for {version}, signature verified"

    def hugging_face() -> str:
        request = urllib.request.Request("https://huggingface.co/", method="HEAD",
                                         headers={"User-Agent": "InkDoc-Selftest"})
        with urllib.request.urlopen(request, timeout=15.0) as response:
            return f"HTTP {response.status}"

    failed = False
    for name, check in (("GitHub", manifest), ("Hugging Face", hugging_face)):
        for attempt in range(1, attempts + 1):
            try:
                print(f"[PASS] {name}: {check()}")
                break
            except urllib.error.HTTPError as err:
                print(f"[PASS] {name}: certificate accepted (HTTP {err.code})")
                break
            except Exception as err:
                reason = getattr(err, "reason", err)
                if isinstance(reason, ssl.SSLCertVerificationError) or attempt == attempts:
                    print(f"[FAIL] {name}: {reason}", file=sys.stderr)
                    failed = True
                    break
                print(f"[*] {name}: {reason}; trying again in {wait_s:.0f}s", file=sys.stderr)
                time.sleep(wait_s)
    return 1 if failed else 0
