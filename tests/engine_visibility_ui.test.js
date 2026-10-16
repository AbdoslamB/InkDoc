// Tests engine visibility and the engine choice in app/ui/app.js.
//
// Each engine card (Settings → Engines) has "Show on main screen". Hiding an
// engine only takes its pill off the main screen: it stays installed, Auto
// still uses it, and Auto (always shown) takes over if it was the choice. The
// saved choice is kept while an engine is not usable yet; Auto is used until
// it is.
//
// Follows save_folder_ui.test.js: slice the blocks out of the source, run them
// against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const html = fs.readFileSync("app/ui/index.html", "utf8");
const css = fs.readFileSync("app/ui/style.css", "utf8");

function slice(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    if (start < 0 || end < 0) throw new Error(`could not locate ${startMarker} in app.js`);
    return source.slice(start, end);
}

const helpers = slice("  // ─── Pure helpers", "  // ─── End of pure helpers");
const selection = slice("  // The user picked an engine", '  // "~12 s per page');
const pillsAndVisibility = slice("  // Bind unified pill clicks", "  // ─── End engine visibility");

function el(extra = {}) {
    const classes = new Set(extra.classes || []);
    const listeners = {};
    return {
        dataset: {}, attrs: {}, checked: false, disabled: false, title: "",
        classList: {
            add: (c) => classes.add(c), remove: (...cs) => cs.forEach((c) => classes.delete(c)),
            contains: (c) => classes.has(c), toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
        },
        addEventListener: (type, fn) => { listeners[type] = fn; },
        fire: (type, e = {}) => listeners[type] && listeners[type](e),
        focus() { this.focused = true; },
        ...extra,
    };
}

function setup({ hidden = [], preferred = "auto", docling = "unknown", glm = null } = {}) {
    const pills = ["auto", "markitdown", "docling", "glm_ocr", "markit"].map((engine) => {
        const p = el();
        p.dataset.engine = engine;
        return p;
    });
    const switches = ["markitdown", "markit", "docling", "glm_ocr"].map((engine) => {
        const label = el();
        const s = el({ closest: (sel) => (sel === ".toggle-switch" ? label : null) });
        s.dataset.engineVisibility = engine;
        s.label = label;
        return s;
    });
    const group = el();
    const root = { dataset: { hiddenEngines: hidden.join(" ") } };
    const saves = [];
    const calls = [];
    const env = {
        allEnginePills: pills,
        document: {
            documentElement: root,
            getElementById: (id) => (id === "enginePillGroup" ? group : null),
            querySelectorAll: (sel) => (sel === "input[data-engine-visibility]" ? switches : []),
        },
        window: { localStorage: { getItem: () => null } },
        savePrefs: (patch) => saves.push(patch),
        updateEngineUI: () => calls.push("updateEngineUI"),
        refreshSwitchEngineButton: () => calls.push("refreshSwitchEngineButton"),
        openSettings: () => calls.push("openSettings"),
        showToast: () => {},
    };
    const state = { preferred, docling, glm };
    const api = new Function("env", "state", `
        const { allEnginePills, document, window, updateEngineUI, refreshSwitchEngineButton, openSettings, showToast } = env;
        ${helpers}
        const prefs = normalizePrefs({ preferred_engine: state.preferred, hidden_engines: document.documentElement.dataset.hiddenEngines.split(" ") });
        let preferredEngine = prefs.preferred_engine;
        let selectedEngine = preferredEngine;
        let doclingState = state.docling;
        let glmStatus = state.glm;
        // As the real one: applied to prefs at once, recorded for the test.
        const savePrefs = (patch) => { env.savePrefs(patch); Object.assign(prefs, patch); };
        ${selection}
        ${pillsAndVisibility}
        return {
            prefs, chooseEngine, restorePreferredEngine, isPillShown, setEngineHidden,
            setVisibilitySwitchAvailable, showOnMainScreenAction,
            get preferred() { return preferredEngine; },
            get selected() { return selectedEngine; },
            setDocling: (s) => { doclingState = s; },
            setGlm: (s) => { glmStatus = s; },
        };
    `)(env, state);
    // Added to api itself: spreading it would read the getters once.
    return Object.assign(api, {
        pills, switches, group, root, saves, calls,
        pill: (engine) => pills.find((p) => p.dataset.engine === engine),
        sw: (engine) => switches.find((s) => s.dataset.engineVisibility === engine),
    });
}

// ── Startup: switches follow what the server put on <html> ──
{
    const t = setup({ hidden: ["docling", "markit"] });
    assert.ok(t.sw("markitdown").checked && t.sw("glm_ocr").checked);
    assert.ok(!t.sw("docling").checked && !t.sw("markit").checked);
    assert.ok(!t.isPillShown(t.pill("docling")) && t.isPillShown(t.pill("auto")));
    assert.strictEqual(t.saves.length, 0, "nothing saved by starting up");
}

// ── Hiding and showing ──
{
    const t = setup();
    t.sw("markit").checked = false;
    t.sw("markit").fire("change");
    assert.strictEqual(t.root.dataset.hiddenEngines, "markit", "the CSS hides the pill from this");
    assert.deepStrictEqual(t.saves, [{ hidden_engines: ["markit"] }]);
    assert.ok(!t.isPillShown(t.pill("markit")));
    assert.strictEqual(t.selected, "auto", "the selection didn't change");
    assert.ok(t.calls.includes("refreshSwitchEngineButton"), "Switch Engine skips it from now on");

    t.sw("docling").checked = false;
    t.sw("docling").fire("change");
    assert.strictEqual(t.root.dataset.hiddenEngines, "docling markit", "always in the same order");

    t.sw("markit").checked = true;
    t.sw("markit").fire("change");
    assert.strictEqual(t.root.dataset.hiddenEngines, "docling");
    assert.deepStrictEqual(t.saves.at(-1), { hidden_engines: ["docling"] });
}

// ── Hiding the chosen engine chooses Auto ──
{
    const t = setup({ preferred: "markit" });
    assert.strictEqual(t.selected, "markit");
    t.setEngineHidden("markit", true);
    assert.strictEqual(t.preferred, "auto");
    assert.strictEqual(t.selected, "auto");
    assert.deepStrictEqual(t.saves, [{ hidden_engines: ["markit"] }, { preferred_engine: "auto" }]);
    // Shown again: stays on Auto, the user's last choice.
    t.setEngineHidden("markit", false);
    assert.strictEqual(t.selected, "auto");
}

// ── Status checks never bring back a pill the user hid ──
{
    const t = setup({ hidden: ["docling"] });
    t.pill("docling").classList.remove("is-hidden"); // what refreshEngines does when Docling is supported
    assert.ok(!t.isPillShown(t.pill("docling")));
    t.pill("glm_ocr").classList.add("is-hidden"); // not offered here
    assert.ok(!t.isPillShown(t.pill("glm_ocr")));
}

// ── Arrow keys skip hidden pills ──
{
    const t = setup({ hidden: ["markitdown", "docling"] });
    const press = (key) => t.group.fire("keydown", { key, preventDefault() {} });
    press("ArrowRight");
    assert.strictEqual(t.selected, "glm_ocr");
    press("ArrowRight");
    assert.strictEqual(t.selected, "markit");
    press("ArrowRight");
    assert.strictEqual(t.selected, "auto", "wraps around, past the hidden ones");
    press("End");
    assert.strictEqual(t.preferred, "markit", "a keyboard choice is saved like a click");
}

// ── A pill click is a choice; clicking the current one saves nothing ──
{
    const t = setup();
    t.pill("markit").fire("click", {});
    assert.strictEqual(t.selected, "markit");
    assert.deepStrictEqual(t.saves, [{ preferred_engine: "markit" }]);
    t.pill("markit").fire("click", {});
    assert.strictEqual(t.saves.length, 1);
}

// ── The saved choice survives an engine that isn't ready; Auto meanwhile ──
{
    const t = setup({ preferred: "docling" });
    assert.strictEqual(t.selected, "docling", "kept while Docling's status is unknown");
    t.setDocling("installable");
    t.restorePreferredEngine();
    assert.strictEqual(t.selected, "auto", "Auto, not MarkItDown");
    assert.strictEqual(t.preferred, "docling");
    assert.strictEqual(t.saves.length, 0, "the automatic change is not saved");
    t.setDocling("installed");
    t.restorePreferredEngine();
    assert.strictEqual(t.selected, "docling", "back once it is installed");
}
{
    const t = setup({ preferred: "glm_ocr" });
    assert.strictEqual(t.selected, "glm_ocr");
    t.setGlm({ usable: false, status: "installing" }); // e.g. an update in progress
    t.restorePreferredEngine();
    assert.strictEqual(t.selected, "auto");
    t.setGlm({ usable: true });
    t.restorePreferredEngine();
    assert.strictEqual(t.selected, "glm_ocr");
    assert.strictEqual(t.saves.length, 0);
}

// ── An engine that can't be used on this computer: its switch is disabled ──
{
    const t = setup();
    t.setVisibilitySwitchAvailable("docling", false, "Docling is not available for this computer.");
    assert.ok(t.sw("docling").disabled);
    assert.strictEqual(t.sw("docling").label.title, "Docling is not available for this computer.");
    t.setVisibilitySwitchAvailable("docling", true);
    assert.ok(!t.sw("docling").disabled);
    assert.strictEqual(t.sw("docling").label.title, "");
}

// ── Installing an engine the user hid offers to show it ──
{
    const t = setup({ hidden: ["glm_ocr"] });
    assert.deepStrictEqual(t.showOnMainScreenAction("docling"), []);
    const [action] = t.showOnMainScreenAction("glm_ocr");
    assert.strictEqual(action.label, "Show on main screen");
    action.onClick();
    assert.strictEqual(t.root.dataset.hiddenEngines, "");
    assert.ok(t.sw("glm_ocr").checked);
}

// ── Markup: one switch at the top of each engine card, none for Auto ──
{
    const cards = { markitdown: "engineCardMarkitdown", markit: "engineCardMarkit", docling: "engineCardDocling", glm_ocr: "engineCardGlmOcr" };
    const names = { markitdown: "MarkItDown", markit: "Markit", docling: "Docling", glm_ocr: "GLM-OCR" };
    for (const [engine, id] of Object.entries(cards)) {
        const from = html.indexOf(`id="${id}"`);
        const card = html.slice(from, html.indexOf("</details>", from));
        const summary = card.slice(0, card.indexOf("</summary>"));
        assert.ok(summary.includes('class="engine-badge engine-hidden-chip">Hidden<'), `${engine}: "Hidden" chip in the header`);
        const content = card.slice(card.indexOf('<div class="engine-card-content">'));
        const first = content.match(/<div class="engine-card-content">\s*<label class="toggle-switch engine-visibility"[^>]*>\s*<input([^>]*)>/);
        assert.ok(first, `${engine}: the switch is the card's first row`);
        assert.ok(first[1].includes(`data-engine-visibility="${engine}"`));
        assert.ok(/\schecked(\s|$)/.test(first[1]), `${engine}: shown by default`);
        assert.ok(first[1].includes(`aria-label="Show on main screen: ${names[engine]}"`), "the visible words are in the name");
        assert.ok(content.includes('<span class="toggle-label">Show on main screen</span>'));
    }
    assert.strictEqual((html.match(/data-engine-visibility=/g) || []).length, 4, "Auto has no switch");
    assert.ok(html.includes("Hidden engines stay installed, and Auto still uses them."));
}

// ── CSS: hidden by attribute, exact ids, before first paint ──
{
    for (const [engine, id] of [["markitdown", "pillMarkitdown"], ["docling", "pillDocling"], ["markit", "pillMarkit"], ["glm_ocr", "pillGlmOcr"]]) {
        assert.ok(css.includes(`html[data-hidden-engines~="${engine}"] #${id}`), `${engine} pill`);
    }
    assert.ok(!css.includes('html[data-hidden-engines~="auto"]'), "Auto is never hidden");
    assert.ok(/\.engine-hidden-chip \{\s*display: none;/.test(css));
    assert.ok(css.includes('html[data-hidden-engines~="docling"] #engineCardDocling .engine-hidden-chip'));
    assert.ok(!/html\[data-hidden-engines[^\]]*\]\s*\*/.test(css), "no rule restyles every element");
}

console.log("[OK] engine_visibility_ui.test.js passed");
