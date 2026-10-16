// Tests Restore defaults (Settings → General, last) in app/ui/app.js.
//
// It asks first, drops changes not yet saved and waits for a save already on
// its way (so neither lands after the reset), sends one POST /settings/reset
// (the server holds the list and keeps engines, add-ons, their setup and the
// save folder), and applies the answer to every control.
//
// Follows save_folder_ui.test.js: slice the block out of the source, run it
// against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const html = fs.readFileSync("app/ui/index.html", "utf8");
const css = fs.readFileSync("app/ui/style.css", "utf8");
const start = source.indexOf("  // ─── Restore defaults ");
const end = source.indexOf("  // ─── End restore defaults", start);
if (start < 0 || end < 0) throw new Error("could not locate the Restore defaults block in app.js");
const block = source.slice(start, end);

function setup({ confirm = true, response = null, reject = false, applyThrows = false } = {}) {
    const log = [];
    const attrs = {};
    let onClick = null;
    const button = {
        setAttribute: (k, v) => { attrs[k] = v; },
        removeAttribute: (k) => { delete attrs[k]; },
        addEventListener: (type, fn) => { if (type === "click") onClick = fn; },
    };
    const dialogs = [];
    const requests = [];
    let answer;
    const env = {
        document: { getElementById: (id) => (id === "btnResetDefaults" ? button : null) },
        apiBase: "/InkDoc",
        LEGACY_PREF_KEYS: ["inkdoc-theme", "inkdoc-engine"],
        removeStorage: (k) => log.push(`remove ${k}`),
        confirmDialog: async (opts) => { dialogs.push(opts); log.push("confirm"); return confirm; },
        settlePrefs: async () => { log.push("settlePrefs"); },
        settleTextSize: async () => { log.push("settleTextSize"); },
        fetchWithToken: (url, options) => {
            requests.push({ url, options });
            log.push(`POST ${url}`);
            return new Promise((resolve, rejectFn) => { answer = { resolve, reject: rejectFn }; });
        },
        applySettings: (s) => { log.push("applySettings"); if (applyThrows) throw new Error("boom"); log.push(s); },
        showToast: (message, type) => log.push(`${type}: ${message}`),
    };
    const api = new Function("env", `
        const { document, apiBase, LEGACY_PREF_KEYS, removeStorage, confirmDialog, settlePrefs, settleTextSize,
                fetchWithToken, applySettings, showToast } = env;
        ${block}
        return { restoreDefaults };
    `)(env);
    const settle = () => new Promise((r) => setImmediate(r));
    async function click() {
        const run = onClick();
        await settle();
        if (answer) {
            if (reject) answer.reject(new TypeError("Failed to fetch"));
            else answer.resolve(response || { ok: true, json: async () => ({ theme: "dark" }) });
        }
        await run;
    }
    return { ...api, log, attrs, dialogs, requests, click, clickNoWait: () => onClick(), answer: () => answer, settle };
}

(async () => {
    // Cancelled: nothing is dropped, waited for or sent.
    {
        const t = setup({ confirm: false });
        await t.click();
        assert.deepStrictEqual(t.log, ["confirm"]);
    }

    // The question says what changes and what is kept; not styled as dangerous.
    {
        const t = setup({ confirm: false });
        await t.click();
        const d = t.dialogs[0];
        assert.strictEqual(d.title, "Restore default settings?");
        assert.strictEqual(d.confirmText, "Restore defaults");
        assert.strictEqual(d.danger, false, "nothing is deleted");
        assert.ok(d.details.some((line) => line.startsWith("Reset:") && line.includes("theme")));
        assert.ok(d.details.some((line) => line.startsWith("Kept:") && line.includes("save folder") && line.includes("engines")));
    }

    // Confirmed: in order, one request, then every control from the answer.
    {
        const t = setup();
        await t.click();
        assert.deepStrictEqual(t.log, [
            "confirm", "settlePrefs", "settleTextSize", "POST /InkDoc/settings/reset",
            "remove inkdoc-theme", "remove inkdoc-engine", "applySettings", { theme: "dark" },
            "success: Default settings restored.",
        ]);
        assert.strictEqual(t.requests[0].options.method, "POST");
        assert.ok(!("aria-disabled" in t.attrs), "usable again");
    }

    // While it runs: marked busy (focus stays), and a second click does nothing.
    {
        const t = setup();
        const first = t.clickNoWait();
        await t.settle();
        assert.strictEqual(t.attrs["aria-disabled"], "true");
        await t.clickNoWait();
        assert.strictEqual(t.requests.length, 1);
        assert.strictEqual(t.dialogs.length, 1, "not even asked again");
        t.answer().resolve({ ok: true, json: async () => ({}) });
        await first;
        assert.ok(!("aria-disabled" in t.attrs));
    }

    // Refused (e.g. settings.json can't be written): the server's words, nothing changed.
    {
        const detail = "InkDoc couldn't write its settings file, so this change will be lost when InkDoc restarts.";
        const t = setup({ response: { ok: false, status: 503, json: async () => ({ detail }) } });
        await t.click();
        assert.ok(t.log.includes(`error: ${detail}`));
        assert.ok(!t.log.includes("applySettings"));
        assert.ok(!t.log.some((l) => String(l).startsWith("remove")), "old keys kept: nothing was reset");
        assert.ok(!("aria-disabled" in t.attrs));
    }
    {
        const t = setup({ response: { ok: false, status: 403, json: async () => { throw new Error("not json"); } } });
        await t.click();
        assert.ok(t.log.includes("error: Couldn't restore the default settings."));
    }
    {
        const t = setup({ reject: true });
        await t.click();
        assert.ok(t.log.includes("error: Couldn't restore the default settings."));
        assert.ok(!("aria-disabled" in t.attrs));
    }

    // A control that can't be updated doesn't hide that the reset was saved.
    {
        const t = setup({ applyThrows: true });
        await t.click();
        assert.ok(t.log.includes("success: Default settings restored."));
    }

    // Markup: the last section of General, after Display, with its caption.
    {
        const general = html.slice(html.indexOf('data-settings-pane="general"'), html.indexOf('data-settings-pane="engines"'));
        const display = general.indexOf('class="settings-section settings-display"');
        const reset = general.indexOf('class="settings-section settings-reset"');
        assert.ok(display > 0 && reset > display, "after Display");
        assert.strictEqual(general.slice(reset).split('class="settings-section').length, 2, "nothing after it");
        assert.ok(/<button type="button" class="btn btn-sm btn-secondary" id="btnResetDefaults">[\s\S]*?Restore defaults\s*<\/button>/.test(general));
        assert.ok(general.includes("Engines, add-ons, your save folder and converted files are kept."));
        assert.ok(/\.settings-reset \{\s*border-top: 1px solid var\(--divider\);/.test(css));
        assert.ok(/\.btn\[aria-disabled="true"\] \{/.test(css));
    }

    // Dividers: the token exists in both themes and the groups use it.
    {
        assert.ok(/\[data-theme="dark"\] \{[^}]*--divider: #343d38;/.test(css));
        assert.ok(/\[data-theme="light"\] \{[^}]*--divider: #c5cec6;/.test(css));
        const display = html.slice(html.indexOf('class="settings-section settings-display"'), html.indexOf('class="settings-section settings-reset"'));
        assert.strictEqual(display.split('class="settings-group').length - 1, 2, "Display: width | text size");
        const updates = html.slice(html.indexOf('id="settingsUpdatesSection"'), html.indexOf('data-settings-pane="about"'));
        assert.strictEqual(updates.split('<div class="settings-group">').length - 1, 2, "Updates: status | daily check");
        assert.ok(!/style="margin-top/.test(updates), "no inline spacing left");
        assert.ok(!/toggle-switch" style="margin-top/.test(html), "no switch with inline spacing anywhere");
        assert.ok(/\.glm-advanced \{[^}]*border-top: 1px solid var\(--divider\);/.test(css));
        assert.ok(/\.engine-enrichment \{[^}]*border-top: 1px solid var\(--divider\);/.test(css));
    }

    console.log("[OK] reset_defaults_ui.test.js passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
