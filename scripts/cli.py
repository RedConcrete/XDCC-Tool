#!/usr/bin/env python3
"""XDCC-Downloader: Wishlist abarbeiten (Suche, Download, Einsortierung)."""

import argparse
import fcntl
import json
import os
import shutil
import time
import re
import traceback
from pathlib import Path

from downloader import (
    search_packs,
    score_pack,
    load_score_config,
    channel_penalties,
    record_channel_result,
    handle_sasl_failure,
    clean_name,
    postprocess,
    category_dirs,
    staging_dir,
    _extra_channels_for,
    _merge_move,
    is_server_disabled,
    is_archive,
    keep_archives,
    video_files,
    drop_archive_if_complete,
)
from xdcc_client import xdcc_download

# Optionale Seerr/Radarr/Sonarr-Anbindung (scripts/arr.py). Ohne Modul oder
# ohne Eintraege in der .env verhaelt sich das Tool wie ohne Anbindung.
try:
    import arr
except Exception:  # pragma: no cover - Modul fehlt / nicht gemountet
    arr = None


def _meta(title: str, category: str) -> dict | None:
    if arr is None:
        return None
    try:
        return arr.get_meta(title, category)
    except Exception:
        return None

WISHLIST_PATH = Path(os.environ.get("WISHLIST", "/app/wishlist.md"))
DONE_LOG_PATH = Path(os.environ.get("DONE_LOG", "/app/downloaded.txt"))

# Reihenfolge in der Wishlist-Datei
SECTION_ORDER = ["serien", "film", "anime", "merkliste"]
SECTION_TITLES = {
    "serien": "Serien",
    "film": "Film",
    "anime": "Anime",
    "merkliste": "Merkliste",
}
# Mapping von Header-Text (lowercase) -> interner Key
SECTION_ALIASES = {
    "serien": "serien",
    "anime": "anime",
    "film": "film",
    "filme": "film",
    "merkliste": "merkliste",
}

# Kategorien, die tatsächlich gesucht/geladen werden (Merkliste nicht)
ACTIVE_CATEGORIES = ["serien", "film", "anime"]

# Letzter Fehlschlag pro Eintrag (fuer die roten Badges im Web-UI).
# logs/ ist ein Verzeichnis-Mount und ueberlebt so Container-Neustarts.
FAILED_PATH = Path(os.environ.get("WISHLIST_FAILED", "/app/logs/wishlist_failed.json"))

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


class _WishlistLock:
    """Dateisperre, damit Web-UI (Badge hinzufuegen/loeschen) und laufender
    Lauf (fertigen Eintrag entfernen) sich nicht gegenseitig ueberschreiben."""
    def __init__(self, path: Path = WISHLIST_PATH):
        self.lock_path = Path(str(path) + ".lock")

    def __enter__(self):
        self.fh = open(self.lock_path, "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def _same(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def add_item(title: str, category: str, path: Path = WISHLIST_PATH) -> bool:
    """Fuegt einen Eintrag hinzu. False, wenn er in der Kategorie schon steht."""
    title = title.strip()
    if not title or category not in SECTION_ORDER:
        return False
    with _WishlistLock(path):
        data = load_wishlist(path)
        if any(_same(t, title) for t in data[category]):
            return False
        data[category].append(title)
        save_wishlist(data, path)
    clear_failed(title, category)
    return True


def remove_item(title: str, category: str, path: Path = WISHLIST_PATH) -> bool:
    with _WishlistLock(path):
        data = load_wishlist(path)
        before = len(data.get(category, []))
        data[category] = [t for t in data.get(category, []) if not _same(t, title)]
        if len(data[category]) == before:
            return False
        save_wishlist(data, path)
    clear_failed(title, category)
    if arr is not None:
        arr.pop_meta(title, category)
    return True


def move_item(title: str, src: str, dst: str, path: Path = WISHLIST_PATH) -> bool:
    if src not in SECTION_ORDER or dst not in SECTION_ORDER or src == dst:
        return False
    with _WishlistLock(path):
        data = load_wishlist(path)
        hit = next((t for t in data[src] if _same(t, title)), None)
        if hit is None:
            return False
        data[src] = [t for t in data[src] if not _same(t, title)]
        if not any(_same(t, hit) for t in data[dst]):
            data[dst].append(hit)
        save_wishlist(data, path)
    clear_failed(title, src)
    if arr is not None:
        arr.move_meta(title, src, dst)
    return True


def _failed_key(title: str, category: str) -> str:
    return f"{category}|{title.strip().lower()}"


def load_failed(path: Path = FAILED_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_failed(data: dict, path: Path = FAILED_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        traceback.print_exc()


def set_failed(title: str, category: str, reason: str) -> None:
    data = load_failed()
    data[_failed_key(title, category)] = {"reason": reason, "ts": time.time()}
    _save_failed(data)


def clear_failed(title: str, category: str) -> None:
    data = load_failed()
    if data.pop(_failed_key(title, category), None) is not None:
        _save_failed(data)


def failed_info(title: str, category: str, failed: dict | None = None) -> dict | None:
    return (failed if failed is not None else load_failed()).get(_failed_key(title, category))


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


def wishlist_title(fname: str) -> str:
    """Titel-Vorschlag fuer die Wishlist aus einem Dateinamen, z.B.
    'Overlord.S04E05.German.1080p...' -> 'Overlord S04E05'."""
    title = clean_name(re.sub(r"\.(mkv|mp4|avi|m4v|ts|tar|zip|rar|7z)$", "", fname, flags=re.IGNORECASE))
    m = re.search(r"\bS(\d{1,2})(?:E(\d{1,3}))?\b", fname, re.IGNORECASE)
    if m:
        title += f" S{int(m.group(1)):02d}"
        if m.group(2):
            title += f"E{int(m.group(2)):02d}"
    return title


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
    return rank_candidates(deduped, query, category)


_EPISODE_TAG_RE = re.compile(r"\bS(\d{1,2})E(\d{1,3})\b", re.IGNORECASE)
_SEASON_TAG_RE = re.compile(r"\bS(\d{1,2})(?:E\d{1,3})?\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"^(19|20)\d\d$")
# Ab diesen Tokens beginnt im Dateinamen der technische Teil (Sprache,
# Aufloesung, Codec, Ton ...). Zahlen dahinter (z.B. "5.1" Ton, "1080p")
# duerfen nicht als Teil-Nummer des Titels gelten.
_TECH_TOKEN_RE = re.compile(
    r"^(german|deutsch|english|ger|eng|dl|md|ld|ac3|aac\d*|dts|ddp?\d*|eac3|truehd|atmos|"
    r"\d{3,4}p|4k|uhd|hdr|sdr|bluray|blu|ray|webrip|web|hdtv|dvdrip|bdrip|remux|"
    r"h26[45]|x26[45]|hevc|avc|xvid|hi10p|"
    r"proper|repack|extended|theatrical|uncut|remastered|unrated|imax|"
    r"multi|dual|audio|dubbed|subbed|complete|"
    r"s\d{1,2}(e\d{1,3})?|staffel|season)$",
    re.IGNORECASE,
)


def _title_part(fname: str) -> list[str]:
    """Tokens des eigentlichen Titels: alles vor Jahr, Staffel-Tag oder
    technischen Angaben. '[ irc.x.net ] '-Praefixe (xdcc.eu) werden entfernt."""
    name = re.sub(r"^\s*\[[^\]]*\]\s*", "", fname)
    name = re.sub(r"\.(mkv|mp4|avi|m4v|ts|tar|zip|rar|7z)$", "", name, flags=re.IGNORECASE)
    tokens = [t for t in re.split(r"[\s._\-()\[\]]+", name) if t]
    out = []
    for t in tokens:
        if _YEAR_RE.match(t) or _TECH_TOKEN_RE.match(t):
            break
        out.append(t)
    return out


def _years(text: str) -> set[str]:
    return {t for t in re.split(r"[\s._\-()\[\]]+", text) if _YEAR_RE.match(t)}


def release_matches(query: str, fname: str) -> bool:
    """Passt ein Release zur gesuchten Teil-Nummer / Jahr / Staffel / Folge?

    - Teil-Nummern ("Toy Story 4") muessen im Titelteil des Dateinamens
      stehen - Zahlen hinter Jahr/Ton/Aufloesung ("5.1", "1080p") zaehlen
      nicht. "Teil 1" passt auch auf Releases ganz ohne Nummer
      ("Toy.Story.1995..."), aber nie auf einen anderen Teil.
    - Ein Jahr in der Suche muss zum Jahr im Dateinamen passen (falls es
      eins hat).
    - Staffel/Folge (S02, S04E05) muessen passen; zu einer gesuchten Folge
      passt auch das Staffelpaket derselben Staffel.
    """
    q_tokens = [t for t in re.split(r"[\s._\-()\[\]]+", query) if t]
    q_nums = {str(int(t)) for t in q_tokens if t.isdigit() and not _YEAR_RE.match(t)}
    q_years = {t for t in q_tokens if _YEAR_RE.match(t)}

    if q_nums:
        f_nums = {str(int(t)) for t in _title_part(fname) if t.isdigit()}
        if f_nums:
            if not q_nums <= f_nums:
                return False
        elif q_nums != {"1"}:
            return False

    if q_years:
        f_years = _years(fname)
        if f_years and not (q_years & f_years):
            return False

    q_ep = _EPISODE_TAG_RE.search(query)
    q_season = _SEASON_TAG_RE.search(query)
    if q_season:
        f_season = _SEASON_TAG_RE.search(fname.replace("_", "."))
        if f_season and int(f_season.group(1)) != int(q_season.group(1)):
            return False
        if q_ep:
            f_ep = _EPISODE_TAG_RE.search(fname.replace("_", "."))
            if f_ep and int(f_ep.group(2)) != int(q_ep.group(2)):
                return False
            if not f_season:
                return False  # ohne Staffelangabe nicht sicher die gesuchte Folge
    return True


def rank_candidates(packs: list[dict], query: str, category: str) -> list[dict]:
    """Nur passende Packs (Teil/Jahr/Staffel/Folge, siehe release_matches),
    sortiert nach score_pack. Unpassende werden verworfen - frueher hingen
    sie als Ausweichliste hinten dran, so kam z.B. bei "Toy Story 1" Teil 5."""
    cfg = load_score_config()
    penalties = channel_penalties(cfg)
    q_ep = _EPISODE_TAG_RE.search(query)

    def key(p):
        score = score_pack(p, category, cfg, penalties)
        # Bei gesuchter Folge die Folge selbst vor dem Staffelpaket
        if q_ep and _EPISODE_TAG_RE.search(p.get("fname", "").replace("_", ".")):
            score += 1000
        return score

    relevant = [p for p in packs if release_matches(query, p.get("fname", ""))]
    return sorted(relevant, key=key, reverse=True)


def download_pack(pack: dict, staging: Path, expected: str, status, progress=None,
                  irc_callback=None, stop_event=None) -> tuple[bool, str | None]:
    """Laedt ein einzelnes Pack nach staging. Gibt (success, filename) zurueck.
    Das Ergebnis fliesst in die Channel-Statistik (Abzug fuer Fehlversuche/Bans)."""
    server = pack.get("server", "")
    seen = {"ban": False, "reason": ""}

    def tracked_status(msg, level="info"):
        if msg.startswith("Gesperrt ("):
            seen["ban"] = True
        if level in ("error", "warning"):
            seen["reason"] = msg
        status(msg, level)

    success, filename = False, None
    try:
        success, filename = xdcc_download(
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
                (lambda: handle_sasl_failure(server, pack["channel"]))
                if pack.get("sasl_user") else None
            ),
            timeout=60,
            extra_channels=_extra_channels_for(server, pack["channel"]),
            progress_callback=progress,
            status_callback=tracked_status,
            stall_callback=_make_stall_cb(tracked_status),
            expected_fname=expected,
            irc_callback=irc_callback,
            stop_event=stop_event,
        )
    finally:
        # Abbruch durch den Nutzer ist kein Fehler des Channels
        if not (stop_event and stop_event.is_set()):
            record_channel_result(server, pack.get("channel", ""), ok=success,
                                  banned=seen["ban"], reason=seen["reason"])
    return success, filename


def sort_into_library(downloaded_path: Path, category: str, status=None) -> None:
    """Entpacken, umbenennen und in den Kategorie-Ordner der Zielplatte verschieben.
    Ist "Archive behalten" aus, wird das Archiv danach geloescht - aber nur,
    wenn alle Videos vollstaendig in der Bibliothek angekommen sind."""
    created = postprocess(downloaded_path, category)
    target_base = category_dirs().get(category, downloaded_path.parent)
    target_base.mkdir(parents=True, exist_ok=True)
    expected = [(target_base / f.relative_to(item.parent), f.stat().st_size)
                for item in created for f in video_files(item)]
    for item in created:
        _merge_move(item, target_base / item.name)

    if is_archive(downloaded_path) and downloaded_path.exists() and not keep_archives():
        removed, reason = drop_archive_if_complete(downloaded_path, expected)
        if status:
            if removed:
                status(f"Archiv gelöscht ({reason})", "info")
            else:
                status(f"Archiv behalten: {reason}", "warning")


def _open_perms(path: Path) -> None:
    """Radarr/Sonarr laufen in einem anderen Container/User und muessen die
    Dateien spaeter umbenennen/ersetzen koennen."""
    try:
        path.chmod(0o777 if path.is_dir() else 0o666)
    except OSError:
        pass


def sort_into_arr(downloaded_path: Path, category: str, meta: dict, status=None) -> bool:
    """Wie sort_into_library, aber Ziel ist der Ordner, den Radarr/Sonarr
    fuer den Titel angelegt haben; danach liest Radarr/Sonarr neu ein.
    False = kein passender Radarr/Sonarr-Eintrag, normal einsortieren."""
    found = arr.find_arr_item(meta, wait=0)
    if not found:
        return False
    app, item = found
    dest_root = arr.host_to_container(item["path"])
    if not dest_root.parent.exists():
        if status:
            status(f"{app}-Ordner {dest_root} nicht erreichbar – normal einsortieren", "warning")
        return False

    created = postprocess(downloaded_path, "film" if app == "radarr" else "serien")
    dest_root.mkdir(parents=True, exist_ok=True)
    _open_perms(dest_root)
    expected = []
    for item_path in created:
        for f in video_files(item_path):
            if app == "radarr":
                dest = dest_root / f"{dest_root.name}{f.suffix}"
                if dest.exists():
                    dest = dest_root / f"{dest_root.name} - German{f.suffix}"
            else:
                season = f.parent.name if f.parent.name.lower().startswith("season") else \
                    f"Season {int(meta.get('season') or 1):02d}"
                (dest_root / season).mkdir(parents=True, exist_ok=True)
                _open_perms(dest_root / season)
                dest = dest_root / season / f.name
            size = f.stat().st_size
            shutil.move(str(f), str(dest))
            _open_perms(dest)
            expected.append((dest, size))
        shutil.rmtree(item_path, ignore_errors=True)

    try:
        arr.rescan(app, item)
    except Exception as e:
        if status:
            status(f"{app} neu einlesen fehlgeschlagen: {e}", "warning")
    if status:
        status(f"In {app}-Ordner einsortiert: {item['path']}", "info")

    if is_archive(downloaded_path) and downloaded_path.exists() and not keep_archives():
        removed, why = drop_archive_if_complete(downloaded_path, expected)
        if status:
            status(f"Archiv gelöscht ({why})" if removed else f"Archiv behalten: {why}",
                   "info" if removed else "warning")
    return True


_GERMAN_RE = re.compile(r"(?<![a-z])(german|deutsch|ger)(?![a-z])", re.IGNORECASE)


def process_item(title: str, category: str, status_cb=None, progress_cb=None,
                 irc_callback=None, stop_event=None, reason: dict | None = None,
                 meta: dict | None = None) -> bool:
    """reason (optional) bekommt unter "msg" den letzten Fehler/Hinweis -
    wird im Web-UI am roten Badge angezeigt."""
    banned_servers: set[str] = set()
    _ban_flag = False

    def status(msg, level="info"):
        nonlocal _ban_flag
        if msg.startswith("Gesperrt ("):
            _ban_flag = True
        if reason is not None and level in ("error", "warning") and msg != "fehlgeschlagen":
            reason["msg"] = msg
        if status_cb:
            status_cb(title, msg, level)

    # Seerr-Eintraege: nur deutsche Fassungen (Englisch macht Radarr/Sonarr),
    # bei leerem Ergebnis auch unter dem Originaltitel suchen.
    queries = [title] + list((meta or {}).get("alt") or [])
    candidates, used = [], title
    for q_title in queries:
        query = _search_query(q_title)
        status(f"Suche: {query}" if query != q_title else f"Suche: {q_title}")
        try:
            candidates = find_candidates(query, category, irc_callback=irc_callback,
                                         stop_event=stop_event)
        except Exception as e:
            status(f"Suche fehlgeschlagen: {e}", "error")
            return False
        if stop_event and stop_event.is_set():
            status("Abgebrochen durch Nutzer", "warning")
            return False
        if meta:
            candidates = [p for p in candidates if _GERMAN_RE.search(p.get("fname", ""))]
        if candidates:
            used = q_title
            break

    if not candidates:
        status("keine deutsche Fassung gefunden" if meta else "nichts gefunden", "warning")
        return False

    expected = _sanitize_expected(used)
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
            if not (meta and arr is not None and sort_into_arr(staging / filename, category, meta, status)):
                sort_into_library(staging / filename, category, status)
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
        sort_into_library(staging / filename, category, status)
    except Exception as e:
        status(f"Einsortieren fehlgeschlagen: {e}", "error")
        traceback.print_exc()
        return {"ok": 0, "failed": 1, "skipped": 0, "total": 1}

    status("fertig & einsortiert", "success")
    return {"ok": 1, "failed": 0, "skipped": 0, "total": 1}


def _next_open(attempted: set) -> tuple[str, str] | None:
    """Naechster offener Eintrag. Die Wishlist wird jedes Mal neu gelesen,
    damit waehrend eines Laufs hinzugefuegte Badges noch mitgenommen werden."""
    wishlist = load_wishlist()
    for category in ACTIVE_CATEGORIES:
        for title in wishlist.get(category, []):
            if (category, title) not in attempted:
                return category, title
    return None


def run_once(status_cb=None, progress_cb=None, irc_callback=None, stop_event=None) -> dict:
    """Arbeitet alle Eintraege ab. Fertige werden aus der Wishlist entfernt
    (und in downloaded.txt protokolliert), fehlgeschlagene bleiben stehen
    und bekommen den Grund in wishlist_failed.json."""
    counts = {"ok": 0, "failed": 0, "skipped": 0, "total": 0}
    attempted: set[tuple[str, str]] = set()

    while not (stop_event and stop_event.is_set()):
        nxt = _next_open(attempted)
        if not nxt:
            break
        category, title = nxt
        attempted.add(nxt)
        meta = _meta(title, category)
        if meta and meta.get("fallback"):
            counts["skipped"] += 1  # laeuft schon ueber Radarr/Sonarr
            continue
        counts["total"] += 1
        reason: dict = {}
        try:
            ok = process_item(title, category, status_cb, progress_cb, irc_callback,
                              stop_event=stop_event, reason=reason, meta=meta)
        except Exception as e:
            ok = False
            reason["msg"] = f"unerwarteter Fehler: {e}"
            if status_cb:
                status_cb(title, reason["msg"], "error")
            traceback.print_exc()

        if ok:
            counts["ok"] += 1
            remove_item(title, category)
        elif stop_event and stop_event.is_set():
            break  # abgebrochen ist kein Fehlschlag
        else:
            counts["failed"] += 1
            msg = reason.get("msg") or "fehlgeschlagen"
            if meta and arr is not None:
                try:
                    fb = arr.fallback_search(meta)
                except Exception as e:
                    fb = None
                    msg = f"{msg} (Radarr/Sonarr nicht erreichbar: {e})"
                if fb:
                    msg = fb
                    meta["fallback"] = True
                    arr.set_meta(title, category, meta)
                    if status_cb:
                        status_cb(title, fb, "warning")
            set_failed(title, category, msg)

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
