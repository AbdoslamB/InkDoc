// Tests the Settings drawer in app/ui/app.js and its markup in index.html.
//
// The drawer opens from the gear (on the section last used) or from engine
// prompts (on Engines), and closes from the gear, the X, Escape and a click
// outside it. Clicks in the confirm dialog and toasts, which Settings opens
// itself, keep it open; while the dialog is up, Escape and Tab are the
// dialog's. Focus moves into the drawer and back out.
//
// Follows save_folder_ui.test.js: slice the block out of the source, run it
// against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const html = fs.readFileSync("app/ui/index.html", "utf8");
const start = source.indexOf("  // ─── Settings Drawer ");
const end = source.indexOf("  // ─── End settings drawer", start);
if (start < 0 || end < 0) throw new Error("could not locate the settings drawer block in app.js");
const block = source.slice(start, end);

const SECTIONS = ["general", "engines", "updates", "about"];

function setup() {
    const doc = { activeElement: null, listeners: {}, cssVars: {}, rootClasses: new Set() };

    function el(name, { parent = null, visible = true } = {}) {
        const listeners = {};
        const attrs = {};
        const classes = new Set();
        const node = {
            name, parent, hidden: false, disabled: false, dataset: {}, isConnected: true,
            classList: {
                add: (c) => classes.add(c), remove: (c) => classes.delete(c),
                contains: (c) => classes.has(c),
            },
            setAttribute: (k, v) => { attrs[k] = String(v); },
            getAttribute: (k) => (k in attrs ? attrs[k] : null),
            removeAttribute: (k) => { delete attrs[k]; },
            addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
            fire: (type, e = {}) => (listeners[type] || []).forEach((fn) => fn(e)),
            focus: () => { doc.activeElement = node; },
            getClientRects: () => (node.visible() ? [{}] : []),
            visible: () => visible && !node.hidden,
            contains: (other) => {
                for (let n = other; n; n = n.parent) if (n === node) return true;
                return false;
            },
        };
        return node;
    }

    const body = el("body");
    const header = el("header", { parent: body });
    header.getBoundingClientRect = () => ({ bottom: 57.4 });
    const gear = el("gear", { parent: header });
    const scrim = el("scrim", { parent: body });
    scrim.hidden = true;
    const drawer = el("drawer", { parent: header });
    const closeBtn = el("close", { parent: drawer });
    const nav = el("nav", { parent: drawer });
    const navItems = SECTIONS.map((s) => {
        const item = el(`nav-${s}`, { parent: nav });
        item.dataset.settingsSection = s;
        if (s === "general") item.setAttribute("aria-current", "page"); // as in index.html
        return item;
    });
    const panes = SECTIONS.map((s) => {
        const pane = el(`pane-${s}`, { parent: drawer });
        pane.dataset.settingsPane = s;
        pane.hidden = s !== "general";
        return pane;
    });
    // One focusable control per pane; it counts as visible only with its pane.
    const controls = panes.map((pane) => {
        const c = el(`control-${pane.dataset.settingsPane}`, { parent: pane });
        c.visible = () => !pane.hidden;
        return c;
    });
    drawer.querySelectorAll = () => [closeBtn, ...navItems, ...controls];
    const dialog = el("dialog", { parent: body });
    dialog.hidden = true;
    const dialogButton = el("dialogButton", { parent: dialog });
    const toast = el("toast", { parent: body });
    const toastButton = el("toastButton", { parent: toast });
    const pill = el("pill", { parent: body });
    const queueItem = el("queueItem", { parent: body });

    doc.activeElement = body;
    const ids = { settingsScrim: scrim, settingsCloseBtn: closeBtn, settingsNav: nav };
    const calls = [];
    const winListeners = {};
    const env = {
        window: { addEventListener: (type, fn) => { (winListeners[type] = winListeners[type] || []).push(fn); } },
        document: {
            body,
            get activeElement() { return doc.activeElement; },
            getElementById: (id) => ids[id],
            querySelector: (sel) => (sel === ".app-header" ? header : null),
            querySelectorAll: (sel) => (sel === "[data-settings-section]" ? navItems : sel === "[data-settings-pane]" ? panes : []),
            documentElement: {
                style: { setProperty: (k, v) => { doc.cssVars[k] = v; } },
                classList: { add: (c) => doc.rootClasses.add(c), remove: (c) => doc.rootClasses.delete(c) },
            },
            addEventListener: (type, fn) => { (doc.listeners[type] = doc.listeners[type] || []).push(fn); },
        },
        settingsPopover: drawer,
        settingsBtn: gear,
        appDialogOverlay: dialog,
        toastNotification: toast,
        loadSettings: () => calls.push("loadSettings"),
        fetchUpdateStatus: () => calls.push("fetchUpdateStatus"),
        refreshAddonStatus: () => calls.push("refreshAddonStatus"),
        refreshGlm: (force) => calls.push(`refreshGlm(${force})`),
    };
    const api = new Function("env", `
        const { window, document, settingsPopover, settingsBtn, appDialogOverlay, toastNotification,
                loadSettings, fetchUpdateStatus, refreshAddonStatus, refreshGlm } = env;
        ${block}
        return { openSettings, closeSettings, isOpen: isSettingsOpen, section: () => settingsSection };
    `)(env);

    // A click on `target`: its own listeners first, then the document's, as in a
    // browser, unless one of them stops propagation.
    function click(target, extra = {}) {
        let stopped = false;
        const path = [];
        for (let n = target; n; n = n.parent) path.push(n);
        const e = { target, composedPath: () => path, stopPropagation: () => { stopped = true; }, ...extra };
        target.fire("click", e);
        if (!stopped) (doc.listeners.click || []).forEach((fn) => fn(e));
    }
    function key(k, { shiftKey = false, on = null } = {}) {
        let prevented = false;
        const e = { key: k, shiftKey, preventDefault: () => { prevented = true; } };
        if (on) on.fire("keydown", e);
        (doc.listeners.keydown || []).forEach((fn) => fn(e));
        return prevented;
    }
    const shownPane = () => panes.filter((p) => !p.hidden).map((p) => p.dataset.settingsPane);
    const current = () => navItems.filter((i) => i.getAttribute("aria-current") === "page").map((i) => i.dataset.settingsSection);

    return {
        ...api, doc, calls, click, key, shownPane, current, winListeners,
        gear, scrim, drawer, closeBtn, nav, navItems, panes, controls, dialog, dialogButton, toast, toastButton, pill, queueItem, body,
    };
}

// The gear opens Settings on General, sets everything up, and closes it again.
{
    const t = setup();
    t.gear.focus();
    t.click(t.gear);
    assert.ok(t.isOpen());
    assert.strictEqual(t.scrim.hidden, false);
    assert.strictEqual(t.gear.getAttribute("aria-expanded"), "true");
    assert.ok(t.doc.rootClasses.has("settings-open"), "the page knows, so toasts move beside the drawer");
    assert.strictEqual(t.doc.cssVars["--settings-top"], "57.4px", "starts exactly where the header ends (not rounded)");
    assert.deepStrictEqual(t.shownPane(), ["general"]);
    assert.deepStrictEqual(t.current(), ["general"]);
    assert.strictEqual(t.doc.activeElement, t.navItems[0], "focus moves to the current section");
    assert.deepStrictEqual(t.calls, ["loadSettings", "fetchUpdateStatus", "refreshAddonStatus", "refreshGlm(true)"]);

    t.click(t.gear);
    assert.ok(!t.isOpen());
    assert.strictEqual(t.scrim.hidden, true);
    assert.strictEqual(t.gear.getAttribute("aria-expanded"), "false");
    assert.ok(!t.doc.rootClasses.has("settings-open"));
    assert.strictEqual(t.doc.activeElement, t.gear, "focus returns to the gear");
}

// The X and Escape close it and hand focus back.
{
    const t = setup();
    t.gear.focus();
    t.click(t.gear);
    t.click(t.closeBtn);
    assert.ok(!t.isOpen());
    assert.strictEqual(t.doc.activeElement, t.gear);

    t.click(t.gear);
    assert.ok(t.key("Escape"));
    assert.ok(!t.isOpen());
    assert.strictEqual(t.doc.activeElement, t.gear);
    assert.ok(!t.key("Escape"), "Escape does nothing while Settings is closed");
}

// Clicks inside the drawer, the confirm dialog or a toast keep it open; a click
// on the scrim closes it and restores focus, a click elsewhere leaves focus where
// the user put it.
{
    const t = setup();
    t.click(t.gear);
    t.click(t.controls[0]);
    t.click(t.dialogButton);
    t.click(t.toastButton);
    assert.ok(t.isOpen(), "drawer, dialog and toast clicks keep Settings open");

    // A button that re-renders (detaches) its own card still counts as inside.
    const detached = t.controls[0];
    const path = [detached, t.panes[0], t.drawer];
    detached.parent = null;
    t.click(detached, { composedPath: () => path });
    assert.ok(t.isOpen());

    t.click(t.scrim);
    assert.ok(!t.isOpen());
    assert.strictEqual(t.doc.activeElement, t.gear);

    t.click(t.gear);
    t.queueItem.focus();
    t.click(t.queueItem);
    assert.ok(!t.isOpen());
    assert.strictEqual(t.doc.activeElement, t.queueItem, "a click elsewhere keeps its focus");
}

// A file dragged over the scrim closes Settings so it can land on the drop zone.
{
    const t = setup();
    t.click(t.gear);
    t.navItems[2].focus();
    t.scrim.fire("dragenter", {});
    assert.ok(!t.isOpen());
    assert.strictEqual(t.doc.activeElement, t.navItems[2], "focus is not moved by a drag");
}

// While the confirm dialog is up, Escape and Tab are left to it.
{
    const t = setup();
    t.click(t.gear);
    t.dialog.hidden = false;
    assert.ok(!t.key("Escape"));
    assert.ok(t.isOpen());
    t.dialogButton.focus();
    assert.ok(!t.key("Tab"));
    assert.strictEqual(t.doc.activeElement, t.dialogButton);
}

// Tab cycles through the drawer's visible controls only, and is always moved by
// the drawer itself (on macOS, WebKit's own Tab skips buttons by default).
{
    const t = setup();
    t.click(t.gear);
    t.controls[0].focus(); // last visible: General's control (other panes are hidden)
    assert.ok(t.key("Tab"));
    assert.strictEqual(t.doc.activeElement, t.closeBtn, "Tab wraps to the first control");
    assert.ok(t.key("Tab", { shiftKey: true }));
    assert.strictEqual(t.doc.activeElement, t.controls[0], "Shift+Tab wraps to the last visible control");
    t.navItems[1].focus();
    assert.ok(t.key("Tab"));
    assert.strictEqual(t.doc.activeElement, t.navItems[2], "Tab in the middle moves to the next control, buttons included");
    assert.ok(t.key("Tab", { shiftKey: true }));
    assert.strictEqual(t.doc.activeElement, t.navItems[1]);
    t.pill.focus();
    assert.ok(t.key("Tab"), "focus outside the drawer is brought back in");
    assert.strictEqual(t.doc.activeElement, t.closeBtn);
    t.pill.focus();
    assert.ok(t.key("Tab", { shiftKey: true }));
    assert.strictEqual(t.doc.activeElement, t.controls[0], "…at the end for Shift+Tab");
}

// A radio group is one Tab stop, on its selected option (Layout width).
{
    const t = setup();
    t.click(t.gear);
    const radio = t.controls[0]; // General's only control, made an unselected radio
    radio.type = "radio";
    radio.checked = false;
    t.navItems[3].focus();
    t.key("Tab");
    assert.strictEqual(t.doc.activeElement, t.closeBtn, "an unselected radio is skipped");
    radio.checked = true;
    t.navItems[3].focus();
    t.key("Tab");
    assert.strictEqual(t.doc.activeElement, radio, "the selected radio is a stop");
}

// Engine prompts open Engines; the gear then reopens where Settings was left.
{
    const t = setup();
    t.pill.focus();
    let stopped = false;
    t.openSettings({ stopPropagation: () => { stopped = true; } }, "engines");
    assert.ok(stopped, "the opening click must not reach the click-outside handler");
    assert.deepStrictEqual(t.shownPane(), ["engines"]);
    assert.strictEqual(t.doc.activeElement, t.navItems[1]);
    t.key("Escape");
    assert.strictEqual(t.doc.activeElement, t.pill, "focus returns to what opened it");

    t.click(t.gear);
    assert.deepStrictEqual(t.shownPane(), ["engines"], "the gear reopens the last section");

    // Opening again while open switches section without moving focus.
    t.controls[1].focus();
    t.openSettings(null, "updates");
    assert.deepStrictEqual(t.shownPane(), ["updates"]);
    assert.strictEqual(t.doc.activeElement, t.controls[1]);

    t.openSettings(null, "nonsense");
    assert.deepStrictEqual(t.shownPane(), ["updates"], "an unknown section changes nothing");
}

// A focus target that has gone away falls back to the gear.
{
    const t = setup();
    t.toastButton.focus();
    t.openSettings(null, "engines");
    t.toast.hidden = true;
    t.toastButton.visible = () => false;
    t.click(t.closeBtn);
    assert.strictEqual(t.doc.activeElement, t.gear);
}

// Nav: clicks switch sections; arrow keys, Home and End move and wrap.
{
    const t = setup();
    t.click(t.gear);
    t.click(t.navItems[3]);
    assert.deepStrictEqual(t.shownPane(), ["about"]);
    assert.deepStrictEqual(t.current(), ["about"]);
    assert.ok(t.isOpen(), "a nav click is inside the drawer");

    const press = (k) => t.nav.fire("keydown", { key: k, preventDefault() {} });
    press("ArrowDown");
    assert.deepStrictEqual(t.shownPane(), ["general"], "wraps from the last to the first");
    assert.strictEqual(t.doc.activeElement, t.navItems[0]);
    press("ArrowLeft");
    assert.deepStrictEqual(t.shownPane(), ["about"], "wraps from the first to the last");
    press("Home");
    assert.deepStrictEqual(t.shownPane(), ["general"]);
    press("End");
    assert.deepStrictEqual(t.shownPane(), ["about"]);
    press("ArrowUp");
    assert.deepStrictEqual(t.shownPane(), ["updates"]);
    press("a");
    assert.deepStrictEqual(t.shownPane(), ["updates"], "other keys are ignored");

    // Tab can leave focus on another item than the shown section: start from focus.
    t.navItems[3].focus(); // About focused, Updates shown
    press("ArrowDown");
    assert.deepStrictEqual(t.shownPane(), ["general"], "moves on from the focused item");
}

// Resizing re-places the drawer only while it is open.
{
    const t = setup();
    t.winListeners.resize.forEach((fn) => fn());
    assert.strictEqual(t.doc.cssVars["--settings-top"], undefined);
    t.click(t.gear);
    t.doc.cssVars["--settings-top"] = "0px";
    t.winListeners.resize.forEach((fn) => fn());
    assert.strictEqual(t.doc.cssVars["--settings-top"], "57.4px");
}

// Every caller outside the drawer is about an engine, so it opens Engines.
{
    const outside = source.slice(0, start) + source.slice(end);
    const callers = outside.match(/openSettings\([^)]*\)/g) || [];
    assert.ok(callers.length >= 6, `expected the engine prompts to call openSettings, found ${callers.length}`);
    callers.forEach((call) => assert.ok(call.includes('"engines"'), `${call} should open the Engines section`));
}

// Markup: one nav item and one pane per section, General shown first, the
// daily update toggle under Updates, and the drawer wired to the gear.
{
    SECTIONS.forEach((s) => {
        assert.strictEqual((html.match(new RegExp(`data-settings-section="${s}"`, "g")) || []).length, 1, `nav item ${s}`);
        assert.strictEqual((html.match(new RegExp(`data-settings-pane="${s}"`, "g")) || []).length, 1, `pane ${s}`);
    });
    const paneTag = (s) => html.match(new RegExp(`<section[^>]*data-settings-pane="${s}"[^>]*>`))[0];
    assert.ok(!/\shidden[\s>]/.test(paneTag("general")), "General is shown first");
    assert.ok(/data-settings-section="general"[^>]*aria-current="page"/.test(html), "and marked current");
    ["engines", "updates", "about"].forEach((s) => assert.ok(/\shidden[\s>]/.test(paneTag(s)), `${s} starts hidden`));

    const paneBody = (s) => {
        const from = html.indexOf(paneTag(s));
        return html.slice(from, html.indexOf("</section>", from));
    };
    assert.ok(paneBody("updates").includes('id="dailyUpdateToggle"'), "daily update toggle sits under Updates");
    assert.ok(paneBody("updates").includes('id="settingsUpdatesSection"'));
    assert.ok(paneBody("engines").includes('id="engineCardGlmOcr"'));
    assert.ok(paneBody("general").includes('id="autoSaveToggle"'));
    assert.ok(paneBody("about").includes('id="aboutUsLink"'), "Read more keeps the id its click handler uses");
    assert.ok(paneBody("about").includes('href="https://abdoslamb.github.io/InkDoc/"'));

    const gear = html.match(/<button id="settingsBtn"[^>]*>/)[0];
    assert.ok(gear.includes('aria-expanded="false"') && gear.includes('aria-controls="settingsPopover"'));
    const drawer = html.match(/<div[^>]*id="settingsPopover"[^>]*>/)[0];
    assert.ok(drawer.includes('role="dialog"') && drawer.includes('aria-modal="true"'));
    assert.ok(/<div class="settings-scrim" id="settingsScrim" hidden>/.test(html), "the scrim starts hidden");
}

// Every element the old popover had is still there exactly once, so no
// getElementById in app.js lost its target in the move.
{
    const ids = `settingsPopover autoSaveToggle btnChangeSaveFolder btnResetSaveFolder saveFolderPath saveFolderProblem
        saveFolderHint doclingFallbackToggle qualityCheckToggle dailyUpdateToggle engineCardMarkitdown versionMarkitdown
        engineCardMarkit versionMarkit engineCardDocling doclingStatusBadge doclingSizeInfo doclingVersionInfo
        doclingSourceInfo doclingPlatformsRow doclingPlatformsInfo doclingPathRow doclingPathInfo doclingProgressContainer
        doclingProgressFill doclingProgressText btnDoclingInstall btnDoclingVerify btnDoclingRemove btnDoclingCancel
        doclingEnrichment addonTitle addonStatusBadge addonReason addonProgressContainer addonProgressFill
        addonProgressText btnAddonInstall btnAddonVerify btnAddonRemove btnAddonCancel doclingCodeEnrichmentToggle
        doclingFormulaEnrichmentToggle doclingEnrichmentCaption engineCardGlmOcr glmStatusBadge glmSizeDesc
        glmRequirement glmModelVersion glmRuntimeVersion glmSizeInfo glmLicenseInfo glmAccelInfo glmSpeedInfo
        glmSourceInfo glmPathRow glmPathInfo glmReason glmProgressContainer glmProgressFill glmProgressText
        btnGlmInstall btnGlmUpdate btnGlmVerify btnGlmRemove btnGlmCancel glmOptions glmGpuToggle glmGpuLabel
        glmGpuCaption glmAdvanced glmSourceSelect glmVariantSelect btnGlmSelftest glmCatalogueInfo
        settingsUpdatesSection appCurrentVersionBadge updateCard updateStatusTitle updateStatusSubtitle btnCheckUpdates
        iconCheckUpdatesSpin btnCheckUpdatesText updateDetails updateNewVersion updateAssetSize updateNotesAccordion
        updateNotesContent updateProgressWrapper updateProgressFill updateProgressText updateActions btnDownloadUpdate
        btnApplyUpdate btnRevealUpdate btnCancelUpdate updateActionHelp updateErrorBox updateErrorMsg aboutUsLink`
        .split(/\s+/).filter(Boolean);
    assert.strictEqual(ids.length, 100);
    ids.forEach((id) => {
        const count = (html.match(new RegExp(`id="${id}"`, "g")) || []).length;
        assert.strictEqual(count, 1, `#${id} should appear exactly once, found ${count}`);
    });
}

console.log("settings drawer UI tests passed");
