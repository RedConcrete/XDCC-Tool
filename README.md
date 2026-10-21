# XDCC Tool

Automatischer Downloader für Serien und Filme über IRC (XDCC) mit Web-UI.
Sucht Packs live über IRC-Suchbots (z. B. `databeast` im BEAST-Netzwerk auf
Abjects), lädt sie per eigenem DCC-Client herunter, entpackt Archive und
sortiert alles automatisch in eine Jellyfin-kompatible Ordnerstruktur.

---

## Features

- Web-UI (Port 5005) zum Bearbeiten der Wishlist und Starten von Läufen mit Live-Log
- Live-Suche über IRC-Suchbots (z. B. `!s <query>` an `databeast` in `#beast-chat`) statt veralteter Web-Caches
- Fallback-Suche über xdcc.eu für Netzwerke ohne eigenen Suchbot
- Eigener DCC-Client (kein HexChat nötig), inkl. Resume bei Verbindungsabbruch
- Titel-Abgleich verhindert Downloads falscher Dateien
- Automatisches Entpacken (TAR, ZIP)
- Jellyfin-konformes Umbenennen/Sortieren der Dateien
- Bereits heruntergeladene Titel werden übersprungen (und können im Web-UI wieder entfernt werden)

---

## Voraussetzungen

- Docker & Docker Compose

VPN-Unterstützung via Mullvad/Gluetun ist eingerichtet und standardmäßig
**aktiv**. Solange sie aktiv ist, läuft `xdcc` im Netzwerk-Namensraum von
`gluetun` (`network_mode: service:gluetun`, siehe
`docker-compose.vpn.yml`). Das hat eine wichtige Konsequenz:

> **Container immer mit beiden Compose-Dateien starten/neu erstellen**,
> sonst fällt die VPN-Kopplung raus:
> ```bash
> docker compose -f docker-compose.yml -f docker-compose.vpn.yml up -d
> ```

VPN-Konfiguration (Key, Adresse, Server, Config-Upload, Deaktivieren)
erfolgt komplett über das Web-UI unter dem Zahnrad (⚙) oben rechts.

---

## Setup

### 1. Repository klonen

```bash
git clone https://github.com/RedConcrete/XDCC-Tool.git
cd XDCC-Tool
```

### 2. Wishlist anlegen

```bash
cp wishlist.example.md wishlist.md
```

Die Wishlist kann danach komplett über das Web-UI gepflegt werden.

**Regeln:**
- Kategorie bestimmt den Zielordner (`serien/` oder `movies/`)
- Staffelnummer als „Staffel NN“ oder „Season NN“ angeben, wird automatisch
  in `SNN` für die Suche umgewandelt
- Bei Filmen: Jahreszahl für bessere Treffer angeben
- Titel sollten möglichst der Schreibweise der Release-Namen entsprechen
  (z. B. „Greys Anatomy“ statt „Grey's Anatomy“), da sonst der Titel-Abgleich
  den Download als falsche Datei verwirft

### 3. Channels konfigurieren (optional)

`channels.json` enthält die IRC-Server/Channels inkl. Suchbot-Konfiguration.
Ohne eigene `channels.json` wird eine Standardkonfiguration für
`irc.abjects.net` (`#beast-xdcc` / Suchbot `databeast` in `#beast-chat`)
verwendet.

Channels, die einen registrierten Nick verlangen (z.B. `#moviegods`, wo man
zum Laden in `#mg-chat` sein muss, das nur registrierte Nicks zulaesst),
bekommen in `channels.json` `"login": true`. Den Account pro Netzwerk
traegst du im Web-UI unter **Menue -> Accounts** ein oder registrierst ihn
dort direkt bei NickServ. Gespeichert wird er in `.env` als
`<NETZWERK>_SASL_USER` / `<NETZWERK>_SASL_PASS` (z.B. `ABJECTS_SASL_USER`),
nie in `channels.json`. **Ohne Account werden diese Channels in der Suche
ausgeblendet.**

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
Web-UI-Dropdown „Zielfestplatte" (in der Download-Karte) — im Code
(`scripts/downloader.py`) muss dafür nichts mehr angepasst werden.

Eine komplett neue physische Platte erfordert zusätzlich, dass sie
überhaupt erst an den Container/LXC durchgereicht wird (z. B. per
`pct set <ID> -mp<N> /mnt/neue-platte,mp=/mnt/neue-platte` auf dem
Proxmox-Host) — das ist ein einmaliger, sicherheitsrelevanter Schritt, der
sich nicht aus dem Web-UI heraus automatisieren lässt.

### 5. Docker starten

```bash
docker compose -f docker-compose.yml -f docker-compose.vpn.yml up -d
```

(Ohne aktives VPN reicht `docker compose up -d xdcc` mit nur der Basis-Datei.)

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
3. Im Web-UI ist die neue Platte sofort im Dropdown „Zielfestplatte"
   auswählbar — kein Code ändern nötig.

**Nur zwischen bereits eingebundenen Platten wechseln** braucht dagegen gar
keinen Neustart — einfach im Dropdown auswählen, wirkt sofort.

---

## Nutzung

Web-UI unter `http://<host>:5005` öffnen:

- **Zahnrad (⚙, oben rechts)**: Einstellungen — aktuell VPN-Konfiguration
  (Key/Adresse/Server, Config-Upload, Deaktivieren)
- **VPN-Karte**: zeigt nur den Live-Status (verbunden/nicht verbunden +
  öffentliche IP), keine Konfiguration
- **Wunschliste**: Serien/Filme/Merkliste bearbeiten und speichern
- **Download**: Zielfestplatte auswählen, „Speichern & Abarbeiten“ startet
  einen Lauf, Live-Log und Fortschrittsbalken zeigen den aktuellen Stand
  (per „Stopp“ abbrechbar)
- **Bereits geladen**: Liste erfolgreich geladener Titel, filterbar; über
  „Entfernen“ kann ein Titel wieder aus der Liste genommen werden, damit er
  beim nächsten Lauf erneut gesucht wird

### Logs

```bash
docker logs xdcc-downloader -f
```

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

## Troubleshooting

**„nichts gefunden" für einen Eintrag**
- Titel anders schreiben (näher an der Release-Schreibweise)
- Staffelnummer/Jahr prüfen
- Pack ggf. noch nicht auf dem Server verfügbar

**„Falscher Titel – überspringe"**
- Der Bot hat eine Datei gesendet, deren Name nicht zum Wishlist-Titel passt
  (Sicherheitsfeature). Meist hilft es, den Wishlist-Titel an die tatsächliche
  Release-Schreibweise anzupassen.

**Eintrag nochmal herunterladen**
- Im Web-UI unter „Bereits geladen" auf „Entfernen" klicken, dann erneut
  „Wishlist abarbeiten" ausführen.

---

## Umgebungsvariablen

| Variable | Standard | Beschreibung |
|----------|----------|--------------|
| `WISHLIST` | `/app/wishlist.md` | Pfad zur Wishlist |
| `TARGETS_ROOT` | `/targets` | Wurzelverzeichnis, unter dem die Zielplatten gemountet sind |
| `TARGET_STATE_FILE` | `/app/download_target.json` | Datei mit aktuell gewählter Zielplatte (über Web-UI änderbar) |
| `DONE_LOG` | `/app/downloaded.txt` | Log erledigter Einträge |
| `CHANNELS_CONFIG` | `/app/channels.json` | Pfad zur Channel-/Suchbot-Konfiguration |
| `SEARCH_LANG` | `German` | Bevorzugte Sprache für die Suche |
| `GLUETUN_CONTROL_URL` | `http://gluetun:8000` | Basis-URL von Gluetuns Control-API für den VPN-Status |
| `GLUETUN_API_KEY` | – | API-Key für Gluetuns Control-API (aus `.env`) |
| `ENV_FILE` | `/app/.env` | Datei, in die die Web-UI VPN-Zugangsdaten schreibt |
