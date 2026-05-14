#!/usr/bin/env python3
"""
Einfacher XDCC-Downloader via direktem IRC-Socket.
Verbindet sich mit dem IRC-Server, joint den Channel und
fordert das Pack per DCC an.
"""

import socket
import time
import struct
import os
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

NICK_BASE = "xdcc"


def make_nick() -> str:
    import random, string
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{NICK_BASE}{suffix}"


class XDCCDownloader:
    def __init__(self, server: str, port: int, channel: str, bot: str,
                 pack: str, output_dir: Path, timeout: int = 120):
        self.server     = server
        self.port       = port
        self.channel    = channel
        self.bot        = bot.lower()
        self.pack       = pack
        self.output_dir = output_dir
        self.timeout    = timeout
        self.nick       = make_nick()
        self.sock       = None
        self.done       = False
        self.success    = False
        self.filename   = None

    def _send(self, msg: str):
        self.sock.sendall((msg + "\r\n").encode("utf-8", errors="replace"))
        log.debug(f">>> {msg}")

    def _recv_lines(self):
        buf = b""
        while not self.done:
            try:
                data = self.sock.recv(4096)
                if not data:
                    break
                buf += data
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    yield line.decode("utf-8", errors="replace")
            except socket.timeout:
                continue

    def _handle_dcc(self, dcc_msg: str):
        """Parst DCC SEND und startet den Datei-Transfer."""
        # Format: DCC SEND filename ip port size
        import re
        m = re.search(r'DCC SEND "?([^"]+?)"?\s+(\d+)\s+(\d+)\s+(\d+)', dcc_msg)
        if not m:
            log.error(f"DCC SEND Parse-Fehler: {dcc_msg}")
            return False

        filename  = m.group(1)
        ip_int    = int(m.group(2))
        port      = int(m.group(3))
        filesize  = int(m.group(4))

        # IP aus Integer konvertieren
        ip = socket.inet_ntoa(struct.pack("!I", ip_int))
        self.filename = filename

        log.info(f"DCC SEND: {filename} | {filesize//1024//1024}MB | {ip}:{port}")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        outpath = self.output_dir / filename

        STALL_TIMEOUT = 30  # Sekunden ohne Daten = Stall
        try:
            dcc_sock = socket.create_connection((ip, port), timeout=8)
            dcc_sock.settimeout(STALL_TIMEOUT)
            received = 0
            start = time.time()
            last_log = start
            last_data = start

            with open(outpath, "wb") as f:
                while received < filesize:
                    try:
                        chunk = dcc_sock.recv(65536)
                    except socket.timeout:
                        stall = time.time() - last_data
                        log.warning(f"  Keine Daten seit {stall:.0f}s – Stall erkannt, breche ab")
                        raise Exception(f"Stall nach {stall:.0f}s ohne Daten")
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    last_data = time.time()
                    # Acknowledge
                    dcc_sock.sendall(struct.pack("!I", received & 0xFFFFFFFF))

                    now = time.time()
                    if now - last_log >= 30:
                        pct = received / filesize * 100
                        speed = received / (now - start) / 1024 / 1024
                        log.info(f"  {pct:.1f}% | {received//1024//1024}MB / {filesize//1024//1024}MB | {speed:.1f} MB/s")
                        last_log = now

            dcc_sock.close()
            elapsed = time.time() - start
            speed = received / elapsed / 1024 / 1024 if elapsed > 0 else 0
            log.info(f"✓ Download fertig: {filename} ({received//1024//1024}MB in {elapsed:.0f}s, {speed:.1f} MB/s)")
            self.success = True
            return True

        except Exception as e:
            log.error(f"DCC Transfer-Fehler: {e}")
            if outpath.exists():
                outpath.unlink()
            return False

    def download(self) -> bool:
        log.info(f"Verbinde mit {self.server}:{self.port} als {self.nick}")
        try:
            self.sock = socket.create_connection((self.server, self.port), timeout=30)
            self.sock.settimeout(5)
        except Exception as e:
            log.error(f"Verbindung fehlgeschlagen: {e}")
            return False

        self._send(f"NICK {self.nick}")
        self._send(f"USER {self.nick} 0 * :{self.nick}")

        registered    = False
        joined        = False
        request_sent  = False
        deadline      = time.time() + self.timeout

        try:
            for line in self._recv_lines():
                if time.time() > deadline:
                    log.error("Timeout - kein Download gestartet")
                    break

                log.debug(f"<<< {line}")

                # PING/PONG
                if line.startswith("PING"):
                    self._send(f"PONG {line.split(':', 1)[-1]}")
                    continue

                parts = line.split()
                if len(parts) < 2:
                    continue

                code = parts[1]

                # 001 = Registered
                if code == "001" and not registered:
                    registered = True
                    log.info(f"Registriert. Joine {self.channel}")
                    self._send(f"JOIN {self.channel}")

                # JOIN bestätigt
                elif "JOIN" in line and self.nick.lower() in line.lower() and not joined:
                    joined = True
                    log.info(f"Channel {self.channel} gejoint. Sende XDCC-Anfrage...")
                    time.sleep(3)
                    self._send(f"PRIVMSG {self.bot} :xdcc send #{self.pack}")
                    request_sent = True
                    deadline = time.time() + self.timeout

                # DCC SEND vom Bot
                elif "PRIVMSG" in line and "DCC SEND" in line:
                    sender = line.split("!")[0].lstrip(":").lower()
                    if sender == self.bot or self.bot in line.lower():
                        dcc_part = line.split("DCC SEND", 1)[1]
                        self.done = True
                        return self._handle_dcc("DCC SEND" + dcc_part)

                # Bot schickt oft via NOTICE
                elif "NOTICE" in line and "DCC SEND" in line:
                    dcc_part = line.split("DCC SEND", 1)[1]
                    self.done = True
                    return self._handle_dcc("DCC SEND" + dcc_part)

                # CTCP DCC
                elif "\x01DCC SEND" in line:
                    dcc_part = line.split("\x01DCC SEND", 1)[1].rstrip("\x01")
                    self.done = True
                    return self._handle_dcc("DCC SEND" + dcc_part)

                # Queue-Meldungen vom Bot
                elif request_sent and "queue" in line.lower():
                    log.info(f"Bot: {line.split(':', 2)[-1].strip()}")
                    deadline = time.time() + 600  # Warteschlange = mehr Zeit

                # Banned oder Error
                elif code in ("474", "473", "475"):
                    log.error(f"Channel-Fehler: {line}")
                    break

        except Exception as e:
            log.error(f"IRC-Fehler: {e}")
        finally:
            self.done = True
            try:
                self._send("QUIT :bye")
                self.sock.close()
            except Exception:
                pass

        return self.success


def xdcc_download(server: str, channel: str, bot: str, pack: str,
                  output_dir: Path, port: int = 6667, timeout: int = 180) -> bool:
    d = XDCCDownloader(server, port, channel, bot, pack, output_dir, timeout)
    return d.download()
