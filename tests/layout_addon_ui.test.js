// Tests the layout model add-on card in app/ui/app.js (Settings > Add-ons).
//
// The card shows what GET /addons/layout_model says: ready when the Docling
// engine carries the model (nothing to download), installable when the add-on
// is published, installed, downloading, or not available (with the reason).
// Its status also decides whether "Tables on scanned pages" is greyed out.
//
// Follows tables_ui.test.js: slice the blocks out of the source, run them
// against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const html = fs.readFileSync("app/ui/index.html", "utf8");
const css = fs.readFileSync("app/ui/style.css", "utf8");

function slice(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    if (start < 0 || end < 0) throw new Error(`could not locate ${startMarker.trim()} in app.js`);
    return source.slice(start, end);
}

const block = slice("  // ─── Layout model add-on ", "  // ─── End layout model add-on");
const helpers = slice("  // ─── Pure helpers", "  // ─── End of pure helpers");
const { autoStepNeeds, getEngineDisplayName } = new Function(
    "window", `${helpers}\nreturn { autoStepNeeds, getEngineDisplayName };`
)({});

function element() {
    return {
        hidden: false, disabled: false, textContent: "", className: "", style: {}, listeners: {},
        addEventListener(type, fn) { this.listeners[type] = fn; },
        click() { return this.listeners.click(); },
    };
}

function setup({ answers = [], confirm = true } = {}) {
    const ids = {};
    for (const id of ["layoutAddonBadge", "layoutAddonMeta", "layoutAddonReason", "layoutAddonProgress",
        "layoutAddonProgressFill", "layoutAddonProgressText", "btnLayoutInstall", "btnLayoutVerify",
        "btnLayoutRemove", "btnLayoutCancel"]) ids[id] = element();
    const log = [];
    const queue = [...answers];
    const env = {
        document: { getElementById: (id) => ids[id] || null },
        apiBase: "/InkDoc",
        ADDON_BUSY_STATES: ["downloading", "verifying", "extracting", "installing"],
        formatBytes: (b) => (b ? `${Math.round(b / 1e6)} MB` : ""),
        updateAutoSteps: () => log.push("updateAutoSteps"),
        showToast: (msg, kind) => log.push(`${kind}: ${msg}`),
        confirmDialog: async (opts) => { log.push(`confirm: ${opts.title}`); return confirm; },
        fetch: async (url) => { log.push(`GET ${url}`); return { ok: true, json: async () => queue.shift() }; },
        fetchWithToken: async (url, opts) => { log.push(`${opts.method} ${url}`); return { ok: true, json: async () => ({ valid: true }) }; },
        setInterval: () => 7,
        clearInterval: () => {},
    };
    const api = new Function("env", `
        const { document, apiBase, ADDON_BUSY_STATES, formatBytes, updateAutoSteps, showToast, confirmDialog,
                fetch, fetchWithToken, setInterval, clearInterval } = env;
        ${block}
        return { renderLayoutAddon, refreshLayoutAddon, pollLayoutAddon, status: () => layoutAddonStatus };
    `)(env);
    const shown = () => Object.entries({ install: "btnLayoutInstall", verify: "btnLayoutVerify", remove: "btnLayoutRemove", cancel: "btnLayoutCancel" })
        .filter(([, id]) => !ids[id].hidden).map(([name]) => name);
    return { api, ids, log, shown };
}

const BASE = {
    name: "layout_model", installed: false, usable: false, installable: false, provided_by_pack: false,
    license: "Apache-2.0", size_bytes: 156500000, reason: "", progress: { status: "idle" },
};

(async () => {
    {
        // The Docling engine carries the model: ready, nothing to download.
        const t = setup();
        t.api.renderLayoutAddon({ ...BASE, usable: true, provided_by_pack: true });
        assert.strictEqual(t.ids.layoutAddonBadge.textContent, "Ready");
        assert.ok(t.ids.layoutAddonBadge.className.includes("engine-badge-installed"));
        assert.strictEqual(t.ids.layoutAddonMeta.textContent, "Comes with the Docling engine · Apache-2.0");
        assert.deepStrictEqual(t.shown(), ["verify"], "files can be checked; nothing to install or remove");
        assert.strictEqual(t.ids.layoutAddonReason.hidden, true);
        assert.ok(t.log.includes("updateAutoSteps"), "the Steps switches follow");
        assert.strictEqual(t.api.status().usable, true);
    }
    {
        // Published, not in the Docling engine: a download of its size.
        const t = setup();
        t.api.renderLayoutAddon({ ...BASE, installable: true });
        assert.strictEqual(t.ids.layoutAddonBadge.textContent, "Not installed");
        assert.strictEqual(t.ids.layoutAddonMeta.textContent, "Download · 157 MB · Apache-2.0");
        assert.deepStrictEqual(t.shown(), ["install"]);
        assert.strictEqual(t.ids.btnLayoutInstall.textContent, "Install (157 MB)");
    }
    {
        const t = setup();
        t.api.renderLayoutAddon({ ...BASE, installed: true, usable: true });
        assert.strictEqual(t.ids.layoutAddonBadge.textContent, "Installed");
        assert.deepStrictEqual(t.shown(), ["verify", "remove"]);
    }
    {
        // Downloading: progress and Cancel only.
        const t = setup();
        t.api.renderLayoutAddon({ ...BASE, progress: { status: "downloading", percent: 40, bytes_downloaded: 62600000, total_bytes: 156500000 } });
        assert.strictEqual(t.ids.layoutAddonBadge.textContent, "Downloading…");
        assert.strictEqual(t.ids.layoutAddonProgress.hidden, false);
        assert.strictEqual(t.ids.layoutAddonProgressFill.style.width, "40%");
        assert.strictEqual(t.ids.layoutAddonProgressText.textContent, "40% — 63 MB of 157 MB");
        assert.deepStrictEqual(t.shown(), ["cancel"]);
    }
    {
        // Not published, no Docling engine: why not, and nothing to press.
        const t = setup();
        const reason = "This add-on has not been published yet. The Docling engine comes with it.";
        t.api.renderLayoutAddon({ ...BASE, size_bytes: 0, reason });
        assert.strictEqual(t.ids.layoutAddonBadge.textContent, "Not available");
        assert.strictEqual(t.ids.layoutAddonReason.hidden, false);
        assert.strictEqual(t.ids.layoutAddonReason.textContent, reason);
        assert.deepStrictEqual(t.shown(), []);
    }
    {
        // Install starts the download, then polls until it is done.
        const t = setup({ answers: [
            { ...BASE, progress: { status: "downloading", percent: 10 } },
            { ...BASE, installed: true, usable: true, progress: { status: "complete" } },
        ] });
        t.api.renderLayoutAddon({ ...BASE, installable: true });
        await t.ids.btnLayoutInstall.click();
        assert.ok(t.log.includes("POST /InkDoc/addons/layout_model/install"));
        await t.api.pollLayoutAddon();
        assert.ok(t.log.includes("success: Layout model installed."));
        assert.strictEqual(t.ids.layoutAddonBadge.textContent, "Installed");
    }
    {
        // Remove asks first; declined, nothing is sent.
        const t = setup({ confirm: false });
        t.api.renderLayoutAddon({ ...BASE, installed: true, usable: true });
        await t.ids.btnLayoutRemove.click();
        assert.ok(t.log.includes("confirm: Remove the layout model?"));
        assert.ok(!t.log.some((line) => line.startsWith("POST")));
    }

    // ─── "Tables on scanned pages" names the add-on it needs ───────────────────
    const step = { name: "scanned_tables", needs_all: ["glm_ocr", "layout_model"], needs_any: [] };
    assert.deepStrictEqual(autoStepNeeds(step, (e) => e === "glm_ocr"), { text: "Needs the layout model", install: "layout_model" });
    assert.deepStrictEqual(autoStepNeeds(step, () => false), { text: "Needs GLM-OCR and the layout model", install: "glm_ocr" });
    assert.strictEqual(getEngineDisplayName("layout_model"), "the layout model");
    assert.ok(source.includes('if (engine === "layout_model") return layoutAddonStatus ? Boolean(layoutAddonStatus.usable) : undefined;'));
    assert.ok(/layout_model: \["addons", "layoutAddonCard"\]/.test(source), "Install opens the card in Add-ons");

    // ─── Markup ──────────────────────────────────────────────────────────────
    const pane = html.slice(html.indexOf('data-settings-pane="addons"'), html.indexOf("</section>", html.indexOf('data-settings-pane="addons"')));
    assert.ok(pane.includes('id="layoutAddonCard"'), "the card sits in Add-ons");
    for (const id of ["btnLayoutInstall", "btnLayoutVerify", "btnLayoutRemove", "btnLayoutCancel"]) {
        assert.ok(new RegExp(`id="${id}" hidden>`).test(pane), `#${id} starts hidden`);
    }
    assert.ok(/\.addon-card \.btn\[hidden\] \{\s*display: none;/.test(css), "hidden beats .btn's display");
    assert.ok(source.includes("refreshLayoutAddon();"), "opening Settings reads its status");

    console.log("layout_addon_ui.test.js: all assertions passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
