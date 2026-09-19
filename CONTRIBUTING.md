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

## 4. Testing & Verification

Before submitting a pull request, run the complete verification suite locally:

```bash
# 1. Syntax check all source directories
python -m compileall -q main.py app tests examples

# 2. Run the integration test suite
python tests/test_server.py

# 3. Verify code style with ruff
ruff check .
```

---

## 5. Submitting Pull Requests

1. **Open an Issue First**: For non-trivial changes, new features, or architectural adjustments, open an issue first to discuss the design.
2. **Keep PRs Focused**: Keep pull requests focused on a single feature or bug fix.
3. **Describe Your Verification**: Document the exact commands you ran and operating systems tested in the PR description using the template.

---

## 6. Security Policy

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

## 7. Code of Conduct

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
