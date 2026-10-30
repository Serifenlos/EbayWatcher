# Ebay Watcher

Ebay Watcher überwacht eBay-Gesuche und schickt über [ntfy](https://ntfy.sh) eine Push-Nachricht aufs Handy,

- wenn eine Auktion kurz vor Ende (Standard: 10 Minuten) noch unter deinem Höchstpreis liegt,
- wenn ein Sofortkauf-Angebot unter deinem Höchstpreis neu auftaucht.

Es nutzt die offizielle eBay Browse API und läuft als einzelner Docker-Container ohne weitere Dienste, zum Beispiel auf einem NAS.

> Ebay Watcher ist noch in Entwicklung (Version 0.x). Neue Versionen und Änderungen stehen unter [Releases](https://github.com/Serifenlos/EbayWatcher/releases).

## Was es kann

- Beliebig viele Gesuche mit Suchbegriff, Ausschlusswörtern, Angebotsformat (Auktion, Sofortkauf, beides), Standort (Deutschland, EU, weltweit) und Zustand
- Höchstpreis wahlweise mit oder ohne Versandkosten, Option „nur versandkostenfrei“
- Es werden nur Angebote berücksichtigt, die nach Deutschland versendet werden; bei Angeboten außerhalb der EU weist die Benachrichtigung auf möglichen Zoll hin
- Angebote nur zur Abholung: im Umkreis deiner Postleitzahl, alle mit Entfernung markiert, oder ausgeblendet
- Startseite mit allen Gesuchen als Karten oder Liste, hell oder dunkel nach Systemeinstellung
- Sicherer Login: Argon2-Passwort-Hash, Sperre nach Fehlversuchen, „Angemeldet bleiben“ für 3 Monate, optional Zwei-Faktor per Authenticator-App, Übersicht und Abmeldung aller Geräte

So arbeitet Ebay Watcher: Es sucht im eingestellten Intervall (Standard 15 Minuten) nach neuen Angeboten und merkt sich jede Auktion mit ihrer Endzeit. Genau X Minuten vor Ende fragt es den aktuellen Preis einmal ab und meldet sich, wenn er unter deinem Limit liegt. Dadurch braucht es nur wenige API-Abfragen.

## Voraussetzungen

- **Docker mit Docker Compose**, direkt oder über Portainer. Das Image gibt es für x86-64 (`linux/amd64`), nicht für ARM-Geräte wie den Raspberry Pi.
- **Ein Konto im [eBay Developer Program](https://developer.ebay.com)** mit einem *Production*-Keyset. Daraus brauchst du die **App ID** und die **Cert ID**. Ebay Watcher liest nur öffentliche Suchergebnisse; ein Login mit deinem eBay-Konto ist nicht nötig.
- **Ein ntfy-Server und die ntfy-App** auf dem Handy. Das kann eine eigene Instanz sein oder der öffentliche Server `https://ntfy.sh`.
- Ebay Watcher ist auf **eBay.de** ausgelegt: Lieferung nach Deutschland, deutsche Postleitzahlen, Preise in Euro.

## Installation

### Mit Docker Compose

```bash
mkdir ebay-watcher && cd ebay-watcher
curl -O https://raw.githubusercontent.com/Serifenlos/EbayWatcher/main/docker/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/Serifenlos/EbayWatcher/main/docker/.env.example
```

In der Datei `.env` mindestens `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` und `ADMIN_PASSWORD` eintragen, dann starten:

```bash
docker compose up -d
```

Die Datenbank liegt danach im Ordner `data` neben der Compose-Datei.

### Mit Portainer

1. Einen Ordner für die Daten anlegen, zum Beispiel `/volume1/docker/ebay-watcher` auf einer Synology.
2. **Stacks → Add stack**, Name `ebay-watcher`, **Web editor**.
3. Den Inhalt von [`docker/docker-compose.yml`](docker/docker-compose.yml) einfügen.
4. Darunter bei **Environment variables** eintragen:

   | Name | Wert |
   | --- | --- |
   | `EBAY_CLIENT_ID` | App ID (Production) |
   | `EBAY_CLIENT_SECRET` | Cert ID (Production) |
   | `ADMIN_PASSWORD` | Passwort für den ersten Login, mindestens 10 Zeichen |
   | `DATA_PATH` | der Datenordner aus Schritt 1 |

   Optional außerdem `ADMIN_USER` (Standard `admin`) sowie `PUID` und `PGID`, siehe [Umgebungsvariablen](#umgebungsvariablen).
5. **Deploy the stack.**

Ist Port 8765 schon belegt, in der Compose-Datei bei `8765:8000` die linke Zahl ändern.

## Erste Schritte

1. `http://ADRESSE-DEINES-SERVERS:8765` öffnen und mit `ADMIN_USER` und `ADMIN_PASSWORD` anmelden.
2. Unter **Einstellungen**:
   - Postleitzahl und Umkreis für Abholung eintragen
   - ntfy-Adresse, Topic und gegebenenfalls Access-Token eintragen und **Testnachricht senden**
   - unter **eBay-Verbindung** auf **Verbindung prüfen** tippen
3. Das erste Gesuch anlegen.

`ADMIN_PASSWORD` wird nur beim allerersten Start verwendet. Später änderst du das Passwort in den Einstellungen.

## Updates

Mit Docker Compose:

```bash
docker compose pull && docker compose up -d
```

In Portainer den Stack öffnen, **Update the stack** wählen und dabei das erneute Laden des Images aktivieren.

Deine Gesuche und Einstellungen bleiben im Datenordner erhalten. Was sich geändert hat, steht unter [Releases](https://github.com/Serifenlos/EbayWatcher/releases). Die laufende Version zeigt Ebay Watcher unter *Einstellungen → Über Ebay Watcher*.

Welche Version du bekommst, legst du über den Image-Tag in der Compose-Datei fest:

| Tag | Bedeutung |
| --- | --- |
| `latest` | immer die neueste Version |
| `0.1` | die neueste Version aus der Reihe 0.1.x |
| `0.1.3` | genau diese Version |

## Sicherheit und HTTPS

Ebay Watcher ist für das eigene Heimnetz gedacht. Gib den Port nicht im Router frei; von unterwegs erreichst du ihn am besten per VPN.

Für HTTPS kannst du einen Reverse Proxy davorschalten, etwa den von Synology DSM, Nginx Proxy Manager oder Caddy, mit Ziel `http://SERVER:8765`. Sobald die Seite per HTTPS aufgerufen wird, setzt Ebay Watcher seine Cookies automatisch mit dem Secure-Flag. Am besten erlaubt der Proxy nur Zugriffe aus dem eigenen Netz.

Bei einem öffentlichen ntfy-Server wie `ntfy.sh` kann jeder mitlesen, der den Topic-Namen kennt. Wähle dort einen schwer zu erratenden Namen oder nutze einen eigenen Server mit Access-Token.

## Passwort vergessen

`ADMIN_PASSWORD` auf ein neues Passwort setzen (mindestens 10 Zeichen), zusätzlich `ADMIN_RESET=true` setzen und den Container neu starten (`docker compose up -d` bzw. in Portainer **Update the stack**). Beim Start wird das Passwort zurückgesetzt, die Zwei-Faktor-Anmeldung ausgeschaltet und alle Geräte werden abgemeldet. Danach `ADMIN_RESET` wieder entfernen.

## Umgebungsvariablen

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `EBAY_CLIENT_ID` | – | App ID (Production) |
| `EBAY_CLIENT_SECRET` | – | Cert ID (Production) |
| `ADMIN_USER` | `admin` | Benutzername beim ersten Start |
| `ADMIN_PASSWORD` | zufällig | Passwort beim ersten Start; fehlt es, steht ein zufälliges im Container-Log |
| `ADMIN_RESET` | – | `true` setzt beim Start das Passwort auf `ADMIN_PASSWORD` zurück und schaltet 2FA ab |
| `DATA_PATH` | `./data` | Ordner auf dem Server für die Datenbank (nur in der Compose-Datei) |
| `PUID` / `PGID` | `1000` | Benutzer und Gruppe, denen der Datenordner gehört. Auf Synology meist `1026` und `100`; per SSH zeigt `id` deine Werte |
| `TZ` | `Europe/Berlin` | Zeitzone für die Anzeige |
| `COOKIE_SECURE` | `auto` | `true` erzwingt Secure-Cookies, `auto` setzt sie nur bei HTTPS |

## Gut zu wissen

- **eBay begrenzt die Abfragen pro Tag** (für die Browse API standardmäßig 5.000). Wie viele heute verbraucht sind, zeigt Ebay Watcher unter *Einstellungen → eBay-Verbindung*. Bei vielen Gesuchen hilft ein längeres Suchintervall.
- **Datenschutz:** Ebay Watcher speichert nur Artikelnummer, Titel, Bild-Link, Preise, Versandkosten, Standort-Land und Endzeit der gefundenen Angebote. Daten über Verkäufer oder Käufer werden nicht gespeichert. Beendete Angebote werden nach sieben Tagen gelöscht.
