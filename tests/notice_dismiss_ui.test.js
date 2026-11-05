// Tests the × on a result's note in app/ui/app.js.
//
// A note above a converted result (a missing-text or table warning, the
// Docling fallback, Auto's tip, tables rebuilt, engine notes) can be closed.
// It stays closed while that result is open, also when it is drawn again (the
// math switch, a click on its row), and comes back when another result is
// opened and this one again, or when it is converted again. A notice with no
// result under it (failed, unsupported, cancelled, empty) has no ×.
//
// Follows large_file_ui.test.js: slice the code out of the source and run it
// against a few stand-in elements, no npm dependency.
const assert = require("assert");
const fs = require("fs");

// Line endings as in the repository: a Windows checkout (core.autocrlf) has CRLF.
const read = (path) => fs.readFileSync(path, "utf8").replace(/\r\n/g, "\n");
const source = read("app/ui/app.js");
const markup = read("app/ui/index.html");
const css = read("app/ui/style.css");

function slice(startMarker, endMarker, from = 0) {
    const start = source.indexOf(startMarker, from);
    const end = source.indexOf(endMarker, start + startMarker.length);
    if (start < 0 || end < 0) throw new Error(`could not locate ${JSON.stringify(startMarker)} in app.js`);
    return source.slice(start, end);
}
const noticeCode = slice("  // ─── Conversion Notice Bar", "  // ─── Engine Management");
const beginStart = source.indexOf("  function beginConversion(item, engine) {");
const beginCode = source.slice(beginStart, source.indexOf("\n  }\n", beginStart) + 4);
const selectCode = slice("  function selectItem(id) {", "  // What Copy and Download take");

// ─── The markup and its style ─────────────────────────────────────────────
const bar = markup.slice(markup.indexOf('id="conversionNoticeBar"'), markup.indexOf('id="renderedContainer"'));
const button = (bar.match(/<button[^>]*id="conversionNoticeClose"[^>]*>/) || [""])[0];
assert.ok(button, "the notice has no close button");
assert.ok(/type="button"/.test(button), "the close button isn't type=button");
assert.ok(/aria-label="Dismiss note"/.test(button), "the close button has no accessible name");
assert.ok(/\shidden[\s>]/.test(button), "the close button shows before any note asks for it");
assert.ok(bar.indexOf("conversionNoticeClose") > bar.indexOf('id="conversionNoticeDetails"'),
    "the close button isn't the last thing in the notice row");

function rule(selector) {
    const at = css.indexOf(`${selector} {`);
    assert.ok(at >= 0, `style.css has no ${selector} rule`);
    return css.slice(at, css.indexOf("}", at));
}
assert.ok(/display:\s*none/.test(rule(".conversion-notice-close[hidden]")), "a hidden × still takes space");
const closeRule = rule(".conversion-notice-close");
assert.ok(/flex-shrink:\s*0/.test(closeRule), "the × can be squeezed by a long note");
assert.ok(/margin:[^;]*\bauto\b/.test(closeRule), "the × isn't pushed to the right edge");
assert.ok(/color:\s*inherit/.test(closeRule), "the × doesn't take the note's colour");
assert.ok(/:focus-visible/.test(css.slice(css.indexOf(".conversion-notice-close"))), "the × has no keyboard focus ring");

// ─── Behaviour ────────────────────────────────────────────────────────────
class FakeElement {
    constructor(id, attrs = {}) {
        this.id = id;
        this.hidden = Boolean(attrs.hidden);
        this.className = attrs.className || "";
        this.textContent = "";
        this.innerHTML = "";
        this.value = "";
        this.children = [];
        this.listeners = {};
        this.attributes = {};
        this.classList = { contains: (c) => this.className.split(/\s+/).includes(c), add() {}, remove() {} };
    }
    get childElementCount() { return this.children.length; }
    setAttribute(name, value) { this.attributes[name] = value; }
    replaceChildren() { this.children = []; }
    appendChild(child) { this.children.push(child); return child; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    click() { (this.listeners.click || []).forEach((fn) => fn({ preventDefault() {} })); }
    focus() { fakeDocument.activeElement = this; }
}

const elements = {
    conversionNoticeBar: new FakeElement("conversionNoticeBar", { className: "conversion-notice" }),
    conversionNoticeIcon: new FakeElement("conversionNoticeIcon"),
    conversionNoticeTitle: new FakeElement("conversionNoticeTitle"),
    conversionNoticeDetail: new FakeElement("conversionNoticeDetail"),
    conversionNoticeActions: new FakeElement("conversionNoticeActions", { hidden: true }),
    conversionNoticeDetails: new FakeElement("conversionNoticeDetails", { hidden: true }),
    conversionNoticeClose: new FakeElement("conversionNoticeClose", { hidden: true }),
};
const fakeDocument = {
    activeElement: null,
    getElementById: (id) => elements[id] || null,
    createElement: (tag) => new FakeElement(tag),
};
const tabRendered = new FakeElement("tabRendered", { className: "tab-btn active" });
const tabRaw = new FakeElement("tabRaw", { className: "tab-btn" });

const noop = () => {};
const stubs = {
    document: fakeDocument,
    window: {},
    tabRendered,
    tabRaw,
    previewFilename: new FakeElement("previewFilename"),
    rawEditor: new FakeElement("rawEditor"),
    previewStats: new FakeElement("previewStats"),
    renderedOutput: new FakeElement("renderedOutput"),
    cancelMath: noop,
    refreshSwitchEngineButton: noop,
    showSourceFor: noop,
    hidePreviewBadges: noop,
    updatePreviewBadges: noop,
    renderQueue: noop,
    wrapTables: noop,
    highlightCodeBlocks: noop,
    typesetMath: noop,
    reconvertItem: noop,
    openSettings: noop,
    startProgressPoll: noop,
    newJobId: () => "job",
    getEngineDisplayName: (engine) => engine,
    selectedEngine: "auto",
    EMPTY_HINT_SVG: "",
    escapeHtml: (s) => s,
    formatQualityWarning: (q) => (q && q.warning) || null,
    qualityNoticeActions: () => [],
    conversionDetailLines: () => [],
    previewSlice: (md) => ({ text: md || "", cut: false }),
    formatSize: (n) => String(n),
    formatTableNote: () => null,
    highDetailOffer: (t) => (t && t.high_detail ? { title: "This table needs high detail", long: "x", highLabel: "h", keepLabel: "k" } : null),
    highDetailActions: () => [],
    visibleHint: (hint) => hint || "",
    prefs: { hidden_engines: [] },
    markdownToHtml: (md) => md,
    renderBasicMarkdown: (md) => md,
};
const names = Object.keys(stubs);
const ui = new Function(...names, `
    let activeItemId = null;
    let queue = [];
    const conversionNoticeBar    = document.getElementById("conversionNoticeBar");
    const conversionNoticeIcon   = document.getElementById("conversionNoticeIcon");
    const conversionNoticeTitle  = document.getElementById("conversionNoticeTitle");
    const conversionNoticeDetail = document.getElementById("conversionNoticeDetail");
${noticeCode}
${beginCode}
${selectCode}
    return { selectItem, beginConversion, setQueue: (q) => { queue = q; } };
`)(...names.map((n) => stubs[n]));

const { conversionNoticeBar: noticeBar, conversionNoticeClose: close, conversionNoticeTitle: title } = elements;
const shown = () => noticeBar.classList.contains("visible");

const noted = { id: "item-a", name: "a.pdf", status: "saved", markdown: "# A", warnings: ["Read with OCR."] };
const fallback = { id: "item-b", name: "b.docx", status: "saved", markdown: "# B", fallbackOccurred: true };
const warned = { id: "item-c", name: "c.pdf", status: "saved", markdown: "# C", quality: { warning: { title: "Some text may be missing", long: "Pages 2-3." } } };
const plain = { id: "item-d", name: "d.txt", status: "saved", markdown: "# D" };
const dense = { id: "item-e", name: "e.png", status: "saved", markdown: "# E", tables: { high_detail: { pages: [1], seconds: 90 } } };
const failures = [
    { id: "item-f1", name: "f.pdf", status: "failed", error: "Broken file.", fileRef: {} },
    { id: "item-f2", name: "f.xyz", status: "unsupported" },
    { id: "item-f3", name: "f.pdf", status: "cancelled", fileRef: {} },
    { id: "item-f4", name: "f.pdf", status: "empty" },
];
ui.setQueue([noted, fallback, warned, plain, dense, ...failures]);

// A table too dense for normal detail: the high-detail offer, a warning that can be closed.
ui.selectItem("item-e");
assert.ok(shown() && title.textContent === "This table needs high detail", "the high-detail offer isn't shown");
assert.ok(noticeBar.className.includes("notice-warning") && close.hidden === false);

// A result's note has the ×.
ui.selectItem("item-a");
assert.ok(shown() && title.textContent === "Note", "the result's note isn't shown");
assert.strictEqual(close.hidden, false, "a result's note has no ×");

// Closing it hides the note and the ×, and keeps keyboard focus in the preview.
fakeDocument.activeElement = close;
close.click();
assert.ok(!shown(), "× didn't close the note");
assert.strictEqual(title.textContent, "");
assert.strictEqual(close.hidden, true, "the × outlived its note");
assert.strictEqual(fakeDocument.activeElement, tabRendered, "focus was lost with the ×");

// Drawn again (the math switch, a click on the same row): still closed.
ui.selectItem("item-a");
assert.ok(!shown(), "a redraw of the same result brought the closed note back");

// Another result: its own note, with its own ×. Back to the first: its note again.
ui.selectItem("item-b");
assert.ok(shown() && /Docling Fallback/.test(title.textContent), "the next result's note didn't show");
assert.strictEqual(close.hidden, false);
ui.selectItem("item-a");
assert.ok(shown() && title.textContent === "Note", "the note didn't come back after another result was opened");
assert.strictEqual(close.hidden, false);

// Warnings close the same way; a mouse click (focus elsewhere) leaves focus alone.
ui.selectItem("item-c");
assert.ok(shown() && title.textContent === "Some text may be missing");
assert.strictEqual(close.hidden, false, "a warning on a result has no ×");
fakeDocument.activeElement = null;
close.click();
assert.ok(!shown());
assert.strictEqual(fakeDocument.activeElement, null, "focus moved though the × didn't have it");

// Converting it again gives a new result, and with it the note.
ui.beginConversion(warned, "docling");
warned.status = "saved";
warned.quality = { warning: { title: "Some text may be missing", long: "Page 4." } };
ui.selectItem("item-c");
assert.ok(shown() && close.hidden === false, "a new result of the same item kept the old note closed");

// A result without a note: no bar, no ×.
ui.selectItem("item-d");
assert.ok(!shown() && close.hidden, "a result without a note shows a bar or an ×");

// No result under it: the notice stays, with no ×, even right after a close.
for (const item of failures) {
    ui.selectItem("item-a");
    close.click();
    ui.selectItem(item.id);
    assert.ok(shown(), `the ${item.status} notice didn't show`);
    assert.strictEqual(close.hidden, true, `the ${item.status} notice can be closed`);
    // Opened again, still not closable.
    ui.selectItem(item.id);
    assert.ok(shown() && close.hidden, `the ${item.status} notice changed on a redraw`);
}

// Only the result branch asks for the ×: every other notice is shown as before.
const resultBranch = selectCode.slice(selectCode.indexOf("    // One notice, most important first"));
const before = selectCode.slice(0, selectCode.indexOf("    // One notice, most important first"));
assert.ok(!/dismissible/.test(before), "a notice without a result asks for the ×");
assert.ok(!/showConversionNotice\(/.test(resultBranch.replace(/: \(type, title, detail, opts\) => showConversionNotice\(/, "")),
    "a result's note is shown without the ×");

console.log("[OK] notice_dismiss_ui.test.js passed");
