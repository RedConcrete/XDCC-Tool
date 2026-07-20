#!/usr/bin/env python3
"""Host-Watcher: beobachtet eine Trigger-Datei und fuehrt bei Bedarf
'docker compose up -d' im xdcc-Projekt aus - mit oder ohne VPN-Kopplung,
je nach VPN_ENABLED in .env. Laeuft ausserhalb von Docker direkt auf dem Host."""

import json
import subprocess
import time
from pathlib import Path

COMPOSE_DIR = Path("/home/dockerserver/docker/xdcc")
TRIGGER_FILE = COMPOSE_DIR / "restart_trigger" / "requested"
LOG_FILE = COMPOSE_DIR / "restart_trigger" / "last_result.log"
ENV_FILE = COMPOSE_DIR / ".env"
POLL_INTERVAL = 3


def vpn_enabled() -> bool:
    if not ENV_FILE.exists():
        return False
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("VPN_ENABLED="):
            return line.split("=", 1)[1].strip().lower() == "true"
    return False


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(COMPOSE_DIR), capture_output=True, text=True, timeout=180)


def _env_value(key: str) -> str:
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def _gluetun_ip() -> str:
    result = subprocess.run(
        ["docker", "inspect", "xdcc-gluetun", "--format",
         "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _gluetun_vpn_status() -> str:
    """Fragt gluetuns eigene Control-API nach dem ECHTEN Tunnel-Status ab.
    Der Docker-Container selbst bleibt bei fehlgeschlagener VPN-Verbindung
    im Status 'running' (gluetun retried intern) - das allein reicht als
    Health-Check nicht aus."""
    ip = _gluetun_ip()
    if not ip:
        return "missing"
    api_key = _env_value("GLUETUN_API_KEY")
    try:
        result = subprocess.run(
            ["curl", "-s", "-m", "3", "-H", f"X-API-Key: {api_key}",
             f"http://{ip}:8000/v1/vpn/status"],
            capture_output=True, text=True, timeout=5,
        )
        return json.loads(result.stdout).get("status", "unknown")
    except Exception:
        return "unreachable"


def _wait_for_gluetun_healthy(timeout: int = 45, stable_for: int = 6) -> bool:
    """Wartet bis die tatsaechliche VPN-Verbindung stabil steht."""
    healthy_since = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _gluetun_vpn_status() == "running":
            if healthy_since is None:
                healthy_since = time.time()
            elif time.time() - healthy_since >= stable_for:
                return True
        else:
            healthy_since = None
        time.sleep(2)
    return False


def _disable_vpn_in_env() -> None:
    if not ENV_FILE.exists():
        return
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    out, found = [], False
    for line in lines:
        if line.strip().startswith("VPN_ENABLED="):
            out.append("VPN_ENABLED=false")
            found = True
        else:
            out.append(line)
    if not found:
        out.append("VPN_ENABLED=false")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def run_restart() -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    enabled = vpn_enabled()
    results = []
    rollback_note = ""

    if enabled:
        # VPN-Modus: gluetun + an gluetun gekoppeltes xdcc neu erstellen.
        # --force-recreate, da blosses "up -d" nach einem network_mode-Wechsel
        # gelegentlich eine inkonsistente Docker-Portbindung hinterlaesst.
        results.append(_run(["docker", "compose", "-f", "docker-compose.yml",
                              "-f", "docker-compose.vpn.yml", "up", "-d", "--force-recreate"]))

        if not _wait_for_gluetun_healthy():
            # gluetun crash-loopt (z.B. falsche VPN-Daten) -> automatischer
            # Rollback, sonst ist die Webseite selbst nicht mehr erreichbar
            rollback_note = ("\n\n!!! ROLLBACK: gluetun kam nicht stabil hoch - "
                              "automatisch auf Nicht-VPN-Modus zurueckgeschaltet !!!\n")
            # WICHTIG: erst gluetun stoppen (haelt sonst Port 5005 noch), dann xdcc
            results.append(_run(["docker", "compose", "-f", "docker-compose.yml", "stop", "gluetun"]))
            results.append(_run(["docker", "compose", "-f", "docker-compose.yml",
                                  "up", "-d", "--force-recreate", "xdcc"]))
            _disable_vpn_in_env()
    else:
        # Kein VPN: gluetun zuerst stoppen (haelt sonst Port 5005 noch belegt),
        # dann xdcc eigenstaendig starten. "up -d" ohne Override wuerde sonst
        # trotzdem ALLE Services im Compose-File mitstarten.
        results.append(_run(["docker", "compose", "-f", "docker-compose.yml", "stop", "gluetun"]))
        results.append(_run(["docker", "compose", "-f", "docker-compose.yml",
                              "up", "-d", "--force-recreate", "xdcc"]))

    log_parts = [f"[{ts}] vpn={enabled}"]
    for i, result in enumerate(results):
        log_parts.append(
            f"--- Befehl {i + 1} (exit={result.returncode}) ---\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    LOG_FILE.write_text("\n".join(log_parts) + rollback_note + "\n", encoding="utf-8")


def main() -> None:
    TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    while True:
        if TRIGGER_FILE.exists():
            try:
                TRIGGER_FILE.unlink()
            except FileNotFoundError:
                pass
            try:
                run_restart()
            except Exception as e:
                LOG_FILE.write_text(f"Watcher-Fehler: {e}\n", encoding="utf-8")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
