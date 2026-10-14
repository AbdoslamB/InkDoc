// Tests math formulas (Settings > Add-ons, KaTeX) in app/ui/app.js.
//
// Detection runs on the real bundled marked; KaTeX, DOMPurify, timers and
// IntersectionObserver are stubs, so the scheduling can be checked exactly.
// Off, or for a document without a delimiter, the preview must come out of
// the global parser unchanged. KaTeX must never load for such documents.
//
// Follows save_folder_ui.test.js: slice the block out of the source, run it
// against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const html = fs.readFileSync("app/ui/index.html", "utf8");

function slice(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    if (start < 0 || end < 0) throw new Error(`could not locate ${startMarker.trim()} in app.js`);
    return source.slice(start, end);
}
const block = slice("  // ─── Math formulas (built-in add-on: KaTeX) ", "  // ─── End math formulas");
const escapeStart = source.indexOf("  function escapeHtml(text) {");
const escapeFn = source.slice(escapeStart, source.indexOf("\n  }", escapeStart) + 4);

// The real bundled marked, loaded the way the page loads it.
function loadMarked() {
    const ctx = {};
    ctx.globalThis = ctx; ctx.window = ctx; ctx.self = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync("app/ui/vendor/marked.min.js", "utf8"), ctx);
    ctx.marked.use({ gfm: true, breaks: true }); // as the render function does
    return ctx.marked;
}

function setup({ renderMath = "on", katexOk = true, cssOk = true, purify = true, io = true, msPerFormula = 3, registers = () => true } = {}) {
    const marked = loadMarked();
    const root = { dataset: {} };
    if (renderMath !== undefined) root.dataset.renderMath = renderMath;
    let now = 0;
    let timers = [];
    const warnings = [];
    const appended = [];
    const katexCalls = [];
    const katex = {
        renderToString(tex, opts) {
            katexCalls.push({ tex, opts });
            now += msPerFormula; // each formula "takes" this long
            if (tex.includes("\\bad")) throw new Error("ParseError");
            return `<span class="katex">${opts.displayMode ? "D" : "I"}:${tex}</span>`;
        },
    };
    let ioCallback = null;
    const observed = new Set();
    const sanitizeCalls = [];
    const win = {
        marked,
        DOMPurify: purify ? { sanitize: (h, cfg) => { sanitizeCalls.push(cfg); return `[clean]${h}`; } } : undefined,
        IntersectionObserver: io ? function (cb) { ioCallback = cb; return { observe: (n) => observed.add(n), unobserve: (n) => observed.delete(n), disconnect: () => observed.clear() }; } : undefined,
    };
    function element(tag) {
        const el = { tag, onload: null, onerror: null };
        // escapeHtml sets textContent and reads innerHTML; serialise as a browser does.
        let text = "";
        Object.defineProperty(el, "textContent", { set: (v) => { text = String(v); }, get: () => text });
        Object.defineProperty(el, "innerHTML", {
            get: () => text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/ /g, "&nbsp;"),
        });
        return el;
    }
    const doc = {
        documentElement: root,
        getElementById: () => null,
        createElement: element,
        head: {
            appendChild(el) {
                appended.push(el);
                setImmediate(() => {
                    if (el.tag === "script") {
                        if (katexOk) { if (registers()) win.katex = katex; el.onload && el.onload(); } else { el.onerror && el.onerror(); }
                    } else if (el.tag === "link") {
                        cssOk ? el.onload && el.onload() : el.onerror && el.onerror();
                    }
                });
            },
        },
    };
    const selected = [];
    const env = {
        window: win, document: doc,
        console: { warn: (m) => warnings.push(m) },
        performance: { now: () => now },
        setTimeout: (fn, ms) => { timers.push(fn); return timers.length; },
        selectItem: (id) => selected.push(id),
        getActive: () => "item-1",
        fetchWithToken: async () => ({ ok: true }), apiBase: "/InkDoc", showToast: () => {},
    };
    const api = new Function("env", `
        const { window, document, console, performance, setTimeout, selectItem, fetchWithToken, apiBase, showToast } = env;
        let activeItemId = env.getActive();
        ${escapeFn}
        ${block}
        return { markdownToHtml, typesetMath, cancelMath, applyRenderMath, syncRenderMath, mark: MATH_MARK,
                 state: () => ({ renderMath, mathParser, mathCacheSize: mathCache.size }) };
    `)(env);
    // Run queued timers (and anything they queue) until none are left.
    const flush = async () => {
        for (let guard = 0; guard < 1000; guard++) {
            await new Promise((r) => setImmediate(r));
            if (!timers.length) {
                await new Promise((r) => setImmediate(r));
                if (!timers.length) return;
            }
            const run = timers; timers = [];
            run.forEach((fn) => fn());
        }
        throw new Error("timers never settled");
    };
    const stepOnce = async () => {
        await new Promise((r) => setImmediate(r));
        const run = timers; timers = [];
        run.forEach((fn) => fn());
    };
    const r = (specs) => mathRoot(specs, api.mark);
    return { ...api, marked, win, root, appended, katexCalls, warnings, selected, flush, stepOnce, r, sanitizeCalls,
             io: () => ioCallback, observed, timersPending: () => timers.length, advance: (ms) => { now += ms; } };
}

// A tiny DOM for typesetMath: nodes with class "math-tex" and the session
// marker (spec[2] === false leaves it off, like look-alike HTML in a document).
function mathRoot(specs, mark) {
    const nodes = specs.map(([tex, display, marked = true]) => ({
        textContent: tex,
        dataset: { display: display ? "1" : "0", math: marked ? mark : "" },
        classes: new Set(["math-tex"]),
        classList: { add(c) { this.owner.classes.add(c); } },
        title: "",
        set innerHTML(v) { this._html = v; },
        get innerHTML() { return this._html; },
    }));
    nodes.forEach((n) => { n.classList.owner = n; });
    return {
        nodes,
        querySelectorAll: (sel) => (sel === `.math-tex[data-math="${mark}"]` ? nodes.filter((n) => n.dataset.math === mark) : []),
        closest: () => ({ scroller: true }),
    };
}

const hasMath = (out) => /class="math-tex/.test(out);

(async () => {
    // ── Detection: formulas from Docling and GLM-OCR become placeholders ──
    {
        const t = setup();
        const cases = [
            ["Energy $E = mc^2$ here.", "E = mc^2", false],
            ["$$\\sum_{i=1}^{n} x_i^2 = \\frac{a*b}{c*d}$$", "\\sum_{i=1}^{n} x_i^2 = \\frac{a*b}{c*d}", true],
            ["$$\n\\mathbf{x}_k = A_{k} \\mathbf{x}_{k-1} \\\\\ny = x^*\n$$", "\\mathbf{x}_k = A_{k} \\mathbf{x}_{k-1} \\\\\ny = x^*", true],
            ["Inline \\(a_1 * b_1\\) here.", "a_1 * b_1", false],
            ["\\[\n\\frac{a*b}{c*d}\n\\]", "\\frac{a*b}{c*d}", true],
            ["see $$x^2$$ inline", "x^2", true],
            ["| A | B |\n|---|---|\n| $a_1$ | 2 |", "a_1", false],
            ["**$x_i$** bold", "x_i", false],
            ["Costs $\\$5$ in TeX", "\\$5", false],
        ];
        for (const [md, tex, display] of cases) {
            const out = t.markdownToHtml(md);
            const want = display ? `data-display="1">` : `data-display="0">`;
            assert.ok(hasMath(out) && out.includes(want), `should be math: ${JSON.stringify(md)} -> ${out}`);
            const escaped = tex.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
            assert.ok(out.includes(escaped), `LaTeX kept exactly: ${JSON.stringify(md)} -> ${out}`);
            assert.ok(!/<em>|<br>/.test(out.split('class="math-tex')[1].split("</")[0]), `not mangled: ${out}`);
        }
    }

    // ── False positives: ordinary text stays text (Pandoc's $ rule) ──
    {
        const t = setup();
        const plain = [
            "Price $5 and $10 each.",
            "From $1,200 – $3,400 a year.",
            "costs $ 5 or $ 6",
            "echo $HOME and $PATH",
            "Use ${var} and ${other} in templates.",
            "a lone $ sign",
            "US$ 20 and $30",
            "Inline `$x$` code",
            "```\n$$x^2$$\n```",
            "Escaped \\$x\\$ dollars",
            "$$\n\nnot math\n\n$$",
            "Total: $5.\nNext line $6.",
            "Range $1$2 only the digit rule stops", // closing $ followed by a digit
            "Total $3,400. Code: `$x$` here.", // a price must not pair with a $ in inline code
        ];
        for (const md of plain) {
            const out = t.markdownToHtml(md);
            assert.ok(!hasMath(out), `should stay text: ${JSON.stringify(md)} -> ${out}`);
        }
        // Too long for inline: stays text.
        assert.ok(!hasMath(t.markdownToHtml("$" + "x".repeat(1001) + "$")));
    }

    // ── Placeholders carry the per-session marker ──
    {
        const t = setup();
        assert.match(t.mark, /^[0-9a-f]{16}$/);
        assert.ok(t.markdownToHtml("a $x$ b").includes(`data-math="${t.mark}"`));
        assert.notStrictEqual(setup().mark, t.mark, "a new marker every session");
    }

    // ── Escaping: a placeholder can never carry markup ──
    {
        const t = setup();
        const out = t.markdownToHtml("$<img src=x onerror=alert(1)>$ and $a</span><script>x$");
        assert.ok(hasMath(out));
        assert.ok(!/<img|<script/.test(out), out);
        assert.ok(out.includes("&lt;img src=x onerror=alert(1)&gt;"));
    }

    // ── Linear time: a large document with one $ stays near the global parser ──
    // (The first version re-scanned the rest of the text before every token:
    // 880 KB of log lines with one $ took ~37 s. Security review, 1.2.0.)
    {
        const t = setup();
        const log = "Cost $5\n" + Array.from({ length: 20000 }, (_, i) => `2026-10-14 12:00 INFO worker ${i} finished task ${i * 7}`).join("\n");
        const cases = [log, "$x$ " + "! ".repeat(80000), "$$\n" + "a\n".repeat(40000), "$$a$$b\n\n".repeat(16000)];
        for (const md of cases) {
            const t0 = Date.now(); t.marked.parse(md); const base = Date.now() - t0;
            const t1 = Date.now(); t.markdownToHtml(md); const math = Date.now() - t1;
            assert.ok(math <= base * 8 + 500, `math parsing ${math} ms vs ${base} ms for ${md.length} characters`);
        }
        // Over the size cap: the global parser, exactly.
        const huge = "$x$\n\n" + "word ".repeat(400001);
        assert.strictEqual(t.markdownToHtml(huge), t.marked.parse(huge));
    }

    // ── Cutting long text to look ahead never changes the text around formulas ──
    {
        const t = setup();
        const prose = Array.from({ length: 60 }, (_, i) => `word${i} *em${i}* \`code${i}\` https://example.com/p${i} and more`).join(" ");
        const md = "$x$ " + prose + " $y$ " + "z".repeat(1500) + " $w$";
        const out = t.markdownToHtml(md);
        const ref = t.marked.parse(md);
        const count = (h, re) => (h.match(re) || []).length;
        assert.strictEqual(count(out, /<em>/g), count(ref, /<em>/g), "emphasis");
        assert.strictEqual(count(out, /<code>/g), count(ref, /<code>/g), "code spans");
        assert.strictEqual(count(out, /<a href="https:\/\/example\.com\/p\d+">/g), 60, "every link kept whole");
        assert.strictEqual(count(out, /class="math-tex"/g), 3, "formulas found before and after long runs");
    }

    // ── Off, or no delimiter: exactly the global parser, no math parser built ──
    {
        const mathDoc = "A $x^2$ and $$\\frac{a*b}{c}$$";
        const off = setup({ renderMath: "off" });
        assert.strictEqual(off.markdownToHtml(mathDoc), off.marked.parse(mathDoc));
        assert.strictEqual(off.state().mathParser, null);
        const on = setup();
        const plainDoc = "# Title\n\nNo formulas, *emphasis* and `code`.";
        assert.strictEqual(on.markdownToHtml(plainDoc), on.marked.parse(plainDoc));
        assert.strictEqual(on.state().mathParser, null, "no math parser for a document without delimiters");
        assert.notStrictEqual(on.markdownToHtml(mathDoc), on.marked.parse(mathDoc));
        on.markdownToHtml(plainDoc);
        assert.strictEqual(on.marked.parse("$a$"), "<p>$a$</p>\n", "the global parser is never given the math rules");
    }

    // ── No attribute (server didn't say) means on; only "off" turns it off ──
    {
        assert.strictEqual(setup({ renderMath: undefined }).state().renderMath, true);
        assert.strictEqual(setup({ renderMath: "off" }).state().renderMath, false);
    }

    // ── Typesetting: KaTeX loads once, only for math; script and CSS both needed ──
    {
        const t = setup();
        t.typesetMath(t.r([]));
        await t.flush();
        assert.strictEqual(t.appended.length, 0, "a preview without formulas loads nothing");
        const r = t.r([["x^2", false], ["\\frac{1}{2}", true]]);
        t.typesetMath(r);
        await t.flush();
        assert.deepStrictEqual(t.appended.map((e) => e.tag), ["link", "script"]);
        assert.strictEqual(t.appended[0].href, "/static/vendor/katex/katex.min.css");
        assert.strictEqual(t.appended[1].src, "/static/vendor/katex/katex.min.js");
        assert.ok(r.nodes.every((n) => n.classes.has("math-done")));
        assert.strictEqual(r.nodes[0].innerHTML, '[clean]<span class="katex">I:x^2</span>', "output is sanitised");
        assert.strictEqual(r.nodes[1].innerHTML, '[clean]<span class="katex">D:\\frac{1}{2}</span>');
        const opts = t.katexCalls[0].opts;
        assert.deepStrictEqual(
            { trust: opts.trust, throwOnError: opts.throwOnError, maxSize: opts.maxSize, maxExpand: opts.maxExpand },
            { trust: false, throwOnError: true, maxSize: 20, maxExpand: 500 });
        // Second preview: KaTeX is not loaded again, and the cache answers.
        const again = t.r([["x^2", false]]);
        t.typesetMath(again);
        await t.flush();
        assert.strictEqual(t.appended.length, 2, "loaded once per session");
        assert.strictEqual(t.katexCalls.length, 2, "same formula comes from the cache");
        assert.ok(again.nodes[0].classes.has("math-done"));
    }

    // ── Look-alike HTML in a document (class="math-tex" without the marker) is never drawn ──
    {
        const t = setup();
        const r = t.r([["x".repeat(400000), true, false], ["y", false]]);
        t.typesetMath(r);
        await t.flush();
        assert.deepStrictEqual(t.katexCalls.map((c) => c.tex), ["y"]);
        assert.ok(!r.nodes[0].classes.has("math-done") && !r.nodes[0].classes.has("math-error"));
    }

    // ── Too long to draw: checked again before KaTeX sees it ──
    {
        const t = setup();
        const r = t.r([["x".repeat(1001), false], ["y".repeat(5001), true], ["ok", true]]);
        t.typesetMath(r);
        await t.flush();
        assert.deepStrictEqual(t.katexCalls.map((c) => c.tex), ["ok"]);
        assert.ok(r.nodes[0].classes.has("math-error") && r.nodes[1].classes.has("math-error"));
        assert.strictEqual(r.nodes[0].title, "Too long to render as a formula");
    }

    // ── KaTeX's output is sanitised with <annotation> text dropped ──
    {
        const t = setup();
        t.typesetMath(t.r([["x", false]]));
        await t.flush();
        assert.deepStrictEqual(t.sanitizeCalls, [{ ADD_FORBID_CONTENTS: ["annotation"] }]);
    }

    // ── Script ran but window.katex wasn't set (shadowed globals): the next document retries ──
    {
        let first = true;
        const t = setup({ registers: () => { const ok = !first; first = false; return ok; } });
        const a = t.r([["x", false]]);
        t.typesetMath(a);
        await t.flush();
        assert.ok(!a.nodes[0].classes.has("math-done"));
        const b = t.r([["y", false]]);
        t.typesetMath(b);
        await t.flush();
        assert.ok(b.nodes[0].classes.has("math-done"), "loaded again for the next document");
        assert.strictEqual(t.appended.filter((e) => e.tag === "script").length, 2);
    }

    // ── A formula KaTeX rejects keeps its LaTeX, marked and explained ──
    {
        const t = setup();
        const r = t.r([["\\bad{x}", false], ["y", false]]);
        t.typesetMath(r);
        await t.flush();
        assert.ok(r.nodes[0].classes.has("math-error") && !r.nodes[0].classes.has("math-done"));
        assert.strictEqual(r.nodes[0].title, "Couldn't render this formula");
        assert.strictEqual(r.nodes[0].innerHTML, undefined, "the LaTeX text is left as it was");
        assert.ok(r.nodes[1].classes.has("math-done"));
    }

    // ── KaTeX's script or stylesheet missing: formulas stay as LaTeX, one warning ──
    for (const opt of [{ katexOk: false }, { cssOk: false }]) {
        const t = setup(opt);
        const r = t.r([["x", false]]);
        t.typesetMath(r);
        await t.flush();
        t.typesetMath(t.r([["y", false]]));
        await t.flush();
        assert.ok(!r.nodes[0].classes.has("math-done"));
        assert.strictEqual(t.warnings.length, 1, "warned once, not for every document");
        assert.strictEqual(t.katexCalls.length, 0);
    }

    // ── No sanitiser: never insert KaTeX output ──
    {
        const t = setup({ purify: false });
        const r = t.r([["x", false]]);
        t.typesetMath(r);
        await t.flush();
        assert.strictEqual(t.appended.length, 0);
        assert.ok(!r.nodes[0].classes.has("math-done"));
    }

    // ── Scheduling: slices of 8 ms, formulas on screen first ──
    {
        const t = setup();
        const r = t.r(Array.from({ length: 12 }, (_, i) => [`f${i}`, false]));
        t.typesetMath(r);
        // Let KaTeX "load" and the first slice be queued.
        for (let i = 0; i < 4 && t.timersPending() === 0; i++) await new Promise((res) => setImmediate(res));
        // The browser reports formulas 9 and 10 on screen before the first slice runs.
        t.io()([{ isIntersecting: true, target: r.nodes[9] }, { isIntersecting: true, target: r.nodes[10] }]);
        await t.stepOnce();
        const order = t.katexCalls.map((c) => c.tex);
        assert.deepStrictEqual(order.slice(0, 2), ["f9", "f10"], "on-screen formulas first");
        assert.strictEqual(order.length, 2, `one slice is about 6 ms of work (${order.length} formulas at 3 ms)`);
        await t.flush();
        assert.strictEqual(t.katexCalls.length, 12, "the rest follow in later slices");
        assert.ok(r.nodes.every((n) => n.classes.has("math-done")));
        assert.strictEqual(t.observed.size, 0, "observer released when done");
    }

    // ── However fast KaTeX is, a slice inserts at most 8 formulas (layout follows) ──
    {
        const t = setup({ msPerFormula: 0 });
        const r = t.r(Array.from({ length: 20 }, (_, i) => [`q${i}`, false]));
        t.typesetMath(r);
        for (let i = 0; i < 4 && t.timersPending() === 0; i++) await new Promise((res) => setImmediate(res));
        await t.stepOnce();
        assert.strictEqual(t.katexCalls.length, 8);
        await t.flush();
        assert.strictEqual(t.katexCalls.length, 20);
    }

    // ── Works without IntersectionObserver too, in document order ──
    {
        const t = setup({ io: false });
        const r = t.r([["a", false], ["b", false], ["c", false]]);
        t.typesetMath(r);
        await t.flush();
        assert.deepStrictEqual(t.katexCalls.map((c) => c.tex), ["a", "b", "c"]);
    }

    // ── A new preview (or cancelMath) stops the old one's drawing ──
    {
        const t = setup();
        const first = t.r(Array.from({ length: 10 }, (_, i) => [`old${i}`, false]));
        t.typesetMath(first);
        for (let i = 0; i < 4 && t.timersPending() === 0; i++) await new Promise((res) => setImmediate(res));
        await t.stepOnce(); // one slice of the old document
        const drawnBefore = t.katexCalls.length;
        t.cancelMath();
        await t.flush();
        assert.strictEqual(t.katexCalls.length, drawnBefore, "nothing more is drawn for a preview that has gone");
        const second = t.r([["new", false]]);
        t.typesetMath(second);
        await t.flush();
        assert.ok(second.nodes[0].classes.has("math-done"));
        assert.ok(!t.katexCalls.some((c, i) => i >= drawnBefore && c.tex.startsWith("old")));
    }

    // ── Switch: applies at once and redraws the open document ──
    {
        const t = setup();
        t.applyRenderMath(false);
        assert.strictEqual(t.root.dataset.renderMath, "off");
        assert.deepStrictEqual(t.selected, ["item-1"], "the open document is drawn again");
        t.applyRenderMath(true);
        assert.strictEqual(t.root.dataset.renderMath, "on");
        t.syncRenderMath(true);
        assert.strictEqual(t.selected.length, 2, "no redraw when nothing changed");
        t.syncRenderMath(false);
        assert.strictEqual(t.state().renderMath, false);
        t.syncRenderMath("anything else");
        assert.strictEqual(t.state().renderMath, true, "only false turns it off");
    }

    // ── Wiring in app.js ──
    {
        const render = source.indexOf("      const rawHtml = markdownToHtml(md);");
        const wrap = source.indexOf("    wrapTables(renderedOutput);", render);
        const highlight = source.indexOf("    highlightCodeBlocks(renderedOutput);", render);
        const typeset = source.indexOf("    typesetMath(renderedOutput);", render);
        assert.ok(render > 0 && wrap > render && highlight > wrap && typeset > highlight, "parse, sanitise, wrap tables, highlight, then math");
        assert.ok(!/window\.marked\.parse\(md\)/.test(source.slice(source.indexOf("  function selectItem(id) {"))), "the preview only parses through markdownToHtml");
        assert.ok(/function selectItem\(id\) \{\s*cancelMath\(\);/.test(source), "a new preview stops the old one's drawing");
        const loadStart = source.indexOf("  async function loadSettings() {");
        const loadEnd = source.indexOf("renderSaveFolder(s.save_folder);", loadStart);
        assert.ok(source.slice(loadStart, loadEnd).includes("if (s.render_math !== undefined) syncRenderMath(s.render_math);"));
    }

    // ── Markup: Add-ons card with the switch; KaTeX is never in the page up front ──
    {
        const pane = html.slice(html.indexOf('id="settingsPaneAddons"'), html.indexOf('id="settingsPaneUpdates"'));
        assert.ok(pane.includes('id="mathToggle" checked'), "switch present, on by default");
        assert.ok(pane.includes("Built in · KaTeX"));
        assert.ok(pane.includes('aria-labelledby="mathAddonTitle"'));
        assert.ok(!/katex/i.test(html.replace(pane, "").replace(/<!--[\s\S]*?-->/g, "")), "no KaTeX script or stylesheet outside the lazy loader");
        for (const f of ["app/ui/vendor/katex/katex.min.js", "app/ui/vendor/katex/katex.min.css"]) assert.ok(fs.existsSync(f), f);
    }

    console.log("math render UI tests passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
