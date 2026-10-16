// Tests the Auto engine and missing-text check helpers in app/ui/app.js.
//
// Follows preview_word_count.test.js: slice the blocks out of the source, run
// them against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");

function slice(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    if (start < 0 || end < 0) throw new Error(`could not locate ${startMarker} in app.js`);
    return source.slice(start, end);
}

const helperBlock = slice("  // ─── Pure helpers", "  // ─── End of pure helpers");
const EXPORTS = [
    "readStorage", "removeStorage", "normalizePrefs", "legacyPrefsFrom", "effectiveEngine",
    "getEngineDisplayName", "formatQualityWarning", "qualityAction", "visibleHint", "describeQueueEngine",
    "describePreviewEngine", "shouldShowHintToast", "getNextEngine", "newJobId",
    "HIDE_AUTO_HINT_KEY", "LEGACY_PREF_KEYS",
];

function load(windowStub) {
    return new Function("window", `${helperBlock}\nreturn { ${EXPORTS.join(", ")} };`)(windowStub);
}

function storage(initial = {}) {
    const data = { ...initial };
    return {
        data,
        getItem: (k) => (k in data ? data[k] : null),
        setItem: (k, v) => { data[k] = String(v); },
        removeItem: (k) => { delete data[k]; },
    };
}

// ─── Default engine (settings.json via the page) ─────────────────────────────
{
    const h = load({ localStorage: storage() });
    assert.strictEqual(h.normalizePrefs({}).preferred_engine, "auto", "new users start on Auto");
    assert.strictEqual(h.normalizePrefs(undefined).preferred_engine, "auto");
    assert.strictEqual(h.normalizePrefs({ preferred_engine: "docling" }).preferred_engine, "docling", "a saved choice is kept");
    assert.strictEqual(h.normalizePrefs({ preferred_engine: "bogus" }).preferred_engine, "auto", "an unknown value falls back to Auto");
    assert.strictEqual(h.normalizePrefs({ preferred_engine: "markit", hidden_engines: ["markit"] }).preferred_engine, "auto",
        "a hidden engine is never the choice");
}

// ─── Preferences left in browser storage by InkDoc 1.2 ───────────────────────
{
    const saved = load({ localStorage: storage({ "inkdoc-engine": "docling", "inkdoc-theme": "light", "inkdoc-hide-auto-hint": "1" }) });
    assert.deepStrictEqual(saved.legacyPrefsFrom(saved.readStorage), { theme: "light", preferred_engine: "docling", auto_hint_hidden: true });

    const legacy = load({ localStorage: storage({ "markitdown-engine": "markit", "markitdown-theme": "dark" }) });
    assert.deepStrictEqual(legacy.legacyPrefsFrom(legacy.readStorage), { theme: "dark", preferred_engine: "markit" }, "the old key names are honoured");

    const junk = load({ localStorage: storage({ "inkdoc-engine": "bogus", "inkdoc-theme": "blue", "inkdoc-hide-auto-hint": "yes" }) });
    assert.deepStrictEqual(junk.legacyPrefsFrom(junk.readStorage), {}, "unknown values are dropped");

    const throwing = {
        get localStorage() { throw new Error("SecurityError: storage is disabled"); },
    };
    const locked = load(throwing);
    assert.deepStrictEqual(locked.legacyPrefsFrom(locked.readStorage), {}, "storage that throws gives nothing");
    locked.removeStorage("inkdoc-engine"); // must not throw

    const store = storage({ "inkdoc-theme": "light" });
    const s = load({ localStorage: store });
    s.LEGACY_PREF_KEYS.forEach(s.removeStorage);
    assert.deepStrictEqual(store.data, {});
    assert.ok(s.LEGACY_PREF_KEYS.includes(s.HIDE_AUTO_HINT_KEY));
}

// ─── The engine used now ──────────────────────────────────────────────────────
{
    const h = load({ localStorage: storage() });
    const yes = () => true;
    const no = () => false;
    const unknown = () => undefined;
    assert.strictEqual(h.effectiveEngine("docling", yes, []), "docling");
    assert.strictEqual(h.effectiveEngine("docling", no, []), "auto", "not usable: Auto, not MarkItDown");
    assert.strictEqual(h.effectiveEngine("docling", unknown, []), "docling", "kept while its status is unknown");
    assert.strictEqual(h.effectiveEngine("docling", yes, ["docling"]), "auto", "hidden: Auto");
    assert.strictEqual(h.effectiveEngine("auto", no, []), "auto");
    assert.strictEqual(h.effectiveEngine("bogus", yes, []), "auto");
}

// ─── Normalised preferences ───────────────────────────────────────────────────
{
    const h = load({ localStorage: storage() });
    assert.deepStrictEqual(h.normalizePrefs({}), {
        theme: "dark", preferred_engine: "auto", hidden_engines: [],
        auto_save: false, auto_hint_hidden: false, legacy_prefs_imported: false,
    });
    assert.deepStrictEqual(h.normalizePrefs({
        theme: "light", hidden_engines: ["markit", "auto", "x", "docling", "markit"], auto_save: "true", auto_hint_hidden: true,
    }), {
        theme: "light", preferred_engine: "auto", hidden_engines: ["docling", "markit"],
        auto_save: false, auto_hint_hidden: true, legacy_prefs_imported: false,
    }, "Auto and unknown names are never hidden; only true is true");
    assert.deepStrictEqual(h.normalizePrefs({ hidden_engines: "docling" }).hidden_engines, []);
}

// ─── Labels ──────────────────────────────────────────────────────────────────
const h = load({ localStorage: storage() });
assert.strictEqual(h.getEngineDisplayName("auto"), "Auto");
assert.strictEqual(h.getEngineDisplayName("docling"), "Docling");
assert.strictEqual(h.getEngineDisplayName("markitdown"), "MarkItDown");
for (const engine of ["auto", "markitdown", "docling", "markit"]) {
    assert.notStrictEqual(h.getNextEngine(engine), "auto", "Switch Engine never cycles to Auto");
}
// Hidden engines are skipped; with none left there is nothing to switch to.
assert.strictEqual(h.getNextEngine("markitdown", false, ["docling"]), "markit");
assert.strictEqual(h.getNextEngine("markit", true, ["glm_ocr"]), "markitdown");
assert.strictEqual(h.getNextEngine("docling", false, ["markit", "markitdown"]), null);
assert.strictEqual(h.getNextEngine("auto", false, ["markitdown"]), "docling");
assert.strictEqual(h.getNextEngine("markitdown", true, ["markitdown"]), "docling", "an item's own engine may be hidden");
assert.ok(h.newJobId().startsWith("ui-") && h.newJobId() !== h.newJobId());

// ─── formatQualityWarning ────────────────────────────────────────────────────
assert.strictEqual(h.formatQualityWarning(null), null);
assert.strictEqual(h.formatQualityWarning({ warning: false, missing_pct: 3 }), null);
{
    const w = h.formatQualityWarning({
        warning: true, missing_pct: 18, scan_pages: [], page_count: 12,
        message: "~18% of the source text may be missing (pages 4, 7). Try Docling.",
    });
    assert.strictEqual(w.short, "~18% text missing");
    assert.strictEqual(w.long, "~18% of the source text may be missing (pages 4, 7). Try Docling.");
    assert.strictEqual(w.title, "Some source text may be missing");
    assert.strictEqual(w.toast, "but ~18% of the source text may be missing");
}
{
    const partial = h.formatQualityWarning({ warning: true, scan_pages: [2, 3], page_count: 8, message: "m" });
    assert.strictEqual(partial.short, "Scanned pages unread");
    assert.strictEqual(partial.toast, "but 2 scanned pages weren't read");
    const whole = h.formatQualityWarning({ warning: true, scan_pages: [1, 2], page_count: 2, message: "m" });
    assert.strictEqual(whole.short, "Scanned PDF");
    assert.strictEqual(whole.title, "No text layer to read");
    const one = h.formatQualityWarning({ warning: true, scan_pages: [5], page_count: 9, message: "m" });
    assert.strictEqual(one.toast, "but 1 scanned page wasn't read");
    const garbled = h.formatQualityWarning({ warning: true, garbled: true, scan_pages: [], message: "m" });
    assert.strictEqual(garbled.short, "Unreadable text");
}

// ─── qualityAction ───────────────────────────────────────────────────────────
assert.deepStrictEqual(h.qualityAction({ suggestion: "docling" }), { label: "Re-convert with Docling", engine: "docling" });
assert.deepStrictEqual(h.qualityAction({ suggestion: "markitdown" }), { label: "Re-convert with MarkItDown", engine: "markitdown" });
assert.deepStrictEqual(h.qualityAction({ suggestion: "install_docling" }), { label: "Install Docling", install: true });
assert.strictEqual(h.qualityAction({ suggestion: "install_docling" }, ["docling"]), null, "never suggest installing a hidden engine");
assert.strictEqual(h.qualityAction({ suggestion: "install_glm_ocr" }, ["glm_ocr"]), null);
assert.deepStrictEqual(h.qualityAction({ suggestion: "docling" }, ["docling"]), { label: "Re-convert with Docling", engine: "docling" },
    "re-converting with an installed hidden engine is still offered");
assert.strictEqual(h.qualityAction({ suggestion: null }), null);
assert.strictEqual(h.qualityAction(null), null);

// ─── Engine tags ─────────────────────────────────────────────────────────────
{
    const autoItem = {
        engine: "docling", engineRequested: "auto", engineUsed: "docling",
        auto: { reason: "5 of 8 pages are scanned images", escalated: false },
    };
    const tag = h.describeQueueEngine(autoItem, "auto");
    assert.deepStrictEqual(tag, { cls: "auto", text: "Auto · Docling", title: "5 of 8 pages are scanned images" });
    assert.strictEqual(h.describeQueueEngine({ engine: "markitdown", engineRequested: "markitdown" }, "markitdown"), null);
    assert.strictEqual(h.describeQueueEngine({ engine: "docling", engineRequested: "docling" }, "auto").text, "Docling");

    assert.strictEqual(h.describePreviewEngine(autoItem).text, "Engine: Auto → Docling");
    assert.strictEqual(h.describePreviewEngine(autoItem).title, "5 of 8 pages are scanned images");
    const escalated = {
        engineRequested: "auto", engineUsed: "docling",
        auto: { reason: "Digital PDF", escalated: true,
                escalation: { from: "markitdown", from_missing_pct: 18, from_scan_pages: 0, to: "docling", kept: "docling" } },
    };
    assert.strictEqual(h.describePreviewEngine(escalated).text, "Engine: Auto → Docling (MarkItDown missed ~18%)");
    const scans = JSON.parse(JSON.stringify(escalated));
    scans.auto.escalation.from_scan_pages = 3;
    assert.strictEqual(h.describePreviewEngine(scans).text, "Engine: Auto → Docling (MarkItDown couldn't read 3 scanned pages)");
    const keptFirst = JSON.parse(JSON.stringify(escalated));
    keptFirst.engineUsed = "markitdown";
    keptFirst.auto.escalation.kept = "markitdown";
    assert.strictEqual(h.describePreviewEngine(keptFirst).text, "Engine: Auto → MarkItDown");
    assert.strictEqual(h.describePreviewEngine({ engineRequested: "markit", engineUsed: "markitdown" }).text, "Engine: MarkItDown");
}

// ─── Install hint: once per session, never after "Don't show again" ──────────
{
    const hh = load({ localStorage: storage() });
    const session = { hintShown: false };
    const hint = "Docling would read the 5 scanned pages. Install it in Settings.";
    assert.strictEqual(hh.shouldShowHintToast(hint, session, false), true);
    session.hintShown = true; // what maybeShowHintToast records
    assert.strictEqual(hh.shouldShowHintToast(hint, session, false), false, "a batch of 40 scans gives one tip");
    assert.strictEqual(hh.shouldShowHintToast("", { hintShown: false }, false), false);
    // "Don't show again" is saved as auto_hint_hidden in settings.json.
    assert.strictEqual(hh.shouldShowHintToast(hint, { hintShown: false }, true), false, "Don't show again sticks");

    // A tip to install an engine the user hid is not shown; other tips are.
    assert.strictEqual(hh.visibleHint(hint, ["docling"]), "");
    assert.strictEqual(hh.visibleHint(hint, ["glm_ocr"]), hint);
    const glmHint = "GLM-OCR would read the text in this image. Install it in Settings.";
    assert.strictEqual(hh.visibleHint(glmHint, ["glm_ocr"]), "");
    const gate = "GLM-OCR would take about 9 min here, over the 5 min limit.";
    assert.strictEqual(hh.visibleHint(gate, ["glm_ocr"]), gate, "only install tips are dropped");
    assert.strictEqual(hh.visibleHint("", []), "");
    assert.strictEqual(hh.visibleHint(undefined, []), "");
}

// ─── updateEngineUI keeps exactly one pill checked ───────────────────────────
{
    const updateSrc = slice("  function updateEngineUI() {", "  // The user picked an engine");
    function pill(engine) {
        const classes = new Set();
        return {
            dataset: { engine }, attrs: {}, tabIndex: 0,
            classList: { toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), contains: (c) => classes.has(c) },
            setAttribute(name, value) { this.attrs[name] = value; },
        };
    }
    const pills = ["auto", "markitdown", "docling", "markit", "glm_ocr"].map(pill);
    for (const selected of ["auto", "docling"]) {
        new Function("allEnginePills", "selectedEngine", "updateCompactBarEngine", "updateSpeedHint",
            `${updateSrc}\nupdateEngineUI();`)(pills, selected, () => { }, () => { });
        const checked = pills.filter((p) => p.attrs["aria-checked"] === "true");
        assert.strictEqual(checked.length, 1, "exactly one pill is aria-checked");
        assert.strictEqual(checked[0].dataset.engine, selected);
        assert.deepStrictEqual(pills.map((p) => p.tabIndex).filter((t) => t === 0).length, 1, "one tab stop");
    }
}

// ─── Markup: Auto is the first pill, and every pill is a radio ───────────────
{
    const markup = fs.readFileSync("app/ui/index.html", "utf8");
    const group = /<div class="engine-pill-group"[\s\S]*?<\/div>/.exec(markup)[0];
    const pills = [...group.matchAll(/<button[^>]*data-engine="([a-z_]+)"[^>]*>/g)];
    assert.deepStrictEqual(pills.map((m) => m[1]), ["auto", "markitdown", "docling", "glm_ocr", "markit"]);
    for (const m of pills) assert.ok(/role="radio"/.test(m[0]), `${m[1]} pill needs role="radio"`);
    assert.ok(/id="previewQualityBadge"/.test(markup));
    assert.ok(/id="qualityCheckToggle"/.test(markup));
    assert.ok(/id="conversionNoticeBar"[^>]*role="status"/.test(markup), "warnings are announced politely");
}

console.log("[OK] auto_engine_ui.test.js passed");
