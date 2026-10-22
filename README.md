# Ebay Watcher

Ein kleines Web-Tool für die eigene DiskStation. Es überwacht eBay-Gesuche und schickt über ntfy eine Push-Nachricht,

- wenn eine Auktion kurz vor Ende (Standard: 10 Minuten) noch unter deinem Höchstpreis liegt,
- wenn ein Sofortkauf-Angebot unter deinem Höchstpreis neu auftaucht.

Es nutzt die offizielle eBay Browse API und läuft als einzelner Docker-Container ohne weitere Dienste.

## Was es kann

- Beliebig viele Gesuche mit Suchbegriff, Ausschlusswörtern, Angebotsformat (Auktion, Sofortkauf, beides), Standort (Deutschland, EU, weltweit) und Zustand
- Höchstpreis wahlweise mit oder ohne Versandkosten, Option „nur versandkostenfrei“
- Es werden nur Angebote berücksichtigt, die nach Deutschland versendet werden; bei Angeboten außerhalb der EU weist die Benachrichtigung auf möglichen Zoll hin
- Angebote nur zur Abholung: im Umkreis deiner Postleitzahl, alle mit Entfernung markiert, oder ausgeblendet
- Startseite mit allen Gesuchen als Karten oder Liste, hell oder dunkel nach Systemeinstellung
- Sicherer Login: Argon2-Passwort-Hash, Sperre nach Fehlversuchen, „Angemeldet bleiben“ für 3 Monate, optional Zwei-Faktor per Authenticator-App, Übersicht und Abmeldung aller Geräte

So arbeitet Ebay Watcher: Das Tool sucht im eingestellten Intervall (Standard 15 Minuten) nach neuen Angeboten und merkt sich jede Auktion mit ihrer Endzeit. Genau X Minuten vor Ende fragt es den aktuellen Preis einmal ab und meldet sich, wenn er unter deinem Limit liegt. Dadurch braucht es nur wenige API-Abfragen.

## Projektaufbau

```
EbayWatcher/
├── source/                    ← hier wird gearbeitet
│   └── app/
│       ├── *.py               Python-Backend (FastAPI)
│       ├── templates/         Seiten (Jinja) mit Tailwind-Klassen
│       │   └── includes/      eingebundene Teile: ui.html (Buttons, Felder, Schalter …), offer.html
│       └── static/
│           ├── css/app.css    Tailwind-Quelle: Farben, Schriften, Dunkelmodus
│           ├── js/app.js
│           └── favicon.svg
├── build/                     ← erzeugt CodeKit, nie von Hand ändern
│   └── app/                   genau das kommt ins Docker-Image
│       └── static/            css/app-min.css und js/app-min.js sind die fertigen Dateien
├── docker/                    Dockerfile, docker-compose.yml, requirements.txt …
├── scripts/check_build.py     prüft, ob build/ zu source/ passt
├── .github/workflows/         GitHub baut daraus das Image
├── config.codekit3            CodeKit-Projekt (nur über CodeKit ändern)
├── package.json               Tailwind-Pakete für CodeKit
└── postcss.config.mjs         sagt CodeKit, dass Tailwind das CSS übersetzt
```

Drei Regeln:

1. **Geändert wird nur in `source/`** (und an den Projektdateien außerhalb davon).
2. **CodeKit spiegelt `source/` nach `build/`.** Python-Dateien und Templates werden unverändert kopiert. `app.css` übersetzt Tailwind zu `app-min.css`, `app.js` wird zu `app-min.js` verkleinert.
3. **`build/` wird mit committet.** GitHub baut das Docker-Image aus `build/app`, also aus genau dem, was CodeKit erzeugt hat.

Dateinamen mit `_` am Anfang behandelt CodeKit als Teildatei und kopiert sie nicht. Deshalb liegen eingebundene Templates im Ordner `includes/` statt `_ui.html`, und `app/` kommt ohne `__init__.py` aus (Python findet das Paket trotzdem).

## Arbeiten mit CodeKit

### Einmal einrichten

1. **Projektdateien von CodeKit fernhalten.** CodeKit verarbeitet alles im Projektordner, auch Dateien außerhalb von `source/`, und legt sie sonst in `build/` ab.
   - In den Projekteinstellungen bei den übersprungenen Ordnern (*Skipped Folders*) `/docker`, `/scripts` und `/.github` ergänzen.
   - In der Dateiliste `README.md`, `package.json`, `.gitignore` (und später `package-lock.json`) auf die Ausgabeaktion *Ignore* stellen.
2. **Tailwind installieren.** In CodeKit im Bereich *Packages* die Pakete aus `package.json` installieren (oder im Terminal im Projektordner `npm install`). Danach gibt es `node_modules/` (bleibt außerhalb von Git) und `package-lock.json` (kommt ins Repository).
3. **Autoprefixer für `app.css` ausschalten** (optional). Tailwind ergänzt Browser-Präfixe selbst.
4. **`build/` nicht ignorieren lassen.** In `.gitignore` darf keine Zeile `build` stehen. Falls CodeKit eine einträgt, die Option dafür in den Projekteinstellungen ausschalten.
5. **Einmal das ganze Projekt bauen.** Danach liegen in `build/` nur noch der Ordner `app` mit unter anderem `main.py`, `templates/`, `static/css/app-min.css` und `static/js/app-min.js`. Zur Kontrolle im Terminal: `python3 scripts/check_build.py`.
6. **Repository anlegen.** In GitHub Desktop *Add Local Repository*, den Ordner `EbayWatcher` wählen, das Repository erstellen und veröffentlichen.

### Jede Änderung

1. In `source/` ändern. CodeKit baut die geänderte Datei sofort nach `build/`.
2. **Neue Tailwind-Klassen in einem Template?** Dann muss auch `app.css` neu übersetzt werden, denn Tailwind erzeugt nur Klassen, die es in den Templates findet. Am sichersten vor jedem Commit einmal das ganze Projekt bauen.
3. In GitHub Desktop `source/` und `build/` zusammen committen und pushen.
4. GitHub prüft mit `scripts/check_build.py`, ob `build/` aktuell ist, und baut dann das Image. Fehlt ein Build, bricht die Action mit einer verständlichen Meldung ab.
5. In Portainer den Stack aktualisieren (siehe [Updates](#updates)).

### Tailwind in diesem Projekt

- **Farben und Größen** stehen als Tokens in `app.css` und heißen nach ihrer Aufgabe: `bg-surface`, `text-muted`, `border-line`, `text-meta`, `rounded-control` …
- **Dunkelmodus:** `app.css` tauscht nur die Farbwerte aus. In den Templates steht `dark:` nur dort, wo sich mehr als die Farbe ändert.
- **Karten und Liste:** Die Varianten `view-cards:` und `view-list:` gelten nur in der jeweiligen Ansicht der Übersicht.
- **Klassen immer ausschreiben.** Tailwind findet nur Klassen, die wörtlich in einer Datei stehen. `{{ 'text-over' if fehler }}` funktioniert, `text-{{ farbe }}` nicht.
- Wiederkehrende Klassenbündel stehen als Makros in `templates/includes/ui.html`, zum Beispiel `{{ ui.button('primary') }}`.

## Lokal testen (optional)

Mit Python 3.12 auf dem Mac, aus dem Projektordner:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r docker/requirements.txt
DATA_DIR=./data EBAY_CLIENT_ID=… EBAY_CLIENT_SECRET=… ADMIN_PASSWORD=test1234test \
  uvicorn app.main:app --app-dir build --reload --reload-dir build
```

Dann `http://localhost:8000` öffnen. Gestartet wird aus `build/`, also genau so wie im Container. Die Testdaten landen in `data/` und damit nie im Repository. Wer möchte, trägt in CodeKit `http://localhost:8000` als externen Server ein; dann lädt die CodeKit-Vorschau bei jeder Änderung neu.

## Auf der DiskStation einrichten

### 1. Image bei GitHub

Bei jedem Push auf `main` baut GitHub Actions automatisch das Docker-Image und legt es unter `ghcr.io/DEIN-GITHUB-NAME/ebay-watcher` ab. Den Fortschritt siehst du im Reiter **Actions**.

Damit die DiskStation das Image laden kann, gibt es zwei Wege:

- **Image öffentlich machen** (einfachster Weg, der Code enthält keine Geheimnisse): Auf GitHub unter deinem Profil **Packages** öffnen, `ebay-watcher` wählen, **Package settings**, ganz unten **Change visibility** auf *Public*.
- **Privat lassen:** In GitHub einen Personal Access Token mit dem Recht `read:packages` erzeugen und in Portainer unter **Registries** eine Registry `ghcr.io` mit deinem GitHub-Namen und diesem Token anlegen.

### 2. Datenordner auf der DiskStation anlegen

In der File Station unter `docker` einen Ordner `ebay-watcher` anlegen. Darin liegt später die Datenbank mit deinen Gesuchen und Einstellungen.

### 3. Stack in Portainer anlegen

**Stacks**, **Add stack**, Name `ebay-watcher`, **Web editor**. Den Inhalt von `docker/docker-compose.yml` einfügen und `DEIN-GITHUB-NAME` ersetzen.

Darunter bei **Environment variables** drei Variablen anlegen:

| Name | Wert |
| --- | --- |
| `EBAY_CLIENT_ID` | App ID aus dem eBay Developer Portal (Production) |
| `EBAY_CLIENT_SECRET` | Cert ID aus dem eBay Developer Portal (Production) |
| `ADMIN_PASSWORD` | dein Passwort für den ersten Login |

`PUID` und `PGID` bestimmen, wem der Datenordner gehört. Auf Synology ist der erste Benutzer meist `1026` in der Gruppe `100`. Per SSH zeigt `id` deine Werte.

**Deploy the stack.**

### 4. Anmelden und einrichten

Im Heimnetz oder per WireGuard `http://IP-DEINER-DS:8765` öffnen und mit dem Benutzernamen aus `ADMIN_USER` und deinem Passwort anmelden. Unter **Einstellungen**:

1. Postleitzahl und Umkreis für Abholung eintragen
2. ntfy-Adresse, Topic und Access-Token eintragen und **Testnachricht senden**
3. Unter **eBay-Verbindung** auf **Verbindung prüfen** tippen

Danach das erste Gesuch anlegen.

`ADMIN_PASSWORD` wird nur beim allerersten Start verwendet. Später änderst du das Passwort in den Einstellungen.

## Passwort vergessen

Im Portainer-Stack `ADMIN_PASSWORD` auf ein neues Passwort setzen (mindestens 10 Zeichen), zusätzlich `ADMIN_RESET` auf `true` setzen und den Stack aktualisieren. Beim Start wird das Passwort zurückgesetzt, die Zwei-Faktor-Anmeldung ausgeschaltet und alle Geräte werden abgemeldet. Danach `ADMIN_RESET` wieder entfernen.

## Updates

Änderung wie oben beschrieben bauen, committen und pushen, dann warten, bis die Action durch ist. Dann in Portainer den Stack öffnen, **Update the stack** wählen und dabei das erneute Laden des Images aktivieren. Die Daten im Ordner bleiben erhalten.

## HTTPS

Ebay Watcher muss nicht aus dem Internet erreichbar sein, also keine Portfreigabe in der FritzBox einrichten. Für HTTPS im Heimnetz:

1. Im DSM-Reverse-Proxy eine Regel anlegen (Quelle: HTTPS, ein eigener Hostname; Ziel: `localhost:8765`)
2. Ein passendes Zertifikat zuweisen
3. Ein Profil für die Zugriffskontrolle anlegen, das nur dein Heimnetz und das WireGuard-Netz erlaubt, und es der Regel zuweisen
4. Den Hostnamen im Pi-hole als lokalen DNS-Eintrag auf die IP der DiskStation zeigen lassen

Sobald die Seite per HTTPS aufgerufen wird, setzt Ebay Watcher seine Cookies automatisch mit dem Secure-Flag.

## Datenschutz

Ebay Watcher speichert nur Artikelnummer, Titel, Bild-Link, Preise, Versandkosten, Standort-Land und Endzeit der gefundenen Angebote. Daten über Verkäufer oder Käufer werden nicht gespeichert. Beendete Angebote werden nach sieben Tagen gelöscht.

## Umgebungsvariablen

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `EBAY_CLIENT_ID` | – | App ID (Production) |
| `EBAY_CLIENT_SECRET` | – | Cert ID (Production) |
| `ADMIN_USER` | `admin` | Benutzername beim ersten Start |
| `ADMIN_PASSWORD` | zufällig | Passwort beim ersten Start; fehlt es, steht ein zufälliges im Container-Log |
| `ADMIN_RESET` | – | `true` setzt beim Start das Passwort auf `ADMIN_PASSWORD` zurück und schaltet 2FA ab |
| `TZ` | `Europe/Berlin` | Zeitzone für die Anzeige |
| `PUID` / `PGID` | `1000` | Besitzer des Datenordners |
| `COOKIE_SECURE` | `auto` | `true` erzwingt Secure-Cookies, `auto` nur bei HTTPS |
| `EBAY_MARKETPLACE` | `EBAY_DE` | eBay-Marktplatz |
