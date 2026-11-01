// Tests the support links: in the app (the heart at the foot of the Settings
// sidebar, and About's "Buy me a coffee"), the website's /support/ page they
// open, and the landing page's own links (the heart in the header, and the
// footer's link).
//
// The app links to the website's /support/ page, never to the platform: a
// released app can't be changed, the page can. The page forwards at once to
// Buy Me a Coffee. The landing page links the platform directly, since it can
// be changed any time. Both places use one heart (an outline, never filled).
// In the desktop app a click opens the fixed address in the system browser,
// whatever href the link carries; in a browser tab the link opens a new tab.
//
// Follows preview_links_ui.test.js: slice the block out of the source, run it
// against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const URL = "https://abdoslamb.github.io/InkDoc/support/";
const PLATFORM = "https://buymeacoffee.com/abdoslamb";
const source = fs.readFileSync("app/ui/app.js", "utf8");
const html = fs.readFileSync("app/ui/index.html", "utf8");
const css = fs.readFileSync("app/ui/style.css", "utf8");
const site = fs.readFileSync("docs/index.html", "utf8");
const page = fs.readFileSync("docs/support/index.html", "utf8");

const anchors = (text, mark) => text.match(new RegExp(`<a\\b[^>]*${mark}[^>]*>`, "g")) || [];
const href = (tag) => (tag.match(/href=["']([^"']*)["']/) || [])[1];

// ─── App: markup ───────────────────────────────────────────────────────────
{
    const links = anchors(html, "data-support-link");
    assert.strictEqual(links.length, 2, "the sidebar heart and About's link");
    assert.ok(!html.includes("buymeacoffee") && !source.includes("buymeacoffee"), "the app never names the platform's address");
    links.forEach((tag) => {
        assert.strictEqual(href(tag), URL);
        assert.ok(tag.includes('target="_blank"') && tag.includes('rel="noopener noreferrer"'), tag);
        assert.ok(tag.includes("data-support-link"), "the click handler finds it");
        assert.ok(!tag.includes("data-settings-section"), "not a Settings section");
    });

    // The heart is drawn once and used by both links; it has no fill.
    const symbol = html.match(/<symbol id="iconHeart"[\s\S]*?<\/symbol>/);
    assert.ok(symbol, "one heart symbol");
    assert.ok(symbol[0].includes('fill="none"') && symbol[0].includes('stroke="currentColor"'));
    assert.strictEqual((html.match(/<use href="#iconHeart">/g) || []).length, 2);

    // The sidebar heart sits after the section list, outside it, with no text
    // but a label and a tooltip.
    const nav = html.indexOf('<nav class="settings-nav"');
    const navEnd = html.indexOf("</nav>", nav);
    const side = html.indexOf('id="settingsSupportLink"');
    assert.ok(nav > 0 && side > navEnd, "after </nav>");
    const sideTag = html.slice(html.lastIndexOf("<a", side), html.indexOf("</a>", side));
    assert.ok(sideTag.includes('aria-label="Support InkDoc"') && sideTag.includes('title="Support InkDoc"'));
    assert.strictEqual(sideTag.replace(/<[^>]*>/g, "").trim(), "", "icon only");

    // About: the line, then the same heart and the words.
    const about = html.slice(html.indexOf('id="settingsPaneAbout"'), html.indexOf("</section>", html.indexOf('id="settingsPaneAbout"')));
    assert.ok(/Built with love by Abdoslam Baabbad ·\s*<a[^>]*id="aboutSupportLink"[^>]*><svg[^>]*><use href="#iconHeart"><\/use><\/svg>Buy me a coffee<\/a>/.test(about));
}

// ─── App: the heart is hidden on About only, and is red in both themes ─────
{
    assert.ok(/\.settings-drawer\[data-section="about"\] \.settings-support \{\s*display: none;/.test(css));
    assert.ok(!/data-section="updates"/.test(css), "Updates keeps the heart");
    const themes = css.match(/\[data-theme="(dark|light)"\] \{[\s\S]*?\n\}/g);
    assert.strictEqual(themes.length, 2);
    themes.forEach((block) => assert.ok(/--heart: #[0-9a-f]{6};/.test(block), "both themes define --heart"));
}

// ─── App: clicks ───────────────────────────────────────────────────────────
const start = source.indexOf("  // The support links (Settings sidebar heart");
const end = source.indexOf("  // ─── End external links", start);
if (start < 0 || end < 0) throw new Error("could not locate the support links block in app.js");
const block = source.slice(start, end);
assert.ok(block.includes(`const SUPPORT_URL = "${URL}";`), "the app's address matches the markup");

function setup({ desktop }) {
    const opened = [];
    const links = [0, 1].map(() => {
        const listeners = {};
        return {
            href: URL,
            addEventListener: (type, fn) => { listeners[type] = fn; },
            click() {
                let prevented = false;
                listeners.click({ preventDefault: () => { prevented = true; } });
                return prevented;
            },
        };
    });
    const window = desktop ? { pywebview: { api: {} } } : {};
    const document = { querySelectorAll: (sel) => (sel === "[data-support-link]" ? links : []) };
    const withWindowApi = (fn) => fn({ open_external_link: (url) => { opened.push(url); return Promise.resolve(true); } });
    new Function("window", "document", "withWindowApi", block)(window, document, withWindowApi);
    return { links, opened };
}

// Desktop: the system browser opens the fixed address, even if the link's
// href was changed; the window itself never navigates.
{
    const t = setup({ desktop: true });
    t.links[1].href = "https://example.com/elsewhere";
    t.links.forEach((link) => assert.strictEqual(link.click(), true, "the window stays on InkDoc"));
    assert.deepStrictEqual(t.opened, [URL, URL]);
}

// Browser tab (Web Bench): the link's own target="_blank" does the work.
{
    const t = setup({ desktop: false });
    t.links.forEach((link) => assert.strictEqual(link.click(), false));
    assert.deepStrictEqual(t.opened, []);
}

// ─── Website: /support/ forwards at once ───────────────────────────────────
{
    // GitHub Pages serves docs/ at /InkDoc/; the trailing slash saves a hop.
    assert.ok(URL.startsWith("https://abdoslamb.github.io/InkDoc/") && URL.endsWith("/support/"));
    // Forwarded before anything is drawn: the refresh (no delay) and the
    // script both sit in <head>; the body's link is for anyone with neither.
    const head = page.slice(0, page.indexOf("</head>"));
    assert.ok(head.includes(`<meta http-equiv="refresh" content="0; url=${PLATFORM}" />`));
    assert.ok(head.includes(`<script>location.replace("${PLATFORM}");</script>`));
    const body = page.slice(page.indexOf("<body>"));
    assert.deepStrictEqual(anchors(body, "href").map(href), [PLATFORM]);
    // One destination, kept out of search results and the sitemap.
    assert.deepStrictEqual([...new Set(page.match(/https:\/\/[^"'<)\s]+/g))], [PLATFORM]);
    assert.ok(head.includes('<meta name="robots" content="noindex" />'));
    assert.ok(!fs.readFileSync("docs/sitemap.xml", "utf8").includes("/support"));
    // Nothing else runs or loads: one inline script, no external files.
    assert.strictEqual((page.match(/<script\b/g) || []).length, 1);
    assert.ok(!/\bsrc=/.test(page) && !/stylesheet/.test(page));
}

// ─── Landing page ──────────────────────────────────────────────────────────
{
    // One address, in the header and the footer, both opening a new tab.
    const links = anchors(site, "buymeacoffee");
    assert.strictEqual(links.length, 2, "the header and the footer");
    links.forEach((tag) => {
        assert.strictEqual(href(tag), PLATFORM);
        assert.ok(tag.includes('target="_blank"') && tag.includes('rel="noopener noreferrer"'), tag);
    });
    assert.strictEqual((site.match(/buymeacoffee\.com/g) || []).length, 2, "no other copy of the address");

    // The header heart: between the theme button and the language menu, icon
    // only, labelled and translated.
    const theme = site.indexOf('id="themeBtn"');
    const heart = site.indexOf('class="tool icon support"');
    const lang = site.indexOf('<div class="lang-menu">');
    assert.ok(theme > 0 && theme < heart && heart < lang);
    const tag = site.slice(heart, site.indexOf("</a>", heart));
    assert.ok(tag.includes('data-i18n-aria="support.aria"') && tag.includes('data-i18n-title="support.aria"'));
    assert.ok(tag.includes('<use href="#i-heart" />'));
    assert.strictEqual(tag.slice(tag.indexOf(">") + 1).replace(/<[^>]*>/g, "").trim(), "", "icon only");
    assert.ok(site.includes('$$("[data-i18n-title]")'), "tooltips are translated too");

    const symbol = site.match(/<symbol id="i-heart"[\s\S]*?<\/symbol>/);
    assert.ok(symbol && symbol[0].includes('fill="none"'), "an outline heart");

    // The footer is one line: who made it and the license, then three links
    // (the same heart with "Buy me a coffee", GitHub, Privacy); the disclaimer
    // is the small print under it, and links the third-party licenses.
    const footer = site.slice(site.indexOf("<footer>"), site.indexOf("</footer>"));
    const keys = (footer.match(/data-i18n[-a-z]*="[^"]+"/g) || []).map((k) => k.split('"')[1]);
    assert.deepStrictEqual(keys, ["foot.by", "foot.license", "foot.coffee", "foot.privacy", "foot.disclaimer"]);
    const items = footer.match(/<li>[\s\S]*?<\/li>/g);
    assert.strictEqual(items.length, 3);
    assert.ok(items[0].includes('class="support"') && items[0].includes('<use href="#i-heart" />'));
    assert.ok(items[1].includes(">GitHub</a>") && items[2].includes('data-i18n="foot.privacy"'));
    assert.ok(/<p class="fine" data-i18n-html="foot.disclaimer">[\s\S]*THIRD_PARTY_NOTICES\.md[\s\S]*<\/p>\s*<\/div>\s*$/.test(footer),
        "the disclaimer is last");
    assert.ok(/\.fine \{[^}]*color: var\(--faint\);[^}]*font-size: \.74rem;/.test(site));

    // Every language has the label, the footer's words, and a disclaimer that
    // links the third-party licenses; nothing is left of the old footer.
    const langs = (site.match(/\{ code: "([a-z]+)"/g) || []).length;
    assert.strictEqual(langs, 9);
    for (const key of ["support.aria", "foot.coffee", "foot.privacy", "foot.license"]) {
        assert.strictEqual(site.split(`"${key}": "`).length - 1, langs, key);
    }
    assert.strictEqual((site.match(/"foot\.by": [^\n]*\bME\b/g) || []).length, langs);
    assert.strictEqual((site.match(/"foot\.disclaimer": [^\n]*a\(NOTICES, "[^"]+"\)/g) || []).length, langs);
    for (const gone of ["foot.tag", "foot.releases", "foot.notices", "COFFEE"]) assert.ok(!site.includes(gone), gone);
}

console.log("support link UI tests passed");
