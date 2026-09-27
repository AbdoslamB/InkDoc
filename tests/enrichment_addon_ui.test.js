// Tests the enrichment add-on state machine in app/ui/app.js.
//
// The two enrichment toggles shipped hardcoded `disabled` in the markup with no
// client code that could ever clear the attribute, so the feature was
// permanently inert. The invariant that replaced it -- the toggles are live if
// and only if the add-on reports usable -- is the whole fix, and it lives in
// client JavaScript that no Python test can reach.
//
// Follows docs_release_selector.test.js: slice the block out of the source, run
// it against stubs, no npm dependency.
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const blockStart = source.indexOf("  // ─── Recognition-Model Add-On");
const settingsStart = source.indexOf("  // ─── Settings Preferences ");
const wiringStart = source.indexOf("  // Both enrichment toggles persist the same way");
if (blockStart < 0 || settingsStart < 0 || wiringStart < 0) {
    throw new Error("could not locate the add-on / settings blocks in app.js");
}
// The add-on block alone, and the add-on block plus loadSettings, so the tests
// that need the two to agree can drive both.
const addonBlock = source.slice(blockStart, settingsStart);
const addonAndSettings = source.slice(blockStart, wiringStart);

function makeElement() {
    return {
        textContent: "", className: "", title: "",
        disabled: false, checked: false,
        style: { display: "", width: "" },
        addEventListener() { },
    };
}

const ELEMENT_NAMES = [
    "doclingCodeEnrichmentToggle", "doclingFormulaEnrichmentToggle",
    "doclingFallbackToggle", "dailyUpdateToggle",
    "addonTitleEl", "addonStatusBadge", "addonReason",
    "addonProgressContainer", "addonProgressFill", "addonProgressText",
    "btnAddonInstall", "btnAddonVerify", "btnAddonRemove", "btnAddonCancel",
];

// The markup ships both toggles with the `disabled` attribute set, so every test
// must start from that state. Asserted against index.html rather than assumed:
// if the attribute were ever dropped from the markup, starting the stubs at
// disabled=false would hide a regression in the code that clears it.
const markup = fs.readFileSync("app/ui/index.html", "utf8");
for (const id of ["doclingCodeEnrichmentToggle", "doclingFormulaEnrichmentToggle"]) {
    const tag = new RegExp(`<input[^>]*id="${id}"[^>]*>`).exec(markup);
    if (!tag) throw new Error(`${id} is missing from index.html`);
    if (!/\sdisabled(\s|>)/.test(tag[0])) {
        throw new Error(`${id} must ship disabled in the markup, got: ${tag[0]}`);
    }
}

function build(block, storedSettings) {
    const env = { apiBase: "", toasts: [] };
    for (const name of ELEMENT_NAMES) env[name] = makeElement();
    env.doclingCodeEnrichmentToggle.disabled = true;
    env.doclingFormulaEnrichmentToggle.disabled = true;
    env.showToast = (message, kind) => env.toasts.push(`${kind}: ${message}`);
    env.fetch = async () => ({ ok: true, json: async () => storedSettings || {} });
    env.fetchWithToken = async () => ({ ok: false, json: async () => ({}) });
    env.confirmDialog = async () => false;
    env.loadSettings = async () => { };

    const names = Object.keys(env);
    const factory = new Function(
        ...names, "setInterval", "clearInterval",
        `${block}\n return { applyAddonStatus, loadSettings, formatBytes };`
    );
    // setInterval is neutered: these tests assert on rendering, and a live poll
    // would keep the process alive after the assertions finish.
    const api = factory(...names.map((n) => env[n]), () => 0, () => { });
    return { env, api };
}

const BASE = {
    name: "code_enrichment",
    title: "Code & Formula Recognition",
    description: "Transcribes code and formulas.",
    progress: { status: "idle" },
};

const failures = [];
function check(label, condition, detail) {
    if (!condition) failures.push(`${label}: ${detail}`);
}

// ── The toggle-enable invariant, across every state the backend reports ───────
const STATES = [
    {
        label: "unreleased (no artifact published)",
        status: {
            ...BASE, installed: false, available: false, installable: false,
            usable: false, pack_installed: true, pack_version: "6.0.0",
            reason: "", size_bytes: 0,
        },
        badge: "Not Released", toggles: false, install: "disabled",
    },
    {
        label: "published, pack ok",
        status: {
            ...BASE, installed: false, available: true, installable: true,
            usable: false, pack_installed: true, pack_version: "6.0.0",
            reason: "", size_bytes: 671088640,
        },
        badge: "Not Installed", toggles: false, install: "enabled",
    },
    {
        label: "published, Docling pack missing",
        status: {
            ...BASE, installed: false, available: true, installable: false,
            usable: false, pack_installed: false,
            reason: "Docling is not installed", size_bytes: 671088640,
        },
        badge: "Unavailable", toggles: false, install: "disabled",
    },
    {
        label: "defective catalogue entry",
        status: {
            ...BASE, installed: false, available: false, installable: false,
            usable: false, pack_installed: true, pack_version: "6.0.0",
            catalogue_state: "defective",
            reason: "This add-on's catalogue entry is invalid; it cannot be installed.",
            size_bytes: 0,
        },
        badge: "Not Released", toggles: false, install: "disabled",
    },
    {
        label: "installed and usable",
        status: {
            ...BASE, installed: true, available: true, installable: false,
            usable: true, pack_installed: true, pack_version: "6.0.0",
            reason: "", size_bytes: 671088640,
        },
        badge: "Installed", toggles: true, install: "hidden",
    },
    {
        label: "installed but pack drifted under it",
        status: {
            ...BASE, installed: true, available: true, installable: false,
            usable: false, pack_installed: true, pack_version: "7.0.0",
            reason: "Installed against Docling 2.130.0, but this engine pack ships 2.140.0.",
            size_bytes: 671088640,
        },
        badge: "Unusable", toggles: false, install: "hidden",
    },
];

for (const scenario of STATES) {
    const { env, api } = build(addonBlock, null);
    api.applyAddonStatus(scenario.status);

    const code = env.doclingCodeEnrichmentToggle;
    const formula = env.doclingFormulaEnrichmentToggle;
    check(scenario.label, code.disabled === !scenario.toggles,
        `code toggle disabled=${code.disabled}, expected ${!scenario.toggles}`);
    check(scenario.label, formula.disabled === !scenario.toggles,
        `formula toggle disabled=${formula.disabled}, expected ${!scenario.toggles}`);
    check(scenario.label, env.addonStatusBadge.textContent === scenario.badge,
        `badge "${env.addonStatusBadge.textContent}", expected "${scenario.badge}"`);
    check(scenario.label, env.addonReason.textContent.length > 0,
        "no reason text rendered");

    const install = env.btnAddonInstall;
    if (scenario.install === "hidden") {
        check(scenario.label, install.style.display === "none",
            "install button should be hidden once installed");
    } else {
        check(scenario.label, install.style.display === "inline-flex",
            "install button should be visible");
        check(scenario.label, install.disabled === (scenario.install === "disabled"),
            `install disabled=${install.disabled}, expected ${scenario.install === "disabled"}`);
    }
}

// Exactly one state may enable the toggles.
const enabling = STATES.filter((s) => s.toggles);
check("invariant", enabling.length === 1 && enabling[0].status.usable === true,
    "only the usable state may enable the toggles");

// ── A published size must be shown, so a 640 MB download is never a surprise ──
{
    const { env, api } = build(addonBlock, null);
    api.applyAddonStatus(STATES[1].status);
    check("size disclosure", /640 MB/.test(env.btnAddonInstall.textContent),
        `install label "${env.btnAddonInstall.textContent}" omits the download size`);
    check("size disclosure", /640 MB/.test(env.addonReason.textContent),
        `reason "${env.addonReason.textContent}" omits the download size`);
}

// ── Checked state stays coherent whichever concurrent refresh lands last ──────
// The settings popover starts loadSettings() and refreshAddonStatus() together.
// A toggle left checked while disabled would claim enrichment is on while the
// worker refuses the job, so neither order may produce that.
(async () => {
    const STORED_ON = {
        docling_code_enrichment: true,
        docling_formula_enrichment: true,
    };
    const unusable = STATES[0].status;
    const usable = STATES[4].status;

    {
        const { env, api } = build(addonAndSettings, STORED_ON);
        api.applyAddonStatus(unusable);
        await api.loadSettings();
        const t = env.doclingCodeEnrichmentToggle;
        check("race: addon then settings", !(t.checked && t.disabled),
            `left checked=${t.checked} disabled=${t.disabled}`);
    }

    {
        const { env, api } = build(addonAndSettings, STORED_ON);
        await api.loadSettings();
        api.applyAddonStatus(unusable);
        const t = env.doclingCodeEnrichmentToggle;
        check("race: settings then addon", !(t.checked && t.disabled),
            `left checked=${t.checked} disabled=${t.disabled}`);
    }

    {
        // Becoming usable must restore what was actually persisted, or a user who
        // had enrichment on would find it silently off after installing.
        const { env, api } = build(addonAndSettings, STORED_ON);
        api.applyAddonStatus(usable);
        await new Promise((resolve) => setTimeout(resolve, 10));
        const t = env.doclingCodeEnrichmentToggle;
        check("becoming usable", !t.disabled && t.checked,
            `expected enabled and checked, got disabled=${t.disabled} checked=${t.checked}`);
    }

    // ── formatBytes ──────────────────────────────────────────────────────────
    {
        const { api } = build(addonBlock, null);
        check("formatBytes", api.formatBytes(0) === null, "0 should render nothing");
        check("formatBytes", api.formatBytes(671088640) === "640 MB", "640 MB");
        check("formatBytes", api.formatBytes(2147483648) === "2.0 GB", "2.0 GB");
    }

    if (failures.length) {
        for (const failure of failures) console.error(`  FAIL ${failure}`);
        console.error(`enrichment_addon_ui: ${failures.length} failure(s)`);
        process.exit(1);
    }
    console.log(`enrichment_addon_ui: PASS (${STATES.length} states, toggle invariant, both race orders)`);
})().catch((error) => {
    console.error(error.message);
    process.exit(1);
});
