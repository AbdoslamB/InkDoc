// Tests the Docling card's update offer in app/ui/app.js.
//
// An InkDoc release can name a newer Docling pack than the one installed. The
// installed pack keeps working, so the card must offer the update (never start
// it), say which Docling it brings, and word the end of the download as an
// update, including a failure, which leaves the installed pack in place.
//
// Follows enrichment_addon_ui.test.js: slice the block out of the source, run it
// against stubs, no npm dependency.
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const start = source.indexOf("  async function pollDoclingProgress()");
const end = source.indexOf("  function startProgressPolling()");
if (start < 0 || end < 0 || end < start) {
    throw new Error("could not locate pollDoclingProgress / renderDoclingUpdate in app.js");
}
const block = source.slice(start, end);
if (!block.includes("function renderDoclingUpdate(")) {
    throw new Error("renderDoclingUpdate must sit between pollDoclingProgress and startProgressPolling");
}

const markup = fs.readFileSync("app/ui/index.html", "utf8");
const button = /<button[^>]*id="btnDoclingUpdate"[^>]*>/.exec(markup);
if (!button || !/display:\s*none/.test(button[0])) {
    throw new Error("btnDoclingUpdate must exist in index.html and ship hidden");
}
const note = /<p[^>]*id="doclingUpdateNote"[^>]*>/.exec(markup);
if (!note || !/\shidden(\s|>)/.test(note[0])) {
    throw new Error("doclingUpdateNote must exist in index.html and ship hidden");
}

function makeElement() {
    return {
        textContent: "", className: "", title: "", hidden: true,
        disabled: false, style: { display: "none", width: "" },
    };
}

const ELEMENTS = [
    "btnDoclingUpdate", "doclingUpdateNote", "btnDoclingInstall", "btnDoclingCancel",
    "btnDoclingVerify", "btnDoclingRemove", "doclingProgressContainer", "doclingProgressFill",
    "doclingProgressText", "doclingStatusBadge",
];

function build(progress, operation) {
    const env = { apiBase: "", toasts: [], refreshed: 0 };
    for (const name of ELEMENTS) env[name] = makeElement();
    env.showToast = (message, kind) => env.toasts.push(`${kind}: ${message}`);
    env.fetch = async () => ({ ok: true, json: async () => progress || { status: "idle" } });
    env.refreshEngines = async () => { env.refreshed += 1; };
    env.showOnMainScreenAction = () => [];
    env.formatSize = (bytes) => `${Math.round(bytes / (1024 * 1024))} MB`;
    const names = Object.keys(env).filter((n) => !["toasts", "refreshed"].includes(n));
    const factory = new Function(
        ...names, "clearInterval",
        `let pollInterval = 1; let doclingOperation = ${JSON.stringify(operation || "install")};\n${block}\n` +
        "return { pollDoclingProgress, renderDoclingUpdate, op: () => doclingOperation };"
    );
    const api = factory(...names.map((n) => env[n]), () => { });
    return { env, api };
}

const INSTALLED_OLD = {
    status: "installed", source_mode: false, update_available: true,
    version: "1.0.0", pack_version: "2.0.0",
    docling_version: "2.130.0", latest_docling_version: "2.140.0",
    download_size_bytes: 1036 * 1024 * 1024,
};

let failures = 0;
async function test(name, fn) {
    try {
        await fn();
        console.log(`ok - ${name}`);
    } catch (err) {
        failures += 1;
        console.log(`not ok - ${name}\n  ${err.message}`);
    }
}
function assert(cond, message) {
    if (!cond) throw new Error(message);
}

(async () => {
    await test("an earlier pack gets the offer, with both Docling versions and the size", () => {
        const { env, api } = build();
        api.renderDoclingUpdate(INSTALLED_OLD);
        assert(env.btnDoclingUpdate.style.display === "inline-flex", "the Update button is shown");
        assert(!env.doclingUpdateNote.hidden, "the note is shown");
        const text = env.doclingUpdateNote.textContent;
        assert(text.includes("Docling 2.140.0 is available"), `names the new Docling: ${text}`);
        assert(text.includes("1036 MB"), `gives the download size: ${text}`);
        assert(text.includes("Docling 2.130.0 keeps working"), `says the installed one keeps working: ${text}`);
        assert(!text.includes("security"), `no security claim without advisories: ${text}`);
    });

    await test("an update that fixes published advisories says so", () => {
        const { env, api } = build();
        api.renderDoclingUpdate({ ...INSTALLED_OLD, security_fixes: ["GHSA-cgc7-9qp3-86m3"] });
        const text = env.doclingUpdateNote.textContent;
        assert(text.includes("download), with security fixes."), `mentions the security fixes: ${text}`);
    });

    await test("the current pack, a source install, or no pack gets no offer", () => {
        for (const docling of [
            { ...INSTALLED_OLD, update_available: false },
            { ...INSTALLED_OLD, source_mode: true },
            { ...INSTALLED_OLD, status: "installable" },
            null,
        ]) {
            const { env, api } = build();
            env.btnDoclingUpdate.style.display = "inline-flex";
            env.doclingUpdateNote.hidden = false;
            api.renderDoclingUpdate(docling);
            assert(env.btnDoclingUpdate.style.display === "none", `no button for ${JSON.stringify(docling)}`);
            assert(env.doclingUpdateNote.hidden, `no note for ${JSON.stringify(docling)}`);
        }
    });

    await test("a finished update says so, and offers nothing for the main screen", async () => {
        const { env, api } = build({ status: "complete" }, "update");
        await api.pollDoclingProgress();
        assert(env.toasts.length === 1 && env.toasts[0] === "success: Docling is updated.", env.toasts.join(" | "));
        assert(api.op() === "install", "the operation resets for the next download");
        assert(env.refreshed === 1, "the card is redrawn");
    });

    await test("a failed update says the installed Docling still works, with the server's reason", async () => {
        const { env, api } = build({ status: "error", error_message: "Checksum mismatch" }, "update");
        await api.pollDoclingProgress();
        assert(
            env.toasts[0] === "error: Update failed; the installed Docling still works: Checksum mismatch",
            env.toasts.join(" | "),
        );
    });

    await test("a failed first install shows the server's reason, not 'Unknown error'", async () => {
        const { env, api } = build({ status: "error", error_message: "Not enough disk space" });
        await api.pollDoclingProgress();
        assert(env.toasts[0] === "error: Installation failed: Not enough disk space", env.toasts.join(" | "));
    });

    await test("while downloading, the Update button is hidden", async () => {
        const { env, api } = build({ status: "downloading", percent: 40 }, "update");
        env.btnDoclingUpdate.style.display = "inline-flex";
        await api.pollDoclingProgress();
        assert(env.btnDoclingUpdate.style.display === "none", "hidden during the download");
        assert(env.btnDoclingCancel.style.display === "inline-flex", "Cancel is offered");
    });

    if (failures) {
        console.log(`\n${failures} failed`);
        process.exit(1);
    }
    console.log("\nall passed");
})();
