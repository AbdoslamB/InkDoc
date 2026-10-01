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
    "readStorage", "writeStorage", "initialEngine", "getEngineDisplayName",
    "formatQualityWarning", "qualityAction", "describeQueueEngine",
    "describePreviewEngine", "shouldShowHintToast", "getNextEngine", "newJobId",
    "HIDE_AUTO_HINT_KEY",
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
    };
}

// ─── Default engine ──────────────────────────────────────────────────────────
{
    const h = load({ localStorage: storage() });
    assert.strictEqual(h.initialEngine(h.readStorage), "auto", "new users start on Auto");

    const saved = load({ localStorage: storage({ "inkdoc-engine": "docling" }) });
    assert.strictEqual(saved.initialEngine(saved.readStorage), "docling", "a stored choice is kept");

    const legacy = load({ localStorage: storage({ "markitdown-engine": "markit" }) });
    assert.strictEqual(legacy.initialEngine(legacy.readStorage), "markit", "the legacy key is honoured");

    const junk = load({ localStorage: storage({ "inkdoc-engine": "bogus" }) });
    assert.strictEqual(junk.initialEngine(junk.readStorage), "auto", "an unknown value falls back to Auto");

    const throwing = {
        get localStorage() { throw new Error("SecurityError: storage is disabled"); },
    };
    const locked = load(throwing);
    assert.strictEqual(locked.initialEngine(locked.readStorage), "auto", "storage that throws still defaults to Auto");
    locked.writeStorage("inkdoc-engine", "docling"); // must not throw
}

// ─── Labels ──────────────────────────────────────────────────────────────────
const h = load({ localStorage: storage() });
assert.strictEqual(h.getEngineDisplayName("auto"), "Auto");
assert.strictEqual(h.getEngineDisplayName("docling"), "Docling");
assert.strictEqual(h.getEngineDisplayName("markitdown"), "MarkItDown");
for (const engine of ["auto", "markitdown", "docling", "markit"]) {
    assert.notStrictEqual(h.getNextEngine(engine), "auto", "Switch Engine never cycles to Auto");
}
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
    const store = storage();
    const hh = load({ localStorage: store });
    const session = { hintShown: false };
    const hint = "Docling would read the 5 scanned pages. Install it in Settings.";
    assert.strictEqual(hh.shouldShowHintToast(hint, session, hh.readStorage), true);
    session.hintShown = true; // what maybeShowHintToast records
    assert.strictEqual(hh.shouldShowHintToast(hint, session, hh.readStorage), false, "a batch of 40 scans gives one tip");
    assert.strictEqual(hh.shouldShowHintToast("", { hintShown: false }, hh.readStorage), false);
    hh.writeStorage(hh.HIDE_AUTO_HINT_KEY, "1");
    assert.strictEqual(hh.shouldShowHintToast(hint, { hintShown: false }, hh.readStorage), false, "Don't show again sticks");
}

// ─── updateEngineUI keeps exactly one pill checked ───────────────────────────
{
    const updateSrc = slice("  function updateEngineUI() {", "  function setEngine(");
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
