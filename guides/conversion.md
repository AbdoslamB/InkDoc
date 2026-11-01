# Conversion core, the MarkItDown worker, saving

How a file or URL becomes Markdown and where the result is saved: `app/core/converter.py`,
the MarkItDown worker, process containment, the job registry and the save folder. The files
this guide covers are listed in AGENTS.md's routing table.

## Rules

- Every actual conversion goes through the engines' real public APIs (AGENTS.md, "What this
  is"). `run_markitdown()` is the one function that calls `MarkItDown.convert_local()`.
- InkDoc's own process never imports `markitdown`, nor the libraries kept in the worker
  (pdfplumber, onnxruntime). `tests/test_startup_imports.py` enforces it;
  `get_markitdown_version()` reads package metadata only.
- Results come back through private files the worker creates exclusively, never through the
  pipe. Auto-save stays in the server.
- A saved file is created with `O_EXCL`, so a file or link that appears at the chosen name is
  never written through. A failed write removes its own partial file.
- The save folder is set only by the desktop folder dialog. No HTTP request can set it.
- No conversion blocks the event loop, and no child process outlives InkDoc.

## How it works

### Converter

`converter.py` maps options to real `MarkItDown(...)` constructor kwargs. Every MarkItDown
conversion goes through `convert_local()`'s MarkItDown branch to
`engines/markitdown_worker_client.convert_with_markitdown()`; `run_markitdown()` runs inside
the worker (or in-process only when the worker can't start).

`auto_save_markdown()` / `get_downloads_dir()` / `unique_download_path()` implement
collision-safe auto-save into a given folder (Downloads by default). The file is created with
`O_EXCL`, so a file or link that appears at the chosen name is never written through; the
next free name is used, and a failed write removes its own partial file.

`queue_model.py` holds the `EngineKind` enum (`markitdown`, `docling`, `markit`, `glm_ocr`,
and `auto`, a routing mode that always resolves to one of the concrete engines) and the
`QueueItem` dataclass. Engine adapters live in `app/core/engines/` (`docling_engine.py`,
`markit_engine.py`; Docling and GLM-OCR are in [engines.md](engines.md)).

`jobs.py` is the in-memory job registry behind `/convert/progress/{job_id}` and
`/convert/cancel/{job_id}` (phase labels and cooperative cancel; engine-neutral).

### The MarkItDown worker

`engines/markitdown_worker.py` / `engines/markitdown_worker_client.py`. The worker is the same
executable started with `--markitdown-worker` (handled in `main.py` before the desktop app is
imported; `main.py --markitdown-worker` from source).

- **Protocol.** JSON lines over stdin/stdout, stdout moved to a dup'd fd before anything is
  imported so library output can't corrupt it (`claim_stdout`), stderr to DEVNULL. Results go
  through private files it creates exclusively, never the pipe; auto-save stays in the server.
- **Actions.** Besides `convert` it answers `tables` (the table finder's JSON,
  `find_tables_in_worker()`) and `layout` (the layout model's tables, `find_layout_in_worker()`),
  so pdfplumber and onnxruntime also stay out of InkDoc's own process.
- **Lifetime.** One worker, several conversions on threads; started on the first conversion or
  `POST /convert/warmup` (one shared start; callers wait on it), stopped after `IDLE_TIMEOUT_S`
  (5 min, `INKDOC_MARKITDOWN_IDLE_S`) with the idle clock stopped during work.
- **Cancel.** Cancel kills it when it is the only conversion and waits for it to exit (so the
  document is released before the server deletes its temp copy; Windows refuses while it is
  open); otherwise the cancelled conversion finishes and is discarded.
- **Results folder.** `inkdoc-markitdown-*` in the temp dir, checked before each use (real dir,
  ours, 0700 on POSIX), also the worker's cwd, result files 0600 with O_EXCL; folders over a
  day old are removed.
- **Failure.** A crash fails that conversion (`MarkItDownWorkerCrashedError`) and the next one
  restarts it; a worker that can't start makes conversions run in-process
  (`INKDOC_MARKITDOWN_WORKER=0` forces that).
- **Containment.** Contained by a Windows Job Object (`engines/process_guard.py`), which also
  caps its memory at half of RAM, at least 2 GB (`conversion_memory_limit()`,
  `INKDOC_CONVERSION_MEMORY_LIMIT_MB`, `0` = off); a MemoryError, also one MarkItDown wraps,
  becomes `MarkItDownOutOfMemoryError` with a sentence for the user. On macOS and Linux the
  worker exits when its stdin closes or its parent pid changes. Not `PR_SET_PDEATHSIG`: Linux
  sends it when the *thread* that spawned the child exits, and the worker is spawned from
  server threads that come and go.

### Saving

`save_location.py` is the save folder. Stored as `save_dir` in `settings.json` (`""` =
Downloads) and set only by the desktop folder dialog (`choose_save_folder` in
`app/desktop/runner.py`); it is deliberately absent from `SettingsPayload`, so no HTTP request
can set it. `save_markdown()` writes there and falls back to Downloads with a sentence saying
why (missing folder: never re-created; folder or a parent replaced by a link or junction since
it was chosen: not followed, detected because the stored path no longer resolves to itself;
write error: `OSError`/`ValueError`); the choice is kept. A window that attached to an
already-running InkDoc refuses to change it, since that process does the saving.
`folder_state()` feeds `GET /settings` and the Download dialog's starting folder.

The accepted weaknesses of saving are listed in AGENTS.md, "Known limitations (security)".

### Threading and execution model

- Conversions never block the main loop. In desktop mode, FastAPI runs in a background daemon
  thread (`uvicorn.Server`) while `pywebview` runs on the main GUI thread.
- The window no longer prewarms markitdown at load; the UI sends `POST /convert/warmup` on
  signs of intent instead.
- In the browser/UI layer (`app/ui/app.js`), document conversions are dispatched via
  asynchronous `fetch()` requests to `/convert/file` or `/convert/url`. Each queue item updates
  reactively without freezing the user interface.
- Files are collision-safely auto-saved to the save folder (`~/Downloads` by default) by the
  backend (`save_location.save_markdown()`) without requiring any manual export step.

## Tests

- `tests/test_markitdown_worker.py`: the worker, its protocol, cancel and crash handling.
- `tests/test_startup_imports.py`: what InkDoc's own process may import.
- `tests/test_charset_retry.py`: the UTF-8 retry for text files.
- `tests/test_save_location.py`: the save folder and its fallbacks.
- `tests/test_auto_engine.py` includes the `convert_item()` example from AGENTS.md.

## Lessons

### Never use PR_SET_PDEATHSIG for a child started from a server thread
- **When:** you make a child process exit together with InkDoc on Linux.
- **Lesson:** Linux sends the parent-death signal when the *thread* that started the child
  ends, not the process. The server's threads come and go, so it killed healthy workers. The
  MarkItDown worker watches its stdin and its parent pid instead.
- **Evidence:** 539fe26 (the MarkItDown worker); the docstring of
  `app/core/engines/markitdown_worker_client.py`.

### On Windows, delete a document only after every process that opened it has exited
- **When:** you delete a temporary copy of an upload, on cancel, refusal or failure.
- **Lesson:** Windows refuses to delete a file that is still open, so the delete fails and the
  copy stays behind. Cancel waits for the worker to exit before the server deletes the temp
  copy, and a refused upload closes its copy before deleting it.
- **Evidence:** 93357aa ("Also fixes the partial temp copy a refused upload left behind on
  Windows").

### Resolve relative paths before handing them to the worker
- **When:** you pass a path from InkDoc's process to the MarkItDown worker.
- **Lesson:** the worker's working directory is its private results folder, not InkDoc's, so a
  relative path means something else there. Resolve it in InkDoc first.
- **Evidence:** bf34ffc.

### A text file's charset is guessed from its start
- **When:** a CSV or other text file fails with "'ascii' codec can't decode".
- **Lesson:** MarkItDown guesses the charset from the first 64 KB, so a file that is ASCII
  there and accented later fails. `run_markitdown` retries once as UTF-8, and only when the
  failure was a decode error, no charset was given and the whole file is valid UTF-8. Keep
  every one of those conditions; a blind retry would hide real encoding errors.
- **Evidence:** 4921465; `tests/test_charset_retry.py`.
