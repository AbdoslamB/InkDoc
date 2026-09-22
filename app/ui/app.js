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
  const dropZoneSection       = document.getElementById("dropZoneSection");
  const dropzoneCompactBar    = document.getElementById("dropzoneCompactBar");
  const dropzoneCollapsible   = document.getElementById("dropzoneCollapsible");
  const dropzoneCollapsibleInner = document.getElementById("dropzoneCollapsibleInner");
  const compactEngineChip     = document.getElementById("compactEngineChip");
  const compactEngineIcon     = document.getElementById("compactEngineIcon");
  const compactEngineName     = document.getElementById("compactEngineName");
  const compactDropLabel      = document.getElementById("compactDropLabel");
  const btnDropzoneToggle     = document.getElementById("btnDropzoneToggle");
  const btnDropzoneCardCollapse = document.getElementById("btnDropzoneCardCollapse");
  const dropzoneLiveStatus    = document.getElementById("dropzoneLiveStatus");

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

  function getEngineIconSvg(engine) {
    if (engine === "docling") {
      return `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a10 10 0 1 0 10 10"></path><path d="M12 6a6 6 0 0 1 6 6"></path><circle cx="12" cy="12" r="2"></circle></svg>`;
    }
    if (engine === "markit") {
      return `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="2" y1="12" x2="22" y2="12"></line><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path></svg>`;
    }
    return `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>`;
  }

  function updateCompactBarEngine() {
    if (compactEngineName) {
      compactEngineName.textContent = getEngineDisplayName(selectedEngine);
    }
    if (compactEngineIcon) {
      compactEngineIcon.innerHTML = getEngineIconSvg(selectedEngine);
    }
    if (compactEngineChip) {
      compactEngineChip.setAttribute("aria-label", `Processing engine: ${getEngineDisplayName(selectedEngine)}. Click to expand drop panel.`);
      compactEngineChip.dataset.engine = selectedEngine;
    }
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

  // ─── Engine Management & Session Security ────────────────────────────────
  const doclingFallbackToggle   = document.getElementById("doclingFallbackToggle");
  const engineCardDocling       = document.getElementById("engineCardDocling");
  const doclingStatusBadge       = document.getElementById("doclingStatusBadge");
  const doclingSizeInfo         = document.getElementById("doclingSizeInfo");
  const doclingVersionInfo      = document.getElementById("doclingVersionInfo");
  const doclingPathRow          = document.getElementById("doclingPathRow");
  const doclingPathInfo         = document.getElementById("doclingPathInfo");
  const doclingProgressContainer= document.getElementById("doclingProgressContainer");
  const doclingProgressFill     = document.getElementById("doclingProgressFill");
  const doclingProgressText     = document.getElementById("doclingProgressText");
  const btnDoclingInstall       = document.getElementById("btnDoclingInstall");
  const btnDoclingVerify        = document.getElementById("btnDoclingVerify");
  const btnDoclingRemove        = document.getElementById("btnDoclingRemove");
  const btnDoclingCancel        = document.getElementById("btnDoclingCancel");

  const previewEngineTag        = document.getElementById("previewEngineTag");
  const previewFallbackBadge    = document.getElementById("previewFallbackBadge");

  // ─── Updates Elements ───────────────────────────────────────────────────
  const appCurrentVersionBadge  = document.getElementById("appCurrentVersionBadge");
  const updateStatusTitle       = document.getElementById("updateStatusTitle");
  const updateStatusSubtitle    = document.getElementById("updateStatusSubtitle");
  const btnCheckUpdates         = document.getElementById("btnCheckUpdates");
  const btnCheckUpdatesText     = document.getElementById("btnCheckUpdatesText");
  const iconCheckUpdatesSpin    = document.getElementById("iconCheckUpdatesSpin");
  const updateDetails           = document.getElementById("updateDetails");
  const updateNewVersion        = document.getElementById("updateNewVersion");
  const updateAssetSize         = document.getElementById("updateAssetSize");
  const updateNotesAccordion    = document.getElementById("updateNotesAccordion");
  const updateNotesContent      = document.getElementById("updateNotesContent");
  const updateProgressWrapper   = document.getElementById("updateProgressWrapper");
  const updateProgressFill      = document.getElementById("updateProgressFill");
  const updateProgressText      = document.getElementById("updateProgressText");
  const btnDownloadUpdate       = document.getElementById("btnDownloadUpdate");
  const btnApplyUpdate          = document.getElementById("btnApplyUpdate");
  const btnRevealUpdate         = document.getElementById("btnRevealUpdate");
  const btnCancelUpdate         = document.getElementById("btnCancelUpdate");
  const updateActionHelp        = document.getElementById("updateActionHelp");
  const updateErrorBox          = document.getElementById("updateErrorBox");
  const updateErrorMsg          = document.getElementById("updateErrorMsg");
  const dailyUpdateToggle       = document.getElementById("dailyUpdateToggle");

  let updatePollInterval = null;
  let currentUpdateState = null;

  function getSessionToken() {
    return window.__INKDOC_SESSION_TOKEN__ || "";
  }

  async function fetchWithToken(url, options = {}) {
    const token = getSessionToken();
    options.headers = options.headers || {};
    if (token) {
      options.headers["X-InkDoc-Token"] = token;
    }
    return fetch(url, options);
  }

  let doclingState = "unknown";
  let pollInterval = null;

  // Update the single pill group to reflect selectedEngine
  function updateEngineUI() {
    allEnginePills.forEach((btn) => {
      if (btn) btn.classList.toggle("active", btn.dataset.engine === selectedEngine);
    });
    updateCompactBarEngine();
  }

  function setEngine(engine) {
    selectedEngine = engine;
    localStorage.setItem("inkdoc-engine", engine);
    updateEngineUI();
  }

  // Bind unified pill clicks
  allEnginePills.forEach((btn) => {
    if (btn) {
      btn.addEventListener("click", () => {
        if (btn.dataset.engine === "docling" && btn.classList.contains("is-installable")) {
          settingsPopover.classList.add("open");
          showToast("Docling is not installed yet. Click 'Install Docling' in Settings to enable it.", "info");
          return;
        }
        setEngine(btn.dataset.engine);
      });
    }
  });

  // Initialize
  updateEngineUI();

  // ─── Collapsible Panel State & Logic ─────────────────────────────────────
  let isDropzoneCollapsed = false;
  let userManuallyExpanded = false;
  let autoCollapseTimer = null;
  let animationTimer = null;
  let batchState = null;

  function hasGeneratedResults() {
    const isWorkbenchVisible = workbenchSection && workbenchSection.style.display !== "none";
    return Boolean(
      isWorkbenchVisible &&
      queue.some(
        (item) => item.status === "saved" || item.status === "empty" || (item.markdown && item.markdown.trim().length > 0)
      )
    );
  }

  function updateResultsState() {
    const hasResults = hasGeneratedResults();
    if (dropZoneSection) {
      dropZoneSection.classList.toggle("has-results", hasResults);
    }
  }

  updateResultsState();

  function setDropzoneCollapsed(collapsed, isUserAction = false) {
    if (isDropzoneCollapsed === collapsed) return;
    isDropzoneCollapsed = collapsed;

    if (dropZoneSection) {
      dropZoneSection.classList.add("is-animating");
      dropZoneSection.classList.toggle("is-collapsed", collapsed);

      if (animationTimer) clearTimeout(animationTimer);
      animationTimer = setTimeout(() => {
        dropZoneSection.classList.remove("is-animating");
        animationTimer = null;

        if (collapsed && dropzoneCollapsibleInner) {
          dropzoneCollapsibleInner.setAttribute("inert", "");
          dropzoneCollapsibleInner.style.visibility = "hidden";
        }
      }, collapsed ? 210 : 270);
    }

    if (!collapsed && dropzoneCollapsibleInner) {
      dropzoneCollapsibleInner.removeAttribute("inert");
      dropzoneCollapsibleInner.style.visibility = "visible";
    }

    const expandedStr = collapsed ? "false" : "true";
    if (compactEngineChip) {
      compactEngineChip.setAttribute("aria-expanded", expandedStr);
    }
    if (btnDropzoneToggle) {
      btnDropzoneToggle.setAttribute("aria-expanded", expandedStr);
      btnDropzoneToggle.setAttribute("aria-label", collapsed ? "Expand drop panel" : "Collapse drop panel");
      btnDropzoneToggle.title = collapsed ? "Expand drop panel" : "Collapse drop panel";
    }
    if (btnDropzoneCardCollapse) {
      btnDropzoneCardCollapse.setAttribute("aria-expanded", expandedStr);
      btnDropzoneCardCollapse.setAttribute("aria-label", collapsed ? "Expand drop panel" : "Collapse drop panel");
      btnDropzoneCardCollapse.title = collapsed ? "Expand drop panel" : "Collapse drop panel";
    }
    if (dropzoneLiveStatus) {
      dropzoneLiveStatus.textContent = collapsed ? "Drop panel collapsed" : "Drop panel expanded";
    }

    // Retain focus without trapping inside collapsed inert elements
    if (collapsed && dropzoneCollapsibleInner && dropzoneCollapsibleInner.contains(document.activeElement)) {
      if (btnDropzoneToggle) btnDropzoneToggle.focus();
    }
  }

  // Handle transition end for clean removal of will-change and inert enforcement
  if (dropzoneCollapsible) {
    dropzoneCollapsible.addEventListener("transitionend", (e) => {
      if (e.target === dropzoneCollapsible && e.propertyName === "grid-template-rows") {
        if (dropZoneSection) dropZoneSection.classList.remove("is-animating");
        if (isDropzoneCollapsed && dropzoneCollapsibleInner) {
          dropzoneCollapsibleInner.setAttribute("inert", "");
          dropzoneCollapsibleInner.style.visibility = "hidden";
        }
      }
    });
  }

  function expandDropzone(isUserAction = false) {
    if (isUserAction) {
      userManuallyExpanded = true;
      if (autoCollapseTimer) {
        clearTimeout(autoCollapseTimer);
        autoCollapseTimer = null;
      }
    }
    setDropzoneCollapsed(false, isUserAction);
  }

  function collapseDropzone(isUserAction = false) {
    if (isUserAction && !hasGeneratedResults()) {
      return;
    }
    if (isUserAction) {
      userManuallyExpanded = false;
      if (autoCollapseTimer) {
        clearTimeout(autoCollapseTimer);
        autoCollapseTimer = null;
      }
    }
    setDropzoneCollapsed(true, isUserAction);
  }

  function toggleDropzone(isUserAction = true) {
    if (isDropzoneCollapsed) {
      expandDropzone(isUserAction);
    } else {
      collapseDropzone(isUserAction);
    }
  }

  if (btnDropzoneToggle) {
    btnDropzoneToggle.addEventListener("click", () => toggleDropzone(true));
  }
  if (compactEngineChip) {
    compactEngineChip.addEventListener("click", () => expandDropzone(true));
  }
  if (btnDropzoneCardCollapse) {
    btnDropzoneCardCollapse.addEventListener("click", () => collapseDropzone(true));
  }

  function registerBatchItems(count) {
    if (!batchState || !batchState.active) {
      batchState = {
        active: true,
        total: count,
        completed: 0,
        successes: 0,
        failures: 0,
        autoCollapsed: false,
      };
      userManuallyExpanded = false; // Reset manual expand latch for new batch
    } else {
      batchState.total += count;
    }
  }

  function handleBatchItemCompleted(item) {
    if (!batchState) return;
    batchState.completed++;

    const isSuccess = (item.status === "saved" || item.status === "empty");
    if (isSuccess) {
      batchState.successes++;
      updateResultsState();
      // Auto-collapse after 350 ms when first result appears
      if (!batchState.autoCollapsed && !userManuallyExpanded && !isDropzoneCollapsed) {
        if (!autoCollapseTimer) {
          autoCollapseTimer = setTimeout(() => {
            autoCollapseTimer = null;
            if (!userManuallyExpanded && !isDropzoneCollapsed && queue.length > 0 && batchState && batchState.successes > 0) {
              collapseDropzone(false);
              batchState.autoCollapsed = true;
            }
          }, 350);
        }
      }
    } else {
      batchState.failures++;
      updateResultsState();
      // Do not auto-collapse when every item failed
      if (batchState.completed >= batchState.total && batchState.successes === 0) {
        if (autoCollapseTimer) {
          clearTimeout(autoCollapseTimer);
          autoCollapseTimer = null;
        }
      }
    }
  }

  // ─── Settings Popover ────────────────────────────────────────────────────
  settingsBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = settingsPopover.classList.toggle("open");
    if (isOpen) {
      loadSettings();
      fetchUpdateStatus();
    }
  });

  document.addEventListener("click", (e) => {
    if (!settingsPopover.contains(e.target) && e.target !== settingsBtn) {
      settingsPopover.classList.remove("open");
    }
  });

  // ─── Progress Polling for Engine Installation ────────────────────────────
  async function pollDoclingProgress() {
    try {
      const resp = await fetch(`${apiBase}/engines/docling/progress`);
      if (!resp.ok) return;
      const prog = await resp.json();
      const status = prog.status || "idle";

      if (status === "idle") {
        if (pollInterval) {
          clearInterval(pollInterval);
          pollInterval = null;
        }
        return;
      }

      if (status === "completed" || status === "complete" || status === "installed") {
        if (pollInterval) {
          clearInterval(pollInterval);
          pollInterval = null;
        }
        showToast("Docling engine pack installed and ready!", "success");
        await refreshEngines();
        return;
      }

      if (status === "error") {
        if (pollInterval) {
          clearInterval(pollInterval);
          pollInterval = null;
        }
        showToast(`Installation failed: ${prog.error || "Unknown error"}`, "error");
        await refreshEngines();
        return;
      }

      if (status === "cancelled") {
        if (pollInterval) {
          clearInterval(pollInterval);
          pollInterval = null;
        }
        showToast("Installation cancelled.", "info");
        await refreshEngines();
        return;
      }

      // Active progress: downloading, extracting, verifying
      if (doclingProgressContainer) doclingProgressContainer.style.display = "block";
      if (doclingProgressFill) doclingProgressFill.style.width = `${Math.round(prog.percent || 0)}%`;
      if (doclingProgressText) {
        doclingProgressText.textContent = prog.message || `${status} (${Math.round(prog.percent || 0)}%)`;
      }
      if (doclingStatusBadge) {
        doclingStatusBadge.textContent = `${status.charAt(0).toUpperCase() + status.slice(1)}…`;
        doclingStatusBadge.className = "engine-badge engine-badge-busy";
      }
      if (btnDoclingInstall) btnDoclingInstall.style.display = "none";
      if (btnDoclingCancel) btnDoclingCancel.style.display = "inline-flex";
      if (btnDoclingVerify) btnDoclingVerify.style.display = "none";
      if (btnDoclingRemove) btnDoclingRemove.style.display = "none";
    } catch (_) {}
  }

  function startProgressPolling() {
    if (pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(pollDoclingProgress, 600);
    pollDoclingProgress();
  }

  // ─── Query Engines State from API ────────────────────────────────────────
  async function refreshEngines() {
    try {
      const resp = await fetch(`${apiBase}/engines`);
      if (!resp.ok) return;
      const data = await resp.json();
      const engines = data.engines || data;

      const docling = engines.docling;
      const markitdown = engines.markitdown;
      const markit = engines.markit;

      const versionMarkitdown = document.getElementById("versionMarkitdown");
      const versionMarkit = document.getElementById("versionMarkit");
      if (versionMarkitdown && markitdown) versionMarkitdown.textContent = `v${markitdown.version || "bundled"}`;
      if (versionMarkit && markit) versionMarkit.textContent = `v${markit.version || "bundled"}`;

      // Update manifest-derived platforms
      const doclingPlatformsInfo = document.getElementById("doclingPlatformsInfo");
      if (doclingPlatformsInfo) {
        const platforms = docling?.supported_platforms || [];
        if (platforms.length > 0) {
          const prettyMap = {
            "windows-x86_64": "Windows x64",
            "linux-x86_64": "Linux x64",
            "macos-arm64": "macOS ARM64",
            "macos-x86_64": "macOS x64",
          };
          doclingPlatformsInfo.textContent = platforms.map(p => prettyMap[p] || p).join(", ");
        } else {
          doclingPlatformsInfo.textContent = "No builds published in manifest yet";
        }
      }

      if (!docling || docling.status === "unsupported" || !docling.supported) {
        // Contract: not installable on this platform -> hidden in conversion selector
        if (pillDocling) pillDocling.classList.add("is-hidden");
        if (selectedEngine === "docling") {
          setEngine("markitdown");
        }
        if (engineCardDocling) {
          engineCardDocling.style.display = "block";
          if (doclingStatusBadge) {
            doclingStatusBadge.textContent = "Not Available";
            doclingStatusBadge.className = "engine-badge engine-badge-unsupported";
          }
          if (doclingPathRow) doclingPathRow.style.display = "none";
          if (doclingProgressContainer) doclingProgressContainer.style.display = "none";
          if (btnDoclingVerify) btnDoclingVerify.style.display = "none";
          if (btnDoclingRemove) btnDoclingRemove.style.display = "none";
          if (btnDoclingCancel) btnDoclingCancel.style.display = "none";
          if (btnDoclingInstall) {
            btnDoclingInstall.style.display = "inline-flex";
            btnDoclingInstall.disabled = true;
            btnDoclingInstall.textContent = "Not Available";
            btnDoclingInstall.title = docling?.description || "Docling pack is not available for this build.";
          }
          if (doclingSizeInfo) {
            doclingSizeInfo.textContent = "Not available for this build";
          }
          if (doclingVersionInfo) {
            doclingVersionInfo.textContent = "—";
          }
        }
        return;
      }

      // Supported on this platform
      if (engineCardDocling) engineCardDocling.style.display = "block";
      if (pillDocling) pillDocling.classList.remove("is-hidden");

      if (docling.version && doclingVersionInfo) {
        doclingVersionInfo.textContent = `v${docling.version}`;
      }
      const downloadSize = docling.download_size_bytes || docling.download_size;
      if (downloadSize && doclingSizeInfo) {
        const mb = (downloadSize / (1024 * 1024)).toFixed(0);
        doclingSizeInfo.textContent = `~${mb} MB`;
      } else if (docling.source_mode && doclingSizeInfo) {
        doclingSizeInfo.textContent = "Active environment";
      }

      doclingState = docling.status;

      if (docling.status === "installed") {
        if (pillDocling) {
          pillDocling.classList.remove("is-installable", "engine-unavailable");
          pillDocling.title = "Docling — IBM AI layout & table recognition. Deep document analysis.";
        }
        if (doclingStatusBadge) {
          doclingStatusBadge.textContent = docling.source_mode ? "Active (Source)" : "Installed";
          doclingStatusBadge.className = "engine-badge engine-badge-installed";
        }
        const doclingSourceInfo = document.getElementById("doclingSourceInfo");
        if (doclingSourceInfo) {
          doclingSourceInfo.textContent = docling.source_mode
            ? "Local Python (Source Mode)"
            : "GitHub Release Asset (SHA-256)";
        }
        if (doclingPathRow) {
          doclingPathRow.style.display = "flex";
          if (doclingPathInfo) doclingPathInfo.textContent = docling.install_path || "Installed";
        }
        if (btnDoclingInstall) btnDoclingInstall.style.display = "none";
        if (btnDoclingCancel) btnDoclingCancel.style.display = "none";
        if (btnDoclingVerify) btnDoclingVerify.style.display = docling.source_mode ? "none" : "inline-flex";
        if (btnDoclingRemove) btnDoclingRemove.style.display = docling.source_mode ? "none" : "inline-flex";
        if (doclingProgressContainer) doclingProgressContainer.style.display = "none";
      } else if (["downloading", "extracting", "verifying"].includes(docling.status)) {
        if (pillDocling) {
          pillDocling.classList.add("is-installable");
          pillDocling.title = "Docling installation in progress...";
        }
        startProgressPolling();
      } else {
        // installable (not installed)
        if (pillDocling) {
          pillDocling.classList.add("is-installable");
          pillDocling.title = "Docling is not installed. Click to open Settings and install.";
        }
        if (doclingStatusBadge) {
          doclingStatusBadge.textContent = "Not Installed";
          doclingStatusBadge.className = "engine-badge engine-badge-not-installed";
        }
        if (doclingPathRow) doclingPathRow.style.display = "none";
        if (btnDoclingInstall) {
          btnDoclingInstall.style.display = "inline-flex";
          btnDoclingInstall.disabled = false;
          btnDoclingInstall.textContent = "Install Docling";
        }
        if (btnDoclingCancel) btnDoclingCancel.style.display = "none";
        if (btnDoclingVerify) btnDoclingVerify.style.display = "none";
        if (btnDoclingRemove) btnDoclingRemove.style.display = "none";
        if (doclingProgressContainer) doclingProgressContainer.style.display = "none";

        if (selectedEngine === "docling") {
          setEngine("markitdown");
        }
      }
    } catch (_) {}
  }

  // ─── Settings Preferences ────────────────────────────────────────────────
  async function loadSettings() {
    try {
      const resp = await fetch(`${apiBase}/settings`);
      if (resp.ok) {
        const s = await resp.json();
        if (doclingFallbackToggle) {
          doclingFallbackToggle.checked = Boolean(s.fallback_to_markitdown ?? s.docling_fallback);
        }
        if (dailyUpdateToggle) {
          dailyUpdateToggle.checked = Boolean(s.check_for_updates_daily);
        }
      }
    } catch (_) {}
  }

  if (doclingFallbackToggle) {
    doclingFallbackToggle.addEventListener("change", async (e) => {
      const checked = e.target.checked;
      try {
        const resp = await fetchWithToken(`${apiBase}/settings`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ fallback_to_markitdown: checked }),
        });
        if (resp.ok) {
          showToast(
            checked
              ? "Automatic fallback to MarkItDown enabled."
              : "Automatic fallback disabled (strict engine execution).",
            "info"
          );
        } else {
          showToast("Failed to update settings.", "error");
          e.target.checked = !checked;
        }
      } catch {
        showToast("Error updating settings.", "error");
        e.target.checked = !checked;
      }
    });
  }

  if (dailyUpdateToggle) {
    dailyUpdateToggle.addEventListener("change", async (e) => {
      const checked = e.target.checked;
      try {
        const resp = await fetchWithToken(`${apiBase}/settings`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ check_for_updates_daily: checked }),
        });
        if (resp.ok) {
          showToast(
            checked
              ? "Daily update checks enabled on startup."
              : "Daily update checks disabled.",
            "info"
          );
        } else {
          showToast("Failed to update settings.", "error");
          e.target.checked = !checked;
        }
      } catch {
        showToast("Error updating settings.", "error");
        e.target.checked = !checked;
      }
    });
  }

  // ─── Update Management UI ─────────────────────────────────────────────────
  function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
  }

  function renderUpdateStatus(s) {
    if (!s) return;
    currentUpdateState = s;

    if (appCurrentVersionBadge) {
      appCurrentVersionBadge.textContent = `v${s.current_version || "—"}`;
    }

    const state = s.state || "idle";

    if (state === "checking") {
      if (btnCheckUpdates) btnCheckUpdates.disabled = true;
      if (iconCheckUpdatesSpin) iconCheckUpdatesSpin.style.display = "inline-block";
      if (btnCheckUpdatesText) btnCheckUpdatesText.textContent = "Checking…";
      if (updateStatusTitle) updateStatusTitle.textContent = "Checking for updates…";
      if (updateErrorBox) updateErrorBox.style.display = "none";
      return;
    }

    if (iconCheckUpdatesSpin) iconCheckUpdatesSpin.style.display = "none";
    if (btnCheckUpdates) btnCheckUpdates.disabled = false;

    if (state === "idle" || state === "up_to_date") {
      if (btnCheckUpdatesText) btnCheckUpdatesText.textContent = "Check for updates";
      if (updateStatusTitle) updateStatusTitle.textContent = "InkDoc is up to date";
      if (updateStatusSubtitle) {
        updateStatusSubtitle.textContent = s.last_checked
          ? `Last checked: ${new Date(s.last_checked).toLocaleDateString()} ${new Date(s.last_checked).toLocaleTimeString()}`
          : "Last checked: Never";
      }
      if (updateDetails) updateDetails.style.display = "none";
      if (updateErrorBox) updateErrorBox.style.display = "none";
      stopUpdatePolling();
      return;
    }

    if (state === "available") {
      if (btnCheckUpdatesText) btnCheckUpdatesText.textContent = "Check again";
      if (updateStatusTitle) updateStatusTitle.textContent = "Update available!";
      if (updateStatusSubtitle) {
        updateStatusSubtitle.textContent = `Version v${s.latest_version} is available.`;
      }
      if (updateDetails) updateDetails.style.display = "block";
      if (updateNewVersion) updateNewVersion.textContent = `v${s.latest_version} available`;
      if (updateAssetSize) {
        updateAssetSize.textContent = s.asset_size ? formatBytes(s.asset_size) : "";
      }

      if (s.release_notes && s.release_notes.trim()) {
        try {
          const rawHtml = marked.parse(s.release_notes);
          const cleanHtml = window.DOMPurify ? window.DOMPurify.sanitize(rawHtml) : rawHtml;
          if (updateNotesContent) updateNotesContent.innerHTML = cleanHtml;
          if (updateNotesAccordion) updateNotesAccordion.style.display = "block";
        } catch (_) {
          if (updateNotesContent) updateNotesContent.textContent = s.release_notes;
          if (updateNotesAccordion) updateNotesAccordion.style.display = "block";
        }
      } else if (updateNotesAccordion) {
        updateNotesAccordion.style.display = "none";
      }

      if (updateProgressWrapper) updateProgressWrapper.style.display = "none";
      if (btnDownloadUpdate) {
        btnDownloadUpdate.style.display = "inline-flex";
        btnDownloadUpdate.disabled = false;
        btnDownloadUpdate.textContent = "Download Update";
      }
      if (btnApplyUpdate) btnApplyUpdate.style.display = "none";
      if (btnRevealUpdate) btnRevealUpdate.style.display = "none";
      if (btnCancelUpdate) btnCancelUpdate.style.display = "none";
      if (updateActionHelp) updateActionHelp.style.display = "none";
      if (updateErrorBox) updateErrorBox.style.display = "none";
      stopUpdatePolling();
      return;
    }

    if (state === "downloading" || state === "verifying") {
      if (btnCheckUpdates) btnCheckUpdates.disabled = true;
      if (updateStatusTitle) {
        updateStatusTitle.textContent = state === "verifying" ? "Verifying update…" : "Downloading update…";
      }
      if (updateDetails) updateDetails.style.display = "block";
      if (updateProgressWrapper) updateProgressWrapper.style.display = "block";

      const pct = Math.round(s.progress_percent || 0);
      if (updateProgressFill) updateProgressFill.style.width = `${pct}%`;
      if (updateProgressText) {
        if (state === "verifying") {
          updateProgressText.textContent = "Verifying cryptographic signature & SHA-256…";
        } else {
          updateProgressText.textContent = `Downloading… ${pct}% (${formatBytes(s.downloaded_bytes)} / ${formatBytes(s.total_bytes)})`;
        }
      }

      if (btnDownloadUpdate) btnDownloadUpdate.style.display = "none";
      if (btnApplyUpdate) btnApplyUpdate.style.display = "none";
      if (btnRevealUpdate) btnRevealUpdate.style.display = "none";
      if (btnCancelUpdate) btnCancelUpdate.style.display = state === "downloading" ? "inline-flex" : "none";
      if (updateErrorBox) updateErrorBox.style.display = "none";
      startUpdatePolling();
      return;
    }

    if (state === "ready_to_install") {
      if (btnCheckUpdatesText) btnCheckUpdatesText.textContent = "Check again";
      if (updateStatusTitle) updateStatusTitle.textContent = "Update ready to install";
      if (updateStatusSubtitle) {
        updateStatusSubtitle.textContent = `Version v${s.latest_version} verified.`;
      }
      if (updateDetails) updateDetails.style.display = "block";
      if (updateProgressWrapper) updateProgressWrapper.style.display = "none";
      if (btnDownloadUpdate) btnDownloadUpdate.style.display = "none";
      if (btnApplyUpdate) {
        btnApplyUpdate.style.display = "inline-flex";
        btnApplyUpdate.disabled = !s.can_apply;
        btnApplyUpdate.textContent = "Install & Restart";
      }
      if (btnRevealUpdate) btnRevealUpdate.style.display = "none";
      if (btnCancelUpdate) btnCancelUpdate.style.display = "none";

      if (updateActionHelp) {
        updateActionHelp.style.display = "block";
        updateActionHelp.textContent = s.can_apply
          ? "Click 'Install & Restart' to update silently. InkDoc will restart automatically."
          : "In-app install is only available in the packaged Windows application.";
      }
      if (updateErrorBox) updateErrorBox.style.display = "none";
      stopUpdatePolling();
      return;
    }

    if (state === "ready_to_reveal") {
      if (btnCheckUpdatesText) btnCheckUpdatesText.textContent = "Check again";
      if (updateStatusTitle) updateStatusTitle.textContent = "Update downloaded";
      if (updateStatusSubtitle) {
        updateStatusSubtitle.textContent = `Version v${s.latest_version} verified and saved to Downloads.`;
      }
      if (updateDetails) updateDetails.style.display = "block";
      if (updateProgressWrapper) updateProgressWrapper.style.display = "none";
      if (btnDownloadUpdate) btnDownloadUpdate.style.display = "none";
      if (btnApplyUpdate) btnApplyUpdate.style.display = "none";
      if (btnRevealUpdate) btnRevealUpdate.style.display = "inline-flex";
      if (btnCancelUpdate) btnCancelUpdate.style.display = "none";

      if (updateActionHelp) {
        updateActionHelp.style.display = "block";
        updateActionHelp.textContent = "The verified release asset is saved in your Downloads folder. Click to reveal in folder.";
      }
      if (updateErrorBox) updateErrorBox.style.display = "none";
      stopUpdatePolling();
      return;
    }

    if (state === "error") {
      if (btnCheckUpdatesText) btnCheckUpdatesText.textContent = "Retry";
      if (updateStatusTitle) updateStatusTitle.textContent = "Couldn't check for updates";
      if (updateStatusSubtitle) {
        updateStatusSubtitle.textContent = s.last_checked
          ? `Last checked: ${new Date(s.last_checked).toLocaleDateString()}`
          : "";
      }
      if (updateProgressWrapper) updateProgressWrapper.style.display = "none";
      if (updateErrorBox) {
        updateErrorBox.style.display = "flex";
        if (updateErrorMsg) {
          updateErrorMsg.textContent = s.error || "An error occurred during update check.";
        }
      }
      stopUpdatePolling();
    }
  }

  async function fetchUpdateStatus() {
    try {
      const resp = await fetch(`${apiBase}/update/status`);
      if (resp.ok) {
        const s = await resp.json();
        renderUpdateStatus(s);
      }
    } catch (_) {}
  }

  function startUpdatePolling() {
    if (updatePollInterval) clearInterval(updatePollInterval);
    updatePollInterval = setInterval(fetchUpdateStatus, 600);
  }

  function stopUpdatePolling() {
    if (updatePollInterval) {
      clearInterval(updatePollInterval);
      updatePollInterval = null;
    }
  }

  if (btnCheckUpdates) {
    btnCheckUpdates.addEventListener("click", async () => {
      renderUpdateStatus({ state: "checking", current_version: currentUpdateState?.current_version });
      try {
        const resp = await fetchWithToken(`${apiBase}/update/check`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ force: true }),
        });
        if (resp.ok) {
          const s = await resp.json();
          renderUpdateStatus(s);
        } else {
          const err = await resp.json().catch(() => ({ detail: "Failed to check for updates" }));
          renderUpdateStatus({
            state: "error",
            error: err.detail,
            current_version: currentUpdateState?.current_version,
          });
        }
      } catch (e) {
        renderUpdateStatus({
          state: "error",
          error: e.message,
          current_version: currentUpdateState?.current_version,
        });
      }
    });
  }

  if (btnDownloadUpdate) {
    btnDownloadUpdate.addEventListener("click", async () => {
      btnDownloadUpdate.disabled = true;
      btnDownloadUpdate.textContent = "Starting download…";
      try {
        const resp = await fetchWithToken(`${apiBase}/update/download`, { method: "POST" });
        if (resp.ok) {
          const s = await resp.json();
          renderUpdateStatus(s);
        } else {
          const err = await resp.json().catch(() => ({ detail: "Failed to start download" }));
          showToast(`Error: ${err.detail}`, "error");
          btnDownloadUpdate.disabled = false;
          btnDownloadUpdate.textContent = "Download Update";
        }
      } catch (e) {
        showToast(`Error: ${e.message}`, "error");
        btnDownloadUpdate.disabled = false;
        btnDownloadUpdate.textContent = "Download Update";
      }
    });
  }

  if (btnCancelUpdate) {
    btnCancelUpdate.addEventListener("click", async () => {
      try {
        const resp = await fetchWithToken(`${apiBase}/update/cancel`, { method: "POST" });
        if (resp.ok) {
          const s = await resp.json();
          renderUpdateStatus(s);
          showToast("Update download cancelled.", "info");
        }
      } catch (_) {}
    });
  }

  if (btnApplyUpdate) {
    btnApplyUpdate.addEventListener("click", async () => {
      btnApplyUpdate.disabled = true;
      btnApplyUpdate.textContent = "Restarting…";
      try {
        const resp = await fetchWithToken(`${apiBase}/update/apply`, { method: "POST" });
        if (resp.ok) {
          showToast("Update launched! InkDoc is restarting...", "success");
        } else {
          const err = await resp.json().catch(() => ({ detail: "Failed to apply update" }));
          showToast(`Error: ${err.detail}`, "error");
          btnApplyUpdate.disabled = false;
          btnApplyUpdate.textContent = "Install & Restart";
        }
      } catch (e) {
        showToast(`Error: ${e.message}`, "error");
        btnApplyUpdate.disabled = false;
        btnApplyUpdate.textContent = "Install & Restart";
      }
    });
  }

  if (btnRevealUpdate) {
    btnRevealUpdate.addEventListener("click", async () => {
      try {
        const resp = await fetchWithToken(`${apiBase}/update/reveal`, { method: "POST" });
        if (resp.ok) {
          showToast("Revealed downloaded package in Downloads folder.", "info");
        } else {
          showToast("Failed to locate downloaded file.", "error");
        }
      } catch (e) {
        showToast(`Error: ${e.message}`, "error");
      }
    });
  }

  // ─── Engine Action Button Handlers ───────────────────────────────────────
  if (btnDoclingInstall) {
    btnDoclingInstall.addEventListener("click", async () => {
      btnDoclingInstall.disabled = true;
      btnDoclingInstall.textContent = "Starting…";
      try {
        const resp = await fetchWithToken(`${apiBase}/engines/docling/install`, {
          method: "POST",
        });
        if (resp.ok) {
          showToast("Starting Docling pack download...", "info");
          startProgressPolling();
        } else {
          const err = await resp.json().catch(() => ({ detail: "Failed to start install" }));
          showToast(`Error: ${err.detail}`, "error");
          btnDoclingInstall.disabled = false;
          btnDoclingInstall.textContent = "Install Docling";
        }
      } catch (e) {
        showToast(`Error starting installation: ${e.message}`, "error");
        btnDoclingInstall.disabled = false;
        btnDoclingInstall.textContent = "Install Docling";
      }
    });
  }

  if (btnDoclingCancel) {
    btnDoclingCancel.addEventListener("click", async () => {
      try {
        await fetchWithToken(`${apiBase}/engines/docling/cancel`, { method: "POST" });
        showToast("Cancelling installation...", "info");
      } catch (_) {}
    });
  }

  if (btnDoclingVerify) {
    btnDoclingVerify.addEventListener("click", async () => {
      const origText = btnDoclingVerify.textContent;
      btnDoclingVerify.textContent = "Verifying…";
      btnDoclingVerify.disabled = true;
      try {
        const resp = await fetchWithToken(`${apiBase}/engines/docling/verify`, {
          method: "POST",
        });
        const res = await resp.json();
        if (res.valid) {
          showToast(`Integrity check passed! All ${res.files_checked} files verified against SHA-256 signatures.`, "success");
        } else {
          showToast(`Integrity check failed: ${res.reason || "Hash mismatch"}`, "error");
        }
      } catch (e) {
        showToast(`Verification failed: ${e.message}`, "error");
      } finally {
        btnDoclingVerify.textContent = origText;
        btnDoclingVerify.disabled = false;
      }
    });
  }

  if (btnDoclingRemove) {
    btnDoclingRemove.addEventListener("click", async () => {
      if (!confirm("Are you sure you want to remove the IBM Docling pack? All installed files and caches will be deleted.")) {
        return;
      }
      try {
        const resp = await fetchWithToken(`${apiBase}/engines/docling/remove`, {
          method: "POST",
        });
        if (resp.ok) {
          showToast("Docling engine pack removed.", "info");
          if (selectedEngine === "docling") {
            setEngine("markitdown");
          }
          await refreshEngines();
        } else {
          showToast("Failed to remove Docling pack.", "error");
        }
      } catch (e) {
        showToast(`Error removing engine: ${e.message}`, "error");
      }
    });
  }

  // ─── Health Check ────────────────────────────────────────────────────────
  async function checkServerHealth() {
    try {
      const resp = await fetch(`${apiBase}/health`);
      if (resp.ok) {
        connectionErrorBar.classList.remove("visible");
        // FastAPI's interactive docs are disabled in packaged builds, where /docs
        // returns 404. Only offer the link when the server actually serves them.
        try {
          const health = await resp.clone().json();
          const showDocs = health && health.docs_enabled === true;
          const docsSection = document.getElementById("apiDocsSection");
          const docsDivider = document.getElementById("apiDocsDivider");
          if (docsSection) docsSection.hidden = !showDocs;
          if (docsDivider) docsDivider.hidden = !showDocs;
        } catch (_) {
          /* leave the link hidden */
        }
        await refreshEngines();
        await loadSettings();
        await fetchUpdateStatus();
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

  // ─── Drag and Drop with Window-Level Safety & Zero Flicker ───────────────
  let dropZoneDragCounter = 0;
  let compactDragCounter = 0;

  // Window-level safety: prevent browser/webview navigation if drop lands outside targets
  window.addEventListener("dragenter", (e) => {
    e.preventDefault();
  });

  window.addEventListener("dragover", (e) => {
    e.preventDefault();
    if (e.dataTransfer) {
      e.dataTransfer.dropEffect = "copy";
    }
  });

  window.addEventListener("dragleave", (e) => {
    e.preventDefault();
  });

  window.addEventListener("dragend", () => {
    dropZoneDragCounter = 0;
    compactDragCounter = 0;
    if (dropZone) dropZone.classList.remove("drag-over");
    if (dropzoneCompactBar) {
      dropzoneCompactBar.classList.remove("drag-over");
      if (compactDropLabel) compactDropLabel.textContent = "Drop files or URL to convert";
    }
  });

  window.addEventListener("drop", (e) => {
    e.preventDefault();
  });

  function handleDroppedData(dt) {
    if (!dt) return;
    if (dt.files && dt.files.length > 0) {
      handleIncomingFiles(Array.from(dt.files));
      return;
    }
    const rawUri = dt.getData("text/uri-list") || dt.getData("text/plain") || "";
    const lines = rawUri.split(/[\r\n]+/);
    const validUrl = lines.map((l) => l.trim()).find((l) => l && !l.startsWith("#") && (l.startsWith("http://") || l.startsWith("https://")));
    if (validUrl) {
      registerBatchItems(1);
      convertUrlItem(validUrl);
    }
  }

  // Full drop zone card
  if (dropZone) {
    dropZone.addEventListener("dragenter", (e) => {
      e.preventDefault();
      dropZoneDragCounter++;
      dropZone.classList.add("drag-over");
    });

    dropZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    });

    dropZone.addEventListener("dragleave", (e) => {
      e.preventDefault();
      dropZoneDragCounter--;
      if (dropZoneDragCounter <= 0) {
        dropZoneDragCounter = 0;
        dropZone.classList.remove("drag-over");
      }
    });

    dropZone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropZoneDragCounter = 0;
      dropZone.classList.remove("drag-over");
      handleDroppedData(e.dataTransfer);
    });
  }

  // Compact bar drop target
  if (dropzoneCompactBar) {
    dropzoneCompactBar.addEventListener("dragenter", (e) => {
      e.preventDefault();
      compactDragCounter++;
      dropzoneCompactBar.classList.add("drag-over");
      if (compactDropLabel) compactDropLabel.textContent = "Drop to convert";
    });

    dropzoneCompactBar.addEventListener("dragover", (e) => {
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    });

    dropzoneCompactBar.addEventListener("dragleave", (e) => {
      e.preventDefault();
      compactDragCounter--;
      if (compactDragCounter <= 0) {
        compactDragCounter = 0;
        dropzoneCompactBar.classList.remove("drag-over");
        if (compactDropLabel) compactDropLabel.textContent = "Drop files or URL to convert";
      }
    });

    dropzoneCompactBar.addEventListener("drop", (e) => {
      e.preventDefault();
      compactDragCounter = 0;
      dropzoneCompactBar.classList.remove("drag-over");
      if (compactDropLabel) compactDropLabel.textContent = "Drop files or URL to convert";
      handleDroppedData(e.dataTransfer);
    });
  }

  // ─── URL Conversion ──────────────────────────────────────────────────────
  btnConvertUrl.addEventListener("click", () => handleUrlSubmission());
  urlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") handleUrlSubmission();
  });

  function handleUrlSubmission() {
    const url = urlInput.value.trim();
    if (!url) return;
    urlInput.value = "";
    registerBatchItems(1);
    convertUrlItem(url);
  }

  // ─── Queue Management ────────────────────────────────────────────────────
  function handleIncomingFiles(files) {
    if (files.length === 0) return;
    registerBatchItems(files.length);
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

        if (resp.status === 409 || msg.includes("engine_not_installed")) {
          item.status = "failed";
          item.error = "Docling is not installed. Open Settings to install the engine pack.";
          item.errorType = "failed";
          selectItem(item.id);
          showToast("Docling is not installed. Install in Settings.", "error");
          settingsPopover.classList.add("open");
          return;
        }

        if (resp.status === 400 && msg.includes("engine_unsupported")) {
          item.status = "unsupported";
          item.error = "Docling is not supported on this platform architecture.";
          item.errorType = "unsupported";
          selectItem(item.id);
          showToast("Docling is not supported on this platform.", "error");
          return;
        }

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
      item.engineRequested = data.engine_requested || engineToUse;
      item.engineUsed = data.engine_used || data.engine || engineToUse;
      item.fallbackOccurred = Boolean(data.fallback);
      item.fallbackReason = data.fallback_reason || "";
      item.engine   = item.engineUsed;
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
        const toastMsg = item.fallbackOccurred
          ? `Converted "${item.name}" via MarkItDown (Docling fallback)`
          : `Converted "${item.name}" via ${getEngineDisplayName(item.engine)}`;
        showToast(toastMsg, "success");
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
      handleBatchItemCompleted(item);
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

        if (resp.status === 409 || msg.includes("engine_not_installed")) {
          item.status = "failed";
          item.error = "Docling is not installed. Open Settings to install the engine pack.";
          item.errorType = "failed";
          selectItem(item.id);
          showToast("Docling is not installed. Install in Settings.", "error");
          settingsPopover.classList.add("open");
          return;
        }

        if (resp.status === 400 && msg.includes("engine_unsupported")) {
          item.status = "unsupported";
          item.error = "Docling is not supported on this platform architecture.";
          item.errorType = "unsupported";
          selectItem(item.id);
          showToast("Docling is not supported on this platform.", "error");
          return;
        }

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
      item.engineRequested = data.engine_requested || engineToUse;
      item.engineUsed = data.engine_used || data.engine || engineToUse;
      item.fallbackOccurred = Boolean(data.fallback);
      item.fallbackReason = data.fallback_reason || "";
      item.engine   = item.engineUsed;
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
        const toastMsg = item.fallbackOccurred
          ? `URL converted via MarkItDown (Docling fallback)`
          : `URL converted via ${getEngineDisplayName(item.engine)}`;
        showToast(toastMsg, "success");
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
      handleBatchItemCompleted(item);
    }
  }

  // ─── Render Queue ────────────────────────────────────────────────────────
  function renderQueue() {
    queueCount.textContent = queue.length;
    updateResultsState();

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
      if (previewEngineTag) previewEngineTag.style.display = "none";
      if (previewFallbackBadge) previewFallbackBadge.style.display = "none";
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
      if (previewEngineTag) previewEngineTag.style.display = "none";
      if (previewFallbackBadge) previewFallbackBadge.style.display = "none";
      const errMsg = item.error || "An unexpected error occurred during conversion.";
      showConversionNotice(
        "failed",
        `Conversion failed — ${engineLabel}`,
        errMsg
      );
      const altEng = item.engine === "markitdown" ? "docling" : "markitdown";
      const altLabel = getEngineDisplayName(altEng);
      renderedOutput.innerHTML = `
        <div class="empty-preview-hint">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"></circle>
            <line x1="12" y1="8" x2="12" y2="12"></line>
            <line x1="12" y1="16" x2="12.01" y2="16"></line>
          </svg>
          <p>${escapeHtml(errMsg)}</p>
          ${item.fileRef ? `<button type="button" class="btn btn-sm btn-mark" id="btnNoticeRetryAlt" style="margin-top: 14px;">↺ Retry with ${altLabel} (Recommended)</button>` : ""}
        </div>`;
      const retryAltBtn = document.getElementById("btnNoticeRetryAlt");
      if (retryAltBtn && item.fileRef) {
        retryAltBtn.addEventListener("click", () => {
          setEngine(altEng);
          item.engine = altEng;
          convertFileItem(item, item.fileRef);
        });
      }
      rawEditor.value = `Error: ${errMsg}`;
      previewStats.textContent = "";
      renderQueue();
      return;
    }

    if (item.status === "empty") {
      if (previewEngineTag) previewEngineTag.style.display = "none";
      if (previewFallbackBadge) previewFallbackBadge.style.display = "none";
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

    // Engine used tag
    if (previewEngineTag) {
      const displayEngine = item.engineUsed || item.engine || selectedEngine;
      previewEngineTag.textContent = `Engine: ${getEngineDisplayName(displayEngine)}`;
      previewEngineTag.style.display = "inline-flex";
    }

    // Fallback badge
    if (previewFallbackBadge) {
      if (item.fallbackOccurred) {
        previewFallbackBadge.style.display = "inline-flex";
        previewFallbackBadge.textContent = "⚠️ Fallback: MarkItDown";
        previewFallbackBadge.title = item.fallbackReason
          ? `Docling failed: ${item.fallbackReason}`
          : "Docling failed to convert this document. Automatically fell back to MarkItDown.";
        showConversionNotice(
          "unsupported",
          "Docling Fallback: Converted with MarkItDown",
          `Docling was unable to parse this document${item.fallbackReason ? " (" + item.fallbackReason + ")" : ""}. Conversion automatically fell back to MarkItDown.`
        );
      } else {
        previewFallbackBadge.style.display = "none";
      }
    }

    // Rendered HTML (Sanitized against DOM XSS)
    if (window.marked) {
      try {
        if (typeof window.marked.use === "function") {
          window.marked.use({ gfm: true, breaks: true });
        }
      } catch (_) {}
      const rawHtml = window.marked.parse(md);
      if (window.DOMPurify) {
        renderedOutput.innerHTML = window.DOMPurify.sanitize(rawHtml);
      } else {
        renderedOutput.innerHTML = renderBasicMarkdown(md);
      }
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

    // Reset queue and restore expanded dropzone
    queue = [];
    activeItemId = null;
    if (autoCollapseTimer) {
      clearTimeout(autoCollapseTimer);
      autoCollapseTimer = null;
    }
    batchState = null;
    userManuallyExpanded = false;
    expandDropzone(false);

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
    workbenchSection.style.display = "none";
    renderQueue();
    updateResultsState();
  });

  // ─── Show Workbench ──────────────────────────────────────────────────────
  function showWorkbench() {
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
