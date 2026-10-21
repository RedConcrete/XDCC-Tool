# XDCC Tool

Downloader für Serien und Filme über IRC (XDCC) mit Web-UI.
Sucht Packs live über IRC-Suchbots (z. B. `databeast` im BEAST-Netzwerk auf
Abjects) und xdcc.eu, lädt sie per eigenem DCC-Client herunter, entpackt
Archive und sortiert alles automatisch in eine Jellyfin-kompatible
Ordnerstruktur — wahlweise automatisch über eine Wunschliste oder gezielt
über die manuelle Suche.

---

## Features

**Automatik (Wunschliste)**
- Serien/Filme in die Wunschliste eintragen, „Abarbeiten“ sucht, lädt und
  sortiert alles der Reihe nach
- Bewertet alle Treffer (Sprache, Qualität, Staffelpaket, Gets, Zuverlässigkeit
  des Channels …) und versucht den besten zuerst, bei Fehlern den nächsten
- Bereits geladene Titel werden übersprungen (im Menü „Geladen“ wieder
  freigebbar)

**Manuelle Suche**
- Suchbegriff + Kategorie eingeben, Treffer als Tabelle mit Größe, Bot/Pack,
  Netzwerk, Gets und Score
- Pro Treffer: **Laden** (genau dieses Pack), **+ Wunschliste**, **+ Merkliste**
- Filterfeld, Lade-Animation während der Suche, letzte Suche bleibt nach
  Neuladen erhalten

**Zuverlässigkeit**
- Live-Suche über IRC-Suchbots, Fallback auf xdcc.eu
- Eigener DCC-Client inkl. Resume bei Verbindungsabbruch
- Titel-Abgleich verhindert Downloads falscher Dateien; wird eine bestimmte
  Folge gesucht (`S04E05`), verdrängt kein Staffelpaket sie
- **Channel-Abzüge:** fehlgeschlagene Downloads und Bans kosten den Channel
  Punkte, die sich mit der Zeit abbauen — nicht funktionierende Channels
  rutschen automatisch nach hinten
- **IRC-Accounts:** Channels, die einen registrierten Nick verlangen, werden
  per SASL angemeldet; ohne Account werden sie in der Suche ausgeblendet
- Automatisches Entpacken (TAR, ZIP, RAR) und Jellyfin-konformes Umbenennen

**Oberfläche**
- Web-UI auf Port 5005, Desktop und Handy
- Die Seite selbst scrollt nicht, nur der Inhalt der Karten; auf dem Handy
  ist immer eine Karte offen (Akkordeon)
- Punkte-Gewichtung, Accounts und VPN direkt im Menü einstellbar

---

## Voraussetzungen

- Docker & Docker Compose

VPN-Unterstützung via Gluetun (WireGuard; Mullvad oder beliebiger Anbieter per
`.conf`-Upload) ist eingerichtet. Mit VPN läuft `xdcc` im
Netzwerk-Namensraum von `gluetun` (`network_mode: service:gluetun`, siehe
`docker-compose.vpn.yml`). Das hat eine wichtige Konsequenz:

> **Container immer mit beiden Compose-Dateien starten/neu erstellen**,
> sonst fällt die VPN-Kopplung raus:
> ```bash
> docker compose -f docker-compose.yml -f docker-compose.vpn.yml up -d
> ```

Die VPN-Zugangsdaten werden im Web-UI unter **Menü (☰) → VPN** gepflegt.

---

## Setup

### 1. Repository klonen

```bash
git clone https://github.com/RedConcrete/XDCC-Tool.git
cd XDCC-Tool
```

### 2. Wunschliste und `.env` anlegen

```bash
cp wishlist.example.md wishlist.md
cp .env.example .env
touch downloaded.txt Chat.txt
```

Die Wunschliste wird danach komplett über das Web-UI gepflegt. `.env` nimmt
VPN-Daten und IRC-Accounts auf, die das Web-UI dort selbst einträgt.

### 3. Channels konfigurieren (optional)

`channels.json` enthält die IRC-Server/Channels inkl. Suchbot-Konfiguration.
Ohne eigene `channels.json` wird eine Standardkonfiguration für
`irc.abjects.net` (`#beast-xdcc` / Suchbot `databeast` in `#beast-chat`)
verwendet.

| Feld | Bedeutung |
|------|-----------|
| `server`, `port`, `tls` | IRC-Server |
| `channel` | Channel, in dem die Bots die Packs anbieten |
| `search_channel`, `search_bot`, `search_cmd` | Live-Suche (`{query}` wird ersetzt); ohne Suchbot nur xdcc.eu |
| `extra_channels` | Channels, die zusätzlich betreten werden müssen (z. B. `#mg-chat` für `#moviegods`) |
| `login` | `true` = Channel verlangt einen registrierten Nick (siehe [IRC-Accounts](#irc-accounts)) |
| `lang` | Sprache des Channels (Info) |

Zugangsdaten stehen **nie** in `channels.json`, sondern in `.env`.

### 4. Zielplatte(n) einbinden

Jede Zielplatte wird in `docker-compose.yml` beim Service `xdcc` als eigener
Unterordner unter `/targets` gemountet:

```yaml
volumes:
  ...
  - /mnt/meine-platte/media:/targets/platte1
```

- **Links** vom `:` steht der echte Pfad auf dem Host/LXC
- **Rechts**, nach `/targets/`, ein frei wählbarer Name

Jeder hier eingetragene Ordner erscheint danach **automatisch** im
Ziel-Dropdown der Download-Karte — im Code muss dafür nichts angepasst werden.

Eine komplett neue physische Platte muss zusätzlich erst an den Container/LXC
durchgereicht werden (z. B. per
`pct set <ID> -mp<N> /mnt/neue-platte,mp=/mnt/neue-platte` auf dem
Proxmox-Host) — ein einmaliger, sicherheitsrelevanter Schritt, der sich nicht
aus dem Web-UI heraus automatisieren lässt.

### 5. Docker starten

```bash
docker compose -f docker-compose.yml -f docker-compose.vpn.yml up -d
```

(Ohne VPN reicht `docker compose up -d xdcc` mit nur der Basis-Datei.)

### 6. VPN-Neustart aus dem Web-UI (optional)

Nach dem Speichern neuer VPN-Daten schreibt das Web-UI eine Trigger-Datei
nach `restart_trigger/`. Den eigentlichen Neustart (`docker compose up -d`
mit oder ohne VPN-Kopplung, je nach `VPN_ENABLED`) führt
`host/vpn_watcher.py` aus, das **außerhalb von Docker** auf dem Host laufen
muss. Den Projektordner ermittelt es selbst (Elternordner von `host/`,
überschreibbar per `XDCC_COMPOSE_DIR`). Beispiel als systemd-Dienst:

```ini
# /etc/systemd/system/xdcc-vpn-watcher.service
[Unit]
Description=XDCC VPN-Restart-Watcher
After=docker.service
Requires=docker.service

[Service]
ExecStart=/usr/bin/python3 /pfad/zu/XDCC-Tool/host/vpn_watcher.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now xdcc-vpn-watcher
```

Läuft der Watcher nicht, nach VPN-Änderungen den Befehl aus Schritt 5 von
Hand ausführen. Achtung: Schlägt die VPN-Verbindung nach dem Neustart fehl,
schaltet der Watcher automatisch auf **Betrieb ohne VPN** zurück
(`VPN_ENABLED=false`), damit das Web-UI erreichbar bleibt.

---

## Weitere Zielplatte nachträglich hinzufügen

Läuft das Tool schon und der Host/LXC hat bereits Zugriff auf die neue
Platte (siehe oben):

1. In `docker-compose.yml` beim Service `xdcc` eine neue Zeile unter
   `volumes:` eintragen:
   ```yaml
   - /mnt/neue-platte/media:/targets/neuername
   ```
2. Container neu erstellen (mit VPN-Override, falls aktiv):
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.vpn.yml up -d --force-recreate xdcc
   ```
3. Im Web-UI ist die neue Platte sofort im Ziel-Dropdown auswählbar.

**Nur zwischen bereits eingebundenen Platten wechseln** braucht keinen
Neustart — einfach im Dropdown auswählen, wirkt sofort.

> Vor jedem Neustart prüfen, dass gerade kein Lauf und keine Suche aktiv
> ist (Download-Karte bzw. Suchkarte) — ein Neustart bricht laufende
> Downloads ab.

---

## Nutzung

Web-UI unter `http://<host>:5005` öffnen.

### Kopfzeile

- **VPN-Badge:** Punkt grün = VPN aktiv. Hover (Handy: Tippen) zeigt Status,
  öffentliche IP und hinterlegten Key.
- **Menü (☰):**

  | Punkt | Inhalt |
  |-------|--------|
  | Chat Log | Roher IRC-Verkehr aller Läufe und Suchen |
  | Läufe | Log-Dateien vergangener Läufe |
  | Geladen | Erfolgreich geladene Wunschlisten-Titel; „Entfernen“ gibt einen Titel für den nächsten Lauf frei |
  | Bewertung | Punkte-Gewichtung und Channel-Abzüge (siehe unten) |
  | Accounts | IRC-Accounts für Login-Channels (siehe unten) |
  | VPN | WireGuard-Key/Adresse/Server, `.conf`-Upload, VPN deaktivieren |

### Wunschliste

Drei Spalten: **Serien** und **Filme** werden beim Abarbeiten gesucht und
geladen, die **Merkliste** ist nur eine Notiz. „Speichern“ sichert alle drei.

**Regeln für Einträge:**
- Kategorie bestimmt den Zielordner (`serien/` oder `movies/`)
- Staffel als „Staffel NN“, „Season NN“ oder `SNN` angeben — wird für die
  Suche in `SNN` umgewandelt. `Overlord S04` bevorzugt das ganze
  Staffelpaket, `Overlord S04E05` genau diese Folge
- Bei Filmen: Jahreszahl für bessere Treffer angeben
- Titel möglichst in Release-Schreibweise (z. B. „Greys Anatomy“ statt
  „Grey's Anatomy“), sonst verwirft der Titel-Abgleich die Datei

### Download

**Abarbeiten** startet einen Lauf über alle offenen Wunschlisten-Einträge,
**Stopp** bricht ab. Daneben wählt **Ziel** die Zielplatte (mit freiem
Speicher). Live-Log und Fortschrittsbalken zeigen den aktuellen Stand;
derselbe Bereich zeigt auch manuelle Downloads.

### Manuelle Suche

Suchbegriff und Kategorie (Film/Serie) eingeben, **Suchen** (während der
Suche: **Abbrechen**). Die Treffer erscheinen nach Score sortiert; der beste
ist als „Top-Treffer“ markiert. Pro Treffer:

- **Laden:** lädt genau dieses Pack und sortiert es ein (ohne Ausweich-Bots;
  schlägt es fehl, einfach den nächsten Treffer nehmen)
- **+ Wunschliste / + Merkliste:** trägt einen aus dem Dateinamen
  abgeleiteten Titel ein (`Planet.Erde.III.S01.German…tar` →
  „Planet Erde III S01“); bereits eingetragene Titel zeigen „✓“

Suche und Lauf schließen sich gegenseitig aus. Sind Login-Channels mangels
Account ausgeblendet, steht das in der Statuszeile der Suche.

### Bewertung (Score)

Jeder Treffer bekommt Punkte; der höchste Score wird zuerst versucht. Alle
Werte sind unter **Menü → Bewertung** änderbar („Standard“ setzt zurück),
Treffer werden beim Speichern sofort neu sortiert. Abweichungen vom Standard
liegen in `score_config.json`.

| Kriterium | Standard |
|-----------|---------:|
| Deutsch (german, deutsch, .ger., DL …) | +1000 |
| Andere Sprache (nur wenn nicht deutsch) | −600 |
| Treffer aus der Live-IRC-Suche (statt xdcc.eu) | +300 |
| 1080p / BluRay / WEB (je Stichwort) | +30 |
| 720p | +15 |
| x265 / HEVC (je Stichwort) | +10 |
| Gets (+1 pro Get, maximal) | +100 |
| Serie: ganze Staffel (`S01`, „Staffel 1“, „Season 1“) | +700 |
| Serie: einzelne Folge (`S01E05`, `1x05`) | +500 |
| Serie: Komplettpaket ohne Staffelangabe (> 2 GB) | −800 |
| Film: Name sieht aus wie Serienfolge | −500 |
| Channel: fehlgeschlagener Download (je Versuch) | −200 |
| Channel: gesperrt (Ban, z. B. `474 +b`) | −1000 |
| Halbwertszeit der Channel-Abzüge | 7 Tage |

Dazu kommen ein paar Punkte für die Dateigröße. Faustregel: Score über 1000
heißt fast immer deutsche Fassung. Ob es der richtige Titel ist, bewertet
der Score nicht — dafür sorgen Suchbegriff, Zahlen-/Folgenabgleich und der
Titel-Abgleich beim Download.

### Channel-Abzüge

Jeder Download-Versuch wird pro Channel (Server + Channel) in
`channel_stats.json` festgehalten:

- Fehlversuch oder Ban → Abzug (siehe Tabelle)
- Abzüge halbieren sich nach der eingestellten Halbwertszeit
- ein erfolgreicher Download setzt den Channel zurück
- Abbruch per Stopp zählt nicht

Unter **Menü → Bewertung → Channel-Abzüge** stehen alle betroffenen Channels
mit Grund und Alter; einzeln oder komplett zurücksetzbar.

### IRC-Accounts

Manche Channels verlangen einen registrierten Nick (in `channels.json`
`"login": true`), z. B. `#moviegods`: dort muss man zum Laden in `#mg-chat`
sein, das nur registrierte Nicks zulässt (`477 You need a registered nick`).

Unter **Menü → Accounts** steht pro betroffenem Netzwerk eine Karte:

- **Nick + Passwort** hinterlegen (Passwort wird nie angezeigt; leer lassen
  behält das alte)
- **Testen:** Probe-Anmeldung per SASL
- **Entfernen:** löscht den Account aus dem Tool (der Nick bleibt beim
  Netzwerk registriert)
- **Neuen Nick registrieren:** Nick, E-Mail und optional Passwort (leer =
  wird erzeugt). Das Tool verbindet sich über das VPN, wartet die von
  NickServ verlangte Nutzungszeit ab und registriert den Nick. Bei Erfolg
  wird der Account direkt gespeichert.

Gespeichert wird in `.env` als `<NETZWERK>_SASL_USER` /
`<NETZWERK>_SASL_PASS` (z. B. `ABJECTS_SASL_USER`). Änderungen greifen sofort,
ohne Neustart. **Ohne Account werden Login-Channels übersprungen und ihre
Treffer ausgeblendet.** Ist der Nick gerade woanders online (z. B. in einem
IRC-Client), lädt das Tool mit einem Zufallsnick ohne Anmeldung weiter.

> Mit einer Wegwerf-E-Mail ist später kein Passwort-Reset möglich — das
> Passwort aus `.env` dann zusätzlich sicher aufbewahren.

### Logs

```bash
docker logs xdcc-downloader -f
```

IRC-Verkehr landet zusätzlich in `Chat.txt`, jeder Lauf in `logs/`.
Passwörter (SASL) werden dort nie mitgeschrieben.

---

## Ordnerstruktur nach dem Download

**Serien:**
```
serien/
└── Solo Leveling/
    └── Season 01/
        ├── Solo Leveling - S01E01.mkv
        └── Solo Leveling - S01E02.mkv
```

**Filme:**
```
movies/
└── Inception 2010/
    └── Inception 2010.mkv
```

---

## Dateien im Projektordner

| Datei | Inhalt | Im Git |
|-------|--------|:------:|
| `.env` | VPN-Daten, IRC-Accounts | nein |
| `wishlist.md` | Wunschliste | nein |
| `downloaded.txt` | Bereits geladene Titel | nein |
| `channels.json` | Server/Channels/Suchbots | ja |
| `score_config.json` | Abweichende Punkte (Menü → Bewertung) | ja, als `{}` |
| `channel_stats.json` | Channel-Abzüge | ja, als `{}` |
| `server_state.json` | Dauerhaft deaktivierte Server | ja, als `{}` |
| `download_target.json` | Gewählte Zielplatte | ja |
| `Chat.txt` | Mitschnitt des IRC-Verkehrs | nein |
| `logs/` | Log-Dateien der Läufe | nein |

Alle als Datei gemounteten Dateien müssen vor dem ersten Start existieren —
fehlt eine, legt Docker stattdessen einen Ordner an (daher `touch
downloaded.txt Chat.txt` in Schritt 2).

---

## Troubleshooting

**„nichts gefunden" für einen Eintrag**
- Titel anders schreiben (näher an der Release-Schreibweise)
- Staffelnummer/Jahr prüfen
- Pack ggf. noch nicht auf dem Server verfügbar
- Statuszeile der Suche prüfen: sind Login-Channels ausgeblendet?

**„Falscher Titel – überspringe"**
- Der Bot hat eine Datei gesendet, deren Name nicht zum Wunschlisten-Titel
  passt (Sicherheitsfeature). Meist hilft es, den Titel an die tatsächliche
  Release-Schreibweise anzupassen.

**`Gesperrt (474): Cannot join channel (+b)`**
- Der Channel hat dich gebannt — meist auf die IP/den Host (also die
  VPN-Adresse), nicht auf den Nick. Der Channel bekommt automatisch einen
  Abzug und rutscht nach hinten. Aufheben lässt sich ein Ban nur über die
  Channel-Betreiber (z. B. `#MG-Help` für `#moviegods`).

**`477 You need a registered nick to join that channel`**
- Der Channel braucht einen Account: in `channels.json` `"login": true`
  setzen und unter Menü → Accounts einen Nick hinterlegen oder registrieren.

**Eintrag nochmal herunterladen**
- Unter Menü → Geladen auf „Entfernen“ klicken, dann erneut „Abarbeiten“.

---

## Umgebungsvariablen

| Variable | Standard | Beschreibung |
|----------|----------|--------------|
| `WISHLIST` | `/app/wishlist.md` | Pfad zur Wunschliste |
| `TARGETS_ROOT` | `/targets` | Wurzelverzeichnis, unter dem die Zielplatten gemountet sind |
| `TARGET_STATE_FILE` | `/app/download_target.json` | Aktuell gewählte Zielplatte (über Web-UI änderbar) |
| `DONE_LOG` | `/app/downloaded.txt` | Bereits geladene Titel |
| `CHAT_LOG` | `/app/Chat.txt` | Mitschnitt des IRC-Verkehrs |
| `LOGS_DIR` | `/app/logs` | Log-Dateien der einzelnen Läufe |
| `CHANNELS_CONFIG` | `/app/channels.json` | Channel-/Suchbot-Konfiguration |
| `SCORE_CONFIG` | `/app/score_config.json` | Abweichende Punkte der Bewertung |
| `CHANNEL_STATS` | `/app/channel_stats.json` | Channel-Abzüge (Fehlversuche/Bans) |
| `SEARCH_LANG` | `German` | Bevorzugte Sprache für die Suche |
| `GLUETUN_CONTROL_URL` | `http://gluetun:8000` | Basis-URL von Gluetuns Control-API für den VPN-Status |
| `GLUETUN_API_KEY` | – | API-Key für Gluetuns Control-API (aus `.env`) |
| `ENV_FILE` | `/app/.env` | Datei, in die das Web-UI VPN-Daten und IRC-Accounts schreibt |
