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
  const res = await fetch("/api/wishlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error("HTTP " + res.status);
}

async function saveWishlistOnly() {
  const statusEl = $("wishlist-save-status");
  statusEl.textContent = "Speichere ...";
  try {
    await saveWishlist();
    statusEl.textContent = "Gespeichert.";
  } catch (e) {
    statusEl.textContent = "Fehler: " + e.message;
  }
  setTimeout(() => { statusEl.textContent = ""; }, 3000);
}

async function loadTarget() {
  try {
    const res = await fetch("/api/target");
    const data = await res.json();
    const select = $("target-select");
    select.innerHTML = "";
    for (const t of data.targets) {
      const opt = document.createElement("option");
      opt.value = t.key;
      const free = t.free_bytes != null
        ? ` — ${(t.free_bytes / 1024 / 1024 / 1024).toFixed(0)} GB frei`
        : "";
      opt.textContent = t.label + free;
      select.appendChild(opt);
    }
    select.value = data.current;
  } catch (e) { /* ignore */ }
}

async function saveTarget() {
  const statusEl = $("target-status");
  const key = $("target-select").value;
  try {
    const res = await fetch("/api/target", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: key }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "unbekannter Fehler");
    statusEl.textContent = "Gespeichert.";
    setTimeout(() => { statusEl.textContent = ""; }, 2000);
  } catch (e) {
    statusEl.textContent = "Fehler: " + e.message;
    await loadTarget();
  }
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

function updateDownloadHeaderStatus(running, progress) {
  const el = $("download-header-status");
  if (!running) {
    el.textContent = "";
    return;
  }
  if (progress && progress.total > 0) {
    const pct = Math.round((progress.received / progress.total) * 100);
    const mbReceived = (progress.received / 1024 / 1024).toFixed(1);
    const mbTotal = (progress.total / 1024 / 1024).toFixed(1);
    el.textContent = `${progress.title}: ${mbReceived} / ${mbTotal} MB (${pct}%)`;
  } else {
    el.textContent = "● läuft";
  }
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
      $("stop-btn").disabled = !data.running || data.stopping;
      $("run-status").textContent = data.stopping ? "Stoppe ..." : (data.running ? "Läuft ..." : "");
      updateDownloadHeaderStatus(data.running, data.progress);

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

async function runOnly() {
  $("log-box").innerHTML = "";
  $("summary-box").classList.add("hidden");
  logOffset = 0;
  const statusEl = $("run-status");

  statusEl.textContent = "Starte ...";
  try {
    const res = await fetch("/api/run", { method: "POST" });
    if (!res.ok && res.status !== 409) throw new Error("HTTP " + res.status);
  } catch (e) {
    statusEl.textContent = "Fehler: " + e.message;
    return;
  }
  pollStatus();
}

async function stopRun() {
  $("stop-btn").disabled = true;
  $("run-status").textContent = "Stoppe ...";
  try {
    const res = await fetch("/api/stop", { method: "POST" });
    if (!res.ok && res.status !== 409) throw new Error("HTTP " + res.status);
  } catch (e) {
    $("run-status").textContent = "Fehler: " + e.message;
  }
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

async function loadVpnConfig() {
  try {
    const res = await fetch("/api/vpn");
    const data = await res.json();
    const isCustom = data.provider === "custom";

    $("vpn-address").value = data.address || "";
    $("vpn-server-field").classList.toggle("hidden", isCustom);
    $("vpn-endpoint-field").classList.toggle("hidden", !isCustom);
    if (isCustom) {
      $("vpn-endpoint").value = data.endpoint || "";
    } else {
      $("vpn-server").value = data.server || "";
    }

    const modeText = isCustom
      ? `Ja (Custom-Config, Endpoint ${data.endpoint || "?"})`
      : (data.has_key ? "Ja (Mullvad)" : "Nein");
    $("vpn-key-text").textContent = modeText;
  } catch (e) { /* ignore */ }
}

async function uploadVpnConfig() {
  const statusEl = $("vpn-upload-status");
  const fileInput = $("vpn-conf-file");
  const file = fileInput.files[0];
  if (!file) {
    statusEl.textContent = "Bitte zuerst eine Datei auswählen.";
    return;
  }

  statusEl.textContent = "Lade hoch ...";
  const formData = new FormData();
  formData.append("file", file);
  try {
    const res = await fetch("/api/vpn/upload", { method: "POST", body: formData });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "unbekannter Fehler");
    statusEl.textContent = "Config übernommen.";
    fileInput.value = "";
    await loadVpnConfig();
    setTimeout(() => { statusEl.textContent = ""; }, 3000);
    await confirmAndRestartVpn();
    return;
  } catch (e) {
    statusEl.textContent = "Fehler: " + e.message;
  }
  setTimeout(() => { statusEl.textContent = ""; }, 6000);
}

async function saveVpnConfig() {
  const statusEl = $("vpn-save-status");
  const privateKey = $("vpn-key").value.trim();
  const address = $("vpn-address").value.trim();
  const server = $("vpn-server").value.trim();

  if (!privateKey || !address) {
    statusEl.textContent = "Private Key und Adresse sind erforderlich.";
    return;
  }

  statusEl.textContent = "Speichere ...";
  try {
    const res = await fetch("/api/vpn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ private_key: privateKey, address, server }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "unbekannter Fehler");
    statusEl.textContent = "Gespeichert.";
    $("vpn-key").value = "";
    await loadVpnConfig();
    setTimeout(() => { statusEl.textContent = ""; }, 3000);
    await confirmAndRestartVpn();
    return;
  } catch (e) {
    statusEl.textContent = "Fehler: " + e.message;
  }
  setTimeout(() => { statusEl.textContent = ""; }, 5000);
}

async function confirmAndRestartVpn() {
  const wantsRestart = confirm(
    "VPN-Konfiguration gespeichert. Container jetzt neu starten, um die Änderung zu aktivieren?"
  );
  if (!wantsRestart) return;
  await triggerVpnRestart();
}

async function triggerVpnRestart() {
  const statusEl = $("vpn-status-text");
  statusEl.textContent = "Neustart läuft ...";
  try {
    const res = await fetch("/api/vpn/restart", { method: "POST" });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "unbekannter Fehler");
  } catch (e) {
    statusEl.textContent = "Fehler beim Neustart: " + e.message;
    return;
  }
  pollVpnRestartLog();
}

async function pollVpnRestartLog(attempt = 0) {
  const statusEl = $("vpn-status-text");
  try {
    const res = await fetch("/api/vpn/restart-log");
    const data = await res.json();
    if (data.done) {
      if (data.log && data.log.includes("ROLLBACK")) {
        statusEl.textContent = "VPN-Verbindung fehlgeschlagen – automatisch zurückgeschaltet.";
      } else {
        statusEl.textContent = "Neustart abgeschlossen.";
      }
      await loadVpnConfig();
      pollVpnStatus();
      return;
    }
  } catch (e) { /* Seite evtl. kurz nicht erreichbar waehrend Neustart - weiter pollen */ }

  if (attempt > 40) {
    statusEl.textContent = "Neustart dauert ungewöhnlich lange - bitte Seite neu laden.";
    return;
  }
  setTimeout(() => pollVpnRestartLog(attempt + 1), 2000);
}

async function disableVpn() {
  const statusEl = $("vpn-disable-status");
  if (!confirm("VPN deaktivieren und Container neu starten (läuft dann ohne VPN)?")) return;
  statusEl.textContent = "Deaktiviere ...";
  try {
    const res = await fetch("/api/vpn/disable", { method: "POST" });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "unbekannter Fehler");
    statusEl.textContent = "";
    await triggerVpnRestart();
  } catch (e) {
    statusEl.textContent = "Fehler: " + e.message;
  }
}

async function pollVpnStatus() {
  try {
    const res = await fetch("/api/vpn/status");
    const data = await res.json();
    const textEl = $("vpn-status-text");
    const dotEl = $("vpn-status-dot");
    const ipRow = $("vpn-ip-row");
    const isActive = data.reachable && data.status === "running";

    const headerDotEl = $("vpn-header-dot");
    if (!data.reachable) {
      textEl.textContent = "nicht aktiv / nicht erreichbar";
      dotEl.className = "status-dot inactive";
      headerDotEl.className = "status-dot inactive";
      ipRow.classList.add("hidden");
    } else {
      textEl.textContent = data.status === "running" ? "aktiv" : (data.status || "unbekannt");
      dotEl.className = "status-dot " + (isActive ? "active" : "inactive");
      headerDotEl.className = "status-dot " + (isActive ? "active" : "inactive");
      if (data.public_ip) {
        $("vpn-ip-text").textContent = data.public_ip;
        ipRow.classList.remove("hidden");
      } else {
        ipRow.classList.add("hidden");
      }
    }
  } catch (e) { /* ignore */ }
  setTimeout(pollVpnStatus, 10000);
}

let searchResults = [];
let searchPolling = false;

function formatSize(bytes) {
  if (!bytes) return "?";
  const gb = bytes / 1024 / 1024 / 1024;
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / 1024 / 1024).toFixed(0)} MB`;
}

function setSearchRunning(running) {
  const btn = $("search-btn");
  btn.textContent = running ? "Abbrechen" : "Suchen";
  btn.classList.toggle("stop-btn", running);
  $("search-query").disabled = running;
  $("search-category").disabled = running;
}

function renderSearchResults() {
  const filter = $("search-filter").value.trim().toLowerCase();
  const body = $("search-results");
  body.innerHTML = "";
  let shown = 0;
  for (const r of searchResults) {
    if (filter && !r.fname.toLowerCase().includes(filter)) continue;
    shown++;
    const tr = document.createElement("tr");
    if (r.index === 0) tr.classList.add("best");

    const cells = [
      { text: r.fname, cls: "fname", sub: r.source },
      { text: formatSize(r.size), cls: "num" },
      { text: `${r.bot} #${r.pack}` },
      { text: `${r.network} ${r.channel}`, cls: "col-optional" },
      { text: r.gets, cls: "num col-optional" },
      { text: r.score, cls: "num col-optional" },
    ];
    for (const c of cells) {
      const td = document.createElement("td");
      if (c.cls) td.className = c.cls;
      td.textContent = c.text;
      if (c.sub) {
        const s = document.createElement("div");
        s.className = "source";
        s.textContent = c.sub;
        td.appendChild(s);
      }
      tr.appendChild(td);
    }

    const td = document.createElement("td");
    const btn = document.createElement("button");
    btn.className = "small-btn";
    btn.textContent = "Laden";
    btn.title = "Genau dieses Pack laden und einsortieren";
    btn.addEventListener("click", () => downloadSearchResult(r));
    td.appendChild(btn);
    tr.appendChild(td);

    body.appendChild(tr);
  }
  $("search-table").classList.toggle("hidden", searchResults.length === 0);
  return shown;
}

function applySearchData(data) {
  searchResults = data.results || [];
  const shown = renderSearchResults();
  const statusEl = $("search-status");
  if (data.running) {
    statusEl.textContent = `Suche „${data.query}“ läuft ...`;
  } else if (data.error) {
    statusEl.textContent = "Fehler: " + data.error;
  } else if (data.query) {
    statusEl.textContent = searchResults.length
      ? `${searchResults.length} Treffer für „${data.query}“` +
        (shown !== searchResults.length ? ` (${shown} angezeigt)` : "")
      : `Nichts gefunden für „${data.query}“`;
  }
}

async function pollSearch() {
  if (searchPolling) return;
  searchPolling = true;
  try {
    while (true) {
      const res = await fetch("/api/search");
      const data = await res.json();
      applySearchData(data);
      setSearchRunning(data.running);
      if (!data.running) break;
      await new Promise((r) => setTimeout(r, 1500));
    }
  } catch (e) {
    $("search-status").textContent = "Fehler: " + e.message;
  } finally {
    searchPolling = false;
  }
}

async function startSearch(e) {
  e.preventDefault();
  if ($("search-btn").textContent === "Abbrechen") {
    await fetch("/api/stop", { method: "POST" });
    return;
  }
  const query = $("search-query").value.trim();
  if (!query) return;
  const statusEl = $("search-status");
  statusEl.textContent = "Starte Suche ...";
  try {
    const res = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, category: $("search-category").value }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "unbekannter Fehler");
  } catch (e) {
    statusEl.textContent = "Fehler: " + e.message;
    return;
  }
  pollSearch();
}

async function downloadSearchResult(r) {
  if (!confirm(`${r.fname} (${formatSize(r.size)}) laden?`)) return;
  try {
    const res = await fetch("/api/search/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index: r.index }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "unbekannter Fehler");
  } catch (e) {
    alert("Download nicht gestartet: " + e.message);
    return;
  }
  $("log-box").innerHTML = "";
  $("summary-box").classList.add("hidden");
  logOffset = 0;
  pollStatus();
}

function setupCollapsibleCards() {
  const STORAGE_KEY = "xdcc-collapsed-cards";
  let collapsed = {};
  try {
    collapsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
  } catch (e) { collapsed = {}; }

  for (const card of document.querySelectorAll(".card[data-card-id]")) {
    const id = card.dataset.cardId;
    if (collapsed[id]) card.classList.add("collapsed");

    const header = card.querySelector(".card-header");
    header.addEventListener("click", () => {
      card.classList.toggle("collapsed");
      collapsed[id] = card.classList.contains("collapsed");
      localStorage.setItem(STORAGE_KEY, JSON.stringify(collapsed));
    });
  }
}

function setupVpnBadge() {
  $("vpn-badge").addEventListener("click", () => {
    $("vpn-popup").classList.remove("hidden");
  });
  $("vpn-popup").addEventListener("click", (e) => {
    if (e.target.id === "vpn-popup") $("vpn-popup").classList.add("hidden");
  });
}

function setupMenuDropdown() {
  const dropdown = $("menu-dropdown");

  $("menu-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    dropdown.classList.toggle("hidden");
  });

  document.addEventListener("click", (e) => {
    if (!dropdown.classList.contains("hidden") && !e.target.closest(".menu-wrap")) {
      dropdown.classList.add("hidden");
    }
  });

  for (const item of dropdown.querySelectorAll(".menu-item")) {
    item.addEventListener("click", () => {
      dropdown.classList.add("hidden");
      $(item.dataset.modal).classList.remove("hidden");
    });
  }

  for (const overlay of document.querySelectorAll(".menu-modal")) {
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) overlay.classList.add("hidden");
    });
  }
}

document.addEventListener("DOMContentLoaded", () => {
  setupCollapsibleCards();
  setupVpnBadge();
  setupMenuDropdown();
  loadWishlist();
  loadTarget();
  loadDownloaded();
  loadChatLog();
  loadRuns();
  pollStatus();
  pollSearch();
  loadVpnConfig();
  pollVpnStatus();

  $("search-form").addEventListener("submit", startSearch);
  $("search-filter").addEventListener("input", renderSearchResults);
  $("target-select").addEventListener("change", saveTarget);
  $("wishlist-save-btn").addEventListener("click", saveWishlistOnly);
  $("vpn-save-btn").addEventListener("click", saveVpnConfig);
  $("vpn-upload-btn").addEventListener("click", uploadVpnConfig);
  $("vpn-disable-btn").addEventListener("click", disableVpn);
  $("run-btn").addEventListener("click", runOnly);
  $("stop-btn").addEventListener("click", stopRun);
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
