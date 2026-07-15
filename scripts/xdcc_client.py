#!/usr/bin/env python3
"""
Einfacher XDCC-Downloader via direktem IRC-Socket.
Verbindet sich mit dem IRC-Server, joint den Channel und
fordert das Pack per DCC an.
"""

import re
import select
import socket
import time
import struct
import os
import logging
import threading
import ssl
import base64
from pathlib import Path

log = logging.getLogger(__name__)

# Gamer-Tag-Nicks statt "xdcc..." als Nick-Basis, da viele XDCC-Channels
# Nicks mit "xdcc" per +b-Bann gegen Leech-Skripte blocken.
_NICK_NAMES = [
    "ShadowWolf", "NightHawk", "DragonSlayer", "PhantomX", "ViperZ",
    "GhostRider", "StormBreaker", "IronFist", "BlazeFury", "CrimsonFox",
    "DarkPhoenix", "RogueAce", "SilentSniper", "ToxicVenom", "FrostByte",
    "ApexPredator", "SteelTitan", "VoidWalker", "ThunderStrike", "NovaBlade",
]

# Hammering-Schutz: Mindestabstand zwischen Verbindungen zum selben Server
_MIN_CONNECT_INTERVAL = 30  # Sekunden
_last_connect: dict[str, float] = {}
_connect_lock = threading.Lock()


def _rate_limit(server: str, status_cb=None) -> None:
    with _connect_lock:
        now = time.time()
        wait = _MIN_CONNECT_INTERVAL - (now - _last_connect.get(server, 0))
        if wait > 0:
            if status_cb:
                status_cb(f"Warte {wait:.0f}s vor erneuter Verbindung zu {server}", "info")
            time.sleep(wait)
        _last_connect[server] = time.time()


_TITLE_STOP = {
    "german", "deutsch", "english", "dl", "aac", "ac3", "dts", "eac3",
    "1080p", "720p", "480p", "2160p", "4k", "uhd",
    "bluray", "webrip", "web", "hdtv", "dvdrip", "bdrip",
    "x264", "x265", "hevc", "h264", "h265", "xvid",
    "proper", "repack", "extended", "remux", "hybrid",
    "mkv", "mp4", "avi", "tar", "zip",
}


def _title_words(fname: str) -> set[str]:
    words = []
    for part in re.split(r'[.\-_\s]+', fname):
        if re.fullmatch(r'\d{4}', part):
            break
        low = part.lower()
        if low in _TITLE_STOP:
            break
        if len(part) >= 2:
            words.append(low)
    return set(words)


def _titles_match(expected: str, actual: str) -> bool:
    exp = _title_words(expected)
    act = _title_words(actual)
    if not exp:
        return True
    key = {w for w in exp if len(w) > 3}
    if not key:
        return bool(exp & act)
    return key.issubset(act)


def make_nick() -> str:
    import random, string
    name = random.choice(_NICK_NAMES)
    suffix = ''.join(random.choices(string.digits, k=3))
    return f"{name}{suffix}"


_IRC_DL_NOISE_RE = re.compile(
    r'^\S+\s+(?:37[256]|00[1-5]|25[0-9]|265|266|315|353|366|MODE)\s'
)


class XDCCDownloader:
    def __init__(self, server: str, port: int, channel: str, bot: str,
                 pack: str, output_dir: Path, timeout: int = 120,
                 extra_channels: list = None,
                 progress_callback=None, status_callback=None, stall_callback=None,
                 skip_names_check: bool = False, expected_fname: str = "",
                 irc_callback=None, tls: bool = False,
                 sasl_user: str = "", sasl_pass: str = "",
                 sasl_fail_callback=None):
        self.server         = server
        self.port           = port
        self.channel        = channel
        self.bot            = bot.lower()
        self.pack           = pack
        self.output_dir     = output_dir
        self.timeout        = timeout
        self.extra_channels = extra_channels or []
        self.nick           = sasl_user if (sasl_user and sasl_pass) else make_nick()
        self.sock           = None
        self.done           = False
        self.success        = False
        self.filename       = None
        self._dcc_resume    = None
        self.progress_callback    = progress_callback
        self.status_callback      = status_callback
        self.stall_callback       = stall_callback
        self.skip_names_check     = skip_names_check
        self.expected_fname       = expected_fname
        self.irc_callback         = irc_callback
        self.file_exists_callback = None   # wird von außen gesetzt
        self.tls = tls
        self.sasl_user = sasl_user
        self.sasl_pass = sasl_pass
        self.sasl_done = not (sasl_user and sasl_pass)
        self.sasl_fail_callback = sasl_fail_callback
        self._skip_dcc_fname      = None   # angekündigte Datei überspringen

    def _send(self, msg: str):
        self.sock.sendall((msg + "\r\n").encode("utf-8", errors="replace"))
        _log_msg = "AUTHENTICATE ****" if msg.startswith("AUTHENTICATE ") and msg != "AUTHENTICATE PLAIN" else msg
        log.debug(f">>> {_log_msg}")
        if self.irc_callback and any(msg.startswith(p) for p in ("JOIN", "PRIVMSG", "QUIT")):
            self.irc_callback(f">> {msg}")

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

    def _parse_dcc_send(self, dcc_msg: str):
        """Parst DCC SEND und gibt (filename, ip, port, filesize) zurück."""
        import re
        m = re.search(r'DCC SEND "?([^"]+?)"?\s+(\d+)\s+(\d+)\s+(\d+)', dcc_msg)
        if not m:
            return None
        filename = m.group(1)
        ip       = socket.inet_ntoa(struct.pack("!I", int(m.group(2))))
        port     = int(m.group(3))
        filesize = int(m.group(4))
        return filename, ip, port, filesize

    def _do_transfer(self, ip: str, port: int, filename: str,
                     filesize: int, resume_pos: int = 0) -> bool:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        outpath = self.output_dir / filename
        STALL_CHECK = 60  # Sekunden ohne Daten → User fragen

        try:
            dcc_sock = socket.create_connection((ip, port), timeout=30)
            dcc_sock.settimeout(None)  # vollständig blockierend – kein Timeout auf recv/sendall
            received = resume_pos
            start    = time.time()

            mode = "ab" if resume_pos > 0 else "wb"
            if resume_pos > 0 and self.status_callback:
                self.status_callback(f"Resume ab {resume_pos//1024//1024} MB", "info")

            with open(outpath, mode) as f:
                while received < filesize:
                    # select() wartet bis Daten da sind – ohne Socket-Timeout zu setzen
                    readable, _, _ = select.select([dcc_sock], [], [], STALL_CHECK)
                    if not readable:
                        # Keine Daten seit STALL_CHECK Sekunden → User fragen
                        keep_going = True
                        if self.stall_callback:
                            keep_going = self.stall_callback(received, filesize)
                        if not keep_going:
                            log.info("Download vom User abgebrochen")
                            return False
                        continue  # weiter warten
                    chunk = dcc_sock.recv(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    dcc_sock.sendall(struct.pack("!I", received & 0xFFFFFFFF))
                    if self.progress_callback:
                        self.progress_callback(received, filesize)

            dcc_sock.close()
            elapsed = time.time() - start
            speed = received / elapsed / 1024 / 1024 if elapsed > 0 else 0
            log.info(f"✓ Download fertig: {filename} ({received//1024//1024}MB in {elapsed:.0f}s, {speed:.1f} MB/s)")
            self.success = True
            return True

        except Exception as e:
            log.error(f"DCC Transfer-Fehler: {e}")
            if self.status_callback:
                self.status_callback(str(e), "error")
            return False

    def _handle_dcc(self, dcc_msg: str):
        """Parst DCC SEND. Bei vorhandener Teil-Datei wird DCC RESUME gesendet."""
        parsed = self._parse_dcc_send(dcc_msg)
        if not parsed:
            log.error(f"DCC SEND Parse-Fehler: {dcc_msg}")
            return False

        filename, ip, port, filesize = parsed
        self.filename = filename
        log.info(f"DCC SEND: {filename} | {filesize//1024//1024}MB | {ip}:{port}")

        if self._skip_dcc_fname and self._skip_dcc_fname == filename:
            log.info(f"  Datei bereits vorhanden – überspringe: {filename}")
            self.success = True
            self.done = True
            return True

        if self.expected_fname and not _titles_match(self.expected_fname, filename):
            log.info(f"  Titel-Mismatch: {filename!r} (erwartet: {self.expected_fname!r})")
            if self.status_callback:
                self.status_callback(f"Falscher Titel – überspringe: {filename}", "warning")
            return False

        self.output_dir.mkdir(parents=True, exist_ok=True)
        outpath    = self.output_dir / filename
        resume_pos = outpath.stat().st_size if outpath.exists() else 0

        if resume_pos > 0 and resume_pos < filesize:
            # DCC RESUME: Info speichern, IRC-Loop sendet das RESUME-Kommando
            log.info(f"  Teil-Datei gefunden ({resume_pos//1024//1024}MB) – DCC RESUME wird ausgehandelt")
            self._dcc_resume = (ip, port, filename, filesize, resume_pos)
            return None  # Signal an IRC-Loop: RESUME senden und auf ACCEPT warten
        elif resume_pos >= filesize:
            log.info(f"  Datei bereits vollständig vorhanden, überspringe")
            self.success = True
            return True

        self.done = True
        return self._do_transfer(ip, port, filename, filesize, resume_pos=0)

    def download(self) -> bool:
        _rate_limit(self.server, self.status_callback)
        log.info(f"Verbinde mit {self.server}:{self.port} als {self.nick}")
        try:
            self.sock = socket.create_connection((self.server, self.port), timeout=30)
            if self.tls:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                self.sock = ctx.wrap_socket(self.sock, server_hostname=self.server)
            self.sock.settimeout(5)
        except Exception as e:
            log.error(f"Verbindung fehlgeschlagen: {e}")
            if self.status_callback:
                self.status_callback(f"Verbindung fehlgeschlagen: {e}", "error")
            return False

        if self.sasl_user and self.sasl_pass:
            self._send("CAP LS 302")
        self._send(f"NICK {self.nick}")
        self._send(f"USER {self.nick} 0 * :{self.nick}")

        registered    = False
        joined        = False
        names_done    = False
        request_sent  = False
        channel_nicks: set[str] = set()
        deadline      = time.time() + self.timeout

        try:
            for line in self._recv_lines():
                if time.time() > deadline:
                    log.error("Timeout - kein Download gestartet")
                    if self.status_callback:
                        self.status_callback("Timeout – Bot hat nicht geantwortet", "error")
                    break

                log.debug(f"<<< {line}")
                if self.irc_callback and not _IRC_DL_NOISE_RE.search(line):
                    self.irc_callback(f"<< {line}")

                # PING/PONG
                if line.startswith("PING"):
                    self._send(f"PONG {line.split(':', 1)[-1]}")
                    continue

                # Server-seitige Trennung (Ban, K-Line, etc.)
                if line.startswith("ERROR"):
                    msg = line.split(":", 1)[-1].strip() if ":" in line else line
                    log.error(f"IRC ERROR: {msg}")
                    if self.status_callback:
                        self.status_callback(f"IRC: {msg}", "error")
                    break

                # SASL-Verhandlung (nur aktiv wenn sasl_user/sasl_pass gesetzt)
                if not self.sasl_done:
                    if re.search(r'\bCAP\b.*\bLS\b', line):
                        self._send("CAP REQ :sasl")
                        continue
                    if re.search(r'\bCAP\b.*\bACK\b', line) and "sasl" in line.lower():
                        self._send("AUTHENTICATE PLAIN")
                        continue
                    if re.search(r'\bCAP\b.*\bNAK\b', line):
                        self.sasl_done = True
                        self._send("CAP END")
                        continue
                    if line.strip() == "AUTHENTICATE +":
                        cred = f"{self.sasl_user}\0{self.sasl_user}\0{self.sasl_pass}".encode()
                        self._send(f"AUTHENTICATE {base64.b64encode(cred).decode()}")
                        continue
                    if re.match(r'^\S+\s+903\s', line):
                        self.sasl_done = True
                        self._send("CAP END")
                        continue
                    if re.match(r'^\S+\s+90[4-7]\s', line):
                        self.sasl_done = True
                        log.warning(f"SASL-Login fehlgeschlagen fuer {self.nick}@{self.server}")
                        if self.sasl_fail_callback:
                            self.sasl_fail_callback()
                        self._send("CAP END")
                        continue

                parts = line.split()
                if len(parts) < 2:
                    continue

                code = parts[1]

                # 001 = Registered
                if code == "001" and not registered:
                    registered = True
                    for ec in self.extra_channels:
                        self._send(f"JOIN {ec}")
                    self._send(f"JOIN {self.channel}")

                # 353 = NAMES-Liste (Channel-Mitglieder)
                elif code == "353" and self.channel.lower() in line.lower():
                    nicks_part = line.split(":", 2)[-1]
                    for n in nicks_part.strip().split():
                        channel_nicks.add(n.lstrip("@+~&%").lower())

                # 366 = End of NAMES
                elif code == "366" and self.channel.lower() in line.lower() and not names_done:
                    names_done = True
                    if not self.skip_names_check:
                        bot_found = (
                            self.bot in channel_nicks or
                            any(nick.endswith(self.bot) for nick in channel_nicks)
                        )
                        if not bot_found:
                            if self.status_callback:
                                self.status_callback(
                                    f"Bot '{self.bot}' nicht im Channel", "warning"
                                )
                            break

                # JOIN bestätigt (eigener Channel)
                elif "JOIN" in line and self.nick.lower() in line.lower() \
                        and self.channel.lower() in line.lower() and not joined:
                    joined = True
                    if self.skip_names_check:
                        # NAMES überspringen – Anfrage sofort stellen
                        names_done = True
                    else:
                        self._send(f"NAMES {self.channel}")

                # Anfrage senden sobald NAMES-Prüfung durch
                elif joined and names_done and not request_sent:
                    cmd = f"xdcc send #{self.pack}"
                    if self.status_callback:
                        self.status_callback(f"/msg {self.bot} {cmd}", "info")
                    time.sleep(1)
                    self._send(f"PRIVMSG {self.bot} :{cmd}")
                    request_sent = True
                    deadline = time.time() + self.timeout

                # DCC SEND vom Bot
                elif "PRIVMSG" in line and "DCC SEND" in line:
                    sender = line.split("!")[0].lstrip(":").lower()
                    if sender == self.bot or self.bot in line.lower():
                        dcc_part = line.split("DCC SEND", 1)[1]
                        result = self._handle_dcc("DCC SEND" + dcc_part)
                        if result is None and self._dcc_resume:
                            _, port, filename, _, resume_pos = self._dcc_resume
                            self._send(f"PRIVMSG {self.bot} :\x01DCC RESUME {filename} {port} {resume_pos}\x01")
                        elif result is not None:
                            self.done = True
                            return result

                # Bot schickt oft via NOTICE
                elif "NOTICE" in line and "DCC SEND" in line:
                    dcc_part = line.split("DCC SEND", 1)[1]
                    result = self._handle_dcc("DCC SEND" + dcc_part)
                    if result is None and self._dcc_resume:
                        _, port, filename, _, resume_pos = self._dcc_resume
                        self._send(f"PRIVMSG {self.bot} :\x01DCC RESUME {filename} {port} {resume_pos}\x01")
                    elif result is not None:
                        self.done = True
                        return result

                # CTCP DCC
                elif "\x01DCC SEND" in line:
                    dcc_part = line.split("\x01DCC SEND", 1)[1].rstrip("\x01")
                    result = self._handle_dcc("DCC SEND" + dcc_part)
                    if result is None and self._dcc_resume:
                        _, port, filename, _, resume_pos = self._dcc_resume
                        self._send(f"PRIVMSG {self.bot} :\x01DCC RESUME {filename} {port} {resume_pos}\x01")
                    elif result is not None:
                        self.done = True
                        return result

                # DCC ACCEPT (Antwort auf DCC RESUME)
                elif "\x01DCC ACCEPT" in line and self._dcc_resume:
                    m = re.search(r"DCC ACCEPT\s+\S+\s+(\d+)\s+(\d+)", line)
                    if m:
                        confirmed_pos = int(m.group(2))
                        ip, port, filename, filesize, _ = self._dcc_resume
                        log.info(f"  DCC ACCEPT: Resume ab {confirmed_pos//1024//1024}MB bestätigt")
                        self.done = True
                        return self._do_transfer(ip, port, filename, filesize, confirmed_pos)

                # NOTICE vom Bot (Queue, Wartezeit, etc.)
                elif request_sent and "NOTICE" in line and self.bot in line.lower():
                    notice_text = line.split(":", 2)[-1].strip()
                    if self.status_callback:
                        self.status_callback(notice_text, "info")

                    # Ungültige Pack-Nummer → sofort abbrechen statt Timeout abwarten
                    if re.search(r"invalid pack", notice_text, re.IGNORECASE):
                        break

                    if re.search(r"\bqueue\b", notice_text, re.IGNORECASE):
                        deadline = time.time() + 600  # Warteschlange = mehr Zeit

                    # Bot kündigt Datei an → nur überspringen wenn vollständig vorhanden
                    m = re.search(r'Sending you pack[^"]*"([^"]+)"[^,]*,\s*which is\s*([\d.]+)\s*([KMGT]?B)',
                                  notice_text, re.IGNORECASE)
                    if not m:
                        m2 = re.search(r'Sending you pack[^"]*"([^"]+)"', notice_text, re.IGNORECASE)
                        if m2:
                            announced = m2.group(1)
                            announced_size = None
                        else:
                            announced = None
                            announced_size = None
                    else:
                        announced = m.group(1)
                        val = float(m.group(2))
                        unit = m.group(3).upper()
                        announced_size = int(val * {"KB": 1024, "MB": 1024**2, "GB": 1024**3,
                                                    "TB": 1024**4, "B": 1}.get(unit, 1))

                    if announced:
                        local_path = self.output_dir / announced
                        exists_in_staging = local_path.exists()
                        exists_elsewhere  = (self.file_exists_callback and
                                             self.file_exists_callback(announced))

                        # Unvollständige Datei → nicht überspringen, DCC RESUME greift
                        if exists_in_staging and announced_size:
                            local_size = local_path.stat().st_size
                            if local_size < announced_size * 0.99:
                                log.info(f"Staging-Datei unvollständig ({local_size} < {announced_size}), DCC RESUME wird versucht")
                                exists_in_staging = False

                        if exists_in_staging or exists_elsewhere:
                            if self.status_callback:
                                self.status_callback(
                                    f"Datei bereits vorhanden – überspringe: {announced}", "warning"
                                )
                            self._skip_dcc_fname = announced

                # Queue-Meldungen vom Bot (fallback)
                elif request_sent and re.search(r"\bqueue\b", line, re.IGNORECASE):
                    bot_msg = line.split(":", 2)[-1].strip()
                    if self.status_callback:
                        self.status_callback(bot_msg, "info")
                    deadline = time.time() + 600

                # scenep2p: 120s Wartezeit nach Connect erzwungen
                elif code == "531":
                    if self.status_callback:
                        self.status_callback("Server verweigert MSG – 120s Wartezeit nötig, überspringe", "error")
                    break

                # Banned oder Error
                elif code in ("474", "473", "475", "465"):
                    banned_ch = parts[3] if len(parts) > 3 else ""
                    msg = line.split(":", 2)[-1].strip() if line.count(":") >= 2 else line
                    if banned_ch.lower() != self.channel.lower():
                        # Ban auf Extra-Channel (z.B. #ZW-CHAT) → weiter ohne diesen Channel
                        log.warning(f"Extra-Channel {banned_ch} gesperrt, fahre ohne ihn fort")
                        continue
                    log.error(f"Channel/Server-Fehler {code}: {msg}")
                    if self.status_callback:
                        self.status_callback(f"Gesperrt ({code}): {msg[:80]}", "error")
                    break

                # Ban-NOTICE vor der Registrierung (z.B. Rizon G-Line)
                elif "NOTICE" in line and "banned" in line.lower() and not registered:
                    msg = line.split(":", 2)[-1].strip()
                    if self.status_callback:
                        self.status_callback(f"Gesperrt: {msg[:80]}", "error")
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
                  output_dir: Path, port: int = 6667, timeout: int = 180,
                  extra_channels: list = None,
                  progress_callback=None, status_callback=None,
                  stall_callback=None,
                  skip_names_check: bool = False,
                  file_exists_callback=None,
                  expected_fname: str = "",
                  irc_callback=None, tls: bool = False,
                  sasl_user: str = "", sasl_pass: str = "",
                  sasl_fail_callback=None) -> tuple[bool, str | None]:
    """Gibt (success, actual_filename) zurück."""
    d = XDCCDownloader(server, port, channel, bot, pack, output_dir, timeout,
                       extra_channels=extra_channels,
                       progress_callback=progress_callback,
                       status_callback=status_callback,
                       stall_callback=stall_callback,
                       skip_names_check=skip_names_check,
                       expected_fname=expected_fname,
                       irc_callback=irc_callback,
                       tls=tls,
                       sasl_user=sasl_user,
                       sasl_pass=sasl_pass,
                       sasl_fail_callback=sasl_fail_callback)
    d.file_exists_callback = file_exists_callback
    success = d.download()
    return success, d.filename
