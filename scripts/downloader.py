#!/usr/bin/env python3
"""
XDCC Wishlist Downloader
Sucht via xdcc.eu und lädt per eigenem IRC/DCC-Client herunter.

Wishlist-Format (wishlist.md):
  ## Anime
  - Demon Slayer S02

  ## Serien
  - Breaking Bad S01E01

  ## Filme
  - Das Boot 1981
"""

import os
import re
import shutil
import socket
import tarfile
import zipfile
import time
import math
import argparse
import logging
import random
import string
import json
import urllib.request
from pathlib import Path
from xdcc_client import xdcc_download

# Noise-Tags die aus Dateinamen entfernt werden
NOISE_TAGS = re.compile(
    r"\b(german|deutsch|english|dl|aac|aac2|ac3|dts|"
    r"1080p|720p|480p|2160p|4k|uhd|"
    r"bluray|blu-ray|webrip|web-dl|web|hdtv|dvdrip|bdrip|"
    r"h264|h265|x264|x265|hevc|xvid|"
    r"proper|repack|extended|theatrical|"
    r"multi|dubbed|subbed)\b",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

WISHLIST_PATH  = Path(os.environ.get("WISHLIST",      "/app/wishlist.md"))
DOWNLOAD_BASE  = Path(os.environ.get("DOWNLOAD_DIR",  "/downloads"))
DONE_LOG       = Path(os.environ.get("DONE_LOG",      "/app/downloaded.txt"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "1800"))
SEARCH_LANG    = os.environ.get("SEARCH_LANG",        "German")

STAGING_DIR = DOWNLOAD_BASE / "downloads"

CATEGORY_DIRS = {
    "serien": DOWNLOAD_BASE / "serien",
    "anime":  DOWNLOAD_BASE / "serien",
    "filme":  DOWNLOAD_BASE / "movies",
}

BEAST_SERVER  = "irc.abjects.net"
BEAST_CHANNEL = "#beast-xdcc"
BEAST_SEARCH_CHANNEL = "#beast-chat"
BEAST_BOT     = "databeast"

VPN_CHECK_URL     = "https://am.i.mullvad.net/json"
VPN_REQUIRED      = os.environ.get("VPN_REQUIRED", "true").lower() == "true"
VPN_RETRY_INTERVAL = int(os.environ.get("VPN_RETRY_INTERVAL", "30"))


def check_vpn() -> bool:
    """Prüft ob Mullvad VPN aktiv ist via am.i.mullvad.net."""
    try:
        req = urllib.request.Request(VPN_CHECK_URL, headers={"User-Agent": "curl/7.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            connected = data.get("mullvad_exit_ip", False)
            if connected:
                server = data.get("mullvad_exit_ip_hostname", "unbekannt")
                country = data.get("country", "")
                log.info(f"VPN aktiv: {server} ({country})")
            return connected
    except Exception as e:
        log.warning(f"VPN-Prüfung fehlgeschlagen: {e}")
        return False


def wait_for_vpn():
    """Prüft VPN beim Start. Bei Fehler: CLI-Prompt ob ohne VPN weiter oder abbrechen."""
    if not VPN_REQUIRED:
        return

    if check_vpn():
        return

    print("\n" + "="*60)
    print("  FEHLER: VPN nicht verbunden!")
    print(f"  Konfigurierter Dienst: Mullvad ({VPN_CHECK_URL})")
    print("="*60)
    print("\n  Das Script ist so konfiguriert, dass es NUR mit VPN")
    print("  läuft. Ein Download ohne VPN ist nicht empfohlen.\n")

    try:
        antwort = input("  Ohne VPN fortfahren? [j/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        antwort = "n"

    if antwort != "j":
        print("\n  Abgebrochen. Bitte VPN starten und erneut versuchen.")
        print("="*60 + "\n")
        raise SystemExit(1)

    print("\n  ⚠  Fortfahren OHNE VPN auf eigene Gefahr.\n")


def parse_wishlist(path: Path) -> dict[str, list[str]]:
    categories: dict[str, list[str]] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("## "):
            current = line[3:].strip().lower()
            if current == "merkliste":
                current = None  # Merkliste wird nicht heruntergeladen
            else:
                categories[current] = []
        elif line.startswith("- ") and current is not None:
            entry = line[2:].strip()
            if entry and not entry.startswith("#"):
                categories[current].append(entry)
    return categories


def already_done(entry: str) -> bool:
    if not DONE_LOG.exists():
        return False
    return entry.lower() in [l.lower() for l in DONE_LOG.read_text().splitlines()]


def mark_done(entry: str):
    with open(DONE_LOG, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


def parse_size(size_str: str) -> int:
    size_str = size_str.strip().upper()
    m = re.match(r"([\d.]+)([KMGT]?)", size_str)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2)
    return int(val * {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}.get(unit, 1))


IRC_COLOR = re.compile(r"\x03\d{0,2}(?:,\d{1,2})?|\x02|\x0f|\x16|\x1f|\x1d")
PAT_PACK  = re.compile(
    r"\(([^)]+)\)\s+(\S+)\s+\((\d+)x\)\s+/msg\s+(\S+)\s+xdcc\s+send\s+#(\d+)",
    re.IGNORECASE,
)


def search_beast_irc_multi(queries: list[tuple[str, str]]) -> dict[str, list[dict]]:
    """Sucht mehrere Einträge in einer einzigen IRC-Session.

    queries: Liste von (entry, search_term)
    Gibt dict {entry: [packs]} zurück.
    Wartet einmalig 60s (Flood-Schutz), dann alle Suchen nacheinander.
    """
    results = {entry: [] for entry, _ in queries}
    if not queries:
        return results

    nick = "usr" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

    try:
        sock = socket.create_connection((BEAST_SERVER, 6667), timeout=30)
        sock.settimeout(2)

        def send(msg):
            sock.sendall((msg + "\r\n").encode("utf-8", errors="replace"))

        send(f"NICK {nick}")
        send(f"USER {nick} 0 * :usr")

        buf = b""
        registered = joined = help_sent = False
        join_time = None
        query_idx = 0          # welche Suche gerade läuft
        search_sent_at = None
        next_search_at = None  # frühester Zeitpunkt für nächste Suche (Zufalls-Delay)
        last_result = None
        current_entry = None
        current_packs = []
        deadline = time.time() + 300 + len(queries) * 45

        while time.time() < deadline:
            try:
                data = sock.recv(4096)
                if not data:
                    break
                buf += data
            except socket.timeout:
                pass

            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                line = line.decode("utf-8", errors="replace")

                if line.startswith("PING"):
                    send(f"PONG {line.split(':', 1)[-1]}")
                    continue

                parts = line.split()
                if len(parts) < 2:
                    continue

                if parts[1] == "001" and not registered:
                    registered = True
                    send(f"JOIN {BEAST_CHANNEL}")
                    send(f"JOIN {BEAST_SEARCH_CHANNEL}")

                elif "JOIN" in line and nick.lower() in line.lower() and not joined:
                    joined = True
                    join_time = time.time()
                    log.info(f"  Im Channel. Starte Suchen...")

                elif search_sent_at and "NOTICE" in line:
                    clean = IRC_COLOR.sub("", line)
                    content = clean.split(":", 2)[-1].strip()
                    log.debug(f"  NOTICE: {content[:150]}")
                    m = PAT_PACK.search(content)
                    if m:
                        last_result = time.time()
                        current_packs.append({
                            "size":    parse_size(m.group(1)),
                            "fname":   m.group(2).strip(),
                            "gets":    m.group(3),
                            "bot":     m.group(4),
                            "pack":    m.group(5),
                            "server":  BEAST_SERVER,
                            "channel": BEAST_CHANNEL,
                        })

            # !help nach 30s
            if joined and not help_sent and join_time and time.time() - join_time > 2:
                send(f"PRIVMSG {BEAST_SEARCH_CHANNEL} :!help")
                help_sent = True

            # Nächste Suche starten wenn bereit
            if joined and join_time and time.time() - join_time > 5:
                # Laufende Suche abschließen wenn Ergebnisse da/timeout
                if search_sent_at:
                    no_result_timeout = time.time() - search_sent_at > 20 and not last_result
                    result_done = last_result and time.time() - last_result > 8
                    if no_result_timeout or result_done:
                        results[current_entry] = current_packs
                        log.info(f"  → {len(current_packs)} Treffer für '{current_entry}'")
                        current_packs = []
                        last_result = None
                        search_sent_at = None
                        query_idx += 1
                        delay = random.uniform(15, 40)
                        next_search_at = time.time() + delay
                        if query_idx < len(queries):
                            log.info(f"  Warte {delay:.0f}s vor nächster Suche...")

                # Nächste Suche abschicken
                if not search_sent_at and query_idx < len(queries):
                    if next_search_at is None or time.time() >= next_search_at:
                        current_entry, search_term = queries[query_idx]
                        log.info(f"  !search {search_term} ({query_idx+1}/{len(queries)})")
                        send(f"PRIVMSG {BEAST_SEARCH_CHANNEL} :!s {search_term}")
                        send(f"PRIVMSG {BEAST_BOT} :!s {search_term}")
                        search_sent_at = time.time()
                elif not search_sent_at and query_idx >= len(queries):
                    break  # Alle Suchen erledigt

        send("QUIT :bye")
        sock.close()

    except Exception as e:
        log.error(f"IRC-Suche fehlgeschlagen: {e}")

    return results


def search_beast_irc(query: str, lang: str = "German") -> list[dict]:
    """Einzelsuche – ruft intern search_beast_irc_multi auf."""
    search_term = f"{query} {lang}" if lang.lower() not in query.lower() else query
    res = search_beast_irc_multi([(query, search_term)])
    return res.get(query, [])


def score_pack(p: dict, category: str = "") -> float:
    name = p.get("fname", "").lower()
    score = 0.0
    for kw in ["german", "deutsch", ".ger.", "dl.", "ger-sub", "[ger"]:
        if kw in name:
            score += 200
    for kw in ["1080p", "bluray", "blu-ray", "webrip", "web-dl"]:
        if kw in name:
            score += 30
    if "720p" in name:
        score += 15
    for kw in ["x265", "hevc", "h265"]:
        if kw in name:
            score += 10
    score += math.log10(max(p.get("size", 1), 1)) * 5
    try:
        score += min(int(p.get("gets", 0)), 100)
    except (ValueError, TypeError):
        pass

    if category in ("serien", "anime"):
        # Episodenmuster stark bevorzugen
        if re.search(r"s\d{1,2}e\d{1,2}", name):
            score += 500
        elif re.search(r"\d{1,2}x\d{1,2}", name):
            score += 400
        elif re.search(r"s\d{1,2}\b", name):
            score += 200
        # Filmpacks stark bestrafen (kein Episodenmuster + typische Filmgröße)
        size_mb = p.get("size", 0) // 1024 // 1024
        has_episode = bool(re.search(r"s\d{1,2}e\d{1,2}|\d{1,2}x\d{1,2}", name))
        if not has_episode and size_mb > 2000:
            score -= 800

    elif category == "filme":
        # Episodenmuster bei Filmen bestrafen
        if re.search(r"s\d{1,2}e\d{1,2}", name):
            score -= 500

    return score


def clean_name(raw: str) -> str:
    """Wandelt 'Some.Series.S02E01.German.1080p.x264-Group' in 'Some Series' um."""
    name = raw
    # Release-Gruppe entfernen (z.B. -WeebPinn am Ende)
    name = re.sub(r"-[A-Za-z0-9]+$", "", name)
    # Dots/Underscores zu Spaces
    name = name.replace(".", " ").replace("_", " ")
    # Noise-Tags entfernen
    name = NOISE_TAGS.sub("", name)
    # SxxExx und alles danach entfernen
    name = re.split(r"\bS\d{1,2}E?\d*\b", name, flags=re.IGNORECASE)[0]
    # Mehrfache Spaces normalisieren
    name = re.sub(r"\s+", " ", name).strip()
    return name


def extract_season_episode(filename: str) -> tuple[int | None, int | None]:
    """Extrahiert Staffel- und Episodennummer aus einem Dateinamen."""
    m = re.search(r"S(\d{1,2})E(\d{1,2})", filename, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"S(\d{1,2})\b", filename, re.IGNORECASE)
    if m:
        return int(m.group(1)), None
    return None, None


def jellyfin_series_name(filename: str) -> str:
    """Gibt den bereinigten Seriennamen zurück."""
    return clean_name(Path(filename).stem)


def rename_for_jellyfin(path: Path, category: str) -> Path:
    """
    Benennt heruntergeladene Dateien/Ordner Jellyfin-konform um.

    Serien/Anime:
      Serie Name/Season XX/Serie Name - SXXEXX.mkv

    Filme:
      Film Name (Jahr)/Film Name (Jahr).mkv
    """
    if category in ("serien", "anime", "tvshows"):
        return _rename_series(path)
    else:
        return _rename_movie(path)


def _rename_series(path: Path) -> Path:
    """Organisiert Serien-Dateien in Jellyfin-Ordnerstruktur."""
    parent = path.parent

    # Wenn es ein Ordner ist: alle Videodateien darin umbenennen
    if path.is_dir():
        video_files = list(path.glob("**/*.mkv")) + list(path.glob("**/*.mp4")) + list(path.glob("**/*.avi"))
        if not video_files:
            return path

        # Serienname aus dem ersten Videodatei-Namen ableiten
        series_name = clean_name(video_files[0].stem)
        season_num, _ = extract_season_episode(video_files[0].name)
        season_label = f"Season {season_num:02d}" if season_num else "Season 01"

        series_dir = parent / series_name
        season_dir = series_dir / season_label
        season_dir.mkdir(parents=True, exist_ok=True)

        for vf in video_files:
            s, e = extract_season_episode(vf.name)
            if s and e:
                new_name = f"{series_name} - S{s:02d}E{e:02d}{vf.suffix}"
            else:
                new_name = vf.name
            dest = season_dir / new_name
            shutil.move(str(vf), str(dest))
            log.info(f"  Umbenannt: {vf.name} → {season_dir.name}/{new_name}")

        # Leeren Ursprungsordner entfernen
        try:
            shutil.rmtree(str(path))
        except Exception:
            pass

        return series_dir

    # Einzelne Videodatei
    elif path.suffix.lower() in (".mkv", ".mp4", ".avi"):
        series_name = clean_name(path.stem)
        s, e = extract_season_episode(path.name)
        season_label = f"Season {s:02d}" if s else "Season 01"

        season_dir = parent / series_name / season_label
        season_dir.mkdir(parents=True, exist_ok=True)

        if s and e:
            new_name = f"{series_name} - S{s:02d}E{e:02d}{path.suffix}"
        else:
            new_name = path.name
        dest = season_dir / new_name
        shutil.move(str(path), str(dest))
        log.info(f"  Umbenannt: {path.name} → {series_name}/{season_label}/{new_name}")
        return season_dir

    return path


def _rename_movie(path: Path) -> Path:
    """Organisiert Film-Dateien Jellyfin-konform."""
    parent = path.parent

    if path.is_dir():
        # RAR-Archiv entpacken falls vorhanden
        rar_parts = sorted(path.glob("*.rar")) or sorted(path.glob("*.r00"))
        if rar_parts:
            import subprocess
            rar_main = rar_parts[0]
            log.info(f"  RAR-Archiv erkannt: {rar_main.name} – entpacke...")
            result = subprocess.run(["7z", "x", str(rar_main), f"-o{path}", "-y"], capture_output=True)
            if result.returncode != 0:
                log.error(f"  RAR-Entpacken fehlgeschlagen: {result.stderr.decode()}")
            else:
                # RAR-Teile löschen
                for f in path.glob("*.r??"):
                    f.unlink(missing_ok=True)
                for f in path.glob("*.rar"):
                    f.unlink(missing_ok=True)
        all_files = [f for f in path.rglob("*") if f.is_file()]
        if not all_files:
            return path
        log.info(f"  Dateien: {[f.name for f in all_files]}")
        candidates = [f for f in all_files if "sample" not in f.name.lower()
                      and f.suffix.lower() in (".mkv", ".mp4", ".avi", ".m4v", ".ts")]
        if not candidates:
            candidates = [f for f in all_files if "sample" not in f.name.lower()]
        if not candidates:
            candidates = all_files
        vf = max(candidates, key=lambda f: f.stat().st_size)
        log.info(f"  Hauptfilm: {vf.name} ({vf.stat().st_size // 1024 // 1024}MB)")
        movie_name = clean_name(vf.stem)
        movie_dir = parent / movie_name
        movie_dir.mkdir(parents=True, exist_ok=True)
        dest = movie_dir / f"{movie_name}{vf.suffix}"
        shutil.move(str(vf), str(dest))
        shutil.rmtree(str(path), ignore_errors=True)
        log.info(f"  Film umbenannt: {vf.name} → {movie_name}/{dest.name}")
        return movie_dir

    elif path.suffix.lower() in (".mkv", ".mp4", ".avi"):
        movie_name = clean_name(path.stem)
        movie_dir = parent / movie_name
        movie_dir.mkdir(parents=True, exist_ok=True)
        dest = movie_dir / f"{movie_name}{path.suffix}"
        shutil.move(str(path), str(dest))
        log.info(f"  Film umbenannt: {path.name} → {movie_name}/{dest.name}")
        return movie_dir

    return path


def extract_archive(archive_path: Path, output_dir: Path) -> list[Path]:
    """Entpackt TAR/ZIP und gibt Liste der extrahierten Pfade zurück."""
    extracted = []
    log.info(f"  Entpacke: {archive_path.name}")

    try:
        if tarfile.is_tarfile(str(archive_path)):
            with tarfile.open(str(archive_path)) as tf:
                tf.extractall(path=str(output_dir))
                # Top-Level Einträge ermitteln
                top = {Path(m.name).parts[0] for m in tf.getmembers()}
                extracted = [output_dir / t for t in top]

        elif zipfile.is_zipfile(str(archive_path)):
            with zipfile.ZipFile(str(archive_path)) as zf:
                zf.extractall(path=str(output_dir))
                top = {Path(m).parts[0] for m in zf.namelist()}
                extracted = [output_dir / t for t in top]

        archive_path.unlink()
        log.info(f"  Archiv entpackt → {[p.name for p in extracted]}")

    except Exception as e:
        log.error(f"  Entpacken fehlgeschlagen: {e}")

    return extracted


def postprocess(downloaded_path: Path, category: str) -> list[Path]:
    """Entpackt Archive, benennt Jellyfin-konform um. Gibt erstellte Top-Level-Pfade zurück."""
    suffix = downloaded_path.suffix.lower()

    if suffix in (".tar", ".zip") or downloaded_path.name.endswith((".tar.gz", ".tgz")):
        extracted = extract_archive(downloaded_path, downloaded_path.parent)
    else:
        extracted = [downloaded_path]

    created = []
    for item in extracted:
        if item.exists():
            result = rename_for_jellyfin(item, category)
            if result and result.exists():
                # Top-Level-Ordner relativ zu staging ermitteln
                try:
                    top = downloaded_path.parent / result.relative_to(downloaded_path.parent).parts[0]
                except Exception:
                    top = result
                if top not in created:
                    created.append(top)
    return created


def remove_from_wishlist(entry: str):
    """Entfernt einen erledigten Eintrag aus der Wishlist."""
    try:
        lines = WISHLIST_PATH.read_text(encoding="utf-8").splitlines()
        new_lines = [l for l in lines if l.strip() != f"- {entry}"]
        WISHLIST_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        log.info(f"  Aus Wishlist entfernt: {entry}")
    except Exception as e:
        log.warning(f"  Wishlist-Entfernung fehlgeschlagen: {e}")


def _merge_move(src: Path, dst: Path):
    """Verschiebt src nach dst. Wenn dst-Ordner schon existiert, werden Inhalte rekursiv gemergt."""
    if not src.exists():
        return
    if not dst.exists():
        shutil.move(str(src), str(dst))
        return
    if src.is_dir() and dst.is_dir():
        for child in list(src.iterdir()):
            _merge_move(child, dst / child.name)
        shutil.rmtree(str(src), ignore_errors=True)
    else:
        shutil.move(str(src), str(dst))


def process_entry_with_packs(entry: str, category: str, packs: list[dict]) -> bool:
    final_dir = CATEGORY_DIRS.get(category, DOWNLOAD_BASE / category)
    staging = STAGING_DIR
    staging.mkdir(parents=True, exist_ok=True)
    log.info(f"[{category}] '{entry}'")

    if not packs:
        log.warning(f"  Keine Packs gefunden für: {entry}")
        return False

    scored = sorted(packs, key=lambda p: score_pack(p, category), reverse=True)
    log.info("  Top Ergebnisse:")
    for i, p in enumerate(scored[:5]):
        log.info(f"    [{i+1}] {p['fname']} | {p['size']//1024//1024}MB | {p['bot']} #{p['pack']}")

    for attempt, pack in enumerate(scored):
        log.info(f"  → Versuch {attempt+1}/{len(scored)}: {pack['fname']} von {pack['bot']}")

        success = xdcc_download(
            server=pack["server"],
            channel=pack["channel"],
            bot=pack["bot"],
            pack=pack["pack"],
            output_dir=staging,
            timeout=7200,
        )

        if success:
            downloaded = staging / pack["fname"]
            if downloaded.exists():
                created = postprocess(downloaded, category)
                final_dir.mkdir(parents=True, exist_ok=True)
                for top in created:
                    dest = final_dir / top.name
                    _merge_move(top, dest)
                    log.info(f"  → Verschoben: downloads/{top.name} → {final_dir.name}/")
            return True

        log.warning(f"  → Pack {attempt+1} fehlgeschlagen ({pack['bot']}), versuche nächsten...")

    log.error(f"  Alle {len(scored)} Packs fehlgeschlagen für: {entry}")
    return False


def process_entry(entry: str, category: str) -> bool:
    """Einzelner Eintrag mit eigener IRC-Session (für direkte Aufrufe)."""
    packs = search_beast_irc(entry, SEARCH_LANG)
    log.info(f"  → {len(packs)} Treffer in #beast-xdcc")
    return process_entry_with_packs(entry, category, packs)


def process_wishlist():
    if not WISHLIST_PATH.exists():
        log.warning(f"Wishlist nicht gefunden: {WISHLIST_PATH}")
        return

    wait_for_vpn()

    categories = parse_wishlist(WISHLIST_PATH)
    pending = [
        (cat, e)
        for cat, entries in categories.items()
        for e in entries
        if not already_done(e)
    ]
    log.info(f"Wishlist: {len(pending)} ausstehende Einträge")

    for category, entry in pending:
        wait_for_vpn()  # Vor jedem Download erneut prüfen
        success = process_entry(entry, category)
        if success:
            mark_done(entry)
            remove_from_wishlist(entry)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="Daemon-Modus (alle 30 Min)")
    parser.add_argument("--once",  action="store_true", help="Einmal laufen")
    args = parser.parse_args()

    if args.watch:
        log.info(f"Watch-Modus (Interval: {CHECK_INTERVAL}s)")
        while True:
            process_wishlist()
            log.info(f"Nächster Check in {CHECK_INTERVAL // 60} Minuten")
            time.sleep(CHECK_INTERVAL)
    else:
        process_wishlist()


if __name__ == "__main__":
    main()
