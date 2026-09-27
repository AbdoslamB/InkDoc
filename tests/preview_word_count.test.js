// Tests the word count stored on queue items in app/ui/app.js.
//
// The preview used to count words on every render with md.trim().split(/\s+/),
// building an array of every word each time an item was clicked. The count is
// now computed once, by setItemMarkdown(), when a conversion lands. It must give
// exactly the number the old expression did.
//
// Follows docs_release_selector.test.js: slice the block out of the source, run
// it against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const start = source.indexOf("  function setItemMarkdown(item, md) {");
const end = source.indexOf("  // ─── Queue Management ");
if (start < 0 || end < 0 || end < start) {
    throw new Error("could not locate setItemMarkdown in app.js");
}
const setItemMarkdown = new Function(`${source.slice(start, end)}\nreturn setItemMarkdown;`)();

const oldCount = (md) => (md.trim() ? md.trim().split(/\s+/).length : 0);

const cases = [
    "",
    "   \n\t  ",
    "one",
    "  leading and trailing  ",
    "# Heading\n\nSome *markdown* text.\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n",
    "tabs\tand\r\nwindows\r\nline endings",
    "non breaking em space　ideographic",
    "```python\nprint('hi')\n```\n",
    "word ".repeat(50000),
];
for (const md of cases) {
    const item = {};
    setItemMarkdown(item, md);
    assert.strictEqual(item.markdown, md);
    assert.strictEqual(item.wordCount, oldCount(md), `word count differs for ${JSON.stringify(md.slice(0, 40))}`);
}

// A conversion that returns no markdown must not leave the item undefined.
for (const md of [undefined, null]) {
    const item = {};
    setItemMarkdown(item, md);
    assert.strictEqual(item.markdown, "");
    assert.strictEqual(item.wordCount, 0);
}

// Every assignment goes through the helper, so no item can carry markdown with
// a stale or missing count; and the render path no longer recounts.
assert.ok(!/item\.markdown\s*=[^=]/.test(source.slice(0, start) + source.slice(end)),
    "item.markdown is assigned outside setItemMarkdown; its wordCount would be stale");
assert.ok(!source.includes("split(/\\s+/).length"), "the preview still recounts words on every render");

console.log("[OK] preview_word_count.test.js passed");
