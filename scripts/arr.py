"""Anbindung an Seerr, Radarr und Sonarr.

Ablauf: Seerr schickt bei genehmigten Anfragen einen Webhook. Daraus
werden Wunschlisten-Eintraege mit deutschem Titel (Seerr liefert ihn in
der Sprache "de"). Laedt xdcc eine deutsche Fassung, landet sie direkt im
Ordner, den Radarr/Sonarr fuer den Titel angelegt haben, und die beiden
lesen neu ein. Findet xdcc nichts Deutsches, wird die normale Suche von
Radarr/Sonarr angestossen (englische Fassung als Notloesung).

Konfiguration in der .env:
  SEERR_URL, SEERR_API_KEY, RADARR_URL, RADARR_API_KEY,
  SONARR_URL, SONARR_API_KEY, SEERR_WEBHOOK_TOKEN
"""

import json
import os
import re
import time
import unicodedata
from pathlib import Path

import requests

from downloader import read_env_file

META_PATH = Path(os.environ.get("WISHLIST_META", "/app/logs/wishlist_meta.json"))
TIMEOUT = 15

APPROVED_TYPES = {"MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"}
_KEYS = ("SEERR_URL", "SEERR_API_KEY", "RADARR_URL", "RADARR_API_KEY",
         "SONARR_URL", "SONARR_API_KEY", "SEERR_WEBHOOK_TOKEN")


def config() -> dict:
    env = read_env_file()
    return {k: env.get(k, "").strip().rstrip("/") if k.endswith("_URL") else env.get(k, "").strip()
            for k in _KEYS}


def enabled(app: str) -> bool:
    c = config()
    return bool(c.get(f"{app.upper()}_URL") and c.get(f"{app.upper()}_API_KEY"))


def _call(app: str, method: str, path: str, **kw):
    c = config()
    url = f"{c[app.upper() + '_URL']}{path}"
    r = requests.request(method, url, headers={"X-Api-Key": c[app.upper() + "_API_KEY"]},
                         timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r.json() if r.content else None


# ---------- Metadaten zu Wunschlisten-Eintraegen ----------

def _key(title: str, category: str) -> str:
    return f"{category}|{title.strip().lower()}"


def load_meta() -> dict:
    try:
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_meta(data: dict) -> None:
    META_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = META_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(META_PATH)


def get_meta(title: str, category: str, meta: dict | None = None) -> dict | None:
    return (meta if meta is not None else load_meta()).get(_key(title, category))


def set_meta(title: str, category: str, value: dict) -> None:
    data = load_meta()
    data[_key(title, category)] = value
    _save_meta(data)


def pop_meta(title: str, category: str) -> dict | None:
    data = load_meta()
    value = data.pop(_key(title, category), None)
    if value is not None:
        _save_meta(data)
    return value


def move_meta(title: str, src: str, dst: str) -> None:
    value = pop_meta(title, src)
    if value is not None:
        set_meta(title, dst, value)


# ---------- Seerr-Webhook -> Wunschlisten-Eintraege ----------

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def search_title(title: str) -> str:
    """'Rapunzel – Neu verföhnt' -> 'Rapunzel Neu verfoehnt' (Release-Schreibweise)."""
    t = title.translate(_UMLAUTS)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = t.replace("&", "and").replace("'", "")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _payload_value(payload: dict, key: str, nested: tuple[str, ...] = ()) -> str:
    v = payload.get(key)
    if v in (None, ""):
        for n in nested:
            sub = payload.get(n) or {}
            if isinstance(sub, dict) and sub.get(key) not in (None, ""):
                v = sub.get(key)
                break
    return str(v or "").strip()


def items_from_webhook(payload: dict) -> list[tuple[str, str, dict]]:
    """[(titel, kategorie, meta), ...] fuer eine genehmigte Seerr-Anfrage."""
    if payload.get("notification_type") not in APPROVED_TYPES:
        return []
    media_type = _payload_value(payload, "media_type", ("media",))
    tmdb_id = _payload_value(payload, "tmdbId", ("media",))
    tvdb_id = _payload_value(payload, "tvdbId", ("media",))
    request_id = _payload_value(payload, "request_id", ("request",))
    if not tmdb_id.isdigit():
        raise ValueError(f"Webhook ohne tmdbId: {payload!r}")

    if media_type == "movie":
        d = _call("seerr", "GET", f"/api/v1/movie/{tmdb_id}", params={"language": "de"})
        year = (d.get("releaseDate") or "")[:4]
        de_title = search_title(d.get("title") or "")
        orig = search_title(d.get("originalTitle") or "")
        title = f"{de_title} {year}".strip()
        alt = [f"{orig} {year}".strip()] if orig and orig.lower() != de_title.lower() else []
        meta = {"source": "seerr", "type": "movie", "tmdbId": int(tmdb_id),
                "request": request_id, "alt": alt}
        return [(title, "film", meta)]

    if media_type == "tv":
        d = _call("seerr", "GET", f"/api/v1/tv/{tmdb_id}", params={"language": "de"})
        name = search_title(d.get("name") or "")
        orig = search_title(d.get("originalName") or "")
        keywords = {(k.get("name") or "").lower() for k in d.get("keywords") or []}
        category = "anime" if "anime" in keywords else "serien"
        seasons: list[int] = []
        if request_id.isdigit():
            req = _call("seerr", "GET", f"/api/v1/request/{request_id}")
            seasons = sorted({int(s["seasonNumber"]) for s in req.get("seasons") or []
                              if int(s.get("seasonNumber", 0)) > 0})
        if not seasons:
            seasons = sorted({int(s["seasonNumber"]) for s in d.get("seasons") or []
                              if int(s.get("seasonNumber", 0)) > 0})
        tvdb = int(tvdb_id) if tvdb_id.isdigit() else (d.get("externalIds") or {}).get("tvdbId")
        items = []
        for n in seasons:
            alt = [f"{orig} S{n:02d}"] if orig and orig.lower() != name.lower() else []
            meta = {"source": "seerr", "type": "tv", "tmdbId": int(tmdb_id), "tvdbId": tvdb,
                    "season": n, "request": request_id, "alt": alt}
            items.append((f"{name} S{n:02d}", category, meta))
        return items
    return []


# ---------- Radarr / Sonarr ----------

def host_to_container(path: str) -> Path:
    """/mnt/jellyfin4/media/movies/X -> /targets/jellyfin4/movies/X"""
    return Path(re.sub(r"^/mnt/(jellyfin\d*)/media(?=/|$)", r"/targets/\1", path))


def find_arr_item(meta: dict, wait: float = 0) -> tuple[str, dict] | None:
    """('radarr', movie) bzw. ('sonarr', series) - Seerr legt den Eintrag
    evtl. erst kurz nach dem Webhook an, daher optional warten."""
    deadline = time.time() + wait
    while True:
        try:
            if meta.get("type") == "movie" and enabled("radarr"):
                hits = _call("radarr", "GET", "/api/v3/movie", params={"tmdbId": meta["tmdbId"]})
                if hits:
                    return "radarr", hits[0]
            elif meta.get("type") == "tv" and enabled("sonarr") and meta.get("tvdbId"):
                hits = _call("sonarr", "GET", "/api/v3/series", params={"tvdbId": meta["tvdbId"]})
                if hits:
                    return "sonarr", hits[0]
        except requests.RequestException:
            pass
        if time.time() >= deadline:
            return None
        time.sleep(10)


def rescan(app: str, item: dict) -> None:
    if app == "radarr":
        _call("radarr", "POST", "/api/v3/command", json={"name": "RescanMovie", "movieId": item["id"]})
    else:
        _call("sonarr", "POST", "/api/v3/command", json={"name": "RescanSeries", "seriesId": item["id"]})


def fallback_search(meta: dict) -> str | None:
    """Normale Suche in Radarr/Sonarr anstossen. Rueckgabe: Text fuers Badge."""
    found = find_arr_item(meta, wait=30)
    if not found:
        return None
    app, item = found
    if app == "radarr":
        _call("radarr", "POST", "/api/v3/command", json={"name": "MoviesSearch", "movieIds": [item["id"]]})
        return "Keine deutsche Fassung – Radarr sucht jetzt die englische"
    _call("sonarr", "POST", "/api/v3/command",
          json={"name": "SeasonSearch", "seriesId": item["id"], "seasonNumber": meta["season"]})
    return "Keine deutsche Fassung – Sonarr sucht jetzt die englische"
