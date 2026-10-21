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
import threading
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

TARGETS_ROOT = Path(os.environ.get("TARGETS_ROOT", "/targets"))
TARGET_STATE_PATH = Path(os.environ.get("TARGET_STATE_FILE", "/app/download_target.json"))
DEFAULT_TARGET = "jellyfin2"


def list_available_targets() -> dict[str, str]:
    """Jeder Unterordner direkt unter TARGETS_ROOT gilt als eigene
    Zielfestplatte (ein eigenes Docker-Volume, siehe docker-compose.yml).
    Neue Platte hinzufuegen = dort einen weiteren Mount eintragen -
    hier im Code muss dafuer nichts mehr angepasst werden, die Auswahl
    im Web-UI erkennt es automatisch."""
    targets = {}
    if TARGETS_ROOT.is_dir():
        for entry in sorted(TARGETS_ROOT.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                targets[entry.name] = entry.name
    return targets


def get_current_target() -> str:
    available = list_available_targets()
    if TARGET_STATE_PATH.exists():
        try:
            key = json.loads(TARGET_STATE_PATH.read_text(encoding="utf-8")).get("target")
            if key in available:
                return key
        except Exception as e:
            log.warning(f"download_target.json Ladefehler: {e}")
    if DEFAULT_TARGET in available:
        return DEFAULT_TARGET
    return next(iter(available), DEFAULT_TARGET)


def set_current_target(key: str) -> None:
    if key not in list_available_targets():
        raise ValueError(f"unbekanntes Ziel: {key}")
    TARGET_STATE_PATH.write_text(json.dumps({"target": key}), encoding="utf-8")


def download_base() -> Path:
    return TARGETS_ROOT / get_current_target()


def staging_dir() -> Path:
    return download_base() / "downloads"


def category_dirs() -> dict[str, Path]:
    base = download_base()
    return {
        "serien": base / "serien",
        "film":   base / "movies",
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
                    irc_callback=None, tls: bool = False,
                    stop_event=None) -> list[dict]:
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
            if stop_event and stop_event.is_set():
                break
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


def search_packs(query: str, irc_callback=None, stop_event=None) -> list[dict]:
    """IRC-Botsuche (primär) + xdcc.eu für Netzwerke ohne Suchbot."""
    results: list[dict] = []
    # Channel-genaues Tracking: BotReign findet #moviegods-Packs → blockiert nur
    # xdcc.eu für #moviegods, nicht für #beast-xdcc (beide auf irc.abjects.net)
    irc_channels: set[tuple] = set()

    disabled_servers = {s for s, v in _load_server_state().items() if v.get("disabled")}

    for ch in CHANNELS:
        if stop_event and stop_event.is_set():
            return results
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
                stop_event=stop_event,
            )
            if found:
                results.extend(found)
                for p in found:
                    p.setdefault("port", ch.get("port", 6667))
                    p.setdefault("tls", ch.get("tls", False))
                    p.setdefault("sasl_user", ch.get("sasl_user", ""))
                    p.setdefault("sasl_pass", ch.get("sasl_pass", ""))
                irc_channels.add((ch["server"], ch["channel"].lower()))

    if stop_event and stop_event.is_set():
        return results

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


# Punkte fuer score_pack(). Im Web-UI unter Menue -> Bewertung anpassbar,
# Abweichungen von den Standardwerten liegen in score_config.json.
SCORE_FIELDS = [
    # (key, label, hinweis, standard)
    ("german", "Deutsch", "german, deutsch, .ger., DL ...", 1000),
    ("foreign", "Andere Sprache", "french, italian, ... (nur wenn nicht deutsch)", -600),
    ("irc_live", "Live-IRC-Treffer", "statt xdcc.eu-Cache", 300),
    ("hd1080", "1080p / BluRay / WEB", "je Stichwort", 30),
    ("hd720", "720p", "", 15),
    ("x265", "x265 / HEVC", "je Stichwort", 10),
    ("gets_max", "Gets (max.)", "+1 pro Get bis zu diesem Wert", 100),
    ("season_pack", "Serie: ganze Staffel", "S01 ohne Folge, Staffel 1, Season 1", 700),
    ("episode", "Serie: einzelne Folge", "S01E05 oder 1x05", 500),
    ("complete_series", "Serie: Komplettpaket ohne Staffel", "große Datei ohne Staffel-/Folgenangabe", -800),
    ("film_episode", "Film: sieht aus wie Serienfolge", "S01E05 im Namen", -500),
    ("channel_fail", "Channel: Download fehlgeschlagen", "je Fehlversuch, baut sich mit der Zeit ab", -200),
    ("channel_ban", "Channel: gesperrt (Ban)", "z.B. 474 Cannot join channel (+b)", -1000),
    ("channel_decay_days", "Channel-Abzug: Halbwertszeit (Tage)", "danach zählt ein Fehler nur noch halb", 7),
]
SCORE_DEFAULTS = {key: default for key, _, _, default in SCORE_FIELDS}
SCORE_CONFIG_PATH = Path(os.environ.get("SCORE_CONFIG", "/app/score_config.json"))
_score_cache = {"mtime": None, "values": dict(SCORE_DEFAULTS)}


def load_score_config() -> dict:
    """Standardwerte + Overrides aus score_config.json (gecacht per mtime)."""
    try:
        mtime = SCORE_CONFIG_PATH.stat().st_mtime
    except OSError:
        return dict(SCORE_DEFAULTS)
    if _score_cache["mtime"] != mtime:
        values = dict(SCORE_DEFAULTS)
        try:
            raw = json.loads(SCORE_CONFIG_PATH.read_text(encoding="utf-8") or "{}")
            values.update({k: int(v) for k, v in raw.items() if k in SCORE_DEFAULTS})
        except Exception as e:
            log.warning(f"score_config.json Ladefehler: {e}")
        _score_cache.update(mtime=mtime, values=values)
    return dict(_score_cache["values"])


def save_score_config(values: dict) -> None:
    """Speichert nur Abweichungen vom Standard (leeres Dict = alles Standard)."""
    overrides = {k: int(v) for k, v in values.items()
                 if k in SCORE_DEFAULTS and int(v) != SCORE_DEFAULTS[k]}
    SCORE_CONFIG_PATH.write_text(json.dumps(overrides, indent=2) + "\n", encoding="utf-8")


# Fehlversuche pro Channel (server + channel), damit nicht funktionierende
# Channels/Bots mit der Zeit nach hinten rutschen. Werte bauen sich mit
# einer Halbwertszeit ab, ein erfolgreicher Download setzt sie zurueck.
CHANNEL_STATS_PATH = Path(os.environ.get("CHANNEL_STATS", "/app/channel_stats.json"))
_channel_stats_lock = threading.Lock()


def _channel_key(server: str, channel: str) -> str:
    return f"{server.lower()} {channel.lower()}"


def _load_channel_stats() -> dict:
    try:
        return json.loads(CHANNEL_STATS_PATH.read_text(encoding="utf-8") or "{}")
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning(f"channel_stats.json Ladefehler: {e}")
        return {}


def _decayed(entry: dict, half_life_days: float, now: float | None = None) -> tuple[float, float]:
    """(fails, bans) eines Eintrags, abgebaut bis jetzt."""
    now = now or time.time()
    age_days = max(0.0, now - entry.get("updated", now)) / 86400
    factor = 0.5 ** (age_days / max(half_life_days, 0.1))
    return entry.get("fails", 0) * factor, entry.get("bans", 0) * factor


def record_channel_result(server: str, channel: str, ok: bool, banned: bool = False,
                          reason: str = "") -> None:
    """Ergebnis eines Download-Versuchs fuer den Channel festhalten."""
    if not server or not channel:
        return
    key = _channel_key(server, channel)
    with _channel_stats_lock:
        stats = _load_channel_stats()
        if ok:
            if stats.pop(key, None) is None:
                return
        else:
            fails, bans = _decayed(stats.get(key, {}), load_score_config()["channel_decay_days"])
            if banned:
                bans += 1
            else:
                fails += 1
            stats[key] = {
                "fails": round(fails, 3),
                "bans": round(bans, 3),
                "updated": time.time(),
                "last_reason": reason[:200],
            }
        try:
            CHANNEL_STATS_PATH.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n",
                                          encoding="utf-8")
        except Exception as e:
            log.warning(f"channel_stats.json Schreibfehler: {e}")


def channel_penalties(cfg: dict | None = None) -> dict[str, dict]:
    """Aktueller (abgebauter) Abzug je Channel-Key."""
    cfg = cfg or load_score_config()
    result = {}
    for key, entry in _load_channel_stats().items():
        fails, bans = _decayed(entry, cfg["channel_decay_days"])
        result[key] = {
            "fails": fails,
            "bans": bans,
            "penalty": fails * cfg["channel_fail"] + bans * cfg["channel_ban"],
            "updated": entry.get("updated"),
            "last_reason": entry.get("last_reason", ""),
        }
    return result


def reset_channel_stats(key: str | None = None) -> None:
    """Einen Channel (key) oder alle (None) zuruecksetzen."""
    with _channel_stats_lock:
        stats = _load_channel_stats() if key else {}
        stats.pop(key, None)
        CHANNEL_STATS_PATH.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n",
                                      encoding="utf-8")


_EPISODE_RE = re.compile(r"s\d{1,2}e\d{1,3}|\b\d{1,2}x\d{1,3}\b")
_SEASON_RE = re.compile(r"\bs\d{1,2}\b|\b(?:staffel|season)[ ._-]?\d{1,2}\b")


def score_pack(p: dict, category: str = "", cfg: dict | None = None,
               penalties: dict | None = None) -> float:
    cfg = cfg or load_score_config()
    penalties = channel_penalties(cfg) if penalties is None else penalties
    name = p.get("fname", "").lower()
    score = 0.0

    score += penalties.get(_channel_key(p.get("server", ""), p.get("channel", "")), {}).get("penalty", 0)

    # Live-IRC-Suche bevorzugen: verhindert dass stale xdcc.eu-Kandidaten
    # alle MAX_CANDIDATES-Slots verbrauchen bevor echte IRC-Packs dran kommen
    if str(p.get("source", "")).startswith("IRC"):
        score += cfg["irc_live"]

    german_kw = ["german", "deutsch", ".ger.", "ger.", "dl.german", "german.dl",
                 "ger-sub", "[ger", ".de.", "multi.german", "german.multi"]
    is_german = any(kw in name for kw in german_kw)
    if is_german:
        score += cfg["german"]

    if not is_german:
        foreign_kw = ["vostfr", "french", "truefrench", ".fr.", "italian", "italiano",
                      "spanish", "espanol", ".es.", "portuguese", "russian", "polish",
                      ".pl.", ".it.", ".ru.", "dubbed"]
        for kw in foreign_kw:
            if kw in name:
                score += cfg["foreign"]
                break

    for kw in ["1080p", "bluray", "blu-ray", "webrip", "web-dl"]:
        if kw in name:
            score += cfg["hd1080"]
    if "720p" in name:
        score += cfg["hd720"]
    for kw in ["x265", "hevc", "h265"]:
        if kw in name:
            score += cfg["x265"]
    score += math.log10(max(p.get("size", 1), 1)) * 5
    try:
        score += min(int(p.get("gets", 0)), cfg["gets_max"])
    except (ValueError, TypeError):
        pass

    is_episode = bool(_EPISODE_RE.search(name))
    if category == "serien":
        if is_episode:
            score += cfg["episode"]
        elif _SEASON_RE.search(name):
            score += cfg["season_pack"]
        elif p.get("size", 0) // 1024 // 1024 > 2000:
            # Komplette Serien-Dumps ohne Staffelangabe
            score += cfg["complete_series"]

    elif category == "film":
        if is_episode:
            score += cfg["film_episode"]

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
    if category == "serien":
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
