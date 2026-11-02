# The desktop window

`app/desktop/` opens InkDoc's own window around the shared UI and gives the page a small set of
native functions: title-bar controls, the Save dialog, the save folder and external links. The
window is Edge WebView2 on Windows and the system's WebKit on macOS and Linux. The files this
guide covers are listed in AGENTS.md's routing table.

## Rules

- The page gets exactly nine native functions, through `window.expose(...)`. Never pass an
  object as `js_api`.
- Methods that write files or settings refuse any page not served by InkDoc itself.
- The save folder is set only here, by the folder dialog (`choose_save_folder`).
- Links inside converted documents and release notes never load in the window.
- Never loosen the page's Content Security Policy to make pywebview work. Fix the bridge.
- `pywebview` stays pinned to the version `webview_bridge.py` was written for; move them
  together.

## How it works

### The runner

`runner.py`:

- Finds an available local loopback port (`13118` or ephemeral).
- Starts FastAPI in a background daemon thread (`uvicorn.Server`).
- Opens a native desktop window via `pywebview` (Microsoft Edge WebView2 on Windows) pointing
  to `http://127.0.0.1:<port>/InkDoc`.
- Gives the page nine functions as `pywebview.api.*` through
  `window.expose(*_page_functions(...))`: title-bar controls, `open_external_link`,
  `save_markdown` (the Download button's Save dialog), and `choose_save_folder` /
  `reset_save_folder` (the only way the save folder is set). Never pass an object as `js_api`:
  pywebview looks up `js_api` attributes by any name the page sends, underscore-named ones
  included, while exposed functions are found by exact name only.
  `tests/test_desktop_save.py` checks the nine names match `app.js` and that nothing else is
  reachable. Methods that write files or settings also refuse any page not served by InkDoc
  itself.
- In the desktop app, links inside converted documents and release notes never load in the
  window (`handleContentLinkClick` in `app.js`): web links open in the system browser, `#`
  links scroll, everything else is ignored.
- Handles graceful shutdown when the window is closed.
- If a window finds InkDoc already running on its port, it attaches to that process instead
  of starting a second server ("attach mode"; see AGENTS.md, "Known limitations (security)").

### The page bridge under the page's policy

`webview_bridge.py`: the page's policy (`server.page_csp`) forbids eval, and WebKit (the Linux
and macOS windows) enforces that against pywebview's own script: pywebview 6.2.1 builds
`pywebview.api` with `new Function` and returns call results through `evaluate_js`, which wraps
them in `eval`. `apply()` (called before the window opens) replaces both: a closure in
`api.js`, and `run_js` for results. Only for the pinned version (`pywebview==6.2.1` in
`requirements.txt`; move it only together with this file, `tests/test_webview_bridge.py` fails
otherwise). Never loosen the policy instead. `inkdoc --selftest-bridge` opens a window under
that exact policy and checks one page → app → page round trip;
`scripts/smoke_test_artifact.py` runs it for every release artifact.

### Windows title bar

`win_chrome.py` gives pywebview's frameless window the styles and window procedure Windows
needs for native behaviour: resizing from every edge, Snap Layouts on the maximise button, and
dragging by the custom title bar.

### Linux host libraries

`app/core/linux_host.py` makes the frozen Linux app use the host's GTK, WebKit2GTK and GLib
(the bundle carries none of them): it points `GI_TYPELIB_PATH` at the host's typelibs and gives
`xdg-open` the host's `LD_LIBRARY_PATH`. Without WebKit2GTK, `runner.py` opens the UI in the
browser and says what to install. Why the bundle carries none of them is in
[release.md](release.md).

### Certificates for HTTPS

`app/core/tls_trust.py` runs first thing in `main.py`, before the worker branch, so the
workers inherit its choice. The packaged app's OpenSSL looks for certificates where the build
machine kept them: on macOS the python.org framework's folder, which a Mac without that Python
lacks, and on Linux `/usr/lib/ssl`, which only Debian and Ubuntu have. In the packaged app on
macOS, and on Linux when that folder has no certificates, it sets `SSL_CERT_FILE` to the
system's own file (`/etc/ssl/cert.pem`; the Fedora, Arch and openSUSE bundles), else the
`certifi` file release.yml bundles (`--collect-data certifi`). Windows (its certificate
store), runs from source and a `SSL_CERT_FILE`/`SSL_CERT_DIR` the user set are left alone.
`INKDOC_SSL_CERT_FILE` marks InkDoc's choice so `linux_host.host_env()` leaves it out of
`xdg-open`. `inkdoc --selftest-https` fetches the real update manifest through the updater's
code and checks its signature; `scripts/smoke_test_artifact.py` runs it for every artifact
and in the linux-compat containers (Fedora exercises the fallback).

## Tests

- `tests/test_desktop_save.py`: the nine page functions and the Save dialog.
- `tests/test_webview_bridge.py`: what the bridge replaces in the pinned pywebview.
- `tests/test_window_handle.py`, `tests/test_titlebar_regions.py`: the Windows title bar.
- `tests/test_linux_host.py`, `tests/test_linux_browser_fallback.py`: Linux host libraries and
  the browser fallback.
- `tests/test_tls_trust.py`: which certificate file each system gets, the self-test, and the
  updater's connection errors.
- `tests/desktop_download_ui.test.js`, `tests/save_folder_ui.test.js`,
  `tests/preview_links_ui.test.js`: the page's side.

## Lessons

### WebKit enforces the page's policy against pywebview's own script
- **When:** you upgrade pywebview, change the page's policy, or a native function silently
  does nothing on macOS or Linux.
- **Lesson:** WebView2 let pywebview's `new Function` and `eval` through; WebKit refused them,
  so on Linux and macOS the page never got `pywebview.api` (no Save dialog, no save folder, no
  links). A desktop feature tested only on Windows is untested. Run
  `inkdoc --selftest-bridge` on each platform.
- **Evidence:** c7a1e08; `tests/test_webview_bridge.py`.

### Find this process's own window, never a window by its title
- **When:** native code needs InkDoc's window handle.
- **Lesson:** `FindWindowW(None, "InkDoc")` matches any process's window with that title. With
  a second InkDoc open, the title-bar setup went to the other window. Enumerate the top-level
  windows and take the visible one owned by this process.
- **Evidence:** 037ec81; `tests/test_window_handle.py`.

### Tests that start the desktop runner must undo its process-wide changes
- **When:** a test calls `run_desktop` or another function that sets process-wide state.
- **Lesson:** `run_desktop` sets the server's port, `INKDOC_DESKTOP_RUNNER` and the update
  manager's desktop mode for the whole process. Left behind, they failed later CORS,
  session-token and update tests in the same run. Restore them in the test's teardown.
- **Evidence:** 9c283fe.
