// Tests the Layout width setting (Settings > General > Display) and the table
// scroll boxes in app/ui/app.js, plus the CSS and markup they rely on.
//
// The server puts the saved width on <html> before first paint; the page keeps
// the radios in step, applies a change at once, saves it to settings.json and
// undoes it if the save fails. Each rendered table gets its own scroll box.
//
// Follows save_folder_ui.test.js: slice the block out of the source, run it
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
const widthBlock = slice("  // ─── Layout width ", "  // ─── End layout width");
const tableBlock = slice("  // ─── Table scroll boxes ", "  // ─── End table scroll boxes");

function setup({ initial, respond = () => ({ ok: true }) } = {}) {
    const root = { dataset: {}, writes: 0 };
    let current;
    Object.defineProperty(root.dataset, "layoutWidth", {
        get: () => current,
        set: (v) => { current = v; root.writes++; },
        enumerable: true,
    });
    if (initial !== undefined) { current = initial; }
    const radios = ["standard", "wide", "full"].map((value) => ({
        value, checked: value === "standard", listeners: [],
        addEventListener(type, fn) { if (type === "change") this.listeners.push(fn); },
    }));
    const posts = [];
    const toasts = [];
    const pending = [];
    const env = {
        document: {
            documentElement: root,
            querySelectorAll: (sel) => (sel === 'input[name="layoutWidth"]' ? radios : []),
        },
        apiBase: "/InkDoc",
        showToast: (msg, kind) => toasts.push({ msg, kind }),
        // Each POST resolves when the test says so, to check ordering.
        fetchWithToken: (url, opts) => {
            posts.push({ url, body: JSON.parse(opts.body) });
            return new Promise((resolve, reject) => pending.push({ resolve, reject, body: JSON.parse(opts.body) }));
        },
    };
    const api = new Function("env", `
        const { document, apiBase, showToast, fetchWithToken } = env;
        ${widthBlock}
        return { syncLayoutWidth, current: () => layoutWidth };
    `)(env);
    const choose = async (value) => {
        radios.forEach((r) => { r.checked = r.value === value; });
        const r = radios.find((x) => x.value === value);
        const done = Promise.all(r.listeners.map((fn) => fn({ target: r })));
        return done;
    };
    const settle = () => new Promise((r) => setImmediate(r));
    const checked = () => radios.filter((r) => r.checked).map((r) => r.value);
    return { ...api, root, radios, posts, toasts, pending, choose, settle, checked, respond };
}

(async () => {
    // The width the server wrote is kept and shown; nothing is rewritten.
    {
        const t = setup({ initial: "wide" });
        assert.strictEqual(t.current(), "wide");
        assert.deepStrictEqual(t.checked(), ["wide"]);
        assert.strictEqual(t.root.writes, 0, "no write when the page already has the right width");
    }

    // No attribute (or a strange one) means Standard.
    for (const initial of [undefined, "huge", ""]) {
        const t = setup({ initial });
        assert.strictEqual(t.current(), "standard");
        assert.strictEqual(t.root.dataset.layoutWidth, "standard");
        assert.deepStrictEqual(t.checked(), ["standard"]);
    }

    // Choosing applies at once, then saves exactly that value.
    {
        const t = setup();
        const done = t.choose("full");
        assert.strictEqual(t.root.dataset.layoutWidth, "full", "applied before the save returns");
        assert.deepStrictEqual(t.posts, [{ url: "/InkDoc/settings", body: { layout_width: "full" } }]);
        t.pending[0].resolve({ ok: true });
        await done;
        assert.strictEqual(t.current(), "full");
        assert.deepStrictEqual(t.toasts, [], "no toast when it worked: the page itself shows the change");
    }

    // A failed save (HTTP error or no connection) puts the previous width back and says so.
    for (const fail of ["http", "network"]) {
        const t = setup({ initial: "wide" });
        const done = t.choose("standard");
        if (fail === "http") t.pending[0].resolve({ ok: false });
        else t.pending[0].reject(new Error("offline"));
        await done;
        assert.strictEqual(t.root.dataset.layoutWidth, "wide");
        assert.deepStrictEqual(t.checked(), ["wide"]);
        assert.deepStrictEqual(t.toasts, [{ msg: "Couldn't save the layout width.", kind: "error" }]);
    }

    // Choosing the current width again sends nothing.
    {
        const t = setup({ initial: "wide" });
        await t.choose("wide");
        assert.deepStrictEqual(t.posts, []);
    }

    // Two quick choices: the newest decides, an older failure doesn't undo it.
    {
        const t = setup();
        const first = t.choose("wide");
        const second = t.choose("full");
        t.pending[0].resolve({ ok: false });
        await first;
        assert.strictEqual(t.root.dataset.layoutWidth, "full", "an older failed save doesn't undo a newer choice");
        t.pending[1].resolve({ ok: true });
        await second;
        assert.strictEqual(t.current(), "full");
        assert.deepStrictEqual(t.toasts, []);
    }

    // Opening Settings re-reads the setting, but never over a save in progress.
    {
        const t = setup();
        t.syncLayoutWidth("wide");
        assert.strictEqual(t.root.dataset.layoutWidth, "wide");
        const done = t.choose("full");
        t.syncLayoutWidth("wide"); // the read that started before the click
        assert.strictEqual(t.root.dataset.layoutWidth, "full");
        t.pending[0].resolve({ ok: true });
        await done;
        t.syncLayoutWidth("full");
        assert.strictEqual(t.current(), "full");
        t.syncLayoutWidth("bogus");
        assert.strictEqual(t.current(), "standard", "an unknown value from the server means Standard");
    }

    // Table scroll boxes: one box per table, idempotent, other content untouched.
    {
        function node(tag, parent = null) {
            const n = {
                tag, parentElement: parent, children: [],
                classList: { contains: (c) => (n.className || "").split(" ").includes(c) },
                replaceWith(other) {
                    const siblings = n.parentElement.children;
                    siblings[siblings.indexOf(n)] = other;
                    other.parentElement = n.parentElement;
                    n.parentElement = null;
                },
                appendChild(child) { child.parentElement = n; n.children.push(child); },
            };
            if (parent) parent.children.push(n);
            return n;
        }
        const root = node("div");
        node("p", root);
        const t1 = node("table", root);
        node("pre", root);
        const t2 = node("table", root);
        const all = () => { const out = []; const walk = (n) => n.children.forEach((c) => { if (c.tag === "table") out.push(c); walk(c); }); walk(root); return out; };
        root.querySelectorAll = (sel) => (sel === "table" ? all() : []);
        const env = { document: { createElement: (tag) => node(tag) } };
        const { wrapTables } = new Function("env", `const { document } = env; ${tableBlock} return { wrapTables };`)(env);

        wrapTables(root);
        assert.deepStrictEqual(root.children.map((c) => c.tag), ["p", "div", "pre", "div"]);
        [t1, t2].forEach((tb) => {
            assert.strictEqual(tb.parentElement.className, "table-scroll");
            assert.strictEqual(tb.parentElement.parentElement, root);
        });
        wrapTables(root);
        assert.deepStrictEqual(root.children.map((c) => c.tag), ["p", "div", "pre", "div"], "wrapping twice adds nothing");
    }

    // Wiring in app.js: tables are wrapped on every render, before highlighting;
    // Settings re-reads the width (guarded, so the add-on test's slice still runs).
    {
        const render = source.indexOf("      const rawHtml = markdownToHtml(md);");
        const wrap = source.indexOf("    wrapTables(renderedOutput);", render);
        const highlight = source.indexOf("    highlightCodeBlocks(renderedOutput);", render);
        assert.ok(render > 0 && wrap > render && highlight > wrap, "wrapTables runs after sanitising, before highlighting");
        const loadStart = source.indexOf("  async function loadSettings() {");
        const loadEnd = source.indexOf("renderSaveFolder(s.save_folder);", loadStart);
        assert.ok(source.slice(loadStart, loadEnd).includes("if (s.layout_width !== undefined) syncLayoutWidth(s.layout_width);"));
    }

    // Markup: one radio per width, Standard checked by default, in General.
    {
        const values = [...html.matchAll(/<input type="radio" class="sr-only" name="layoutWidth" value="(\w+)"( checked)?>/g)];
        assert.deepStrictEqual(values.map((m) => m[1]), ["standard", "wide", "full"]);
        assert.deepStrictEqual(values.filter((m) => m[2]).map((m) => m[1]), ["standard"]);
        const general = html.slice(html.indexOf('data-settings-pane="general"'), html.indexOf('data-settings-pane="engines"'));
        assert.ok(general.includes('id="layoutWidthGroup"') && general.includes('role="radiogroup"'));
    }

    // CSS: the three widths, Standard as before, and never animated.
    {
        assert.ok(/:root \{\s*--layout-max: 1400px;/.test(css));
        assert.ok(/html\[data-layout-width="wide"\] \{\s*--layout-max: 1920px;/.test(css));
        assert.ok(/html\[data-layout-width="full"\] \{\s*--layout-max: none;/.test(css));
        const container = css.slice(css.indexOf(".app-container {"), css.indexOf("}", css.indexOf(".app-container {")));
        assert.ok(container.includes("max-width: var(--layout-max);"));
        assert.ok(!/transition/.test(container), "the width must not be animated (long documents would stutter)");
        assert.ok(/\.markdown-rendered \.table-scroll \{\s*overflow-x: auto;/.test(css));
        assert.ok(/\.markdown-rendered img \{\s*max-width: 100%;\s*height: auto;/.test(css));
        assert.ok(/\.markdown-rendered > p,[\s\S]*?max-width: calc\(1000px \* var\(--preview-scale\)\);/.test(css), "text keeps 1000px at 100% and the same line length at other text sizes");
        assert.ok(/\.markdown-rendered > p:has\(> img\) \{\s*max-width: none;/.test(css),
            "a paragraph holding a picture is not held to the text width");
    }

    console.log("layout width UI tests passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
