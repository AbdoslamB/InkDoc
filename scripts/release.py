#!/usr/bin/env python3
"""One-command InkDoc release driver.

Set VERSION below, run the script, and it carries the release from the current
working tree through to a verified, published release -- pausing only where a
human genuinely has to act.

    python scripts/release.py                 # preflight, plan, then run
    python scripts/release.py --dry-run       # show the plan, touch nothing
    python scripts/release.py --resume        # continue after offline signing
    python scripts/release.py --only preflight

What it does NOT do, on purpose
-------------------------------
Two steps are deliberately left to you, because automating them would remove the
security property they exist to provide:

1. `scripts/sign_manifest.py` unlocks an encrypted Ed25519 key with a passphrase.
   That key is what the in-app updater verifies against, and it never touches CI
   or disk in plaintext. A script that could sign unattended would defeat the
   threat model in docs/RELEASE_RUNBOOK.md section 1.

2. Publishing a stable release with "Set as the latest release" ticked. The
   updater resolves /releases/latest/download/, so marking a release Latest
   before its signed manifest is attached makes every client 404 -- which is
   exactly what v1.0.2 did. release.yml stops stable tags at a draft for this
   reason; this script respects that and stops with instructions.

Everything else -- pushing, waiting on CI, the conditional engine-pack rebuild,
the manifest commit-back, tagging, asset and provenance verification -- runs
without intervention.

Ordering, and why it is not linear
----------------------------------
build_pack.py copies exactly one repository file into an engine pack:
app/core/engines/worker.py. So a pack only needs rebuilding when that file has
changed since the last pack tag, and this script checks rather than assuming --
a pack build is roughly an hour across three runners.

When a pack IS rebuilt there is a cycle to respect: the pack workflow emits a
manifest.json that must be committed back into app/core/manifest.json, because
release.yml runs verify_engine_manifest_guard.py, which makes live HEAD/Range
requests against the published pack URLs. The app release therefore cannot start
until the pack release has finished and its manifest has landed on main.

Failure and resumption
----------------------
Progress is written to build/.release-state.json after every phase, so a failure
during a forty-minute CI wait resumes instead of restarting. Tags and release
assets are immutable in this project; the script asserts preconditions and
aborts rather than guessing, because every incident in this repo's history came
from a precondition nobody checked -- a pack that shipped a virtualenv which
could not start, a pack built from a tree without the enrichment worker, and a
commit made on a detached HEAD that a later checkout orphaned.
"""
from __future__ import annotations

# ─── The only line you normally change ───────────────────────────────────────
VERSION = "1.0.4"
# A tag containing a hyphen (e.g. "1.0.4-rc1") is a pre-release: release.yml
# publishes those automatically and they are never marked Latest, so the signing
# pause below does not apply to them.
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "build" / ".release-state.json"

REPO = "AbdoslamB/InkDoc"
WORKER_PATH = "app/core/engines/worker.py"
MANIFEST_PATH = REPO_ROOT / "app" / "core" / "manifest.json"

RELEASE_WORKFLOW = "Release"
CI_WORKFLOW = "CI"
PACK_WORKFLOW = "Build Engine Packs"

# Asserted by release.yml's publish-release job before it will go further.
REQUIRED_ASSETS = [
    "inkdoc-setup.exe",
    "inkdoc.exe",
    "inkdoc-windows.zip",
    "inkdoc-macos.zip",
    "inkdoc-linux.zip",
]
SIGNED_MANIFEST_ASSET = "inkdoc-update-manifest.json"

# Pack tags are immutable once published; the workflow refuses to reuse them.
BURNED_PACK_TAGS = {
    "docling-pack-v1",  # shipped without model weights
    "docling-pack-v2",  # shipped a virtualenv that could not start
    "docling-pack-v3",  # superseded
    "docling-pack-v4",  # built from a tree without the enrichment worker
}

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?(?:\+[0-9A-Za-z.\-]+)?$")

PHASES = ["preflight", "sync", "pack", "app", "sign", "verify"]

# Set from --dry-run in main(). Guards save_state so a rehearsal writes nothing.
DRY_RUN = False


# ─── Output ──────────────────────────────────────────────────────────────────

class Abort(Exception):
    """A precondition failed. The message explains what to do about it."""


def banner(text: str) -> None:
    print(f"\n\033[1m{'=' * 72}\n{text}\n{'=' * 72}\033[0m", flush=True)


def step(text: str) -> None:
    print(f"  [*] {text}", flush=True)


def ok(text: str) -> None:
    print(f"  \033[32m[OK]\033[0m {text}", flush=True)


def warn(text: str) -> None:
    print(f"  \033[33m[!]\033[0m {text}", flush=True)


def action(text: str) -> None:
    print(f"  \033[36m[>]\033[0m {text}", flush=True)


# ─── Process helpers ─────────────────────────────────────────────────────────

def run(
    cmd: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    cwd: Path | None = None,
    stream: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command. `stream` lets long CI waits print as they go."""
    if stream:
        proc = subprocess.run(cmd, cwd=cwd or REPO_ROOT)
        if check and proc.returncode != 0:
            raise Abort(f"Command failed ({proc.returncode}): {' '.join(cmd)}")
        return proc
    proc = subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise Abort(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{detail}")
    return proc


def out(cmd: list[str], **kw) -> str:
    """Stdout, stripped.

    The strip is right for the metadata this is used for -- branch names, tag
    lists, porcelain status, JSON -- but it makes this unsuitable for comparing
    file contents, because it eats the trailing newline that almost every file
    ends with. Ask git whether something differs; do not diff strings here.
    """
    return (run(cmd, **kw).stdout or "").strip()


def git(*args: str, **kw) -> str:
    return out(["git", *args], **kw)


def gh_json(args: list[str]) -> object:
    return json.loads(out(["gh", *args]) or "null")


# ─── State ───────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    # A rehearsal must leave nothing behind. Writing state during --dry-run made
    # the next real run believe preflight had already passed and skip it.
    if DRY_RUN:
        return
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def mark(state: dict, phase: str, **extra) -> None:
    state.setdefault("done", [])
    if phase not in state["done"]:
        state["done"].append(phase)
    state.update(extra)
    state["version"] = VERSION
    save_state(state)


# ─── CI helpers ──────────────────────────────────────────────────────────────

def find_run(workflow: str, head: str, timeout: float = 300.0) -> int:
    """Find the workflow run for a branch or tag, waiting for it to appear.

    A tag push takes a few seconds to register a run, so this polls rather than
    failing on the first empty result.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = gh_json([
            "run", "list", "-R", REPO, "--workflow", workflow,
            "--limit", "30", "--json", "databaseId,headBranch,status,conclusion",
        ]) or []
        for r in runs:
            if r.get("headBranch") == head:
                return int(r["databaseId"])
        time.sleep(5)
    raise Abort(
        f"No '{workflow}' run appeared for '{head}' within {int(timeout)}s.\n"
        f"Check https://github.com/{REPO}/actions"
    )


def watch_run(run_id: int, label: str) -> None:
    action(f"Watching {label} (run {run_id}) -- this can take a while")
    run(["gh", "run", "watch", str(run_id), "-R", REPO, "--exit-status", "--compact"],
        stream=True)
    conclusion = out([
        "gh", "run", "view", str(run_id), "-R", REPO, "--json", "conclusion",
        "-q", ".conclusion",
    ])
    if conclusion != "success":
        raise Abort(
            f"{label} concluded '{conclusion}'.\n"
            f"  gh run view {run_id} -R {REPO} --log-failed"
        )
    ok(f"{label} succeeded")


# ─── Phase 1: preflight ──────────────────────────────────────────────────────

def phase_preflight(args, state: dict) -> None:
    banner("PHASE 1/6  PREFLIGHT")

    if not SEMVER.match(VERSION):
        raise Abort(f"VERSION '{VERSION}' is not semantic (expected e.g. 1.0.4 or 1.0.4-rc1).")
    ok(f"VERSION {VERSION} is well formed")

    for tool in ("git", "gh"):
        if not shutil.which(tool):
            raise Abort(f"'{tool}' is not on PATH.")
    ok("git and gh are available")

    # The failure that orphaned a commit once already: committing on a detached
    # HEAD, then losing it to the next checkout.
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise Abort(
            "HEAD is detached. Commits made here are not on any branch and a later\n"
            "  checkout will orphan them. Create a branch first:\n"
            "      git switch -c <branch-name>"
        )
    ok(f"On branch '{branch}'")
    state["branch"] = branch

    run(["gh", "auth", "status"])
    remote = git("remote", "get-url", "origin")
    if REPO.lower() not in remote.lower():
        raise Abort(f"origin is '{remote}', expected it to point at {REPO}.")
    ok(f"gh authenticated, origin is {REPO}")

    dirty = git("status", "--porcelain")
    if dirty and not args.allow_dirty:
        raise Abort(
            "Working tree has uncommitted changes. Commit them (they must be in the\n"
            "  tag for the build to contain them), or re-run with --allow-dirty if\n"
            "  they are deliberately not part of this release:\n"
            + "\n".join(f"      {ln}" for ln in dirty.splitlines()[:12])
        )
    ok("Working tree is clean" if not dirty else "Working tree dirty (--allow-dirty)")

    tag = f"v{VERSION}"
    if git("tag", "--list", tag):
        raise Abort(f"Tag {tag} already exists locally. Bump VERSION or delete it.")
    if out(["git", "ls-remote", "--tags", "origin", tag]):
        raise Abort(f"Tag {tag} already exists on origin. Bump VERSION.")
    # release.yml aborts if a release or draft already exists for the tag.
    existing = run(["gh", "release", "view", tag, "-R", REPO], check=False)
    if existing.returncode == 0:
        raise Abort(f"A release or draft already exists for {tag}. Bump VERSION.")
    ok(f"Tag {tag} is free locally, on origin, and as a release")

    changelog = REPO_ROOT / "CHANGELOG.md"
    if changelog.is_file():
        text = changelog.read_text(encoding="utf-8", errors="replace")
        if f"## [{VERSION}]" in text:
            ok(f"CHANGELOG has a [{VERSION}] section")
        else:
            warn(
                f"CHANGELOG.md has no '## [{VERSION}]' section. The release notes are "
                "generated by GitHub, so this is not fatal, but the changelog will lag."
            )

    if args.skip_tests:
        warn("Local test suite skipped (--skip-tests)")
    else:
        py = sys.executable
        step("ruff check .")
        run([py, "-m", "ruff", "check", "."], stream=True)
        step("compileall")
        run([py, "-m", "compileall", "-q", "main.py", "app", "tests", "scripts"], stream=True)
        step("pytest tests/")
        run([py, "-m", "pytest", "tests/", "-q"], stream=True)
        ok("Lint, compile and tests pass")

    mark(state, "preflight")


# ─── Phase 2: sync ───────────────────────────────────────────────────────────

def phase_sync(args, state: dict) -> None:
    banner("PHASE 2/6  PUSH AND WAIT FOR CI")
    branch = state.get("branch") or git("rev-parse", "--abbrev-ref", "HEAD")

    if args.dry_run:
        action(f"[dry-run] would push '{branch}' to origin and wait for {CI_WORKFLOW}")
        return

    action(f"Pushing '{branch}' to origin")
    run(["git", "push", "-u", "origin", branch], stream=True)
    ok("Pushed")

    if branch == "main":
        watch_run(find_run(CI_WORKFLOW, branch), f"{CI_WORKFLOW} on {branch}")
        mark(state, "sync")
        return

    # Tagging off a feature branch works: release.yml triggers on the tag pattern
    # alone, only ever reads github.ref_name, and Actions checks out the tag's
    # commit. Requiring main is therefore a policy, not a constraint. It stays the
    # default because shipping unmerged code as a stable release is usually a
    # mistake, but --allow-branch opts out -- which is what a release candidate
    # cut from a feature branch needs.
    pr = gh_json(["pr", "list", "-R", REPO, "--head", branch, "--json", "number,url"]) or []

    if not args.allow_branch:
        lines = [f"Branch '{branch}' is not main."]
        if pr:
            lines.append(f"  Open PR: {pr[0]['url']}")
        lines += [
            "  By default releases are cut from main. Either merge first:",
            "      git switch main && git pull",
            "      python scripts/release.py --resume",
            "  or release from this branch deliberately:",
            "      python scripts/release.py --allow-branch --resume",
        ]
        raise Abort("\n".join(lines))

    warn(f"Releasing from '{branch}' rather than main (--allow-branch)")
    if "-" not in VERSION:
        warn(f"v{VERSION} is a STABLE tag cut from an unmerged branch.")

    if pr:
        watch_run(find_run(CI_WORKFLOW, branch), f"{CI_WORKFLOW} for {pr[0]['url']}")
    else:
        # ci.yml triggers only on pushes to main and on pull requests, so there is
        # no run to wait for here. Say so rather than blocking on one that will
        # never appear -- preflight already ran the same ruff/compileall/pytest set.
        warn(
            f"No PR for '{branch}', so {CI_WORKFLOW} will not run against it. "
            "Preflight ran the same checks locally."
        )
        if args.skip_tests:
            raise Abort(
                "Refusing to release with neither CI nor local tests: --skip-tests "
                "was given and no PR exists for this branch.\n"
                "  Drop --skip-tests, or open a PR so CI runs."
            )

    mark(state, "sync")


# ─── Phase 3: engine pack (conditional) ──────────────────────────────────────

def last_pack_tag() -> str | None:
    tags = git("tag", "--list", "docling-pack-v*").split()
    if not tags:
        return None
    def n(t: str) -> int:
        m = re.search(r"v(\d+)$", t)
        return int(m.group(1)) if m else -1
    return max(tags, key=n)


def next_pack_tag() -> str:
    nums = [int(m.group(1))
            for t in git("tag", "--list", "docling-pack-v*").split()
            if (m := re.search(r"v(\d+)$", t))]
    nums += [int(m.group(1)) for t in BURNED_PACK_TAGS if (m := re.search(r"v(\d+)$", t))]
    return f"docling-pack-v{max(nums, default=0) + 1}"


def phase_pack(args, state: dict) -> None:
    banner("PHASE 3/6  ENGINE PACK (conditional)")

    prev = last_pack_tag()
    if args.force_pack:
        changed = True
        step("--force-pack given")
    elif prev is None:
        changed = True
        step("No previous pack tag found")
    else:
        # The only repository file build_pack.py copies into a pack.
        diff = run(["git", "diff", "--quiet", prev, "HEAD", "--", WORKER_PATH], check=False)
        changed = diff.returncode != 0
        step(f"{WORKER_PATH} {'changed' if changed else 'unchanged'} since {prev}")

    if not changed:
        ok("Engine pack is up to date -- skipping a ~1 hour rebuild")
        mark(state, "pack", pack_skipped=True)
        return

    tag = next_pack_tag()
    if tag in BURNED_PACK_TAGS:
        raise Abort(f"{tag} is published and immutable. Bump past it.")
    ok(f"Next pack tag: {tag}")

    if args.dry_run:
        action(f"[dry-run] would tag {tag}, watch {PACK_WORKFLOW}, commit its manifest")
        return

    # A pack built from a tree without the current worker silently ships a worker
    # that ignores its options. That is exactly how docling-pack-v4 was burned.
    #
    # Ask git rather than comparing file contents: `out()` strips, which ate the
    # trailing newline and made this fail for every file that ends in one -- that
    # is, all of them. git also gets autocrlf and .gitattributes right, which a
    # hand-rolled CRLF normalisation does not.
    worker_dirty = run(
        ["git", "status", "--porcelain", "--", WORKER_PATH], check=False
    ).stdout.strip()
    if worker_dirty:
        raise Abort(
            f"{WORKER_PATH} has uncommitted changes:\n"
            f"      {worker_dirty}\n"
            "  The pack is built from the tag's tree, so they would not reach it.\n"
            "  Commit them first."
        )
    ok(f"{WORKER_PATH} is committed and matches HEAD")

    action(f"Tagging and pushing {tag}")
    run(["git", "tag", tag])
    run(["git", "push", "origin", tag], stream=True)
    watch_run(find_run(PACK_WORKFLOW, tag), f"{PACK_WORKFLOW} ({tag})")

    # Pull the number out first: a backslash inside an f-string needs 3.12+,
    # and this has to run on whatever Python the maintainer has.
    pack_num = re.search(r"v(\d+)$", tag).group(1)
    expected_version = pack_num + ".0.0"
    dest = REPO_ROOT / "build" / "packs"
    dest.mkdir(parents=True, exist_ok=True)
    action("Downloading the merged manifest")
    run(["gh", "release", "download", tag, "-R", REPO,
         "--pattern", "manifest.json", "--dir", str(dest), "--clobber"], stream=True)

    data = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    got = data.get("pack_version")
    if got != expected_version:
        raise Abort(
            f"Published pack reports pack_version '{got}', expected '{expected_version}'.\n"
            "  That means the tag was cut from a tree without the workflow's\n"
            "  --pack-version wiring, so the pack cannot be told apart from older\n"
            "  ones. Do not ship it: fix main and cut the next tag."
        )
    ok(f"Manifest reports pack_version {got}")

    shutil.copyfile(dest / "manifest.json", MANIFEST_PATH)
    if git("status", "--porcelain", "--", str(MANIFEST_PATH.relative_to(REPO_ROOT))):
        run(["git", "add", str(MANIFEST_PATH.relative_to(REPO_ROOT))])
        run(["git", "commit", "-m", f"chore(docling): ship the {tag} manifest"])
        run(["git", "push"], stream=True)
        ok("Manifest committed and pushed")
    else:
        ok("Manifest already current")

    mark(state, "pack", pack_tag=tag)


# ─── Phase 4: app release ────────────────────────────────────────────────────

def phase_app(args, state: dict) -> None:
    banner("PHASE 4/6  APP RELEASE")
    tag = f"v{VERSION}"

    # release.yml runs this guard against the live pack URLs before it builds.
    # Running it here turns a forty-minute CI failure into an instant one.
    step("verify_engine_manifest_guard.py (live asset checks)")
    run([sys.executable, "scripts/verify_engine_manifest_guard.py"], stream=True)
    ok("Engine manifest guard passes")

    if args.dry_run:
        action(f"[dry-run] would tag {tag}, watch {RELEASE_WORKFLOW}, verify draft assets")
        return

    action(f"Tagging and pushing {tag}")
    run(["git", "tag", tag])
    run(["git", "push", "origin", tag], stream=True)
    watch_run(find_run(RELEASE_WORKFLOW, tag), f"{RELEASE_WORKFLOW} ({tag})")

    assets = {a["name"] for a in (gh_json(
        ["release", "view", tag, "-R", REPO, "--json", "assets"]) or {}).get("assets", [])}
    missing = [a for a in REQUIRED_ASSETS if a not in assets]
    if missing:
        raise Abort(f"Draft {tag} is missing required assets: {', '.join(missing)}")
    ok(f"All {len(REQUIRED_ASSETS)} required assets present on the draft")

    mark(state, "app", tag=tag)


# ─── Phase 5: provenance and offline signing ─────────────────────────────────

def _download_assets(tag: str) -> Path:
    dest = REPO_ROOT / "build" / "verify"
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    run(["gh", "release", "download", tag, "-R", REPO, "--dir", str(dest), "--clobber"],
        stream=True)
    return dest


def _verify_provenance(tag: str) -> None:
    """Check every binary against its GitHub build attestation."""
    dest = _download_assets(tag)
    failures: list[str] = []
    checked = 0
    for f in sorted(dest.iterdir()):
        # Checksums and the signed manifest are produced outside the build job,
        # so they carry no build provenance of their own.
        if not f.is_file() or f.name == SIGNED_MANIFEST_ASSET or f.name.startswith("SHA256SUMS"):
            continue
        checked += 1
        if run(["gh", "attestation", "verify", str(f), "--repo", REPO],
               check=False).returncode == 0:
            ok(f"provenance verified: {f.name}")
        else:
            failures.append(f.name)
    if failures:
        raise Abort("Provenance verification failed for: " + ", ".join(failures))
    if not checked:
        raise Abort(f"No binaries downloaded from {tag} to verify.")


def confirm(question: str, *, assume_yes: bool) -> bool:
    """Ask a yes/no question. Declines rather than throwing when there is no tty."""
    if assume_yes:
        print(f"  {question} [auto-yes]")
        return True
    try:
        return input(f"  {question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        print("\n  No terminal to confirm on. Re-run with --yes to proceed.")
        return False


def phase_sign(args, state: dict) -> None:
    banner("PHASE 5/6  PROVENANCE AND SIGNING")
    tag = f"v{VERSION}"

    if args.dry_run:
        if "-" in VERSION:
            action("[dry-run] pre-release: publishes automatically, nothing to sign")
        else:
            action("[dry-run] would verify provenance, then run sign_manifest.py")
        return

    if "-" in VERSION:
        ok("Pre-release: release.yml publishes it automatically, nothing to sign")
        mark(state, "sign")
        return

    assets = {a["name"] for a in (gh_json(
        ["release", "view", tag, "-R", REPO, "--json", "assets"]) or {}).get("assets", [])}
    if SIGNED_MANIFEST_ASSET in assets:
        ok(f"{SIGNED_MANIFEST_ASSET} is already attached -- signing already done")
        mark(state, "sign")
        return

    # The runbook verifies provenance before signing, and so does this: signing
    # vouches for these bytes, so their origin should be established first.
    step("Verifying build provenance before signing")
    _verify_provenance(tag)

    key_file = Path.home() / ".inkdoc-keys" / "inkdoc_signing_key.pem"
    if not key_file.is_file():
        warn(f"No signing key at {key_file}")
        warn("sign_manifest.py will tell you where it expects one.")

    print()
    print("  The signing key is encrypted and its passphrase is never stored, so")
    print("  this next step asks you for it directly. Nothing is published by it:")
    print("  it only attaches the signed manifest to the draft.")
    print()
    if not confirm(f"Sign and upload the update manifest for {tag}?", assume_yes=args.yes):
        raise Abort(
            "Signing declined. The draft is intact; re-run with --resume when ready."
        )

    # stream=True inherits this terminal, which is what lets getpass prompt for
    # the passphrase normally. Capturing stdio here would hang on a hidden prompt.
    step("Running scripts/sign_manifest.py (it will prompt for your passphrase)")
    run([sys.executable, "scripts/sign_manifest.py", "--tag", tag], stream=True)

    assets = {a["name"] for a in (gh_json(
        ["release", "view", tag, "-R", REPO, "--json", "assets"]) or {}).get("assets", [])}
    if SIGNED_MANIFEST_ASSET not in assets:
        raise Abort(
            f"sign_manifest.py finished but {SIGNED_MANIFEST_ASSET} is not attached "
            f"to {tag}. Check its output above."
        )
    ok(f"{SIGNED_MANIFEST_ASSET} attached to the draft")
    mark(state, "sign")


# ─── Phase 6: final verification and publication ─────────────────────────────

def phase_verify(args, state: dict) -> None:
    banner("PHASE 6/6  VERIFY AND PUBLISH")
    tag = f"v{VERSION}"

    if args.dry_run:
        action("[dry-run] would verify every asset, then offer to publish")
        return

    info = gh_json(["release", "view", tag, "-R", REPO,
                    "--json", "assets,isDraft,isPrerelease,url"]) or {}
    assets = {a["name"] for a in info.get("assets", [])}

    missing = [a for a in REQUIRED_ASSETS if a not in assets]
    if missing:
        raise Abort(f"Release {tag} is missing: {', '.join(missing)}")
    ok(f"All {len(REQUIRED_ASSETS)} binaries present")

    if any(a.startswith("SHA256SUMS") for a in assets):
        ok("Checksum files present")
    else:
        warn("No SHA256SUMS-*.txt asset found")

    is_pre = bool(info.get("isPrerelease"))
    if not is_pre:
        if SIGNED_MANIFEST_ASSET not in assets:
            raise Abort(
                f"{SIGNED_MANIFEST_ASSET} is not attached to {tag}.\n"
                f"  Run: python scripts/release.py --resume"
            )
        ok(f"{SIGNED_MANIFEST_ASSET} is attached")

    # Phase 5 verified provenance before signing; re-run only if it was skipped
    # (a pre-release, or a resume that found the manifest already attached).
    if "sign" not in set(state.get("done", [])) or is_pre:
        step("Verifying build provenance")
        _verify_provenance(tag)

    if not info.get("isDraft"):
        ok("Release is already published")
        mark(state, "verify")
        banner("RELEASE COMPLETE")
        print(f"  {info.get('url', '')}\n")
        return

    if is_pre:
        ok("Pre-release published by CI")
        mark(state, "verify")
        return

    # The one decision left that a script should not make on its own.
    print()
    print("  Everything checks out. Publishing marks this release \033[1mLatest\033[0m, which")
    print("  is what /releases/latest/download/ resolves to -- so every existing")
    print("  install will see it as the update on their next check.")
    print()
    print(f"  Draft: {info.get('url', '')}")
    print()
    if not confirm(f"Publish {tag} and mark it Latest?", assume_yes=args.yes):
        mark(state, "verify")
        banner("VERIFIED, NOT PUBLISHED")
        print(
            f"  The draft is complete and signed. Publish it when you are ready:\n\n"
            f"      gh release edit {tag} -R {REPO} --draft=false --latest\n\n"
            f"  or re-run: python scripts/release.py --resume\n"
        )
        return

    step("Publishing and marking Latest")
    run(["gh", "release", "edit", tag, "-R", REPO, "--draft=false", "--latest"], stream=True)

    final = gh_json(["release", "view", tag, "-R", REPO,
                     "--json", "isDraft,url"]) or {}
    if final.get("isDraft"):
        raise Abort(f"{tag} is still a draft after publishing. Check the GitHub UI.")

    # Ask the API which release is actually Latest rather than reading a field on
    # this one: `--json isLatest` is rejected by older gh builds, and what matters
    # is the pointer the updater resolves, not a flag on the release object.
    latest = out(["gh", "api", f"repos/{REPO}/releases/latest", "--jq", ".tag_name"],
                 check=False)
    if latest != tag:
        raise Abort(
            f"{tag} published, but GitHub still reports '{latest or 'nothing'}' as\n"
            f"  Latest. The updater resolves /releases/latest/download/, so it would\n"
            f"  serve the wrong release. Fix with:\n"
            f"      gh release edit {tag} -R {REPO} --latest"
        )
    ok(f"Published, and GitHub reports {tag} as Latest")

    mark(state, "verify")
    banner("RELEASE COMPLETE")
    print(f"  {final.get('url', '')}\n")



PHASE_FUNCS = {
    "preflight": phase_preflight,
    "sync": phase_sync,
    "pack": phase_pack,
    "app": phase_app,
    "sign": phase_sign,
    "verify": phase_verify,
}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="Plan only; change nothing")
    p.add_argument("--resume", action="store_true", help="Skip phases already recorded as done")
    p.add_argument("--only", choices=PHASES, help="Run a single phase")
    p.add_argument("--force-pack", action="store_true", help="Rebuild the engine pack unconditionally")
    p.add_argument("--skip-tests", action="store_true", help="Skip the local test suite in preflight")
    p.add_argument("--allow-dirty", action="store_true", help="Proceed with uncommitted changes")
    p.add_argument("--allow-branch", action="store_true",
                   help="Release from the current branch instead of requiring main")
    p.add_argument("--yes", action="store_true", help="Do not prompt before the first push")
    args = p.parse_args()

    global DRY_RUN
    DRY_RUN = args.dry_run

    state = load_state() if args.resume else {}
    if args.resume and state.get("version") not in (None, VERSION):
        print(f"[!] Saved state is for {state.get('version')}, not {VERSION}. Starting fresh.")
        state = {}
    done = set(state.get("done", []))

    # Decisions stick across a resume. Having to remember which flags the first
    # invocation used is exactly the kind of thing that goes wrong halfway
    # through an hour-long release.
    for flag in ("allow_branch", "allow_dirty", "force_pack", "skip_tests"):
        if getattr(args, flag):
            state[flag] = True
        elif state.get(flag):
            setattr(args, flag, True)
            warn(f"--{flag.replace('_', '-')} carried over from the earlier run")
    if not args.dry_run:
        save_state(state)

    todo = [args.only] if args.only else [ph for ph in PHASES if ph not in done]
    if not todo:
        print("Nothing to do -- every phase is already recorded as complete.")
        return 0

    banner(f"InkDoc release v{VERSION}"
           + ("  [DRY RUN]" if args.dry_run else "")
           + (f"\nresuming; already done: {', '.join(sorted(done))}" if done else ""))
    print("  Plan: " + " -> ".join(todo))

    if not args.dry_run and not args.yes and not args.only:
        print("\n  This pushes commits and tags to GitHub. Tags and release assets in this")
        print("  project are immutable -- a mistake costs a version number.")
        try:
            answer = input("  Continue? [y/N] ").strip().lower()
        except EOFError:
            # No terminal attached (piped input, a wrapper script, CI). Declining
            # is the safe reading, and it beats an unhandled traceback.
            print("\n  No terminal to confirm on. Re-run with --yes to proceed.")
            return 1
        if answer not in ("y", "yes"):
            print("  Aborted.")
            return 1

    try:
        for phase in todo:
            PHASE_FUNCS[phase](args, state)
    except Abort as exc:
        print(f"\n\033[31m[ABORT]\033[0m {exc}\n", file=sys.stderr)
        # Only add the generic hint when the message did not already give a
        # specific command. Printing "--resume" under an abort that says to use
        # "--allow-branch --resume" contradicts the instruction above it.
        if "release.py" not in str(exc):
            print("  Fix the above, then: python scripts/release.py --resume", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[!] Interrupted. Re-run with --resume to continue.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
