# Contributing to InkDoc

Thank you for your interest in contributing to InkDoc! We welcome code contributions, bug reports, documentation enhancements, and feature suggestions.

---

## 1. Getting Started

### Fork and Clone
```bash
git clone https://github.com/AbdoslamB/inkdoc.git
cd inkdoc
```

### Environment Setup
Create a virtual environment and install development dependencies:
```bash
python -m venv .venv

# On Windows (PowerShell)
.venv\Scripts\Activate.ps1

# On macOS / Linux
source .venv/bin/activate

# Install dependencies
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install ruff
```

### Running Locally
```bash
# Run the Desktop Application (Edge WebView2 / WebKit)
python main.py

# Run in Headless API mode
python main.py --headless --port 13118
```

---

## 2. Project Architecture

The codebase follows a domain-driven structure:

```
inkdoc/
├── app/
│   ├── core/                  # Conversion business logic & engine adapters
│   │   ├── converter.py       # MarkItDown wrapper, option mapping & Downloads auto-save
│   │   ├── queue_model.py     # EngineKind enum, QueueItem data definitions
│   │   └── engines/           # IBM Docling & Markit engine adapters
│   ├── server/                # Embedded FastAPI backend
│   │   └── server.py          # Local REST endpoints (/convert, /health, /extensions)
│   ├── desktop/               # Native desktop runner
│   │   └── runner.py          # pywebview window manager & loopback orchestration
│   └── ui/                    # Shared Web & Desktop user interface
│       ├── index.html         # DOM layout, dropzone canvas, engine selector pills
│       ├── style.css          # Inkbench Obsidian design system (CSS custom properties)
│       └── app.js             # Reactive client-side logic & marked.js preview
├── tests/                     # Automated integration tests (test_server.py)
├── examples/                  # Developer client scripts & demos
├── API.md                     # REST API reference manual
└── main.py                    # Unified desktop & headless CLI entry point
```

---

## 3. Development Guidelines

### Code Style & Quality
* **Backend (Python)**:
  - Write clean, readable Python 3.10+ code with explicit type annotations where appropriate.
  - Conversions must never block the main loop; engine operations run asynchronously or in background workers.
  - Follow PEP 8 guidelines. Verify your code with `ruff check .`.
* **Frontend (Web/Desktop UI)**:
  - The UI in `app/ui/` is the single source of truth for both the desktop app and the web bench.
  - Use Vanilla HTML5, CSS Custom Properties, and modern standard JavaScript (ES6+ async/await).
  - Adhere to the Inkbench Obsidian design system defined in `app/ui/style.css`.
* **Cross-Platform Compatibility**:
  - Keep all features compatible with Windows, macOS, and Linux.
  - Use `pathlib.Path` for filesystem operations rather than platform-specific string concatenation.

---

## 4. Optional Engine Packs (Docling) & Manifest Management

InkDoc supports optional deep layout parsing via isolated engine packs (IBM Docling). Engine packs are distributed as signed release assets and must satisfy strict cryptographic verification.

### Integrity & Fail-Closed Policy
- **No Placeholders**: Never commit placeholder hashes (e.g. `PLACEHOLDER_*`), empty hashes, or truncated hashes to `app/core/manifest.json`.
- **Fail Closed**: Any missing hash, empty hash, placeholder, or hash that is not a 64-character hex SHA-256 aborts the installation before any archive extraction and marks the engine pack as unsupported for that build.
- **Release Guard**: CI and Release workflows enforce `python scripts/generate_engine_manifest.py --verify-manifest`. Builds fail immediately if the manifest contains placeholder or malformed entries.

### Building Packs and Generating Hashes
1. **Build the Pack**:
   ```bash
   python scripts/build_pack.py --output-dir dist/packs
   ```
2. **Compute Real SHA-256 Hashes and File Tree**:
   ```bash
   python scripts/generate_engine_manifest.py --archive dist/packs/docling-windows-x86_64.zip --platform windows-x86_64
   ```
3. **Update Manifest Directly**:
   ```bash
   python scripts/generate_engine_manifest.py --archive dist/packs/docling-windows-x86_64.zip --platform windows-x86_64 --update-manifest
   ```
4. **Verify Manifest Integrity**:
   ```bash
   python scripts/generate_engine_manifest.py --verify-manifest
   ```

---

## 5. Testing & Verification

Before submitting a pull request, run the complete verification suite locally:

```bash
# 1. Syntax check all source directories
python -m compileall -q main.py app tests examples scripts

# 2. Run the manifest release guard
python scripts/generate_engine_manifest.py --verify-manifest

# 3. Run the test suites
python tests/test_server.py
python tests/test_optional_engine.py

# 4. Verify code style with ruff
ruff check .
```

---

## 6. Submitting Pull Requests

1. **Open an Issue First**: For non-trivial changes, new features, or architectural adjustments, open an issue first to discuss the design.
2. **Keep PRs Focused**: Keep pull requests focused on a single feature or bug fix.
3. **Describe Your Verification**: Document the exact commands you ran and operating systems tested in the PR description using the template.

### Dependency Updates

All dependency updates (whether submitted manually or generated automatically by Dependabot) must satisfy the following requirements before merging:
1. **Automated CI Validation**: The PR must cleanly pass all checks in [`.github/workflows/ci.yml`](.github/workflows/ci.yml) across all supported platforms (Windows, macOS, and Linux).
2. **Smoke Test of Built App**: Changes to core desktop runtime dependencies (including `pywebview`, `fastapi`, and `uvicorn`) require a local smoke test of a built application package to verify that desktop window events, pywebview loopback orchestration, and PyInstaller bundling continue to function without regressions.

---

## 7. Security Policy

### Supported Versions
Only the latest release is supported with security fixes:

| Version | Supported |
| ------- | --------- |
| Latest  | ✅        |
| Older   | ❌        |

### Reporting a Vulnerability
If you find a security issue (for example, unsafe handling of untrusted input during file conversion, or something that could execute code from a malicious file), please **do not open a public issue**.

Instead, report it privately via [GitHub Security Advisories](https://github.com/AbdoslamB/inkdoc/security/advisories/new) for this repository. Include:

* A description of the vulnerability and its impact
* Steps to reproduce it, or a proof-of-concept file if relevant
* The affected version/commit

You will receive an initial response within a few days. Once a fix is available, a new release will be published and the advisory disclosed.

> [!NOTE]
> This app wraps Microsoft's [`markitdown`](https://github.com/microsoft/markitdown) package for primary file parsing and IBM [`docling`](https://github.com/DS4SD/docling) for layout analysis. If the issue originates upstream in `markitdown` or `docling` itself rather than in InkDoc's code, please also report it upstream following their security policies.

---

## 8. Code of Conduct

### Our Pledge
We as members, contributors, and leaders pledge to make participation in our community a harassment-free experience for everyone, regardless of age, body size, visible or invisible disability, ethnicity, sex characteristics, gender identity and expression, level of experience, education, socio-economic status, nationality, personal appearance, race, religion, or sexual identity and orientation.

We pledge to act and interact in ways that contribute to an open, welcoming, diverse, inclusive, and healthy community.

### Our Standards
Examples of behavior that contributes to a positive environment:
* Demonstrating empathy and kindness toward other people
* Being respectful of differing opinions, viewpoints, and experiences
* Giving and gracefully accepting constructive feedback
* Accepting responsibility and apologizing to those affected by mistakes, and learning from the experience
* Focusing on what is best not just for us as individuals, but for the overall community

Examples of unacceptable behavior:
* The use of sexualized language or imagery, and sexual attention or advances of any kind
* Trolling, insulting or derogatory comments, and personal or political attacks
* Public or private harassment
* Publishing others' private information, such as a physical or email address, without their explicit permission
* Other conduct which could reasonably be considered inappropriate in a professional setting

### Enforcement Responsibilities & Scope
Project maintainers are responsible for clarifying and enforcing our standards of acceptable behavior and will take appropriate and fair corrective action in response to any behavior that they deem inappropriate, threatening, offensive, or harmful.

Instances of abusive, harassing, or otherwise unacceptable behavior may be reported by contacting the maintainer directly through GitHub. All complaints will be reviewed and investigated promptly and fairly.

This Code of Conduct is adapted from the [Contributor Covenant](https://www.contributor-covenant.org), version 2.1.

---

## 9. Release Runbook

Maintainers follow a strict release candidate rehearsal procedure before publishing a stable release:

### 1. Release Candidate (RC) Rehearsal
1. **Prepare Branch**: Verify `CHANGELOG.md` is updated and CI is green on `main`. Always tag only from an up-to-date `main` branch:
   ```bash
   git checkout main
   git pull origin main
   ```
2. **Push RC Tag**:
   ```bash
   git tag v1.0.2-rc1
   git push origin v1.0.2-rc1
   ```
3. **Automated Pipeline**:
   - `create-draft-release` initializes a draft release marked as pre-release (`--prerelease`).
   - `build` matrix builds all platforms, runs full end-to-end smoke testing (`--selftest`, multi-format document conversions) against built artifacts, and uploads them to the draft.
   - `publish-release` publishes the draft with `--prerelease --latest=false`.
4. **Validation**: Test the uploaded artifacts on real hardware.

### 2. Recovery After a Failed Tag Run
If a build step or smoke test fails during a release run, the pipeline aborts without publishing:
1. **Delete the Draft Release and Remote Tag**:
   Use `gh release delete` with `--cleanup-tag` to delete both the GitHub draft release and the remote git tag:
   ```bash
   gh release delete v1.0.2-rc1 --yes --cleanup-tag
   ```
2. **Delete the Local Git Tag**:
   ```bash
   git tag -d v1.0.2-rc1
   ```
3. **Fix and Retry as Next RC**: Fix the issue on a feature/fix branch, merge to `main`, and push the next release candidate tag (`v1.0.2-rc2`). Never re-use or force-push an existing tag.

### 3. Stable Release
Once an RC is fully verified, tag only from an up-to-date `main`:
```bash
git checkout main
git pull origin main
git tag v1.0.2
git push origin v1.0.2
```
Tags without hyphens are automatically published as the official stable release with `--latest=true`.
