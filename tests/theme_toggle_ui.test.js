// Tests the theme toggle in app/ui/app.js.
//
// Where the web engine supports View Transitions, the new theme grows as a
// circle from the button (as on the website). Without them, or with reduced
// motion, the theme switches at once, exactly as before. Either way the theme
// is saved and the natively drawn Windows title bar button is re-synced.
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

function setup({ saved = null, viewTransitions = false, reduceMotion = false, failReady = false, throwOnStart = false } = {}) {
    const attrs = {};
    const classes = new Set();
    const storage = saved ? { "inkdoc-theme": saved } : {};
    const animations = [];
    const transitions = [];
    let syncs = 0;
    let listener = null;

    const root = {
        getAttribute: (n) => attrs[n] ?? null,
        setAttribute: (n, v) => { attrs[n] = v; },
        classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) },
        animate: (keyframes, options) => { animations.push({ keyframes, options }); },
    };
    const document = { documentElement: root };
    if (viewTransitions) {
        document.startViewTransition = (callback) => {
            if (throwOnStart) throw new Error("not allowed");
            // The engine runs the update a little later, after it captures the old view.
            const done = Promise.resolve().then(callback);
            const t = {
                ready: failReady ? done.then(() => Promise.reject(new Error("skipped"))) : done,
                finished: done,
            };
            t.ready.catch(() => {});
            transitions.push(t);
            return t;
        };
    }
    const window = {
        innerWidth: 1200,
        innerHeight: 800,
        matchMedia: (q) => ({ matches: q === "(prefers-reduced-motion: reduce)" && reduceMotion }),
        __inkdocSyncTitlebar: () => { syncs++; },
    };
    const themeToggle = {
        addEventListener: (type, fn) => { if (type === "click") listener = fn; },
        getBoundingClientRect: () => ({ left: 1100, top: 10, width: 40, height: 40 }),
    };

    new Function("env", `
        const { window, document, themeToggle, readStorage, writeStorage } = env;
        ${block}
    `)({
        window, document, themeToggle,
        readStorage: (k) => storage[k] ?? null,
        writeStorage: (k, v) => { storage[k] = v; },
    });

    return {
        click: () => listener(),
        theme: () => attrs["data-theme"],
        storage, classes, animations, transitions,
        syncs: () => syncs,
    };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
    // Start-up: the saved theme is restored, dark by default.
    assert.strictEqual(setup().theme(), "dark");
    assert.strictEqual(setup({ saved: "light" }).theme(), "light");

    // 1. No View Transitions: switches at once, saved, title bar synced (as before).
    {
        const t = setup();
        t.click();
        assert.strictEqual(t.theme(), "light", "applied synchronously");
        assert.strictEqual(t.storage["inkdoc-theme"], "light");
        assert.strictEqual(t.syncs(), 1);
        assert.strictEqual(t.animations.length, 0);
    }

    // 2. Reduced motion: also instant, and no transition is started.
    {
        const t = setup({ viewTransitions: true, reduceMotion: true });
        t.click();
        assert.strictEqual(t.theme(), "light");
        assert.strictEqual(t.transitions.length, 0, "reduced motion must not start a transition");
        assert.strictEqual(t.syncs(), 1);
    }

    // 3. Supported: the circle grows from the button to the farthest corner.
    {
        const t = setup({ viewTransitions: true });
        t.click();
        assert.ok(t.classes.has("theme-switching"), "colour fades paused during the switch");
        await settle();
        assert.strictEqual(t.theme(), "light");
        assert.strictEqual(t.storage["inkdoc-theme"], "light");
        assert.strictEqual(t.syncs(), 1);
        assert.strictEqual(t.animations.length, 1);
        const { keyframes, options } = t.animations[0];
        const x = 1120, y = 30;
        const radius = Math.hypot(Math.max(x, 1200 - x), Math.max(y, 800 - y));
        assert.deepStrictEqual(keyframes.clipPath, [
            `circle(0px at ${x}px ${y}px)`,
            `circle(${radius}px at ${x}px ${y}px)`,
        ]);
        assert.strictEqual(options.pseudoElement, "::view-transition-new(root)");
        assert.ok(options.duration <= 350, "kept short: clicks are ignored while it plays");
        assert.ok(!t.classes.has("theme-switching"), "fades restored afterwards");
    }

    // 4. The animation is skipped or fails: no error escapes, the theme still applies.
    {
        const t = setup({ viewTransitions: true, failReady: true });
        t.click();
        await settle();
        assert.strictEqual(t.theme(), "light");
        assert.strictEqual(t.animations.length, 0);
        assert.ok(!t.classes.has("theme-switching"));
    }
    {
        const t = setup({ viewTransitions: true, throwOnStart: true });
        t.click();
        assert.strictEqual(t.theme(), "light", "falls back to an instant switch");
        assert.ok(!t.classes.has("theme-switching"));
    }

    // 5. Two quick clicks toggle twice, even before the first update has run.
    {
        const t = setup({ viewTransitions: true });
        t.click();
        t.click();
        await settle();
        assert.strictEqual(t.theme(), "dark");
        assert.strictEqual(t.storage["inkdoc-theme"], "dark");
    }
    {
        const t = setup({ saved: "light" });
        t.click();
        t.click();
        t.click();
        assert.strictEqual(t.theme(), "dark");
    }

    // The CSS that pauses colour fades during the circle is in place.
    const css = fs.readFileSync("app/ui/style.css", "utf8");
    assert.ok(css.includes("html.theme-switching *"), "colour fades must be paused during the switch");

    console.log("[OK] theme_toggle_ui.test.js passed");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
