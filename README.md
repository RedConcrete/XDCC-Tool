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

VPN-Unterstützung via Mullvad/Gluetun ist im `docker-compose.yml` vorbereitet,
aber standardmäßig deaktiviert (`restart: "no"`, kein `network_mode` auf
`xdcc` gesetzt). Bei Bedarf siehe Kommentar in `docker-compose.yml`.

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

### 4. Docker starten

```bash
docker compose up -d xdcc
```

---

## Nutzung

Web-UI unter `http://<host>:5005` öffnen:

- **Wunschliste**: Serien/Filme/Merkliste bearbeiten und speichern
- **Download**: „Wishlist abarbeiten“ startet einen Lauf, Live-Log und
  Fortschrittsbalken zeigen den aktuellen Stand
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
| `DOWNLOAD_DIR` | `/downloads` | Zielverzeichnis |
| `DONE_LOG` | `/app/downloaded.txt` | Log erledigter Einträge |
| `CHANNELS_CONFIG` | `/app/channels.json` | Pfad zur Channel-/Suchbot-Konfiguration |
| `SEARCH_LANG` | `German` | Bevorzugte Sprache für die Suche |
