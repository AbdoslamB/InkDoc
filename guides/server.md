# The REST API, settings and URL conversion

InkDoc's embedded FastAPI server: its endpoints, the lanes that keep conversions from
overloading the computer, settings, the page it serves, and how URLs are fetched. The public
endpoint contract is [API.md](../API.md); keep it in step with the code. The files this guide
covers are listed in AGENTS.md's routing table.

## Rules

- Every lane is acquired in async code before `asyncio.to_thread`. A conversion never blocks
  the event loop.
- Response metadata is built in one place, `_conversion_payload`.
- The save folder is never settable over HTTP (it is absent from `SettingsPayload`).
- Saved interface preferences reach the page only as values checked by
  `sanitize_ui_preferences`.
- `POST /settings` and the reset write strictly and answer `503` if `settings.json` can't be
  written; they never pretend to have saved.
- URL fetches are made once, as InkDoc. A site that refuses is never retried, disguised or
  solved: that would get around the site's access rules.
- Every hop of a URL fetch is checked against private, loopback and metadata addresses.

## How it works

### Endpoints and lanes

`server.py` holds the FastAPI REST API endpoints (`/convert/file`, `/convert/url`,
`/convert/batch`, `/convert/progress/{job_id}`, `/convert/cancel/{job_id}`, `/health`,
`/extensions`), and dual-mounts `/static` and `/ui`.

- **Lanes.** Docling (1), GLM-OCR (1), default (2) and a probe lane (1) for pdfium work, all
  acquired in async code before `asyncio.to_thread`.
- **Engines.** `/engines/{name}/…` dispatches `glm_ocr` to `GlmOcrManager`; `/convert/estimate`
  gives GLM-OCR's page count and time estimate.
- **Order of work.** Explicit engines convert → auto-save → check (missing text; tables too at
  Balanced/Thorough, report only, after the save so a late cancel can't undo it); Auto probes →
  converts → checks → (maybe re-converts) → table stage → auto-saves once.
- **Auto quality.** `auto_quality` comes from the request or the setting, the steps switched
  off from the `thorough_passes_off` setting (`_auto_quality_options`; names checked against
  the registry, `422` otherwise), and `GET /settings` adds the registry's step list
  (`thorough_passes`) for the page's switches.
- **Metadata.** Response metadata is built in one place, `_conversion_payload` (`tables`, and
  `X-Tables-Rebuilt`/`X-Tables-Warning` headers for text responses).
- **Uploads.** Files of any size are accepted; over 100 MB the server refuses an upload only
  when less than 1 GB of disk would stay free (`507`).

### Settings and the page

- `GET`/`POST /settings` and `POST /settings/reset` (the list of what is reset is
  `RESETTABLE_SETTINGS` in `engine_manager.py`; never the save folder, engine/add-on setup or
  `glm_ocr_catalogue_seen`).
- Saved interface preferences reach the page before first paint in the nonce'd script
  (`_initial_page_state`: `data-theme`, `data-hidden-engines`, `window.__INKDOC_PREFS__`), only
  as values checked by `sanitize_ui_preferences`.
- `POST /settings` and the reset write strictly (`update_settings(..., strict=True)`) and
  answer `503` if `settings.json` can't be written.
- The page's Content Security Policy is built in one place, `server.page_csp`. It forbids
  eval; [desktop.md](desktop.md) explains what that means for the desktop window.

### URL conversion

- **Fetching.** `url_fetcher.py` makes one request with connect and read timeouts, a streamed
  size cap and a limited number of manually followed redirects. `security.py` validates every
  hop against loopback, private, link-local, reserved, multicast and cloud metadata addresses,
  and the fetch connects to the validated IP so DNS can't change it in between.
- **Blocked sites.** A site that refuses a URL fetch (401, 403, 429, or a bot check:
  `cf-mitigated: challenge`) raises `UrlBlockedError` (`url_fetcher.py`, a `UrlFetchError`),
  worded by `url_fetch_failure` with how to get the page through the user's own browser; still
  `502`. Never retry, disguise the request or solve the check: that would get around the
  site's access rules.
- **JavaScript pages.** `web_shell.py` is pure, standard library only: web pages that build
  their content with JavaScript. `open_url_source()` calls `inspect_page()` on every fetched
  HTML page (no type override):
  - a page whose body shows any text is left alone (the scan stops at the first word);
  - a shell is converted from its schema.org JSON-LD (`JobPosting`, `Article` family,
    `Product`, `Event`; the readable object with the most text, at least 200 characters)
    rebuilt as a small HTML page with every value escaped and no script, frame or form, with a
    note in `warnings` (`FetchedSource.notes`);
  - a shell without, whose short `<noscript>` asks for JavaScript, raises
    `JavaScriptPageError` (`422` from `/convert/url`).

  Never inspected: YouTube, Wikipedia and Bing (MarkItDown reads their raw HTML), pages over
  5 MB, pages without a `<body>` tag. Any other failure leaves the page as fetched.

## Tests

- `tests/test_server.py`: endpoints, lanes, the session-token check, CORS.
- `tests/test_tables_api.py`, `tests/test_glm_ocr_api.py`: the tables and GLM-OCR endpoints.
- `tests/test_ui_preferences.py`, `tests/test_display_settings.py`: settings.
- `tests/test_url_fetcher.py`, `tests/test_web_shell.py`: URL conversion.

## Lessons

### An upload is already on disk when the handler runs
- **When:** you want to limit or refuse uploads.
- **Lesson:** Starlette spools the whole upload to disk before the handler runs. Refusing a
  large file by size (the old 413) saved nothing and only discarded the upload. Refuse on what
  actually matters (free disk space), and close and delete the temporary copy.
- **Evidence:** 93357aa.

### Every refusal ends its job
- **When:** an endpoint that registered a job returns early with an error.
- **Lesson:** a job left "running" stays in `GET /convert/progress` until the registry expires
  it, and the page waits on it. End the job on every error path.
- **Evidence:** bf34ffc (an upload refused as too large left its job running).
