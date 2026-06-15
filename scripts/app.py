#!/usr/bin/env python3
"""Lokale Web-UI fuer den XDCC-Downloader (LAN-only, Port 5005)."""

import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import cli

app = Flask(__name__)

_lock = threading.Lock()
_state = {
    "running": False,
    "log": [],          # Liste von {"ts","title","msg","level"}
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


def _run_job():
    try:
        summary = cli.run_once(_status_cb, _progress_cb)
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
        _state["summary"] = None
        _state["progress"] = None
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
