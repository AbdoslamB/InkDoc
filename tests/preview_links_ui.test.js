// Tests link handling in converted documents (app/ui/app.js).
//
// In the desktop app a link must never load another page inside InkDoc's own
// window: web links open in the system browser, other links do nothing, and
// links to a heading in the same document still scroll. A middle click is
// handled like a click. In a browser tab (Web Bench) nothing changes. Release
// notes have no links of their own: release_notes_ui.test.js.
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

const linkBlock = slice("  // ─── Links in converted documents", "  // ─── End content links");
assert.ok(!linkBlock.includes("updateNotes"), "release notes are no longer rendered with links");

function container() {
    return { listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; } };
}

function setupLinks({ desktop }) {
    const opened = [];
    const api = { open_external_link: (url) => { opened.push(url); return Promise.resolve(true); } };
    const renderedOutput = container();
    new Function("env", `
        const { window, withWindowApi, renderedOutput } = env;
        ${linkBlock}
    `)({
        window: desktop ? { pywebview: { api } } : {},
        withWindowApi: (fn) => fn(api),
        renderedOutput,
    });
    return { opened, renderedOutput };
}

// href: the link's href (null: no link under the click). xlink: an SVG link's
// xlink:href instead.
function click(target, href, { type = "click", button = 0, xlink = false } = {}) {
    const link = href === null ? null : {
        getAttribute: (n) => (n === "href" && !xlink ? href : null),
        getAttributeNS: (ns, n) => (ns === "http://www.w3.org/1999/xlink" && n === "href" && xlink ? href : null),
    };
    const event = {
        type,
        button,
        defaultPrevented: false,
        target: { closest: (sel) => (sel === "a, area" ? link : null) },
        preventDefault() { this.defaultPrevented = true; },
    };
    target.listeners[type](event);
    return event;
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
    // Desktop: web links go to the system browser, never into the window.
    {
        const t = setupLinks({ desktop: true });
        for (const href of ["https://example.com/page", "http://example.org", "HTTPS://EXAMPLE.COM"]) {
            const e = click(t.renderedOutput, href);
            assert.ok(e.defaultPrevented, `${href} must not load in the window`);
        }
        await settle();
        assert.deepStrictEqual(t.opened, ["https://example.com/page", "http://example.org", "HTTPS://EXAMPLE.COM"]);
    }

    // Desktop: a middle click is handled like a click. Left alone, WebView2
    // would hand the link, whatever its scheme, to the system to open.
    {
        const t = setupLinks({ desktop: true });
        const middle = { type: "auxclick", button: 1 };
        assert.ok(click(t.renderedOutput, "https://example.com/m", middle).defaultPrevented);
        assert.ok(click(t.renderedOutput, "file:///C:/Windows/notepad.exe", middle).defaultPrevented, "a file: link must not be opened");
        assert.ok(click(t.renderedOutput, "#top", middle).defaultPrevented, "a heading link opens nothing either");
        assert.ok(!click(t.renderedOutput, "https://example.com/r", { type: "auxclick", button: 2 }).defaultPrevented, "other buttons are left alone");
        await settle();
        assert.deepStrictEqual(t.opened, ["https://example.com/m"]);
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

    // Desktop: image-map areas and SVG links (xlink:href) are links too.
    {
        const t = setupLinks({ desktop: true });
        assert.ok(click(t.renderedOutput, "https://example.com/svg", { xlink: true }).defaultPrevented);
        assert.ok(click(t.renderedOutput, "mailto:a@b.c", { xlink: true, type: "auxclick", button: 1 }).defaultPrevented);
        assert.ok(!click(t.renderedOutput, "", {}).defaultPrevented, "an <a> without href is no link");
        await settle();
        assert.deepStrictEqual(t.opened, ["https://example.com/svg"]);
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
        assert.ok(!click(t.renderedOutput, "page2.html").defaultPrevented);
        assert.ok(!click(t.renderedOutput, "https://example.com", { type: "auxclick", button: 1 }).defaultPrevented);
        await settle();
        assert.deepStrictEqual(t.opened, []);
    }

    console.log("[OK] preview_links_ui.test.js passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
