import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const SETTING_ID = "CivitFetch.ApiKey";

/**
 * Module-level download state. It lives outside any DOM node so that switching away from
 * and back to the sidebar tab (which re-runs render() and rebuilds the panel) never loses
 * an in-progress download — the panel just re-attaches to this state. The SSE reader loop
 * writes here and calls notify(); whichever panel is currently mounted reflects it.
 */
const dl = {
  url: "", // last-typed link (persisted across re-mounts)
  folder: "", // last-selected folder (persisted across re-mounts)
  phase: "idle", // idle | resolving | downloading | paused | done | error
  filename: "",
  pct: null, // 0..100, or null when total size is unknown
  downloaded: 0,
  total: 0,
  message: "", // status line text
};

// The mounted panel registers an apply() here so the reader can push updates to it.
let applyToPanel = null;
function notify() {
  if (applyToPanel) applyToPanel();
}

function el(tag, props = {}, children = []) {
  const node = Object.assign(document.createElement(tag), props);
  for (const [k, v] of Object.entries(props.style || {})) node.style[k] = v;
  for (const c of children) node.append(c);
  return node;
}

function humanBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(n) / Math.log(1024));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

const active = () => dl.phase === "resolving" || dl.phase === "downloading";

/** Tell the server to pause or stop the running download. */
async function sendControl(action) {
  dl.message = action === "pause" ? "Pausing…" : "Stopping…";
  notify();
  try {
    await api.fetchApi("/civitfetch/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
  } catch (e) {
    dl.message = `Control failed: ${e}`;
    notify();
  }
}

/** Kick off (or resume) the download. Runs its reader loop detached from the DOM. */
async function startDownload() {
  if (active()) return; // one at a time
  const url = dl.url.trim();
  const folder = dl.folder;
  if (!url) {
    dl.phase = "error";
    dl.message = "Paste a link first.";
    return notify();
  }
  if (!folder) {
    dl.phase = "error";
    dl.message = "Pick a destination folder.";
    return notify();
  }

  const token = app.ui.settings.getSettingValue(SETTING_ID) || "";

  // Resuming a paused download: keep the current progress on screen. The server continues
  // from the existing .part file via an HTTP Range request.
  const resuming = dl.phase === "paused";
  dl.phase = "resolving";
  if (!resuming) {
    dl.filename = "";
    dl.pct = null;
    dl.downloaded = 0;
    dl.total = 0;
  }
  dl.message = resuming ? "Resuming…" : "Resolving link…";
  notify();

  let resp;
  try {
    resp = await api.fetchApi("/civitfetch/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, folder, token }),
    });
  } catch (e) {
    dl.phase = "error";
    dl.message = `Request failed: ${e}`;
    return notify();
  }

  if (!resp.ok || !resp.body) {
    let msg = `HTTP ${resp.status}`;
    try {
      msg = (await resp.json()).error || msg;
    } catch (_) {}
    dl.phase = "error";
    dl.message = msg;
    return notify();
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const handle = (evt) => {
    if (evt.event === "start") {
      dl.phase = "downloading";
      dl.filename = evt.filename;
      dl.total = evt.total || 0;
      dl.message = `Downloading ${evt.filename}…`;
    } else if (evt.event === "progress") {
      dl.phase = "downloading";
      dl.downloaded = evt.downloaded;
      dl.total = evt.total || dl.total;
      dl.pct = evt.pct;
      dl.message =
        evt.pct != null
          ? `${evt.pct}%  —  ${humanBytes(evt.downloaded)} / ${humanBytes(evt.total)}`
          : `Downloaded ${humanBytes(evt.downloaded)}…`;
    } else if (evt.event === "done") {
      dl.phase = "done";
      dl.pct = 100;
      dl.message = `✓ Saved ${evt.filename} to ${folder}`;
    } else if (evt.event === "paused") {
      dl.phase = "paused";
      dl.downloaded = evt.downloaded ?? dl.downloaded;
      dl.total = evt.total || dl.total;
      dl.message = `⏸ Paused — ${humanBytes(dl.downloaded)}${
        dl.total ? " / " + humanBytes(dl.total) : ""
      }. Click Resume to continue.`;
    } else if (evt.event === "stopped") {
      dl.phase = "idle";
      dl.pct = null;
      dl.downloaded = 0;
      dl.total = 0;
      dl.filename = "";
      dl.message = "⏹ Download stopped.";
    } else if (evt.event === "error") {
      dl.phase = "error";
      dl.message = `✗ ${evt.message}`;
    }
    notify();
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const raw = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 2);
        if (raw.startsWith("data:")) {
          try {
            handle(JSON.parse(raw.slice(5).trim()));
          } catch (_) {}
        }
      }
    }
    // Stream ended without an explicit terminal event — settle the phase.
    if (active()) {
      dl.phase = "done";
      dl.message = dl.filename ? `✓ Saved ${dl.filename} to ${folder}` : "Finished.";
      notify();
    }
  } catch (e) {
    dl.phase = "error";
    dl.message = `Stream error: ${e}`;
    notify();
  }
}

function buildPanel(root) {
  root.innerHTML = "";
  Object.assign(root.style, {
    padding: "12px",
    display: "flex",
    flexDirection: "column",
    gap: "10px",
    fontSize: "13px",
  });

  const heading = el("div", {
    textContent: "Download a model from CivitAI / Civitai Red",
    style: { fontWeight: "600", marginBottom: "2px" },
  });

  const urlInput = el("input", {
    type: "text",
    placeholder: "Paste a civitai.com or civitai.red link…",
    value: dl.url,
    style: {
      width: "100%",
      boxSizing: "border-box",
      padding: "6px 8px",
      borderRadius: "6px",
      border: "1px solid var(--border-color, #444)",
      background: "var(--comfy-input-bg, #222)",
      color: "var(--input-text, #ddd)",
    },
  });

  const folderRow = el("div", { style: { display: "flex", gap: "6px", alignItems: "center" } });
  const folderSelect = el("select", {
    style: {
      flex: "1",
      padding: "6px 8px",
      borderRadius: "6px",
      border: "1px solid var(--border-color, #444)",
      background: "var(--comfy-input-bg, #222)",
      color: "var(--input-text, #ddd)",
    },
  });
  const refreshBtn = el("button", {
    textContent: "⟳",
    title: "Refresh folder list",
    style: { padding: "6px 10px", borderRadius: "6px", cursor: "pointer" },
  });
  folderRow.append(folderSelect, refreshBtn);

  const downloadBtn = el("button", {
    textContent: "Download",
    style: {
      padding: "8px",
      borderRadius: "6px",
      cursor: "pointer",
      fontWeight: "600",
      background: "var(--p-button-primary-background, #3a6ea5)",
      color: "#fff",
      border: "none",
    },
  });

  const controlRow = el("div", {
    style: { display: "none", gap: "6px" },
  });
  const pauseBtn = el("button", {
    textContent: "Pause",
    style: { flex: "1", padding: "6px", borderRadius: "6px", cursor: "pointer" },
  });
  const stopBtn = el("button", {
    textContent: "Stop",
    style: {
      flex: "1",
      padding: "6px",
      borderRadius: "6px",
      cursor: "pointer",
      background: "var(--p-button-danger-background, #a33)",
      color: "#fff",
      border: "none",
    },
  });
  controlRow.append(pauseBtn, stopBtn);

  const fileLabel = el("div", {
    style: {
      fontWeight: "600",
      color: "var(--input-text, #ddd)",
      wordBreak: "break-all",
      display: "none",
    },
  });

  const progressWrap = el("div", {
    style: {
      height: "8px",
      borderRadius: "4px",
      background: "var(--comfy-input-bg, #222)",
      overflow: "hidden",
      display: "none",
    },
  });
  const progressBar = el("div", {
    style: { height: "100%", width: "0%", background: "#4a9", transition: "width .15s" },
  });
  progressWrap.append(progressBar);

  const status = el("div", {
    style: { minHeight: "18px", color: "var(--descrip-text, #aaa)", whiteSpace: "pre-wrap" },
  });

  root.append(
    heading,
    urlInput,
    folderRow,
    downloadBtn,
    controlRow,
    fileLabel,
    progressWrap,
    status
  );

  // Reflect the shared download state onto this freshly-built panel. Called on mount and
  // on every notify() from the reader loop.
  function apply() {
    const running = active();
    const paused = dl.phase === "paused";

    downloadBtn.disabled = running;
    refreshBtn.disabled = running || paused;
    downloadBtn.textContent =
      dl.phase === "resolving"
        ? "Resolving…"
        : dl.phase === "downloading"
        ? "Downloading…"
        : paused
        ? "Resume"
        : "Download";

    // Pause only applies while actively downloading; Stop applies to running or paused.
    controlRow.style.display = running || paused ? "flex" : "none";
    pauseBtn.style.display = dl.phase === "downloading" ? "block" : "none";

    if (dl.phase === "idle") {
      fileLabel.style.display = "none";
      progressWrap.style.display = "none";
    } else {
      fileLabel.textContent = dl.filename || "";
      fileLabel.style.display = dl.filename ? "block" : "none";
      progressWrap.style.display = "block";
      progressBar.style.width = dl.pct != null ? `${dl.pct}%` : running ? "100%" : "0%";
      progressBar.style.background =
        dl.phase === "error" ? "#c55" : paused ? "#e0a030" : "#4a9";
    }

    status.textContent = dl.message || "";
    status.style.color =
      dl.phase === "error" ? "#e77" : dl.phase === "done" ? "#6c6" : "var(--descrip-text, #aaa)";
  }

  async function loadFolders() {
    folderSelect.innerHTML = "";
    folderSelect.append(el("option", { textContent: "Loading…", value: "" }));
    try {
      const resp = await api.fetchApi("/civitfetch/folders");
      const data = await resp.json();
      folderSelect.innerHTML = "";
      const folders = data.folders || [];
      if (!folders.length) {
        folderSelect.append(el("option", { textContent: "(no folders found)", value: "" }));
        return;
      }
      for (const f of folders) folderSelect.append(el("option", { textContent: f, value: f }));
      // Restore prior selection if it still exists, else pick a sensible default.
      const preferred =
        (dl.folder && folders.includes(dl.folder) && dl.folder) ||
        folders.find((f) => f === "checkpoints") ||
        folders[0];
      folderSelect.value = preferred;
      dl.folder = preferred;
    } catch (e) {
      folderSelect.innerHTML = "";
      folderSelect.append(el("option", { textContent: "(error loading)", value: "" }));
    }
  }

  urlInput.addEventListener("input", () => (dl.url = urlInput.value));
  folderSelect.addEventListener("change", () => (dl.folder = folderSelect.value));
  urlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") startDownload();
  });
  refreshBtn.onclick = loadFolders;
  downloadBtn.onclick = startDownload; // also resumes when phase === "paused"
  pauseBtn.onclick = () => sendControl("pause");
  stopBtn.onclick = () => sendControl("stop");

  // This panel is now the live one; route reader updates here.
  applyToPanel = apply;
  apply(); // restore any in-progress / finished download immediately
  loadFolders();
}

app.registerExtension({
  name: "CivitFetch",
  settings: [
    {
      id: SETTING_ID,
      name: "CivitAI API Key",
      type: "text",
      defaultValue: "",
      tooltip:
        "Optional. Required for gated / login-only downloads. Get one at " +
        "civitai.com → Account Settings → API Keys.",
    },
  ],
  async setup() {
    if (!app.extensionManager?.registerSidebarTab) {
      console.warn("[CivitFetch] Sidebar tab API unavailable; update ComfyUI frontend.");
      return;
    }
    app.extensionManager.registerSidebarTab({
      id: "civitfetch",
      icon: "pi pi-download",
      title: "CivitFetch",
      tooltip: "Download models from CivitAI",
      type: "custom",
      render: (el) => buildPanel(el),
    });
  },
});
