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

let searchRunning = false;
let searchCategory = "film";

function setSearchRunning(running) {
  searchRunning = running;
  const btn = $("search-btn");
  btn.textContent = running ? "Abbrechen" : "Suchen";
  btn.classList.toggle("stop-btn", running);
  $("search-query").disabled = running;
  $("search-category").disabled = running;
}

function wishlistHas(section, title) {
  const t = title.toLowerCase();
  return textareaToList(section).some((s) => s.toLowerCase() === t);
}

async function addToWishlist(section, title, btn) {
  if (!wishlistHas(section, title)) {
    const ta = $(section);
    ta.value = ta.value.trim() ? `${ta.value.trimEnd()}\n${title}` : title;
  }
  btn.disabled = true;
  try {
    await saveWishlist();
    btn.textContent = "✓ " + btn.dataset.label;
  } catch (e) {
    btn.disabled = false;
    alert("Wunschliste nicht gespeichert: " + e.message);
  }
}

function makeListButton(section, label, r) {
  const btn = document.createElement("button");
  btn.className = "small-btn";
  btn.dataset.label = label;
  btn.title = `„${r.title}“ zur ${label} hinzufügen`;
  if (wishlistHas(section, r.title)) {
    btn.textContent = "✓ " + label;
    btn.disabled = true;
  } else {
    btn.textContent = "+ " + label;
    btn.addEventListener("click", () => addToWishlist(section, r.title, btn));
  }
  return btn;
}

function renderSearchSkeleton() {
  const body = $("search-results");
  body.innerHTML = "";
  for (let i = 0; i < 6; i++) {
    const tr = document.createElement("tr");
    tr.className = "skeleton-row";
    for (const cls of ["fname", "num size", "bot", "col-optional", "num col-optional", "num col-optional", "row-actions"]) {
      const td = document.createElement("td");
      if (cls) td.className = cls;
      const bar = document.createElement("span");
      bar.className = "skeleton";
      td.appendChild(bar);
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  $("search-table").classList.remove("hidden");
}

function addCell(tr, text, cls, label) {
  const td = document.createElement("td");
  if (cls) td.className = cls;
  if (label) td.dataset.label = label;
  td.textContent = text;
  tr.appendChild(td);
  return td;
}

function renderSearchResults() {
  if (searchRunning) {
    renderSearchSkeleton();
    return 0;
  }
  const filter = $("search-filter").value.trim().toLowerCase();
  const body = $("search-results");
  body.innerHTML = "";
  let shown = 0;
  for (const r of searchResults) {
    if (filter && !r.fname.toLowerCase().includes(filter)) continue;
    shown++;
    const tr = document.createElement("tr");
    if (r.index === 0) tr.classList.add("best");

    const nameTd = addCell(tr, r.fname, "fname");
    const sub = document.createElement("div");
    sub.className = "source";
    sub.textContent = r.index === 0 ? `${r.source} · Top-Treffer` : r.source;
    nameTd.appendChild(sub);
    addCell(tr, formatSize(r.size), "num size", "Größe");
    addCell(tr, `${r.bot} #${r.pack}`, "bot", "Bot");
    addCell(tr, `${r.network} ${r.channel}`, "col-optional", "Netzwerk");
    addCell(tr, r.gets, "num col-optional", "Gets");
    addCell(tr, r.score, "num col-optional", "Score");

    const td = addCell(tr, "", "row-actions");
    const dl = document.createElement("button");
    dl.className = "small-btn primary";
    dl.textContent = "Laden";
    dl.title = "Genau dieses Pack laden und einsortieren";
    dl.addEventListener("click", () => downloadSearchResult(r));
    td.appendChild(dl);
    td.appendChild(makeListButton(searchCategory, "Wunschliste", r));
    td.appendChild(makeListButton("merkliste", "Merkliste", r));

    body.appendChild(tr);
  }
  $("search-table").classList.toggle("hidden", searchResults.length === 0);
  return shown;
}

function applySearchData(data) {
  searchResults = data.results || [];
  searchCategory = data.category || "film";
  // Nach Neuladen der Seite Suchfeld + Kategorie der letzten Suche wiederherstellen
  if (data.query && !$("search-query").value) {
    $("search-query").value = data.query;
    $("search-category").value = searchCategory;
  }
  setSearchRunning(data.running);
  const shown = renderSearchResults();
  const statusEl = $("search-status");
  statusEl.classList.toggle("loading", !!data.running);
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
  setSearchRunning(true);
  renderSearchSkeleton();
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

async function loadScoreConfig() {
  try {
    const res = await fetch("/api/score");
    const data = await res.json();
    const box = $("score-fields");
    box.innerHTML = "";
    for (const f of data.fields) {
      const field = document.createElement("div");
      field.className = "field score-field";

      const label = document.createElement("label");
      label.htmlFor = "score-" + f.key;
      label.textContent = f.label;
      if (f.hint) {
        const hint = document.createElement("span");
        hint.className = "hint";
        hint.textContent = f.hint;
        label.appendChild(hint);
      }

      const input = document.createElement("input");
      input.type = "number";
      input.step = "10";
      input.id = "score-" + f.key;
      input.dataset.key = f.key;
      input.dataset.default = f.default;
      input.value = f.value;
      input.title = `Standard: ${f.default}`;
      const markChanged = () =>
        field.classList.toggle("changed", String(input.value) !== String(f.default));
      input.addEventListener("input", markChanged);
      markChanged();

      field.appendChild(label);
      field.appendChild(input);
      box.appendChild(field);
    }
  } catch (e) {
    $("score-status").textContent = "Fehler beim Laden: " + e.message;
  }
}

function formatAge(ts) {
  if (!ts) return "";
  const h = (Date.now() / 1000 - ts) / 3600;
  if (h < 1) return "gerade eben";
  if (h < 48) return `vor ${Math.round(h)} Std.`;
  return `vor ${Math.round(h / 24)} Tagen`;
}

async function loadChannelPenalties() {
  const list = $("channel-list");
  try {
    const res = await fetch("/api/channels");
    const rows = await res.json();
    list.innerHTML = "";
    $("channel-reset-all-btn").disabled = rows.length === 0;
    if (!rows.length) {
      const li = document.createElement("li");
      li.className = "channel-empty";
      li.textContent = "Keine Abzüge – alle Channels haben zuletzt funktioniert.";
      list.appendChild(li);
      return;
    }
    for (const r of rows) {
      const li = document.createElement("li");

      const info = document.createElement("div");
      info.className = "channel-info";
      const name = document.createElement("div");
      name.className = "channel-name";
      name.textContent = r.key;
      const meta = document.createElement("div");
      meta.className = "channel-meta";
      const parts = [];
      if (r.bans >= 0.1) parts.push(`${r.bans} Ban`);
      if (r.fails >= 0.1) parts.push(`${r.fails} Fehlversuche`);
      parts.push(formatAge(r.updated));
      meta.textContent = parts.join(" · ") + (r.last_reason ? ` – ${r.last_reason}` : "");
      info.appendChild(name);
      info.appendChild(meta);

      const pen = document.createElement("span");
      pen.className = "channel-penalty";
      pen.textContent = r.penalty;

      const btn = document.createElement("button");
      btn.className = "small-btn";
      btn.textContent = "Zurücksetzen";
      btn.addEventListener("click", () => resetChannelPenalties(r.key));

      li.appendChild(info);
      li.appendChild(pen);
      li.appendChild(btn);
      list.appendChild(li);
    }
  } catch (e) {
    list.textContent = "Fehler beim Laden: " + e.message;
  }
}

async function resetChannelPenalties(key) {
  if (!key && !confirm("Abzüge aller Channels zurücksetzen?")) return;
  try {
    const res = await fetch("/api/channels", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(key ? { key } : {}),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "unbekannter Fehler");
  } catch (e) {
    alert("Zurücksetzen fehlgeschlagen: " + e.message);
  }
  await loadChannelPenalties();
  // Treffer wurden serverseitig umsortiert -> Tabelle (und Indizes) neu laden
  await pollSearch();
}

async function saveScoreConfig(reset) {
  const statusEl = $("score-status");
  if (reset && !confirm("Alle Punkte auf Standard zurücksetzen?")) return;
  const values = {};
  for (const input of $("score-fields").querySelectorAll("input[data-key]")) {
    values[input.dataset.key] = input.value;
  }
  statusEl.textContent = "Speichere ...";
  try {
    const res = await fetch("/api/score", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(reset ? { reset: true } : { values }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "unbekannter Fehler");
    statusEl.textContent = "Gespeichert – Treffer neu sortiert.";
    await loadScoreConfig();
    await loadChannelPenalties();
    // Treffer wurden serverseitig umsortiert -> Tabelle (und Indizes) neu laden
    await pollSearch();
  } catch (e) {
    statusEl.textContent = "Fehler: " + e.message;
  }
  setTimeout(() => { statusEl.textContent = ""; }, 4000);
}

function setupCollapsibleCards() {
  const STORAGE_KEY = "xdcc-collapsed-cards";
  const MOBILE_KEY = "xdcc-mobile-open-card";
  // Handy: Akkordeon (nur eine Karte offen), eigener gespeicherter Zustand,
  // damit der Desktop-Zustand nicht ueberschrieben wird.
  const mobile = window.matchMedia("(max-width: 740px)");
  const cards = [...document.querySelectorAll(".card[data-card-id]")];
  let collapsed = {};
  try {
    collapsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
  } catch (e) { collapsed = {}; }
  let mobileOpen = "search";
  try {
    const stored = localStorage.getItem(MOBILE_KEY);
    if (stored !== null) mobileOpen = stored;
  } catch (e) { /* ignore */ }

  function apply() {
    for (const card of cards) {
      const id = card.dataset.cardId;
      card.classList.toggle("collapsed", mobile.matches ? id !== mobileOpen : !!collapsed[id]);
    }
  }

  for (const card of cards) {
    const id = card.dataset.cardId;
    card.querySelector(".card-header").addEventListener("click", () => {
      if (mobile.matches) {
        mobileOpen = card.classList.contains("collapsed") ? id : "";
        try { localStorage.setItem(MOBILE_KEY, mobileOpen); } catch (e) { /* ignore */ }
      } else {
        collapsed[id] = !card.classList.contains("collapsed");
        try { localStorage.setItem(STORAGE_KEY, JSON.stringify(collapsed)); } catch (e) { /* ignore */ }
      }
      apply();
    });
  }
  mobile.addEventListener("change", apply);
  apply();
}

function setVpnDropdownOpen(open) {
  $("vpn-wrap").classList.toggle("open", open);
  $("vpn-badge").setAttribute("aria-expanded", String(open));
}

function setupVpnBadge() {
  // Desktop: oeffnet per Hover (CSS). Klick/Tippen pinnt es offen, z.B. fuers Handy.
  $("vpn-badge").addEventListener("click", (e) => {
    e.stopPropagation();
    setVpnDropdownOpen(!$("vpn-wrap").classList.contains("open"));
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#vpn-wrap")) setVpnDropdownOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      setVpnDropdownOpen(false);
      document.activeElement.blur();
    }
  });
}

function setupMenuDropdown() {
  const dropdown = $("menu-dropdown");

  $("menu-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    setVpnDropdownOpen(false);
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
  loadWishlist().then(pollSearch);
  loadTarget();
  loadDownloaded();
  loadChatLog();
  loadRuns();
  pollStatus();
  loadVpnConfig();
  pollVpnStatus();

  loadScoreConfig();
  document.querySelector('[data-modal="score-modal"]').addEventListener("click", () => {
    loadScoreConfig();
    loadChannelPenalties();
  });
  $("channel-reset-all-btn").addEventListener("click", () => resetChannelPenalties(null));
  $("score-save-btn").addEventListener("click", () => saveScoreConfig(false));
  $("score-reset-btn").addEventListener("click", () => saveScoreConfig(true));
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
