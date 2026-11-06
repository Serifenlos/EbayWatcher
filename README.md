# Heuhaufen

Heuhaufen überwacht eBay-Gesuche und schickt über [ntfy](https://ntfy.sh) eine Push-Nachricht aufs Handy,

- wenn eine Auktion kurz vor Ende (Standard: 10 Minuten) noch unter deinem Höchstpreis liegt; über den Knopf „1 Min vorher erinnern“ kommt eine Minute vor Schluss ein zweiter, dringender Hinweis, falls der Preis dann noch passt,
- wenn ein Sofortkauf-Angebot unter deinem Höchstpreis neu auftaucht.

Es nutzt die offizielle eBay Browse API und läuft als einzelner Docker-Container ohne weitere Dienste, zum Beispiel auf einem NAS.

> Heuhaufen ist noch in Entwicklung (Version 0.x). Neue Versionen und Änderungen stehen unter [Releases](https://github.com/Serifenlos/Heuhaufen/releases).

## Was es kann

- Beliebig viele Gesuche mit Suchbegriff, Ausschlusswörtern, Angebotsformat (Auktion, Sofortkauf, beides), Standort (Deutschland, EU, weltweit) und Zustand
- Höchstpreis wahlweise mit oder ohne Versandkosten, Option „nur versandkostenfrei“; Preise werden mit Versand angezeigt (darunter Artikelpreis und Versand), abschaltbar
- Es werden nur Angebote berücksichtigt, die nach Deutschland versendet werden; bei Angeboten außerhalb der EU weist die Benachrichtigung auf möglichen Zoll hin
- Angebote nur zur Abholung: im Umkreis deiner Postleitzahl, alle mit Entfernung markiert, oder ausgeblendet. Angebote, die du im Umkreis abholen kannst, sind markiert und zählen ohne Versandkosten, auch beim Limit
- Startseite mit allen Gesuchen als Karten oder Liste, sortiert nach der nächsten endenden Auktion, hell oder dunkel nach Systemeinstellung
- Treffer sortierbar nach Endzeit, Preis, Einstelldatum oder Entfernung und seitenweise (Vorgabe 20 pro Seite, einstellbar); unpassende Angebote lassen sich ausblenden (in der Liste oder in der Galerie, mit „Rückgängig“) und werden dann auch nicht mehr gemeldet
- Zustand jedes Angebots im Wortlaut von eBay (zum Beispiel „Gebraucht“ oder „Als Ersatzteil/defekt“), in der Liste und in den Benachrichtigungen
- Bilder-Galerie: Ein Klick aufs Vorschaubild oder den Titel zeigt alle Bilder des Angebots groß (mit [PhotoSwipe](https://photoswipe.com)), daneben Preis, Zustand, Gebote, Artikelmerkmale und die Beschreibung des Verkäufers; zu eBay geht es über den Knopf in der Galerie oder mit gedrückter Cmd-/Strg-Taste
- Durchschnittspreis (Ø) neben jedem Angebot: Vergleich mit den Sofortkauf-Angeboten desselben Produkts der letzten Monate, auf Wunsch getrennt nach neu und gebraucht; deutlich günstigere Angebote sind grün markiert
- Sicherer Login: Argon2-Passwort-Hash, Sperre nach Fehlversuchen, „Angemeldet bleiben“ für 3 Monate, optional Zwei-Faktor per Authenticator-App, Übersicht und Abmeldung aller Geräte

So arbeitet Heuhaufen: Es sucht im eingestellten Intervall (Standard 15 Minuten, pro Gesuch änderbar) nach neuen Angeboten und merkt sich jede Auktion mit ihrer Endzeit. Genau X Minuten vor Ende fragt es den aktuellen Preis einmal ab und meldet sich, wenn er unter deinem Limit liegt. Dadurch braucht es nur wenige API-Abfragen.

## Voraussetzungen

- **Docker mit Docker Compose**, direkt oder über Portainer. Das Image gibt es für x86-64 (`linux/amd64`), nicht für ARM-Geräte wie den Raspberry Pi.
- **Ein Konto im [eBay Developer Program](https://developer.ebay.com)** mit einem *Production*-Keyset. Daraus brauchst du die **App ID** und die **Cert ID**. Heuhaufen liest nur öffentliche Suchergebnisse; ein Login mit deinem eBay-Konto ist nicht nötig.
- **Ein ntfy-Server und die ntfy-App** auf dem Handy. Das kann eine eigene Instanz sein oder der öffentliche Server `https://ntfy.sh`.
- Heuhaufen ist auf **eBay.de** ausgelegt: Lieferung nach Deutschland, deutsche Postleitzahlen, Preise in Euro.

## Installation

### Mit Docker Compose

```bash
mkdir heuhaufen && cd heuhaufen
curl -O https://raw.githubusercontent.com/Serifenlos/Heuhaufen/main/docker/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/Serifenlos/Heuhaufen/main/docker/.env.example
```

In der Datei `.env` mindestens `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` und `ADMIN_PASSWORD` eintragen, dann starten:

```bash
docker compose up -d
```

Die Datenbank liegt danach im Ordner `data` neben der Compose-Datei.

### Mit Portainer

1. Einen Ordner für die Daten anlegen, zum Beispiel `/volume1/docker/heuhaufen` auf einer Synology.
2. **Stacks → Add stack**, Name `heuhaufen`, **Web editor**.
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

Deine Gesuche und Einstellungen bleiben im Datenordner erhalten. Was sich geändert hat, steht unter [Releases](https://github.com/Serifenlos/Heuhaufen/releases). Die laufende Version zeigt Heuhaufen unter *Einstellungen → Über Heuhaufen*.

Welche Version du bekommst, legst du über den Image-Tag in der Compose-Datei fest:

| Tag | Bedeutung |
| --- | --- |
| `latest` | immer die neueste Version |
| `0.1` | die neueste Version aus der Reihe 0.1.x |
| `0.1.3` | genau diese Version |


### Umstieg von „Ebay Watcher“

Bis November 2026 hieß Heuhaufen „Ebay Watcher“ (Image `ghcr.io/serifenlos/ebaywatcher`). Beim ersten Start übernimmt Heuhaufen die alte Datenbank `ebay-watcher.db` aus dem Datenordner unter dem neuen Namen `heuhaufen.db`, mit allen Gesuchen, Einstellungen, Durchschnittspreisen und Anmeldungen. Du bleibst angemeldet.

1. Den alten Container stoppen und den Datenordner sichern (einfach kopieren).
2. In der Compose-Datei das Image auf `ghcr.io/serifenlos/heuhaufen:latest` ändern (neue Vorlage: [`docker/docker-compose.yml`](docker/docker-compose.yml)). Wichtig: derselbe Datenordner wie bisher, also dasselbe `DATA_PATH`. Wer den Ordner umbenennen möchte, macht das, solange der Container gestoppt ist, und trägt den neuen Pfad ein.
3. Starten. Im Container-Log steht einmal „Datenbank ebay-watcher.db übernommen als heuhaufen.db“.

In Portainer am einfachsten: den alten Stack stoppen, einen neuen Stack `heuhaufen` mit der neuen Compose-Datei und denselben Umgebungsvariablen anlegen, prüfen, ob alles da ist, und erst dann den alten Stack löschen. Der Datenordner bleibt dabei erhalten, weil er auf dem Server liegt und nicht im Stack. Alter und neuer Container dürfen nie gleichzeitig laufen.

## Sicherheit und HTTPS

Heuhaufen ist für das eigene Heimnetz gedacht. Gib den Port nicht im Router frei; von unterwegs erreichst du ihn am besten per VPN.

Für HTTPS kannst du einen Reverse Proxy davorschalten, etwa den von Synology DSM, Nginx Proxy Manager oder Caddy, mit Ziel `http://SERVER:8765`. Sobald die Seite per HTTPS aufgerufen wird, setzt Heuhaufen seine Cookies automatisch mit dem Secure-Flag. Am besten erlaubt der Proxy nur Zugriffe aus dem eigenen Netz.

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

- **eBay begrenzt die Abfragen pro Tag** (für die Browse API standardmäßig 5.000). Wie viele heute verbraucht sind und wofür (Suche, Endspurt, Erinnerung, Durchschnittspreis, Galerie), zeigt Heuhaufen unter *Einstellungen → eBay-Verbindung*. Seiten anschauen kostet keine Abfragen. Heuhaufen spart von selbst: Bei „Auktion und Sofortkauf“ reicht eine Abfrage für beide, solange es höchstens 200 Treffer gibt, und Abholangebote im Umkreis sucht es höchstens einmal pro Stunde. Bei vielen Gesuchen hilft ein längeres Suchintervall, für einzelne Gesuche unter *Weitere Optionen*. Bei reinen Auktionen reichen meist 60 Minuten, weil Heuhaufen jede gemerkte Auktion kurz vor Ende ohnehin eigens prüft. Die Galerie fragt beim Öffnen einmal nach Beschreibung und Geboten, für dasselbe Angebot höchstens alle 10 Minuten.
- **Durchschnittspreis:** Verkaufte Preise gibt die eBay-API nicht heraus. Der Ø ist deshalb der Median der Sofortkauf-Angebote desselben Produkts, also ein Angebotspreis. Heuhaufen merkt sich die Angebote, die es dabei sieht, und zählt alle, die im eingestellten Zeitraum (Vorgabe 6 Monate) zu sehen waren, jedes nur einmal. So wächst der Vergleich mit der Zeit, ohne zusätzliche Abfragen. Angebote, die vor ihrem Ende verschwinden, weist Heuhaufen als vermutlich verkauft aus. Als dasselbe Produkt gelten Angebote mit gleicher eBay-Katalognummer, gleicher Artikelnummer im Titel (etwa `87 01 250` oder `576982`) oder gleichem Modell und ähnlichem Titel (etwa „1220E“, aber nicht „1260E + 1220E“), und zwar nur mit derselben Stückzahl: ein „2er Set“ wird nicht mit Einzelgeräten verglichen. Gibt es weniger als drei Vergleichsangebote oder liegen ihre Preise beim Titelvergleich weit auseinander, steht kein Ø da. Dafür braucht Heuhaufen pro Gesuch eine Abfrage am Tag und für Angebote mit Artikel- oder Katalognummer gezielte Abfragen, deren Zahl pro Tag du unter *Einstellungen → Durchschnittspreis* begrenzt.
- **Knöpfe in der Benachrichtigung:** „1 Min vorher erinnern“ erscheint nur, wenn unter *Einstellungen → Standort und Suche* die **Adresse von Heuhaufen** eingetragen ist. Beim Tippen schickt das Handy eine Anfrage an diese Adresse, es muss sie also erreichen können.
- **Bilder:** Jedes Artikelbild lädt Heuhaufen nur einmal bei eBay und speichert es im Datenordner unter `cache/bilder`, für die Galerie in großer Auflösung beim ersten Öffnen. Das kostet keine API-Abfragen, die Bildadressen kommen schon mit der Suche. Bilder, die 14 Tage nicht angezeigt wurden, löscht es von selbst, und insgesamt belegt der Ordner höchstens etwa 500 MB. Ist unter *Einstellungen → Standort und Suche* die **Adresse von Heuhaufen** eingetragen, kommen auch die Bilder in den Benachrichtigungen aus diesem Cache. Das Handy muss diese Adresse dann erreichen können, sonst fehlt das Bild in der Benachrichtigung.
- **Datenschutz:** Heuhaufen speichert nur Artikelnummer, Titel, Artikelbilder, Zustand, Preise, Versandkosten, Standort-Land und Endzeit der gefundenen Angebote, für die Galerie außerdem Beschreibung, Artikelmerkmale und die Zahl der Gebote und Bieter. Daten über Verkäufer oder Käufer werden nicht gespeichert. Beendete Angebote werden nach sieben Tagen gelöscht.

## Fremdsoftware

Die Bilder-Galerie nutzt [PhotoSwipe](https://photoswipe.com) von Dmytro Semenov (MIT-Lizenz). Es ist als Paket in `package.json` eingetragen und wird beim Bauen unverändert eingebunden: das Skript in `js/photoswipe-min.js`, die Stile in `css/app-min.css`.
