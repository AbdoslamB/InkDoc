// Tests the Auto quality steps in app/ui/app.js (Settings > General > Auto
// quality > Steps): one switch per step of Balanced and Thorough.
//
// The steps come from GET /settings (thorough_passes), never from a list in
// the page. The setting is the list of steps switched off. A step whose
// engine isn't usable here is greyed out, with a button that opens that
// engine's card. A change applies at once, is saved, and is undone if the
// save fails; a slower GET /settings can't put the old state back while a
// save is on its way.
//
// Follows tables_ui.test.js: slice the blocks out of the source, run them
// against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const html = fs.readFileSync("app/ui/index.html", "utf8");

function slice(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    if (start < 0 || end < 0) throw new Error(`could not locate ${startMarker.trim()} in app.js`);
    return source.slice(start, end);
}

const helperBlock = slice("  // ─── Pure helpers", "  // ─── End of pure helpers");
const { autoStepNeeds, getEngineDisplayName } = new Function(
    "window", `${helperBlock}\nreturn { autoStepNeeds, getEngineDisplayName };`
)({});
const stepsBlock = slice("  // ─── Auto quality steps ", "  // ─── End Auto quality steps");

// The steps as GET /settings sends them (app/core/thorough_passes.py catalogue()).
const STEPS = [
    { name: "digital_layout", title: "Layout-aware reading", description: "d1",
      level: "thorough", needs_all: [], needs_any: ["docling", "glm_ocr"], cost: "high" },
    { name: "polish", title: "Original punctuation", description: "d2",
      level: "thorough", needs_all: [], needs_any: [], cost: "low" },
    { name: "table_check", title: "Table check", description: "d3",
      level: "balanced", needs_all: [], needs_any: [], cost: "low" },
    { name: "borderless_tables", title: "Tables without lines", description: "d4",
      level: "thorough", needs_all: ["glm_ocr"], needs_any: [], cost: "medium" },
];

// ─── autoStepNeeds ───────────────────────────────────────────────────────────
{
    const avail = (map) => (engine) => map[engine];
    const byName = (name) => STEPS.find((s) => s.name === name);
    assert.strictEqual(autoStepNeeds(byName("polish"), avail({})), null, "no engine needed");
    assert.deepStrictEqual(autoStepNeeds(byName("borderless_tables"), avail({ glm_ocr: false })),
        { text: "Needs GLM-OCR", install: "glm_ocr" });
    assert.strictEqual(autoStepNeeds(byName("borderless_tables"), avail({})), null,
        "not known yet counts as usable: nothing greys out before the engines' status arrives");
    assert.strictEqual(autoStepNeeds(byName("digital_layout"), avail({ docling: false, glm_ocr: true })), null);
    assert.deepStrictEqual(autoStepNeeds(byName("digital_layout"), avail({ docling: false, glm_ocr: false })),
        { text: "Needs Docling or GLM-OCR", install: "docling" });
    const both = { name: "page_split", needs_all: ["docling", "glm_ocr"], needs_any: [] };
    assert.deepStrictEqual(autoStepNeeds(both, avail({ docling: false, glm_ocr: false })),
        { text: "Needs Docling and GLM-OCR", install: "docling" });
    assert.deepStrictEqual(autoStepNeeds(both, avail({ docling: true, glm_ocr: false })),
        { text: "Needs GLM-OCR", install: "glm_ocr" }, "only what is missing");
    assert.strictEqual(autoStepNeeds({ name: "x" }, avail({})), null, "a step without needs fields");
}

// ─── A minimal DOM ───────────────────────────────────────────────────────────
class Node {
    constructor(tag) {
        this.tag = tag;
        this.children = [];
        this.attrs = {};
        this.dataset = {};
        this.listeners = {};
        this.hidden = false;
        this.className = "";
        this.textContent = "";
        this.title = "";
    }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; this.replaced = (this.replaced || 0) + 1; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    fire(type, event = {}) { return Promise.all((this.listeners[type] || []).map((fn) => fn(event))); }
    find(pred) {
        for (const child of this.children) {
            if (pred(child)) return child;
            const deeper = child.find(pred);
            if (deeper) return deeper;
        }
        return null;
    }
}

function setup({ available = {} } = {}) {
    const box = new Node("details");
    box.hidden = true; // as in index.html, until the steps arrive
    const list = new Node("ul");
    const posts = [];
    const toasts = [];
    const opened = [];
    const pending = [];
    const env = {
        document: {
            getElementById: (id) => ({ autoSteps: box, autoStepsList: list })[id] || null,
            createElement: (tag) => new Node(tag),
        },
        apiBase: "/InkDoc",
        showToast: (msg, kind) => toasts.push({ msg, kind }),
        fetchWithToken: (url, opts) => {
            posts.push({ url, body: JSON.parse(opts.body) });
            return new Promise((resolve, reject) => pending.push({ resolve, reject }));
        },
        engineAvailable: (engine) => available[engine],
        openEngineCard: (e, engine) => opened.push(engine),
        autoStepNeeds,
        getEngineDisplayName,
    };
    const api = new Function("env", `
        const { document, apiBase, showToast, fetchWithToken, engineAvailable, openEngineCard,
                autoStepNeeds, getEngineDisplayName } = env;
        ${stepsBlock}
        return { syncAutoSteps, updateAutoSteps, off: () => autoStepsOff };
    `)(env);
    const row = (name) => list.children.find((li) => li.find((n) => n.dataset && n.dataset.autoStep === name));
    const input = (name) => row(name).find((n) => n.tag === "input");
    const needs = (name) => row(name).find((n) => n.className === "auto-step-needs");
    const install = (name) => needs(name).find((n) => n.tag === "button");
    const toggle = (name, on) => { input(name).checked = on; return input(name).fire("change"); };
    return { api, box, list, posts, toasts, opened, pending, available, row, input, needs, install, toggle };
}

(async () => {
    {
        // Built from the server's list; the switches show the setting.
        const t = setup();
        t.api.syncAutoSteps(undefined, ["polish"]);
        assert.strictEqual(t.box.hidden, true, "nothing to show before the steps arrive");
        t.api.syncAutoSteps(STEPS, ["polish", "from_a_newer_version"]);
        assert.strictEqual(t.box.hidden, false);
        assert.strictEqual(t.list.children.length, STEPS.length);
        assert.strictEqual(t.list.children[0], t.row("table_check"), "Balanced's step first");
        assert.strictEqual(t.list.children[1], t.row("digital_layout"), "then the server's order");
        assert.strictEqual(t.input("polish").checked, false);
        assert.strictEqual(t.input("table_check").checked, true);
        assert.deepStrictEqual(t.api.off(), ["polish"], "unknown names dropped: they would fail the next save");
        const level = (name) => t.row(name).find((n) => n.className === "auto-step-level").textContent;
        assert.strictEqual(level("table_check"), "Balanced and Thorough");
        assert.strictEqual(level("polish"), "Thorough");
        const caption = t.row("polish").find((n) => n.className === "settings-caption");
        assert.strictEqual(t.input("polish").attrs["aria-describedby"], caption.id, "the description is announced");
        assert.strictEqual(caption.textContent, "d2");
        // The same list again: rows are kept, so a refresh never steals the focus.
        t.api.syncAutoSteps(STEPS.map((s) => ({ ...s })), []);
        assert.strictEqual(t.list.replaced, 1);
        assert.strictEqual(t.input("polish").checked, true);
        t.api.syncAutoSteps([{ name: 7 }, null, { name: "polish", title: "P" }], []);
        assert.strictEqual(t.list.children.length, 1, "malformed entries are skipped");
    }
    {
        // A step whose engine isn't usable is greyed out, with a way to install it.
        const t = setup({ available: { docling: true, glm_ocr: false } });
        t.api.syncAutoSteps(STEPS, []);
        assert.strictEqual(t.input("borderless_tables").disabled, true);
        assert.strictEqual(t.input("borderless_tables").checked, true, "the choice is kept, only greyed out");
        assert.strictEqual(t.needs("borderless_tables").hidden, false);
        assert.strictEqual(t.needs("borderless_tables").find((n) => n.tag === "span").textContent, "Needs GLM-OCR");
        assert.strictEqual(t.install("borderless_tables").attrs["aria-label"], "Install GLM-OCR");
        await t.install("borderless_tables").fire("click");
        assert.deepStrictEqual(t.opened, ["glm_ocr"]);
        assert.strictEqual(t.input("digital_layout").disabled, false, "Docling is enough");
        assert.strictEqual(t.needs("digital_layout").hidden, true);
        // GLM-OCR installed meanwhile: the engines' refresh updates the switches.
        t.available.glm_ocr = true;
        t.api.updateAutoSteps();
        assert.strictEqual(t.input("borderless_tables").disabled, false);
        assert.strictEqual(t.needs("borderless_tables").hidden, true);
    }
    {
        // A change is saved, and a slower read does not undo it meanwhile.
        const t = setup();
        t.api.syncAutoSteps(STEPS, ["polish"]);
        const done = t.toggle("table_check", false);
        assert.deepStrictEqual(t.posts[0], { url: "/InkDoc/settings", body: { thorough_passes_off: ["polish", "table_check"] } });
        t.api.syncAutoSteps(STEPS, ["polish"]);
        assert.strictEqual(t.input("table_check").checked, false);
        t.pending[0].resolve({ ok: true });
        await done;
        assert.strictEqual(t.toasts.length, 0);
        // Switched back on.
        const again = t.toggle("polish", true);
        assert.deepStrictEqual(t.posts[1].body, { thorough_passes_off: ["table_check"] });
        t.pending[1].resolve({ ok: true });
        await again;
        assert.deepStrictEqual(t.api.off(), ["table_check"]);
    }
    {
        // A failed save puts the switch back and says so.
        const t = setup();
        t.api.syncAutoSteps(STEPS, []);
        const done = t.toggle("polish", false);
        t.pending[0].resolve({ ok: false });
        await done;
        assert.strictEqual(t.input("polish").checked, true);
        assert.deepStrictEqual(t.api.off(), []);
        assert.strictEqual(t.toasts[0].kind, "error");
    }
    {
        // Two quick changes: only the newest save decides.
        const t = setup();
        t.api.syncAutoSteps(STEPS, []);
        const first = t.toggle("polish", false);
        const second = t.toggle("table_check", false);
        assert.deepStrictEqual(t.posts[1].body, { thorough_passes_off: ["polish", "table_check"] });
        t.pending[0].resolve({ ok: false });
        t.pending[1].resolve({ ok: true });
        await Promise.all([first, second]);
        assert.deepStrictEqual(t.api.off(), ["polish", "table_check"]);
        assert.strictEqual(t.toasts.length, 0);
    }

    // ─── Markup and wiring ───────────────────────────────────────────────────
    assert.ok(/<details class="auto-steps" id="autoSteps" hidden>/.test(html), "collapsed and hidden until filled");
    assert.ok(html.includes('<ul class="auto-steps-list" id="autoStepsList"'));
    assert.ok(html.indexOf('id="autoSteps"') > html.indexOf('id="autoQualityCaption"'), "under Auto quality");
    assert.ok(source.includes("syncAutoSteps(s.thorough_passes, s.thorough_passes_off);"), "fed by applySettings");
    const refresh = slice("  async function refreshEngines()", "  // ─── GLM-OCR engine card");
    assert.ok(/finally \{[\s\S]*updateAutoSteps\(\);/.test(refresh), "Docling's status updates the switches");
    const glm = slice("  function renderGlm(st)", "  async function refreshGlm(");
    assert.ok(glm.includes("updateAutoSteps();"), "GLM-OCR's status updates the switches");
    assert.ok(!/innerHTML/.test(stepsBlock), "built with textContent only");

    console.log("auto_steps_ui.test.js: all assertions passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
