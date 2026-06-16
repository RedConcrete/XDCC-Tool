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

function appendChatLines(lines) {
  const box = $("irc-box");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  for (const line of lines) {
    if (!line) continue;
    const div = document.createElement("div");
    if (line.startsWith("===") || line.startsWith("===")) {
      div.className = "irc-line sep";
    } else if (line.startsWith(">>")) {
      div.className = "irc-line sent";
    } else {
      div.className = "irc-line recv";
    }
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

document.addEventListener("DOMContentLoaded", () => {
  loadWishlist();
  loadDownloaded();
  loadChatLog();
  pollStatus(); // falls bereits ein Lauf aktiv ist

  $("save-btn").addEventListener("click", saveWishlist);
  $("run-btn").addEventListener("click", startRun);
  $("downloaded-filter").addEventListener("input", applyDownloadedFilter);
  $("log-clear-btn").addEventListener("click", () => { $("irc-box").innerHTML = ""; });
  $("log-bottom-btn").addEventListener("click", () => {
    const b = $("irc-box"); b.scrollTop = b.scrollHeight;
  });
});
