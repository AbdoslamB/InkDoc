// Tests the preview text size (Settings > General > Display) in app/ui/app.js,
// plus the CSS, markup and Preferences grouping it relies on.
//
// A− and A+ step through seven sizes. A click applies at once; the save to
// settings.json waits until the clicking stops, and only one save is ever in
// flight. A failed save puts the saved size back. No size is shown on screen;
// screen readers are told.
//
// Follows save_folder_ui.test.js: slice the block out of the source, run it
// against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const html = fs.readFileSync("app/ui/index.html", "utf8");
const css = fs.readFileSync("app/ui/style.css", "utf8");

const start = source.indexOf("  // ─── Preview text size ");
const end = source.indexOf("  // ─── End preview text size", start);
if (start < 0 || end < 0) throw new Error("could not locate the preview text size block in app.js");
const block = source.slice(start, end);

const SIZES = [85, 90, 100, 110, 120, 135, 150];

function setup({ initial } = {}) {
    const root = { dataset: {}, writes: 0 };
    let current = initial;
    Object.defineProperty(root.dataset, "textSize", {
        get: () => current,
        set: (v) => { current = v; root.writes++; },
    });
    function button() {
        const attrs = {};
        return {
            listeners: [], setAttribute: (k, v) => { attrs[k] = v; }, getAttribute: (k) => attrs[k],
            addEventListener(type, fn) { if (type === "click") this.listeners.push(fn); },
            click() { this.listeners.forEach((fn) => fn()); },
        };
    }
    const els = { btnTextSmaller: button(), btnTextLarger: button(), textSizeStatus: { textContent: "" } };
    // Manual timers: the test moves time forward.
    let now = 0;
    let timers = [];
    const setTimeoutStub = (fn, ms) => { const t = { fn, at: now + ms }; timers.push(t); return t; };
    const clearTimeoutStub = (t) => { timers = timers.filter((x) => x !== t); };
    const advance = async (ms) => {
        now += ms;
        for (;;) {
            const due = timers.filter((t) => t.at <= now).sort((a, b) => a.at - b.at)[0];
            if (!due) break;
            timers = timers.filter((t) => t !== due);
            due.fn();
            await new Promise((r) => setImmediate(r));
        }
    };
    const posts = [];
    const pending = [];
    const toasts = [];
    const env = {
        document: { documentElement: root, getElementById: (id) => els[id] },
        apiBase: "/InkDoc",
        showToast: (msg, kind) => toasts.push({ msg, kind }),
        fetchWithToken: (url, opts) => {
            posts.push(JSON.parse(opts.body));
            return new Promise((resolve, reject) => pending.push({ resolve, reject }));
        },
        setTimeout: setTimeoutStub,
        clearTimeout: clearTimeoutStub,
    };
    const api = new Function("env", `
        const { document, apiBase, showToast, fetchWithToken, setTimeout, clearTimeout } = env;
        ${block}
        return { syncTextSize, size: () => textSize, saved: () => textSizeSaved };
    `)(env);
    const settle = () => new Promise((r) => setImmediate(r));
    const disabled = () => [els.btnTextSmaller.getAttribute("aria-disabled"), els.btnTextLarger.getAttribute("aria-disabled")];
    return { ...api, root, els, posts, pending, toasts, advance, settle, disabled, timers: () => timers };
}

(async () => {
    // The size the server wrote is kept; nothing is rewritten on load.
    {
        const t = setup({ initial: "120" });
        assert.strictEqual(t.size(), 120);
        assert.strictEqual(t.root.writes, 0);
        assert.deepStrictEqual(t.disabled(), ["false", "false"]);
    }

    // No attribute, or one that isn't a step, means 100%.
    for (const initial of [undefined, "105", "abc", ""]) {
        const t = setup({ initial });
        assert.strictEqual(t.size(), 100, String(initial));
        assert.strictEqual(t.root.dataset.textSize, "100");
    }

    // A+ walks up every step and stops at 150%; A− walks back to 85%.
    {
        const t = setup({ initial: "85" });
        assert.deepStrictEqual(t.disabled(), ["true", "false"], "A− is marked unavailable at the smallest size");
        const seen = [t.size()];
        for (let i = 0; i < 10; i++) { t.els.btnTextLarger.click(); seen.push(t.size()); }
        assert.deepStrictEqual([...new Set(seen)], SIZES);
        assert.strictEqual(t.root.dataset.textSize, "150");
        assert.deepStrictEqual(t.disabled(), ["false", "true"], "A+ is marked unavailable at the largest size");
        for (let i = 0; i < 10; i++) t.els.btnTextSmaller.click();
        assert.strictEqual(t.size(), 85);
        assert.strictEqual(t.els.textSizeStatus.textContent, "Preview text size 85%", "screen readers hear the size");
    }

    // Applied at once; saved once, 400 ms after the last click.
    {
        const t = setup();
        t.els.btnTextLarger.click();
        t.els.btnTextLarger.click();
        t.els.btnTextLarger.click();
        assert.strictEqual(t.root.dataset.textSize, "135", "each click shows at once");
        await t.advance(399);
        assert.deepStrictEqual(t.posts, [], "nothing saved while clicking");
        await t.advance(1);
        assert.deepStrictEqual(t.posts, [{ preview_text_size: 135 }], "one save for three clicks");
        t.pending[0].resolve({ ok: true });
        await t.settle();
        assert.strictEqual(t.saved(), 135);
        assert.deepStrictEqual(t.toasts, []);
    }

    // Back to the saved size before the delay: nothing to save.
    {
        const t = setup({ initial: "110" });
        t.els.btnTextLarger.click();
        t.els.btnTextSmaller.click();
        await t.advance(500);
        assert.deepStrictEqual(t.posts, []);
    }

    // A failed save (HTTP error or no connection) shows the saved size again and says so.
    for (const fail of ["http", "network"]) {
        const t = setup({ initial: "100" });
        t.els.btnTextLarger.click();
        await t.advance(400);
        if (fail === "http") t.pending[0].resolve({ ok: false });
        else t.pending[0].reject(new Error("offline"));
        await t.settle();
        assert.strictEqual(t.root.dataset.textSize, "100");
        assert.deepStrictEqual(t.toasts, [{ msg: "Couldn't save the text size.", kind: "error" }]);
    }

    // Clicks during a save: only one request at a time, and the latest size ends up saved.
    {
        const t = setup();
        t.els.btnTextLarger.click(); // 110
        await t.advance(400);
        assert.deepStrictEqual(t.posts, [{ preview_text_size: 110 }]);
        t.els.btnTextLarger.click(); // 120, while 110 is still being saved
        await t.advance(400);
        assert.strictEqual(t.posts.length, 1, "waits for the first save");
        t.pending[0].resolve({ ok: true });
        await t.settle();
        await t.advance(400);
        assert.deepStrictEqual(t.posts, [{ preview_text_size: 110 }, { preview_text_size: 120 }]);
        t.pending[1].resolve({ ok: true });
        await t.settle();
        assert.strictEqual(t.saved(), 120);
        assert.strictEqual(t.root.dataset.textSize, "120");
    }

    // A failure that the user has already moved past does not undo the newer choice.
    {
        const t = setup();
        t.els.btnTextLarger.click(); // 110
        await t.advance(400);
        t.els.btnTextLarger.click(); // 120 queued
        t.pending[0].resolve({ ok: false });
        await t.settle();
        assert.strictEqual(t.root.dataset.textSize, "120");
        assert.deepStrictEqual(t.toasts, []);
    }

    // Opening Settings re-reads the size, but never over a pending change.
    {
        const t = setup();
        t.syncTextSize(135);
        assert.strictEqual(t.root.dataset.textSize, "135");
        assert.strictEqual(t.saved(), 135);
        t.els.btnTextSmaller.click(); // 120 pending
        t.syncTextSize(135);
        assert.strictEqual(t.root.dataset.textSize, "120");
        await t.advance(400);
        t.syncTextSize(135); // save in flight
        assert.strictEqual(t.root.dataset.textSize, "120");
        t.pending[0].resolve({ ok: true });
        await t.settle();
        t.syncTextSize("bogus");
        assert.strictEqual(t.size(), 100, "an unknown value from the server means 100%");
    }

    // Wiring: Settings re-reads the size (guarded, so the add-on test's slice still runs).
    {
        const loadStart = source.indexOf("  async function loadSettings() {");
        const loadEnd = source.indexOf("renderSaveFolder(s.save_folder);", loadStart);
        assert.ok(source.slice(loadStart, loadEnd).includes("if (s.preview_text_size !== undefined) syncTextSize(s.preview_text_size);"));
    }

    // Markup: A− and A+ only (no size shown), labelled; a live sample drawn with
    // the preview's own class; a status line for screen readers.
    {
        const display = html.slice(html.indexOf('class="settings-section settings-display"'), html.indexOf('data-settings-pane="engines"'));
        const smaller = display.match(/<button[^>]*id="btnTextSmaller"[^>]*>([\s\S]*?)<\/button>/);
        const larger = display.match(/<button[^>]*id="btnTextLarger"[^>]*>([\s\S]*?)<\/button>/);
        assert.ok(smaller && smaller[0].includes('aria-label="Smaller text"') && smaller[1].includes("A−"));
        assert.ok(larger && larger[0].includes('aria-label="Larger text"') && larger[1].includes("A+"));
        assert.ok(!/\d+\s*%/.test(display.replace(/<!--[\s\S]*?-->/g, "")), "no percentage is shown");
        assert.ok(/class="markdown-rendered text-size-sample" aria-hidden="true"/.test(display));
        assert.ok(/id="textSizeStatus" role="status" aria-live="polite"/.test(display));
    }

    // Preferences: three groups (saving, Docling fallback, PDF check), each toggle
    // in its own, no inline spacing left over.
    {
        const prefs = html.slice(html.indexOf(">Preferences<"), html.indexOf('class="settings-section settings-display"'));
        const groups = prefs.split('<div class="settings-group">').slice(1);
        assert.strictEqual(groups.length, 3);
        assert.ok(groups[0].includes('id="autoSaveToggle"') && groups[0].includes('id="btnChangeSaveFolder"'));
        assert.ok(groups[1].includes('id="doclingFallbackToggle"'));
        assert.ok(groups[2].includes('id="qualityCheckToggle"'));
        assert.ok(!/toggle-switch" style="margin-top/.test(prefs));
        assert.ok(/\.settings-group \+ \.settings-group \{[^}]*border-top: 1px solid var\(--rule\);/.test(css), "a line only between groups");
    }

    // CSS: one rule per step, and every preview font size scales from its former value.
    {
        assert.ok(/:root \{[^}]*--preview-scale: 1;/.test(css));
        for (const step of SIZES.filter((s) => s !== 100)) {
            const re = new RegExp(`html\\[data-text-size="${step}"\\] \\{ --preview-scale: ${step / 100}; \\}`);
            assert.ok(re.test(css), `rule for ${step}%`);
        }
        const expected = [
            [".markdown-rendered {", 14], [".markdown-rendered h1 {", 24], [".markdown-rendered h2 {", 20],
            [".markdown-rendered h3 {", 16], [".markdown-rendered code {", 12], [".markdown-rendered pre code {", 13],
            [".markdown-rendered table {", 13], [".raw-markdown-editor {", 13],
        ];
        for (const [selector, px] of expected) {
            const at = css.indexOf("\n" + selector);
            const rule = css.slice(at, css.indexOf("}", at));
            assert.ok(rule.includes(`font-size: calc(${px}px * var(--preview-scale));`), `${selector} scales from ${px}px`);
            assert.ok(!/transition/.test(rule), `${selector} must not animate its size`);
        }
        // Nothing else in the preview still uses a fixed font size.
        const fixed = [...css.matchAll(/\n(\.markdown-rendered[^{]*)\{([^}]*)\}/g)]
            .filter((m) => !m[1].includes("text-size-sample"))
            .filter((m) => /font-size:\s*\d+px/.test(m[2]));
        assert.deepStrictEqual(fixed.map((m) => m[1].trim()), []);
    }

    console.log("text size UI tests passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
