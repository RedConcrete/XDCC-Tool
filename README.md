# XDCC Tool

Automatischer Downloader für Serien, Anime und Filme über IRC (XDCC). Sucht Packs im BEAST-Netzwerk auf Abjects, lädt sie per DCC herunter, entpackt Archive und sortiert alles automatisch in eine Jellyfin-kompatible Ordnerstruktur.

---

## Features

- Suche direkt via IRC im `#beast-xdcc` Channel
- Eigener DCC-Client (kein HexChat nötig)
- VPN-Pflicht via Mullvad (konfigurierbar)
- Automatisches Entpacken (TAR, ZIP)
- Jellyfin-konformes Umbenennen der Dateien
- Daemon-Modus (alle 30 Minuten prüfen)
- Bereits heruntergeladene Titel werden übersprungen

---

## Voraussetzungen

- Docker & Docker Compose
- Mullvad VPN Account (optional, deaktivierbar)

---

## Setup

### 1. Repository klonen

```bash
git clone https://github.com/RedConcrete/XDCC-Tool.git
cd XDCC-Tool
```

### 2. .env Datei erstellen

```bash
cp .env.example .env
```

Dann die Werte in `.env` eintragen:

```env
MULLVAD_PRIVATE_KEY=dein_wireguard_private_key
MULLVAD_ADDRESS=deine_wireguard_adresse/32
```

> VPN deaktivieren: In `docker-compose.yml` `VPN_REQUIRED=false` setzen.

### 3. Wishlist anlegen

```bash
cp wishlist.example.md wishlist.md
```

Wishlist bearbeiten:

```markdown
# XDCC Wunschliste

## Serien
- Breaking Bad S01
- The Office S02

## Anime
- Solo Leveling S01
- Demon Slayer S03

## Filme
- Inception 2010
- The Dark Knight 2008
```

**Regeln:**
- Kategorie bestimmt den Zielordner (`serien/` oder `movies/`)
- Staffelnummer immer angeben: `S01`, `S02` usw.
- Bei Filmen: Jahreszahl für bessere Treffer
- Sprache wird automatisch Deutsch gesucht

### 4. Docker starten

```bash
docker compose up -d
```

---

## Nutzung

### Einmalig ausführen

```bash
docker compose run --rm xdcc
```

### Daemon-Modus (alle 30 Min)

In `docker-compose.yml` den Command anpassen:

```yaml
command: ["python3", "/app/downloader.py", "--watch"]
```

### Logs

```bash
docker logs xdcc-downloader -f
```

---

## Ordnerstruktur nach dem Download

**Serien/Anime:**
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

**„0 Treffer" für einen Eintrag**
- Titel anders schreiben
- Staffelnummer prüfen
- Pack ggf. noch nicht auf dem Server verfügbar

**Eintrag nochmal herunterladen**
```bash
sed -i '/Titel S01/d' downloaded.txt
```

---

## Umgebungsvariablen

| Variable | Standard | Beschreibung |
|----------|----------|--------------|
| `WISHLIST` | `/app/wishlist.md` | Pfad zur Wishlist |
| `DOWNLOAD_DIR` | `/downloads` | Zielverzeichnis |
| `DONE_LOG` | `/app/downloaded.txt` | Log erledigter Einträge |
| `SEARCH_LANG` | `German` | Sprache für die Suche |
| `VPN_REQUIRED` | `true` | VPN-Pflicht ein/aus |
| `CHECK_INTERVAL` | `1800` | Intervall im Watch-Modus (Sekunden) |
