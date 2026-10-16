// Tests the table check in app/ui/app.js: the Auto quality setting (Settings >
// General) and how a conversion's `tables` metadata is shown.
//
// Auto quality is Fast, Balanced or Thorough. A change applies at once, is
// saved to settings.json and undone if the save fails; a slower GET /settings
// can't put the old choice back while a save is on its way. formatTableNote
// turns the response's `tables` into a warning (a table left wrong) or a note
// (tables rebuilt from the PDF), or nothing.
//
// Follows layout_width_ui.test.js: slice the block out of the source, run it
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

// ─── formatTableNote ─────────────────────────────────────────────────────────
const helperBlock = slice("  // ─── Pure helpers", "  // ─── End of pure helpers");
const { formatTableNote } = new Function("window", `${helperBlock}\nreturn { formatTableNote };`)({});

assert.strictEqual(formatTableNote(null), null);
assert.strictEqual(formatTableNote({ checked: false, skipped_reason: "error" }), null);
assert.strictEqual(formatTableNote({ checked: true, warning: false, repaired: [] }), null, "nothing to say");

{
    const note = formatTableNote({
        checked: true, warning: true, repaired: [],
        message: "Page 1: 2 side-by-side tables came out as one.",
    });
    assert.strictEqual(note.warning, true);
    assert.strictEqual(note.short, "Table may be wrong");
    assert.strictEqual(note.long, "Page 1: 2 side-by-side tables came out as one.");
    assert.ok(note.toast.startsWith("but "), "reads after 'Converted X, '");
}
{
    const note = formatTableNote({ checked: true, warning: false, repaired: [{ page: 1, tables: 2 }, { page: 4, tables: 1 }] });
    assert.strictEqual(note.warning, false);
    assert.strictEqual(note.short, "3 tables rebuilt");
    assert.strictEqual(note.title, "Tables rebuilt from the PDF");
    assert.ok(note.long.includes("marked with a comment"), "says where to see it");
}
{
    const note = formatTableNote({ checked: true, warning: false, repaired: [{ page: 2, tables: 1 }] });
    assert.strictEqual(note.short, "1 table rebuilt");
    assert.strictEqual(note.title, "Table rebuilt from the PDF");
}
{
    // Thorough: figures filled in and paragraphs put back count too.
    const note = formatTableNote({ checked: true, warning: false, repaired: [{ page: 7, tables: 2 }],
        figures_filled: 4, paragraphs_restored: 1 });
    assert.strictEqual(note.short, "2 tables rebuilt · 4 figures filled in · 1 paragraph restored");
    assert.strictEqual(note.title, "Improved from the PDF");
    assert.ok(note.long.includes("chart labels and values"));
    const figs = formatTableNote({ checked: true, warning: false, repaired: [], figures_filled: 1 });
    assert.strictEqual(figs.title, "Figure text added from the PDF");
    assert.strictEqual(figs.short, "1 figure filled in");
    const paras = formatTableNote({ checked: true, warning: false, repaired: [], paragraphs_restored: 2 });
    assert.strictEqual(paras.title, "Text restored from the PDF");
    assert.strictEqual(paras.toast, "2 paragraphs restored from the PDF");
}
{
    // A table left wrong outranks tables rebuilt elsewhere in the same file.
    const note = formatTableNote({ checked: true, warning: true, message: "m", repaired: [{ page: 1, tables: 1 }] });
    assert.strictEqual(note.warning, true);
}

// ─── Auto quality setting ────────────────────────────────────────────────────
const qualityBlock = slice("  // ─── Auto quality ", "  // ─── End Auto quality");

function setup() {
    const radios = ["fast", "balanced", "thorough"].map((value) => ({
        value, checked: false, listeners: [],
        addEventListener(type, fn) { if (type === "change") this.listeners.push(fn); },
    }));
    const posts = [];
    const toasts = [];
    const pending = [];
    const env = {
        document: { querySelectorAll: (sel) => (sel === 'input[name="autoQuality"]' ? radios : []) },
        apiBase: "/InkDoc",
        showToast: (msg, kind) => toasts.push({ msg, kind }),
        fetchWithToken: (url, opts) => {
            posts.push({ url, body: JSON.parse(opts.body) });
            return new Promise((resolve, reject) => pending.push({ resolve, reject }));
        },
    };
    const api = new Function("env", `
        const { document, apiBase, showToast, fetchWithToken } = env;
        ${qualityBlock}
        return { syncAutoQuality, current: () => autoQuality };
    `)(env);
    const choose = (value) => {
        const radio = radios.find((r) => r.value === value);
        radios.forEach((r) => { r.checked = r === radio; });
        return Promise.all(radio.listeners.map((fn) => fn()));
    };
    const checked = () => radios.find((r) => r.checked).value;
    return { api, radios, posts, toasts, pending, choose, checked };
}

(async () => {
    {
        const t = setup();
        assert.strictEqual(t.api.current(), "fast", "Fast until the settings arrive");
        assert.strictEqual(t.checked(), "fast");
        t.api.syncAutoQuality("balanced");
        assert.strictEqual(t.checked(), "balanced");
        t.api.syncAutoQuality("nonsense");
        assert.strictEqual(t.checked(), "fast", "unknown values fall back to Fast");
    }
    {
        // A change is saved, and a slower read does not undo it meanwhile.
        const t = setup();
        const done = t.choose("thorough");
        assert.deepStrictEqual(t.posts[0], { url: "/InkDoc/settings", body: { auto_quality: "thorough" } });
        t.api.syncAutoQuality("fast");
        assert.strictEqual(t.checked(), "thorough");
        t.pending[0].resolve({ ok: true });
        await done;
        assert.strictEqual(t.api.current(), "thorough");
        assert.strictEqual(t.toasts.length, 0);
    }
    {
        // A failed save puts the previous level back and says so.
        const t = setup();
        t.api.syncAutoQuality("balanced");
        const done = t.choose("thorough");
        t.pending[0].resolve({ ok: false });
        await done;
        assert.strictEqual(t.checked(), "balanced");
        assert.strictEqual(t.toasts[0].kind, "error");
    }

    // ─── Markup ──────────────────────────────────────────────────────────────
    for (const level of ["fast", "balanced", "thorough"]) {
        assert.ok(html.includes(`name="autoQuality" value="${level}"`), `radio for ${level}`);
    }
    assert.ok(/id="autoQualityGroup" role="radiogroup" aria-labelledby="autoQualityLabel"/.test(html));
    assert.ok(html.includes('value="fast" checked'), "Fast is checked before the settings load");
    assert.ok(source.includes("if (s.auto_quality !== undefined) syncAutoQuality(s.auto_quality);"));
    assert.ok(source.includes("item.tables           = data.tables || null;"));

    console.log("tables_ui.test.js: all assertions passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
