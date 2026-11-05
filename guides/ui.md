# The shared UI

`app/ui/` is the single shared web and desktop UI: the desktop window and the local Web Bench
load the same files from the same server. Plain HTML, CSS custom properties and modern
JavaScript, with no build step. The files this guide covers are listed in AGENTS.md's routing
table.

## Rules

- One UI for both the desktop window and the Web Bench. Nothing forks per shell; desktop-only
  features check for `pywebview.api` (see [desktop.md](desktop.md)).
- Interface preferences live in `settings.json`, never in browser storage: the desktop window
  clears browser storage at every launch.
- A converted document is untrusted: every preview render goes through DOMPurify.
- Third-party libraries are bundled in `app/ui/vendor/`, never fetched at runtime, every file
  pinned by SHA-256 in `vendor/VENDOR.json`, and never line-ending converted
  (`.gitattributes`).
- Downloadable add-ons may only ever be data, never JavaScript that runs in the page.
- The page's Content Security Policy forbids eval (`server.page_csp`). Don't loosen it to make
  a script work.
- Every control works from the keyboard, in WebView2 and in WebKit.

## How it works

### Files

- `index.html`: application DOM structure, dropzone, engine pills, live preview pane, Settings
  drawer (General / Engines / Add-ons / Updates / About; it sits below the header so the title
  bar stays usable, and every engine prompt opens it on Engines).
- `style.css`: Inkbench design system: obsidian dark & light themes, glowing engine selector
  pills, responsive layout.
- `app.js`: client-side logic: drag-and-drop, API conversion, live markdown preview
  (`marked.js`), auto-save toggle, engine switching.
- `vendor/`: bundled third-party JS/CSS/fonts (marked, DOMPurify, highlight.js, KaTeX). Never
  fetched at runtime; every file pinned by SHA-256 in `vendor/VENDOR.json`
  (`tests/test_vendor_manifest.py`); never line-ending converted (`.gitattributes`).
- `logo.svg`, `favicon.svg`: brand assets.

### Preferences

Interface preferences (theme, engine choice, hidden engines, auto-save, Auto's tip) live in
`settings.json`, never in browser storage (the desktop window clears it at every launch):

- the `Preferences` block reads them from the page and saves changes in one grouped
  `POST /settings` after 250 ms, one at a time;
- `importLegacyPrefs` moves InkDoc 1.2's browser-storage values once;
- `preferredEngine` is the saved choice, `selectedEngine` the one in use (Auto while the choice
  is hidden or not usable; automatic changes go through `selectEngine` and are never saved);
- hidden pills are hidden by CSS from `data-hidden-engines` on `<html>`, apart from
  `.is-hidden` (not offered here).

How the server puts them in the page before first paint is in [server.md](server.md).

### The preview

- **Parsing.** Every preview parse goes through `markdownToHtml`: with `render_math` off, or
  no `$`, `\(` or `\[` in the document, it is the global `marked.parse`, unchanged; otherwise a
  separate `marked.Marked` instance with the math rules. `typesetMath` lazy-loads KaTeX and
  draws formulas in slices.
- **Large results.** The preview draws at most `PREVIEW_MAX_CHARS` (5 MB) of a result
  (`previewSlice`); a cut Raw view is read-only and Copy/Download take the whole result
  (`fullMarkdownFor`).
- **Large files.** Files of any size are accepted; over 100 MB the page only warns
  (`largeFileNotice`, the row's chip). The server's side is in [server.md](server.md).
- **Links.** In the desktop app, links in a document go through `handleContentLinkClick`; see
  [desktop.md](desktop.md).
- **Original view.** The Original switch (`btnSplitView`) shows the source beside the result:
  images from `item.fileRef` in an `<img>`, PDFs as page images from `/preview/pdf`
  ([server.md](server.md)), drawn only near the view, two at a time, and let go far from it.
  Never a frame, object or PDF viewer: the policy stays as it is. Nothing loads while it is
  off.

### Add-ons in Settings

Built-in add-ons are bundled code switched by a setting (math formulas → `render_math`).
Downloadable add-ons may only ever be data (models, fonts, dictionaries) through the signed
`AddonManager` catalogue, never JavaScript that runs in the page. Its downloadable add-on is the
layout model (`layoutAddon*` ids, `/addons/layout_model/*`); the `addon*` ids are the Docling
recognition model's card under Engines.

The support links open `SUPPORT_URL`, the website's `/support/` page; see
[release.md](release.md).

## Tests

The Node tests (`node tests/<name>.test.js`, no npm) check `app.js` and `index.html` without a
browser (`tests/docs_release_selector.test.js` checks the website instead; see
[release.md](release.md)). Each covers one feature, named in its file name: preferences, engine visibility, reset to
defaults, theme toggle, settings drawer, layout width, text size, math rendering, preview links,
word count, tables, Auto's steps, large files, notices, release notes, save folder, desktop
download, support link, the Original view, and the engine and add-on cards. The full list is in AGENTS.md's
commands.

On the Python side: `tests/test_vendor_manifest.py` (the pinned libraries) and
`tests/test_ui_preferences.py`.

## Lessons

### Hide a form control visually, never with display: none
- **When:** you restyle a checkbox or other input, such as the toggle switches.
- **Lesson:** `display: none` takes the control out of the keyboard entirely, so Tab skips
  every switch. Hide it visually only, and show the focus ring on the visible part.
- **Evidence:** b88521a.

### WebKit shows :focus-visible only after focus that already showed it
- **When:** script moves the focus (a drawer, a dialog) and the focus ring should appear.
- **Lesson:** WebKit (macOS and Linux windows) shows `:focus-visible` for script-moved focus
  only when the previous focus showed it, so after a mouse click no ring appears. The drawer
  carries `.keyboard-nav` while used from the keyboard and shows the ring on `:focus`. Test
  focus behaviour in WebKit, not only in WebView2.
- **Evidence:** b88521a.
