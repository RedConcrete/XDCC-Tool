#!/usr/bin/env python3
"""XDCC-Downloader: Wishlist abarbeiten (Suche, Download, Einsortierung)."""

import argparse
import os
import re
import traceback
from pathlib import Path

from downloader import (
    search_packs,
    score_pack,
    clean_name,
    postprocess,
    category_dirs,
    staging_dir,
    _extra_channels_for,
    _merge_move,
    is_server_disabled,
    mark_server_disabled,
)
from xdcc_client import xdcc_download

WISHLIST_PATH = Path(os.environ.get("WISHLIST", "/app/wishlist.md"))
DONE_LOG_PATH = Path(os.environ.get("DONE_LOG", "/app/downloaded.txt"))

# Reihenfolge in der Wishlist-Datei
SECTION_ORDER = ["serien", "film", "merkliste"]
SECTION_TITLES = {
    "serien": "Serien",
    "film": "Film",
    "merkliste": "Merkliste",
}
# Mapping von Header-Text (lowercase) -> interner Key
SECTION_ALIASES = {
    "serien": "serien",
    "anime": "serien",
    "film": "film",
    "filme": "film",
    "merkliste": "merkliste",
}

# Kategorien, die tatsächlich gesucht/geladen werden (Merkliste nicht)
ACTIVE_CATEGORIES = ["serien", "film"]

MAX_CANDIDATES = 15
MAX_STALLS = 3  # max. Anzahl 60s-Stalls bevor Kandidat abgebrochen wird


def load_wishlist(path: Path = WISHLIST_PATH) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {k: [] for k in SECTION_ORDER}
    if not path.exists():
        return result

    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.rstrip()
        m = re.match(r"^##\s+(.+)$", line)
        if m:
            current = SECTION_ALIASES.get(m.group(1).strip().lower())
            continue
        m = re.match(r"^-\s+(.+)$", line)
        if m and current:
            title = m.group(1).strip()
            if title:
                result[current].append(title)
    return result


def save_wishlist(data: dict[str, list[str]], path: Path = WISHLIST_PATH) -> None:
    lines = ["# XDCC Wunschliste", ""]
    for key in SECTION_ORDER:
        lines.append(f"## {SECTION_TITLES[key]}")
        lines.append("")
        for title in data.get(key, []):
            title = title.strip()
            if title:
                lines.append(f"- {title}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def load_done(path: Path = DONE_LOG_PATH) -> set[str]:
    if not path.exists():
        return set()
    return {l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}


def append_done(title: str, path: Path = DONE_LOG_PATH) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(title + "\n")


def remove_done(title: str, path: Path = DONE_LOG_PATH) -> bool:
    if not path.exists():
        return False
    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if title not in lines:
        return False
    lines = [l for l in lines if l != title]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return True


_SEASON_RE = re.compile(r"\b(?:staffel|season)\s*(\d{1,2})\b", re.IGNORECASE)


def _search_query(title: str) -> str:
    """'Staffel NN'/'Season NN' -> 'SNN' (Suchindizes kennen nur die
    englische SxxExx-Notation) und entfernt Satzzeichen (z.B. ':'), die
    bei xdcc.eu zu 0 Treffern fuehren."""
    query = _SEASON_RE.sub(lambda m: f"S{int(m.group(1)):02d}", title)
    query = re.sub(r"[^\w\s]", " ", query)
    return re.sub(r"\s+", " ", query).strip()


def _sanitize_expected(title: str) -> str:
    """clean_name() laesst Satzzeichen/Staffelangaben uebrig, die
    _titles_match() gegen reale Dateinamen nicht matcht (z.B. 'Staffel 22'
    oder das Apostroph in "Grey's Anatomy" vs. 'Greys.Anatomy...')."""
    name = clean_name(title)
    name = _SEASON_RE.sub("", name)
    name = name.replace("'", "").replace("’", "")
    name = re.sub(r"[^\w\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _make_stall_cb(status):
    state = {"stalls": 0}

    def stall_cb(received, total):
        state["stalls"] += 1
        mb = received // 1024 // 1024
        status(f"Keine Daten seit 60s ({mb} MB) - Versuch {state['stalls']}/{MAX_STALLS}", "warning")
        return state["stalls"] < MAX_STALLS

    return stall_cb


def find_candidates(query: str, category: str, irc_callback=None, stop_event=None) -> list[dict]:
    """Sucht Packs, entfernt Duplikate und sortiert nach Relevanz + score_pack."""
    results = search_packs(query, irc_callback=irc_callback, stop_event=stop_event)

    seen = set()
    deduped = []
    for p in results:
        key = (p.get("server"), p.get("channel"), p.get("bot"), p.get("pack"))
        if key not in seen:
            seen.add(key)
            deduped.append(p)

    # Zahlen im gesuchten Titel (z.B. "The Purge 3") muessen auch im
    # Dateinamen vorkommen - sonst waehlt score_pack (der nur Qualitaet/
    # Sprache/Groesse bewertet, nicht Relevanz) bei Franchise-Titeln mit
    # mehreren Teilen leicht den falschen Teil (z.B. immer den mit dem
    # hoechsten Qualitaets-Score, unabhaengig von der gesuchten Nummer).
    def _matches_query_numbers(fname: str) -> bool:
        q_nums = set(re.findall(r"\b\d+\b", query))
        if not q_nums:
            return True
        f_nums = set(re.findall(r"\b\d+\b", fname.replace(".", " ").replace("_", " ")))
        return bool(q_nums & f_nums)

    relevant = [p for p in deduped if _matches_query_numbers(p.get("fname", ""))]
    irrelevant = [p for p in deduped if p not in relevant]
    return (
        sorted(relevant, key=lambda p: score_pack(p, category), reverse=True)
        + sorted(irrelevant, key=lambda p: score_pack(p, category), reverse=True)
    )


def download_pack(pack: dict, staging: Path, expected: str, status, progress=None,
                  irc_callback=None, stop_event=None) -> tuple[bool, str | None]:
    """Laedt ein einzelnes Pack nach staging. Gibt (success, filename) zurueck."""
    server = pack.get("server", "")
    return xdcc_download(
        server=server,
        channel=pack["channel"],
        bot=pack["bot"],
        pack=pack["pack"],
        output_dir=staging,
        port=pack.get("port", 6667),
        tls=pack.get("tls", False),
        sasl_user=pack.get("sasl_user", ""),
        sasl_pass=pack.get("sasl_pass", ""),
        sasl_fail_callback=(
            (lambda srv=server: mark_server_disabled(
                srv, "SASL-Login fehlgeschlagen (kein/ungültiger Account)"))
            if pack.get("sasl_user") else None
        ),
        timeout=60,
        extra_channels=_extra_channels_for(server, pack["channel"]),
        progress_callback=progress,
        status_callback=status,
        stall_callback=_make_stall_cb(status),
        expected_fname=expected,
        irc_callback=irc_callback,
        stop_event=stop_event,
    )


def sort_into_library(downloaded_path: Path, category: str) -> None:
    """Entpacken, umbenennen und in den Kategorie-Ordner der Zielplatte verschieben."""
    created = postprocess(downloaded_path, category)
    target_base = category_dirs().get(category, downloaded_path.parent)
    target_base.mkdir(parents=True, exist_ok=True)
    for item in created:
        _merge_move(item, target_base / item.name)


def process_item(title: str, category: str, status_cb=None, progress_cb=None,
                 irc_callback=None, stop_event=None) -> bool:
    banned_servers: set[str] = set()
    _ban_flag = False

    def status(msg, level="info"):
        nonlocal _ban_flag
        if msg.startswith("Gesperrt ("):
            _ban_flag = True
        if status_cb:
            status_cb(title, msg, level)

    query = _search_query(title)
    status(f"Suche: {query}" if query != title else f"Suche: {title}")
    try:
        candidates = find_candidates(query, category, irc_callback=irc_callback,
                                     stop_event=stop_event)
    except Exception as e:
        status(f"Suche fehlgeschlagen: {e}", "error")
        return False

    if stop_event and stop_event.is_set():
        status("Abgebrochen durch Nutzer", "warning")
        return False

    if not candidates:
        status("nichts gefunden", "warning")
        return False

    expected = _sanitize_expected(title)
    channel_failures: dict[tuple, int] = {}
    # Ziel wird einmal pro Titel fixiert, damit ein Wechsel der Zielplatte
    # waehrend des Laufs nicht mitten in einem Download-Versuch greift.
    staging = staging_dir()

    for i, pack in enumerate(candidates[:MAX_CANDIDATES]):
        if stop_event and stop_event.is_set():
            status("Abgebrochen durch Nutzer", "warning")
            return False

        server = pack.get("server", "")
        bot_key = (server, pack.get("channel", ""), pack.get("bot", ""))

        if server in banned_servers:
            status(f"Überspringe {pack.get('channel')} – auf {server} gesperrt", "warning")
            continue
        if is_server_disabled(server):
            status(f"Überspringe {server} – kein gültiger Account (dauerhaft deaktiviert)", "warning")
            continue
        if channel_failures.get(bot_key, 0) >= 2:
            status(f"Überspringe {pack.get('bot')} (zu viele Fehlversuche)", "warning")
            continue

        fname = pack.get("fname", "?")
        status(f"Versuch {i + 1}/{min(len(candidates), MAX_CANDIDATES)}: "
               f"{fname} ({server} {pack.get('channel')})")

        _ban_flag = False
        success, filename = False, None
        try:
            success, filename = download_pack(
                pack, staging, expected, status,
                progress=(lambda r, t: progress_cb(title, r, t)) if progress_cb else None,
                irc_callback=irc_callback,
                stop_event=stop_event,
            )
        except Exception as e:
            status(f"Download-Fehler: {e}", "error")
            success, filename = False, None

        if stop_event and stop_event.is_set():
            return False

        if _ban_flag:
            banned_servers.add(server)
        elif not success and filename and (staging / filename).exists():
            # Verbindungsabbruch mit Teildatei → nächster Bot wird DCC RESUME anbieten
            mb = (staging / filename).stat().st_size / 1024 / 1024
            status(f"Verbindung unterbrochen – {mb:.0f} MB gesichert, nächster Bot versucht Resume", "warning")

        if not success:
            if not filename:  # Kein Transfer gestartet → Bot-Fehlzähler erhöhen
                channel_failures[bot_key] = channel_failures.get(bot_key, 0) + 1
            continue

        status(f"Download fertig: {filename}", "success")
        try:
            sort_into_library(staging / filename, category)
        except Exception as e:
            status(f"Einsortieren fehlgeschlagen: {e}", "error")
            traceback.print_exc()
            continue

        append_done(title)
        status("fertig & einsortiert", "success")
        return True

    status("fehlgeschlagen", "error")
    return False


def download_single(pack: dict, category: str, status_cb=None, progress_cb=None,
                    irc_callback=None, stop_event=None) -> dict:
    """Manuell ausgewaehltes Pack laden und einsortieren (ohne Wishlist,
    ohne Fallback auf andere Bots). Rueckgabe im Format von run_once()."""
    title = pack.get("fname", "?")

    def status(msg, level="info"):
        if status_cb:
            status_cb(title, msg, level)

    status(f"Manueller Download: {pack.get('bot')} #{pack.get('pack')} "
           f"({pack.get('server')} {pack.get('channel')})")
    staging = staging_dir()
    try:
        success, filename = download_pack(
            pack, staging, title, status,
            progress=(lambda r, t: progress_cb(title, r, t)) if progress_cb else None,
            irc_callback=irc_callback,
            stop_event=stop_event,
        )
    except Exception as e:
        status(f"Download-Fehler: {e}", "error")
        success, filename = False, None

    if stop_event and stop_event.is_set():
        status("Abgebrochen durch Nutzer", "warning")
        return {"ok": 0, "failed": 1, "skipped": 0, "total": 1}

    if not success:
        status("fehlgeschlagen", "error")
        return {"ok": 0, "failed": 1, "skipped": 0, "total": 1}

    status(f"Download fertig: {filename}", "success")
    try:
        sort_into_library(staging / filename, category)
    except Exception as e:
        status(f"Einsortieren fehlgeschlagen: {e}", "error")
        traceback.print_exc()
        return {"ok": 0, "failed": 1, "skipped": 0, "total": 1}

    status("fertig & einsortiert", "success")
    return {"ok": 1, "failed": 0, "skipped": 0, "total": 1}


def run_once(status_cb=None, progress_cb=None, irc_callback=None, stop_event=None) -> dict:
    wishlist = load_wishlist()
    done = load_done()

    counts = {"ok": 0, "failed": 0, "skipped": 0, "total": 0}

    for category in ACTIVE_CATEGORIES:
        if stop_event and stop_event.is_set():
            break
        for title in wishlist.get(category, []):
            if stop_event and stop_event.is_set():
                break
            if title in done:
                counts["skipped"] += 1
                continue

            counts["total"] += 1
            try:
                ok = process_item(title, category, status_cb, progress_cb, irc_callback,
                                  stop_event=stop_event)
            except Exception as e:
                ok = False
                if status_cb:
                    status_cb(title, f"unerwarteter Fehler: {e}", "error")
                traceback.print_exc()

            if ok:
                counts["ok"] += 1
                done.add(title)
            else:
                counts["failed"] += 1

    return counts


def main():
    parser = argparse.ArgumentParser(description="XDCC-Downloader")
    parser.add_argument("--once", action="store_true",
                         help="Wishlist einmal abarbeiten und beenden")
    parser.parse_args()

    from rich.console import Console
    console = Console()

    def status_cb(title, msg, level="info"):
        style = {"error": "bold red", "warning": "yellow", "success": "bold green"}.get(level)
        console.print(f"[bold]{title}[/bold]: {msg}", style=style)

    console.print("[bold cyan]XDCC-Downloader: Wishlist wird abgearbeitet ...[/bold cyan]")
    counts = run_once(status_cb)
    console.print(
        f"\n[bold]Fertig.[/bold] {counts['ok']} geladen, "
        f"{counts['failed']} fehlgeschlagen, {counts['skipped']} bereits vorhanden "
        f"({counts['total']} offene Titel verarbeitet)"
    )


if __name__ == "__main__":
    main()
