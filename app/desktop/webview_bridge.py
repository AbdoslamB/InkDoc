"""pywebview's page bridge under InkDoc's page policy.

The page allows scripts only from itself and its nonce, never `eval`
(`server.page_csp`). pywebview 6.2.1 builds `window.pywebview.api` with
`new Function(...)` and hands each call's result back to the page through
`Window.evaluate_js`, which wraps the script in `eval()`. WebKit, which draws the
Linux and macOS windows, refuses both under that policy, so the page never got
`pywebview.api` there: no Save dialog, no save folder, no links in the system
browser. WebView2 (Windows) doesn't apply the page's policy to pywebview's own
script, which is why Windows was never affected.

apply() makes both work without `eval`: each API function becomes a plain
closure, and call results go back through `run_js`, which runs the script as it
is. Only pywebview 6.2.1 is changed (requirements.txt pins it); for any other
version apply() changes nothing and says so. tests/test_webview_bridge.py fails
when an upgrade changes what is replaced, and `--selftest-bridge` checks a real
window in every release build.
"""
from __future__ import annotations

import sys

PATCHED_VERSION = "6.2.1"

# webview/js/api.js, _createApi: the one `new Function` in pywebview's page script.
NEW_FUNCTION = """      nestedObject[functionName] = new Function(
        sanitize_params(params),
        funcBody
      );"""
# The same function body as a closure. `funcName` is a `var` of the enclosing
# loop, so each function keeps its own copy.
CLOSURE = """      nestedObject[functionName] = (function (name) {
        return function () {
          var __id = (Math.random() + '').substring(2);
          var promise = new Promise(function (resolve, reject) {
            window.pywebview._checkValue(name, resolve, reject, __id);
          });
          window.pywebview._jsApiCallback(name, Array.prototype.slice.call(arguments), __id);
          return promise;
        };
      })(funcName);"""
# webview/util.py, js_bridge_call: how every API call's result (or error) returns.
RETURN_PREFIX = 'window.pywebview._returnValuesCallbacks["'
# Marks the replacements, so applying twice never wraps them twice.
_MARK = "_inkdoc_webview_bridge"


def patch_api_js(js_code: str) -> str:
    """pywebview's page script with the closure in place of `new Function`."""
    if js_code.count(NEW_FUNCTION) != 1:
        raise ValueError("pywebview's api.js no longer has the expected `new Function` block")
    return js_code.replace(NEW_FUNCTION, CLOSURE)


def installed_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("pywebview")
    except Exception:
        return None


def apply() -> bool:
    """Patch the imported pywebview (once); True when it is patched."""
    found = installed_version()
    if found != PATCHED_VERSION:
        print(f"[Warn] pywebview {found} is not {PATCHED_VERSION}: the page bridge is left as it is; "
              "on Linux and macOS the page may not reach the window's functions.", file=sys.stderr)
        return False

    try:
        import webview.util as util
        from webview.window import Window
    except Exception as exc:  # never keeps the window from opening
        print(f"[Warn] pywebview's page bridge could not be patched: {exc}", file=sys.stderr)
        return False

    if not getattr(util.load_js_files, _MARK, False):
        original_load = util.load_js_files

        def load_js_files(window, platform):
            js_code, finish_script = original_load(window, platform)
            return patch_api_js(js_code), finish_script

        setattr(load_js_files, _MARK, True)
        # inject_pywebview looks load_js_files up in util's globals at call time.
        util.load_js_files = load_js_files

    if not getattr(Window.evaluate_js, _MARK, False):
        original_evaluate = Window.evaluate_js

        def evaluate_js(self, script, callback=None):
            if callback is None and script.startswith(RETURN_PREFIX):
                return self.run_js(script)
            return original_evaluate(self, script, callback)

        setattr(evaluate_js, _MARK, True)
        Window.evaluate_js = evaluate_js
    return True
