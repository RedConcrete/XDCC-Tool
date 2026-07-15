#!/usr/bin/env python3
"""
XDCC Downloader – Kern-Bibliothek für cli.py
Suche, Scoring, Postprocessing, Datei-Umbenennung.
"""

import os
import re
import shutil
import socket
import tarfile
import zipfile
import time
import math
import logging
import json
import urllib.request
import urllib.parse
import ssl
from pathlib import Path
from xdcc_client import xdcc_download, _rate_limit, make_nick  # noqa: F401

NOISE_TAGS = re.compile(
    r"\b(german|deutsch|english|dl|aac|aac2|ac3|dts|"
    r"1080p|720p|480p|2160p|4k|uhd|"
    r"bluray|blu-ray|webrip|web-dl|web|hdtv|dvdrip|bdrip|"
    r"h264|h265|x264|x265|hevc|xvid|"
    r"proper|repack|extended|theatrical|"
    r"multi|dubbed|subbed)\b",
    re.IGNORECASE,
)

log = logging.getLogger(__name__)

DOWNLOAD_BASE = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
STAGING_DIR   = DOWNLOAD_BASE / "downloads"

CATEGORY_DIRS = {
    "serien": DOWNLOAD_BASE / "serien",
    "anime":  DOWNLOAD_BASE / "serien",
    "filme":  DOWNLOAD_BASE / "movies",
    "film":   DOWNLOAD_BASE / "movies",
}

CHANNELS_CONFIG = Path(os.environ.get("CHANNELS_CONFIG", "/app/channels.json"))

SERVER_STATE_PATH = Path(os.environ.get("SERVER_STATE_FILE", "/app/server_state.json"))


def _load_server_state() -> dict:
    if SERVER_STATE_PATH.exists():
        try:
            return json.loads(SERVER_STATE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"server_state.json Ladefehler: {e}")
    return {}


def _save_server_state(state: dict) -> None:
    try:
        SERVER_STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning(f"server_state.json Schreibfehler: {e}")


def is_server_disabled(server: str) -> bool:
    return _load_server_state().get(server, {}).get("disabled", False)


def mark_server_disabled(server: str, reason: str) -> None:
    state = _load_server_state()
    state[server] = {
        "disabled": True,
        "reason": reason,
        "since": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_server_state(state)
    log.warning(f"Server {server} dauerhaft deaktiviert: {reason}")


def _load_channels() -> list[dict]:
    if CHANNELS_CONFIG.exists():
        try:
            data = json.loads(CHANNELS_CONFIG.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                for ch in data:
                    # Passwoerter kommen aus der Umgebung (.env), nie aus channels.json
                    if "sasl_pass_env" in ch and "sasl_pass" not in ch:
                        ch["sasl_pass"] = os.environ.get(ch["sasl_pass_env"], "")
                return data
        except Exception as e:
            log.warning(f"channels.json Ladefehler: {e}")
    return [{"server": "irc.abjects.net", "port": 6667,
             "channel": "#beast-xdcc", "search_channel": "#beast-chat",
             "search_bot": "databeast", "search_cmd": "!s {query}",
             "lang": "German"}]


CHANNELS = _load_channels()

_CHANNEL_CFG = {
    (ch["server"], ch["channel"].lower()): ch
    for ch in CHANNELS
}

NETWORK_SERVERS = {
    "abjects":       "irc.abjects.net",
    "abandoned-irc": "irc.abandoned-irc.net",
    "rizon":         "irc.rizon.net",
    "undernet":      "irc.undernet.org",
    "efnet":         "irc.efnet.org",
    "irchighway":    "irc.irchighway.net",
    "scenep2p":      "irc.scenep2p.net",
    "criten":        "irc.criten.net",
}

XDCC_EU_URL = "https://www.xdcc.eu/search.php"


def _extra_channels_for(server: str, channel: str) -> list:
    cfg = _CHANNEL_CFG.get((server, channel.lower()), {})
    return cfg.get("extra_channels", [])


def _server_for_network(network: str) -> str:
    key = network.lower().replace(" ", "-")
    for k, v in NETWORK_SERVERS.items():
        if k in key:
            return v
    return f"irc.{key}.net"


def parse_size(size_str: str) -> int:
    size_str = size_str.strip().upper()
    m = re.match(r"([\d.]+)([KMGT]?)", size_str)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2)
    return int(val * {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}.get(unit, 1))


def search_xdcc_eu(query: str) -> list[dict]:
    """Sucht auf xdcc.eu (Fallback für Netzwerke ohne IRC-Suchbot)."""
    params = urllib.parse.urlencode({"searchkey": query})
    try:
        req = urllib.request.Request(
            f"{XDCC_EU_URL}?{params}", headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.error(f"xdcc.eu Fehler: {e}")
        return []

    packs = []
    last_network = ""
    last_channel = ""

    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)]

        if len(cells) == 7:
            network, channel, bot, slot, gets, size, fname = cells
            last_network = network
            last_channel = channel
        elif len(cells) == 5 and last_network:
            # rowspan rows: network/channel cells omitted after first row of a group
            bot, slot, gets, size, fname = cells
            network, channel = last_network, last_channel
        else:
            continue

        slot = slot.lstrip("#").strip()
        if not slot.isdigit():
            continue
        packs.append({
            "size":    parse_size(size),
            "fname":   fname.strip(),
            "gets":    gets.replace("x", "").strip() or "0",
            "bot":     bot.strip(),
            "pack":    slot,
            "server":  _server_for_network(network),
            "channel": channel.strip(),
            "network": network.strip(),
            "source":  "xdcc.eu",
        })
    return packs


_MIRC_FORMAT_RE = re.compile(r"\x03(\d{1,2}(,\d{1,2})?)?|[\x02\x0f\x16\x1f]")


def _strip_mirc_formatting(s: str) -> str:
    """Entfernt mIRC-Farbcodes/Formatierung (z.B. von databeast-NOTICEs)."""
    return _MIRC_FORMAT_RE.sub("", s)


def _irc_bot_search(server: str, port: int, channel: str,
                    search_bot: str, search_cmd: str,
                    query: str, timeout: int = 30,
                    download_channel: str | None = None,
                    irc_callback=None, tls: bool = False) -> list[dict]:
    """Sucht direkt via IRC-Suchbot (z.B. databeast auf Abjects)."""
    results = []
    nick = make_nick()

    _rate_limit(server)
    try:
        sock = socket.create_connection((server, port), timeout=15)
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=server)
        sock.settimeout(3)
    except Exception as e:
        log.warning(f"IRC-Suche: Verbindung zu {server} fehlgeschlagen: {e}")
        return []

    _IRC_NOISE_RE = re.compile(
        r'^\S+\s+(?:37[256]|00[1-5]|25[0-9]|265|266|315|353|366|MODE)\s'
    )

    def send(msg: str):
        sock.sendall((msg + "\r\n").encode("utf-8", errors="replace"))
        if irc_callback and any(msg.startswith(p) for p in ("JOIN", "PRIVMSG", "QUIT")):
            irc_callback(f">> {msg}")

    try:
        send(f"NICK {nick}")
        send(f"USER {nick} 0 * :{nick}")

        buf = b""
        registered = False
        joined = False
        searching = False
        last_result = time.time()
        deadline = time.time() + timeout

        while time.time() < deadline:
            if searching and time.time() - last_result > 4:
                break

            try:
                data = sock.recv(4096)
                if not data:
                    break
                buf += data
            except socket.timeout:
                continue

            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                line = line.decode("utf-8", errors="replace")

                if line.startswith("PING"):
                    send(f"PONG {line.split(':', 1)[-1]}")
                    continue

                if irc_callback and not _IRC_NOISE_RE.search(line):
                    irc_callback(f"<< {line}")

                parts = line.split()
                if len(parts) < 2:
                    continue
                code = parts[1]

                if code == "001" and not registered:
                    registered = True
                    send(f"JOIN {channel}")

                elif "JOIN" in line and nick.lower() in line.lower() \
                        and channel.lower() in line.lower() and not joined:
                    joined = True
                    cmd = search_cmd.format(query=query)
                    time.sleep(1)
                    send(f"PRIVMSG {channel} :{cmd}")
                    searching = True
                    last_result = time.time()

                elif "NOTICE" in line and search_bot.lower() in line.lower():
                    notice = line.split(":", 2)[-1].strip()
                    notice = _strip_mirc_formatting(notice)

                    fname = bot = pack = size = gets = None

                    # databeast-Format: (4.0G) Name.mkv (1314x) /msg BOT xdcc send #1169
                    m = re.search(
                        r'\(([0-9.]+[KMGT]?B?)\)\s+(\S+)\s+\((\d+)x\)\s+/msg\s+(\S+)\s+xdcc send #(\d+)',
                        notice, re.IGNORECASE
                    )
                    if m:
                        size, fname, gets, bot, pack = m.groups()
                    else:
                        # BotReign-Format: 001)   5x | 185M | Name.mkv | /msg BOT XDCC SEND 110 | Used: ...
                        m = re.search(
                            r'^\d+\)\s*(\d+)x\s*\|\s*([0-9.]+[KMGT]?i?B?)\s*\|\s*(.+?)\s*\|\s*/msg\s+(\S+)\s+xdcc send\s+(\d+)',
                            notice, re.IGNORECASE
                        )
                        if m:
                            gets, size, fname, bot, pack = m.groups()
                        else:
                            # zombie-warez [o_0]-Format:
                            # 0x [928M] Name.tar -( Command: /MSG Zombie-Slayer XDCC SEND #125 )
                            m = re.search(
                                r'(\d+)x\s+\[([0-9.]+[KMGT]?i?B?)\]\s+(.+?)\s+-\(\s*Command:\s*/MSG\s+(\S+)\s+XDCC\s+SEND\s*#(\d+)',
                                notice, re.IGNORECASE
                            )
                            if m:
                                gets, size, fname, bot, pack = m.groups()

                    if fname:
                        results.append({
                            "fname":   fname,
                            "bot":     bot,
                            "pack":    pack,
                            "size":    parse_size(size),
                            "server":  server,
                            "channel": download_channel or channel,
                            "network": server.split(".")[1].capitalize(),
                            "gets":    gets,
                            "source":  f"IRC ({search_bot})",
                        })
                        last_result = time.time()

    except Exception as e:
        log.warning(f"IRC-Suche Fehler ({server}): {e}")
    finally:
        try:
            send("QUIT :bye")
            sock.close()
        except Exception:
            pass

    return results


def search_packs(query: str, irc_callback=None) -> list[dict]:
    """IRC-Botsuche (primär) + xdcc.eu für Netzwerke ohne Suchbot."""
    results: list[dict] = []
    # Channel-genaues Tracking: BotReign findet #moviegods-Packs → blockiert nur
    # xdcc.eu für #moviegods, nicht für #beast-xdcc (beide auf irc.abjects.net)
    irc_channels: set[tuple] = set()

    disabled_servers = {s for s, v in _load_server_state().items() if v.get("disabled")}

    for ch in CHANNELS:
        if ch["server"] in disabled_servers:
            continue
        if "search_bot" in ch:
            found = _irc_bot_search(
                server=ch["server"],
                port=ch.get("port", 6667),
                channel=ch["search_channel"],
                search_bot=ch["search_bot"],
                search_cmd=ch.get("search_cmd", "xdcc search {query}"),
                query=query,
                download_channel=ch["channel"],
                irc_callback=irc_callback,
                tls=ch.get("tls", False),
            )
            if found:
                results.extend(found)
                for p in found:
                    p.setdefault("port", ch.get("port", 6667))
                    p.setdefault("tls", ch.get("tls", False))
                    p.setdefault("sasl_user", ch.get("sasl_user", ""))
                    p.setdefault("sasl_pass", ch.get("sasl_pass", ""))
                irc_channels.add((ch["server"], ch["channel"].lower()))

    for p in search_xdcc_eu(query):
        if p["server"] in disabled_servers:
            continue
        key = (p["server"], p.get("channel", "").lower())
        if key not in irc_channels:
            cfg = _CHANNEL_CFG.get(key, {})
            p.setdefault("port", cfg.get("port", 6667))
            p.setdefault("tls", cfg.get("tls", False))
            p.setdefault("sasl_user", cfg.get("sasl_user", ""))
            p.setdefault("sasl_pass", cfg.get("sasl_pass", ""))
            results.append(p)

    return results


def score_pack(p: dict, category: str = "") -> float:
    name = p.get("fname", "").lower()
    score = 0.0

    # Live-IRC-Suche bevorzugen: verhindert dass stale xdcc.eu-Kandidaten
    # alle MAX_CANDIDATES-Slots verbrauchen bevor echte IRC-Packs dran kommen
    if str(p.get("source", "")).startswith("IRC"):
        score += 300

    german_kw = ["german", "deutsch", ".ger.", "ger.", "dl.german", "german.dl",
                 "ger-sub", "[ger", ".de.", "multi.german", "german.multi"]
    is_german = any(kw in name for kw in german_kw)
    if is_german:
        score += 1000

    if not is_german:
        foreign_kw = ["vostfr", "french", "truefrench", ".fr.", "italian", "italiano",
                      "spanish", "espanol", ".es.", "portuguese", "russian", "polish",
                      ".pl.", ".it.", ".ru.", "dubbed"]
        for kw in foreign_kw:
            if kw in name:
                score -= 600
                break

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
        if re.search(r"s\d{1,2}e\d{1,2}", name):
            score += 500
        elif re.search(r"\d{1,2}x\d{1,2}", name):
            score += 400
        elif re.search(r"s\d{1,2}\b", name):
            score += 200
        size_mb = p.get("size", 0) // 1024 // 1024
        if not re.search(r"s\d{1,2}e\d{1,2}|\d{1,2}x\d{1,2}", name) and size_mb > 2000:
            # Season-Packs (z.B. S07.German.tar) nur leicht bestrafen,
            # komplette Serien-Dumps ohne Staffelangabe stark
            if re.search(r"\bs\d{1,2}\b", name):
                score -= 200
            else:
                score -= 800

    elif category in ("filme", "film"):
        if re.search(r"s\d{1,2}e\d{1,2}", name):
            score -= 500

    return score


def clean_name(raw: str) -> str:
    name = raw
    name = re.sub(r"-[A-Za-z0-9]+$", "", name)          # trailing group entfernen
    name = name.replace(".", " ").replace("_", " ")
    name = NOISE_TAGS.sub("", name)
    name = re.split(r"\bS\d{1,2}E?\d*\b", name, flags=re.IGNORECASE)[0]
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^[a-zA-Z0-9]{2,6}-(?=[a-zA-Z0-9])", "", name).strip()  # leading group
    return name


def extract_season_episode(filename: str) -> tuple[int | None, int | None]:
    m = re.search(r"S(\d{1,2})E(\d{1,2})", filename, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"S(\d{1,2})\b", filename, re.IGNORECASE)
    if m:
        return int(m.group(1)), None
    return None, None


def rename_for_jellyfin(path: Path, category: str) -> Path:
    if category in ("serien", "anime", "tvshows"):
        return _rename_series(path)
    return _rename_movie(path)


def _rename_series(path: Path) -> Path:
    parent = path.parent

    if path.is_dir():
        all_videos = list(path.glob("**/*.mkv")) + list(path.glob("**/*.mp4")) + list(path.glob("**/*.avi"))
        video_files = [f for f in all_videos if "sample" not in f.name.lower()] or all_videos
        if not video_files:
            return path

        main_file = max(video_files, key=lambda f: f.stat().st_size)
        series_name = clean_name(main_file.stem)
        season_num, _ = extract_season_episode(main_file.name)
        season_label = f"Season {season_num:02d}" if season_num else "Season 01"

        season_dir = parent / series_name / season_label
        season_dir.mkdir(parents=True, exist_ok=True)

        for vf in video_files:
            s, e = extract_season_episode(vf.name)
            new_name = f"{series_name} - S{s:02d}E{e:02d}{vf.suffix}" if s and e else vf.name
            shutil.move(str(vf), str(season_dir / new_name))

        try:
            shutil.rmtree(str(path))
        except Exception:
            pass
        return parent / series_name

    elif path.suffix.lower() in (".mkv", ".mp4", ".avi"):
        series_name = clean_name(path.stem)
        s, e = extract_season_episode(path.name)
        season_label = f"Season {s:02d}" if s else "Season 01"
        season_dir = parent / series_name / season_label
        season_dir.mkdir(parents=True, exist_ok=True)
        new_name = f"{series_name} - S{s:02d}E{e:02d}{path.suffix}" if s and e else path.name
        shutil.move(str(path), str(season_dir / new_name))
        return parent / series_name

    return path


def _rename_movie(path: Path) -> Path:
    parent = path.parent

    if path.is_dir():
        rar_parts = sorted(path.glob("*.rar")) or sorted(path.glob("*.r00"))
        if rar_parts:
            import subprocess
            result = subprocess.run(
                ["7z", "x", str(rar_parts[0]), f"-o{path}", "-y"], capture_output=True
            )
            if result.returncode == 0:
                for f in list(path.glob("*.r??")) + list(path.glob("*.rar")):
                    f.unlink(missing_ok=True)

        all_files = [f for f in path.rglob("*") if f.is_file()]
        if not all_files:
            return path

        candidates = [f for f in all_files
                      if "sample" not in f.name.lower()
                      and f.suffix.lower() in (".mkv", ".mp4", ".avi", ".m4v", ".ts")]
        if not candidates:
            candidates = [f for f in all_files if "sample" not in f.name.lower()]
        if not candidates:
            candidates = all_files

        vf = max(candidates, key=lambda f: f.stat().st_size)
        movie_name = clean_name(vf.stem)
        movie_dir = parent / movie_name
        movie_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(vf), str(movie_dir / f"{movie_name}{vf.suffix}"))
        shutil.rmtree(str(path), ignore_errors=True)
        return movie_dir

    elif path.suffix.lower() in (".mkv", ".mp4", ".avi"):
        movie_name = clean_name(path.stem)
        movie_dir = parent / movie_name
        movie_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(movie_dir / f"{movie_name}{path.suffix}"))
        return movie_dir

    return path


def extract_archive(archive_path: Path, output_dir: Path) -> list[Path]:
    """Entpackt TAR/ZIP, gibt Top-Level-Pfade zurück."""
    extracted = []
    try:
        if tarfile.is_tarfile(str(archive_path)):
            with tarfile.open(str(archive_path)) as tf:
                tf.extractall(path=str(output_dir))
                top = {Path(m.name).parts[0] for m in tf.getmembers() if Path(m.name).parts}
                extracted = [output_dir / t for t in top if t != "."]
        elif zipfile.is_zipfile(str(archive_path)):
            with zipfile.ZipFile(str(archive_path)) as zf:
                zf.extractall(path=str(output_dir))
                top = {Path(m).parts[0] for m in zf.namelist() if Path(m).parts}
                extracted = [output_dir / t for t in top if t != "."]
    except Exception as e:
        log.error(f"Entpacken fehlgeschlagen: {e}")
        raise
    return extracted


def postprocess(downloaded_path: Path, category: str) -> list[Path]:
    """Entpackt Archive und benennt Jellyfin-konform um."""
    suffix = downloaded_path.suffix.lower()

    if suffix in (".tar", ".zip") or downloaded_path.name.endswith((".tar.gz", ".tgz")):
        extracted = extract_archive(downloaded_path, downloaded_path.parent)
    else:
        extracted = [downloaded_path]

    created = []
    for item in extracted:
        if "sample" in item.name.lower() or not item.exists():
            continue
        result = rename_for_jellyfin(item, category)
        if result and result.exists():
            try:
                top = downloaded_path.parent / result.relative_to(downloaded_path.parent).parts[0]
            except Exception:
                top = result
            if top not in created:
                created.append(top)
    return created


def _merge_move(src: Path, dst: Path):
    """Verschiebt src nach dst, mergt Ordner rekursiv."""
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
