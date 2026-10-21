#!/usr/bin/env python3
"""Lokale Web-UI fuer den XDCC-Downloader (LAN-only, Port 5005)."""

import importlib
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request
import requests

import cli
import downloader

CHAT_LOG_PATH = Path(os.environ.get("CHAT_LOG", "/app/Chat.txt"))
LOGS_DIR = Path(os.environ.get("LOGS_DIR", "/app/logs"))
ENV_FILE_PATH = Path(os.environ.get("ENV_FILE", "/app/.env"))
GLUETUN_CONTROL_URL = os.environ.get("GLUETUN_CONTROL_URL", "http://gluetun:8000")
GLUETUN_API_KEY = os.environ.get("GLUETUN_API_KEY", "")
TRIGGER_DIR = Path(os.environ.get("RESTART_TRIGGER_DIR", "/app/restart_trigger"))
TRIGGER_FILE = TRIGGER_DIR / "requested"
RESTART_LOG_FILE = TRIGGER_DIR / "last_result.log"

app = Flask(__name__)

_lock = threading.Lock()
_stop_event = threading.Event()
_state = {
    "running": False,
    "log": [],          # Liste von {"ts","title","msg","level"}
    "irc_log": [],      # Liste von {"ts","line"} – roh IRC-Traffic
    "summary": None,     # Ergebnis von run_once() nach Abschluss
    "progress": None,    # {"title","received","total"} waehrend Download
    "run_log_path": None,  # Pfad zur Log-Datei des aktuellen Laufs
}
# Manuelle Suche. Die Packs (inkl. SASL-Daten) bleiben serverseitig,
# die UI bekommt nur eine bereinigte Liste und waehlt per Index.
_search = {
    "running": False,
    "query": "",
    "category": "film",
    "results": [],
    "error": None,
}

_LOG_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.txt$")


def _write_to_file(path: Path, text: str) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def _status_cb(title: str, msg: str, level: str = "info") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        _state["log"].append({"ts": time.time(), "title": title, "msg": msg, "level": level})
        if level in ("success", "error") or "fertig" in msg or "fehlgeschlagen" in msg:
            _state["progress"] = None
        run_log = _state["run_log_path"]
    if run_log:
        _write_to_file(run_log, f"[{ts}] [{level.upper()}] [{title}] {msg}\n")


def _progress_cb(title: str, received: int, total: int) -> None:
    with _lock:
        _state["progress"] = {"title": title, "received": received, "total": total}


def _irc_cb(line: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    stamped = f"[{ts}] {line}"
    with _lock:
        _state["irc_log"].append({"ts": time.time(), "line": stamped})
        run_log = _state["run_log_path"]
    _write_to_file(CHAT_LOG_PATH, stamped + "\n")
    if run_log:
        _write_to_file(run_log, stamped + "\n")


def _run_job(job) -> None:
    try:
        summary = job()
    except Exception as e:
        summary = {"error": str(e)}
        _status_cb("Allgemein", f"Lauf abgebrochen: {e}", "error")

    with _lock:
        _state["running"] = False
        _state["summary"] = summary
        _state["progress"] = None
        run_log = _state["run_log_path"]
        _state["run_log_path"] = None

    if run_log:
        ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if summary and not summary.get("error"):
            footer = (
                f"\n{'=' * 60}\n"
                f"=== Lauf beendet: {ts_now} | "
                f"ok: {summary.get('ok', 0)}, fehlgeschlagen: {summary.get('failed', 0)} ===\n"
                f"{'=' * 60}\n"
            )
        else:
            err = (summary or {}).get("error", "unbekannt")
            footer = (
                f"\n{'=' * 60}\n"
                f"=== Lauf abgebrochen: {ts_now} | {err} ===\n"
                f"{'=' * 60}\n"
            )
        _write_to_file(run_log, footer)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/wishlist", methods=["GET"])
def get_wishlist():
    return jsonify(cli.load_wishlist())


@app.route("/api/wishlist", methods=["POST"])
def post_wishlist():
    data = request.get_json(force=True) or {}
    cleaned = {}
    for key in cli.SECTION_ORDER:
        items = data.get(key, [])
        cleaned[key] = [str(t).strip() for t in items if str(t).strip()]
    cli.save_wishlist(cleaned)
    return jsonify({"ok": True})


@app.route("/api/target", methods=["GET"])
def get_target():
    targets = []
    for key, label in downloader.list_available_targets().items():
        free_bytes = None
        try:
            free_bytes = shutil.disk_usage(downloader.TARGETS_ROOT / key).free
        except Exception:
            pass
        targets.append({"key": key, "label": label, "free_bytes": free_bytes})
    return jsonify({"current": downloader.get_current_target(), "targets": targets})


@app.route("/api/target", methods=["POST"])
def set_target():
    data = request.get_json(force=True) or {}
    key = str(data.get("target", "")).strip()
    try:
        downloader.set_current_target(key)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


def _start_job(job, label: str):
    """Startet einen Lauf (Wishlist oder manueller Download) im Hintergrund."""
    with _lock:
        if _state["running"]:
            return jsonify({"ok": False, "error": "Lauf bereits aktiv"}), 409
        if _search["running"]:
            return jsonify({"ok": False, "error": "Suche läuft noch"}), 409
        # Reload cli module so code changes on disk take effect without container restart
        importlib.reload(cli)
        now = datetime.now()
        run_filename = now.strftime("%Y-%m-%d_%H-%M-%S") + ".txt"
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        run_log_path = LOGS_DIR / run_filename
        _stop_event.clear()
        _state["running"] = True
        _state["log"] = []
        _state["irc_log"] = []
        _state["summary"] = None
        _state["progress"] = None
        _state["run_log_path"] = run_log_path

    ts_str = now.strftime("%Y-%m-%d %H:%M:%S")
    header = f"\n{'=' * 60}\n=== {label} gestartet: {ts_str} ===\n{'=' * 60}\n"
    _write_to_file(CHAT_LOG_PATH, header)
    _write_to_file(run_log_path, header)

    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/run", methods=["POST"])
def run():
    return _start_job(
        lambda: cli.run_once(_status_cb, _progress_cb, _irc_cb, stop_event=_stop_event),
        "Lauf",
    )


def _search_job(query: str, category: str) -> None:
    results, error = [], None
    try:
        results = cli.find_candidates(cli._search_query(query), category,
                                      irc_callback=_irc_cb, stop_event=_stop_event)
    except Exception as e:
        error = str(e)
    with _lock:
        _search["running"] = False
        _search["results"] = results
        _search["error"] = error


@app.route("/api/search", methods=["POST"])
def start_search():
    data = request.get_json(force=True) or {}
    query = str(data.get("query", "")).strip()
    category = str(data.get("category", "film")).strip()
    if not query:
        return jsonify({"ok": False, "error": "Suchbegriff fehlt"}), 400
    if category not in cli.ACTIVE_CATEGORIES:
        return jsonify({"ok": False, "error": "unbekannte Kategorie"}), 400
    with _lock:
        if _state["running"]:
            return jsonify({"ok": False, "error": "Lauf aktiv – Suche danach starten"}), 409
        if _search["running"]:
            return jsonify({"ok": False, "error": "Suche läuft bereits"}), 409
        importlib.reload(cli)
        _stop_event.clear()
        _search.update(running=True, query=query, category=category, results=[], error=None)
    threading.Thread(target=_search_job, args=(query, category), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/search")
def get_search():
    with _lock:
        results = [
            {
                "index": i,
                "fname": p.get("fname", ""),
                "title": cli.wishlist_title(p.get("fname", "")),
                "size": p.get("size", 0),
                "bot": p.get("bot", ""),
                "pack": p.get("pack", ""),
                "network": p.get("network", ""),
                "channel": p.get("channel", ""),
                "gets": p.get("gets", ""),
                "source": p.get("source", ""),
                "score": round(cli.score_pack(p, _search["category"])),
            }
            for i, p in enumerate(_search["results"])
        ]
        return jsonify({
            "running": _search["running"],
            "query": _search["query"],
            "category": _search["category"],
            "error": _search["error"],
            "results": results,
        })


@app.route("/api/search/download", methods=["POST"])
def download_search_result():
    data = request.get_json(force=True) or {}
    index = data.get("index")
    with _lock:
        results = _search["results"]
        category = _search["category"]
        if not isinstance(index, int) or not 0 <= index < len(results):
            return jsonify({"ok": False, "error": "ungültiger Treffer"}), 400
        pack = results[index]
    return _start_job(
        lambda: cli.download_single(pack, category, _status_cb, _progress_cb, _irc_cb,
                                            stop_event=_stop_event),
        "Manueller Download",
    )


@app.route("/api/score")
def get_score_config():
    values = downloader.load_score_config()
    return jsonify({
        "fields": [
            {"key": k, "label": label, "hint": hint, "default": default, "value": values[k]}
            for k, label, hint, default in downloader.SCORE_FIELDS
        ],
    })


@app.route("/api/score", methods=["POST"])
def save_score_config():
    data = request.get_json(force=True) or {}
    if data.get("reset"):
        values = dict(downloader.SCORE_DEFAULTS)
    else:
        raw = data.get("values", {})
        try:
            values = {k: int(raw[k]) for k in downloader.SCORE_DEFAULTS if k in raw}
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Nur ganze Zahlen erlaubt"}), 400
        if any(abs(v) > 100000 for v in values.values()):
            return jsonify({"ok": False, "error": "Werte zwischen -100000 und 100000"}), 400
        values = {**downloader.load_score_config(), **values}
    try:
        downloader.save_score_config(values)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Vorhandene Suchtreffer mit den neuen Punkten neu sortieren
    with _lock:
        if not _search["running"] and _search["results"]:
            _search["results"] = cli.rank_candidates(
                _search["results"], cli._search_query(_search["query"]), _search["category"])
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def stop():
    with _lock:
        if not (_state["running"] or _search["running"]):
            return jsonify({"ok": False, "error": "Kein Lauf aktiv"}), 409
        _stop_event.set()
    return jsonify({"ok": True})


@app.route("/api/status")
def status():
    since = request.args.get("since", default=0, type=int)
    with _lock:
        log_slice = _state["log"][since:]
        return jsonify({
            "running": _state["running"],
            "stopping": _state["running"] and _stop_event.is_set(),
            "log": log_slice,
            "next": len(_state["log"]),
            "summary": _state["summary"],
            "progress": _state["progress"],
        })


@app.route("/api/downloaded")
def downloaded():
    if not cli.DONE_LOG_PATH.exists():
        return jsonify([])
    lines = [l.strip() for l in cli.DONE_LOG_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    return jsonify(list(reversed(lines)))


@app.route("/api/downloaded", methods=["DELETE"])
def delete_downloaded():
    data = request.get_json(force=True) or {}
    title = str(data.get("title", "")).strip()
    if not title:
        return jsonify({"ok": False, "error": "title fehlt"}), 400
    removed = cli.remove_done(title)
    return jsonify({"ok": removed})


@app.route("/api/irc")
def irc_log_endpoint():
    since = request.args.get("since", default=0, type=int)
    with _lock:
        log_slice = _state["irc_log"][since:]
        return jsonify({
            "lines": log_slice,
            "next": len(_state["irc_log"]),
        })


@app.route("/api/logs")
def chat_logs():
    tail = request.args.get("tail", default=0, type=int)
    since_byte = request.args.get("since_byte", default=-1, type=int)
    if not CHAT_LOG_PATH.exists():
        return jsonify({"lines": [], "size": 0})
    size = CHAT_LOG_PATH.stat().st_size
    if since_byte >= 0 and since_byte < size:
        with open(CHAT_LOG_PATH, "rb") as f:
            f.seek(since_byte)
            chunk = f.read().decode("utf-8", errors="replace")
        return jsonify({"lines": chunk.splitlines(), "size": size})
    if tail > 0:
        text = CHAT_LOG_PATH.read_text(encoding="utf-8", errors="replace")
        return jsonify({"lines": text.splitlines()[-tail:], "size": size})
    return jsonify({"lines": [], "size": size})


@app.route("/api/runs")
def list_runs():
    if not LOGS_DIR.exists():
        return jsonify([])
    files = sorted(LOGS_DIR.glob("*.txt"), reverse=True)[:50]
    return jsonify([
        {"name": f.name, "size": f.stat().st_size}
        for f in files
    ])


@app.route("/api/runs/<filename>")
def get_run_log(filename: str):
    if not _LOG_FILENAME_RE.match(filename):
        abort(400)
    path = LOGS_DIR / filename
    if not path.exists():
        abort(404)
    tail = request.args.get("tail", default=0, type=int)
    size = path.stat().st_size
    if tail > 0:
        text = path.read_text(encoding="utf-8", errors="replace")
        return jsonify({"lines": text.splitlines()[-tail:], "size": size})
    text = path.read_text(encoding="utf-8", errors="replace")
    return jsonify({"lines": text.splitlines(), "size": size})


def _ipv4_only(address: str) -> str:
    """Gluetun/Mullvad-Configs enthalten teils IPv4+IPv6 kommagetrennt.
    IPv6 wird von gluetun in dieser Umgebung nicht unterstuetzt -> raus damit."""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    ipv4_parts = [p for p in parts if ":" not in p]
    return ",".join(ipv4_parts) if ipv4_parts else address


def _read_env_file() -> dict:
    result = {}
    if ENV_FILE_PATH.exists():
        for line in ENV_FILE_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def _write_env_file(updates: dict) -> None:
    current = _read_env_file()
    current.update(updates)
    lines = [f"{k}={v}" for k, v in current.items()]
    ENV_FILE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.route("/api/vpn", methods=["GET"])
def get_vpn_config():
    env = _read_env_file()
    provider = env.get("VPN_SERVICE_PROVIDER", "mullvad")
    result = {
        "has_key": bool(env.get("MULLVAD_PRIVATE_KEY")),
        "address": env.get("MULLVAD_ADDRESS", ""),
        "server": env.get("MULLVAD_SERVER_HOSTNAMES", "nl-ams-wg-103"),
        "provider": provider,
        "vpn_enabled": env.get("VPN_ENABLED", "").strip().lower() == "true",
    }
    if provider == "custom":
        result["endpoint"] = f'{env.get("WIREGUARD_ENDPOINT_IP", "")}:{env.get("WIREGUARD_ENDPOINT_PORT", "")}'
        result["public_key"] = env.get("WIREGUARD_PUBLIC_KEY", "")
    return jsonify(result)


def _parse_wg_conf(text: str) -> dict:
    parsed = {}
    section = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if "=" not in line or not section:
            continue
        key, val = line.split("=", 1)
        parsed[f"{section}.{key.strip().lower()}"] = val.strip()
    return parsed


@app.route("/api/vpn/disable", methods=["POST"])
def disable_vpn():
    try:
        _write_env_file({"VPN_ENABLED": "false"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/vpn/restart", methods=["POST"])
def restart_vpn():
    try:
        TRIGGER_DIR.mkdir(parents=True, exist_ok=True)
        if RESTART_LOG_FILE.exists():
            RESTART_LOG_FILE.unlink()
        TRIGGER_FILE.write_text(datetime.now().isoformat(), encoding="utf-8")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/vpn/restart-log")
def vpn_restart_log():
    if not RESTART_LOG_FILE.exists():
        return jsonify({"done": False})
    return jsonify({"done": True, "log": RESTART_LOG_FILE.read_text(encoding="utf-8", errors="replace")})


@app.route("/api/vpn/upload", methods=["POST"])
def upload_vpn_config():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "Keine Datei erhalten"}), 400

    raw = f.read(20_000)  # WireGuard-Configs sind winzig, alles darueber ist verdaechtig
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return jsonify({"ok": False, "error": "Datei ist kein gültiger Text (.conf erwartet)"}), 400

    parsed = _parse_wg_conf(text)
    private_key = parsed.get("interface.privatekey", "")
    address = _ipv4_only(parsed.get("interface.address", ""))
    public_key = parsed.get("peer.publickey", "")
    endpoint = parsed.get("peer.endpoint", "")

    if not (private_key and address and public_key and endpoint):
        return jsonify({
            "ok": False,
            "error": "Config unvollständig – PrivateKey, Address, PublicKey und Endpoint werden benötigt",
        }), 400

    if ":" in endpoint:
        endpoint_ip, endpoint_port = endpoint.rsplit(":", 1)
    else:
        endpoint_ip, endpoint_port = endpoint, "51820"

    try:
        _write_env_file({
            "MULLVAD_PRIVATE_KEY": private_key,
            "MULLVAD_ADDRESS": address,
            "MULLVAD_SERVER_HOSTNAMES": "",  # gehoert nur zum mullvad-Modus, sonst lehnt gluetun custom ab
            "VPN_SERVICE_PROVIDER": "custom",
            "WIREGUARD_ENDPOINT_IP": endpoint_ip.strip(),
            "WIREGUARD_ENDPOINT_PORT": endpoint_port.strip(),
            "WIREGUARD_PUBLIC_KEY": public_key,
            "VPN_ENABLED": "true",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "endpoint": f"{endpoint_ip.strip()}:{endpoint_port.strip()}"})


@app.route("/api/vpn", methods=["POST"])
def save_vpn_config():
    data = request.get_json(force=True) or {}
    private_key = str(data.get("private_key", "")).strip()
    address = str(data.get("address", "")).strip()
    server = str(data.get("server", "")).strip()

    if not private_key or not address:
        return jsonify({"ok": False, "error": "Private Key und Adresse sind erforderlich"}), 400

    updates = {
        "MULLVAD_PRIVATE_KEY": private_key,
        "MULLVAD_ADDRESS": _ipv4_only(address),
        "VPN_SERVICE_PROVIDER": "mullvad",
        "VPN_ENABLED": "true",
        # gehoeren nur zum custom-Modus, sonst irritiert es gluetuns Mullvad-Provider-Validierung
        "WIREGUARD_ENDPOINT_IP": "",
        "WIREGUARD_ENDPOINT_PORT": "",
        "WIREGUARD_PUBLIC_KEY": "",
    }
    if server:
        updates["MULLVAD_SERVER_HOSTNAMES"] = server

    try:
        _write_env_file(updates)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


def _gluetun_base_url() -> str | None:
    """Im VPN-Modus (network_mode: service:gluetun) teilen sich xdcc und
    gluetun denselben Netzwerk-Stack -> gluetun ist dann nur ueber
    localhost erreichbar, nicht mehr per Hostnamen 'gluetun'. Im
    Nicht-VPN-Modus ist es umgekehrt. Beide Faelle einfach durchprobieren."""
    headers = {"X-API-Key": GLUETUN_API_KEY} if GLUETUN_API_KEY else {}
    for base in ("http://localhost:8000", GLUETUN_CONTROL_URL):
        try:
            r = requests.get(f"{base}/v1/vpn/status", headers=headers, timeout=1.5)
            if r.ok:
                return base
        except Exception:
            continue
    return None


@app.route("/api/vpn/status")
def vpn_status():
    headers = {"X-API-Key": GLUETUN_API_KEY} if GLUETUN_API_KEY else {}
    base = _gluetun_base_url()
    if not base:
        return jsonify({"reachable": False})

    try:
        r = requests.get(f"{base}/v1/vpn/status", headers=headers, timeout=2)
        r.raise_for_status()
        status = r.json().get("status")
    except Exception:
        return jsonify({"reachable": False})

    public_ip = None
    try:
        r_ip = requests.get(f"{base}/v1/publicip/ip", headers=headers, timeout=2)
        if r_ip.ok:
            public_ip = r_ip.json().get("public_ip")
    except Exception:
        pass

    return jsonify({"reachable": True, "status": status, "public_ip": public_ip})


if __name__ == "__main__":
    Path("/app/templates").mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=5005)
