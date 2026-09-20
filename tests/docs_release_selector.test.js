const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync("docs/index.html", "utf8");
const marker = "Dynamically upgrade download links";
const markerPosition = source.indexOf(marker);
const start = source.lastIndexOf("(function () {", markerPosition);
const end = source.indexOf("})();", markerPosition) + 5;
if (start < 0 || end < 5) throw new Error("release IIFE not found");
const releaseIife = source.slice(start, end);

async function runSelector(releases) {
    const links = [
        { href: "https://github.com/AbdoslamB/inkdoc/releases/latest/download/inkdoc.exe" },
        { href: "https://github.com/AbdoslamB/inkdoc/releases/latest/download/inkdoc-linux.zip" },
    ];
    const context = {
        AbortController: class { abort() { } },
        setTimeout: () => 0,
        clearTimeout: () => { },
        fetch: () => releases instanceof Error
            ? Promise.reject(releases)
            : Promise.resolve({ ok: true, json: () => Promise.resolve(releases) }),
        document: { querySelectorAll: () => links },
    };
    vm.runInNewContext(releaseIife, context);
    await new Promise((resolve) => setImmediate(resolve));
    return links.map((link) => link.href);
}

(async () => {
    const releases = [
        { tag_name: "v1.0.1", prerelease: true, draft: false },
        { tag_name: "v1.0.2-rc1", prerelease: true, draft: false },
        { tag_name: "docling-pack-v1", prerelease: false, draft: false },
        { tag_name: "v1.0.0", prerelease: false, draft: false },
    ];
    const first = await runSelector(releases);
    if (!first.every((href) => href.includes("/releases/download/v1.0.0/"))) {
        throw new Error(`expected v1.0.0, got ${first}`);
    }

    const second = await runSelector([
        { tag_name: "v1.0.2", prerelease: false, draft: false },
        ...releases,
    ]);
    if (!second.every((href) => href.includes("/releases/download/v1.0.2/"))) {
        throw new Error(`expected v1.0.2, got ${second}`);
    }

    for (const input of [[], new Error("network rejected")]) {
        const unchanged = await runSelector(input);
        if (!unchanged.every((href) => href.includes("/releases/latest/download/"))) {
            throw new Error(`expected unchanged fallback hrefs, got ${unchanged}`);
        }
    }

    console.log("docs_release_selector: PASS");
})().catch((error) => {
    console.error(error.message);
    process.exit(1);
});
