/**
 * InkDoc Web Bench — Client-side JavaScript
 */
document.addEventListener("DOMContentLoaded", () => {
  // ─── Pure helpers (no DOM; sliced out and tested by tests/auto_engine_ui.test.js) ──
  const ENGINES = ["auto", "markitdown", "docling", "markit", "glm_ocr"];
  const HIDE_AUTO_HINT_KEY = "inkdoc-hide-auto-hint";

  // Storage can throw (private windows, browsers set to block site data), and
  // a failed read must never stop the app from starting.
  function readStorage(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (_) {
      return null;
    }
  }

  function writeStorage(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (_) {
      // Not remembered this time; nothing else depends on it.
    }
  }

  // New users start on Auto. Anyone with a saved choice keeps it.
  function initialEngine(read) {
    const saved = read("inkdoc-engine") || read("markitdown-engine");
    return ENGINES.includes(saved) ? saved : "auto";
  }

  function getEngineDisplayName(engine) {
    if (engine === "auto")    return "Auto";
    if (engine === "docling") return "Docling";
    if (engine === "markit")  return "Markit";
    if (engine === "glm_ocr") return "GLM-OCR";
    return "MarkItDown";
  }

  // The short label and long message for a quality report, or null when there
  // is nothing to warn about. Labels always carry text, never just an icon.
  function formatQualityWarning(quality) {
    if (!quality || !quality.warning) return null;
    const message = quality.message || "Part of the source text may be missing.";
    const scans = (quality.scan_pages || []).length;
    let short = "Text may be missing";
    let title = "Some source text may be missing";
    let toast = "but part of the source text may be missing";
    if (scans) {
      short = scans === quality.page_count ? "Scanned PDF" : "Scanned pages unread";
      title = scans === quality.page_count ? "No text layer to read" : "Scanned pages weren't read";
      toast = `but ${scans} scanned page${scans === 1 ? " wasn't" : "s weren't"} read`;
    } else if (quality.garbled) {
      short = "Unreadable text";
      title = "Some text came out unreadable";
      toast = "but part of it came out as unreadable characters";
    } else if (quality.missing_pct != null) {
      short = `~${quality.missing_pct}% text missing`;
      toast = `but ~${quality.missing_pct}% of the source text may be missing`;
    }
    return { short, long: message, title, toast };
  }

  // The one-click fix a quality report suggests, as { label, engine } or
  // { label, install: true }; null when there is nothing to offer.
  function qualityAction(quality) {
    const s = quality && quality.suggestion;
    if (s === "docling" || s === "markitdown" || s === "markit" || s === "glm_ocr") {
      return { label: `Re-convert with ${getEngineDisplayName(s)}`, engine: s };
    }
    if (s === "install_docling") return { label: "Install Docling", install: true };
    if (s === "install_glm_ocr") return { label: "Download GLM-OCR", install: true };
    return null;
  }

  // What the queue row's engine tag says. Auto items always show where they went.
  function describeQueueEngine(item, selected) {
    if (item.engineRequested === "auto" && item.engineUsed) {
      return {
        cls: "auto",
        text: `Auto · ${getEngineDisplayName(item.engineUsed)}`,
        title: (item.auto && item.auto.reason) || "Chosen by Auto",
      };
    }
    if (item.engine && item.engine !== selected) {
      return { cls: item.engine, text: getEngineDisplayName(item.engine), title: "" };
    }
    return null;
  }

  // The preview's "Engine: …" tag, with the escalation spelled out.
  function describePreviewEngine(item) {
    const used = getEngineDisplayName(item.engineUsed || item.engine);
    if (item.engineRequested !== "auto") return { text: `Engine: ${used}`, title: "" };
    const auto = item.auto || {};
    let text = `Engine: Auto → ${used}`;
    const esc = auto.escalation;
    if (auto.escalated && esc && esc.kept === esc.to) {
      const from = getEngineDisplayName(esc.from);
      if (esc.from_scan_pages) {
        text += ` (${from} couldn't read ${esc.from_scan_pages} scanned page${esc.from_scan_pages === 1 ? "" : "s"})`;
      } else if (esc.from_missing_pct != null) {
        text += ` (${from} missed ~${esc.from_missing_pct}%)`;
      }
    }
    return { text, title: auto.reason || "" };
  }

  // The install hint appears as a toast at most once per session, and never
  // again after "Don't show again". It always stays in the item's own notice.
  function shouldShowHintToast(hint, session, read) {
    if (!hint || session.hintShown) return false;
    if (read(HIDE_AUTO_HINT_KEY) === "1") return false;
    return true;
  }

  // Concrete engines only: the Switch Engine button never cycles to Auto, and
  // reaches GLM-OCR only when it is installed and passed its self-test.
  function getNextEngine(current, glmUsable = false) {
    if (current === "markitdown") return "docling";
    if (current === "docling")    return "markit";
    if (current === "markit" && glmUsable) return "glm_ocr";
    return "markitdown";
  }

  // "45 s", "6 min", "1 h 10 min" (matches auto_engine.format_duration).
  function formatDuration(seconds) {
    const s = Math.max(0, Math.round(Number(seconds) || 0));
    if (s < 90) return `${s} s`;
    const minutes = Math.round(s / 60);
    if (minutes < 60) return `${minutes} min`;
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return m ? `${h} h ${m} min` : `${h} h`;
  }

  // "412 MB", "1.43 GB".
  function formatSize(bytes) {
    const b = Number(bytes) || 0;
    if (b >= 1e9) return `${(b / 1e9).toFixed(2)} GB`;
    if (b >= 1e6) return `${Math.round(b / 1e6)} MB`;
    if (b >= 1e3) return `${Math.round(b / 1e3)} KB`;
    return `${b} B`;
  }

  // What a converting queue row says: "Page 7 / 30 · ~8 min left" while an
  // engine reports pages (GLM-OCR), otherwise the phase message.
  function formatJobProgress(p) {
    if (!p) return "";
    if (p.pages > 1 && typeof p.page === "number" && p.phase === "converting") {
      let text = `Page ${Math.min(p.page + 1, p.pages)} / ${p.pages}`;
      if (p.page > 0 && typeof p.seconds_remaining_est === "number") {
        text += ` · ~${formatDuration(p.seconds_remaining_est)} left`;
      }
      return text;
    }
    return p.message || "";
  }

  function glmAccelLabel(accel) {
    if (accel === "vulkan") return "GPU: Vulkan";
    if (accel === "metal") return "GPU: Metal";
    return "CPU";
  }

  // The GLM-OCR card's progress line: "From Hugging Face · 412 MB of 1.43 GB",
  // or after a fallback "From InkDoc mirror (Hugging Face unreachable) · …".
  function formatGlmProgress(prog) {
    if (!prog) return "";
    if (prog.status === "downloading" && prog.source_label) {
      const from = `From ${prog.source_label}${prog.fallback_reason ? ` (${prog.fallback_reason})` : ""}`;
      return prog.total_bytes
        ? `${from} · ${formatSize(prog.bytes_downloaded)} of ${formatSize(prog.total_bytes)}`
        : from;
    }
    return prog.message || prog.status || "";
  }

  // The warning shown before GLM-OCR downloads anything. Nothing starts until
  // the user confirms it.
  function glmDownloadWarning(status, variantId) {
    const variants = (status && status.variants) || [];
    const variant = variants.find((v) => v.id === variantId) || variants.find((v) => v.id === (status && status.variant));
    const runtime = Math.max(0, ((status && status.download_size_bytes) || 0) - ((variants.find((v) => v.id === (status && status.variant)) || {}).size_bytes || 0));
    const total = (variant ? variant.size_bytes : 0) + runtime || (status && status.download_size_bytes) || 0;
    const size = formatSize(total);
    return {
      title: "Download GLM-OCR?",
      message: `GLM-OCR is a large AI model. About ${size} will be downloaded and stored on this computer before it can be used.`,
      details: [
        `Download size: ${size}. On a slow connection this can take a long time.`,
        "Processing is much slower than the other engines: on a typical laptop CPU expect roughly 10 to 60 seconds per page, so a 30-page scan can take 10 to 30 minutes. A GPU makes it much faster.",
        "Needs about 3 GB of free memory while converting.",
        "Only the model is downloaded (from Hugging Face, or InkDoc's GitHub mirror as a fallback). Your documents are processed on this computer and never uploaded.",
      ],
      confirmText: `Download ${size}`,
    };
  }

  // The question asked before a long GLM-OCR job.
  function glmLongJobPrompt(seconds, accel, count) {
    const what = count > 1 ? `these ${count} files` : "this file";
    const tip = accel === "cpu" ? " Tip: turn on GPU acceleration in Settings to make it faster." : "";
    return {
      title: "This will take a while",
      message: `Converting ${what} with GLM-OCR will take about ${formatDuration(seconds)} on this computer (${glmAccelLabel(accel)}). Convert now?${tip}`,
    };
  }

  function newJobId() {
    const rand = (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
    return `ui-${rand}`;
  }
  // ─── End of pure helpers ──

  // ─── State ───────────────────────────────────────────────────────────────
  let queue = [];
  let activeItemId = null;
  let autoSaveToDownloads = false;
  let selectedEngine = initialEngine(readStorage);
  const hintSession = { hintShown: false };
  // Last /engines/glm_ocr/status payload; null until the first refresh.
  let glmStatus = null;

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
  const pillAuto          = document.getElementById("pillAuto");
  const pillMarkitdown    = document.getElementById("pillMarkitdown");
  const pillDocling       = document.getElementById("pillDocling");
  const pillMarkit        = document.getElementById("pillMarkit");
  const pillGlmOcr        = document.getElementById("pillGlmOcr");
  const allEnginePills    = [pillAuto, pillMarkitdown, pillDocling, pillMarkit, pillGlmOcr];
  const engineSpeedHint   = document.getElementById("engineSpeedHint");

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

  // Custom confirmation dialog (replaces window.confirm)
  const appDialogOverlay  = document.getElementById("appDialogOverlay");
  const appDialogIcon     = document.getElementById("appDialogIcon");
  const appDialogTitle    = document.getElementById("appDialogTitle");
  const appDialogMessage  = document.getElementById("appDialogMessage");
  const appDialogCancel   = document.getElementById("appDialogCancel");
  const appDialogConfirm  = document.getElementById("appDialogConfirm");

  // Conversion Notice Bar elements
  const conversionNoticeBar    = document.getElementById("conversionNoticeBar");
  const conversionNoticeIcon   = document.getElementById("conversionNoticeIcon");
  const conversionNoticeTitle  = document.getElementById("conversionNoticeTitle");
  const conversionNoticeDetail = document.getElementById("conversionNoticeDetail");

  // ─── Engine Helpers ──────────────────────────────────────────────────────
  function getEngineIconSvg(engine) {
    if (engine === "auto") {
      return `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 4V2"></path><path d="M15 16v-2"></path><path d="M8 9h2"></path><path d="M20 9h2"></path><path d="M17.8 11.8 19 13"></path><path d="M15 9h.01"></path><path d="M17.8 6.2 19 5"></path><path d="m3 21 9-9"></path><path d="M12.2 6.2 11 5"></path></svg>`;
    }
    if (engine === "docling") {
      return `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a10 10 0 1 0 10 10"></path><path d="M12 6a6 6 0 0 1 6 6"></path><circle cx="12" cy="12" r="2"></circle></svg>`;
    }
    if (engine === "glm_ocr") {
      return `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7V5a2 2 0 0 1 2-2h2"></path><path d="M17 3h2a2 2 0 0 1 2 2v2"></path><path d="M21 17v2a2 2 0 0 1-2 2h-2"></path><path d="M7 21H5a2 2 0 0 1-2-2v-2"></path><line x1="7" y1="9" x2="17" y2="9"></line><line x1="7" y1="12" x2="17" y2="12"></line><line x1="7" y1="15" x2="13" y2="15"></line></svg>`;
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
    warning:     `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>`,
    info:        `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>`,
  };

  const conversionNoticeActions = document.getElementById("conversionNoticeActions");
  const conversionNoticeDetails = document.getElementById("conversionNoticeDetails");

  // opts.actions: [{ label, onClick, primary }]; opts.details: [string] shown
  // behind a "Details" toggle. Everything is set with textContent: messages can
  // carry file names and server text.
  function showConversionNotice(type, title, detail, opts = {}) {
    conversionNoticeBar.className = `conversion-notice notice-${type} visible`;
    // Failures interrupt; warnings and tips wait for a pause.
    conversionNoticeBar.setAttribute("aria-live", type === "failed" ? "assertive" : "polite");
    conversionNoticeIcon.innerHTML = ICONS[type] || ICONS.failed;
    conversionNoticeTitle.textContent = title;
    conversionNoticeDetail.textContent = detail;

    if (conversionNoticeActions) {
      conversionNoticeActions.replaceChildren();
      const actions = opts.actions || [];
      const details = opts.details || [];
      actions.forEach((action) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = `btn btn-sm ${action.primary === false ? "btn-ghost" : "btn-secondary"} notice-action`;
        btn.textContent = action.label;
        btn.addEventListener("click", action.onClick);
        conversionNoticeActions.appendChild(btn);
      });
      if (details.length && conversionNoticeDetails) {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "btn btn-sm btn-ghost notice-details-toggle";
        toggle.textContent = "Details";
        toggle.setAttribute("aria-expanded", "false");
        toggle.setAttribute("aria-controls", "conversionNoticeDetails");
        toggle.addEventListener("click", () => {
          const open = conversionNoticeDetails.hidden;
          conversionNoticeDetails.hidden = !open;
          toggle.setAttribute("aria-expanded", String(open));
          toggle.textContent = open ? "Hide details" : "Details";
        });
        conversionNoticeActions.appendChild(toggle);
      }
      conversionNoticeActions.hidden = conversionNoticeActions.childElementCount === 0;
    }
    if (conversionNoticeDetails) {
      conversionNoticeDetails.replaceChildren();
      (opts.details || []).forEach((line) => {
        const li = document.createElement("li");
        li.textContent = line;
        conversionNoticeDetails.appendChild(li);
      });
      conversionNoticeDetails.hidden = true;
    }
  }

  function hideConversionNotice() {
    conversionNoticeBar.className = "conversion-notice";
    conversionNoticeTitle.textContent = "";
    conversionNoticeDetail.textContent = "";
    conversionNoticeIcon.innerHTML = "";
    if (conversionNoticeActions) {
      conversionNoticeActions.replaceChildren();
      conversionNoticeActions.hidden = true;
    }
    if (conversionNoticeDetails) {
      conversionNoticeDetails.replaceChildren();
      conversionNoticeDetails.hidden = true;
    }
  }

  // ─── Engine Management & Session Security ────────────────────────────────
  const doclingFallbackToggle   = document.getElementById("doclingFallbackToggle");
  const doclingCodeEnrichmentToggle    = document.getElementById("doclingCodeEnrichmentToggle");
  const doclingFormulaEnrichmentToggle = document.getElementById("doclingFormulaEnrichmentToggle");
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

  // Recognition-model add-on (gates the two enrichment toggles above it).
  const addonTitleEl            = document.getElementById("addonTitle");
  const addonStatusBadge        = document.getElementById("addonStatusBadge");
  const addonReason             = document.getElementById("addonReason");
  const addonProgressContainer  = document.getElementById("addonProgressContainer");
  const addonProgressFill       = document.getElementById("addonProgressFill");
  const addonProgressText       = document.getElementById("addonProgressText");
  const btnAddonInstall         = document.getElementById("btnAddonInstall");
  const btnAddonVerify          = document.getElementById("btnAddonVerify");
  const btnAddonRemove          = document.getElementById("btnAddonRemove");
  const btnAddonCancel          = document.getElementById("btnAddonCancel");

  const previewEngineTag        = document.getElementById("previewEngineTag");
  const previewFallbackBadge    = document.getElementById("previewFallbackBadge");
  const previewQualityBadge     = document.getElementById("previewQualityBadge");
  const qualityCheckToggle      = document.getElementById("qualityCheckToggle");

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
  const settingsUpdateDot       = document.getElementById("settingsUpdateDot");
  const aboutVersion            = document.getElementById("aboutVersion");

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

  // Update the single pill group to reflect selectedEngine. The group is a
  // radiogroup: exactly one pill is aria-checked, and only that pill is in the
  // tab order (arrow keys move within the group).
  function updateEngineUI() {
    allEnginePills.forEach((btn) => {
      if (!btn) return;
      const checked = btn.dataset.engine === selectedEngine;
      btn.classList.toggle("active", checked);
      btn.setAttribute("aria-checked", checked ? "true" : "false");
      btn.tabIndex = checked ? 0 : -1;
    });
    updateCompactBarEngine();
    updateSpeedHint();
  }

  function setEngine(engine) {
    selectedEngine = engine;
    writeStorage("inkdoc-engine", engine);
    updateEngineUI();
  }

  // "~12 s per page on this computer (CPU)" under the pills while GLM-OCR is selected.
  function updateSpeedHint() {
    if (!engineSpeedHint) return;
    const spp = glmStatus && glmStatus.usable ? glmStatus.seconds_per_page : null;
    if (selectedEngine === "glm_ocr" && spp) {
      engineSpeedHint.textContent = `GLM-OCR: ~${formatDuration(spp)} per page on this computer (${glmAccelLabel(glmStatus.accel_in_use)})`;
      engineSpeedHint.hidden = false;
    } else {
      engineSpeedHint.hidden = true;
      engineSpeedHint.textContent = "";
    }
  }

  // Bind unified pill clicks
  allEnginePills.forEach((btn) => {
    if (btn) {
      btn.addEventListener("click", (e) => {
        if (btn.dataset.engine === "docling" && btn.classList.contains("is-installable")) {
          openSettings(e, "engines");
          showToast("Docling is not installed yet. Click 'Install Docling' in Settings to enable it.", "info");
          return;
        }
        if (btn.dataset.engine === "glm_ocr" && btn.classList.contains("is-installable")) {
          openSettings(e, "engines");
          const card = document.getElementById("engineCardGlmOcr");
          if (card) {
            card.open = true;
            // Its buttons (Download, Verify…) to the middle of the pane: the card
            // is the last and tallest, and at the bottom the toast would cover them.
            (card.querySelector(".engine-actions") || card).scrollIntoView({ block: "center" });
          }
          showToast(
            glmStatus && glmStatus.installed
              ? "GLM-OCR didn't pass its self-test. Run it again in Settings."
              : "GLM-OCR isn't downloaded yet. Download it in Settings.",
            "info"
          );
          return;
        }
        setEngine(btn.dataset.engine);
      });
    }
  });

  // Arrow keys move the selection within the group, as for any radio group.
  // Hidden pills and Docling while it is not installed are skipped: selecting
  // those has to stay a deliberate click.
  const enginePillGroup = document.getElementById("enginePillGroup");
  if (enginePillGroup) {
    enginePillGroup.addEventListener("keydown", (e) => {
      const keys = ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"];
      if (!keys.includes(e.key)) return;
      const choices = allEnginePills.filter(
        (btn) => btn && !btn.classList.contains("is-hidden") && !btn.classList.contains("is-installable")
      );
      if (choices.length === 0) return;
      e.preventDefault();
      let idx = choices.findIndex((btn) => btn.dataset.engine === selectedEngine);
      if (e.key === "Home") idx = 0;
      else if (e.key === "End") idx = choices.length - 1;
      else if (e.key === "ArrowRight" || e.key === "ArrowDown") idx = (idx + 1) % choices.length;
      else idx = (idx - 1 + choices.length) % choices.length;
      setEngine(choices[idx].dataset.engine);
      choices[idx].focus();
    });
  }

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

  // Counts for the "Converted 12 of 12 files · 2 with warnings" toast. Kept
  // apart from batchState, which spans every drop of a session for the
  // auto-collapse logic: a summary covers one drop (or drops made while it ran).
  let batchSummary = null;

  function registerBatchSummary(count) {
    if (!batchSummary || batchSummary.completed >= batchSummary.total) {
      batchSummary = { total: 0, completed: 0, ok: 0, failed: 0, warnings: 0 };
    }
    batchSummary.total += count;
  }

  function recordBatchSummary(item, isSuccess) {
    // Re-conversions finish here too but were never registered; ignore them.
    if (!batchSummary || batchSummary.completed >= batchSummary.total) return;
    batchSummary.completed++;
    if (isSuccess) batchSummary.ok++;
    else if (item.status !== "cancelled") batchSummary.failed++;
    if (isSuccess && item.quality && item.quality.warning) batchSummary.warnings++;

    // One summary when a multi-file batch finishes, after the last item's own toast.
    if (batchSummary.total > 1 && batchSummary.completed >= batchSummary.total) {
      const s = batchSummary;
      let text = `Converted ${s.ok} of ${s.total} files`;
      if (s.warnings) text += ` · ${s.warnings} with warnings`;
      if (s.failed) text += ` · ${s.failed} failed`;
      showToast(text, s.failed || s.warnings ? "info" : "success");
    }
  }

  function registerBatchItems(count) {
    registerBatchSummary(count);
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
    recordBatchSummary(item, isSuccess);

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

  // ─── Settings Drawer ─────────────────────────────────────────────────────
  // A panel on the right, below the header, with four sections. The gear, the
  // X, Escape and a click outside close it; clicks in the confirm dialog and in
  // toasts do not, since Settings opens those itself and they sit above it.
  // Nothing here runs while it is closed.
  const settingsScrim    = document.getElementById("settingsScrim");
  const settingsCloseBtn = document.getElementById("settingsCloseBtn");
  const settingsNav      = document.getElementById("settingsNav");
  const settingsNavItems = Array.from(document.querySelectorAll("[data-settings-section]"));
  const settingsPanes    = Array.from(document.querySelectorAll("[data-settings-pane]"));
  const appHeader        = document.querySelector(".app-header");
  const SETTINGS_FOCUSABLE = 'button, [href], input, select, textarea, summary, [tabindex]:not([tabindex="-1"])';
  let settingsSection = "general";
  let settingsReturnFocus = null;

  function isSettingsOpen() {
    return settingsPopover.classList.contains("open");
  }

  // The drawer starts where the header ends, so the title bar stays usable.
  // Not rounded: at 125% or 150% display scaling the header ends between
  // pixels, and rounding would leave a hairline gap or overlap.
  function placeSettings() {
    if (!appHeader) return;
    const top = Math.max(0, appHeader.getBoundingClientRect().bottom);
    document.documentElement.style.setProperty("--settings-top", `${top}px`);
  }

  function showSettingsSection(name) {
    if (!settingsPanes.some((pane) => pane.dataset.settingsPane === name)) return;
    settingsSection = name;
    settingsPanes.forEach((pane) => { pane.hidden = pane.dataset.settingsPane !== name; });
    settingsNavItems.forEach((item) => {
      if (item.dataset.settingsSection === name) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
  }

  // Opens Settings from anywhere in the page: on `section` if given (callers
  // outside the gear are all about an engine), otherwise where it was left.
  // When called from a click, that click must not reach the document-level
  // "click outside closes Settings" handler, which would shut it straight away.
  function openSettings(e, section) {
    if (e && typeof e.stopPropagation === "function") e.stopPropagation();
    if (section) showSettingsSection(section);
    if (!isSettingsOpen()) {
      settingsReturnFocus = document.activeElement;
      placeSettings();
      settingsPopover.classList.add("open");
      document.documentElement.classList.add("settings-open"); // moves toasts beside it
      settingsScrim.hidden = false;
      settingsBtn.setAttribute("aria-expanded", "true");
      const current = settingsNavItems.find((item) => item.dataset.settingsSection === settingsSection);
      (current || settingsPopover).focus();
    }
    loadSettings();
    fetchUpdateStatus();
    refreshAddonStatus();
    refreshGlm(true);
  }

  // restoreFocus is false when the user has moved on (clicked elsewhere, or
  // dragged a file in): focus then stays where they put it.
  function closeSettings({ restoreFocus = true } = {}) {
    if (!isSettingsOpen()) return;
    settingsPopover.classList.remove("open");
    document.documentElement.classList.remove("settings-open");
    settingsScrim.hidden = true;
    settingsBtn.setAttribute("aria-expanded", "false");
    const back = settingsReturnFocus;
    settingsReturnFocus = null;
    if (!restoreFocus) return;
    const usable = back && back !== document.body && back.isConnected && back.getClientRects().length > 0;
    (usable ? back : settingsBtn).focus();
  }

  settingsBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (isSettingsOpen()) closeSettings();
    else openSettings(e);
  });

  settingsCloseBtn.addEventListener("click", () => closeSettings());

  // composedPath() is fixed when the click starts, so a button that re-renders
  // its own card still counts as inside.
  document.addEventListener("click", (e) => {
    if (!isSettingsOpen()) return;
    const path = typeof e.composedPath === "function" ? e.composedPath() : [e.target];
    const keepOpen = [settingsPopover, settingsBtn, appDialogOverlay, toastNotification];
    if (path.some((el) => keepOpen.includes(el))) return;
    closeSettings({ restoreFocus: e.target === settingsScrim });
  });

  // A file dragged towards the page is meant for the drop zone under the scrim.
  settingsScrim.addEventListener("dragenter", () => closeSettings({ restoreFocus: false }));

  // While the confirm dialog is up, Escape and Tab belong to it.
  // Tab is always moved here rather than left to the web engine: with macOS's
  // "Keyboard navigation" off (the default), WebKit's own Tab skips buttons and
  // would jump to the URL box behind the drawer.
  document.addEventListener("keydown", (e) => {
    if (!isSettingsOpen() || !appDialogOverlay.hidden) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeSettings();
    } else if (e.key === "Tab") {
      // A radio group is one stop, on its selected option, as browsers do.
      const focusables = Array.from(settingsPopover.querySelectorAll(SETTINGS_FOCUSABLE))
        .filter((el) => !el.disabled && el.getClientRects().length > 0 && !(el.type === "radio" && !el.checked));
      if (focusables.length === 0) return;
      e.preventDefault();
      const idx = focusables.indexOf(document.activeElement);
      const last = focusables.length - 1;
      const next = e.shiftKey
        ? (idx <= 0 ? last : idx - 1)
        : (idx < 0 || idx === last ? 0 : idx + 1);
      focusables[next].focus();
    }
  });

  // Focused on click too: WebKit on macOS doesn't focus clicked buttons, and
  // the arrow keys below start from the focused item.
  settingsNavItems.forEach((item) => {
    item.addEventListener("click", () => {
      showSettingsSection(item.dataset.settingsSection);
      item.focus();
    });
  });

  // Arrow keys move between sections, as in the engine pill group. They start
  // from the focused item, which Tab can move away from the shown section.
  settingsNav.addEventListener("keydown", (e) => {
    const keys = ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"];
    if (!keys.includes(e.key)) return;
    e.preventDefault();
    let idx = settingsNavItems.indexOf(document.activeElement);
    if (idx < 0) idx = settingsNavItems.findIndex((item) => item.dataset.settingsSection === settingsSection);
    if (e.key === "Home") idx = 0;
    else if (e.key === "End") idx = settingsNavItems.length - 1;
    else if (e.key === "ArrowRight" || e.key === "ArrowDown") idx = (idx + 1) % settingsNavItems.length;
    else idx = (idx - 1 + settingsNavItems.length) % settingsNavItems.length;
    showSettingsSection(settingsNavItems[idx].dataset.settingsSection);
    settingsNavItems[idx].focus();
  });

  window.addEventListener("resize", () => {
    if (isSettingsOpen()) placeSettings();
  });
  // ─── End settings drawer ─────────────────────────────────────────────────

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
      if (engines.glm_ocr) renderGlm(engines.glm_ocr);

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
    } catch (_) {
      // Existing behaviour: a failed engine refresh is silent.
    } finally {
      // The add-on's installability is derived from pack state, so it has to be
      // re-read whenever that changes. refreshEngines returns early on several
      // paths, so this belongs in finally rather than at the end of the body.
      refreshAddonStatus();
    }
  }

  // ─── GLM-OCR engine card ──────────────────────────────────────────────────
  // States come from /engines/glm_ocr/status: unreleased ("Coming soon"),
  // unsupported, defective, installable, installing, installed (usable or
  // self-test failed). Status reads the server's completion marker only.
  const engineCardGlmOcr     = document.getElementById("engineCardGlmOcr");
  const glmStatusBadge       = document.getElementById("glmStatusBadge");
  const glmSizeDesc          = document.getElementById("glmSizeDesc");
  const glmModelVersion      = document.getElementById("glmModelVersion");
  const glmRuntimeVersion    = document.getElementById("glmRuntimeVersion");
  const glmSizeInfo          = document.getElementById("glmSizeInfo");
  const glmLicenseInfo       = document.getElementById("glmLicenseInfo");
  const glmAccelInfo         = document.getElementById("glmAccelInfo");
  const glmSpeedInfo         = document.getElementById("glmSpeedInfo");
  const glmPathRow           = document.getElementById("glmPathRow");
  const glmPathInfo          = document.getElementById("glmPathInfo");
  const glmReason            = document.getElementById("glmReason");
  const glmProgressContainer = document.getElementById("glmProgressContainer");
  const glmProgressFill      = document.getElementById("glmProgressFill");
  const glmProgressText      = document.getElementById("glmProgressText");
  const btnGlmInstall        = document.getElementById("btnGlmInstall");
  const btnGlmUpdate         = document.getElementById("btnGlmUpdate");
  const btnGlmVerify         = document.getElementById("btnGlmVerify");
  const btnGlmRemove         = document.getElementById("btnGlmRemove");
  const btnGlmCancel         = document.getElementById("btnGlmCancel");
  const glmOptions           = document.getElementById("glmOptions");
  const glmGpuToggle         = document.getElementById("glmGpuToggle");
  const glmGpuLabel          = document.getElementById("glmGpuLabel");
  const glmGpuCaption        = document.getElementById("glmGpuCaption");
  const glmSourceSelect      = document.getElementById("glmSourceSelect");
  const glmVariantSelect     = document.getElementById("glmVariantSelect");
  const btnGlmSelftest       = document.getElementById("btnGlmSelftest");
  const glmCatalogueInfo     = document.getElementById("glmCatalogueInfo");
  const GLM_BUSY = ["downloading", "extracting", "verifying", "selftest", "installing"];
  let glmPollTimer = null;
  let glmRemoteChecked = false;

  function glmIsUsable() {
    return Boolean(glmStatus && glmStatus.usable);
  }

  function showGlmButtons({ install = false, update = false, verify = false, remove = false, cancel = false }) {
    if (btnGlmInstall) btnGlmInstall.style.display = install ? "inline-flex" : "none";
    if (btnGlmUpdate)  btnGlmUpdate.style.display  = update ? "inline-flex" : "none";
    if (btnGlmVerify)  btnGlmVerify.style.display  = verify ? "inline-flex" : "none";
    if (btnGlmRemove)  btnGlmRemove.style.display  = remove ? "inline-flex" : "none";
    if (btnGlmCancel)  btnGlmCancel.style.display  = cancel ? "inline-flex" : "none";
  }

  function setGlmBadge(text, cls) {
    if (!glmStatusBadge) return;
    glmStatusBadge.textContent = text;
    glmStatusBadge.className = `engine-badge ${cls}`;
  }

  function setGlmReason(text) {
    if (!glmReason) return;
    glmReason.textContent = text || "";
    glmReason.hidden = !text;
  }

  function renderGlmProgress(prog) {
    if (!glmProgressContainer) return;
    const busy = prog && GLM_BUSY.includes(prog.status);
    glmProgressContainer.style.display = busy ? "block" : "none";
    if (!busy) return;
    if (glmProgressFill) glmProgressFill.style.width = `${Math.round(prog.percent || 0)}%`;
    if (glmProgressText) glmProgressText.textContent = formatGlmProgress(prog);
  }

  function renderGlm(st) {
    if (!st) return;
    glmStatus = st;
    const state = st.status;
    if (engineCardGlmOcr) engineCardGlmOcr.style.display = "block";

    const size = formatSize(st.download_size_bytes);
    if (glmSizeDesc) glmSizeDesc.textContent = st.download_size_bytes ? `~${size} download.` : "";
    if (glmSizeInfo) glmSizeInfo.textContent = st.download_size_bytes ? `~${size}` : "—";
    if (glmModelVersion) glmModelVersion.textContent = (st.version && st.version.model) || "—";
    if (glmRuntimeVersion) glmRuntimeVersion.textContent = st.version && st.version.runtime ? `llama.cpp ${st.version.runtime}` : "—";
    if (glmLicenseInfo) glmLicenseInfo.textContent = st.license ? `${st.license} (model and runtime)` : "MIT";
    if (glmAccelInfo) glmAccelInfo.textContent = glmAccelLabel(st.accel_in_use);
    if (glmSpeedInfo) glmSpeedInfo.textContent = st.seconds_per_page ? `~${formatDuration(st.seconds_per_page)} per page` : "Measured after download";
    if (glmPathRow) glmPathRow.style.display = st.install_path ? "flex" : "none";
    if (glmPathInfo) glmPathInfo.textContent = st.install_path || "—";

    // Pill: hidden unless GLM-OCR can be offered here; gated until usable.
    const offer = ["installable", "installing", "installed"].includes(state);
    if (pillGlmOcr) {
      pillGlmOcr.classList.toggle("is-hidden", !offer);
      pillGlmOcr.classList.toggle("is-installable", offer && !st.usable);
      pillGlmOcr.title = st.usable
        ? `GLM-OCR — OCR for scans, photos, math and tables. ~${formatDuration(st.seconds_per_page || 0)} per page here (${glmAccelLabel(st.accel_in_use)}).`
        : (st.installed
            ? "GLM-OCR didn't pass its self-test. Click to open Settings."
            : "GLM-OCR isn't downloaded. Click to open Settings and download it.");
    }
    if (selectedEngine === "glm_ocr" && !st.usable) setEngine("markitdown");

    if (glmOptions) glmOptions.style.display = st.installed ? "block" : "none";
    if (btnGlmInstall) { btnGlmInstall.disabled = false; btnGlmInstall.textContent = "Download GLM-OCR"; }

    if (state === "unreleased") {
      setGlmBadge("Coming soon", "engine-badge-not-installed");
      setGlmReason(st.reason);
      showGlmButtons({});
    } else if (state === "unsupported" || state === "defective") {
      setGlmBadge("Not Available", "engine-badge-unsupported");
      setGlmReason(st.reason);
      showGlmButtons({ install: true });
      if (btnGlmInstall) { btnGlmInstall.disabled = true; btnGlmInstall.textContent = "Not Available"; }
    } else if (state === "installing") {
      setGlmBadge(st.progress && st.progress.status === "selftest" ? "Testing…" : "Downloading…", "engine-badge-busy");
      setGlmReason("");
      showGlmButtons({ cancel: true });
      startGlmPolling();
    } else if (state === "installed") {
      setGlmBadge(st.usable ? "Installed" : "Self-test failed", st.usable ? "engine-badge-installed" : "engine-badge-unsupported");
      setGlmReason(st.reason);
      showGlmButtons({ update: Boolean(st.update_available), verify: true, remove: true });
      if (btnGlmUpdate && st.update) {
        btnGlmUpdate.textContent = `Update available (${formatSize(st.update.size_bytes)})`;
      }
    } else {
      setGlmBadge("Not Installed", "engine-badge-not-installed");
      setGlmReason("");
      showGlmButtons({ install: true });
    }
    renderGlmProgress(st.progress);

    // GPU toggle.
    const gpu = st.gpu || {};
    if (glmGpuToggle) {
      glmGpuToggle.checked = Boolean(gpu.selected);
      glmGpuToggle.disabled = Boolean(gpu.locked) || !gpu.available || state === "installing";
    }
    if (glmGpuLabel) glmGpuLabel.textContent = gpu.locked ? "GPU acceleration (Metal)" : "Use GPU acceleration";
    if (glmGpuCaption) {
      const times = st.selftest && st.selftest.cpu && st.selftest.gpu && st.selftest.gpu.passed
        ? ` Measured: ${formatDuration(st.selftest.gpu.seconds_per_page)} per page on GPU vs ${formatDuration(st.selftest.cpu.seconds_per_page)} on CPU.`
        : "";
      glmGpuCaption.textContent = gpu.locked
        ? (gpu.reason || "On a Mac GLM-OCR uses Metal automatically, with a CPU fallback.") + times
        : gpu.reason
          ? gpu.reason + times
          : gpu.installed
            ? `GPU runtime (Vulkan) installed and tested.${times}`
            : `Downloads a GPU runtime (Vulkan, ~${formatSize(gpu.size_bytes || 33e6)}) on first use and tests it. Works with NVIDIA, AMD and Intel graphics; no CUDA needed.${times}`;
    }

    // Advanced.
    if (glmSourceSelect) glmSourceSelect.value = st.download_source || "auto";
    if (glmVariantSelect) {
      const current = glmVariantSelect.value;
      glmVariantSelect.replaceChildren();
      (st.variants || []).forEach((v) => {
        const opt = document.createElement("option");
        opt.value = v.id;
        opt.textContent = `${v.label} (${formatSize(v.size_bytes)})`;
        glmVariantSelect.appendChild(opt);
      });
      glmVariantSelect.value = st.installed ? st.variant : (current || st.variant);
    }
    if (btnGlmSelftest) btnGlmSelftest.disabled = state === "installing";
    if (glmCatalogueInfo) {
      const remote = st.remote_catalogue || {};
      glmCatalogueInfo.textContent = `Catalogue v${st.catalogue_version} (${st.catalogue_origin === "remote" ? "signed online update" : "built in"}).${remote.reason ? " " + remote.reason : ""}`;
    }
    updateSpeedHint();
  }

  async function refreshGlm(checkRemote = false) {
    const remote = checkRemote && !glmRemoteChecked;
    try {
      const resp = await fetch(`${apiBase}/engines/glm_ocr/status?check_remote=${remote ? "true" : "false"}`);
      if (!resp.ok) return null;
      const st = await resp.json();
      if (remote) glmRemoteChecked = true;
      renderGlm(st);
      return st;
    } catch (_) {
      return null;
    }
  }

  function stopGlmPolling() {
    if (glmPollTimer) {
      clearInterval(glmPollTimer);
      glmPollTimer = null;
    }
  }

  async function pollGlmProgress() {
    try {
      const resp = await fetch(`${apiBase}/engines/glm_ocr/progress`);
      if (!resp.ok) return;
      const prog = await resp.json();
      if (GLM_BUSY.includes(prog.status)) {
        renderGlmProgress(prog);
        setGlmBadge(prog.status === "selftest" ? "Testing…" : (prog.status === "downloading" ? "Downloading…" : "Installing…"), "engine-badge-busy");
        showGlmButtons({ cancel: true });
        return;
      }
      stopGlmPolling();
      if (prog.status === "complete") {
        const done = prog.operation === "selftest" ? "GLM-OCR self-test finished." :
          prog.operation === "gpu" ? "GPU runtime installed and tested." :
          prog.operation === "update" ? "GLM-OCR updated." : "GLM-OCR is downloaded and ready.";
        showToast(done, "success");
      } else if (prog.status === "error") {
        const actions = prog.can_try_mirror && (glmStatus && glmStatus.download_source) !== "mirror_only"
          ? [{ label: "Try the InkDoc mirror", onClick: () => retryGlmFromMirror() }]
          : [];
        showToast(`GLM-OCR: ${prog.error_message || "the operation failed."}`, "error", { actions, duration: 12000 });
      } else if (prog.status === "cancelled") {
        showToast("GLM-OCR download cancelled.", "info");
      }
      await refreshGlm(false);
    } catch (_) {}
  }

  function startGlmPolling() {
    if (glmPollTimer) return;
    glmPollTimer = setInterval(pollGlmProgress, 700);
    pollGlmProgress();
  }

  async function postGlm(path, body) {
    const resp = await fetchWithToken(`${apiBase}/engines/glm_ocr/${path}`, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error((typeof data.detail === "string" && data.detail) || resp.statusText);
    return data;
  }

  async function startGlmInstall(variant) {
    try {
      await postGlm("install", { gpu: false, variant: variant || null });
      showToast("Downloading GLM-OCR…", "info");
      startGlmPolling();
    } catch (err) {
      showToast(`Could not start the download: ${err.message}`, "error");
    }
  }

  async function retryGlmFromMirror() {
    try {
      await fetchWithToken(`${apiBase}/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ glm_ocr_download_source: "mirror_only" }),
      });
    } catch (_) {}
    if (glmStatus && glmStatus.installed && glmStatus.update_available) {
      postGlm("update").then(startGlmPolling).catch((err) => showToast(err.message, "error"));
    } else {
      startGlmInstall(glmVariantSelect ? glmVariantSelect.value : null);
    }
  }

  if (btnGlmInstall) {
    btnGlmInstall.addEventListener("click", async () => {
      const st = glmStatus || (await refreshGlm(true));
      if (!st) return;
      const variant = glmVariantSelect ? glmVariantSelect.value : st.variant;
      const warning = glmDownloadWarning(st, variant);
      const ok = await confirmDialog({
        title: warning.title,
        message: warning.message,
        details: warning.details,
        confirmText: warning.confirmText,
        cancelText: "Not now",
        danger: false,
        focusCancel: true,
      });
      if (!ok) return;
      startGlmInstall(variant);
    });
  }

  if (btnGlmUpdate) {
    btnGlmUpdate.addEventListener("click", async () => {
      const upd = (glmStatus && glmStatus.update) || {};
      const ok = await confirmDialog({
        title: "Update GLM-OCR?",
        message: `Downloads ${formatSize(upd.size_bytes)} (only the parts that changed) and tests them. The current version keeps working until the new one passes its self-test.`,
        confirmText: "Update",
        danger: false,
      });
      if (!ok) return;
      try {
        await postGlm("update");
        startGlmPolling();
      } catch (err) {
        showToast(err.message, "error");
      }
    });
  }

  if (btnGlmCancel) {
    btnGlmCancel.addEventListener("click", async () => {
      try { await postGlm("cancel"); } catch (_) {}
    });
  }

  if (btnGlmVerify) {
    btnGlmVerify.addEventListener("click", async () => {
      const orig = btnGlmVerify.textContent;
      btnGlmVerify.textContent = "Verifying…";
      btnGlmVerify.disabled = true;
      try {
        const res = await postGlm("verify");
        if (res.valid) showToast("GLM-OCR files verified: every hash matches.", "success");
        else showToast(`GLM-OCR verification failed: ${(res.mismatches || [res.reason]).join("; ")}`, "error", { duration: 12000 });
      } catch (err) {
        showToast(err.message, "error");
      } finally {
        btnGlmVerify.textContent = orig;
        btnGlmVerify.disabled = false;
      }
    });
  }

  if (btnGlmRemove) {
    btnGlmRemove.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: "Remove GLM-OCR?",
        message: "The downloaded model and runtime will be deleted. You can download them again later from Settings.",
        confirmText: "Remove",
      });
      if (!ok) return;
      try {
        await postGlm("remove");
        showToast("GLM-OCR removed.", "info");
        if (selectedEngine === "glm_ocr") setEngine("markitdown");
      } catch (err) {
        showToast(`Could not remove GLM-OCR: ${err.message}`, "error");
      }
      refreshGlm(false);
    });
  }

  if (glmGpuToggle) {
    glmGpuToggle.addEventListener("change", async () => {
      const enabled = glmGpuToggle.checked;
      glmGpuToggle.disabled = true;
      try {
        const res = await postGlm("gpu", { enabled });
        if (res.status === "started") {
          showToast("Downloading and testing the GPU runtime…", "info");
          startGlmPolling();
        } else {
          showToast(enabled ? "GLM-OCR will use the GPU." : "GLM-OCR will use the CPU.", "success");
          refreshGlm(false);
        }
      } catch (err) {
        glmGpuToggle.checked = !enabled;
        showToast(err.message, "error", { duration: 10000 });
        refreshGlm(false);
      }
    });
  }

  if (glmSourceSelect) {
    glmSourceSelect.addEventListener("change", async () => {
      try {
        await fetchWithToken(`${apiBase}/settings`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ glm_ocr_download_source: glmSourceSelect.value }),
        });
        if (glmStatus) glmStatus.download_source = glmSourceSelect.value;
      } catch (_) {
        showToast("Could not save the download source.", "error");
      }
    });
  }

  if (glmVariantSelect) {
    glmVariantSelect.addEventListener("change", async () => {
      if (!glmStatus || !glmStatus.installed || glmVariantSelect.value === glmStatus.variant) return;
      const variant = (glmStatus.variants || []).find((v) => v.id === glmVariantSelect.value);
      const ok = await confirmDialog({
        title: `Switch to ${variant ? variant.label : glmVariantSelect.value}?`,
        message: `This downloads the ${variant ? variant.label : ""} model (${formatSize(variant ? variant.size_bytes : 0)}) and tests it. The current model keeps working until then.`,
        confirmText: "Download",
        danger: false,
        focusCancel: true,
      });
      if (!ok) {
        glmVariantSelect.value = glmStatus.variant;
        return;
      }
      startGlmInstall(glmVariantSelect.value);
    });
  }

  if (btnGlmSelftest) {
    btnGlmSelftest.addEventListener("click", async () => {
      try {
        await postGlm("selftest");
        startGlmPolling();
      } catch (err) {
        showToast(err.message, "error");
      }
    });
  }

  // ─── GLM-OCR long-job confirmation ───────────────────────────────────────
  // Before a GLM-OCR job that would take longer than long_job_seconds, ask.
  // A folder drop asks once for the whole batch.
  const GLM_MULTIPAGE = [".pdf", ".tif", ".tiff"];

  async function estimateGlmFile(file) {
    try {
      const form = new FormData();
      form.append("file", file);
      const resp = await fetch(`${apiBase}/convert/estimate?engine=glm_ocr`, { method: "POST", body: form });
      if (!resp.ok) return null;
      return await resp.json();
    } catch (_) {
      return null;
    }
  }

  async function confirmGlmJob(files) {
    if (!glmStatus || !glmStatus.usable) return true; // the server answers 409 with the reason
    const spp = glmStatus.seconds_per_page || 30;
    const limit = glmStatus.long_job_seconds || 300;
    let total = glmStatus.load_seconds || 0;
    for (const file of files) {
      const name = (file.name || "").toLowerCase();
      if (GLM_MULTIPAGE.some((ext) => name.endsWith(ext))) {
        const est = await estimateGlmFile(file);
        total += est && est.pages ? est.pages * spp : spp;
      } else {
        total += spp;
      }
      if (total > limit * 50) break; // already far past the limit
    }
    if (total <= limit) return true;
    const prompt = glmLongJobPrompt(total, glmStatus.accel_in_use, files.length);
    return confirmDialog({
      title: prompt.title,
      message: prompt.message,
      confirmText: "Convert",
      cancelText: "Cancel",
      danger: false,
    });
  }

  function markNotConverted(item) {
    item.status = "cancelled";
    item.error = "Not converted.";
    item.errorType = "cancelled";
    renderQueue();
    handleBatchItemCompleted(item);
  }

  // ─── Recognition-Model Add-On ────────────────────────────────────────────
  // The two enrichment toggles are inert without the CodeFormula weights, and
  // Docling resolves models only against the pack's pinned artifacts_path, so
  // there is no way to just try it: the model is either installed there or the
  // conversion fails. This renders the four states the backend already
  // distinguishes (unavailable / installable / busy / usable) and clears the
  // toggles' disabled attribute only when it reports usable.
  const ADDON_NAME = "code_enrichment";
  const ADDON_BUSY_STATES = ["downloading", "verifying", "extracting", "installing"];
  let addonPollInterval = null;

  function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return null;
    const mb = bytes / (1024 * 1024);
    return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(0)} MB`;
  }

  function setEnrichmentTogglesEnabled(enabled) {
    [doclingCodeEnrichmentToggle, doclingFormulaEnrichmentToggle].forEach((el) => {
      if (!el) return;
      el.disabled = !enabled;
      // A checked-but-disabled toggle would claim enrichment is on while the
      // engine refuses the job for want of the model. Assigning checked does not
      // dispatch a change event, so this does not write anything back.
      if (!enabled) el.checked = false;
    });
  }

  function showAddonButtons({ install, verify, remove, cancel }) {
    if (btnAddonInstall) btnAddonInstall.style.display = install ? "inline-flex" : "none";
    if (btnAddonVerify)  btnAddonVerify.style.display  = verify  ? "inline-flex" : "none";
    if (btnAddonRemove)  btnAddonRemove.style.display  = remove  ? "inline-flex" : "none";
    if (btnAddonCancel)  btnAddonCancel.style.display  = cancel  ? "inline-flex" : "none";
  }

  function applyAddonStatus(st) {
    if (!st || !addonStatusBadge) return;

    if (addonTitleEl && st.title) addonTitleEl.textContent = st.title;

    const progress = st.progress || {};
    const size = formatBytes(st.size_bytes);

    if (ADDON_BUSY_STATES.includes(progress.status)) {
      // Progress polling owns the badge and buttons while an install runs.
      startAddonPolling();
      return;
    }

    if (addonProgressContainer) addonProgressContainer.style.display = "none";

    if (st.usable) {
      addonStatusBadge.textContent = "Installed";
      addonStatusBadge.className = "engine-badge engine-badge-installed";
      if (addonReason) {
        addonReason.textContent =
          "Recognition model installed. Both enrichment options are available.";
      }
      showAddonButtons({ install: false, verify: true, remove: true, cancel: false });
      setEnrichmentTogglesEnabled(true);
      // Now that the controls are live, re-read the persisted values they were
      // held back from showing.
      loadSettings();
      return;
    }

    // Not usable from here on, so the toggles must stay disabled.
    setEnrichmentTogglesEnabled(false);
    addonStatusBadge.className = "engine-badge engine-badge-not-installed";

    if (st.installed) {
      // On disk, but blocked by a pack or app version requirement.
      addonStatusBadge.textContent = "Unusable";
      if (addonReason) {
        addonReason.textContent =
          st.reason || "The installed model cannot be used by this build.";
      }
      showAddonButtons({ install: false, verify: true, remove: true, cancel: false });
      return;
    }

    if (st.installable) {
      addonStatusBadge.textContent = "Not Installed";
      if (addonReason) {
        addonReason.textContent = size
          ? `Optional ${size} download. Required by both enrichment options.`
          : "Optional download. Required by both enrichment options.";
      }
      if (btnAddonInstall) {
        btnAddonInstall.disabled = false;
        btnAddonInstall.textContent = size ? `Install Model (${size})` : "Install Model";
        btnAddonInstall.title = st.description || "";
      }
      showAddonButtons({ install: true, verify: false, remove: false, cancel: false });
      return;
    }

    // Not installable. Either the pack is missing or too old, or the add-on has
    // no published artifact yet (empty url/sha256 in addons.json), which is the
    // expected state before the model ships.
    addonStatusBadge.textContent = st.available ? "Unavailable" : "Not Released";
    if (addonReason) {
      addonReason.textContent =
        st.reason ||
        (st.available
          ? "This add-on cannot be installed right now."
          : "The recognition model has not been published yet. These options will become available in a future release.");
    }
    if (btnAddonInstall) {
      btnAddonInstall.disabled = true;
      btnAddonInstall.textContent = "Install Model";
    }
    showAddonButtons({ install: true, verify: false, remove: false, cancel: false });
  }

  async function refreshAddonStatus() {
    if (!addonStatusBadge) return;
    try {
      const resp = await fetch(`${apiBase}/addons/${ADDON_NAME}`);
      if (!resp.ok) return;
      applyAddonStatus(await resp.json());
    } catch (_) {}
  }

  function stopAddonPolling() {
    if (addonPollInterval) {
      clearInterval(addonPollInterval);
      addonPollInterval = null;
    }
  }

  async function pollAddonProgress() {
    try {
      const resp = await fetch(`${apiBase}/addons/${ADDON_NAME}`);
      if (!resp.ok) return;
      const st = await resp.json();
      const prog = st.progress || {};
      const status = prog.status || "idle";

      if (ADDON_BUSY_STATES.includes(status)) {
        const pct = Math.round(prog.percent || 0);
        const label = status.charAt(0).toUpperCase() + status.slice(1);
        if (addonProgressContainer) addonProgressContainer.style.display = "block";
        if (addonProgressFill) addonProgressFill.style.width = `${pct}%`;
        if (addonProgressText) {
          const done = formatBytes(prog.bytes_downloaded);
          const total = formatBytes(prog.total_bytes);
          const detail = done && total ? ` — ${done} of ${total}` : "";
          addonProgressText.textContent = `${label} (${pct}%)${detail}`;
        }
        if (addonStatusBadge) {
          addonStatusBadge.textContent = `${label}…`;
          addonStatusBadge.className = "engine-badge engine-badge-busy";
        }
        showAddonButtons({ install: false, verify: false, remove: false, cancel: true });
        setEnrichmentTogglesEnabled(false);
        return;
      }

      stopAddonPolling();

      if (status === "complete") {
        showToast("Recognition model installed. Enrichment options are now available.", "success");
      } else if (status === "error") {
        showToast(`Model installation failed: ${prog.error_message || "Unknown error"}`, "error");
      } else if (status === "cancelled") {
        showToast("Model installation cancelled.", "info");
      }
      applyAddonStatus(st);
    } catch (_) {}
  }

  function startAddonPolling() {
    stopAddonPolling();
    addonPollInterval = setInterval(pollAddonProgress, 600);
    pollAddonProgress();
  }

  if (btnAddonInstall) {
    btnAddonInstall.addEventListener("click", async () => {
      btnAddonInstall.disabled = true;
      btnAddonInstall.textContent = "Starting…";
      try {
        const resp = await fetchWithToken(`${apiBase}/addons/${ADDON_NAME}/install`, {
          method: "POST",
        });
        if (resp.ok) {
          showToast("Downloading the recognition model…", "info");
          startAddonPolling();
        } else {
          const err = await resp.json().catch(() => ({ detail: "Failed to start install" }));
          showToast(`Error: ${err.detail}`, "error");
          await refreshAddonStatus();
        }
      } catch (e) {
        showToast(`Error starting model download: ${e.message}`, "error");
        await refreshAddonStatus();
      }
    });
  }

  if (btnAddonCancel) {
    btnAddonCancel.addEventListener("click", async () => {
      try {
        await fetchWithToken(`${apiBase}/addons/${ADDON_NAME}/cancel`, { method: "POST" });
        showToast("Cancelling model download…", "info");
      } catch (_) {}
    });
  }

  if (btnAddonVerify) {
    btnAddonVerify.addEventListener("click", async () => {
      const origText = btnAddonVerify.textContent;
      btnAddonVerify.textContent = "Verifying…";
      btnAddonVerify.disabled = true;
      try {
        const resp = await fetchWithToken(`${apiBase}/addons/${ADDON_NAME}/verify`, {
          method: "POST",
        });
        const res = await resp.json();
        if (res.valid) {
          showToast("Integrity check passed — model files match their SHA-256 hashes.", "success");
        } else {
          showToast(`Integrity check failed: ${res.reason || "Hash mismatch"}`, "error");
        }
      } catch (e) {
        showToast(`Verification failed: ${e.message}`, "error");
      } finally {
        btnAddonVerify.textContent = origText;
        btnAddonVerify.disabled = false;
      }
    });
  }

  if (btnAddonRemove) {
    btnAddonRemove.addEventListener("click", async () => {
      const confirmed = await confirmDialog({
        title: "Remove recognition model?",
        message:
          "The code and formula recognition weights will be deleted and both enrichment options will be turned off. You can reinstall the model later from Settings.",
        confirmText: "Remove Model",
        cancelText: "Cancel",
        danger: true,
      });
      if (!confirmed) return;
      try {
        const resp = await fetchWithToken(`${apiBase}/addons/${ADDON_NAME}/remove`, {
          method: "POST",
        });
        if (resp.ok) {
          showToast("Recognition model removed.", "info");
          // The server turns both flags off when the model goes away, so reload
          // the settings to match what was actually persisted.
          await loadSettings();
          await refreshAddonStatus();
        } else {
          showToast("Failed to remove the recognition model.", "error");
        }
      } catch (e) {
        showToast(`Error removing model: ${e.message}`, "error");
      }
    });
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
        if (qualityCheckToggle) {
          qualityCheckToggle.checked = s.quality_check_enabled !== false;
        }
        // Guarded by .disabled so this cannot race refreshAddonStatus() into
        // showing a checked toggle the add-on state says is unusable. Whichever
        // of the two lands last, the result is the same.
        if (doclingCodeEnrichmentToggle && !doclingCodeEnrichmentToggle.disabled) {
          doclingCodeEnrichmentToggle.checked = Boolean(s.docling_code_enrichment);
        }
        if (doclingFormulaEnrichmentToggle && !doclingFormulaEnrichmentToggle.disabled) {
          doclingFormulaEnrichmentToggle.checked = Boolean(s.docling_formula_enrichment);
        }
        if (s.layout_width !== undefined) syncLayoutWidth(s.layout_width);
        if (s.preview_text_size !== undefined) syncTextSize(s.preview_text_size);
        if (s.render_math !== undefined) syncRenderMath(s.render_math);
        // Last, so nothing in the save folder row can stop the toggles above.
        renderSaveFolder(s.save_folder);
      }
    } catch (_) {}
  }

  // Both enrichment toggles persist the same way and differ only in their key
  // and wording. Registered once at startup, not from loadSettings(): that runs
  // again every time the settings popover is opened, and re-registering there
  // would stack a fresh listener per open, firing one POST and one toast per
  // accumulated listener on a single click.
  function wireEnrichmentToggle(el, key, label) {
    if (!el) return;
    el.addEventListener("change", async (e) => {
      const checked = e.target.checked;
      try {
        const resp = await fetchWithToken(`${apiBase}/settings`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ [key]: checked }),
        });
        if (resp.ok) {
          showToast(
            checked
              ? `${label} enabled. Docling will load the recognition model for every conversion.`
              : `${label} disabled.`,
            "info"
          );
        } else {
          // The server refuses to enable either flag while the add-on is not
          // usable, so surface its reason rather than a generic failure.
          const err = await resp.json().catch(() => ({}));
          showToast(err.detail || `Failed to update ${label}.`, "error");
          e.target.checked = !checked;
        }
      } catch {
        showToast(`Failed to update ${label}.`, "error");
        e.target.checked = !checked;
      }
    });
  }

  wireEnrichmentToggle(doclingCodeEnrichmentToggle, "docling_code_enrichment", "Code Enrichment");
  wireEnrichmentToggle(doclingFormulaEnrichmentToggle, "docling_formula_enrichment", "Formula Enrichment");

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

  if (qualityCheckToggle) {
    qualityCheckToggle.addEventListener("change", async (e) => {
      const checked = e.target.checked;
      try {
        const resp = await fetchWithToken(`${apiBase}/settings`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ quality_check_enabled: checked }),
        });
        if (resp.ok) {
          showToast(
            checked
              ? "PDFs will be checked for missing text."
              : "Missing-text check turned off.",
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

  // ─── Layout width ────────────────────────────────────────────────────────
  // How much of a wide window InkDoc uses (Settings → General → Display). The
  // server puts the saved choice on <html> before first paint, so the page
  // never jumps on load. It is saved in settings.json, not browser storage,
  // which the desktop app's web view clears on every launch. A change applies
  // at once and is undone if it can't be saved.
  const LAYOUT_WIDTHS = ["standard", "wide", "full"];
  const layoutWidthRadios = Array.from(document.querySelectorAll('input[name="layoutWidth"]'));
  let layoutWidth = applyLayoutWidth(document.documentElement.dataset.layoutWidth);
  let layoutWidthSaves = 0; // newest save wins; loadSettings waits for it

  function applyLayoutWidth(value) {
    const width = LAYOUT_WIDTHS.includes(value) ? value : "standard";
    if (document.documentElement.dataset.layoutWidth !== width) {
      document.documentElement.dataset.layoutWidth = width;
    }
    layoutWidthRadios.forEach((radio) => { radio.checked = radio.value === width; });
    return width;
  }

  // From GET /settings, each time Settings opens. Skipped while a change is
  // being saved, so a slower read can't put the old choice back.
  function syncLayoutWidth(value) {
    if (layoutWidthSaves === 0) layoutWidth = applyLayoutWidth(value);
  }

  layoutWidthRadios.forEach((radio) => {
    radio.addEventListener("change", async () => {
      if (!radio.checked || radio.value === layoutWidth) return;
      const previous = layoutWidth;
      layoutWidth = applyLayoutWidth(radio.value);
      const save = ++layoutWidthSaves;
      let saved = false;
      try {
        const resp = await fetchWithToken(`${apiBase}/settings`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ layout_width: layoutWidth }),
        });
        saved = resp.ok;
      } catch (_) {}
      if (save !== layoutWidthSaves) return; // a newer choice is on its way
      layoutWidthSaves = 0;
      if (!saved) {
        layoutWidth = applyLayoutWidth(previous);
        showToast("Couldn't save the layout width.", "error");
      }
    });
  });
  // ─── End layout width ────────────────────────────────────────────────────

  // ─── Preview text size ───────────────────────────────────────────────────
  // A− and A+ (Settings → General → Display) step through seven sizes for the
  // document preview, Rendered and Raw. As with the layout width, the server
  // puts the saved step on <html> before first paint and settings.json keeps
  // it. A click applies at once (the live sample and the preview follow through
  // CSS); the save waits until the clicking stops, so several clicks are one
  // write, and only one save is ever in flight.
  const TEXT_SIZES = [85, 90, 100, 110, 120, 135, 150];
  const TEXT_SIZE_SAVE_DELAY_MS = 400;
  const btnTextSmaller = document.getElementById("btnTextSmaller");
  const btnTextLarger  = document.getElementById("btnTextLarger");
  const textSizeStatus = document.getElementById("textSizeStatus");
  let textSize = applyTextSize(Number(document.documentElement.dataset.textSize));
  let textSizeSaved = textSize; // what settings.json is known to hold
  let textSizeTimer = null;
  let textSizeSaving = false;

  // aria-disabled rather than disabled at the ends: a disabled button would
  // drop keyboard focus the moment it reached the limit.
  function applyTextSize(value) {
    const size = TEXT_SIZES.includes(value) ? value : 100;
    if (document.documentElement.dataset.textSize !== String(size)) {
      document.documentElement.dataset.textSize = String(size);
    }
    if (btnTextSmaller) btnTextSmaller.setAttribute("aria-disabled", String(size === TEXT_SIZES[0]));
    if (btnTextLarger) btnTextLarger.setAttribute("aria-disabled", String(size === TEXT_SIZES[TEXT_SIZES.length - 1]));
    return size;
  }

  function stepTextSize(direction) {
    const idx = TEXT_SIZES.indexOf(textSize) + direction;
    if (idx < 0 || idx >= TEXT_SIZES.length) return;
    textSize = applyTextSize(TEXT_SIZES[idx]);
    // No size is shown on screen; screen readers are told.
    if (textSizeStatus) textSizeStatus.textContent = `Preview text size ${textSize}%`;
    clearTimeout(textSizeTimer);
    textSizeTimer = setTimeout(saveTextSize, TEXT_SIZE_SAVE_DELAY_MS);
  }

  async function saveTextSize() {
    textSizeTimer = null;
    if (textSizeSaving) {
      textSizeTimer = setTimeout(saveTextSize, TEXT_SIZE_SAVE_DELAY_MS);
      return;
    }
    const value = textSize;
    if (value === textSizeSaved) return;
    textSizeSaving = true;
    let saved = false;
    try {
      const resp = await fetchWithToken(`${apiBase}/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ preview_text_size: value }),
      });
      saved = resp.ok;
    } catch (_) {}
    textSizeSaving = false;
    if (saved) {
      textSizeSaved = value;
    } else if (textSize === value && !textSizeTimer) {
      // Not moved on since: show what is actually saved again.
      textSize = applyTextSize(textSizeSaved);
      showToast("Couldn't save the text size.", "error");
    }
  }

  // From GET /settings, each time Settings opens; never over a pending change.
  function syncTextSize(value) {
    if (textSizeTimer || textSizeSaving) return;
    textSize = textSizeSaved = applyTextSize(Number(value));
  }

  if (btnTextSmaller) btnTextSmaller.addEventListener("click", () => stepTextSize(-1));
  if (btnTextLarger) btnTextLarger.addEventListener("click", () => stepTextSize(1));
  // ─── End preview text size ───────────────────────────────────────────────

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
    if (aboutVersion) aboutVersion.textContent = `Version ${s.current_version || "—"}`;

    const state = s.state || "idle";
    // Marks Settings → Updates while an update waits or is on its way. Left as
    // is while checking, so "Check again" doesn't make it blink.
    if (settingsUpdateDot && state !== "checking") {
      settingsUpdateDot.hidden = !["available", "downloading", "verifying", "ready_to_install", "ready_to_reveal"].includes(state);
    }

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
          if (window.DOMPurify) {
            const cleanHtml = window.DOMPurify.sanitize(marked.parse(s.release_notes));
            if (updateNotesContent) updateNotesContent.innerHTML = cleanHtml;
          } else if (updateNotesContent) {
            // No sanitiser loaded: show the notes as text, never as HTML.
            updateNotesContent.textContent = s.release_notes;
          }
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
      const confirmed = await confirmDialog({
        title: "Remove Docling pack?",
        message: "All installed IBM Docling files and caches will be deleted. You can reinstall the pack later from Settings.",
        confirmText: "Remove Pack",
        cancelText: "Cancel",
        danger: true,
      });
      if (!confirmed) {
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

  // pywebview injects its api asynchronously; queue calls until it exists.
  // Used by the custom title bar controls below and by in-page external
  // links (window.pywebview exists in the desktop app on every OS, not just
  // where the frameless custom title bar is used).
  function withWindowApi(fn) {
    if (window.pywebview && window.pywebview.api) {
      fn(window.pywebview.api);
    } else {
      window.addEventListener("pywebviewready", () => {
        if (window.pywebview && window.pywebview.api) fn(window.pywebview.api);
      }, { once: true });
    }
  }

  // ─── Links in converted documents and release notes ─────────────────────
  // In the desktop app a plain link would load the other page inside InkDoc's
  // own window. Web links open in the system browser instead, and no other
  // link may replace the app; links to a heading in the same document still
  // scroll. In a browser tab (Web Bench) links behave as before.
  function handleContentLinkClick(e) {
    if (!window.pywebview) return;
    const link = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!link) return;
    const href = link.getAttribute("href") || "";
    if (href.startsWith("#")) return;
    e.preventDefault();
    if (/^https?:\/\//i.test(href)) {
      withWindowApi((api) => Promise.resolve(api.open_external_link(href)).catch(() => {}));
    }
  }
  if (renderedOutput) renderedOutput.addEventListener("click", handleContentLinkClick);
  if (updateNotesContent) updateNotesContent.addEventListener("click", handleContentLinkClick);
  // ─── End content links ───────────────────────────────────────────────────

  // ─── External Links (Settings) ─────────────────────────────────────────────
  // Inside the desktop app, route through pywebview so links open in the OS
  // default browser instead of navigating the app's own window. In a plain
  // browser tab (Web Bench) there's no window.pywebview: let the anchor's
  // normal target="_blank" behavior handle it.
  const aboutUsLink = document.getElementById("aboutUsLink");
  if (aboutUsLink) {
    aboutUsLink.addEventListener("click", (e) => {
      if (window.pywebview) {
        e.preventDefault();
        withWindowApi((api) => api.open_external_link(aboutUsLink.href).catch(() => {}));
      }
    });
  }

  // ─── Custom title bar (frameless desktop window only) ────────────────────
  // The flag is injected into the page before first paint, so the controls never
  // flash in. It is false in a browser tab and on macOS and Linux, which keep
  // their native window frames.
  if (window.__INKDOC_CUSTOM_TITLEBAR__ === true) {
    document.body.classList.add("has-custom-titlebar");
    const maximizeBtn = document.getElementById("winMaximize");

    function setMaximizeAffordance(isMaximized) {
      if (!maximizeBtn) return;
      maximizeBtn.title = isMaximized ? "Restore" : "Maximise";
      maximizeBtn.setAttribute("aria-label", maximizeBtn.title);
    }

    function toggleMaximize() {
      withWindowApi((api) =>
        api.toggle_maximize_window().then(setMaximizeAffordance).catch(() => {})
      );
    }

    // Windows will only offer Snap Layouts over a maximise button it can
    // hit-test, and WebView2's own child window sits in front of ours. The
    // native side cuts this button's rectangle out of that child, which means
    // it also has to draw the button -- so it needs both the exact rectangle
    // and the active theme's colours. Only the page knows either: they move
    // with layout, DPI and the theme toggle.
    function cssColor(name, fallback) {
      const raw = getComputedStyle(document.documentElement)
        .getPropertyValue(name)
        .trim();
      const m = raw.match(/^#([0-9a-f]{6})$/i);
      if (m) return parseInt(m[1], 16);
      const rgb = raw.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
      if (rgb) return (+rgb[1] << 16) | (+rgb[2] << 8) | +rgb[3];
      return fallback;
    }

    function reportTitlebarChrome() {
      if (!maximizeBtn || !window.pywebview || !window.pywebview.api) return;
      const api = window.pywebview.api;
      const r = maximizeBtn.getBoundingClientRect();
      if (r.width && r.height && api.set_titlebar_button_rect) {
        const scale = window.devicePixelRatio || 1;
        const call = api.set_titlebar_button_rect(
          Math.round(r.left * scale),
          Math.round(r.top * scale),
          Math.round(r.right * scale),
          Math.round(r.bottom * scale)
        );
        if (call && call.catch) call.catch(() => {});
      }
      if (api.set_titlebar_colors) {
        const call = api.set_titlebar_colors(
          cssColor("--elevated", 0x1b1e18),
          cssColor("--sunken", 0x0f110c),
          cssColor("--muted", 0x8e968a),
          cssColor("--ink", 0xe8ede6)
        );
        if (call && call.catch) call.catch(() => {});
      }
    }

    // Exposed so the theme toggle can re-send the palette.
    window.__inkdocSyncTitlebar = reportTitlebarChrome;

    window.__inkdocMaximizeState = (isMaximized) => {
      setMaximizeAffordance(!!isMaximized);
      reportTitlebarChrome();
    };

    withWindowApi(() => reportTitlebarChrome());
    window.addEventListener("resize", reportTitlebarChrome);

    const minimizeBtn = document.getElementById("winMinimize");
    const closeBtn = document.getElementById("winClose");
    if (minimizeBtn) {
      minimizeBtn.addEventListener("click", () =>
        withWindowApi((api) => api.minimize_window().catch(() => {}))
      );
    }
    if (maximizeBtn) maximizeBtn.addEventListener("click", toggleMaximize);
    if (closeBtn) {
      closeBtn.addEventListener("click", () =>
        withWindowApi((api) => api.close_window().catch(() => {}))
      );
    }

    // Double-clicking the title bar toggles maximise, as every platform expects.
    document.querySelectorAll(".pywebview-drag-region").forEach((region) => {
      region.addEventListener("dblclick", toggleMaximize);
    });
  }

  // ─── Theme Toggle ────────────────────────────────────────────────────────
  const savedTheme = readStorage("inkdoc-theme") || readStorage("markitdown-theme") || "dark";
  document.documentElement.setAttribute("data-theme", savedTheme);
  // The theme the last click asked for. A View Transition applies it a frame
  // later, so a quick second click must not read it back from the page.
  let requestedTheme = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    writeStorage("inkdoc-theme", theme);
    // The maximise button is drawn natively, so it can't pick up the new
    // palette from CSS on its own.
    if (window.__inkdocSyncTitlebar) window.__inkdocSyncTitlebar();
  }

  // The new theme grows as a circle from the button, as on the website, where
  // the web engine supports View Transitions. Without them, or with reduced
  // motion, it switches at once as before.
  themeToggle.addEventListener("click", () => {
    requestedTheme = requestedTheme === "dark" ? "light" : "dark";
    const next = requestedTheme;
    const root = document.documentElement;
    const reduceMotion = typeof window.matchMedia === "function"
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduceMotion || typeof document.startViewTransition !== "function") {
      applyTheme(next);
      return;
    }

    const r = themeToggle.getBoundingClientRect();
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    const radius = Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y));
    // Pause the colour fades, or the circle would reveal half-faded colours.
    root.classList.add("theme-switching");
    const finish = () => root.classList.remove("theme-switching");

    let transition;
    try {
      transition = document.startViewTransition(() => applyTheme(next));
    } catch (_) {
      applyTheme(next);
      finish();
      return;
    }
    // Short on purpose: while a View Transition plays, the engine sends clicks
    // and drops to the page background, where they are ignored.
    transition.ready
      .then(() => {
        root.animate(
          { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
          { duration: 350, easing: "cubic-bezier(.2,.8,.2,1)", pseudoElement: "::view-transition-new(root)" }
        );
      })
      .catch(() => {});
    transition.finished.then(finish, finish);
  });

  // ─── Auto-save Toggle ────────────────────────────────────────────────────
  autoSaveToggle.addEventListener("change", (e) => {
    autoSaveToDownloads = e.target.checked;
    showToast(
      autoSaveToDownloads
        ? `Converted files will also be saved to ${saveFolderState ? saveFolderState.path : "Downloads"}`
        : "Auto-save disabled",
      "info"
    );
  });

  // ─── File Browsing ───────────────────────────────────────────────────────
  btnBrowseFiles.addEventListener("click", () => fileInput.click());
  // Folder picking has two routes, and the difference is which dialog the user
  // gets. A webkitdirectory input makes Chromium draw its own "upload N files?"
  // confirmation, which lives outside the page and cannot be styled or
  // suppressed. showDirectoryPicker() instead raises a permission request that
  // the desktop shell auto-grants (see runner._configure_webview2), so the only
  // confirmation left is ours, matching every other dialog in the app. The
  // input stays as the fallback for the Web Bench and for any engine without
  // the File System Access API, where the browser's prompt is unavoidable.
  const SKIP_DIRS = new Set([".git", "node_modules", "__pycache__", ".venv"]);

  async function collectDirectoryFiles(handle, path = "", out = []) {
    for await (const entry of handle.values()) {
      if (entry.kind === "directory") {
        if (SKIP_DIRS.has(entry.name)) continue;
        await collectDirectoryFiles(entry, `${path}${entry.name}/`, out);
      } else {
        const file = await entry.getFile();
        // Mirror what a webkitdirectory input reports, so anything downstream
        // that wants the path inside the folder still finds it.
        try {
          Object.defineProperty(file, "webkitRelativePath", {
            value: `${path}${entry.name}`,
            configurable: true,
          });
        } catch { /* read-only in some engines; the name alone is enough */ }
        out.push(file);
      }
    }
    return out;
  }

  btnBrowseFolder.addEventListener("click", async () => {
    if (!window.showDirectoryPicker) {
      folderInput.click();
      return;
    }
    let handle;
    try {
      handle = await window.showDirectoryPicker({ id: "inkdoc-folder", mode: "read" });
    } catch {
      return; // the user dismissed the picker
    }

    let files;
    try {
      files = await collectDirectoryFiles(handle);
    } catch (e) {
      showToast(`Could not read that folder: ${e.message}`, "error");
      return;
    }
    if (files.length === 0) {
      showToast(`No files found in "${handle.name}".`, "info");
      return;
    }

    const confirmed = await confirmDialog({
      title: `Add ${files.length} file${files.length === 1 ? "" : "s"}?`,
      message: `"${handle.name}" contains ${files.length} file${files.length === 1 ? "" : "s"}, which will be converted with ${getEngineDisplayName(selectedEngine)}.`,
      confirmText: "Add Files",
      cancelText: "Cancel",
      danger: false,
    });
    if (!confirmed) return;

    handleIncomingFiles(files);
  });

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

  // Word count is computed once here rather than on every preview render, which
  // re-ran split(/\s+/) and built an array of every word each time an item in the
  // queue was clicked.
  function setItemMarkdown(item, md) {
    item.markdown = md || "";
    let words = 0;
    const re = /\S+/g;
    while (re.exec(item.markdown) !== null) words++;
    item.wordCount = words;
  }

  // ─── Queue Management ────────────────────────────────────────────────────
  function handleIncomingFiles(files) {
    if (files.length === 0) return;
    registerBatchItems(files.length);
    showWorkbench();

    const pending = files.map((file) => {
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
      return { item, file };
    });
    renderQueue();
    const start = () => pending.forEach(({ item, file }) => convertFileItem(item, file));
    if (selectedEngine === "glm_ocr") {
      confirmGlmJob(files).then((ok) => {
        if (ok) start();
        else pending.forEach(({ item }) => markNotConverted(item));
      });
    } else {
      start();
    }
  }

  // ─── Live progress & cancel ─────────────────────────────────────────────
  // Each conversion carries a job_id; while it runs, the row shows the phase the
  // server reports ("Checking…", "Retrying with Docling…") and offers Cancel.
  const PROGRESS_POLL_MS = 1000;

  function stopProgressPoll(item) {
    if (item.progressTimer) {
      clearInterval(item.progressTimer);
      item.progressTimer = null;
    }
  }

  function startProgressPoll(item) {
    stopProgressPoll(item);
    const jobId = item.jobId;
    if (!jobId) return;
    item.progressTimer = setInterval(async () => {
      if (item.jobId !== jobId || item.status !== "converting") {
        stopProgressPoll(item);
        return;
      }
      try {
        const resp = await fetch(`${apiBase}/convert/progress/${encodeURIComponent(jobId)}`);
        if (!resp.ok) return;
        const p = await resp.json();
        if (item.jobId !== jobId || item.status !== "converting" || item.cancelRequested) return;
        const text = formatJobProgress(p);
        if (text && text !== item.progressMessage) {
          item.progressMessage = text;
          renderQueue();
        }
      } catch (_) {}
    }, PROGRESS_POLL_MS);
  }

  async function cancelItem(item) {
    if (!item.jobId || item.status !== "converting" || item.cancelRequested) return;
    item.cancelRequested = true;
    item.progressMessage = "Cancelling…";
    renderQueue();
    try {
      await fetch(`${apiBase}/convert/cancel/${encodeURIComponent(item.jobId)}`, { method: "POST" });
    } catch (_) {}
  }

  // Resets an item for a new run and returns that run's job id. Responses that
  // arrive for an older run (a re-convert started meanwhile) are ignored.
  function beginConversion(item, engine) {
    item.engine          = engine;
    item.status          = "converting";
    item.jobId           = newJobId();
    item.progressMessage = "";
    item.cancelRequested = false;
    item.error           = "";
    item.errorType       = "";
    item.auto            = null;
    item.quality         = null;
    item.warnings        = [];
    renderQueue();
    startProgressPoll(item);
    return item.jobId;
  }

  async function readErrorDetail(resp, fallback) {
    const errData = await resp.json().catch(() => ({ detail: resp.statusText }));
    return (typeof errData.detail === "string" && errData.detail) || fallback;
  }

  function applyConversionError(item, httpStatus, msg, engineToUse, isUrl) {
    if (httpStatus === 409 && msg === "conversion_cancelled") {
      item.status    = "cancelled";
      item.error     = "Conversion cancelled. Nothing was saved.";
      item.errorType = "cancelled";
      selectItem(item.id);
      showToast(`Cancelled: ${item.name}`, "info");
      return;
    }

    const isGlm = engineToUse === "glm_ocr" || msg.includes("GLM-OCR");
    if (msg.includes("engine_not_installed") || msg.includes("engine_not_ready")) {
      item.status = "failed";
      item.error = !isGlm
        ? "Docling is not installed. Open Settings to install the engine pack."
        : msg.includes("engine_not_ready")
          ? "GLM-OCR didn't pass its self-test. Open Settings to run it again."
          : "GLM-OCR isn't downloaded. Open Settings to download it.";
      item.errorType = "failed";
      selectItem(item.id);
      showToast(isGlm ? item.error : "Docling is not installed. Install in Settings.", "error");
      openSettings(null, "engines");
      return;
    }

    if (httpStatus === 400 && msg.includes("engine_unsupported")) {
      item.status = "unsupported";
      item.error = isGlm
        ? "GLM-OCR is not available on this platform."
        : "Docling is not supported on this platform architecture.";
      item.errorType = "unsupported";
      selectItem(item.id);
      showToast(isGlm ? item.error : "Docling is not supported on this platform.", "error");
      return;
    }

    // Any other 409 is not about Docling (an update being applied, for one), so
    // it is shown as the server worded it.
    const errType = httpStatus === 409 ? "failed" : classifyError(msg);
    item.status    = errType; // "unsupported" or "failed"
    item.error     = msg;
    item.errorType = errType;
    selectItem(item.id);
    if (errType === "unsupported") {
      showToast(`${getEngineDisplayName(engineToUse)} does not support this ${isUrl ? "URL type" : "format"}`, "error");
    } else {
      showToast(isUrl || httpStatus === 409 ? `Failed: ${msg}` : `Failed: ${item.name}`, "error");
    }
  }

  function maybeShowHintToast(hint) {
    if (!shouldShowHintToast(hint, hintSession, readStorage)) return false;
    hintSession.hintShown = true;
    showToast(hint, "info", {
      duration: 9000,
      actions: [
        { label: hint.includes("Docling") ? "Install Docling" : "Open Settings", onClick: (e) => openSettings(e, "engines") },
        { label: "Don't show again", onClick: () => writeStorage(HIDE_AUTO_HINT_KEY, "1") },
      ],
    });
    return true;
  }

  function applyConversionResult(item, data, engineToUse) {
    item.engineRequested  = data.engine_requested || engineToUse;
    item.engineUsed       = data.engine_used || data.engine || engineToUse;
    item.fallbackOccurred = Boolean(data.fallback);
    item.fallbackReason   = data.fallback_reason || "";
    item.auto             = data.auto || null;
    item.quality          = data.quality || null;
    item.warnings         = Array.isArray(data.warnings) ? data.warnings : [];
    item.engine           = item.engineUsed;
    setItemMarkdown(item, data.markdown);
    item.savedTo          = data.saved_to_downloads || "";

    const warning = formatQualityWarning(item.quality);
    const via = item.engineRequested === "auto"
      ? `Auto → ${getEngineDisplayName(item.engineUsed)}`
      : getEngineDisplayName(item.engineUsed);
    const label = item.sourceUrl ? "URL" : `"${item.name}"`;

    if (!data.markdown || data.markdown.trim() === "") {
      item.status    = "empty";
      item.errorType = "empty";
      selectItem(item.id);
      showToast(
        warning ? `No text extracted from ${label}: ${warning.long}` : `No text extracted from ${label} — try a different engine`,
        "info"
      );
    } else {
      item.status = "saved";
      selectItem(item.id);
      if (item.fallbackOccurred) {
        showToast(`Converted ${label} via MarkItDown (Docling fallback)`, "success");
      } else if (warning) {
        showToast(`Converted ${label}, ${warning.toast}`, "info");
      } else if (item.warnings.length) {
        showToast(`Converted ${label} via ${via} (${item.warnings.length} note${item.warnings.length === 1 ? "" : "s"})`, "info");
      } else if (!(item.auto && maybeShowHintToast(item.auto.hint))) {
        showToast(`Converted ${label} via ${via}`, "success");
      }
    }
    noteSaveFallback(data.save_fallback);
  }

  function applyNetworkError(item, err, engineToUse, isUrl) {
    const errType = classifyError(err.message);
    item.status    = errType;
    item.error     = err.message;
    item.errorType = errType;
    selectItem(item.id);
    showToast(
      errType === "unsupported"
        ? `${getEngineDisplayName(engineToUse)} does not support this ${isUrl ? "URL type" : "format"}`
        : `Failed: ${isUrl ? err.message : item.name}`,
      "error"
    );
  }

  async function convertFileItem(item, file, engine = item.engine || selectedEngine) {
    const jobId = beginConversion(item, engine);

    const formData = new FormData();
    formData.append("file", file);
    const params = new URLSearchParams({
      save_to_downloads: autoSaveToDownloads.toString(),
      response_format:   "json",
      engine:            engine,
      job_id:            jobId,
    });

    try {
      const resp = await fetch(`${apiBase}/convert/file?${params.toString()}`, {
        method: "POST",
        body:   formData,
      });
      if (item.jobId !== jobId) return; // superseded by a newer run
      if (!resp.ok) {
        applyConversionError(item, resp.status, await readErrorDetail(resp, "Conversion error"), engine, false);
        return;
      }
      applyConversionResult(item, await resp.json(), engine);
    } catch (err) {
      if (item.jobId === jobId) applyNetworkError(item, err, engine, false);
    } finally {
      if (item.jobId === jobId) {
        stopProgressPoll(item);
        renderQueue();
        handleBatchItemCompleted(item);
      }
    }
  }

  function convertUrlItem(url, forcedEngine = null) {
    showWorkbench();
    const item = {
      id:        "item-" + Math.random().toString(36).substr(2, 9),
      name:      url,
      engine:    forcedEngine || selectedEngine,
      sourceUrl: url,
      status:    "pending",
      markdown:  "",
      error:     "",
      savedTo:   "",
    };
    queue.unshift(item);
    return runUrlConversion(item, item.engine);
  }

  async function runUrlConversion(item, engine) {
    const jobId = beginConversion(item, engine);
    const payload = {
      url:               item.sourceUrl,
      save_to_downloads: autoSaveToDownloads,
      engine:            engine,
      job_id:            jobId,
    };

    try {
      const resp = await fetch(`${apiBase}/convert/url?response_format=json`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(payload),
      });
      if (item.jobId !== jobId) return;
      if (!resp.ok) {
        applyConversionError(item, resp.status, await readErrorDetail(resp, "URL conversion error"), engine, true);
        return;
      }
      applyConversionResult(item, await resp.json(), engine);
    } catch (err) {
      if (item.jobId === jobId) applyNetworkError(item, err, engine, true);
    } finally {
      if (item.jobId === jobId) {
        stopProgressPoll(item);
        renderQueue();
        handleBatchItemCompleted(item);
      }
    }
  }

  // A user-initiated re-run with a specific engine. It never changes the
  // engine pill: re-converting one file with Docling must not quietly move
  // someone off Auto for everything they drop next.
  async function reconvertItem(item, engine) {
    if (!item || item.status === "converting") return;
    if (engine === "glm_ocr" && item.fileRef && !(await confirmGlmJob([item.fileRef]))) return;
    if (item.fileRef) {
      convertFileItem(item, item.fileRef, engine);
    } else if (item.sourceUrl) {
      runUrlConversion(item, engine);
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
          badgeText  = item.progressMessage || "Converting…";
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
        } else if (item.status === "cancelled") {
          badgeClass = "status-cancelled";
          badgeText  = "Cancelled";
        }

        const tag = describeQueueEngine(item, selectedEngine);
        const engineTag = tag
          ? `<span class="badge-queue-engine ${escapeHtml(tag.cls)}"${tag.title ? ` title="${escapeHtml(tag.title)}"` : ""}>${escapeHtml(tag.text)}</span>`
          : "";

        // Saved stays Saved: a warning never undoes the save. The chip says why
        // in words, not just with an icon or a colour.
        const warning = (item.status === "saved" || item.status === "empty") ? formatQualityWarning(item.quality) : null;
        const warnChip = warning
          ? `<span class="status-warn" title="${escapeHtml(warning.long)}" aria-label="Warning: ${escapeHtml(warning.long)}"><span aria-hidden="true">⚠</span> ${escapeHtml(warning.short)}</span>`
          : "";
        const subLine = engineTag || warnChip ? `<div class="item-sub">${engineTag}${warnChip}</div>` : "";

        const cancelBtn = item.status === "converting" && item.jobId && !item.cancelRequested
          ? `<button type="button" class="btn btn-icon queue-cancel" data-cancel="${item.id}" title="Cancel conversion" aria-label="Cancel converting ${escapeHtml(item.name)}">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
            </button>`
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
              ${subLine}
            </div>
          </div>
          <div class="item-status">
            <span class="status-pill ${badgeClass}">${escapeHtml(badgeText)}</span>
            ${cancelBtn}
          </div>
        </div>`;
      })
      .join("");

    // Attach click handlers
    document.querySelectorAll(".queue-item").forEach((el) => {
      el.addEventListener("click", () => selectItem(el.getAttribute("data-id")));
    });
    document.querySelectorAll(".queue-cancel").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const item = queue.find((i) => i.id === btn.getAttribute("data-cancel"));
        if (item) cancelItem(item);
      });
    });
  }

  // ─── Syntax Highlighting ─────────────────────────────────────────────────
  // highlight.js is fetched on first use, not up front: a conversion with no
  // fenced code never pays for it, and the Raw tab is a separate container so
  // it is never touched. Highlighting runs *after* DOMPurify has written into
  // the DOM, so what it sees is already sanitised and all it does is wrap
  // existing text nodes in spans. The code itself is never rewritten, and Copy
  // and Download read item.markdown rather than this DOM, so what the user
  // takes away is always exactly what the converter produced.
  const HLJS_SRC = "/static/vendor/highlight.min.js";
  // Past this size the pause costs more than the colour is worth.
  const HLJS_MAX_CHARS = 100000;
  // Docling names the language accurately, which is what belongs in the saved
  // .md, but a few of those names have no grammar of their own while a close
  // relative reads almost identically. Highlighting those with the relative
  // beats leaving them grey. Languages with no near neighbour -- bc, ceylon,
  // cobol, dc, forth -- are deliberately absent and stay plain.
  const HLJS_ALIASES = {
    cuda: "cpp",
    cython: "python",
    octave: "matlab",
    racket: "scheme",
    tikz: "latex",
  };
  let hljsLoader = null;

  function loadHighlighter() {
    if (window.hljs) return Promise.resolve(window.hljs);
    if (hljsLoader) return hljsLoader;
    hljsLoader = new Promise((resolve) => {
      const el = document.createElement("script");
      el.src = HLJS_SRC;
      el.async = true;
      el.onload = () => resolve(window.hljs || null);
      el.onerror = () => {
        // Leave the blocks plain and allow a later render to retry.
        hljsLoader = null;
        resolve(null);
      };
      document.head.appendChild(el);
    });
    return hljsLoader;
  }

  function highlightCodeBlocks(root) {
    if (!root) return;
    let blocks;
    try {
      blocks = Array.from(root.querySelectorAll("pre code[class*='language-']"));
    } catch (_) {
      return;
    }
    if (blocks.length === 0) return;

    loadHighlighter().then((hljs) => {
      if (!hljs) return;

      const run = (block) => {
        try {
          if (block.dataset.hljsDone === "1") return;
          const cls = Array.from(block.classList).find((c) => c.startsWith("language-"));
          const named = cls ? cls.slice("language-".length).toLowerCase() : "";
          const lang = HLJS_ALIASES[named] || named;
          // Only languages this build actually ships a grammar for. Without
          // the check highlight.js warns and falls back to plaintext for every
          // unlabelled or unsupported fence, which is noise, not information.
          if (!lang || !hljs.getLanguage(lang)) return;
          const source = block.textContent;
          if (source.length > HLJS_MAX_CHARS) return;
          block.dataset.hljsDone = "1";
          // highlight() rather than highlightElement() so the grammar is chosen
          // here: an aliased block keeps its accurate language- class, which
          // highlightElement would not know how to resolve. The input is the
          // element's own text, already sanitised, and highlight.js escapes
          // what it returns.
          block.innerHTML = hljs.highlight(source, {
            language: lang,
            ignoreIllegals: true,
          }).value;
          block.classList.add("hljs");
        } catch (_) {
          // One bad block must never cost the rest of the preview.
        }
      };

      if (blocks.length <= 8 || typeof window.requestIdleCallback !== "function") {
        blocks.forEach(run);
        return;
      }
      // Long documents are spread over idle slices so the preview paints first.
      let i = 0;
      const pump = (deadline) => {
        while (i < blocks.length && (deadline.didTimeout || deadline.timeRemaining() > 4)) {
          run(blocks[i++]);
        }
        if (i < blocks.length) window.requestIdleCallback(pump, { timeout: 250 });
      };
      window.requestIdleCallback(pump, { timeout: 250 });
    });
  }

  // ─── Table scroll boxes ──────────────────────────────────────────────────
  // Each rendered table goes in its own box that scrolls sideways, so a wide
  // table no longer drags the whole document with it. Nothing is measured:
  // one wrapper per table, no layout read, so it stays cheap on long documents.
  function wrapTables(root) {
    root.querySelectorAll("table").forEach((table) => {
      if (table.parentElement && table.parentElement.classList.contains("table-scroll")) return;
      const box = document.createElement("div");
      box.className = "table-scroll";
      table.replaceWith(box);
      box.appendChild(table);
    });
  }
  // ─── End table scroll boxes ──────────────────────────────────────────────

  // ─── Math formulas (built-in add-on: KaTeX) ──────────────────────────────
  // LaTeX from Docling's formula recognition and GLM-OCR ($…$, $$…$$, \(…\),
  // \[…\]) is drawn with KaTeX in the Rendered tab. Switched off in Settings →
  // Add-ons, or for a document with no delimiter at all, the Markdown goes
  // through the global parser exactly as before. KaTeX is bundled and loaded
  // only the first time a document has math; formulas on screen are drawn
  // first and the rest in short slices, so the window never blocks. Saved
  // files, Copy, Download and the Raw tab always use the Markdown itself.
  const KATEX_JS  = "/static/vendor/katex/katex.min.js";
  const KATEX_CSS = "/static/vendor/katex/katex.min.css";
  const MATH_HINT = /\$|\\\(|\\\[/;
  const MATH_INLINE_MAX = 1000;   // characters in one inline formula
  const MATH_MAX = 5000;          // a display formula longer than this stays text
  const MATH_LOOKAHEAD = 512;     // one search for an inline formula reads no further
  const MATH_DOC_MAX = 2000000;   // larger documents are parsed exactly as before
  const MATH_SLICE_MS = 6;        // KaTeX work per slice…
  const MATH_SLICE_MAX = 8;       // …and formulas per slice: the browser lays
                                  // out what was inserted after each slice
  const MATH_CACHE_MAX = 500;
  const KATEX_OPTIONS = { throwOnError: true, trust: false, strict: "ignore", maxSize: 20, maxExpand: 500, output: "htmlAndMathml" };
  // KaTeX's <annotation> holds the LaTeX again; DOMPurify drops the element but
  // would keep its text inside <math>, so drop the text too.
  const MATH_SANITIZE = { ADD_FORBID_CONTENTS: ["annotation"] };
  // Placeholders carry this per-session value and only they are drawn, so
  // look-alike HTML written into a document (class="math-tex") never is.
  const MATH_MARK = mathMark();
  const mathToggle = document.getElementById("mathToggle");
  let renderMath = document.documentElement.dataset.renderMath !== "off";
  let mathParser = null;
  let katexLoader = null;
  let mathRun = 0;                // every render bumps it; older work stops
  let mathObserver = null;
  let mathSaves = 0;
  const mathCache = new Map();    // "D"/"I" + LaTeX → sanitised KaTeX HTML, or null if it failed

  function mathMark() {
    const bytes = new Uint8Array(8);
    if (window.crypto && typeof window.crypto.getRandomValues === "function") {
      window.crypto.getRandomValues(bytes);
    } else {
      for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    }
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  function mathPlaceholder(tex, display) {
    const tag = display ? "div" : "span";
    const cls = display ? "math-tex math-block" : "math-tex";
    return `<${tag} class="${cls}" data-math="${MATH_MARK}" data-display="${display ? 1 : 0}">${escapeHtml(tex)}</${tag}>`;
  }

  // $$…$$ or \[…\] starting a line, on one line (Docling) or across lines
  // (GLM-OCR). A blank line inside means it isn't a formula, so a stray $$
  // can never swallow the rest of the document. Every search here is
  // bounded (the paragraph, or MATH_MAX), so parsing stays linear: marked
  // asks again before every block, and unbounded searches made a large
  // document with one $ take tens of seconds (security review, 1.2.0).
  const MATH_BLOCK = {
    name: "mathBlock",
    level: "block",
    // marked passes the rest of the current block from its second character
    // on, so only the following lines can start a formula, and a blank line
    // ends the paragraph it would cut, so nothing past it matters here.
    start(src) {
      let i = src.indexOf("\n");
      while (i !== -1) {
        i++;
        let j = i;
        while (j < src.length && (src[j] === " " || src[j] === "\t")) j++;
        if (j >= src.length || src[j] === "\n") return undefined;
        if (j - i <= 3 && (src.startsWith("$$", j) || src.startsWith("\\[", j))) return i;
        i = src.indexOf("\n", j);
      }
      return undefined;
    },
    tokenizer(src) {
      const m = /^[ \t]{0,3}(\$\$|\\\[)/.exec(src);
      if (!m) return undefined;
      const close = m[1] === "$$" ? "$$" : "\\]";
      const from = m[0].length;
      const end = src.slice(from, from + MATH_MAX + close.length).indexOf(close);
      if (end < 0) return undefined;
      const body = src.slice(from, from + end);
      const tex = body.trim();
      if (!tex || /\n[ \t]*\n/.test(body)) return undefined;
      let after = from + end + close.length;
      while (src[after] === " " || src[after] === "\t") after++;
      if (after < src.length && src[after] !== "\n") return undefined;
      if (src[after] === "\n") after++;
      return { type: "mathBlock", raw: src.slice(0, after), text: tex };
    },
    renderer(token) { return mathPlaceholder(token.text, true) + "\n"; },
  };

  // \(…\), $…$ and $$…$$ inside a line. For $, Pandoc's rule, which keeps
  // prices out: the opening $ is not followed by a space, the closing $ is
  // not preceded by a space nor followed by a digit; here the formula also
  // stays on one line and never runs into a backtick, so a price can't pair
  // with a $ inside inline code. \$ is always a literal dollar. Code spans
  // and code blocks are read by marked before this rule can see them.
  const MATH_INLINE = {
    name: "mathInline",
    level: "inline",
    // Looks at most MATH_LOOKAHEAD characters ahead. When no formula can start
    // there, it hands marked a point to cut the text at (a space if there is
    // one, so links aren't split) and is asked again from there: each call
    // costs the same, however long the paragraph.
    start(src) {
      const limit = Math.min(src.length, MATH_LOOKAHEAD);
      let cut = -1;
      for (let i = 0; i < limit; i++) {
        const c = src[i];
        if (c === "$") return i;
        if (c === "\\") {
          if (src[i + 1] === "(") return i;
          i++; // skip whatever is escaped, \$ included
          continue;
        }
        if (c === " " || c === "\n" || c === "\t") cut = i;
      }
      if (limit === src.length) return undefined;
      return cut > 0 ? cut : limit - 1;
    },
    tokenizer(src) {
      if (src.startsWith("\\(")) {
        const end = src.slice(2, 2 + MATH_INLINE_MAX + 2).indexOf("\\)");
        const tex = end > 0 ? src.slice(2, 2 + end) : "";
        if (!tex.trim() || tex.includes("\n\n")) return undefined;
        return { type: "mathInline", raw: src.slice(0, end + 4), text: tex.trim(), display: false };
      }
      if (src[0] !== "$") return undefined;
      const display = src[1] === "$";
      const open = display ? 2 : 1;
      const first = src[open];
      if (!first || first === "$" || /\s/.test(first)) return undefined;
      let i = open;
      while (i < src.length && i - open <= MATH_INLINE_MAX) {
        const c = src[i];
        if (c === "\\") { i += 2; continue; }
        if (c === "\n" || c === "`") return undefined;
        if (c === "$") break;
        i++;
      }
      if (src[i] !== "$" || i - open > MATH_INLINE_MAX) return undefined;
      if (display && src[i + 1] !== "$") return undefined;
      const tex = src.slice(open, i);
      if (/\s$/.test(tex)) return undefined;
      const close = i + (display ? 2 : 1);
      if (/\d/.test(src[close] || "")) return undefined;
      return { type: "mathInline", raw: src.slice(0, close), text: tex, display };
    },
    renderer(token) { return mathPlaceholder(token.text, token.display); },
  };

  // The one place the preview's Markdown becomes HTML.
  function markdownToHtml(md) {
    if (!renderMath || md.length > MATH_DOC_MAX || !MATH_HINT.test(md) || typeof window.marked.Marked !== "function") {
      return window.marked.parse(md);
    }
    if (!mathParser) {
      mathParser = new window.marked.Marked({ gfm: true, breaks: true });
      mathParser.use({ extensions: [MATH_BLOCK, MATH_INLINE] });
    }
    return mathParser.parse(md);
  }

  // Script and stylesheet together: KaTeX's HTML is unreadable without its
  // CSS, so if either fails the formulas simply stay as LaTeX text. If the
  // script ran but didn't register window.katex (a document can shadow the
  // globals its loader checks, e.g. <a id="exports">), the next document may
  // try again; a missing file is not retried.
  function loadKatex() {
    if (katexLoader) return katexLoader;
    katexLoader = new Promise((resolve) => {
      const done = (ok) => {
        const k = window.katex;
        const usable = ok && k && typeof k.renderToString === "function";
        if (ok && !usable) katexLoader = null;
        resolve(usable ? k : null);
      };
      const css = document.createElement("link");
      css.rel = "stylesheet";
      css.href = KATEX_CSS;
      const cssLoaded = new Promise((r) => { css.onload = () => r(true); css.onerror = () => r(false); });
      const js = document.createElement("script");
      js.src = KATEX_JS;
      js.async = true;
      const jsLoaded = new Promise((r) => { js.onload = () => r(true); js.onerror = () => r(false); });
      document.head.appendChild(css);
      document.head.appendChild(js);
      Promise.all([cssLoaded, jsLoaded]).then(([a, b]) => {
        if (!a || !b) console.warn("Math formulas: KaTeX could not be loaded; showing LaTeX as text.");
        done(a && b);
      });
    });
    return katexLoader;
  }

  function typesetOne(node, katex) {
    const tex = node.textContent;
    const display = node.dataset.display === "1";
    // The parser already keeps to these limits; checked again here because
    // KaTeX's output grows with its input and one formula is drawn at once.
    if (tex.length > (display ? MATH_MAX : MATH_INLINE_MAX)) {
      node.classList.add("math-error");
      node.title = "Too long to render as a formula";
      return;
    }
    const key = (display ? "D" : "I") + tex;
    let html = mathCache.get(key);
    if (html === undefined) {
      try {
        html = window.DOMPurify.sanitize(katex.renderToString(tex, { ...KATEX_OPTIONS, displayMode: display }), MATH_SANITIZE);
      } catch (_) {
        html = null;
      }
      if (mathCache.size >= MATH_CACHE_MAX) mathCache.delete(mathCache.keys().next().value);
      mathCache.set(key, html);
    }
    if (html === null) {
      node.classList.add("math-error");
      node.title = "Couldn't render this formula";
      return;
    }
    node.innerHTML = html;
    node.classList.add("math-done");
  }

  function cancelMath() {
    mathRun++;
    if (mathObserver) {
      mathObserver.disconnect();
      mathObserver = null;
    }
  }

  // Formulas on screen (or within 600px of it) first, then the rest in
  // document order, in slices of MATH_SLICE_MS. setTimeout rather than
  // requestIdleCallback, which WebKit (macOS, Linux) doesn't have.
  function typesetMath(root) {
    cancelMath();
    const run = mathRun;
    if (!renderMath || !window.DOMPurify) return;
    const nodes = Array.from(root.querySelectorAll(`.math-tex[data-math="${MATH_MARK}"]`));
    if (nodes.length === 0) return;
    loadKatex().then((katex) => {
      if (!katex || run !== mathRun) return;
      const left = new Set(nodes);
      const soon = [];
      let next = 0;
      let queued = false;
      // Only the first report matters: it says which formulas are on screen
      // now. After that the rest go in document order anyway, and keeping
      // hundreds of targets observed would cost a check on every frame.
      if (typeof window.IntersectionObserver === "function") {
        mathObserver = new window.IntersectionObserver((entries) => {
          entries.forEach((e) => { if (e.isIntersecting && left.has(e.target)) soon.push(e.target); });
          if (mathObserver) mathObserver.disconnect();
          mathObserver = null;
          kick();
        }, { root: root.closest(".tab-content"), rootMargin: "600px 0px" });
        nodes.forEach((n) => mathObserver.observe(n));
      }
      function take() {
        while (soon.length) {
          const n = soon.shift();
          if (left.has(n)) return n;
        }
        while (next < nodes.length) {
          const n = nodes[next++];
          if (left.has(n)) return n;
        }
        return null;
      }
      function kick() {
        if (queued) return;
        queued = true;
        setTimeout(step, 0);
      }
      function step() {
        queued = false;
        if (run !== mathRun) return;
        const until = performance.now() + MATH_SLICE_MS;
        let count = 0;
        do {
          const n = take();
          if (!n) {
            if (mathObserver) mathObserver.disconnect();
            mathObserver = null;
            return;
          }
          left.delete(n);
          typesetOne(n, katex);
        } while (++count < MATH_SLICE_MAX && performance.now() < until);
        kick();
      }
      kick();
    });
  }

  // The switch: off means the preview is parsed exactly as before and KaTeX
  // is never loaded. Applies at once (the open document is drawn again) and
  // is kept in settings.json; the server puts it on <html> before first paint.
  function applyRenderMath(on) {
    renderMath = on;
    document.documentElement.dataset.renderMath = on ? "on" : "off";
    if (mathToggle) mathToggle.checked = on;
    if (activeItemId) selectItem(activeItemId);
  }

  // From GET /settings, each time Settings opens; never over a pending save.
  function syncRenderMath(value) {
    const on = value !== false;
    if (mathSaves === 0 && on !== renderMath) applyRenderMath(on);
  }

  if (mathToggle) {
    mathToggle.checked = renderMath;
    mathToggle.addEventListener("change", async () => {
      const on = mathToggle.checked;
      const before = renderMath;
      applyRenderMath(on);
      const save = ++mathSaves;
      let saved = false;
      try {
        const resp = await fetchWithToken(`${apiBase}/settings`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ render_math: on }),
        });
        saved = resp.ok;
      } catch (_) {}
      if (save !== mathSaves) return; // a newer change decides
      mathSaves = 0;
      if (!saved) {
        applyRenderMath(before);
        showToast("Couldn't save the math setting.", "error");
      }
    });
  }
  // ─── End math formulas ───────────────────────────────────────────────────

  // ─── Select & Display Item ───────────────────────────────────────────────
  const EMPTY_HINT_SVG = `<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line></svg>`;

  // Lines for the notice's Details disclosure: what was compared and why the
  // engine was chosen, so a warning can be judged rather than taken on trust.
  function conversionDetailLines(item) {
    const lines = [];
    const q = item.quality;
    if (item.auto) {
      if (item.auto.reason) lines.push(`Auto: ${item.auto.reason}`);
      const esc = item.auto.escalation;
      if (esc && esc.error) {
        lines.push(`Retry with ${getEngineDisplayName(esc.to)} failed: ${esc.error}`);
      } else if (esc && esc.kept !== esc.to) {
        lines.push(`Retried with ${getEngineDisplayName(esc.to)}; kept ${getEngineDisplayName(esc.kept)}, which found more of the text.`);
      }
    }
    if (item.fallbackOccurred) {
      lines.push(`Docling failed${item.fallbackReason ? `: ${item.fallbackReason}` : ""}; converted with MarkItDown.`);
    }
    (item.warnings || []).forEach((line) => lines.push(line));
    if (q) {
      (q.details || []).forEach((line) => lines.push(line));
      const list = (pages) => pages.slice(0, 20).join(", ") + (pages.length > 20 ? ", …" : "");
      if (q.low_pages && q.low_pages.length) lines.push(`Pages with the most missing text: ${list(q.low_pages)}`);
      if (q.scan_pages && q.scan_pages.length) lines.push(`Scanned pages not read: ${list(q.scan_pages)}`);
      if (q.skipped_reason) lines.push(`Comparison skipped: ${q.skipped_reason.replace(/_/g, " ")}.`);
    }
    return lines;
  }

  function qualityNoticeActions(item) {
    const action = qualityAction(item.quality);
    if (!action) return [];
    if (action.install) return [{ label: action.label, onClick: (e) => openSettings(e, "engines") }];
    if (!(item.fileRef || item.sourceUrl)) return [];
    return [{ label: action.label, onClick: () => reconvertItem(item, action.engine) }];
  }

  function updatePreviewBadges(item) {
    if (previewEngineTag) {
      const tag = describePreviewEngine(item);
      previewEngineTag.textContent = tag.text;
      previewEngineTag.title = tag.title;
      previewEngineTag.style.display = "inline-flex";
    }
    if (previewFallbackBadge) {
      if (item.fallbackOccurred) {
        previewFallbackBadge.style.display = "inline-flex";
        previewFallbackBadge.textContent = "⚠️ Fallback: MarkItDown";
        previewFallbackBadge.title = item.fallbackReason
          ? `Docling failed: ${item.fallbackReason}`
          : "Docling failed to convert this document. Automatically fell back to MarkItDown.";
      } else {
        previewFallbackBadge.style.display = "none";
      }
    }
    if (previewQualityBadge) {
      const warning = formatQualityWarning(item.quality);
      if (warning) {
        previewQualityBadge.style.display = "inline-flex";
        previewQualityBadge.textContent = `⚠ ${warning.short}`;
        previewQualityBadge.title = warning.long;
        previewQualityBadge.setAttribute("aria-label", `Warning: ${warning.long}`);
      } else {
        previewQualityBadge.style.display = "none";
        previewQualityBadge.removeAttribute("aria-label");
      }
    }
  }

  function hidePreviewBadges() {
    if (previewEngineTag) previewEngineTag.style.display = "none";
    if (previewFallbackBadge) previewFallbackBadge.style.display = "none";
    if (previewQualityBadge) previewQualityBadge.style.display = "none";
  }

  function selectItem(id) {
    cancelMath(); // formulas still being drawn belong to the previous preview
    activeItemId = id;
    const item = queue.find((i) => i.id === id);
    if (!item) return;

    previewFilename.textContent = item.name;

    // Update re-convert button tooltip
    if (btnSwitchEngine) {
      if (item.fileRef || item.sourceUrl) {
        btnSwitchEngine.style.display = "inline-flex";
        const nextEng = getNextEngine(item.engine, glmIsUsable());
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
      hidePreviewBadges();
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

    if (item.status === "cancelled") {
      hidePreviewBadges();
      const again = (item.fileRef || item.sourceUrl)
        ? [{ label: `Convert again with ${engineLabel}`, onClick: () => reconvertItem(item, item.engine) }]
        : [];
      showConversionNotice("info", "Conversion cancelled", "Nothing was saved.", { actions: again });
      renderedOutput.innerHTML = `<div class="empty-preview-hint">${EMPTY_HINT_SVG}<p>This conversion was cancelled.</p></div>`;
      rawEditor.value = "";
      previewStats.textContent = "";
      renderQueue();
      return;
    }

    if (item.status === "failed" || item.status === "error") {
      hidePreviewBadges();
      const errMsg = item.error || "An unexpected error occurred during conversion.";
      showConversionNotice(
        "failed",
        `Conversion failed — ${engineLabel}`,
        errMsg
      );
      const altEng = item.engine === "markitdown" ? "docling" : "markitdown";
      const altLabel = getEngineDisplayName(altEng);
      const canRetry = Boolean(item.fileRef || item.sourceUrl);
      renderedOutput.innerHTML = `
        <div class="empty-preview-hint">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"></circle>
            <line x1="12" y1="8" x2="12" y2="12"></line>
            <line x1="12" y1="16" x2="12.01" y2="16"></line>
          </svg>
          <p>${escapeHtml(errMsg)}</p>
          ${canRetry ? `<button type="button" class="btn btn-sm btn-mark" id="btnNoticeRetryAlt" style="margin-top: 14px;">↺ Retry with ${altLabel} (Recommended)</button>` : ""}
        </div>`;
      const retryAltBtn = document.getElementById("btnNoticeRetryAlt");
      if (retryAltBtn && canRetry) {
        // Retries this item only; the engine pill stays where the user put it.
        retryAltBtn.addEventListener("click", () => reconvertItem(item, altEng));
      }
      rawEditor.value = `Error: ${errMsg}`;
      previewStats.textContent = "";
      renderQueue();
      return;
    }

    if (item.status === "empty") {
      hidePreviewBadges();
      const warning = formatQualityWarning(item.quality);
      if (warning) {
        // The check knows why nothing came out (a scanned PDF): say so, and
        // offer the engine that can read it, instead of "try a different engine".
        updatePreviewBadges(item);
        showConversionNotice("empty", warning.title, warning.long, {
          actions: qualityNoticeActions(item),
          details: conversionDetailLines(item),
        });
      } else {
        showConversionNotice(
          "empty",
          `No text extracted from this file by ${engineLabel}`,
          `${engineLabel} processed the file successfully, but found no text to extract. This can happen with image-only files or unsupported content types. Try switching engines with the ↺ button.`
        );
      }
      renderedOutput.innerHTML = `<div class="empty-preview-hint">${EMPTY_HINT_SVG}<p>The file was processed, but no text content was found.</p></div>`;
      rawEditor.value = "";
      previewStats.textContent = "";
      renderQueue();
      return;
    }

    const md = item.markdown || "";
    rawEditor.value = md;

    const words = item.wordCount || 0;
    previewStats.textContent = words > 0 ? `${words.toLocaleString()} words` : "";

    updatePreviewBadges(item);

    // One notice, most important first: a missing-text warning, then a
    // Docling fallback, then Auto's install tip, then plain information.
    const warning = formatQualityWarning(item.quality);
    const details = conversionDetailLines(item);
    if (warning) {
      showConversionNotice("warning", warning.title, warning.long, {
        actions: qualityNoticeActions(item),
        details,
      });
    } else if (item.fallbackOccurred) {
      showConversionNotice(
        "unsupported",
        "Docling Fallback: Converted with MarkItDown",
        `Docling was unable to parse this document${item.fallbackReason ? " (" + item.fallbackReason + ")" : ""}. Conversion automatically fell back to MarkItDown.`,
        { details }
      );
    } else if (item.auto && item.auto.hint) {
      showConversionNotice("info", "Tip", item.auto.hint, {
        actions: [{ label: item.auto.hint.includes("Docling") ? "Install Docling" : "Open Settings", onClick: (e) => openSettings(e, "engines") }],
        details,
      });
    } else if (item.warnings && item.warnings.length) {
      showConversionNotice("info", "Note", item.warnings[0], { details });
    } else if (item.quality && item.quality.image_pages && item.quality.image_pages.length) {
      const note = (item.quality.details || []).find((line) => line.includes("full-page image"));
      if (note) showConversionNotice("info", "Note", note, { details });
    }

    // Rendered HTML (Sanitized against DOM XSS)
    if (window.marked) {
      try {
        if (typeof window.marked.use === "function") {
          window.marked.use({ gfm: true, breaks: true });
        }
      } catch (_) {}
      const rawHtml = markdownToHtml(md);
      if (window.DOMPurify) {
        renderedOutput.innerHTML = window.DOMPurify.sanitize(rawHtml);
      } else {
        renderedOutput.innerHTML = renderBasicMarkdown(md);
      }
    } else {
      renderedOutput.innerHTML = renderBasicMarkdown(md);
    }
    wrapTables(renderedOutput);
    highlightCodeBlocks(renderedOutput);
    typesetMath(renderedOutput);

    renderQueue();
  }

  // ─── Switch Engine (Re-convert) ──────────────────────────────────────────
  // Files and URLs go through the same request and result handling, so a URL
  // re-convert reports its engine, fallback, Auto choice and warnings too.
  if (btnSwitchEngine) {
    btnSwitchEngine.addEventListener("click", () => {
      const item = queue.find((i) => i.id === activeItemId);
      if (!item) return;
      reconvertItem(item, getNextEngine(item.engine, glmIsUsable()));
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

  // ─── Desktop save (Download button) ──────────────────────────────────────
  // The desktop window does not perform <a download> (pywebview cancels it),
  // so the app saves through a native Save dialog instead. The toast reports
  // what actually happened; a cancelled dialog says nothing.
  function saveMarkdownViaDesktop(fileName, content) {
    withWindowApi((api) => {
      btnDownloadMarkdown.disabled = true;
      Promise.resolve()
        .then(() => api.save_markdown(fileName, content))
        .then((res) => {
          if (res && res.status === "saved") {
            showToast(`Saved to ${res.path}`, "success");
          } else if (!res || res.status !== "cancelled") {
            showToast(`Could not save the file: ${(res && res.message) || "unknown error"}`, "error");
          }
        })
        .catch((err) => {
          showToast(`Could not save the file: ${(err && err.message) || err}`, "error");
        })
        .finally(() => {
          btnDownloadMarkdown.disabled = false;
        });
    });
  }
  // ─── End desktop save ─────────────────────────────────────────────────────

  // ─── Save folder ─────────────────────────────────────────────────────────
  // Where auto-save writes. Shown everywhere, changed only in the desktop app:
  // the path comes from the system's folder dialog (pywebview.api), never from
  // this page, and no HTTP request can set it.
  const saveFolderPath      = document.getElementById("saveFolderPath");
  const saveFolderProblem   = document.getElementById("saveFolderProblem");
  const saveFolderHint      = document.getElementById("saveFolderHint");
  const btnChangeSaveFolder = document.getElementById("btnChangeSaveFolder");
  const btnResetSaveFolder  = document.getElementById("btnResetSaveFolder");
  let saveFolderState = null;
  // Each fallback sentence is shown once per session, so a batch of files into
  // a missing folder gives one notice rather than one per file.
  const shownSaveFallbacks = new Set();

  function renderSaveFolder(state) {
    if (!state || !saveFolderPath) return;
    saveFolderState = state;
    const shown = state.custom ? state.chosen : state.path;
    // The path box is right-to-left so a long path keeps its end visible; the
    // marks stop that from moving a leading "/" or drive letter to the other side.
    saveFolderPath.textContent = `\u200E${shown}\u200E`;
    saveFolderPath.title = shown;
    saveFolderProblem.textContent = state.problem || "";
    saveFolderProblem.style.display = state.problem ? "" : "none";
    const desktop = Boolean(window.pywebview);
    btnChangeSaveFolder.style.display = desktop ? "" : "none";
    btnResetSaveFolder.style.display = desktop && state.custom ? "" : "none";
    saveFolderHint.style.display = desktop ? "none" : "";
  }

  function changeSaveFolder(method) {
    withWindowApi((api) => {
      btnChangeSaveFolder.disabled = true;
      btnResetSaveFolder.disabled = true;
      Promise.resolve()
        .then(() => api[method]())
        .then((res) => {
          if (res && res.status === "saved") {
            renderSaveFolder(res.folder);
            shownSaveFallbacks.clear();
            if (res.warning) showToast(res.warning, "info");
            else showToast(`Auto-save now uses ${res.folder.path}`, "success");
          } else if (!res || res.status !== "cancelled") {
            showToast(`Could not change the save folder: ${(res && res.message) || "unknown error"}`, "error");
          }
        })
        .catch((err) => {
          showToast(`Could not change the save folder: ${(err && err.message) || err}`, "error");
        })
        .finally(() => {
          btnChangeSaveFolder.disabled = false;
          btnResetSaveFolder.disabled = false;
        });
    });
  }

  function noteSaveFallback(message) {
    if (!message || shownSaveFallbacks.has(message)) return;
    shownSaveFallbacks.add(message);
    showToast(message, "info");
  }

  if (btnChangeSaveFolder) btnChangeSaveFolder.addEventListener("click", () => changeSaveFolder("choose_save_folder"));
  if (btnResetSaveFolder) btnResetSaveFolder.addEventListener("click", () => changeSaveFolder("reset_save_folder"));
  // ─── End save folder ─────────────────────────────────────────────────────

  btnDownloadMarkdown.addEventListener("click", () => {
    const item = queue.find((i) => i.id === activeItemId);
    if (!item || !item.markdown) {
      showToast("No markdown content to download", "error");
      return;
    }

    const content  = rawEditor.value || item.markdown;
    const baseName = item.name.replace(/\.[^/.]+$/, "");
    const fileName = `${baseName || "document"}.md`;
    if (window.pywebview) {
      saveMarkdownViaDesktop(fileName, content);
      return;
    }

    const blob     = new Blob([content], { type: "text/markdown;charset=utf-8" });
    const url      = URL.createObjectURL(blob);
    const a        = document.createElement("a");
    a.href         = url;
    a.download     = fileName;
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
  // opts.actions: [{ label, onClick }] rendered as buttons; any of them also
  // dismisses the toast. One timer at a time, so an earlier toast's timer can
  // no longer hide a newer one early.
  let toastTimer = null;
  function showToast(message, type = "info", opts = {}) {
    toastNotification.replaceChildren();
    const text = document.createElement("span");
    text.className = "toast-message";
    text.textContent = message;
    toastNotification.appendChild(text);
    const actions = opts.actions || [];
    if (actions.length) {
      const row = document.createElement("span");
      row.className = "toast-actions";
      actions.forEach((action) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "toast-action";
        btn.textContent = action.label;
        btn.addEventListener("click", (e) => {
          toastNotification.classList.remove("show");
          action.onClick(e);
        });
        row.appendChild(btn);
      });
      toastNotification.appendChild(row);
    }
    toastNotification.className = `toast-notification toast-${type} show`;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toastNotification.classList.remove("show");
      toastTimer = null;
    }, opts.duration || (actions.length ? 8000 : 3000));
  }

  // ─── Custom Confirmation Dialog (replaces window.confirm) ─────────────────
  // Resolves true on confirm, false on cancel/backdrop/Escape. Only one dialog
  // is ever open at a time in this app, so listeners are attached and torn
  // down per call rather than kept registered permanently.
  // details: optional bullet lines under the message. focusCancel: start on
  // Cancel even for a non-destructive action (a large download, say), so a
  // stray Enter never starts it.
  const appDialogDetails = document.getElementById("appDialogDetails");
  function confirmDialog({ title, message, details = [], confirmText = "Confirm", cancelText = "Cancel", danger = true, focusCancel = danger } = {}) {
    return new Promise((resolve) => {
      appDialogTitle.textContent   = title || "Are you sure?";
      appDialogMessage.textContent = message || "";
      if (appDialogDetails) {
        appDialogDetails.replaceChildren();
        details.forEach((line) => {
          const li = document.createElement("li");
          li.textContent = line;
          appDialogDetails.appendChild(li);
        });
        appDialogDetails.hidden = details.length === 0;
      }
      appDialogConfirm.textContent = confirmText;
      appDialogCancel.textContent  = cancelText;
      appDialogConfirm.className   = `btn ${danger ? "btn-danger" : "btn-primary"}`;
      appDialogIcon.classList.toggle("is-neutral", !danger);

      const previouslyFocused = document.activeElement;
      const focusables = [appDialogCancel, appDialogConfirm];

      function cleanup(result) {
        appDialogOverlay.hidden = true;
        appDialogOverlay.removeEventListener("click", onOverlayClick);
        appDialogConfirm.removeEventListener("click", onConfirm);
        appDialogCancel.removeEventListener("click", onCancel);
        document.removeEventListener("keydown", onKeydown);
        if (previouslyFocused && typeof previouslyFocused.focus === "function") {
          previouslyFocused.focus();
        }
        resolve(result);
      }

      function onConfirm() { cleanup(true); }
      function onCancel()  { cleanup(false); }
      function onOverlayClick(e) {
        if (e.target === appDialogOverlay) cleanup(false);
      }
      function onKeydown(e) {
        if (e.key === "Escape") {
          e.preventDefault();
          cleanup(false);
        } else if (e.key === "Tab") {
          // Minimal focus trap: the dialog only ever has these two buttons.
          e.preventDefault();
          const idx = focusables.indexOf(document.activeElement);
          const nextIdx = e.shiftKey
            ? (idx <= 0 ? focusables.length - 1 : idx - 1)
            : (idx === focusables.length - 1 ? 0 : idx + 1);
          focusables[nextIdx].focus();
        }
      }

      appDialogConfirm.addEventListener("click", onConfirm);
      appDialogCancel.addEventListener("click", onCancel);
      appDialogOverlay.addEventListener("click", onOverlayClick);
      document.addEventListener("keydown", onKeydown);

      appDialogOverlay.hidden = false;
      // Default focus sits on the non-destructive action so a stray Enter
      // key press can't trigger something irreversible like Remove Pack.
      (focusCancel ? appDialogCancel : appDialogConfirm).focus();
    });
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
