# Changelog

All notable changes to InkDoc are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.3.1] - 2026-11-04

**If updating from inside InkDoc fails** with "CERTIFICATE_VERIFY_FAILED" (1.2.0 on
some Windows computers; 1.3.0 on a Mac, or on Linux other than Ubuntu and Debian),
download this version from the website once and install it over the one you have.
Your settings, engines and add-ons are kept, and from this version on, updates work
inside InkDoc.

### Added
- **See the original beside its result.** The new Original switch next to
  Rendered and Raw shows a PDF's pages or an image next to the converted
  Markdown, so you can check tables and numbers against the source at a glance.
  Pages are drawn only as you scroll to them, and nothing is loaded while the
  switch is off, so conversions are as fast as before.
- **Thorough reads charts, diagrams, forms and scans far better.** At the
  *Thorough* Auto quality level:
  - Charts drawn in a PDF get a table of their values, measured from their
    bars and lines against the axis labels, never estimated; a chart that
    states an average its values don't give is left out.
  - Flowcharts and diagrams keep their structure, box by box, and a page of
    several figures lists each figure's labels under its caption.
  - Scanned pages that carry an old, poor OCR text are read again from the
    image with GLM-OCR.
  - A table in a photo or scanned image comes out as a table, with its numbers
    checked against the first reading, instead of one cell per line.
  - A table too dense to read at normal speed (a page-sized directory, a
    ledger) can be read again in high detail: InkDoc offers it when your
    computer can, says about how long it takes, and lets you keep the normal
    result instead. On a 16-column directory page it found 92% of the numbers,
    where normal speed found almost none.
  - A page an engine lost most of is read again by another engine.
  - Lines broken one per paragraph are joined, and checklist boxes are marked ☐.
  - Bold, italics, footnote marks and exponents come back from the PDF's fonts.
  - With GLM-OCR, the text inside pictures is read, and with the layout model,
    so are formulas Docling left undecoded.

  Each step has its own switch in Settings → General → Auto quality → Steps,
  and everything InkDoc adds says where it came from.

### Fixed
- **Secure connections work on every Mac and on Fedora, Arch and openSUSE.** In
  1.3.0 the macOS app, and the Linux app outside Ubuntu and Debian, couldn't
  verify any website's certificate, so checking for updates, downloading Docling,
  GLM-OCR or add-ons, and converting web pages all failed with "certificate
  verify failed". InkDoc now uses the system's own certificates, or the ones it
  ships with, when they aren't where it first looks. Windows, Ubuntu and Debian
  work as before.
- **A failed update check says what to do.** When InkDoc can't verify the update
  server's certificate, Settings → Updates says so in plain words, and every
  failed check or download offers a link to download the newest version from the
  website. A new release that isn't ready for updating from inside InkDoc yet is
  reported as that, not as "No signed update information has been published".
- **New releases reach installed copies as soon as they're out.** 1.3.0 was
  published a few hours before its signed update information, so update checks
  found nothing to install. A release is now published only after that
  information is up, and a check runs on every release.
- **Table notes read correctly.** A note about one table's rows, columns or
  cell shapes said "a table' rows"; it now says "a table's rows".
- **No more pages written twice by GLM-OCR.** On a long, dense page GLM-OCR
  sometimes started the page over and wrote most of it a second time. The
  second pass is now left out, keeping any entries it added at the end, and
  the conversion notes say so.

## [1.3.0] - 2026-11-01

**Upgrading from 1.2:** your settings, engines and add-ons are kept. On Linux,
1.3.0 needs Ubuntu 24.04, Debian 13, Fedora 40 or newer; on older systems, keep
1.2.0. On a Mac, the download is now a disk image. If you use Docling, update it
afterwards in Settings → Engines → Docling: the update includes security fixes.

### Added
- **Auto quality** (Settings → General): *Fast* (the default; conversions are
  exactly as before), *Balanced* or *Thorough*. The API has a matching
  `auto_quality` setting and per-request parameter.
- **Table check (Balanced and Thorough).** After a PDF conversion, InkDoc reads
  the PDF's own tables, from their ruling lines and the text layer, and
  compares them with the result. With **Auto**, a table the engine got wrong
  is rebuilt from the PDF: side-by-side tables merged into one, tables that
  came out as loose lines or as a picture placeholder, dropped tables, shapes
  in cells left out, merged cells and stacked headers read wrongly. A table is
  rebuilt only when every word of the PDF inside it lands in one of its cells.
  Each rebuilt table is marked with a comment in the saved file
  (`<!-- inkdoc:table page=3 source=pdf-lines checked=text-layer -->`), and the
  result's notice says how many were rebuilt. Other engines are only checked,
  with a warning when a table looks wrong. Rebuilt tables are plain Markdown:
  merged cells are repeated and stacked headers flattened ("Revenue 2023").
  Shapes in cells are named by what is drawn ("[green shape]"). On InkDoc's
  table test pages, tables read exactly went from 2 to 12 of 12 with
  MarkItDown and from 3 to 10 of 12 with Docling (12 of 12 at Thorough).
  Pages with drawn lines or boxes cost about 10–260 ms each; documents without
  tables cost nothing more.
- **Thorough reads complex digital PDFs properly.** A digital PDF goes to
  Docling, which follows columns, sidebars and side-by-side tables, instead of
  MarkItDown, which interleaves them line by line. Long PDFs go in 12-page
  parts, and GLM-OCR is used when Docling isn't installed. Then InkDoc adds
  what the engine left out, from the PDF's own text:
  - the labels and values inside charts, with a labelled bar chart as a table;
  - rating grids drawn with coloured markers and ◀ ▶ arrows, as a table
    (for example `○ ○ ○ ● ○ (green)` under "Overweight");
  - whole paragraphs the engine dropped;
  - the PDF's `&`, dashes and quotes where the engine simplified them.

  On an 8-page investment report with two columns, side-by-side tables,
  rating grids and charts, Thorough found 97.5% of the PDF's words and filled
  292 table cells. GLM-OCR found 89.6% and filled 217, and MarkItDown's
  tables came out interleaved.
- **Show or hide engines** (Settings → Engines): each engine card starts with a
  **Show on main screen** switch. Turning it off removes that engine's button
  from the main screen and marks the card **Hidden**; the engine stays
  installed, **Auto** still uses it, and its card keeps every control. Auto is
  always shown. Tips to install an engine you hid are no longer shown, and
  **Switch engine** skips hidden engines.
- **Restore defaults** (Settings → General, at the bottom): puts the theme,
  engine choice and hidden engines, auto-save, Auto's tips, the Docling
  fallback, the PDF check, Auto quality and its steps, layout width, text size,
  math formulas and the daily update check back to how InkDoc starts out, after
  asking. Installed engines and add-ons and their settings, the save folder and
  converted files are kept. The API has a matching `POST /settings/reset`.
- **Tables on scanned pages (Thorough).** With GLM-OCR and the layout model,
  InkDoc finds each table on a scanned page and has GLM-OCR read it on its
  own. That reading takes the place of what GLM-OCR wrote for the whole page
  (often plain lines, side-by-side tables run together, or a shifted header)
  only when it holds the same words; a note or source line next to the table
  stays where it was. Each rebuilt table is marked in the file
  (`source=glm-ocr checked=layout,page-reading`). On InkDoc's table test pages
  scanned at 200 DPI, tables read exactly went from 4 to 9 of 13, with no
  invented cells.
- **Tables without lines (Thorough).** A borderless table that the engine
  wrote as loose text or dropped is found from the positions of the PDF's
  words. It is rebuilt only when GLM-OCR's own reading of that area agrees,
  and it keeps the PDF's exact text, so this step needs GLM-OCR installed.
  Two-column articles, tables of contents and forms are not mistaken for
  tables.
- **Page split for mixed PDFs (Thorough).** With both Docling and GLM-OCR
  installed, a PDF with both text pages and scanned pages is read page by page.
  Docling reads the text pages and GLM-OCR only the scanned ones, and the
  results are joined in page order. The result's details list which engine read
  which pages.
- **Switch single steps off** (Settings → General → Auto quality → Steps):
  one switch per step of Balanced and Thorough, all on by default. A step that
  needs an engine you haven't installed is greyed out, with a button to
  install it. The API has a matching `thorough_passes_off` setting.
- **Layout model add-on** (Settings → Add-ons): Docling's layout model
  "heron" (Apache-2.0, about 157 MB), run by InkDoc on this computer. The
  Docling engine already includes it, so with Docling installed nothing is
  downloaded.
- **What each step did** (API): `tables.passes` lists every step of the
  chosen Auto quality level, whether it ran, what it changed, or why it was
  skipped (for example "needs Docling and GLM-OCR").
- **Support InkDoc**: a small red heart at the foot of the Settings sidebar
  and a "Buy me a coffee" link in Settings → About open
  [Buy Me a Coffee](https://buymeacoffee.com/abdoslamb) in your browser,
  through the website's support page, so the address can change without an
  update. The sidebar heart is hidden on About, which has its own link.
  Nothing is loaded until you click. The website has the same heart in its
  header and footer.

### Changed
- **Less memory while idle.** MarkItDown now runs in a separate process that
  starts when you are about to convert (dragging a file over the window,
  opening the file or folder picker, clicking into the URL box, or the first
  conversion) and closes after 5 idle minutes. InkDoc itself no longer loads
  MarkItDown and its ~20 libraries, which it used to load at startup and could
  never release. Measured on Windows 11 for the whole app (InkDoc and its
  WebView2 processes, unique memory): **247 MB → 166 MB** at idle after
  startup, and **270 MB → 167 MB** once the separate process has closed after
  converting. While it runs it uses about 125 MB, all of it returned when it
  closes. Conversions give the same results as before; the first one after a
  pause can take 1–3 s longer while the process starts, unless one of the
  signs above started it already. A file that crashes MarkItDown no longer
  takes InkDoc down with it, and cancelling a MarkItDown conversion that is
  the only one running now stops it at once. If the separate process can't
  start, InkDoc converts in its own process as before.
- **Clearer Settings.** Shorter descriptions throughout, and the settings you
  use most come first. General → Conversion now reads Auto-save, Auto quality,
  then the missing-text check; Display starts with the text size. "Fall back to
  MarkItDown if Docling fails" moved into the Docling engine card. The engine
  cards follow the order of the main screen (MarkItDown, Docling, GLM-OCR,
  Markit), with each engine's Install button above its technical details.
  Docling's "Code Enrichment" and "Formula Enrichment" are now "Code
  recognition" and "Formula recognition", and under Auto quality → Steps the
  Balanced step comes first. Nothing works differently.
- **What's new, at a glance.** When an update is available, Settings → Updates
  lists five of the release's changes, the first ones of each heading (Added,
  Changed, Fixed…), each in one sentence, at the bottom of the update card, with a
  **Full release notes** link that opens the release page in your browser.
  The card has more room around its parts, and long lines wrap instead of
  making the panel scroll sideways. The notes are now the version's own
  CHANGELOG section, on the release page too, instead of GitHub's generated
  list, which was often a single link.
- If the chosen engine can't be used yet (not installed, being updated, or
  hidden), InkDoc uses **Auto** until it can, and goes back to your choice once
  it is ready. Before, it switched to MarkItDown and forgot the choice.
- Switching to the dark theme now shrinks the light theme back into the theme
  button, the reverse of the circle that grows when switching to light.
- The lines between groups of settings are easier to see in the dark theme,
  and Display, Updates and the GLM-OCR options are now divided the same way.
- The note above a converted result (a warning, a tip or tables rebuilt) has
  a **×** to close it once read. It comes back when you open the result again
  after another one, or convert the file again. A failed conversion's message
  can't be closed.
- **Files over 100 MB are converted.** They used to be refused (HTTP 413)
  after the whole file had already been uploaded. Any size is now accepted;
  InkDoc says that large files can take longer (a note when they are added
  and a "Large file" label on their row while they convert) without stopping
  anything. To keep the computer responsive with very large files:
  - the preview shows the first 5 MB of a larger result (the saved file,
    Copy and Download always have all of it);
  - on Windows, a MarkItDown conversion may use at most half of the
    computer's memory (at least 2 GB); past that it stops with a message
    saying so, instead of the computer running out of memory;
  - a file over 100 MB is refused, with a plain message (HTTP 507), only
    when converting it would leave less than 1 GB of disk free, and any
    upload the disk can't hold gets the same message instead of a server
    error.
- **Python 3.14 inside the apps** (Windows and macOS had 3.11.9, which has had
  no binary security release since April 2024; Linux had 3.10). Conversions
  give the same Markdown as before. While a document converts, InkDoc's
  conversion process uses about 20 MB more memory.
- **The Linux download is an AppImage** (`inkdoc-linux-x86_64.AppImage`, about
  110 MB instead of a 150 MB zip): make it executable and start it, nothing
  to unpack. It uses your system's own WebKit2GTK and GTK, and needs **Ubuntu
  24.04, Debian 13, Fedora 40 or newer** (glibc 2.39). On Ubuntu 22.04 and
  other older systems, keep using 1.2.0: updating from 1.2.0 there downloads
  an AppImage that won't start. From this version on, the app doesn't offer
  an update the system can't run, and a downloaded AppImage is made executable.
  Without WebKit2GTK, InkDoc opens in your web browser and says what to
  install; it keeps running until you stop it (`pkill -f inkdoc`).
- **The macOS download is a disk image** (`inkdoc-macos.dmg` instead of
  `inkdoc-macos.zip`): open it and drag InkDoc onto Applications. Updating
  from 1.2 on a Mac saves the disk image to Downloads.
- **MarkItDown 0.1.8**, a release of bug fixes. In CSV files, a `|` or a line
  break inside a value no longer breaks the table, and a byte-order mark no
  longer ends up in the first column's name. Underlined and double-struck text
  in Word files, and struck-out text on web pages, are now kept. A link with an
  encoded `/` (`%2F`) now keeps pointing where it did, and Atom feeds keep their
  formatted content. Other output is unchanged. InkDoc now pins the exact
  MarkItDown version it was tested with.

### Fixed
- The desktop app now remembers the **theme**, the **engine** you picked,
  **Auto-save** and Auto's **Don't show again** between launches. They were
  kept in browser storage, which the desktop window clears at every launch;
  they are now saved with the other settings and applied before the window
  first draws, so a light theme no longer opens dark first. Choices made in
  earlier versions of the browser Web Bench are carried over once.
- A setting that can't be written to disk (for example while an antivirus scan
  holds the file) is retried briefly, and if it still fails InkDoc now says so
  instead of losing it silently at the next launch.
- Settings changed just before closing Settings, or just before closing the
  window with InkDoc's own close button (Windows), are saved at once instead of
  after a short pause that closing the window could cut off.
- In the desktop app, a middle click on a link in a converted document, and
  any click on a link in an image map or an SVG drawing, is handled like a
  click on an ordinary link: web links open in your browser, and other links,
  such as links to files, are ignored instead of loading in InkDoc's window or
  being handed to the system to open.
- API: an upload refused because the disk is full no longer leaves its job
  reported as running by `GET /convert/progress/{job_id}`.
- On Windows, an upload that can't be stored no longer leaves a partial copy
  in the temp folder (it was deleted while still open, which Windows refuses).
- **A web page whose site blocks automated downloads** (for example a job
  board behind a Cloudflare bot check) now says so, with what to do instead:
  open the page in your browser, print it to PDF or save it, and drop that
  file into InkDoc. It used to show only "HTTP error 403". Sign-in pages
  (401) and rate limits (429) get their own message. InkDoc makes one
  request, as itself, and never retries or disguises it.
- **Web pages built with JavaScript** (job boards such as Ashby, and other
  apps that draw the page in the browser) no longer convert to a single line
  such as "You need to enable JavaScript to run this app." InkDoc runs no
  JavaScript; it now converts the copy such a page carries for search engines
  (a job posting, article, product or event) and says so. A page without one
  gets a message saying what to do (print it to PDF from your browser, then
  convert that) instead of a one-line file. Other pages are unchanged.
- Docling's **Verify Integrity** now says how many files it checked (it said
  "All undefined files verified"), and a Docling install that fails shows the
  reason instead of "Unknown error".
- A CSV or text file larger than 64 KB that has accented letters or other
  non-English characters only after its beginning (a long export with one
  "Zoë" near the end, say) now converts. MarkItDown guessed the file's
  encoding from its first 64 KB and failed with "'ascii' codec can't decode".
  InkDoc now tries such a file again as UTF-8 when it is valid UTF-8.
- **The Windows title bar behaves like any other window's.** The window can be
  resized from its top edge and top corners (only the sides, bottom and outer
  corners worked before). The whole title bar, its margins and the gaps
  between its buttons included, moves the window, and a double-click
  maximises and restores it, with Windows' own snapping. Switching the theme
  no longer turns the maximise button to the new colour before the rest of
  the title bar. A second InkDoc window now gets its own resizable border,
  Snap Layouts and maximise button, instead of these being set up on the
  first one.
- **On Linux, the window's own functions work**: the Download button's Save
  dialog, choosing a save folder in Settings, and links that open in your
  browser. The page couldn't reach them, because the window component builds
  that connection with code the page's security policy refuses to run. InkDoc
  now builds it without that, and the policy stays as strict as before.

### Security
- **Docling 2.140.0, with security fixes.** If you have Docling, update it in
  Settings → Engines → Docling. The card says how large the download is; your
  Docling keeps working until you update, a conversion in progress finishes
  first, and the code and formula recognition model is kept. Docling 2.130.0
  could be made to use all its memory by a document: a web page, HTML file or
  spreadsheet with a huge table cell span (GHSA-cgc7-9qp3-86m3), or a small
  archive with millions of entries (GHSA-3cr3-8m4c-fpxw). The update also
  brings Docling's newer reading order for PDFs and many fixes for Word
  documents (Docling engine pack 2).

## [1.2.0] - 2026-10-14

### Changed
- **Settings** now opens as a wider panel on the right with five sections:
  **General**, **Engines**, **Add-ons**, **Updates** and **About**. "Check for updates
  daily" has moved to Updates, and About has a short description of InkDoc.
  The panel closes from the gear, the **X**, **Esc** or a click outside it.
  The title bar stays usable while it is open, and dragging a file onto the
  window closes it so the file lands in the drop zone. Prompts to install or
  fix an engine open it on Engines. While it is open, notifications appear
  beside it instead of over its buttons.
- In Settings → General, thin lines now separate the unrelated preferences
  (saving, the Docling fallback and the PDF check), so each description sits
  clearly with its own switch.
- Switching between the dark and light theme now reveals the new theme as
  a circle growing from the theme button, as on the website. Where the
  system's web engine doesn't support it, or "reduce motion" is on, the
  theme switches instantly as before.

### Security
- In the desktop app, links inside converted documents and release notes no
  longer open inside InkDoc's window: web links open in your browser, and
  other links are ignored.
- The page can now call only the nine desktop functions it uses; nothing else
  in the desktop app is reachable from page scripts.
- If the save folder's location can't be checked, files are saved to
  Downloads instead.
- Release notes are shown as plain text if the HTML sanitiser is unavailable.
- The preview's HTML sanitiser, DOMPurify, is updated from 3.1.6 to 3.4.16,
  picking up the upstream security fixes released since.

### Added
- **Save folder** setting (Settings → General): choose where auto-save
  writes, instead of always using Downloads. **Change…** opens your system's
  folder dialog, the choice is remembered, and the desktop **Download** button's
  Save dialog opens there too. If the folder is missing or can't be written to
  (for example an unplugged drive), files are saved to Downloads and InkDoc
  says so; the folder is used again once it is back. The folder can only be
  chosen in the desktop app: no web page or API request can change it. API
  responses report a fallback in the new `save_fallback` field
  ([#6](https://github.com/AbdoslamB/InkDoc/issues/6)).
- **Layout width** setting (Settings → General → Display): **Standard** (up to
  1,400 px, as before), **Wide** (up to 1,920 px) or **Full** (the whole
  window). On large screens, tables, code and images get the extra room while
  paragraphs keep a readable line length; windows narrower than 1,400 px look
  the same with any choice. It is remembered between launches of the desktop
  app and applied before the window first draws, so the layout never jumps.
  `GET`/`POST /settings` report and accept it as `layout_width`.
- **Preview text size** (Settings → General → Display): **A−** and **A+** make
  the converted document's text smaller or larger in seven steps (85% to 150%),
  in both the Rendered and Raw tabs, with a live sample right below the
  buttons. Headings, code and tables keep their proportions, and 100% is
  exactly the size used before. It is remembered between launches like the
  layout width; `GET`/`POST /settings` use `preview_text_size` (85, 90, 100,
  110, 120, 135 or 150).
- **Math formulas** in the preview, and a new **Settings → Add-ons** section
  where it can be switched off. LaTeX from Docling's formula recognition and
  GLM-OCR (`$…$`, `$$…$$`, `\(…\)`, `\[…\]`) is drawn as real formulas
  with KaTeX instead of showing as mangled text. KaTeX is bundled, works
  offline and is loaded only for documents that contain math, so other
  documents, startup and saved files are unchanged. Prices such as `$5` and
  code are left alone; if ordinary text with `$` signs is ever shown as math,
  turn **Math formulas** off. `GET`/`POST /settings` use `render_math`.

### Fixed
- A wide table in the preview now scrolls sideways on its own instead of
  moving the whole document, and large pictures shrink to fit the preview.
- Every switch in Settings can now be reached with Tab and flipped with
  Space, and keyboard focus in Settings is visible on macOS and Linux too.

## [1.1.1] - 2026-10-14

### Fixed
- The **Download** button in the desktop app now opens a Save dialog (starting
  in Downloads) and writes the `.md` file where you choose. It previously did
  nothing while showing "Downloaded", because the desktop window blocks
  in-page downloads. The message now only reports a save that actually
  happened. The browser version is unchanged
  ([#6](https://github.com/AbdoslamB/InkDoc/issues/6)).

## [1.1.0] - 2026-10-01

### Added
- **GLM-OCR engine**, an optional download (~1.45 GB) from Settings → Processing
  Engines: the zai-org GLM-OCR model (MIT), run locally by llama.cpp's
  `llama-server` (MIT), for dense scans, phone photos of documents, math and
  complex tables. Clicking **Download** first shows a warning with the download
  size and the slower processing time, and nothing is downloaded until it is
  confirmed. Files come from the official source first (Hugging Face, llama.cpp's
  GitHub releases) and from InkDoc's own GitHub mirror if that fails for any
  reason, including a wrong hash; every file is checked against SHA-256 hashes
  pinned in InkDoc, and the runtime is re-hashed before every launch. The install
  only completes once the model reads a test image on the computer, which also
  measures its speed there. Optional GPU acceleration (Vulkan on Windows/Linux for
  NVIDIA, AMD and Intel graphics, Metal on Apple Silicon) is kept only when it
  beats the CPU. Documents never leave the computer: the server is loopback-only
  with a per-session key and has downloads disabled. Also: a precision choice
  (Q8, or F16 for +0.8 GB), Verify, Remove, Self-test again, "InkDoc mirror only"
  for networks that block Hugging Face, and "Update available" when newer tested
  pins exist (never automatic). API: `engine=glm_ocr`, `/engines/glm_ocr/…`,
  `POST /convert/estimate`, `glm_ocr_available` in `/health`, and a `warnings`
  field (`X-Engine-Warnings`).
- **Per-page progress for GLM-OCR**: the queue shows *"Page 7 / 30 · ~8 min left"*,
  Cancel stops before the next page and saves nothing, and InkDoc asks before a
  GLM-OCR job longer than 5 minutes (once for a whole folder drop). A speed hint
  under the pills shows the measured seconds per page.
- **Auto uses GLM-OCR** for scans and images when it is installed and the file
  should finish within 5 minutes on this computer; otherwise it uses Docling, and
  says why. GIF images, which Docling cannot read, go to GLM-OCR when installed.
- A newer *signed* GLM-OCR catalogue published on GitHub can bring newer tested
  model and runtime pins without an app release (Ed25519, same keys as app
  updates, rollback blocked), checked only when the GLM-OCR card is opened.
- The Windows uninstaller asks whether to also remove downloaded engines and
  models (Docling, GLM-OCR), showing their size, and keeps them by default.
- **Auto engine**, a fourth pill and the default for new users (a saved engine
  choice is kept; the API default stays `markitdown`). Auto picks the engine per
  file with a quick check of the PDF's text layer: MarkItDown for digital
  documents, Docling for scanned pages and images when it is installed, and Markit
  for YAML and XML. If MarkItDown's result is missing text and Docling is
  installed, Auto converts again with Docling and keeps the better result. Every
  choice is reported in the queue, the preview and the API (`engine=auto`, `auto`
  field, `X-Auto-Reason`). Auto only uses engines already on disk: it never
  downloads a model, and suggests installing Docling instead.
- **Missing-text check** for PDFs, with every engine and for URLs that serve a PDF.
  The result is compared with the PDF's own text, and a warning names the likely
  cause (scanned pages, unreadable font encoding, a dropped table) with a
  one-click "Re-convert with …" fix and a Details list. An empty scanned PDF now
  says it is scanned instead of a bare "Empty". The check never changes or delays
  the saved file, can be turned off in Settings ("Check PDFs for missing text"),
  and is available to API clients as the `quality` field and the `check_quality`
  option.
- Live progress and Cancel for conversions: the queue shows each phase
  ("Checking…", "Retrying with Docling…"), and Cancel stops before the next phase
  without saving anything. API: optional `job_id`,
  `GET /convert/progress/{job_id}` and `POST /convert/cancel/{job_id}`.
- A summary toast when a multi-file drop finishes, with the number of warnings
  and failures.
- `scripts/calibrate_quality_check.py` measures the check and Auto's routes on a
  local document corpus.

### Changed
- The engine pills form a proper radio group: arrow keys move the selection, and
  screen readers announce the checked engine. Warnings are announced politely and
  carry text, not just an icon or colour.
- Re-converting one item with another engine (Retry, Switch Engine, or a warning's
  fix) no longer changes the engine selected for new files.
- Re-converting a URL with Switch Engine now reports the engine used, fallbacks
  and warnings, like any other conversion.
- `engine_used` (and `X-Engine-Used`) now names the engine that actually produced
  the result. A file the Markit route hands to MarkItDown (PDF and Office files on
  Windows) is reported as `markitdown`, where 1.0.0 reported `markit`.
- `/convert/url` downloads the page before taking a conversion slot, so a slow
  download no longer holds up other conversions.
- `pypdfium2` is listed in `requirements.txt` (it was already installed and
  bundled through MarkItDown's PDF support). So is `Pillow`, which GLM-OCR uses
  directly (also already bundled).
- Missing-text warnings name whichever OCR engine is installed or installable, so
  a GLM-OCR result with missing text suggests MarkItDown rather than itself.
- The engine processes (Docling worker, GLM-OCR server) are stopped when the window
  closes or `--headless` is stopped.

### Fixed
- Files converted through the app or `/convert/file` were saved to Downloads under
  the server's temporary name (`tmpab12cd.md`) instead of their own (`report.md`).
- A URL download that failed part way (size cap, timeout) left its temporary file
  behind.
- Every HTTP 409 was reported as "Docling is not installed", including "update in
  progress".
- Clicking the Docling pill before Docling is installed opened Settings and closed
  it again straight away.
- An earlier toast's timer could hide a newer toast early.
- Uninstalling on Windows left the downloaded Docling pack (~1 GB) behind with no
  way to remove it short of deleting the folder by hand.
- The release workflow finds the Inno Setup compiler wherever version 6 or 7 is
  installed, and CI now compiles `installer.iss` on every run.
- Publishing an enrichment add-on release no longer marks it as the Latest
  release. The in-app updater reads `/releases/latest/download/`, so an add-on
  holding Latest would have cut off every installed copy's update check until the
  next app release.

## [1.0.0] - 2026-09-26

Initial release.

### Conversion
- Three conversion engines, switchable per item from the UI or per request from
  the API:
  - **MarkItDown** (Microsoft), bundled: Word, Excel, PowerPoint, PDF, HTML, CSV,
    JSON, XML, images and audio.
  - **Docling** (IBM Research), an optional in-app engine pack: layout analysis,
    OCR and TableFormer table reconstruction for complex and scanned PDFs.
  - **Markit**, bundled: EPUB, Jupyter notebooks, YAML and RSS/Atom feeds.
- Drop a file or folder, paste a URL, or browse: conversion starts immediately and
  the Markdown is saved to `~/Downloads` with collision-safe names. No export step.
- An engine is never substituted silently. Falling back from Docling to MarkItDown
  happens only when enabled in Settings, and is reported on the result.
- Optional Code & Formula Recognition add-on for Docling, installed from Settings,
  which transcribes code blocks and formulas from page images and labels each code
  block's language.

### Application
- One interface for the desktop app (Edge WebView2 on Windows, WebKit on macOS,
  WebKitGTK on Linux) and the local web workbench at
  `http://127.0.0.1:13118/InkDoc`.
- Live rendered and raw Markdown preview, with dark and light themes.
- Local REST API (`/convert/file`, `/convert/url`, `/convert/batch`, `/health`,
  `/extensions`) with OpenAPI documentation at `/docs`, and a headless server mode
  (`python main.py --headless`).
- Docling runs in an isolated worker process, from an engine pack checked against
  a pinned manifest of per-file SHA-256 hashes.

### Security
- In-app updates are verified against an Ed25519-signed manifest before anything is
  downloaded, and are refused for any version not newer than the one installed.
- URL conversion validates targets against SSRF, and the local server enforces
  Host and Origin checks and a per-session token on management endpoints.
- Release assets carry GitHub build provenance attestations and published SHA-256
  checksums.

### Distribution
- Windows installer (`inkdoc-setup.exe`), portable executable (`inkdoc.exe`) and
  portable ZIP; macOS (Apple Silicon) bundle; Linux (x86_64) bundle.

[Unreleased]: https://github.com/AbdoslamB/InkDoc/compare/v1.3.1...HEAD
[1.3.1]: https://github.com/AbdoslamB/InkDoc/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/AbdoslamB/InkDoc/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/AbdoslamB/InkDoc/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/AbdoslamB/InkDoc/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/AbdoslamB/InkDoc/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/AbdoslamB/InkDoc/releases/tag/v1.0.0
