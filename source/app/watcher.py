"""Hintergrundprüfung.

Zwei getrennte Abläufe:
* Entdecken: jedes aktive Gesuch wird im eingestellten Intervall (oder seinem eigenen) nach neuen
  Angeboten durchsucht. Abholangebote im Umkreis werden höchstens einmal pro Stunde gesucht.
* Endspurt: jede gemerkte Auktion wird genau einmal X Minuten vor ihrem Ende geprüft. Hat man in der
  Benachrichtigung „1 Min vorher erinnern“ gedrückt, wird sie 1 Minute vor Ende noch einmal geprüft.
Sofortkauf-Angebote unter dem Limit werden direkt beim Entdecken gemeldet.
Dazu alle paar Minuten: Vergleichsangebote für den Durchschnittspreis holen (market.py).

Wie gesucht wird, entscheidet die Plattform (plattformen/, z. B. ebay.py). Hier steht nur, was für alle
gilt: Limit, Ausschlusswörter, Ausgeblendete, Endspurt, Erinnerung, Benachrichtigungen.

Mehrere Konten: Jedes Gesuch gehört einem Konto. Gesucht und benachrichtigt wird mit dessen Einstellungen
(Zugangsdaten, Standort, ntfy …). Gesuche gesperrter Konten ruhen.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from urllib.parse import quote

from . import db, details, images, market, notify, signed, util
from .plattformen import liste as plattformen
from .plattformen.basis import Plattform, PlatformError

log = logging.getLogger("heuhaufen")

TICK_SECONDS = 30
MAX_SINGLE_PUSHES = 5          # mehr neue Sofortkauf-Treffer auf einmal -> eine Sammelnachricht
MAX_END_LOOKUPS = 15           # fehlende Endzeiten pro Lauf über die Einzelabfrage nachladen
MARKET_EVERY = 300             # Durchschnittspreis: alle 5 Minuten nachsehen, ob etwas zu holen ist …
MARKET_LOOKUPS_PER_RUN = 3     # … und dann höchstens so viele gezielte Abfragen auf einmal

ITEM_FIELDS = [
    "platform", "title", "url", "image", "images", "is_auction", "has_bin", "price", "bin_price", "currency",
    "shipping", "shipping_known", "bid_count", "end_time", "country", "location_text",
    "pickup", "pickup_only", "distance_km", "near", "epid", "cond", "condition_text",
]


# Gesuche und Angebote aktiver Konten (Gesuche ohne Konto gibt es nur in Tests)
ACTIVE_OWNER = "COALESCE((SELECT u.active FROM users u WHERE u.id = s.user_id), 1) = 1"


class Watcher:
    REMIND_BEFORE = 60          # „1 Min vorher erinnern“: so viele Sekunden vor Ende kommt der Hinweis
    REMIND_LEAD = 8             # … und so viel früher wird nachgesehen (Zeit für Abfrage und Versand)

    def __init__(self) -> None:
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.last_tick: float | None = None
        self.last_checks: dict[tuple, dict] = {}     # (Konto, Plattform) → Ergebnis von „Verbindung prüfen“
        self.ntfy_errors: dict = {}                  # Konto → letzter Fehler beim Senden an ntfy
        self._settings: dict = {}                    # Einstellungen je Konto, gilt für einen Takt
        self._market_next = 0.0

    @staticmethod
    def platform(key: str) -> Plattform:
        return plattformen.get(key)

    def settings_for(self, user_id, given: dict | None = None) -> dict:
        """Einstellungen des Kontos, dem ein Gesuch gehört (innerhalb eines Takts nur einmal gelesen).
        given: dieselben für alle (Tests mit nur einem Konto)."""
        if given is not None:
            return given
        if user_id not in self._settings:
            self._settings[user_id] = db.get_settings(user_id)
        return self._settings[user_id]

    def _ntfy_result(self, settings: dict, error: Exception | None = None) -> None:
        uid = settings.get("_user_id")
        if error is None:
            self.ntfy_errors.pop(uid, None)
        else:
            self.ntfy_errors[uid] = str(error)
            log.warning("ntfy: %s", error)

    def ntfy_error(self, user_id) -> str | None:
        return self.ntfy_errors.get(user_id)

    # ------------------------------------------------------------ Lebenszyklus

    def start(self) -> None:
        self.task = asyncio.create_task(self._loop(), name="heuhaufen")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        await plattformen.close()

    def poke(self) -> None:
        self.wake.set()

    def forget_pickup(self, user_id: int | None) -> None:
        """PLZ oder Umkreis eines Kontos geändert: beim nächsten Lauf seiner Gesuche auch die Abholsuche."""
        ids = [r["id"] for r in db.q("SELECT id FROM searches WHERE user_id IS ?", (user_id,))]
        for p in plattformen.ALL.values():
            p.forget_pickup(ids)

    @staticmethod
    def interval_seconds(s: dict, settings: dict) -> int:
        """Suchintervall eines Gesuchs: sein eigenes, sonst das aus den Einstellungen."""
        minutes = s.get("interval_minutes") or db.setting_int(settings, "interval_minutes", 15)
        return max(5, minutes) * 60

    async def _loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Fehler im Prüfzyklus")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self._next_wait())
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    def _next_wait(self) -> float:
        """Bis zum nächsten Takt warten, bei einer anstehenden Erinnerung aber pünktlich aufwachen."""
        row = db.q1("SELECT MIN(end_time) AS t FROM items WHERE remind = 1 AND reminded = 0 AND status = 'active' "
                    "AND end_time > ?", (time.time(),))
        if row and row["t"]:
            due = row["t"] - self.REMIND_BEFORE - self.REMIND_LEAD - time.time()
            return max(1.0, min(TICK_SECONDS, due))
        return TICK_SECONDS

    async def tick(self) -> None:
        self.last_tick = time.time()
        self._settings = {}
        db.prune()
        images.cleanup_if_due()
        await self.check_endings()
        await self.check_reminders()
        for s in db.q(f"SELECT * FROM searches s WHERE s.active = 1 AND {ACTIVE_OWNER} ORDER BY s.id"):
            settings = self.settings_for(s["user_id"])
            if not any(p.configured(settings) for p in plattformen.of_search(s)):
                continue
            if s["last_run"] is None or time.time() - s["last_run"] >= self.interval_seconds(s, settings):
                await self.discover(s, settings)
                await self.check_endings()
        await self.update_market()

    # ------------------------------------------------------------ Entdecken

    async def discover(self, s: dict, settings: dict) -> None:
        """Neue Angebote eines Gesuchs auf allen seinen Plattformen."""
        now = time.time()
        errors = []
        for p in plattformen.of_search(s):
            if not p.configured(settings):
                continue
            try:
                await self._discover_on(p, s, settings, now)
            except PlatformError as e:
                log.warning("Gesuch %s (%s): %s", s["id"], p.name, e)
                errors.append(str(e) if len(plattformen.of_search(s)) == 1 else f"{p.name}: {e}")
        db.ex("UPDATE searches SET last_run = ?, last_error = ?, initialized = CASE WHEN ? THEN initialized ELSE 1 END "
              "WHERE id = ?", (now, "; ".join(errors) or None, bool(errors), s["id"]))

    async def _discover_on(self, p: Plattform, s: dict, settings: dict, now: float) -> None:
        radius = db.setting_int(settings, "radius_km", 50)
        result = await p.find(s, settings, now)
        keep_pickup = result.keep_pickup

        excluded = util.exclusion_words(s["exclude"])
        hidden = db.hidden_ids(s["id"])   # von Hand ausgeblendet: weiter aktualisieren, aber nie melden
        seen: set[str] = set()
        missing_end: list[str] = []
        new_fixed: list[dict] = []
        as_fixed = {**s, "format": "fixed"}

        for item_id, (raw, pickup) in result.items.items():
            existing = db.q1("SELECT * FROM items WHERE search_id = ? AND item_id = ?", (s["id"], item_id))
            if keep_pickup and existing and existing["pickup"]:
                pickup = True   # ohne Abholsuche: Abholung und Entfernung von der letzten übernehmen
            data = p.parse(raw, pickup)
            if pickup and data["distance_km"] is None and existing:
                data["distance_km"] = existing["distance_km"]
            # Abholbar im eingestellten Umkreis: dann zählt der Preis ohne Versand
            distance = data["distance_km"]
            data["near"] = int(bool(pickup) and (distance <= radius if distance is not None
                                                  else s["pickup_mode"] == "radius"))
            title = data["title"].lower()
            if any(word in title for word in excluded):
                continue
            seen.add(item_id)

            if existing:
                if data["end_time"] is None:
                    data["end_time"] = existing["end_time"]
                moved_later = bool(existing["end_checked"] and data["end_time"] and existing["end_time"]
                                   and data["end_time"] > existing["end_time"] + 60)
                self._update_item(s["id"], item_id, data, now, reset_end=moved_later)
                row = {**existing, **data, "status": "active"}
            else:
                self._insert_item(s["id"], item_id, data, now)
                row = {**data, "status": "active", "end_checked": 0, "notified_bin": 0, "notified_end": 0}

            if row["is_auction"] and not row["end_time"]:
                missing_end.append(item_id)

            if s["format"] in ("fixed", "both") and row["has_bin"] and not row["notified_bin"] and item_id not in hidden:
                view = util.item_view(row, as_fixed, now)
                if view["within"]:
                    new_fixed.append(view)

        # Nicht mehr gefunden: über Limit, beendet oder Filter geändert. Abholangebote prüft erst die
        # nächste Abholsuche. Nur Angebote dieser Plattform, die anderen hat sie nicht gesucht.
        for row in db.q("SELECT item_id, pickup FROM items WHERE search_id = ? AND platform = ? AND status = 'active'",
                        (s["id"], p.key)):
            if row["item_id"] not in seen and not (keep_pickup and row["pickup"]):
                db.ex("UPDATE items SET status = 'dropped' WHERE search_id = ? AND item_id = ?",
                      (s["id"], row["item_id"]))

        # Manche Suchergebnisse liefern bei Auktionen keine Endzeit mit
        for item_id in missing_end[:MAX_END_LOOKUPS]:
            try:
                got = await p.fetch(item_id, settings, "suche")
            except PlatformError:
                break
            if got and got[1]:
                details.store(item_id, got[1])   # Beschreibung für die Galerie gleich mitnehmen
            end = got[0]["end_time"] if got else None
            if end:
                db.ex("UPDATE items SET end_time = ? WHERE search_id = ? AND item_id = ?", (end, s["id"], item_id))

        await self._notify_fixed(s, settings, new_fixed)

    # ------------------------------------------------------------ Endspurt

    async def check_endings(self, given: dict | None = None) -> None:
        now = time.time()
        rows = db.q(
            f"""
            SELECT i.* FROM items i JOIN searches s ON s.id = i.search_id
            WHERE s.active = 1 AND {ACTIVE_OWNER} AND s.format IN ('auction', 'both')
              AND i.status = 'active' AND i.is_auction = 1 AND i.end_checked = 0
              AND i.end_time IS NOT NULL AND i.end_time > ?
              AND i.end_time - s.lead_minutes * 60 <= ?
              AND NOT EXISTS (SELECT 1 FROM hidden_items h
                              WHERE h.search_id = i.search_id AND h.item_id = i.item_id)
            ORDER BY i.end_time LIMIT 25
            """,
            (now, now),
        )
        if not rows:
            return

        for row in rows:
            s = db.q1("SELECT * FROM searches WHERE id = ?", (row["search_id"],))
            settings = self.settings_for(s["user_id"] if s else None, given)
            if not s or not plattformen.of_item(row).configured(settings):
                continue
            try:
                item = await self._fresh_item(row, settings, "endspurt")
            except PlatformError as e:
                log.warning("Endspurt-Prüfung %s: %s", row["item_id"], e)
                return  # beim nächsten Takt erneut versuchen

            key = (s["id"], row["item_id"])
            if item is None:
                db.ex("UPDATE items SET end_checked = 1 WHERE search_id = ? AND item_id = ?", key)
                continue
            now = time.time()
            if item["end_time"] <= now:
                db.ex("UPDATE items SET status = 'ended', end_checked = 1 WHERE search_id = ? AND item_id = ?", key)
                continue
            if item["end_time"] - s["lead_minutes"] * 60 > now + 60:
                continue  # Endzeit hat sich verschoben: später prüfen

            view = util.item_view(item, s, now)
            if view["within"] and notify.configured(settings):
                try:
                    await notify.send(settings, **self._auction_push(s, view, settings))
                except notify.NtfyError as e:
                    self._ntfy_result(settings, e)
                    continue  # nächster Takt versucht es erneut
                self._ntfy_result(settings)
            db.ex(
                "UPDATE items SET end_checked = 1, notified_end = ? WHERE search_id = ? AND item_id = ?",
                (1 if view["within"] else 0, *key),
            )

    async def refresh(self, row: dict, settings: dict) -> dict | None:
        """Galerie: aktueller Stand eines Angebots samt Beschreibung und Geboten (details.py)."""
        return await self._fresh_item(row, settings, "galerie")

    async def _fresh_item(self, row: dict, settings: dict, purpose: str) -> dict | None:
        """Aktueller Stand eines gemerkten Angebots über die Einzelabfrage der Plattform (eine Abfrage).
        None: dort nicht mehr da. Beschreibung, Merkmale und Gebote landen nebenbei in details.py."""
        got = await plattformen.of_item(row).fetch(row["item_id"], settings, purpose, bool(row["pickup"]))
        if got is None:
            db.ex("UPDATE items SET status = 'gone' WHERE search_id = ? AND item_id = ?",
                  (row["search_id"], row["item_id"]))
            return None
        fresh, info = got
        if info:
            details.store(row["item_id"], info)
        # Was die Einzelabfrage nicht (zuverlässig) liefert, aus der Suche übernehmen
        for field in ("pickup", "pickup_only", "distance_km", "near"):
            fresh[field] = row[field]
        if fresh["pickup_only"]:
            fresh["shipping"], fresh["shipping_known"] = 0.0, 1
        elif not fresh["shipping_known"] and row["shipping_known"]:
            fresh["shipping"], fresh["shipping_known"] = row["shipping"], 1
        for field in ("end_time", "image", "images", "url", "title", "epid", "cond", "condition_text"):
            if not fresh[field]:
                fresh[field] = row[field]
        self._update_item(row["search_id"], row["item_id"], fresh, time.time(), reset_end=False)
        return {**row, **fresh, "status": "active"}

    # ------------------------------------------------------------ Erinnerung 1 Minute vor Ende

    async def check_reminders(self, given: dict | None = None) -> None:
        """Angebote mit gedrücktem „1 Min vorher erinnern“: kurz vor Ende den Preis holen und, wenn er
        noch passt, auffällig melden. Passt er nicht mehr, kommt ein stiller Hinweis."""
        now = time.time()
        rows = db.q(
            f"""
            SELECT i.* FROM items i JOIN searches s ON s.id = i.search_id
            WHERE s.active = 1 AND {ACTIVE_OWNER} AND i.remind = 1 AND i.reminded = 0 AND i.status = 'active'
              AND i.end_time IS NOT NULL AND i.end_time > ? AND i.end_time - ? <= ?
              AND NOT EXISTS (SELECT 1 FROM hidden_items h
                              WHERE h.search_id = i.search_id AND h.item_id = i.item_id)
            ORDER BY i.end_time
            """,
            (now, self.REMIND_BEFORE + self.REMIND_LEAD, now),
        )
        for row in rows:
            key = (row["search_id"], row["item_id"])
            db.ex("UPDATE items SET reminded = 1 WHERE search_id = ? AND item_id = ?", key)   # nur ein Versuch
            s = db.q1("SELECT * FROM searches WHERE id = ?", (row["search_id"],))
            settings = self.settings_for(s["user_id"], given)
            try:
                item = await self._fresh_item(row, settings, "erinnerung")
            except PlatformError as e:
                log.warning("Erinnerung %s: %s", row["item_id"], e)
                item = row   # lieber mit dem letzten bekannten Preis erinnern als gar nicht
            if item is None or not notify.configured(settings):
                continue
            view = util.item_view(item, s, time.time())
            try:
                await notify.send(settings, **self._reminder_push(s, view, settings))
                self._ntfy_result(settings)
            except notify.NtfyError as e:
                self._ntfy_result(settings, e)

    def _remind_url(self, settings: dict, s: dict, v: dict) -> str | None:
        base = self._app_base(settings)
        if not base:
            return None
        item_id = str(v["item_id"])
        return (f"{base}/erinnern/{s['id']}/{quote(item_id, safe='')}"
                f"?s={signed.sign('erinnern', s['id'], item_id)}")

    def _reminder_push(self, s: dict, v: dict, settings: dict) -> dict:
        name = util.short(s["name"] or s["query"], 40)
        bids = v["bid_count"] or 0
        price = f"{self._price_line(v, 'Aktuell', settings)}, {bids} {'Gebot' if bids == 1 else 'Gebote'}"
        if not v["within"]:
            return {   # still: Preis inzwischen über dem Limit
                "title": f"Nicht mehr unter Limit: {name}",
                "message": "\n".join([util.short(v["title"], 120), *self._condition_line(v), price, util.limit_text(s)]),
                "click": v["url"], "priority": 2, "tags": ["chart_with_upwards_trend"],
            }
        lines = [util.short(v["title"], 120), *self._condition_line(v), price, *self._market_line(s, v, settings),
                 util.limit_text(s), *v["flags"]]
        return {
            "title": f"Letzte Minute: {name}",
            "message": "\n".join(lines),
            "click": v["url"],
            "image": self._image(settings, s, v),
            "priority": 5,
            "tags": ["rotating_light", "alarm_clock"],
            "actions": [{"action": "view", "label": f"Jetzt bei {plattformen.of_item(v).name} bieten",
                         "url": v["url"], "clear": True}],
        }

    # ------------------------------------------------------------ Durchschnittspreis

    async def update_market(self, given: dict | None = None) -> None:
        """Holt Vergleichsangebote: zuerst die tägliche Suche je Gesuch, dann gezielte Abfragen für
        Angebote unter dem Limit – erst die, denen der Vergleich fehlt, dann wöchentlich die übrigen,
        damit der Preisverlauf weiterwächst (siehe market.py). Die Vergleichsangebote sind für alle Konten
        gemeinsam; abgefragt wird mit den Zugangsdaten und im Tageslimit des Kontos, dem das Gesuch gehört."""
        if time.time() < self._market_next:
            return
        self._market_next = time.time() + MARKET_EVERY
        market.prune()
        searches = []
        for s in db.q(f"SELECT * FROM searches s WHERE s.active = 1 AND {ACTIVE_OWNER} AND s.last_run IS NOT NULL "
                      "ORDER BY s.id"):
            settings = self.settings_for(s["user_id"], given)
            if market.enabled(settings) and self._market_platform(s, settings):
                searches.append((s, settings))

        for s, settings in searches:   # höchstens eine Gesuch-Suche pro Durchgang, nur wenn es Angebote gibt
            key = market.search_key(s["id"])
            has_items = db.q1("SELECT 1 AS x FROM items WHERE search_id = ? AND status = 'active' LIMIT 1", (s["id"],))
            if has_items and market.due(key):
                await self._fetch_market(self._market_platform(s, settings), key, settings, s["exclude"], s=s)
                return

        now = time.time()
        wanted: dict[str, tuple[int, float, dict, dict, dict]] = {}
        in_use: set[str] = set()
        for s, settings in searches:
            items = db.q("SELECT * FROM items WHERE search_id = ?", (s["id"],))
            views = util.search_overview(s, items, now, db.hidden_ids(s["id"]))["matches"]
            comparison = market.Comparison(s, settings, views)
            for v in views:
                key = market.lookup_key(v)
                if not key:
                    continue
                in_use.add(key)
                if key not in wanted and market.due(key, now):
                    # Reihenfolge: fehlender Ø vor Auffrischen, bald endende Auktionen zuerst
                    missing = comparison.compare(v) is None
                    wanted[key] = (0 if missing else 1, v["end_time"] or float("inf"), s, v, settings)
        market.touch(in_use, now)

        budgets: dict = {}   # Konto → noch erlaubte gezielte Abfragen heute
        done = 0
        for key, (_prio, _end, s, v, settings) in sorted(wanted.items(), key=lambda w: w[1][:2]):
            if done >= MARKET_LOOKUPS_PER_RUN:
                break
            uid = settings.get("_user_id")
            if uid not in budgets:
                budgets[uid] = market.daily_lookups(settings) - market.lookups_today(uid)
            if budgets[uid] <= 0:
                continue
            budgets[uid] -= 1
            done += 1
            p = self._market_platform(s, settings)
            if key.startswith("epid:"):
                await self._fetch_market(p, key, settings, catalog=key[5:])
            else:
                brand = (s["query"].split() or [""])[0]
                number = market.raw_number(v["title"], key[3:])
                await self._fetch_market(p, key, settings, query=f"{brand} {number}".strip())

    @staticmethod
    def _market_platform(s: dict, settings: dict) -> Plattform | None:
        """Woher die Vergleichsangebote eines Gesuchs kommen: von seiner ersten eingerichteten Plattform."""
        return next((p for p in plattformen.of_search(s) if p.configured(settings)), None)

    async def _fetch_market(self, p: Plattform, key: str, settings: dict, exclude: str = "", s: dict | None = None,
                            query: str | None = None, catalog: str | None = None) -> None:
        try:
            listings, complete = await p.compare(settings, s=s, query=query, catalog=catalog)
        except PlatformError as e:
            log.info("Durchschnittspreis %s: %s", key, e)
            market.store(key, None, user_id=settings.get("_user_id"))
            return
        excluded = util.exclusion_words(exclude)
        if excluded:
            listings = [x for x in listings if not any(w in (x.get("title") or "").lower() for w in excluded)]
        market.store(key, listings, complete=complete, user_id=settings.get("_user_id"))

    # ------------------------------------------------------------ Nachrichten

    async def _notify_fixed(self, s: dict, settings: dict, views: list[dict]) -> None:
        if not views:
            return
        if notify.configured(settings):
            name = s["name"] or s["query"]
            try:
                if len(views) > 1 and (not s["initialized"] or len(views) > MAX_SINGLE_PUSHES):
                    cheapest = min(views, key=lambda v: v["total"] or 0)
                    shown = cheapest["gross"] if util.with_shipping(settings) else cheapest["rel_price"]
                    await notify.send(
                        settings,
                        title=f"{util.short(name, 40)}: {len(views)} Sofortkauf-Angebote unter Limit",
                        message=(f"Das günstigste kostet {util.eur(shown)}"
                                 f"{' mit Versand' if util.with_shipping(settings) else ''}. "
                                 "Alle Angebote findest du in der Übersicht."),
                        click=self._app_link(settings, s),
                        priority=3,
                        tags=["shopping_cart"],
                    )
                else:
                    for v in views:
                        await notify.send(settings, **self._fixed_push(s, v, settings))
                self._ntfy_result(settings)
            except notify.NtfyError as e:
                self._ntfy_result(settings, e)
                return  # nicht als gemeldet markieren, nächster Lauf versucht es erneut
        for v in views:
            db.ex("UPDATE items SET notified_bin = 1 WHERE search_id = ? AND item_id = ?", (s["id"], v["item_id"]))

    @staticmethod
    def _app_base(settings: dict) -> str:
        return (settings.get("app_url") or "").strip().rstrip("/")

    def _app_link(self, settings: dict, s: dict) -> str | None:
        base = self._app_base(settings)
        return f"{base}/searches/{s['id']}" if base else None

    def _image(self, settings: dict, s: dict, v: dict) -> str | None:
        """Bild für die Benachrichtigung: aus dem eigenen Bild-Cache, wenn die Adresse von
        Heuhaufen eingetragen ist (das Handy muss sie erreichen), sonst direkt von der Plattform."""
        base = self._app_base(settings)
        path = images.url(s["id"], v["item_id"], v["image"])
        if base and path:
            return base + path
        return plattformen.of_item(v).image(v["image"], images.THUMB_SIZE)

    def _actions(self, settings: dict, s: dict, v: dict, remind: bool = False) -> list[dict]:
        """Knöpfe der Benachrichtigung (ntfy erlaubt höchstens drei)."""
        actions = [{"action": "view", "label": f"Bei {plattformen.of_item(v).name} öffnen", "url": v["url"],
                    "clear": True}]
        remind_url = self._remind_url(settings, s, v) if remind else None
        if remind_url:   # das Handy schickt den POST an Heuhaufen, dafür muss es die Adresse erreichen
            actions.append({"action": "http", "label": "1 Min vorher erinnern", "url": remind_url,
                            "method": "POST", "clear": False})
        link = self._app_link(settings, s)
        if link:
            actions.append({"action": "view", "label": "In Heuhaufen öffnen", "url": link})
        return actions

    @staticmethod
    def _price_line(v: dict, label: str, settings: dict) -> str:
        """z. B. „Preis 43,99 € (39 € + 4,99 € Versand)“ oder ohne Versand-Anzeige „Preis 39 € zzgl. 4,99 € Versand“."""
        price = util.eur(v["rel_price"])
        if v.get("near") and v["shipping_known"] and v["shipping"] and not v["pickup_only"]:
            return f"{label} {price} bei Abholung (sonst + {util.eur(v['shipping'])} Versand)"
        if v["shipping_known"] and v["shipping"] and not v["pickup_only"]:
            shipping = util.eur(v["shipping"])
            if util.with_shipping(settings):
                return f"{label} {util.eur(v['gross'])} ({price} + {shipping} Versand)"
            return f"{label} {price} zzgl. {shipping} Versand"
        if v["shipping_known"] and not v["pickup_only"]:
            return f"{label} {price}, Versand kostenlos"
        return f"{label} {price}"

    @staticmethod
    def _condition_line(v: dict) -> list[str]:
        """z. B. „Zustand: Gebraucht“ (Wortlaut der Plattform, ohne Zusatz in Klammern)."""
        label = util.condition_label(v)
        return [f"Zustand: {label}"] if label else []

    @staticmethod
    def _market_line(s: dict, v: dict, settings: dict) -> list[str]:
        if not market.enabled(settings):
            return []
        m = market.Comparison(s, settings, [v]).compare(v)
        return [f"Ø {util.eur(m['median'])} ({m['delta_text']})"] if m else []

    def _auction_push(self, s: dict, v: dict, settings: dict) -> dict:
        minutes = max(1, round((v["ends_in"] or 0) / 60))
        bids = v["bid_count"] or 0
        lines = [
            util.short(v["title"], 120),
            *self._condition_line(v),
            f"{self._price_line(v, 'Aktuell', settings)}, {bids} {'Gebot' if bids == 1 else 'Gebote'}",
            *self._market_line(s, v, settings),
            util.limit_text(s),
            *v["flags"],
        ]
        return {
            "title": f"Noch {minutes} Min: {util.short(s['name'] or s['query'], 40)}",
            "message": "\n".join(lines),
            "click": v["url"],
            "image": self._image(settings, s, v),
            "priority": 4,   # hoch; dringend (5) bleibt der Erinnerung 1 Minute vor Ende vorbehalten
            "tags": ["hourglass_flowing_sand"],
            "actions": self._actions(settings, s, v, remind=s["lead_minutes"] * 60 > self.REMIND_BEFORE + 60),
        }

    def _fixed_push(self, s: dict, v: dict, settings: dict) -> dict:
        lines = [util.short(v["title"], 120), *self._condition_line(v), self._price_line(v, "Preis", settings),
                 *self._market_line(s, v, settings), util.limit_text(s), *v["flags"]]
        return {
            "title": f"Sofortkauf unter Limit: {util.short(s['name'] or s['query'], 40)}",
            "message": "\n".join(lines),
            "click": v["url"],
            "image": self._image(settings, s, v),
            "priority": 4,
            "tags": ["shopping_cart"],
            "actions": self._actions(settings, s, v),
        }

    # ------------------------------------------------------------ Datenbank

    @staticmethod
    def _insert_item(sid: int, item_id: str, data: dict, now: float) -> None:
        cols = ",".join(ITEM_FIELDS)
        marks = ",".join("?" * len(ITEM_FIELDS))
        db.ex(
            f"INSERT INTO items(search_id, item_id, {cols}, first_seen, last_seen, status) "
            f"VALUES (?, ?, {marks}, ?, ?, 'active')",
            [sid, item_id, *[data[c] for c in ITEM_FIELDS], now, now],
        )

    @staticmethod
    def _update_item(sid: int, item_id: str, data: dict, now: float, reset_end: bool) -> None:
        sets = ", ".join(f"{c} = ?" for c in ITEM_FIELDS)
        extra = ", end_checked = 0, notified_end = 0, reminded = 0" if reset_end else ""
        db.ex(
            f"UPDATE items SET {sets}, last_seen = ?, status = 'active'{extra} "
            "WHERE search_id = ? AND item_id = ?",
            [*[data[c] for c in ITEM_FIELDS], now, sid, item_id],
        )

    # ------------------------------------------------------------ Einstellungen

    async def check_platform(self, key: str, user_id=None) -> None:
        """Einstellungen → „Verbindung prüfen“ mit den Zugangsdaten eines Kontos."""
        p = plattformen.get(key)
        slot = (user_id, p.key)
        try:
            limits = await p.check(db.get_settings(user_id))
            self.last_checks[slot] = {"ok": True, "at": time.time(), **limits}
        except PlatformError as e:
            self.last_checks[slot] = {"ok": False, "at": time.time(), "error": str(e)}
        except Exception as e:   # unerwartete Antwort: anzeigen statt die Einstellungen abstürzen zu lassen
            log.exception("Verbindung prüfen (%s)", p.name)
            self.last_checks[slot] = {"ok": False, "at": time.time(),
                                      "error": f"Prüfung fehlgeschlagen ({e.__class__.__name__})."}
