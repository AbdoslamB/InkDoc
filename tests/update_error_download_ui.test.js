// Tests the "Download from the website" link under an update error (Settings ->
// Updates). When the check or the download fails in a way a download from the
// website gets around (no signed manifest yet, a certificate this computer can't
// verify, a network or verification error), the server sets `offer_download` and
// the link appears; otherwise it stays hidden. In the desktop app a click opens
// the website's download section in the system browser, whatever href the link
// carries; in a browser tab the link opens a new tab.
//
// Follows support_link_ui.test.js: slice the block out of the source, run it
// against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const URL = "https://abdoslamb.github.io/InkDoc/#download";
const source = fs.readFileSync("app/ui/app.js", "utf8");
const html = fs.readFileSync("app/ui/index.html", "utf8");
const css = fs.readFileSync("app/ui/style.css", "utf8");
const site = fs.readFileSync("docs/index.html", "utf8");

// ─── Markup: inside the error box, hidden until the server offers it ─────────
{
    const box = html.slice(html.indexOf('id="updateErrorBox"'), html.indexOf("</div>", html.indexOf('id="updateErrorBox"')));
    const tags = box.match(/<a\b[^>]*>/g) || [];
    assert.strictEqual(tags.length, 1, "one link in the error box");
    const tag = tags[0];
    assert.ok(tag.includes('id="updateErrorDownload"'));
    assert.ok(tag.includes(`href="${URL}"`));
    assert.ok(tag.includes('target="_blank"') && tag.includes('rel="noopener noreferrer"'));
    assert.ok(/\shidden[\s>]/.test(tag), "hidden by default");
    assert.ok(box.indexOf('id="updateErrorMsg"') < box.indexOf('id="updateErrorDownload"'), "after the message");
    // The website has the section the link points to.
    assert.ok(site.includes('id="download"'));
}

// ─── CSS: the attribute hides it, whatever display the link has ──────────────
assert.ok(/\.update-error-download\[hidden\] \{\s*display: none;/.test(css));

// ─── Rendering: shown only when the status offers it ─────────────────────────
{
    const branch = source.slice(source.indexOf('updateStatusTitle.textContent = "Couldn\'t check for updates"'));
    const line = "if (updateErrorDownload) updateErrorDownload.hidden = s.offer_download !== true;";
    assert.ok(branch.indexOf(line) > 0 && branch.indexOf(line) < branch.indexOf("stopUpdatePolling();"),
        "the error branch sets the link from offer_download");
    assert.strictEqual(source.split("updateErrorDownload.hidden").length - 1, 1, "set in one place");
}

// ─── Clicks ──────────────────────────────────────────────────────────────────
const start = source.indexOf("  // When an update can't be checked or downloaded here");
const end = source.indexOf("  // The support links (Settings sidebar heart", start);
if (start < 0 || end < 0) throw new Error("could not locate the update download link block in app.js");
// The shared helper the block calls (openFixedAddress), sliced out the same way.
const helperStart = source.indexOf("  // Links that open one fixed address");
const helper = source.slice(helperStart, source.indexOf("  // When an update can't be checked", helperStart));
const block = helper + source.slice(start, end);
assert.ok(block.includes(`const WEBSITE_DOWNLOAD_URL = "${URL}";`), "the app's address matches the markup");

function setup({ desktop }) {
    const opened = [];
    const listeners = {};
    const link = {
        href: URL,
        addEventListener: (type, fn) => { listeners[type] = fn; },
        click() {
            let prevented = false;
            listeners.click({ preventDefault: () => { prevented = true; } });
            return prevented;
        },
    };
    const window = desktop ? { pywebview: { api: {} } } : {};
    const withWindowApi = (fn) => fn({ open_external_link: (url) => { opened.push(url); return Promise.resolve(true); } });
    new Function("window", "updateErrorDownload", "withWindowApi", block)(window, link, withWindowApi);
    return { link, opened };
}

// Desktop: the system browser opens the fixed address, even if the href was changed.
{
    const t = setup({ desktop: true });
    t.link.href = "https://example.com/elsewhere";
    assert.strictEqual(t.link.click(), true, "the window stays on InkDoc");
    assert.deepStrictEqual(t.opened, [URL]);
}

// Browser tab (Web Bench): the link's own target="_blank" does the work.
{
    const t = setup({ desktop: false });
    assert.strictEqual(t.link.click(), false);
    assert.deepStrictEqual(t.opened, []);
}

console.log("update_error_download_ui: all checks passed");
