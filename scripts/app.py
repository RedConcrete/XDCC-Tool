#!/usr/bin/env python3
"""Lokale Web-UI fuer den XDCC-Downloader (LAN-only, Port 5005)."""

import os
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import cli

CHAT_LOG_PATH = Path(os.environ.get("CHAT_LOG", "/app/Chat.txt"))

app = Flask(__name__)

_lock = threading.Lock()
_state = {
    "running": False,
    "log": [],          # Liste von {"ts","title","msg","level"}
    "irc_log": [],      # Liste von {"ts","line"} – roh IRC-Traffic
    "summary": None,     # Ergebnis von run_once() nach Abschluss
    "progress": None,    # {"title","received","total"} waehrend Download
}


def _status_cb(title, msg, level="info"):
    with _lock:
        _state["log"].append({"ts": time.time(), "title": title, "msg": msg, "level": level})
        if level in ("success", "error") or "fertig" in msg or "fehlgeschlagen" in msg:
            _state["progress"] = None


def _progress_cb(title, received, total):
    with _lock:
        _state["progress"] = {"title": title, "received": received, "total": total}


def _irc_cb(line: str):
    with _lock:
        _state["irc_log"].append({"ts": time.time(), "line": line})
    try:
        with open(CHAT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _run_job():
    try:
        summary = cli.run_once(_status_cb, _progress_cb, _irc_cb)
    except Exception as e:
        summary = {"error": str(e)}
        _status_cb("Allgemein", f"Lauf abgebrochen: {e}", "error")
    with _lock:
        _state["running"] = False
        _state["summary"] = summary
        _state["progress"] = None


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


@app.route("/api/run", methods=["POST"])
def run():
    with _lock:
        if _state["running"]:
            return jsonify({"ok": False, "error": "Lauf bereits aktiv"}), 409
        _state["running"] = True
        _state["log"] = []
        _state["irc_log"] = []
        _state["summary"] = None
        _state["progress"] = None
    try:
        with open(CHAT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"=== Lauf gestartet: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            f.write(f"{'='*60}\n")
    except Exception:
        pass
    threading.Thread(target=_run_job, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/status")
def status():
    since = request.args.get("since", default=0, type=int)
    with _lock:
        log_slice = _state["log"][since:]
        return jsonify({
            "running": _state["running"],
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
        lines = text.splitlines()
        return jsonify({"lines": lines[-tail:], "size": size})
    return jsonify({"lines": [], "size": size})


@app.route("/api/downloaded", methods=["DELETE"])
def delete_downloaded():
    data = request.get_json(force=True) or {}
    title = str(data.get("title", "")).strip()
    if not title:
        return jsonify({"ok": False, "error": "title fehlt"}), 400
    removed = cli.remove_done(title)
    return jsonify({"ok": removed})


if __name__ == "__main__":
    Path("/app/templates").mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=5005)
