const SECTIONS = ["serien", "film", "merkliste"];

let logOffset = 0;
let ircOffset = 0;
let chatLogSize = 0;
let polling = false;

function $(id) {
  return document.getElementById(id);
}

function textareaToList(id) {
  return $(id).value
    .split("\n")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

async function loadWishlist() {
  const res = await fetch("/api/wishlist");
  const data = await res.json();
  for (const key of SECTIONS) {
    $(key).value = (data[key] || []).join("\n");
  }
}

async function saveWishlist() {
  const payload = {};
  for (const key of SECTIONS) {
    payload[key] = textareaToList(key);
  }
  const statusEl = $("save-status");
  statusEl.textContent = "Speichere ...";
  try {
    const res = await fetch("/api/wishlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    statusEl.textContent = "Gespeichert.";
  } catch (e) {
    statusEl.textContent = "Fehler beim Speichern: " + e.message;
  }
  setTimeout(() => { statusEl.textContent = ""; }, 3000);
}

async function loadDownloaded() {
  const res = await fetch("/api/downloaded");
  const items = await res.json();
  renderDownloaded(items);
}

let downloadedItems = [];

function renderDownloaded(items) {
  downloadedItems = items;
  applyDownloadedFilter();
}

function applyDownloadedFilter() {
  const filter = $("downloaded-filter").value.trim().toLowerCase();
  const list = $("downloaded-list");
  list.innerHTML = "";
  for (const item of downloadedItems) {
    if (filter && !item.toLowerCase().includes(filter)) continue;
    const li = document.createElement("li");

    const span = document.createElement("span");
    span.textContent = item;
    li.appendChild(span);

    const btn = document.createElement("button");
    btn.textContent = "Entfernen";
    btn.className = "remove-btn";
    btn.title = "Aus 'Bereits geladen' entfernen (wird beim nächsten Lauf erneut gesucht)";
    btn.addEventListener("click", () => removeDownloaded(item));
    li.appendChild(btn);

    list.appendChild(li);
  }
}

async function removeDownloaded(title) {
  try {
    const res = await fetch("/api/downloaded", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "unbekannter Fehler");
    downloadedItems = downloadedItems.filter((i) => i !== title);
    applyDownloadedFilter();
  } catch (e) {
    alert("Fehler beim Entfernen: " + e.message);
  }
}

function classifyIrcLine(line) {
  const raw = line.replace(/^\[\d{2}:\d{2}:\d{2}\] /, "");
  if (raw.startsWith("===") || line.startsWith("===")) return "sep";
  if (raw.startsWith(">>")) return "sent";
  if (raw.startsWith("[SUCCESS]")) return "success";
  if (raw.startsWith("[ERROR]")) return "error";
  if (raw.startsWith("[WARNING]")) return "warning";
  return "recv";
}

function appendChatLines(lines, box) {
  box = box || $("irc-box");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  for (const line of lines) {
    if (!line) continue;
    const div = document.createElement("div");
    div.className = "irc-line " + classifyIrcLine(line);
    div.textContent = line;
    box.appendChild(div);
  }
  if (atBottom && lines.length > 0) {
    box.scrollTop = box.scrollHeight;
  }
}

async function loadChatLog() {
  try {
    const res = await fetch("/api/logs?tail=300");
    const data = await res.json();
    $("irc-box").innerHTML = "";
    appendChatLines(data.lines);
    chatLogSize = data.size;
  } catch (e) { /* ignore */ }
}

function appendLogLines(lines) {
  const box = $("log-box");
  for (const entry of lines) {
    const div = document.createElement("div");
    div.className = "log-line " + (entry.level || "info");
    div.textContent = `[${entry.title}] ${entry.msg}`;
    box.appendChild(div);
  }
  if (lines.length > 0) {
    box.scrollTop = box.scrollHeight;
  }
}

function updateProgress(progress) {
  const box = $("progress-box");
  if (!progress) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const pct = progress.total > 0 ? Math.round((progress.received / progress.total) * 100) : 0;
  const mbReceived = (progress.received / 1024 / 1024).toFixed(1);
  const mbTotal = (progress.total / 1024 / 1024).toFixed(1);
  box.querySelector(".progress-label").textContent =
    `${progress.title}: ${mbReceived} / ${mbTotal} MB (${pct}%)`;
  box.querySelector(".progress-bar-fill").style.width = pct + "%";
}

function showSummary(summary) {
  const box = $("summary-box");
  if (!summary) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  if (summary.error) {
    box.textContent = "Lauf abgebrochen: " + summary.error;
    return;
  }
  box.textContent =
    `Fertig: ${summary.ok} geladen, ${summary.failed} fehlgeschlagen, ` +
    `${summary.skipped} bereits vorhanden (${summary.total} offene Titel verarbeitet)`;
}

async function pollStatus() {
  if (polling) return;
  polling = true;
  try {
    while (true) {
      const [res, chatRes] = await Promise.all([
        fetch(`/api/status?since=${logOffset}`),
        fetch(`/api/logs?since_byte=${chatLogSize}`),
      ]);
      const data = await res.json();
      const chatData = await chatRes.json();
      appendLogLines(data.log);
      logOffset = data.next;
      appendChatLines(chatData.lines);
      chatLogSize = chatData.size;
      updateProgress(data.progress);

      $("run-btn").disabled = data.running;
      $("run-status").textContent = data.running ? "Läuft ..." : "";

      if (!data.running) {
        showSummary(data.summary);
        if (data.summary) {
          await loadDownloaded();
          await loadRuns();
        }
        break;
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
  } finally {
    polling = false;
  }
}

async function startRun() {
  $("log-box").innerHTML = "";
  $("summary-box").classList.add("hidden");
  logOffset = 0;
  try {
    const res = await fetch("/api/run", { method: "POST" });
    if (!res.ok && res.status !== 409) throw new Error("HTTP " + res.status);
  } catch (e) {
    $("run-status").textContent = "Fehler: " + e.message;
    return;
  }
  pollStatus();
}

function parseRunName(filename) {
  // "2026-06-16_17-41-22.txt" -> "2026-06-16 17:41:22"
  const base = filename.replace(".txt", "");
  const parts = base.split("_");
  if (parts.length !== 2) return base;
  const timeStr = parts[1].replace(/-/g, ":");
  return `${parts[0]}  ${timeStr}`;
}

async function loadRuns() {
  try {
    const res = await fetch("/api/runs");
    const runs = await res.json();
    const list = $("runs-list");
    if (!runs.length) {
      list.innerHTML = '<span class="muted">Keine Logs vorhanden</span>';
      return;
    }
    list.innerHTML = "";
    for (const r of runs) {
      const btn = document.createElement("button");
      btn.className = "run-entry";
      const kb = r.size > 0 ? ` (${Math.ceil(r.size / 1024)} KB)` : "";
      btn.textContent = parseRunName(r.name) + kb;
      btn.dataset.filename = r.name;
      btn.addEventListener("click", () => openRunLog(r.name));
      list.appendChild(btn);
    }
  } catch (e) { /* ignore */ }
}

async function openRunLog(filename) {
  // Highlight selected
  for (const btn of $("runs-list").querySelectorAll(".run-entry")) {
    btn.classList.toggle("active", btn.dataset.filename === filename);
  }
  try {
    const res = await fetch(`/api/runs/${encodeURIComponent(filename)}`);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    $("run-viewer-title").textContent = parseRunName(filename);
    const box = $("run-viewer-box");
    box.innerHTML = "";
    appendChatLines(data.lines, box);
    box.scrollTop = 0;
    $("run-viewer").classList.remove("hidden");
  } catch (e) {
    $("run-viewer-title").textContent = "Fehler beim Laden";
    $("run-viewer").classList.remove("hidden");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  loadWishlist();
  loadDownloaded();
  loadChatLog();
  loadRuns();
  pollStatus();

  $("save-btn").addEventListener("click", saveWishlist);
  $("run-btn").addEventListener("click", startRun);
  $("downloaded-filter").addEventListener("input", applyDownloadedFilter);
  $("log-clear-btn").addEventListener("click", () => { $("irc-box").innerHTML = ""; });
  $("log-bottom-btn").addEventListener("click", () => {
    const b = $("irc-box"); b.scrollTop = b.scrollHeight;
  });
  $("run-viewer-close").addEventListener("click", () => {
    $("run-viewer").classList.add("hidden");
    for (const btn of $("runs-list").querySelectorAll(".run-entry")) {
      btn.classList.remove("active");
    }
  });
});
