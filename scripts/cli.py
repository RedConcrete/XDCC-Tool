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
    CATEGORY_DIRS,
    STAGING_DIR,
    _extra_channels_for,
    _merge_move,
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

MAX_CANDIDATES = 8
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


def process_item(title: str, category: str, status_cb=None, progress_cb=None,
                 irc_callback=None) -> bool:
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
        results = search_packs(query, irc_callback=irc_callback)
    except Exception as e:
        status(f"Suche fehlgeschlagen: {e}", "error")
        return False

    if not results:
        status("nichts gefunden", "warning")
        return False

    seen = set()
    deduped = []
    for p in results:
        key = (p.get("server"), p.get("channel"), p.get("bot"), p.get("pack"))
        if key not in seen:
            seen.add(key)
            deduped.append(p)

    candidates = sorted(deduped, key=lambda p: score_pack(p, category), reverse=True)
    expected = _sanitize_expected(title)
    channel_failures: dict[tuple, int] = {}

    for i, pack in enumerate(candidates[:MAX_CANDIDATES]):
        server = pack.get("server", "")
        bot_key = (server, pack.get("channel", ""), pack.get("bot", ""))

        if server in banned_servers:
            status(f"Überspringe {pack.get('channel')} – auf {server} gesperrt", "warning")
            continue
        if channel_failures.get(bot_key, 0) >= 2:
            status(f"Überspringe {pack.get('bot')} (zu viele Fehlversuche)", "warning")
            continue

        fname = pack.get("fname", "?")
        status(f"Versuch {i + 1}/{min(len(candidates), MAX_CANDIDATES)}: "
               f"{fname} ({server} {pack.get('channel')})")

        success, filename = False, None
        for _dl_try in range(2):  # max 1 Retry bei Verbindungsabbruch (DCC RESUME)
            _ban_flag = False
            try:
                success, filename = xdcc_download(
                    server=server,
                    channel=pack["channel"],
                    bot=pack["bot"],
                    pack=pack["pack"],
                    output_dir=STAGING_DIR,
                    port=pack.get("port", 6667),
                    timeout=60,
                    extra_channels=_extra_channels_for(server, pack["channel"]),
                    progress_callback=(lambda r, t: progress_cb(title, r, t)) if progress_cb else None,
                    status_callback=status,
                    stall_callback=_make_stall_cb(status),
                    expected_fname=expected,
                    irc_callback=irc_callback,
                )
            except Exception as e:
                status(f"Download-Fehler: {e}", "error")
                success, filename = False, None

            if _ban_flag:
                banned_servers.add(server)
                break
            if success:
                break
            # Verbindungsabbruch während DCC-Übertragung → Resume versuchen
            if filename and (STAGING_DIR / filename).exists() and _dl_try == 0:
                status("Verbindung unterbrochen – versuche DCC RESUME …", "warning")
                continue
            break

        if not success:
            if not filename:  # Kein Transfer gestartet → Bot-Fehlzähler erhöhen
                channel_failures[bot_key] = channel_failures.get(bot_key, 0) + 1
            continue

        status(f"Download fertig: {filename}", "success")
        try:
            downloaded_path = STAGING_DIR / filename
            created = postprocess(downloaded_path, category)
            target_base = CATEGORY_DIRS.get(category, STAGING_DIR)
            target_base.mkdir(parents=True, exist_ok=True)
            for item in created:
                _merge_move(item, target_base / item.name)
        except Exception as e:
            status(f"Einsortieren fehlgeschlagen: {e}", "error")
            traceback.print_exc()
            continue

        append_done(title)
        status("fertig & einsortiert", "success")
        return True

    status("fehlgeschlagen", "error")
    return False


def run_once(status_cb=None, progress_cb=None, irc_callback=None) -> dict:
    wishlist = load_wishlist()
    done = load_done()

    counts = {"ok": 0, "failed": 0, "skipped": 0, "total": 0}

    for category in ACTIVE_CATEGORIES:
        for title in wishlist.get(category, []):
            if title in done:
                counts["skipped"] += 1
                continue

            counts["total"] += 1
            try:
                ok = process_item(title, category, status_cb, progress_cb, irc_callback)
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
