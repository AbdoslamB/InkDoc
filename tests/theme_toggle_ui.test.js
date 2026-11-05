// Tests the theme toggle in app/ui/app.js.
//
// Where the web engine supports View Transitions, switching to light grows the
// light theme as a circle from the button, and switching to dark shrinks it
// back into the button. Without them, or with reduced motion, the theme
// switches at once. Either way the theme is saved (settings.json, through
// savePrefs; browser storage doesn't survive the desktop window) and the
// natively drawn Windows title bar button is re-synced: at once without the
// circle, and with it when the circle's edge reaches the button (a frame early),
// or at the transition's end if the circle never got there.
//
// Follows save_folder_ui.test.js: slice the block out of the source, run it
// against stubs, no npm dependency.
const assert = require("assert");
const fs = require("fs");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const start = source.indexOf("  // ─── Theme Toggle ");
const end = source.indexOf("  // ─── Auto-save Toggle ", start);
if (start < 0 || end < 0) throw new Error("could not locate the theme toggle block in app.js");
const block = source.slice(start, end);

function setup({
    initial = "dark", viewTransitions = false, reduceMotion = false, failReady = false,
    throwOnStart = false, maximizeButton = null, nativeTitlebar = true,
    syncThrows = 0, innerWidth = 1200, innerHeight = 800,
    toggleRect = { left: 1100, top: 10, width: 40, height: 40 },
} = {}) {
    // The server sets data-theme before first paint.
    const attrs = { "data-theme": initial };
    const classes = new Set();
    const saves = [];
    const prefs = { theme: initial };
    const animations = [];
    const transitions = [];
    let frames = [];
    // The theme each title bar sync saw: it reads the page's colours then.
    const syncedThemes = [];
    let listener = null;

    const root = {
        getAttribute: (n) => attrs[n] ?? null,
        setAttribute: (n, v) => { attrs[n] = v; },
        classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) },
        animate: (keyframes, options) => {
            // progress: the eased progress the engine reports; null once it ends.
            const a = {
                keyframes, options, cancelled: false, progress: 0,
                cancel() { this.cancelled = true; this.progress = null; },
                effect: { getComputedTiming: () => ({ progress: a.progress }) },
            };
            animations.push(a);
            return a;
        },
    };
    const document = {
        documentElement: root,
        getElementById: (id) => (id === "winMaximize" && maximizeButton
            ? { getBoundingClientRect: () => maximizeButton }
            : null),
    };
    if (viewTransitions) {
        document.startViewTransition = (callback) => {
            if (throwOnStart) throw new Error("not allowed");
            // The engine runs the update a little later, after it captures the
            // old view; the transition ends when the test says so.
            const done = Promise.resolve().then(callback);
            let end;
            const finished = new Promise((resolve) => { end = resolve; });
            const t = {
                ready: failReady ? done.then(() => Promise.reject(new Error("skipped"))) : done,
                finished: failReady ? done : done.then(() => finished),
                end: () => end(),
            };
            t.ready.catch(() => {});
            transitions.push(t);
            return t;
        };
    }
    const window = {
        innerWidth,
        innerHeight,
        matchMedia: (q) => ({ matches: q === "(prefers-reduced-motion: reduce)" && reduceMotion }),
        requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
    };
    let throwsLeft = syncThrows;
    if (nativeTitlebar) {
        window.__inkdocSyncTitlebar = () => {
            if (throwsLeft > 0) {
                throwsLeft--;
                throw new Error("bridge unavailable");
            }
            syncedThemes.push(attrs["data-theme"]);
        };
    }
    const themeToggle = {
        addEventListener: (type, fn) => { if (type === "click") listener = fn; },
        getBoundingClientRect: () => toggleRect,
    };

    const api = new Function("env", `
        const { window, document, themeToggle, prefs, savePrefs } = env;
        ${block}
        return { setThemeNow };
    `)({
        window, document, themeToggle, prefs,
        savePrefs: (patch) => { saves.push(patch); Object.assign(prefs, patch); },
    });

    return {
        ...api,
        click: () => listener(),
        theme: () => attrs["data-theme"],
        saves, prefs, classes, animations, transitions, syncedThemes,
        syncs: () => syncedThemes.length,
        pendingFrames: () => frames.length,
        // Runs one animation frame with the newest circle at this eased progress.
        frame: (progress) => {
            if (animations.length) animations[animations.length - 1].progress = progress;
            const due = frames;
            frames = [];
            due.forEach((fn) => fn());
        },
    };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
const x = 1120, y = 30;
const radius = Math.hypot(Math.max(x, 1200 - x), Math.max(y, 800 - y));
const small = `circle(0px at ${x}px ${y}px)`;
const full = `circle(${radius}px at ${x}px ${y}px)`;

(async () => {
    // Nothing is read from or written to browser storage any more.
    assert.ok(!/Storage\(/.test(block), "the theme lives in settings.json, not browser storage");

    // Start-up: the server's theme is used; the first click goes the other way.
    {
        const t = setup({ initial: "light" });
        assert.strictEqual(t.theme(), "light");
        t.click();
        assert.strictEqual(t.theme(), "dark");
    }

    // 1. No View Transitions: switches at once, saved, title bar synced.
    {
        const t = setup();
        t.click();
        assert.strictEqual(t.theme(), "light", "applied synchronously");
        assert.deepStrictEqual(t.saves, [{ theme: "light" }]);
        assert.strictEqual(t.syncs(), 1);
        assert.strictEqual(t.animations.length, 0);
    }

    // 2. Reduced motion: instant both ways, and no transition is started.
    {
        const t = setup({ viewTransitions: true, reduceMotion: true });
        t.click();
        assert.strictEqual(t.theme(), "light");
        t.click();
        assert.strictEqual(t.theme(), "dark");
        assert.strictEqual(t.transitions.length, 0, "reduced motion must not start a transition");
        assert.strictEqual(t.syncs(), 2);
    }

    // 3. To light: the new (light) view grows from the button to the farthest corner.
    {
        const t = setup({ viewTransitions: true });
        t.click();
        assert.ok(t.classes.has("theme-switching"), "colour fades paused during the switch");
        assert.ok(!t.classes.has("theme-to-dark"), "the new view stays on top");
        await settle();
        assert.strictEqual(t.theme(), "light");
        assert.deepStrictEqual(t.saves, [{ theme: "light" }]);
        assert.strictEqual(t.syncs(), 1);
        assert.strictEqual(t.animations.length, 1);
        const { keyframes, options } = t.animations[0];
        assert.deepStrictEqual(keyframes.clipPath, [small, full]);
        assert.strictEqual(options.pseudoElement, "::view-transition-new(root)");
        assert.strictEqual(options.easing, "cubic-bezier(.2,.8,.2,1)");
        assert.strictEqual(options.fill, "none");
        assert.ok(options.duration <= 350, "kept short: clicks are ignored while it plays");
        t.transitions[0].end();
        await settle();
        assert.ok(!t.classes.has("theme-switching"), "fades restored afterwards");
        assert.ok(t.animations[0].cancelled, "nothing is left applied to the next switch's views");
    }

    // 4. To dark: the old (light) view, drawn on top, shrinks back into the button.
    {
        const t = setup({ initial: "light", viewTransitions: true });
        t.click();
        assert.ok(t.classes.has("theme-to-dark"), "the old view goes above the new one");
        await settle();
        assert.strictEqual(t.theme(), "dark");
        assert.deepStrictEqual(t.saves, [{ theme: "dark" }]);
        const { keyframes, options } = t.animations[0];
        assert.deepStrictEqual(keyframes.clipPath, [full, small], "the reverse of growing");
        assert.strictEqual(options.pseudoElement, "::view-transition-old(root)");
        assert.strictEqual(options.easing, "cubic-bezier(.8,0,.8,.2)", "the growing curve, played backwards");
        assert.strictEqual(options.fill, "forwards", "held at nothing until the transition ends");
        assert.strictEqual(options.duration, 350);
        t.transitions[0].end();
        await settle();
        assert.ok(!t.classes.has("theme-to-dark") && !t.classes.has("theme-switching"));
        assert.ok(t.animations[0].cancelled, "the held circle is released");
    }

    // 5. The animation is skipped or fails: no error escapes, the theme still applies.
    {
        const t = setup({ viewTransitions: true, failReady: true });
        t.click();
        await settle();
        assert.strictEqual(t.theme(), "light");
        assert.strictEqual(t.animations.length, 0);
        assert.ok(!t.classes.has("theme-switching"));
    }
    {
        const t = setup({ initial: "light", viewTransitions: true, throwOnStart: true });
        t.click();
        assert.strictEqual(t.theme(), "dark", "falls back to an instant switch");
        assert.ok(!t.classes.has("theme-switching") && !t.classes.has("theme-to-dark"));
    }

    // 6. Two quick clicks toggle twice, even before the first update has run, and
    //    the first switch's end leaves the second's classes alone.
    {
        const t = setup({ viewTransitions: true });
        t.click(); // to light
        t.click(); // to dark
        await settle();
        assert.strictEqual(t.theme(), "dark");
        assert.deepStrictEqual(t.saves, [{ theme: "light" }, { theme: "dark" }]);
        assert.ok(t.classes.has("theme-to-dark"));
        t.transitions[0].end(); // the first, skipped one ends
        await settle();
        assert.ok(t.classes.has("theme-switching") && t.classes.has("theme-to-dark"), "still the second one's");
        assert.ok(t.animations[0].cancelled, "but its own circle is released");
        t.transitions[1].end();
        await settle();
        assert.ok(!t.classes.has("theme-switching") && !t.classes.has("theme-to-dark"));
    }
    {
        const t = setup({ initial: "light" });
        t.click();
        t.click();
        t.click();
        assert.strictEqual(t.theme(), "dark");
    }

    // 7. Restore defaults, an import or another window: applied at once, not saved again.
    {
        const t = setup({ initial: "light", viewTransitions: true });
        t.setThemeNow("dark");
        assert.strictEqual(t.theme(), "dark");
        assert.strictEqual(t.prefs.theme, "dark");
        assert.strictEqual(t.saves.length, 0);
        assert.strictEqual(t.transitions.length, 0, "no circle");
        assert.strictEqual(t.syncs(), 1);
        t.setThemeNow("bogus");
        assert.strictEqual(t.theme(), "dark");
        t.setThemeNow("light");
        t.click();
        await settle();
        assert.strictEqual(t.theme(), "dark", "the next click starts from it");
    }

    // 8. The native maximise button follows the circle's edge, not the click.
    //    The circle is centred on the theme button (1120, 30); the maximise
    //    button's centre (1184, 30) is 64 px away, a reach of 64 / radius.
    const maximizeButton = { left: 1170, top: 18, width: 28, height: 24 };
    const reach = 64 / radius;
    {
        // To light: the growing circle reaches it early on.
        const t = setup({ viewTransitions: true, maximizeButton });
        t.click();
        await settle();
        assert.strictEqual(t.theme(), "light");
        assert.strictEqual(t.syncs(), 0, "not when the page's theme changes under the old view");
        t.frame(reach * 0.2); // no speed known yet
        t.frame(reach * 0.45); // next frame ~0.7 of the way: not yet
        assert.strictEqual(t.syncs(), 0);
        t.frame(reach * 0.75); // next frame passes it: sync now, a frame early
        assert.deepStrictEqual(t.syncedThemes, ["light"]);
        assert.strictEqual(t.pendingFrames(), 0, "stops watching once synced");
        t.transitions[0].end();
        await settle();
        assert.strictEqual(t.syncs(), 1, "the end does not sync again");
    }
    {
        // To dark: the shrinking light view uncovers it only near the end.
        const t = setup({ initial: "light", viewTransitions: true, maximizeButton });
        t.click();
        await settle();
        const at = 1 - reach;
        // Steps of 0.02: each frame predicts the next one 0.02 further on.
        for (const p of [at - 0.07, at - 0.05, at - 0.03]) t.frame(p);
        assert.strictEqual(t.syncs(), 0, "most of the circle is still light over it");
        t.frame(at - 0.01); // next frame passes it
        assert.deepStrictEqual(t.syncedThemes, ["dark"]);
        t.transitions[0].end();
        await settle();
        assert.strictEqual(t.syncs(), 1);
    }
    {
        // The circle ends (or is cancelled) before a frame saw it cross:
        // the transition's end syncs it.
        const t = setup({ initial: "light", viewTransitions: true, maximizeButton });
        t.click();
        await settle();
        t.frame(0.3);
        t.frame(null);
        assert.strictEqual(t.syncs(), 0);
        assert.strictEqual(t.pendingFrames(), 0, "no frame loop left running");
        t.transitions[0].end();
        await settle();
        assert.deepStrictEqual(t.syncedThemes, ["dark"]);
    }
    {
        // Skipped before the circle starts: synced once, at the end.
        const t = setup({ viewTransitions: true, failReady: true, maximizeButton });
        t.click();
        await settle();
        assert.strictEqual(t.animations.length, 0);
        assert.deepStrictEqual(t.syncedThemes, ["light"]);
    }
    {
        // startViewTransition throws: an instant switch, synced at once.
        const t = setup({ initial: "light", viewTransitions: true, throwOnStart: true, maximizeButton });
        t.click();
        assert.deepStrictEqual(t.syncedThemes, ["dark"]);
    }
    {
        // Two quick clicks: only the newest switch syncs, and it ends on its theme.
        const t = setup({ viewTransitions: true, maximizeButton });
        t.click(); // to light
        t.click(); // to dark
        await settle();
        assert.strictEqual(t.theme(), "dark");
        t.transitions[0].end(); // the skipped first one
        await settle();
        assert.strictEqual(t.syncs(), 0, "the skipped switch must not sync");
        t.frame(0.5);
        t.frame(0.99);
        assert.deepStrictEqual(t.syncedThemes, ["dark"]);
        t.transitions[1].end();
        await settle();
        assert.deepStrictEqual(t.syncedThemes, ["dark"]);
    }
    {
        // The sync throws at the crossing: the classes still come off at the
        // end, and the end tries the sync again.
        const t = setup({ initial: "light", viewTransitions: true, maximizeButton, syncThrows: 1 });
        t.click();
        await settle();
        t.frame(0.97);
        t.frame(0.99);
        assert.strictEqual(t.syncs(), 0, "the first attempt threw");
        t.transitions[0].end();
        await settle();
        assert.ok(!t.classes.has("theme-switching") && !t.classes.has("theme-to-dark"));
        assert.deepStrictEqual(t.syncedThemes, ["dark"], "retried at the end");
    }
    {
        // A window with no size: no crossing to find, so the end syncs it.
        const t = setup({ viewTransitions: true, maximizeButton: { left: 0, top: 0, width: 0, height: 0 },
            innerWidth: 0, innerHeight: 0, toggleRect: { left: 0, top: 0, width: 0, height: 0 } });
        t.click();
        await settle();
        assert.strictEqual(t.pendingFrames(), 0, "no frame loop");
        t.transitions[0].end();
        await settle();
        assert.deepStrictEqual(t.syncedThemes, ["light"]);
    }
    {
        // A browser tab: no native title bar, no frame loop, nothing breaks.
        const t = setup({ viewTransitions: true, maximizeButton, nativeTitlebar: false });
        t.click();
        await settle();
        assert.strictEqual(t.pendingFrames(), 0);
        t.transitions[0].end();
        await settle();
        assert.strictEqual(t.theme(), "light");
    }

    // The CSS that pauses colour fades and stacks the shrinking view is in place.
    const css = fs.readFileSync("app/ui/style.css", "utf8");
    assert.ok(css.includes("html.theme-switching *"), "colour fades must be paused during the switch");
    assert.ok(/html\.theme-to-dark::view-transition-old\(root\) \{\s*z-index: 2;/.test(css));
    assert.ok(/html\.theme-to-dark::view-transition-new\(root\) \{\s*z-index: 1;/.test(css));

    console.log("[OK] theme_toggle_ui.test.js passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
