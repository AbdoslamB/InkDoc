// Tests the Download button in app/ui/app.js (issue #6).
//
// In the desktop window pywebview cancels <a download>, so the button must save
// through pywebview.api.save_markdown (a native Save dialog) and report what
// really happened. In a plain browser (Web Bench) the old Blob download stays.
//
// Follows preview_word_count.test.js: slice the blocks out of the source, run
// them against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");

function slice(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    if (start < 0 || end < 0) throw new Error(`could not locate ${JSON.stringify(startMarker)} in app.js`);
    return source.slice(start, end);
}

const desktopSave = slice("  // ─── Desktop save (Download button)", "  // ─── End desktop save");
const clickHandler = slice('  btnDownloadMarkdown.addEventListener("click"', "  // ─── Tab Switching");

function setup({ pywebview, editorValue = "" } = {}) {
    const toasts = [];
    const anchors = [];
    const button = {
        disabled: false,
        listener: null,
        addEventListener(type, fn) { if (type === "click") this.listener = fn; },
    };
    const env = {
        window: pywebview ? { pywebview } : {},
        document: {
            createElement: () => { const a = { click() { a.clicked = true; } }; anchors.push(a); return a; },
            body: { appendChild() {}, removeChild() {} },
        },
        Blob: function Blob(parts, opts) { this.parts = parts; this.opts = opts; },
        URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
        showToast: (msg, kind) => toasts.push({ msg, kind }),
        withWindowApi: (fn) => fn(pywebview.api),
        rawEditor: { value: editorValue },
        btnDownloadMarkdown: button,
        queue: [{ id: 1, name: "Quarterly report.pdf", markdown: "# converted\n" }],
        activeItemId: 1,
    };
    new Function("env", `
        const { window, document, Blob, URL, showToast, withWindowApi, rawEditor, btnDownloadMarkdown } = env;
        const queue = env.queue;
        const activeItemId = env.activeItemId;
        ${desktopSave}
        ${clickHandler}
    `)(env);
    return { click: () => button.listener(), button, toasts, anchors };
}

function desktopApi(reply) {
    const calls = [];
    return {
        calls,
        api: { save_markdown: (...args) => { calls.push(args); return typeof reply === "function" ? reply() : Promise.resolve(reply); } },
    };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
    // Desktop, saved: the dialog route is used, never the <a download> one.
    {
        const { api, calls } = desktopApi({ status: "saved", path: "C:\\Users\\me\\Downloads\\Quarterly report.md" });
        const t = setup({ pywebview: { api } });
        t.click();
        assert.strictEqual(t.button.disabled, true, "button stays disabled while the dialog is open");
        await settle();
        assert.deepStrictEqual(calls, [["Quarterly report.md", "# converted\n"]]);
        assert.strictEqual(t.anchors.length, 0, "desktop must not fall back to <a download>");
        assert.deepStrictEqual(t.toasts, [{ msg: "Saved to C:\\Users\\me\\Downloads\\Quarterly report.md", kind: "success" }]);
        assert.strictEqual(t.button.disabled, false);
    }

    // Edited text in the Raw tab is what gets saved.
    {
        const { api, calls } = desktopApi({ status: "saved", path: "/tmp/x.md" });
        const t = setup({ pywebview: { api }, editorValue: "# edited\n" });
        t.click();
        await settle();
        assert.strictEqual(calls[0][1], "# edited\n");
    }

    // Cancelled: no toast at all, button usable again.
    {
        const { api } = desktopApi({ status: "cancelled" });
        const t = setup({ pywebview: { api } });
        t.click();
        await settle();
        assert.deepStrictEqual(t.toasts, []);
        assert.strictEqual(t.button.disabled, false);
    }

    // Error reply, rejected promise, and a throwing bridge all show an error toast.
    for (const reply of [
        { status: "error", message: "disk full" },
        () => Promise.reject(new Error("bridge gone")),
        () => { throw new Error("no api"); },
        undefined,
    ]) {
        const { api } = desktopApi(reply);
        const t = setup({ pywebview: { api } });
        t.click();
        await settle();
        assert.strictEqual(t.toasts.length, 1, `one toast for ${String(reply)}`);
        assert.strictEqual(t.toasts[0].kind, "error");
        assert.ok(t.toasts[0].msg.startsWith("Could not save the file: "));
        assert.ok(!t.toasts.some((x) => x.kind === "success"), "never claims success on failure");
        assert.strictEqual(t.button.disabled, false);
    }

    // Web Bench (no pywebview): the browser download is unchanged.
    {
        const t = setup();
        t.click();
        await settle();
        assert.strictEqual(t.anchors.length, 1);
        assert.strictEqual(t.anchors[0].download, "Quarterly report.md");
        assert.strictEqual(t.anchors[0].href, "blob:x");
        assert.strictEqual(t.anchors[0].clicked, true);
        assert.deepStrictEqual(t.toasts, [{ msg: "Downloaded Quarterly report.md", kind: "success" }]);
    }

    console.log("[OK] desktop_download_ui.test.js passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
