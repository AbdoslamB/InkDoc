// Tests the release notes under Settings → Updates (app/ui/app.js, index.html,
// style.css).
//
// The notes are the CHANGELOG's section for the release. The panel shows five
// entries, the first ones of each heading in turn, each cut to its first
// sentence, under their headings, then
// "+ N more changes" and a link to the release page. It is built from text
// nodes only and keeps no link from the notes; the release page opens in the
// system browser in the desktop app. It sits at the bottom of the update card,
// and nothing in it can widen the pane.
//
// Follows preview_links_ui.test.js: slice the block out of the source, run it
// against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const html = fs.readFileSync("app/ui/index.html", "utf8");
const css = fs.readFileSync("app/ui/style.css", "utf8");
const changelog = fs.readFileSync("CHANGELOG.md", "utf8").replace(/\r\n?/g, "\n");

const start = source.indexOf("  // ─── Release notes ───");
const end = source.indexOf("  // ─── End release notes", start);
if (start < 0 || end < 0) throw new Error("could not locate the release notes block in app.js");
const block = source.slice(start, end);

const URL = "https://github.com/AbdoslamB/InkDoc/releases/tag/v1.2.0";

// ─── A small DOM ─────────────────────────────────────────────────────────────
const created = [];
function el(tag) {
    const node = {
        tagName: tag.toUpperCase(),
        children: [],
        className: "",
        hidden: false,
        attrs: {},
        listeners: {},
        _text: "",
        rebuilt: 0,
        appendChild(child) { this.children.push(child); return child; },
        replaceChildren() { this.children = []; this.rebuilt++; },
        setAttribute(n, v) { this.attrs[n] = String(v); },
        getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
        addEventListener(type, fn) { this.listeners[type] = fn; },
        get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); },
        set textContent(v) { this._text = String(v); this.children = []; },
    };
    return node;
}
const document = {
    createElement(tag) { created.push(tag); return el(tag); },
    createTextNode(text) { return { tagName: "#text", textContent: String(text) }; },
};

function setup({ desktop = true } = {}) {
    const opened = [];
    const api = { open_external_link: (url) => { opened.push(url); return Promise.resolve(true); } };
    const ui = {
        updateNotes: el("div"),
        updateNotesTitle: el("h3"),
        updateNotesBody: el("div"),
        updateNotesMore: el("span"),
        updateNotesLink: el("a"),
    };
    ui.updateNotes.hidden = true;
    ui.updateNotesLink.hidden = true;
    ui.updateNotesLink.setAttribute("href", "#");
    const exported = new Function("env", `
        const { window, document, withWindowApi, updateNotes, updateNotesTitle, updateNotesBody,
                updateNotesMore, updateNotesLink } = env;
        ${block}
        return { renderReleaseNotes, releaseHighlights };
    `)({
        window: desktop ? { pywebview: { api } } : {},
        document,
        withWindowApi: (fn) => fn(api),
        ...ui,
    });
    return { ...ui, ...exported, opened };
}

// The rendered body as [{ group, items: [text…] }], and the <li> nodes.
function readBody(body) {
    const groups = [];
    let group = null;
    for (const child of body.children) {
        if (child.className === "update-notes-group") {
            group = { group: child.textContent, items: [] };
            groups.push(group);
        } else if (child.tagName === "UL") {
            if (!group || group.items.length) {
                group = { group: "", items: [] };
                groups.push(group);
            }
            child.children.forEach((li) => group.items.push(li.textContent));
        }
    }
    return groups;
}
const allItems = (body) => readBody(body).flatMap((g) => g.items);

function section(version) {
    const from = changelog.indexOf(`## [${version}]`);
    const body = changelog.slice(changelog.indexOf("\n", from) + 1);
    return body.slice(0, body.search(/^## /m)).trim();
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
    // ─── The CHANGELOG's own section ─────────────────────────────────────────
    {
        const t = setup();
        const notes = section("1.2.0");
        t.renderReleaseNotes({ latest_version: "1.2.0", release_notes: notes, release_url: URL });
        assert.strictEqual(t.updateNotes.hidden, false);
        assert.strictEqual(t.updateNotesTitle.textContent, "What's new in v1.2.0");

        const groups = readBody(t.updateNotesBody);
        const items = groups.flatMap((g) => g.items);
        assert.strictEqual(items.length, 5, "five highlights");
        // Each heading's first entry, then the next round: Changed, Security,
        // Added and Fixed each give one, and Changed a second.
        assert.deepStrictEqual(groups.map((g) => [g.group, g.items.length]),
            [["Changed", 2], ["Security", 1], ["Added", 1], ["Fixed", 1]],
            "under the CHANGELOG's own headings, in its order");
        assert.strictEqual(
            items[0],
            "Settings now opens as a wider panel on the right with five sections: General, Engines, Add-ons, Updates and About.",
            "the first sentence, Markdown markers removed",
        );
        items.forEach((text) => {
            assert.ok(text.length <= 181, `short: ${text}`);
            assert.ok(!/https?:|\*\*|`|\]\(/.test(text), `no links or Markdown left: ${text}`);
        });

        const total = (notes.match(/^- /gm) || []).length;
        assert.strictEqual(t.updateNotesMore.textContent, `+ ${total - 5} more changes`);
        assert.strictEqual(t.updateNotesLink.hidden, false);
        assert.strictEqual(t.updateNotesLink.getAttribute("href"), URL);

        // Bold leads stay bold; code stays code.
        const firstLi = t.updateNotesBody.children.find((c) => c.tagName === "UL").children[0];
        assert.deepStrictEqual(firstLi.children.filter((c) => c.tagName === "STRONG").map((c) => c.textContent),
            ["Settings", "General", "Engines", "Add-ons", "Updates", "About"]);
    }

    // The section of the version being released, if it is there yet.
    // The release this was written for: two additions, two changes, one fix,
    // rather than five additions.
    {
        const t = setup();
        t.renderReleaseNotes({ latest_version: "1.3.0", release_notes: section("1.3.0"), release_url: URL });
        const groups = readBody(t.updateNotesBody);
        assert.deepStrictEqual(groups.map((g) => [g.group, g.items.length]), [["Added", 2], ["Changed", 2], ["Fixed", 1]]);
        assert.ok(groups[0].items[0].startsWith("Auto quality"), groups[0].items[0]);
        assert.ok(groups[1].items[0].startsWith("Less memory while idle. MarkItDown now runs"), "a short lead-in takes the next sentence");
        assert.strictEqual(groups[1].items[1], "Clearer Settings. Shorter descriptions throughout, and the settings you use most come first.");
        assert.strictEqual(groups[0].items[1].split(". ").length, 2, "Table check: its lead-in and one sentence");
        assert.ok(groups[2].items[0].startsWith("The desktop app now remembers"), groups[2].items[0]);
    }

    for (const version of ["1.1.0", "1.0.0"]) {
        const t = setup();
        t.renderReleaseNotes({ latest_version: version, release_notes: section(version), release_url: URL });
        assert.strictEqual(allItems(t.updateNotesBody).length, 5, `${version}: five highlights`);
    }

    // ─── GitHub's generated notes ────────────────────────────────────────────
    {
        const t = setup();
        t.renderReleaseNotes({
            latest_version: "1.2.1",
            release_url: URL,
            release_notes: [
                "## What's Changed",
                "* Fix the save folder on network drives by @someone in https://github.com/AbdoslamB/InkDoc/pull/12",
                "* Faster PDF probe by @other-dev in https://github.com/AbdoslamB/InkDoc/pull/13",
                "",
                "## New Contributors",
                "* @other-dev made their first contribution in https://github.com/AbdoslamB/InkDoc/pull/13",
                "",
                "**Full Changelog**: https://github.com/AbdoslamB/InkDoc/compare/v1.2.0...v1.2.1",
            ].join("\r\n"),
        });
        assert.deepStrictEqual(readBody(t.updateNotesBody), [
            { group: "", items: ["Fix the save folder on network drives", "Faster PDF probe"] },
        ], "titles only: no authors, links, contributors or comparison link");
        assert.strictEqual(t.updateNotesMore.textContent, "");
    }

    // Nothing but the comparison link: say where the highlights are.
    {
        const t = setup();
        t.renderReleaseNotes({
            latest_version: "1.2.0",
            release_url: URL,
            release_notes: "**Full Changelog**: https://github.com/AbdoslamB/InkDoc/compare/v1.1.1...v1.2.0",
        });
        assert.strictEqual(t.updateNotes.hidden, false);
        assert.deepStrictEqual(allItems(t.updateNotesBody), []);
        assert.strictEqual(t.updateNotesBody.children[0].className, "update-notes-empty");
        assert.ok(!t.updateNotesBody.textContent.includes("http"));
        assert.strictEqual(t.updateNotesLink.hidden, false);
    }

    // No notes and no release page: no panel.
    {
        const t = setup();
        t.renderReleaseNotes({ latest_version: "1.2.0", release_notes: "", release_url: null });
        assert.strictEqual(t.updateNotes.hidden, true);
    }

    // ─── Cutting an entry short ──────────────────────────────────────────────
    {
        const t = setup();
        const long = "word ".repeat(80).trim();
        t.renderReleaseNotes({
            latest_version: "9.0.0",
            release_url: URL,
            release_notes: [
                "### Added",
                "- **Save folder** setting (Settings → General): choose where auto-save",
                "  writes, instead of always using Downloads. **Change…** opens your system's",
                "  folder dialog ([#6](https://github.com/AbdoslamB/InkDoc/issues/6)).",
                "- Uses `save_fallback` in API responses, e.g. for a missing drive.",
                `- ${long}`,
            ].join("\n"),
        });
        const items = allItems(t.updateNotesBody);
        assert.strictEqual(items[0], "Save folder setting (Settings → General): choose where auto-save writes, instead of always using Downloads.");
        assert.strictEqual(items[1], "Uses save_fallback in API responses, e.g. for a missing drive.", "no cut after \"e.g.\"");
        assert.ok(items[2].endsWith("…") && items[2].length <= 181 && !items[2].includes("  "), "a long entry ends on a word");
        const codeLi = t.updateNotesBody.children.find((c) => c.tagName === "UL").children[1];
        assert.deepStrictEqual(codeLi.children.filter((c) => c.tagName === "CODE").map((c) => c.textContent), ["save_fallback"]);
    }

    // ─── Nothing in the notes becomes HTML or a link ─────────────────────────
    {
        created.length = 0;
        const t = setup();
        t.renderReleaseNotes({
            latest_version: "9.9.9",
            release_url: URL,
            release_notes: [
                "### <img src=x onerror=alert(1)>Added",
                "- <script>alert(1)</script>**Bold** [click](javascript:alert(1)) <a href=\"file:///C:/x.exe\">here</a> <https://evil.example/>",
                "- ![logo](https://evil.example/x.png) Plain entry.",
            ].join("\n"),
        });
        assert.deepStrictEqual([...new Set(created)].sort(), ["li", "p", "strong", "ul"], "only list, text and emphasis elements");
        const text = t.updateNotesBody.textContent;
        assert.ok(!/[<>]|javascript:|file:|evil|onerror/.test(text), text);
        assert.ok(text.includes("click") && text.includes("here") && text.includes("Plain entry."));
    }

    // ─── The release page link ───────────────────────────────────────────────
    for (const bad of ["javascript:alert(1)", "http://github.com/AbdoslamB/InkDoc", "file:///C:/x", "https://a b", 42, null]) {
        const t = setup();
        t.renderReleaseNotes({ latest_version: "1.2.0", release_notes: "- One.", release_url: bad });
        assert.strictEqual(t.updateNotesLink.hidden, true, `no link for ${bad}`);
        assert.strictEqual(t.updateNotesLink.getAttribute("href"), "#");
    }

    function clickLink(t, type, button) {
        const e = { type, button, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
        t.updateNotesLink.listeners[type](e);
        return e;
    }
    {
        const t = setup({ desktop: true });
        t.renderReleaseNotes({ latest_version: "1.2.0", release_notes: "- One.", release_url: URL });
        assert.ok(clickLink(t, "click", 0).defaultPrevented, "never loads in the window");
        assert.ok(clickLink(t, "auxclick", 1).defaultPrevented, "a middle click neither");
        assert.ok(!clickLink(t, "auxclick", 2).defaultPrevented, "other buttons are left alone");
        await settle();
        assert.deepStrictEqual(t.opened, [URL, URL], "opened in the system browser");
    }
    {
        const t = setup({ desktop: false });
        t.renderReleaseNotes({ latest_version: "1.2.0", release_notes: "- One.", release_url: URL });
        assert.ok(!clickLink(t, "click", 0).defaultPrevented, "Web Bench: the link's target=_blank opens a tab");
        await settle();
        assert.deepStrictEqual(t.opened, []);
    }

    // Polling while downloading re-renders nothing that hasn't changed.
    {
        const t = setup();
        const s = { latest_version: "1.2.0", release_notes: section("1.2.0"), release_url: URL };
        t.renderReleaseNotes(s);
        t.renderReleaseNotes({ ...s, state: "downloading", progress_percent: 40 });
        assert.strictEqual(t.updateNotesBody.rebuilt, 1);
    }

    // ─── Where the panel is shown ────────────────────────────────────────────
    {
        const render = source.slice(source.indexOf("  function renderUpdateStatus(s) {"), source.indexOf("  async function fetchUpdateStatus()"));
        const calls = (state) => {
            const at = render.indexOf(state);
            const next = render.indexOf("return;", at);
            return render.slice(at, next);
        };
        for (const state of ['state === "available"', 'state === "downloading"', 'state === "ready_to_install"', 'state === "ready_to_reveal"']) {
            assert.ok(calls(state).includes("renderReleaseNotes(s);"), `${state} shows the notes`);
        }
        assert.ok(/state === "up_to_date"[\s\S]*?updateNotes\.hidden = true;[\s\S]*?return;/.test(render), "up to date: no notes");
        assert.ok(!/innerHTML|marked|DOMPurify/.test(block), "the notes are never parsed as HTML");
    }

    // ─── Markup and styles ───────────────────────────────────────────────────
    {
        const card = html.slice(html.indexOf('id="updateCard"'), html.indexOf('id="dailyUpdateToggle"'));
        const at = (id) => card.indexOf(`id="${id}"`);
        assert.ok(at("updateNotes") > at("updateActions") && at("updateNotes") > at("updateErrorBox"),
            "the notes come after the buttons and any error, at the bottom of the card");
        assert.ok(at("updateNotes") > card.lastIndexOf('id="updateDetails"'), "outside the details");
        assert.ok(!card.includes("<details"), "no accordion");
        const link = card.match(/<a [^>]*id="updateNotesLink"[^>]*>/)[0];
        assert.ok(link.includes('target="_blank"') && link.includes('rel="noopener noreferrer"') && /\shidden[\s>]/.test(link));
        assert.ok(/<div [^>]*id="updateNotes"[^>]*\shidden[\s>]/.test(card), "hidden until there is an update");

        const rule = (sel) => (css.match(new RegExp(`\\n${sel.replace(/[.[\]]/g, "\\$&")} \\{([^}]*)\\}`)) || [])[1] || "";
        assert.ok(/overflow-wrap: anywhere;/.test(rule(".update-card")), "long words wrap instead of widening the pane");
        assert.ok(/min-width: 0;/.test(rule(".update-card")));
        assert.ok(/display: none;/.test(rule(".update-notes[hidden]")), "hidden wins over display: flex");
        assert.ok(/display: none;/.test(rule(".update-notes-link[hidden]")));
        assert.ok(!/max-height/.test(rule(".update-notes") + rule(".update-notes-list")), "no small scroll box");
        assert.ok(!/update-notes-accordion|update-notes-content/.test(css), "old rules removed");
    }

    console.log("[OK] release_notes_ui.test.js passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
