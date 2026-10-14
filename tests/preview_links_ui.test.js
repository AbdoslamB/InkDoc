// Tests link handling in converted documents and release notes (app/ui/app.js).
//
// In the desktop app a link must never load another page inside InkDoc's own
// window: web links open in the system browser, other links do nothing, and
// links to a heading in the same document still scroll. In a browser tab
// (Web Bench) nothing changes. Release notes are never inserted as HTML when
// the sanitiser is missing.
//
// Follows save_folder_ui.test.js: slice the blocks out of the source, run
// them against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");

function slice(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    if (start < 0 || end < 0) throw new Error(`could not locate ${JSON.stringify(startMarker)} in app.js`);
    return source.slice(start, end);
}

// ─── Content links ───────────────────────────────────────────────────────────

const linkBlock = slice("  // ─── Links in converted documents and release notes", "  // ─── End content links");

function container() {
    return { listener: null, addEventListener(type, fn) { if (type === "click") this.listener = fn; } };
}

function setupLinks({ desktop }) {
    const opened = [];
    const api = { open_external_link: (url) => { opened.push(url); return Promise.resolve(true); } };
    const renderedOutput = container();
    const updateNotesContent = container();
    new Function("env", `
        const { window, withWindowApi, renderedOutput, updateNotesContent } = env;
        ${linkBlock}
    `)({
        window: desktop ? { pywebview: { api } } : {},
        withWindowApi: (fn) => fn(api),
        renderedOutput,
        updateNotesContent,
    });
    return { opened, renderedOutput, updateNotesContent };
}

function click(target, href) {
    const link = href === null ? null : { getAttribute: (n) => (n === "href" ? href : null) };
    const event = {
        defaultPrevented: false,
        target: { closest: (sel) => (sel === "a[href]" ? link : null) },
        preventDefault() { this.defaultPrevented = true; },
    };
    target.listener(event);
    return event;
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
    // Desktop: web links go to the system browser, never into the window.
    for (const where of ["renderedOutput", "updateNotesContent"]) {
        const t = setupLinks({ desktop: true });
        for (const href of ["https://example.com/page", "http://example.org", "HTTPS://EXAMPLE.COM"]) {
            const e = click(t[where], href);
            assert.ok(e.defaultPrevented, `${href} must not load in the window (${where})`);
        }
        await settle();
        assert.deepStrictEqual(t.opened, ["https://example.com/page", "http://example.org", "HTTPS://EXAMPLE.COM"]);
    }

    // Desktop: any other link is stopped and not opened anywhere.
    {
        const t = setupLinks({ desktop: true });
        for (const href of ["page2.html", "/InkDoc/other", "file:///C:/x.txt", "javascript:void(0)", "mailto:a@b.c", "//evil.example/x"]) {
            assert.ok(click(t.renderedOutput, href).defaultPrevented, `${href} must not replace the app`);
        }
        await settle();
        assert.deepStrictEqual(t.opened, []);
    }

    // Desktop: links to a heading in the same document still scroll.
    {
        const t = setupLinks({ desktop: true });
        assert.ok(!click(t.renderedOutput, "#section-2").defaultPrevented);
        assert.ok(!click(t.renderedOutput, null).defaultPrevented, "clicks outside links are untouched");
        await settle();
        assert.deepStrictEqual(t.opened, []);
    }

    // Web Bench: nothing is intercepted, so the browser behaves as before.
    {
        const t = setupLinks({ desktop: false });
        assert.ok(!click(t.renderedOutput, "https://example.com").defaultPrevented);
        assert.ok(!click(t.updateNotesContent, "page2.html").defaultPrevented);
        await settle();
        assert.deepStrictEqual(t.opened, []);
    }

    // ─── Release notes ──────────────────────────────────────────────────────

    const notesStart = "      if (s.release_notes && s.release_notes.trim()) {";
    const notesBlock = slice(notesStart, "      } else if (updateNotesAccordion) {") + "      }\n";
    const NOTES = "## v1.2.0\n\n<img src=x onerror=alert(1)> Save folder setting";

    function renderNotes({ purify }) {
        const content = { innerHTML: "", textContent: "" };
        const accordion = { style: { display: "none" } };
        new Function("env", `
            const { window, marked, s, updateNotesContent, updateNotesAccordion } = env;
            ${notesBlock}
        `)({
            window: purify ? { DOMPurify: { sanitize: (html) => `[clean]${html}` } } : {},
            marked: { parse: (md) => `<p>${md}</p>` },
            s: { release_notes: NOTES },
            updateNotesContent: content,
            updateNotesAccordion: accordion,
        });
        return { content, accordion };
    }

    {
        const { content, accordion } = renderNotes({ purify: false });
        assert.strictEqual(content.innerHTML, "", "without DOMPurify nothing is inserted as HTML");
        assert.strictEqual(content.textContent, NOTES, "the notes are shown as text instead");
        assert.strictEqual(accordion.style.display, "block");
    }
    {
        const { content } = renderNotes({ purify: true });
        assert.strictEqual(content.innerHTML, `[clean]<p>${NOTES}</p>`, "with DOMPurify the sanitised HTML is used");
    }

    console.log("[OK] preview_links_ui.test.js passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
