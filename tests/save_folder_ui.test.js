// Tests the Save folder row in app/ui/app.js (Settings > General).
//
// The folder is shown everywhere but changed only in the desktop app, through
// pywebview.api (the system's folder dialog). A fallback to Downloads is
// announced once per message, not once per file.
//
// Follows desktop_download_ui.test.js: slice the block out of the source, run
// it against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const start = source.indexOf("  // ─── Save folder ─");
const end = source.indexOf("  // ─── End save folder", start);
if (start < 0 || end < 0) throw new Error("could not locate the save folder block in app.js");
const block = source.slice(start, end);

function element() {
    return {
        textContent: "", title: "", disabled: false, style: { display: "none" }, listener: null,
        addEventListener(type, fn) { if (type === "click") this.listener = fn; },
    };
}

function setup({ api } = {}) {
    const els = {
        saveFolderPath: element(), saveFolderProblem: element(), saveFolderHint: element(),
        btnChangeSaveFolder: element(), btnResetSaveFolder: element(),
    };
    const toasts = [];
    const env = {
        window: api ? { pywebview: { api } } : {},
        document: { getElementById: (id) => els[id] },
        showToast: (msg, kind) => toasts.push({ msg, kind }),
        withWindowApi: (fn) => fn(api),
    };
    const fns = new Function("env", `
        const { window, document, showToast, withWindowApi } = env;
        ${block}
        return { renderSaveFolder, noteSaveFallback, state: () => saveFolderState };
    `)(env);
    return { ...fns, els, toasts };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
const shown = (el) => el.style.display !== "none";
const LRM = "\u200E";

const DEFAULT = { path: "C:\\Users\\me\\Downloads", chosen: "", custom: false, problem: "" };
const CUSTOM = { path: "D:\\Notes", chosen: "D:\\Notes", custom: true, problem: "" };
const MISSING = {
    path: "C:\\Users\\me\\Downloads", chosen: "E:\\USB\\Notes", custom: true,
    problem: "This folder was not found. Files are saved to Downloads until it is back.",
};

(async () => {
    // Desktop, default folder: Change shown, reset hidden, no warning.
    {
        const t = setup({ api: {} });
        t.renderSaveFolder(DEFAULT);
        assert.strictEqual(t.els.saveFolderPath.textContent, `${LRM}${DEFAULT.path}${LRM}`);
        assert.strictEqual(t.els.saveFolderPath.title, DEFAULT.path);
        assert.ok(shown(t.els.btnChangeSaveFolder));
        assert.ok(!shown(t.els.btnResetSaveFolder));
        assert.ok(!shown(t.els.saveFolderProblem));
        assert.ok(!shown(t.els.saveFolderHint));
    }

    // Custom folder: reset available. Missing folder: shows the chosen folder and the warning.
    {
        const t = setup({ api: {} });
        t.renderSaveFolder(CUSTOM);
        assert.ok(shown(t.els.btnResetSaveFolder));
        t.renderSaveFolder(MISSING);
        assert.strictEqual(t.els.saveFolderPath.title, "E:\\USB\\Notes");
        assert.ok(shown(t.els.saveFolderProblem));
        assert.strictEqual(t.els.saveFolderProblem.textContent, MISSING.problem);
    }

    // Web Bench: shown, but no way to change it.
    {
        const t = setup();
        t.renderSaveFolder(CUSTOM);
        assert.ok(!shown(t.els.btnChangeSaveFolder));
        assert.ok(!shown(t.els.btnResetSaveFolder));
        assert.ok(shown(t.els.saveFolderHint));
    }

    // Missing settings data leaves the row alone.
    {
        const t = setup({ api: {} });
        t.renderSaveFolder(undefined);
        assert.strictEqual(t.state(), null);
    }

    // Change: the bridge picks the folder; the page never sends a path.
    {
        const calls = [];
        const api = { choose_save_folder: (...args) => { calls.push(args); return Promise.resolve({ status: "saved", folder: CUSTOM }); } };
        const t = setup({ api });
        t.renderSaveFolder(DEFAULT);
        t.els.btnChangeSaveFolder.listener();
        assert.strictEqual(t.els.btnChangeSaveFolder.disabled, true);
        await settle();
        assert.deepStrictEqual(calls, [[]], "choose_save_folder takes no arguments");
        assert.strictEqual(t.els.saveFolderPath.title, "D:\\Notes");
        assert.deepStrictEqual(t.toasts, [{ msg: "Auto-save now uses D:\\Notes", kind: "success" }]);
        assert.strictEqual(t.els.btnChangeSaveFolder.disabled, false);
        assert.strictEqual(t.els.btnResetSaveFolder.disabled, false);
    }

    // Saved but not kept for next time: the warning is shown instead.
    {
        const api = { choose_save_folder: () => Promise.resolve({ status: "saved", folder: CUSTOM, warning: "couldn't be saved for next time" }) };
        const t = setup({ api });
        t.els.btnChangeSaveFolder.listener();
        await settle();
        assert.deepStrictEqual(t.toasts, [{ msg: "couldn't be saved for next time", kind: "info" }]);
    }

    // Reset goes through its own bridge method.
    {
        const api = { reset_save_folder: () => Promise.resolve({ status: "saved", folder: DEFAULT }) };
        const t = setup({ api });
        t.renderSaveFolder(CUSTOM);
        t.els.btnResetSaveFolder.listener();
        await settle();
        assert.ok(!shown(t.els.btnResetSaveFolder));
        assert.strictEqual(t.toasts[0].kind, "success");
    }

    // Cancel: nothing changes, nothing is said. Errors and rejections: one error toast.
    for (const [reply, expectToast] of [
        [() => Promise.resolve({ status: "cancelled" }), false],
        [() => Promise.resolve({ status: "error", message: "InkDoc can't write to that folder." }), true],
        [() => Promise.reject(new Error("bridge gone")), true],
        [() => { throw new Error("no api"); }, true],
        [() => Promise.resolve(undefined), true],
    ]) {
        const t = setup({ api: { choose_save_folder: reply } });
        t.renderSaveFolder(DEFAULT);
        t.els.btnChangeSaveFolder.listener();
        await settle();
        assert.strictEqual(t.els.saveFolderPath.title, DEFAULT.path, "the shown folder is unchanged");
        if (expectToast) {
            assert.strictEqual(t.toasts.length, 1);
            assert.strictEqual(t.toasts[0].kind, "error");
            assert.ok(t.toasts[0].msg.startsWith("Could not change the save folder: "));
        } else {
            assert.deepStrictEqual(t.toasts, []);
        }
        assert.strictEqual(t.els.btnChangeSaveFolder.disabled, false);
    }

    // Fallback notices: once per distinct message, reset after a new folder is chosen.
    {
        const api = { choose_save_folder: () => Promise.resolve({ status: "saved", folder: CUSTOM }) };
        const t = setup({ api });
        const a = "Saved to Downloads because the save folder E:\\USB\\Notes was not found.";
        const b = "Saved to Downloads because the save folder D:\\Notes can't be written to.";
        t.noteSaveFallback(a);
        t.noteSaveFallback(a);
        t.noteSaveFallback(a);
        t.noteSaveFallback(null);
        t.noteSaveFallback(undefined);
        t.noteSaveFallback(b);
        assert.deepStrictEqual(t.toasts.map((x) => x.msg), [a, b]);
        t.els.btnChangeSaveFolder.listener();
        await settle();
        t.noteSaveFallback(a);
        assert.strictEqual(t.toasts[t.toasts.length - 1].msg, a, "shown again after the folder changed");
    }

    // The conversion result path calls noteSaveFallback after the per-file toast,
    // since showToast replaces whatever toast is showing.
    const resultStart = source.indexOf("item.savedTo          = data.saved_to_downloads");
    const perFileToast = source.indexOf("showToast(`Converted ${label} via ${via}`", resultStart);
    const fallbackCall = source.indexOf("noteSaveFallback(data.save_fallback);", resultStart);
    assert.ok(resultStart > 0 && perFileToast > resultStart && fallbackCall > perFileToast,
        "noteSaveFallback must run after the per-file toast");

    // Settings shows the folder every time the popover opens (loadSettings runs then).
    const loadStart = source.indexOf("  async function loadSettings() {");
    const loadEnd = source.indexOf("\n  }\n", loadStart) >= 0
        ? source.indexOf("\n  }\n", loadStart)
        : source.indexOf("\r\n  }\r\n", loadStart);
    assert.ok(loadStart > 0 && loadEnd > loadStart, "could not locate loadSettings");
    assert.ok(source.slice(loadStart, loadEnd).includes("renderSaveFolder(s.save_folder);"),
        "loadSettings must render the save folder");

    console.log("[OK] save_folder_ui.test.js passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
