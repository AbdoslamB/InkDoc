// Tests the GLM-OCR helpers in app/ui/app.js (slice pattern, no npm).
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");

function slice(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    if (start < 0 || end < 0) throw new Error(`could not locate ${startMarker} in app.js`);
    return source.slice(start, end);
}

const helperBlock = slice("  // ─── Pure helpers", "  // ─── End of pure helpers");
const EXPORTS = [
    "getEngineDisplayName", "qualityAction", "getNextEngine", "formatDuration", "formatSize",
    "formatJobProgress", "glmAccelLabel", "formatGlmProgress", "glmDownloadWarning", "glmLongJobPrompt",
    "initialEngine", "readStorage",
];
const h = new Function("window", `${helperBlock}\nreturn { ${EXPORTS.join(", ")} };`)({ localStorage: { getItem: () => null, setItem() {} } });

// ─── Labels and engine cycling ───────────────────────────────────────────────
assert.strictEqual(h.getEngineDisplayName("glm_ocr"), "GLM-OCR");
assert.strictEqual(h.getNextEngine("markit"), "markitdown", "GLM-OCR is skipped while not usable");
assert.strictEqual(h.getNextEngine("markit", true), "glm_ocr", "reached once usable");
assert.strictEqual(h.getNextEngine("glm_ocr", true), "markitdown");
assert.strictEqual(h.getNextEngine("markitdown", true), "docling");
assert.deepStrictEqual(h.qualityAction({ suggestion: "glm_ocr" }), { label: "Re-convert with GLM-OCR", engine: "glm_ocr" });
assert.deepStrictEqual(h.qualityAction({ suggestion: "install_glm_ocr" }), { label: "Download GLM-OCR", install: true });
{
    const stored = new Function("window", `${helperBlock}\nreturn { initialEngine, readStorage };`)({
        localStorage: { getItem: (k) => (k === "inkdoc-engine" ? "glm_ocr" : null), setItem() {} },
    });
    assert.strictEqual(stored.initialEngine(stored.readStorage), "glm_ocr", "a stored GLM-OCR choice is kept");
}

// ─── Formatting ──────────────────────────────────────────────────────────────
assert.strictEqual(h.formatDuration(45), "45 s");
assert.strictEqual(h.formatDuration(480), "8 min");
assert.strictEqual(h.formatDuration(4200), "1 h 10 min");
assert.strictEqual(h.formatSize(1454102427), "1.45 GB");
assert.strictEqual(h.formatSize(412e6), "412 MB");
assert.strictEqual(h.glmAccelLabel("vulkan"), "GPU: Vulkan");
assert.strictEqual(h.glmAccelLabel("metal"), "GPU: Metal");
assert.strictEqual(h.glmAccelLabel("cpu"), "CPU");

// ─── Queue row progress: "Page 7 / 30 · ~8 min left" ─────────────────────────
assert.strictEqual(
    h.formatJobProgress({ phase: "converting", page: 6, pages: 30, seconds_remaining_est: 480, message: "Reading…" }),
    "Page 7 / 30 · ~8 min left"
);
assert.strictEqual(h.formatJobProgress({ phase: "converting", page: 0, pages: 30, message: "x" }), "Page 1 / 30",
    "no estimate before the first page finishes");
assert.strictEqual(h.formatJobProgress({ phase: "checking", page: 30, pages: 30, message: "Checking…" }), "Checking…");
assert.strictEqual(h.formatJobProgress({ phase: "converting", message: "Converting with Docling…" }), "Converting with Docling…");
assert.strictEqual(h.formatJobProgress(null), "");

// ─── Card progress with source and fallback ──────────────────────────────────
assert.strictEqual(
    h.formatGlmProgress({ status: "downloading", source_label: "Hugging Face", bytes_downloaded: 412e6, total_bytes: 1.43e9 }),
    "From Hugging Face · 412 MB of 1.43 GB"
);
assert.strictEqual(
    h.formatGlmProgress({ status: "downloading", source_label: "InkDoc mirror", fallback_reason: "Hugging Face unreachable", bytes_downloaded: 0, total_bytes: 1.43e9 }),
    "From InkDoc mirror (Hugging Face unreachable) · 0 B of 1.43 GB"
);
assert.strictEqual(h.formatGlmProgress({ status: "selftest", message: "Testing GLM-OCR on this computer…" }),
    "Testing GLM-OCR on this computer…");

// ─── The warning before download (size + slower processing) ──────────────────
{
    const status = {
        download_size_bytes: 1454102427, variant: "q8",
        variants: [{ id: "q8", label: "Standard (Q8)", size_bytes: 1434837056 }, { id: "f16", label: "High precision (F16)", size_bytes: 2270175296 }],
    };
    const w = h.glmDownloadWarning(status, "q8");
    assert.ok(/1\.45 GB/.test(w.message), w.message);
    assert.strictEqual(w.confirmText, "Download 1.45 GB");
    const text = w.details.join(" ");
    assert.ok(/slower/i.test(text) && /per page/.test(text), "the warning explains slower processing");
    assert.ok(/3 GB of free memory/.test(text));
    assert.ok(/never uploaded/.test(text), "privacy is stated");
    const f16 = h.glmDownloadWarning(status, "f16");
    assert.ok(/2\.29 GB/.test(f16.message), f16.message);
}

// ─── Long-job confirmation ───────────────────────────────────────────────────
{
    const cpu = h.glmLongJobPrompt(1500, "cpu", 1);
    assert.ok(/about 25 min on this computer \(CPU\)/.test(cpu.message), cpu.message);
    assert.ok(/GPU/.test(cpu.message), "suggests GPU on CPU");
    const gpu = h.glmLongJobPrompt(600, "vulkan", 12);
    assert.ok(/these 12 files/.test(gpu.message) && !/Tip/.test(gpu.message));
}

// ─── Markup ──────────────────────────────────────────────────────────────────
{
    const markup = fs.readFileSync("app/ui/index.html", "utf8");
    assert.ok(/data-engine="glm_ocr"[^>]*id="pillGlmOcr"/.test(markup), "GLM-OCR pill");
    assert.ok(/class="engine-pill is-hidden" data-engine="glm_ocr"/.test(markup), "hidden until the catalogue allows it");
    assert.ok(/id="engineCardGlmOcr"/.test(markup), "GLM-OCR settings card");
    assert.ok(/never uploaded/.test(markup), "privacy line in the card");
    for (const id of ["btnGlmInstall", "btnGlmCancel", "btnGlmVerify", "btnGlmRemove", "btnGlmUpdate",
        "glmGpuToggle", "glmSourceSelect", "glmVariantSelect", "btnGlmSelftest", "glmProgressText", "appDialogDetails", "engineSpeedHint"]) {
        assert.ok(markup.includes(`id="${id}"`), `missing #${id}`);
    }
}

console.log("[OK] glm_ocr_ui.test.js passed");
