"""Fail-closed validation of the optional add-on catalogue.

The pack manifest already has this guard -- `generate_engine_manifest.py
--verify-manifest` rejects placeholder and malformed hashes before a release can
go out. `app/core/addons.json` had no equivalent, which is how it came to contain
a literal `"<publish the archive and put its URL here>"` URL: `build_addon.py`
printed an entry for a human to paste, nothing checked what was pasted, and
`get_status` treated any non-empty string as a usable URL.

Validation itself lives in `AddonManager.validate_addon_entry`, not here, so the
running application, the installer and this guard can never disagree about what a
valid entry is. This script only decides which states are acceptable in which
context:

    # CI on every push: defects fail, an unreleased add-on is fine.
    python scripts/verify_addon_catalogue.py

    # Release guard: every add-on must additionally be published.
    python scripts/verify_addon_catalogue.py --require-published
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.addon_manager import (  # noqa: E402
    ADDONS_CATALOGUE_PATH,
    AddonManager,
    CatalogueState,
)


def verify(catalogue_path: Path, require_published: bool) -> tuple[bool, list[str]]:
    """Return (ok, messages). Never raises for catalogue content problems."""
    messages: list[str] = []

    if not catalogue_path.is_file():
        return False, [f"Catalogue not found: {catalogue_path}"]

    mgr = AddonManager(catalogue_path=catalogue_path)
    names = mgr.list_addon_names()
    if not names:
        # An empty catalogue is almost certainly a broken file rather than an
        # intentional state, and silently passing would defeat the guard.
        return False, [f"Catalogue at {catalogue_path} declares no add-ons"]

    ok = True
    for name in names:
        state, defects = mgr.catalogue_state(name)

        if state is CatalogueState.DEFECTIVE:
            ok = False
            messages.append(f"[FAIL] {name}: defective entry")
            messages.extend(f"          - {d}" for d in defects)
            continue

        if state is CatalogueState.UNRELEASED:
            if require_published:
                ok = False
                messages.append(
                    f"[FAIL] {name}: unreleased (url and sha256 are empty), "
                    "but --require-published was given"
                )
            else:
                messages.append(
                    f"[ OK ] {name}: unreleased -- reported unavailable, "
                    "install is refused, enrichment toggles stay disabled"
                )
            continue

        addon = mgr.get_addon(name)
        messages.append(
            f"[ OK ] {name}: published -- {addon.size_bytes / 1e6:.1f} MB, "
            f"{len(addon.sha256_files)} hashed files, requires pack "
            f"{addon.min_pack_version}+"
        )

    return ok, messages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalogue",
        type=Path,
        default=ADDONS_CATALOGUE_PATH,
        help="Catalogue to check (default: app/core/addons.json)",
    )
    parser.add_argument(
        "--require-published",
        action="store_true",
        help="Also fail when an add-on has no published artifact yet",
    )
    args = parser.parse_args()

    ok, messages = verify(args.catalogue, args.require_published)
    print(f"[*] Verifying add-on catalogue: {args.catalogue}")
    for line in messages:
        print(line)

    if ok:
        print("[OK] Add-on catalogue is valid.")
        return 0
    print("[FAIL] Add-on catalogue is invalid. Refusing to pass.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
