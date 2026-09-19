/**
 * InkDoc Web Bench — Client-side JavaScript
 */
document.addEventListener("DOMContentLoaded", () => {
  // ─── State ───────────────────────────────────────────────────────────────
  let queue = [];
  let activeItemId = null;
  let autoSaveToDownloads = false;
  let selectedEngine = localStorage.getItem("inkdoc-engine") || localStorage.getItem("markitdown-engine") || "markitdown";

  // Base path resolution for /InkDoc, /MarkItDown, or root mounting
  const pathLower = window.location.pathname.toLowerCase();
  const apiBase = pathLower.startsWith("/inkdoc") ? "/InkDoc" : (pathLower.startsWith("/markitdown") ? "/MarkItDown" : "");

  // ─── DOM Elements ────────────────────────────────────────────────────────
  const dropZone          = document.getElementById("dropZone");
  const fileInput         = document.getElementById("fileInput");
  const folderInput       = document.getElementById("folderInput");
  const btnBrowseFiles    = document.getElementById("btnBrowseFiles");
  const btnBrowseFolder   = document.getElementById("btnBrowseFolder");
  const urlInput          = document.getElementById("urlInput");
  const btnConvertUrl     = document.getElementById("btnConvertUrl");
  const autoSaveToggle    = document.getElementById("autoSaveToggle");
  const themeToggle       = document.getElementById("themeToggle");
  const settingsBtn       = document.getElementById("settingsBtn");
  const settingsPopover   = document.getElementById("settingsPopover");
  const connectionErrorBar   = document.getElementById("connectionErrorBar");
  const connectionErrorMsg   = document.getElementById("connectionErrorMsg");

  // Single unified engine pill selector
  const pillMarkitdown    = document.getElementById("pillMarkitdown");
  const pillDocling       = document.getElementById("pillDocling");
  const pillMarkit        = document.getElementById("pillMarkit");
  const allEnginePills    = [pillMarkitdown, pillDocling, pillMarkit];

  const workbenchSection  = document.getElementById("workbenchSection");
  const queueList         = document.getElementById("queueList");
  const queueCount        = document.getElementById("queueCount");
  const btnClearQueue     = document.getElementById("btnClearQueue");

  const tabRendered       = document.getElementById("tabRendered");
  const tabRaw            = document.getElementById("tabRaw");
  const renderedContainer = document.getElementById("renderedContainer");
  const rawContainer      = document.getElementById("rawContainer");
  const renderedOutput    = document.getElementById("renderedOutput");
  const rawEditor         = document.getElementById("rawEditor");

  const previewFilename   = document.getElementById("previewFilename");
  const previewStats      = document.getElementById("previewStats");
  const btnSwitchEngine   = document.getElementById("btnSwitchEngine");
  const btnCopyMarkdown   = document.getElementById("btnCopyMarkdown");
  const copyText          = document.getElementById("copyText");
  const btnDownloadMarkdown = document.getElementById("btnDownloadMarkdown");
  const toastNotification   = document.getElementById("toastNotification");

  // Conversion Notice Bar elements
  const conversionNoticeBar    = document.getElementById("conversionNoticeBar");
  const conversionNoticeIcon   = document.getElementById("conversionNoticeIcon");
  const conversionNoticeTitle  = document.getElementById("conversionNoticeTitle");
  const conversionNoticeDetail = document.getElementById("conversionNoticeDetail");

  // ─── Engine Helpers ──────────────────────────────────────────────────────
  function getEngineDisplayName(engine) {
    if (engine === "docling") return "Docling";
    if (engine === "markit")  return "Markit";
    return "MarkItDown";
  }

  function getNextEngine(current) {
    if (current === "markitdown") return "docling";
    if (current === "docling")    return "markit";
    return "markitdown";
  }

  // ─── Error Classification ────────────────────────────────────────────────
  /**
   * Returns 'unsupported' when the engine rejected the file format,
   * 'failed' for all other hard conversion failures.
   */
  function classifyError(message) {
    const msg = (message || "").toLowerCase();
    if (
      msg.includes("unsupported format") ||
      msg.includes("not supported") ||
      msg.includes("file format not allowed") ||
      msg.includes("no converter attempted") ||
      msg.includes("format none does not match") ||
      msg.includes("format is not supported")
    ) {
      return "unsupported";
    }
    return "failed";
  }

  // ─── Conversion Notice Bar ──────────────────────────────────────────────
  const ICONS = {
    failed:      `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>`,
    unsupported: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"></line></svg>`,
    empty:       `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line></svg>`,
  };

  function showConversionNotice(type, title, detail) {
    conversionNoticeBar.className = `conversion-notice notice-${type} visible`;
    conversionNoticeIcon.innerHTML = ICONS[type] || ICONS.failed;
    conversionNoticeTitle.textContent = title;
    conversionNoticeDetail.textContent = detail;
  }

  function hideConversionNotice() {
    conversionNoticeBar.className = "conversion-notice";
    conversionNoticeTitle.textContent = "";
    conversionNoticeDetail.textContent = "";
    conversionNoticeIcon.innerHTML = "";
  }

  // Update the single pill group to reflect selectedEngine
  function updateEngineUI() {
    allEnginePills.forEach((btn) => {
      if (btn) btn.classList.toggle("active", btn.dataset.engine === selectedEngine);
    });
  }

  function setEngine(engine) {
    selectedEngine = engine;
    localStorage.setItem("inkdoc-engine", engine);
    updateEngineUI();
  }

  // Bind unified pill clicks
  allEnginePills.forEach((btn) => {
    if (btn) btn.addEventListener("click", () => setEngine(btn.dataset.engine));
  });

  // Initialize
  updateEngineUI();

  // ─── Settings Popover ────────────────────────────────────────────────────
  settingsBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    settingsPopover.classList.toggle("open");
  });

  document.addEventListener("click", (e) => {
    if (!settingsPopover.contains(e.target) && e.target !== settingsBtn) {
      settingsPopover.classList.remove("open");
    }
  });

  // ─── Health Check ────────────────────────────────────────────────────────
  async function checkServerHealth() {
    try {
      const resp = await fetch(`${apiBase}/health`);
      if (resp.ok) {
        const data = await resp.json();
        const isDoclingAvailable = Boolean(data.docling_available);
        const isMarkitAvailable  = Boolean(data.markit_available);

        // Hide error banner on success
        connectionErrorBar.classList.remove("visible");

        // Populate per-engine version spans in settings popover
        const versionMarkitdown = document.getElementById("versionMarkitdown");
        const versionDocling    = document.getElementById("versionDocling");
        const versionMarkit     = document.getElementById("versionMarkit");
        const engineCardDocling = document.getElementById("engineCardDocling");

        if (versionMarkitdown) versionMarkitdown.textContent = data.markitdown_version ? `v${data.markitdown_version}` : "";
        if (versionDocling)    versionDocling.textContent    = isDoclingAvailable ? `v${data.docling_version}` : "not installed";
        if (versionMarkit)     versionMarkit.textContent     = isMarkitAvailable  ? `v${data.markit_version}`  : "not installed";

        // Dim Docling pill if not installed
        if (!isDoclingAvailable && pillDocling) {
          pillDocling.title += "\n⚠ Not installed — run: pip install docling";
        }
      } else {
        connectionErrorBar.classList.add("visible");
        connectionErrorMsg.textContent = `API returned an error (${resp.status}). Make sure the server is running correctly.`;
      }
    } catch {
      connectionErrorBar.classList.add("visible");
      connectionErrorMsg.textContent = "Unable to reach the API server — make sure it is running on port 13118.";
    }
  }
  checkServerHealth();

  // ─── Theme Toggle ────────────────────────────────────────────────────────
  const savedTheme = localStorage.getItem("inkdoc-theme") || localStorage.getItem("markitdown-theme") || "dark";
  document.documentElement.setAttribute("data-theme", savedTheme);

  themeToggle.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("inkdoc-theme", next);
  });

  // ─── Auto-save Toggle ────────────────────────────────────────────────────
  autoSaveToggle.addEventListener("change", (e) => {
    autoSaveToDownloads = e.target.checked;
    showToast(
      autoSaveToDownloads
        ? "Converted files will also be saved to ~/Downloads"
        : "Auto-save to Downloads disabled",
      "info"
    );
  });

  // ─── File Browsing ───────────────────────────────────────────────────────
  btnBrowseFiles.addEventListener("click", () => fileInput.click());
  btnBrowseFolder.addEventListener("click", () => folderInput.click());

  fileInput.addEventListener("change", (e) => {
    if (e.target.files && e.target.files.length > 0) {
      handleIncomingFiles(Array.from(e.target.files));
      fileInput.value = "";
    }
  });

  folderInput.addEventListener("change", (e) => {
    if (e.target.files && e.target.files.length > 0) {
      handleIncomingFiles(Array.from(e.target.files));
      folderInput.value = "";
    }
  });

  // ─── Drag and Drop ───────────────────────────────────────────────────────
  ["dragenter", "dragover"].forEach((eventName) => {
    window.addEventListener(eventName, (e) => {
      e.preventDefault();
      dropZone.classList.add("drag-over");
    });
  });

  ["dragleave", "dragend"].forEach((eventName) => {
    window.addEventListener(eventName, (e) => {
      e.preventDefault();
      if (e.clientX === 0 || e.clientY === 0) {
        dropZone.classList.remove("drag-over");
      }
    });
  });

  window.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      handleIncomingFiles(Array.from(e.dataTransfer.files));
    }
  });

  // ─── URL Conversion ──────────────────────────────────────────────────────
  btnConvertUrl.addEventListener("click", () => handleUrlSubmission());
  urlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") handleUrlSubmission();
  });

  function handleUrlSubmission() {
    const url = urlInput.value.trim();
    if (!url) return;
    urlInput.value = "";
    convertUrlItem(url);
  }

  // ─── Queue Management ────────────────────────────────────────────────────
  function handleIncomingFiles(files) {
    if (files.length === 0) return;
    showWorkbench();

    files.forEach((file) => {
      const item = {
        id:       "item-" + Math.random().toString(36).substr(2, 9),
        name:     file.name,
        engine:   selectedEngine,
        fileRef:  file,
        status:   "pending",
        markdown: "",
        error:    "",
        savedTo:  "",
      };
      queue.unshift(item);
      renderQueue();
      convertFileItem(item, file);
    });
  }

  async function convertFileItem(item, file) {
    item.status = "converting";
    renderQueue();

    const formData = new FormData();
    formData.append("file", file);

    const engineToUse = item.engine || selectedEngine;
    const params = new URLSearchParams({
      save_to_downloads: autoSaveToDownloads.toString(),
      response_format:   "json",
      engine:            engineToUse,
    });

    try {
      const resp = await fetch(`${apiBase}/convert/file?${params.toString()}`, {
        method: "POST",
        body:   formData,
      });

      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({ detail: resp.statusText }));
        const msg = errData.detail || "Conversion error";
        const errType = classifyError(msg);
        item.status    = errType; // "unsupported" or "failed"
        item.error     = msg;
        item.errorType = errType;
        selectItem(item.id);
        showToast(
          errType === "unsupported"
            ? `${getEngineDisplayName(engineToUse)} does not support this format`
            : `Failed: ${item.name}`,
          "error"
        );
        return;
      }

      const data = await resp.json();
      item.engine   = data.engine || engineToUse;
      item.markdown = data.markdown;
      item.savedTo  = data.saved_to_downloads || "";

      // Detect empty / blank output (converted OK but no text extracted)
      if (!data.markdown || data.markdown.trim() === "") {
        item.status    = "empty";
        item.errorType = "empty";
        selectItem(item.id);
        showToast(`No text extracted from "${item.name}" — try a different engine`, "info");
      } else {
        item.status = "saved";
        selectItem(item.id);
        showToast(`Converted "${item.name}" via ${getEngineDisplayName(item.engine)}`, "success");
      }
    } catch (err) {
      const errType = classifyError(err.message);
      item.status    = errType;
      item.error     = err.message;
      item.errorType = errType;
      selectItem(item.id);
      showToast(
        errType === "unsupported"
          ? `${getEngineDisplayName(engineToUse)} does not support this format`
          : `Failed: ${item.name}`,
        "error"
      );
    } finally {
      renderQueue();
    }
  }

  async function convertUrlItem(url, forcedEngine = null) {
    showWorkbench();

    const engineToUse = forcedEngine || selectedEngine;
    const item = {
      id:        "item-" + Math.random().toString(36).substr(2, 9),
      name:      url,
      engine:    engineToUse,
      sourceUrl: url,
      status:    "converting",
      markdown:  "",
      error:     "",
      savedTo:   "",
    };
    queue.unshift(item);
    renderQueue();

    const payload = {
      url:               url,
      save_to_downloads: autoSaveToDownloads,
      engine:            engineToUse,
    };

    try {
      const resp = await fetch(`${apiBase}/convert/url?response_format=json`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(payload),
      });

      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({ detail: resp.statusText }));
        const msg = errData.detail || "URL conversion error";
        const errType = classifyError(msg);
        item.status    = errType;
        item.error     = msg;
        item.errorType = errType;
        selectItem(item.id);
        showToast(
          errType === "unsupported"
            ? `${getEngineDisplayName(engineToUse)} does not support this URL type`
            : `Failed: ${msg}`,
          "error"
        );
        return;
      }

      const data = await resp.json();
      item.engine   = data.engine || engineToUse;
      item.markdown = data.markdown;
      item.savedTo  = data.saved_to_downloads || "";

      if (!data.markdown || data.markdown.trim() === "") {
        item.status    = "empty";
        item.errorType = "empty";
        selectItem(item.id);
        showToast(`No content extracted — try a different engine`, "info");
      } else {
        item.status = "saved";
        selectItem(item.id);
        showToast(`URL converted via ${getEngineDisplayName(item.engine)}`, "success");
      }
    } catch (err) {
      const errType = classifyError(err.message);
      item.status    = errType;
      item.error     = err.message;
      item.errorType = errType;
      selectItem(item.id);
      showToast(
        errType === "unsupported"
          ? `${getEngineDisplayName(engineToUse)} does not support this URL type`
          : `Failed: ${err.message}`,
        "error"
      );
    } finally {
      renderQueue();
    }
  }

  // ─── Render Queue ────────────────────────────────────────────────────────
  function renderQueue() {
    queueCount.textContent = queue.length;

    if (queue.length === 0) {
      queueList.innerHTML = `<div style="text-align:center;color:var(--muted);padding:20px;font-size:13px;">No items in queue</div>`;
      return;
    }

    queueList.innerHTML = queue
      .map((item) => {
        const isSelected = item.id === activeItemId;

        let badgeClass = "status-pending";
        let badgeText  = "Pending";

        if (item.status === "converting") {
          badgeClass = "status-converting";
          badgeText  = "Converting…";
        } else if (item.status === "saved") {
          badgeClass = "status-saved";
          badgeText  = item.savedTo ? "Saved" : "Done";
        } else if (item.status === "failed" || item.status === "error") {
          badgeClass = "status-error";
          badgeText  = "Failed";
        } else if (item.status === "unsupported") {
          badgeClass = "status-unsupported";
          badgeText  = "Unsupported";
        } else if (item.status === "empty") {
          badgeClass = "status-empty";
          badgeText  = "Empty";
        }

        // Show engine badge only when it differs from the currently selected engine
        const engineTag = item.engine !== selectedEngine
          ? `<span class="badge-queue-engine ${item.engine}">${getEngineDisplayName(item.engine)}</span>`
          : "";

        return `
        <div class="queue-item ${isSelected ? "selected" : ""}" data-id="${item.id}">
          <div class="item-main">
            <div class="item-icon">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                <polyline points="14 2 14 8 20 8"></polyline>
              </svg>
            </div>
            <div class="item-info">
              <div class="item-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>
              ${engineTag ? `<div class="item-sub">${engineTag}</div>` : ""}
            </div>
          </div>
          <span class="status-pill ${badgeClass}">${badgeText}</span>
        </div>`;
      })
      .join("");

    // Attach click handlers
    document.querySelectorAll(".queue-item").forEach((el) => {
      el.addEventListener("click", () => selectItem(el.getAttribute("data-id")));
    });
  }

  // ─── Select & Display Item ───────────────────────────────────────────────
  function selectItem(id) {
    activeItemId = id;
    const item = queue.find((i) => i.id === id);
    if (!item) return;

    previewFilename.textContent = item.name;

    // Update re-convert button tooltip
    if (btnSwitchEngine) {
      if (item.fileRef || item.sourceUrl) {
        btnSwitchEngine.style.display = "inline-flex";
        const nextEng = getNextEngine(item.engine);
        btnSwitchEngine.title = `Re-convert with ${getEngineDisplayName(nextEng)}`;
      } else {
        btnSwitchEngine.style.display = "none";
      }
    }

    // Always reset the notice bar first
    hideConversionNotice();

    const engineLabel = getEngineDisplayName(item.engine || selectedEngine);
    const ext = (item.name || "").split(".").pop().toUpperCase() || "file";

    if (item.status === "unsupported") {
      showConversionNotice(
        "unsupported",
        `${ext} format is not supported by ${engineLabel}`,
        `${engineLabel} cannot convert .${ext.toLowerCase()} files. Try switching to a different engine using the ↺ button above.`
      );
      renderedOutput.innerHTML = `<div class="empty-preview-hint"><svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"></line></svg><p>This format is not supported by ${escapeHtml(engineLabel)}.</p></div>`;
      rawEditor.value = "";
      previewStats.textContent = "";
      renderQueue();
      return;
    }

    if (item.status === "failed" || item.status === "error") {
      const errMsg = item.error || "An unexpected error occurred during conversion.";
      showConversionNotice(
        "failed",
        `Conversion failed — ${engineLabel}`,
        errMsg
      );
      renderedOutput.innerHTML = `<div class="empty-preview-hint"><svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg><p>Conversion failed. See the notice above for details.</p></div>`;
      rawEditor.value = `Error: ${errMsg}`;
      previewStats.textContent = "";
      renderQueue();
      return;
    }

    if (item.status === "empty") {
      showConversionNotice(
        "empty",
        `No text extracted from this file by ${engineLabel}`,
        `${engineLabel} processed the file successfully, but found no text to extract. This can happen with image-only files or unsupported content types. Try switching engines with the ↺ button.`
      );
      renderedOutput.innerHTML = `<div class="empty-preview-hint"><svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line></svg><p>The file was processed, but no text content was found.</p></div>`;
      rawEditor.value = "";
      previewStats.textContent = "";
      renderQueue();
      return;
    }

    const md = item.markdown || "";
    rawEditor.value = md;

    // Word count
    const words = md.trim() ? md.trim().split(/\s+/).length : 0;
    previewStats.textContent = words > 0 ? `${words.toLocaleString()} words` : "";

    // Rendered HTML
    if (window.marked) {
      renderedOutput.innerHTML = window.marked.parse(md);
    } else {
      renderedOutput.innerHTML = renderBasicMarkdown(md);
    }

    renderQueue();
  }

  // ─── Switch Engine (Re-convert) ──────────────────────────────────────────
  if (btnSwitchEngine) {
    btnSwitchEngine.addEventListener("click", () => {
      const item = queue.find((i) => i.id === activeItemId);
      if (!item) return;

      const altEngine = getNextEngine(item.engine);
      item.engine = altEngine;

      if (item.fileRef) {
        convertFileItem(item, item.fileRef);
      } else if (item.sourceUrl) {
        item.status = "converting";
        renderQueue();

        fetch(`${apiBase}/convert/url?response_format=json`, {
          method:  "POST",
          headers: { "Content-Type": "application/json" },
          body:    JSON.stringify({
            url:               item.sourceUrl,
            save_to_downloads: autoSaveToDownloads,
            engine:            altEngine,
          }),
        })
          .then(async (resp) => {
            if (!resp.ok) {
              const err = await resp.json().catch(() => ({ detail: resp.statusText }));
              throw new Error(err.detail || "URL conversion error");
            }
            return resp.json();
          })
          .then((data) => {
            item.status   = "saved";
            item.markdown = data.markdown;
            item.savedTo  = data.saved_to_downloads || "";
            selectItem(item.id);
            showToast(`Re-converted with ${getEngineDisplayName(altEngine)}`, "success");
          })
          .catch((err) => {
            item.status = "error";
            item.error  = err.message;
            selectItem(item.id);
            showToast(`Failed: ${err.message}`, "error");
          })
          .finally(() => renderQueue());
      }
    });
  }

  // ─── Copy & Download ─────────────────────────────────────────────────────
  btnCopyMarkdown.addEventListener("click", async () => {
    const item = queue.find((i) => i.id === activeItemId);
    if (!item || !item.markdown) {
      showToast("No markdown content to copy", "error");
      return;
    }

    try {
      await navigator.clipboard.writeText(rawEditor.value || item.markdown);
      copyText.textContent = "Copied!";
      btnCopyMarkdown.style.borderColor = "var(--mark)";
      setTimeout(() => {
        copyText.textContent = "Copy";
        btnCopyMarkdown.style.borderColor = "";
      }, 1500);
      showToast("Markdown copied to clipboard!", "success");
    } catch {
      showToast("Failed to copy to clipboard", "error");
    }
  });

  btnDownloadMarkdown.addEventListener("click", () => {
    const item = queue.find((i) => i.id === activeItemId);
    if (!item || !item.markdown) {
      showToast("No markdown content to download", "error");
      return;
    }

    const content  = rawEditor.value || item.markdown;
    const blob     = new Blob([content], { type: "text/markdown;charset=utf-8" });
    const url      = URL.createObjectURL(blob);
    const a        = document.createElement("a");
    const baseName = item.name.replace(/\.[^/.]+$/, "");
    a.href         = url;
    a.download     = `${baseName || "document"}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast(`Downloaded ${a.download}`, "success");
  });

  // ─── Tab Switching ───────────────────────────────────────────────────────
  tabRendered.addEventListener("click", () => {
    tabRendered.classList.add("active");
    tabRaw.classList.remove("active");
    renderedContainer.classList.add("active");
    rawContainer.classList.remove("active");
  });

  tabRaw.addEventListener("click", () => {
    tabRaw.classList.add("active");
    tabRendered.classList.remove("active");
    rawContainer.classList.add("active");
    renderedContainer.classList.remove("active");
  });

  // ─── Clear Queue ─────────────────────────────────────────────────────────
  btnClearQueue.addEventListener("click", () => {
    queue = [];
    activeItemId = null;
    previewFilename.textContent = "No file selected";
    previewStats.textContent    = "";
    renderedOutput.innerHTML    = `
      <div class="empty-preview-hint">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
          <polyline points="14 2 14 8 20 8"></polyline>
          <line x1="16" y1="13" x2="8" y2="13"></line>
          <line x1="16" y1="17" x2="8" y2="17"></line>
        </svg>
        <p>Select an item from the queue to preview converted Markdown</p>
      </div>`;
    rawEditor.value = "";
    renderQueue();

    // Return to full dropzone view
    dropZone.classList.remove("compact");
    workbenchSection.style.display = "none";
  });

  // ─── Show Workbench ──────────────────────────────────────────────────────
  function showWorkbench() {
    dropZone.classList.add("compact");
    workbenchSection.style.display = "grid";
  }

  // ─── Toast Notifications ─────────────────────────────────────────────────
  function showToast(message, type = "info") {
    toastNotification.textContent = message;
    toastNotification.className   = `toast-notification toast-${type} show`;
    setTimeout(() => {
      toastNotification.classList.remove("show");
    }, 3000);
  }

  // ─── Utilities ───────────────────────────────────────────────────────────
  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function renderBasicMarkdown(md) {
    return escapeHtml(md).replace(/\n/g, "<br>");
  }
});
