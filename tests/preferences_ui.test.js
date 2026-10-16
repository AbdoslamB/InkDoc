// Tests the saved preferences in app/ui/app.js (theme, engine choice, hidden
// engines, auto-save, Auto's tip): read from the page at startup, saved to
// settings.json in one grouped request after a short pause, one save in flight
// at a time, and moved once out of the browser storage InkDoc 1.2 used.
//
// Follows save_folder_ui.test.js: slice the blocks out of the source, run them
// against stubs (fake timers, a fake fetch), no npm dependency.
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");

function slice(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    if (start < 0 || end < 0) throw new Error(`could not locate ${startMarker} in app.js`);
    return source.slice(start, end);
}

const helpers = slice("  // ─── Pure helpers", "  // ─── End of pure helpers");
const block = slice("  // ─── Preferences ", "  // ─── End preferences");

function setup({ serverPrefs = {}, theme = "dark", hidden = "", storage = {}, noPrefs = false } = {}) {
    // Fake timers: run only when the test advances the clock.
    let now = 0;
    let nextId = 1;
    const timers = new Map();
    const setTimeout = (fn, ms) => { const id = nextId++; timers.set(id, { fn, at: now + ms }); return id; };
    const clearTimeout = (id) => { timers.delete(id); };
    function advance(ms) {
        now += ms;
        for (const [id, t] of [...timers].sort((a, b) => a[1].at - b[1].at)) {
            if (t.at <= now && timers.has(id)) { timers.delete(id); t.fn(); }
        }
    }

    // A fetch whose answers the test controls.
    const requests = [];
    function fetchWithToken(url, options) {
        let respond;
        const promise = new Promise((resolve, reject) => { respond = { resolve, reject }; });
        requests.push({ url, options, body: JSON.parse(options.body), respond });
        return promise;
    }
    const ok = () => ({ ok: true, json: async () => ({}) });
    const fail = (status, detail) => ({ ok: false, status, json: async () => (detail ? { detail } : {}) });

    const toasts = [];
    const store = { ...storage };
    const window = {
        __INKDOC_PREFS__: noPrefs ? undefined : {
            preferred_engine: "auto", auto_save: false, auto_hint_hidden: false, legacy_prefs_imported: false,
            ...serverPrefs,
        },
        localStorage: {
            getItem: (k) => (k in store ? store[k] : null),
            setItem: (k, v) => { store[k] = String(v); },
            removeItem: (k) => { delete store[k]; },
        },
    };
    const document = {
        documentElement: {
            getAttribute: (n) => (n === "data-theme" ? theme : null),
            dataset: { hiddenEngines: hidden },
        },
    };

    const api = new Function("env", `
        const { window, document, fetchWithToken, showToast, setTimeout, clearTimeout } = env;
        const apiBase = "/InkDoc";
        ${helpers}
        ${block}
        return { prefs, prefsFromServer, savePrefs, sendPrefs, flushPrefs, settlePrefs, prefsBusy, importLegacyPrefs };
    `)({
        window, document, fetchWithToken, setTimeout, clearTimeout,
        showToast: (message, type) => toasts.push({ message, type }),
    });
    return { ...api, requests, toasts, store, advance, timers, ok, fail };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
    // ── Startup: what the server put on the page, no request ──
    {
        const t = setup({
            serverPrefs: { preferred_engine: "markit", auto_save: true, auto_hint_hidden: true, legacy_prefs_imported: true },
            theme: "light",
            hidden: "docling glm_ocr",
        });
        assert.ok(t.prefsFromServer);
        assert.deepStrictEqual(t.prefs, {
            theme: "light", preferred_engine: "markit", hidden_engines: ["docling", "glm_ocr"],
            auto_save: true, auto_hint_hidden: true, legacy_prefs_imported: true,
        });
        assert.strictEqual(t.requests.length, 0, "nothing is fetched at startup");
    }
    {
        const t = setup({ noPrefs: true });
        assert.ok(!t.prefsFromServer, "a page without the session script asks the server instead");
        assert.strictEqual(t.prefs.preferred_engine, "auto");
        assert.strictEqual(t.importLegacyPrefs(), null, "and never imports (it has no token either)");
    }

    // ── Several changes within the pause: one request with all of them ──
    {
        const t = setup();
        t.savePrefs({ theme: "light" });
        t.advance(100);
        t.savePrefs({ preferred_engine: "docling" });
        t.advance(100);
        t.savePrefs({ theme: "dark" });
        assert.strictEqual(t.requests.length, 0, "waits for the clicking to stop");
        assert.ok(t.prefsBusy());
        t.advance(250);
        assert.strictEqual(t.requests.length, 1);
        assert.strictEqual(t.requests[0].url, "/InkDoc/settings");
        assert.deepStrictEqual(t.requests[0].body, { theme: "dark", preferred_engine: "docling" }, "the newest value wins");
        assert.strictEqual(t.prefs.theme, "dark", "applied at once");
        t.requests[0].respond.resolve(t.ok());
        await settle();
        assert.ok(!t.prefsBusy());
    }

    // ── One save in flight: a change made meanwhile goes after it ──
    {
        const t = setup();
        t.savePrefs({ theme: "light" });
        t.advance(250);
        t.savePrefs({ auto_save: true });
        t.advance(250);
        assert.strictEqual(t.requests.length, 1, "still waiting for the first");
        t.requests[0].respond.resolve(t.ok());
        await settle();
        t.advance(250);
        assert.strictEqual(t.requests.length, 2);
        assert.deepStrictEqual(t.requests[1].body, { auto_save: true });
        t.requests[1].respond.resolve(t.ok());
        await settle();
        assert.strictEqual(t.toasts.length, 0);
    }

    // ── A failed save: reported once, never undone, retried with the next change ──
    {
        const t = setup();
        t.savePrefs({ theme: "light" });
        t.advance(250);
        t.requests[0].respond.resolve(t.fail(503, "InkDoc couldn't write its settings file, so this change will be lost when InkDoc restarts."));
        await settle();
        assert.strictEqual(t.prefs.theme, "light", "not undone");
        assert.ok(t.prefsBusy(), "a failed change is not overwritten by a read from the server");
        assert.deepStrictEqual(t.toasts, [{ message: "InkDoc couldn't write its settings file, so this change will be lost when InkDoc restarts.", type: "error" }]);

        t.savePrefs({ preferred_engine: "markit" });
        t.advance(250);
        assert.deepStrictEqual(t.requests[1].body, { theme: "light", preferred_engine: "markit" }, "the failed change goes again");
        t.requests[1].respond.reject(new TypeError("Failed to fetch"));
        await settle();
        assert.strictEqual(t.toasts.length, 1, "one toast per run of failures");

        t.savePrefs({ preferred_engine: "auto" });
        t.advance(250);
        assert.deepStrictEqual(t.requests[2].body, { theme: "light", preferred_engine: "auto" }, "a newer value replaces the failed one");
        t.requests[2].respond.resolve(t.ok());
        await settle();
        t.savePrefs({ theme: "dark" });
        t.advance(250);
        t.requests[3].respond.resolve(t.fail(403));
        await settle();
        assert.strictEqual(t.toasts.length, 2, "a new failure after a success is reported again");
        assert.match(t.toasts[1].message, /back to how it was when InkDoc restarts/);
    }

    // ── Settings closing / the page going away ──
    {
        const t = setup();
        t.flushPrefs();
        assert.strictEqual(t.requests.length, 0, "nothing waiting, nothing sent");
        t.savePrefs({ hidden_engines: ["markit"] });
        t.flushPrefs();
        assert.strictEqual(t.requests.length, 1, "sent at once");
        assert.strictEqual(t.requests[0].options.keepalive, false);
        assert.strictEqual(t.timers.size, 0);

        t.savePrefs({ theme: "light" });
        t.flushPrefs(true);
        assert.strictEqual(t.requests.length, 2, "the page is going: sent even with one in flight");
        assert.strictEqual(t.requests[1].options.keepalive, true, "outlives the page");
        t.requests[1].respond.reject(new TypeError("aborted"));
        await settle();
        assert.strictEqual(t.toasts.length, 0, "no toast for a page that is closing");
    }

    // ── Restore defaults: unsaved changes dropped, a running save waited for ──
    {
        const t = setup();
        t.savePrefs({ theme: "light" });
        t.advance(250);
        t.savePrefs({ preferred_engine: "docling" });
        let settled = false;
        const done = t.settlePrefs().then(() => { settled = true; });
        await settle();
        assert.ok(!settled, "waits for the save on its way");
        t.requests[0].respond.resolve(t.ok());
        await done;
        assert.ok(!t.prefsBusy());
        t.advance(1000);
        assert.strictEqual(t.requests.length, 1, "the dropped change is never sent");
    }

    // ── Moving InkDoc 1.2's browser-storage preferences, once ──
    {
        const t = setup({ storage: { "inkdoc-theme": "light", "inkdoc-engine": "markit", "inkdoc-hide-auto-hint": "1", unrelated: "x" } });
        const running = t.importLegacyPrefs();
        assert.strictEqual(t.requests.length, 1, "sent at once, not after the pause");
        assert.deepStrictEqual(t.requests[0].body, {
            theme: "light", preferred_engine: "markit", auto_hint_hidden: true, legacy_prefs_imported: true,
        });
        assert.strictEqual(t.prefs.preferred_engine, "markit");
        assert.ok("inkdoc-theme" in t.store, "kept until the save succeeds");
        t.requests[0].respond.resolve(t.ok());
        const result = await running;
        assert.ok(result.ok);
        assert.deepStrictEqual(t.store, { unrelated: "x" }, "only InkDoc's old keys are removed");
        assert.strictEqual(t.importLegacyPrefs(), null, "once");
    }
    {
        // The desktop window had nothing kept: only the flag is saved.
        const t = setup();
        t.importLegacyPrefs();
        assert.deepStrictEqual(t.requests[0].body, { legacy_prefs_imported: true });
    }
    {
        // Failed: the keys stay, so the next load tries again.
        const t = setup({ storage: { "markitdown-theme": "light" } });
        const running = t.importLegacyPrefs();
        t.requests[0].respond.resolve(t.fail(503));
        assert.ok(!(await running).ok);
        assert.strictEqual(t.store["markitdown-theme"], "light");
    }
    {
        // Already done for this settings.json (another window, an earlier launch).
        const t = setup({ serverPrefs: { legacy_prefs_imported: true }, storage: { "inkdoc-theme": "light" } });
        assert.strictEqual(t.importLegacyPrefs(), null);
        assert.strictEqual(t.requests.length, 0);
    }
    {
        // An old engine choice that is now hidden is not brought back.
        const t = setup({ hidden: "docling", storage: { "inkdoc-engine": "docling" } });
        t.importLegacyPrefs();
        assert.deepStrictEqual(t.requests[0].body, { legacy_prefs_imported: true });
    }

    // The desktop close button (Windows title bar) sends waiting changes before
    // asking Python to close the window: a closing web view drops them, and
    // pagehide can't get a request out of it. Bounded, so closing never hangs.
    {
        const at = source.indexOf('closeBtn.addEventListener("click", async () => {');
        assert.ok(at > 0, "the close button waits for something first");
        const handler = source.slice(at, source.indexOf("});", source.indexOf("close_window()", at)));
        const flush = handler.indexOf("Promise.all([flushPrefs(true), flushTextSize(true)])");
        const close = handler.indexOf("api.close_window()");
        assert.ok(flush > 0 && close > flush, "saves are sent before the window closes");
        assert.ok(/Promise\.race\(\[[\s\S]*setTimeout\(resolve, 600\)/.test(handler), "and waited for at most 600 ms");
    }
    {
        // Nothing waiting: flushPrefs resolves at once, so the window closes at once.
        const t = setup();
        assert.strictEqual(await t.flushPrefs(true), true);
        assert.strictEqual(t.requests.length, 0);
    }

    console.log("[OK] preferences_ui.test.js passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
