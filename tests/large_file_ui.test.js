// Tests the large-file notice in app/ui/app.js.
//
// The server used to refuse files over 100 MB (HTTP 413). It now accepts any
// size, and the page says, without stopping anything, that large files can take
// longer: a toast when they are added and a chip on the queue row.
//
// Follows preview_word_count.test.js: slice the block out of the source, run
// it, no npm dependency.
const assert = require("assert");
const fs = require("fs");

// Line endings as in the repository: a Windows checkout (core.autocrlf) has CRLF.
const source = fs.readFileSync("app/ui/app.js", "utf8").replace(/\r\n/g, "\n");
const start = source.indexOf('  // "412 MB", "1.43 GB".');
const end = source.indexOf("  // What a converting queue row says");
if (start < 0 || end < 0 || end < start) {
    throw new Error("could not locate the large-file helpers in app.js");
}
const { isLargeFile, largeFileNotice, LARGE_FILE_BYTES, previewSlice, PREVIEW_MAX_CHARS } = new Function(
    `${source.slice(start, end)}\nreturn { isLargeFile, largeFileNotice, LARGE_FILE_BYTES, previewSlice, PREVIEW_MAX_CHARS };`
)();

const MB = 1024 * 1024;
assert.strictEqual(LARGE_FILE_BYTES, 100 * MB);

// Over 100 MB is large; exactly 100 MB and anything odd is not.
assert.strictEqual(isLargeFile({ size: 100 * MB }), false);
assert.strictEqual(isLargeFile({ size: 100 * MB + 1 }), true);
for (const f of [null, undefined, {}, { size: "x" }]) assert.strictEqual(isLargeFile(f), false);

// No large file, no toast.
assert.strictEqual(largeFileNotice([]), "");
assert.strictEqual(largeFileNotice(undefined), "");
assert.strictEqual(largeFileNotice([{ name: "a.pdf", size: 5 * MB }]), "");

// One large file is named, with its size.
const one = largeFileNotice([{ name: "a.pdf", size: 5 * MB }, { name: "scan.pdf", size: 240e6 }]);
assert.strictEqual(one, '"scan.pdf" is 240 MB. Large files can take longer to convert.');

// Several are counted in one toast, not one toast each.
const two = largeFileNotice([{ name: "a.pdf", size: 101 * MB }, { name: "b.pdf", size: 2e9 }]);
assert.strictEqual(two, "2 files are over 100 MB. Large files can take longer to convert.");

// The notice never gates the conversion: handleIncomingFiles shows it and
// starts the files either way.
const intakeStart = source.indexOf("  function handleIncomingFiles(files) {");
const intake = source.slice(intakeStart, source.indexOf("\n  }\n", intakeStart));
assert.ok(/if \(largeNotice\) showToast\(largeNotice, "info"/.test(intake), "the large-file toast is not shown on intake");
assert.ok(!/largeNotice[^;]*return/.test(intake), "the large-file notice stops the conversion");

// The queue row carries the chip while the file waits or converts.
assert.ok(/const largeChip = \(item\.status === "pending" \|\| item\.status === "converting"\) && isLargeFile\(item\.fileRef\)/.test(source),
    "the queue row has no large-file chip");

// ─── The preview of a very large result ───────────────────────────────────
assert.strictEqual(PREVIEW_MAX_CHARS, 5 * 1000 * 1000);

// Up to the limit: everything, unchanged (every result that previews in full today).
for (const md of ["", undefined, "# Title\n\nText.", "x".repeat(PREVIEW_MAX_CHARS)]) {
    const shown = previewSlice(md);
    assert.strictEqual(shown.cut, false);
    assert.strictEqual(shown.text, md || "");
}

// Over it: the start, ending at the last paragraph break before the limit.
const para = "A paragraph of converted text.\n\n";
const long = para.repeat(Math.ceil((PREVIEW_MAX_CHARS * 2) / para.length));
const cut = previewSlice(long);
assert.strictEqual(cut.cut, true);
assert.ok(cut.text.length <= PREVIEW_MAX_CHARS);
assert.ok(cut.text.length > PREVIEW_MAX_CHARS - para.length);
assert.ok(long.startsWith(cut.text) && cut.text.endsWith("text."), "cut mid-paragraph");

// No paragraph break to end on: a plain cut at the limit.
const solid = previewSlice("y".repeat(PREVIEW_MAX_CHARS + 10));
assert.strictEqual(solid.text.length, PREVIEW_MAX_CHARS);

// Copy and Download take the whole result when the preview is cut, and the
// (possibly edited) Raw view otherwise, as before.
const fmStart = source.indexOf("  function fullMarkdownFor(item) {");
const fmEnd = source.indexOf("\n  }\n", fmStart) + 4;
const fullMarkdownFor = new Function("rawEditor", `${source.slice(fmStart, fmEnd)}\nreturn fullMarkdownFor;`);
const editor = { value: "edited" };
assert.strictEqual(fullMarkdownFor(editor)({ previewCut: true, markdown: "everything" }), "everything");
assert.strictEqual(fullMarkdownFor(editor)({ previewCut: false, markdown: "everything" }), "edited");
assert.strictEqual(fullMarkdownFor({ value: "" })({ markdown: "everything" }), "everything");
assert.ok(source.includes("navigator.clipboard.writeText(fullMarkdownFor(item))"), "Copy doesn't take the whole result");
assert.ok(source.includes("const content  = fullMarkdownFor(item);"), "Download doesn't take the whole result");
const outsideHelper = source.slice(0, fmStart) + source.slice(fmEnd);
assert.ok(!outsideHelper.includes("rawEditor.value || item.markdown"), "something still copies the cut Raw view");

// A cut Raw view is read-only, and the next item starts editable again.
assert.ok(source.includes("rawEditor.readOnly = shown.cut;"));
const selStart = source.indexOf("  function selectItem(id) {");
assert.ok(source.slice(selStart, selStart + 400).includes("rawEditor.readOnly = false;"));

// And the server no longer refuses by size.
const server = fs.readFileSync("app/server/server.py", "utf8").replace(/\r\n/g, "\n");
assert.ok(!server.includes("MAX_FILE_SIZE_BYTES"), "the server still has a per-file size limit");
assert.ok(!server.includes("HTTP_413_REQUEST_ENTITY_TOO_LARGE,\n                        detail=f\"File exceeds"),
    "the server still refuses large uploads");

console.log("[OK] large_file_ui.test.js passed");
