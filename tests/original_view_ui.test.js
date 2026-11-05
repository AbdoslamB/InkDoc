// Tests the Original view in app/ui/app.js: the source document beside the
// Rendered or Raw view.
//
// Nothing is loaded while the switch is off. An image is shown from the file
// the page holds. A PDF is uploaded once (POST /preview/pdf), its pages are
// laid out at their own proportions and drawn only as they come near the
// visible part, two at a time, and let go when far away; a PDF the server no
// longer keeps is uploaded again once. Clear lets the server delete its copies.
// No frame, object or embed anywhere: the page's policy stays as it is.
//
// Follows notice_dismiss_ui.test.js: slice the code out of the source and run
// it against a few stand-in elements, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const read = (path) => fs.readFileSync(path, "utf8").replace(/\r\n/g, "\n");
const source = read("app/ui/app.js");
const markup = read("app/ui/index.html");
const css = read("app/ui/style.css");

const start = source.indexOf("  // ─── Original view");
const end = source.indexOf("  // ─── End Original view", start);
assert.ok(start >= 0 && end > start, "could not locate the Original view in app.js");
const viewCode = source.slice(start, end);

// ─── The markup and its style ─────────────────────────────────────────────
const header = markup.slice(markup.indexOf('class="preview-header"'), markup.indexOf('class="preview-meta"'));
const toggle = (header.match(/<button[^>]*id="btnSplitView"[^>]*>/) || [""])[0];
assert.ok(toggle, "the Original switch isn't in the preview header");
assert.ok(/type="button"/.test(toggle) && /aria-pressed="false"/.test(toggle), "the switch isn't an off toggle button");
const paneTag = (markup.match(/<div[^>]*id="sourcePane"[^>]*>/) || [""])[0];
assert.ok(/\shidden[\s>]/.test(paneTag),"the source pane shows before the switch is on");
assert.ok(/role="region"/.test(paneTag) && /aria-label="Original document"/.test(paneTag), "the source pane has no name");
assert.ok(/tabindex="0"/.test(paneTag), "the source pane can't be scrolled from the keyboard");
assert.ok(markup.indexOf('id="sourcePane"') < markup.indexOf('id="renderedContainer"'), "the original isn't on the left");
for (const tag of ["iframe", "object", "embed"]) {
    assert.ok(!new RegExp(`<${tag}[\\s>]`, "i").test(markup), `index.html has an <${tag}>`);
    assert.ok(!source.includes(`createElement("${tag}")`), `app.js makes an <${tag}>`);
}
assert.ok(/\.source-pane\[hidden\]\s*\{\s*display:\s*none/.test(css), "a hidden source pane still takes space");
assert.ok(/\.split-toggle:focus-visible/.test(css), "the switch has no keyboard focus ring");
assert.ok(/\.preview-panes\.is-split\s*\{\s*flex-direction:\s*column/.test(css), "narrow windows don't stack the panes");
assert.ok(/showSourceFor\(item\);/.test(source.slice(source.indexOf("  function selectItem(id) {"))), "selecting an item doesn't update the original");
assert.ok(/releaseSourcePdfs\(queue\);\s*closeSource\(\);\s*queue = \[\];/.test(source), "Clear doesn't release the kept PDFs");

// ─── Stand-ins ────────────────────────────────────────────────────────────
class ClassList {
    constructor() { this.set = new Set(); }
    add(c) { this.set.add(c); }
    remove(c) { this.set.delete(c); }
    contains(c) { return this.set.has(c); }
    toggle(c, on) { if (on === undefined ? !this.set.has(c) : on) this.set.add(c); else this.set.delete(c); }
}
class FakeElement {
    constructor(tag) {
        this.tagName = tag;
        this.children = [];
        this.parent = null;
        this.classList = new ClassList();
        this.dataset = {};
        this.style = {};
        this.attrs = {};
        this.listeners = {};
        this.hidden = false;
        this.textContent = "";
        this.scrollTop = 0;
        this.clientWidth = 500;
    }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return this.attrs[k]; }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    dispatch(type) { (this.listeners[type] || []).forEach((fn) => fn({})); }
    appendChild(child) { child.parent = this; this.children.push(child); return child; }
    replaceChildren(...nodes) { this.children.forEach((c) => { c.parent = null; }); this.children = []; nodes.forEach((n) => this.appendChild(n)); }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }
    querySelector(sel) { return this.children.find((c) => c.tagName === sel) || null; }
    click() { this.dispatch("click"); }
}
const elements = {
    btnSplitView: new FakeElement("button"),
    previewPanes: new FakeElement("div"),
    sourcePane: new FakeElement("div"),
};
elements.sourcePane.hidden = true;
const fakeDocument = {
    getElementById: (id) => elements[id] || null,
    createElement: (tag) => new FakeElement(tag),
};

let blobs = 0;
const live = new Set();
const fakeURL = {
    createObjectURL: () => { const url = `blob:${++blobs}`; live.add(url); return url; },
    revokeObjectURL: (url) => { live.delete(url); },
};

let observer = null;
class FakeObserver {
    constructor(cb, opts) { this.cb = cb; this.opts = opts; this.targets = []; observer = this; }
    observe(t) { this.targets.push(t); }
    disconnect() { this.targets = []; this.disconnected = true; }
    show(pages, on = true) { this.cb(pages.map((target) => ({ target, isIntersecting: on }))); }
}

// The server: requests are recorded and answered when the test says so.
const requests = [];
let keptId = "p1";
function fakeFetch(url, opts = {}) {
    return new Promise((resolve, reject) => {
        const req = { url, method: opts.method || "GET", opts, resolve, reject };
        requests.push(req);
        if (opts.signal) opts.signal.addEventListener("abort", () => {
            req.done = true;
            reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
        });
    });
}
const respond = (req, status, body) => req.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    blob: async () => ({ size: 1 }),
});
const flush = () => new Promise((r) => setImmediate(r));
const pending = (pred) => requests.filter((r) => !r.done && pred(r));
function answer(req, status, body) { req.done = true; respond(req, status, body); }

class FakeFormData { constructor() { this.parts = []; } append(...a) { this.parts.push(a); } }
const pagehide = [];
const fakeWindow = { devicePixelRatio: 2, addEventListener: (type, fn) => { if (type === "pagehide") pagehide.push(fn); } };

const ui = new Function(
    "document", "window", "URL", "IntersectionObserver", "FormData", "AbortController", "fetchWithToken", "apiBase",
    `
    let activeItemId = null;
    let queue = [];
${viewCode}
    return {
        setSplitView, showSourceFor, closeSource, releaseSourcePdfs, sourceKind,
        select: (item) => { activeItemId = item ? item.id : null; if (item) showSourceFor(item); },
        setQueue: (q) => { queue = q; },
        isOn: () => splitOn,
    };
`)(fakeDocument, fakeWindow, fakeURL, FakeObserver, FakeFormData, AbortController, fakeFetch, "/InkDoc");

const pane = elements.sourcePane;
const pdf = { id: "item-pdf", name: "report.pdf", fileRef: { name: "report.pdf", type: "application/pdf" } };
const png = { id: "item-png", name: "scan.PNG", fileRef: { name: "scan.PNG", type: "image/png" } };
const docx = { id: "item-docx", name: "a.docx", fileRef: { name: "a.docx", type: "" } };
const web = { id: "item-url", name: "example.com", sourceUrl: "https://example.com/" };

(async () => {
    // Which files have an original to show.
    assert.strictEqual(ui.sourceKind(pdf), "pdf");
    assert.strictEqual(ui.sourceKind({ name: "x", fileRef: { name: "x", type: "application/pdf" } }), "pdf");
    assert.strictEqual(ui.sourceKind(png), "image");
    assert.strictEqual(ui.sourceKind(docx), null);
    assert.strictEqual(ui.sourceKind({ name: "a.tiff", fileRef: { name: "a.tiff", type: "image/tiff" } }), null);
    assert.strictEqual(ui.sourceKind(web), null);

    // Off: selecting items loads nothing.
    ui.setQueue([pdf, png, docx, web]);
    ui.select(pdf);
    ui.select(png);
    await flush();
    assert.strictEqual(requests.length, 0, "the switch is off but something was fetched");
    assert.strictEqual(blobs, 0, "the switch is off but a blob URL was made");
    assert.strictEqual(pane.hidden, true);

    // On with an image: shown from the file the page holds, nothing uploaded.
    elements.btnSplitView.click();
    assert.ok(ui.isOn());
    assert.strictEqual(elements.btnSplitView.getAttribute("aria-pressed"), "true");
    assert.ok(elements.previewPanes.classList.contains("is-split"));
    assert.strictEqual(pane.hidden, false);
    const img = pane.children[0];
    assert.strictEqual(img.tagName, "img");
    assert.strictEqual(img.className, "source-image");
    assert.ok(live.has(img.src), "the image has no blob URL");
    assert.strictEqual(requests.length, 0, "an image was uploaded");

    // Selecting the same item again (a conversion finishing) keeps what is shown.
    ui.select(png);
    assert.strictEqual(pane.children[0], img);

    // Files without an original, and web addresses, say why.
    ui.select(docx);
    assert.strictEqual(live.size, 0, "the image's blob URL outlived it");
    assert.ok(/PDFs and images/.test(pane.children[0].textContent));
    ui.select(web);
    assert.ok(/web address/.test(pane.children[0].textContent));

    // A PDF: uploaded once, pages laid out at their proportions.
    ui.select(pdf);
    assert.ok(/Opening/.test(pane.children[0].textContent));
    const upload = pending((r) => r.method === "POST")[0];
    assert.strictEqual(upload.url, "/InkDoc/preview/pdf");
    answer(upload, 200, { id: keptId, pages: [[612, 792], [792, 612], [612, 792], [612, 792]] });
    await flush();
    const pages = pane.children;
    assert.strictEqual(pages.length, 4);
    assert.strictEqual(pages[1].style.aspectRatio, "792 / 612");
    assert.strictEqual(pages[0].dataset.page, "1");
    assert.strictEqual(observer.opts.root, pane);
    assert.strictEqual(pending((r) => r.method === "GET").length, 0, "pages were drawn before they came into view");

    // Three pages come into view: two are asked for, the third waits.
    observer.show([pages[0], pages[1], pages[2]]);
    await flush();
    let gets = pending((r) => r.method === "GET");
    assert.strictEqual(gets.length, 2, "more than two pages were asked for at once");
    assert.strictEqual(gets[0].url, "/InkDoc/preview/pdf/p1/page/1?width=1000");
    answer(gets[0], 200);
    await flush(); await flush();
    assert.strictEqual(pages[0].children[0].tagName, "img");
    assert.strictEqual(pages[0].children[0].alt, "Page 1");
    gets = pending((r) => r.method === "GET");
    assert.deepStrictEqual(gets.map((r) => r.url.match(/page\/(\d)/)[1]), ["2", "3"], "the waiting page wasn't asked for next");

    // A page that leaves stops loading; a drawn page that leaves is let go.
    observer.show([pages[2], pages[0]], false);
    await flush();
    assert.ok(gets[1].opts.signal.aborted, "a page scrolled past is still loading");
    assert.strictEqual(pages[0].children.length, 0, "a page far away is still held");
    assert.strictEqual(live.size, 0, "a let-go page's blob URL wasn't revoked");

    // The server no longer keeps the PDF: it is uploaded again, once.
    answer(gets[0], 404, { detail: "gone" });
    await flush();
    const again = pending((r) => r.method === "POST");
    assert.strictEqual(again.length, 1, "the PDF wasn't uploaded again");
    answer(again[0], 200, { id: "p2", pages: [[612, 792], [792, 612], [612, 792], [612, 792]] });
    await flush(); await flush();
    const retry = pending((r) => r.method === "GET");
    assert.strictEqual(retry.length, 1);
    assert.ok(retry[0].url.startsWith("/InkDoc/preview/pdf/p2/page/2"), "the retry didn't use the new copy");
    answer(retry[0], 200);
    await flush(); await flush();
    assert.strictEqual(pages[1].children[0].tagName, "img");

    // A page the server can't draw says so.
    observer.show([pages[3]]);
    await flush();
    answer(pending((r) => r.method === "GET")[0], 422, {});
    await flush(); await flush();
    assert.ok(pages[3].classList.contains("is-failed"));

    // Switching off lets everything go; on again reuses the kept copy.
    elements.btnSplitView.click();
    assert.strictEqual(pane.hidden, true);
    assert.ok(observer.disconnected);
    assert.strictEqual(live.size, 0, "blob URLs outlived the view");
    elements.btnSplitView.click();
    await flush();
    assert.strictEqual(pending((r) => r.method === "POST").length, 0, "the PDF was uploaded again for nothing");
    assert.strictEqual(pane.children.length, 4);

    // A failed upload says why, and the next look tries again.
    const other = { id: "item-pdf2", name: "b.pdf", fileRef: { name: "b.pdf", type: "application/pdf" } };
    ui.setQueue([pdf, other]);
    ui.select(other);
    answer(pending((r) => r.method === "POST")[0], 422, { detail: "This PDF is password-protected, so its pages can't be shown." });
    await flush(); await flush();
    assert.ok(/password-protected/.test(pane.children[0].textContent));
    ui.select(pdf);
    ui.select(other);
    assert.strictEqual(pending((r) => r.method === "POST").length, 1, "a failed upload isn't tried again");

    // Clear (and the page closing) let the server delete its copies.
    ui.releaseSourcePdfs([pdf, other]);
    const deletes = requests.filter((r) => r.method === "DELETE");
    assert.deepStrictEqual(deletes.map((r) => r.url), ["/InkDoc/preview/pdf/p2"]);
    assert.strictEqual(deletes[0].opts.keepalive, true, "the release doesn't survive the page closing");
    assert.strictEqual(pdf.sourcePdfId, null);
    assert.strictEqual(pagehide.length, 1, "nothing releases the copies when the page closes");

    console.log("[OK] original_view_ui.test.js passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
