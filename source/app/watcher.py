"""Hintergrundprüfung.

Zwei getrennte Abläufe:
* Entdecken: jedes aktive Gesuch wird im eingestellten Intervall (oder seinem eigenen) nach neuen
  Angeboten durchsucht. Abholangebote im Umkreis werden höchstens einmal pro Stunde gesucht.
* Endspurt: jede gemerkte Auktion wird genau einmal X Minuten vor ihrem Ende geprüft. Hat man in der
  Benachrichtigung „1 Min vorher erinnern“ gedrückt, wird sie 1 Minute vor Ende noch einmal geprüft.
Sofortkauf-Angebote unter dem Limit werden direkt beim Entdecken gemeldet.
Dazu alle paar Minuten: Vergleichsangebote für den Durchschnittspreis holen (market.py).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from urllib.parse import quote

from . import db, details, images, market, notify, signed, util
from .ebay import EbayClient, EbayError

log = logging.getLogger("heuhaufen")

TICK_SECONDS = 30
MAX_SINGLE_PUSHES = 5          # mehr neue Sofortkauf-Treffer auf einmal -> eine Sammelnachricht
PICKUP_RADIUS_ALL_KM = 1000    # Abholmodus "alle anzeigen": praktisch ganz Deutschland
MAX_END_LOOKUPS = 15           # fehlende Endzeiten pro Lauf über getItem nachladen
MARKET_EVERY = 300             # Durchschnittspreis: alle 5 Minuten nachsehen, ob etwas zu holen ist …
MARKET_LOOKUPS_PER_RUN = 3     # … und dann höchstens so viele gezielte Abfragen auf einmal
PICKUP_EVERY = 3600            # Abholsuche höchstens stündlich; Abholangebote ändern sich selten
SPLIT_FOR = 6 * 3600           # „Beides“ mit mehr als 200 Treffern: so lange gleich getrennt suchen

ITEM_FIELDS = [
    "title", "url", "image", "images", "is_auction", "has_bin", "price", "bin_price", "currency",
    "shipping", "shipping_known", "bid_count", "end_time", "country", "location_text",
    "pickup", "pickup_only", "distance_km", "near", "epid", "cond", "condition_text",
]
FORMATS = {"auction": ["AUCTION"], "fixed": ["FIXED_PRICE"], "both": ["AUCTION", "FIXED_PRICE"]}


class Watcher:
    REMIND_BEFORE = 60          # „1 Min vorher erinnern“: so viele Sekunden vor Ende kommt der Hinweis
    REMIND_LEAD = 8             # … und so viel früher wird bei eBay nachgesehen (Zeit für Abfrage und Versand)

    def __init__(self) -> None:
        self.ebay = EbayClient()
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.last_tick: float | None = None
        self.last_ebay_check: dict | None = None
        self.last_ntfy_error: str | None = None
        self._market_next = 0.0
        self._pickup_runs: dict[int, float] = {}    # Gesuch → letzte Abholsuche
        self._split_until: dict[int, float] = {}    # Gesuch → bis dann Auktion und Sofortkauf getrennt suchen

    # ------------------------------------------------------------ Lebenszyklus

    def start(self) -> None:
        self.task = asyncio.create_task(self._loop(), name="heuhaufen")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        await self.ebay.close()

    def poke(self) -> None:
        self.wake.set()

    def forget_pickup(self) -> None:
        """PLZ oder Umkreis geändert: beim nächsten Lauf jedes Gesuchs auch die Abholsuche machen."""
        self._pickup_runs.clear()

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
        db.prune()
        images.cleanup_if_due()
        if not self.ebay.configured:
            return
        settings = db.get_settings()
        await self.check_endings(settings)
        await self.check_reminders(settings)
        for s in db.q("SELECT * FROM searches WHERE active = 1 ORDER BY id"):
            if s["last_run"] is None or time.time() - s["last_run"] >= self.interval_seconds(s, settings):
                await self.discover(s, settings)
                await self.check_endings(settings)
        await self.update_market(settings)

    # ------------------------------------------------------------ Entdecken

    @staticmethod
    def _base_filters(s: dict) -> list[str]:
        f = [f"price:[..{s['max_price']:.2f}]", "priceCurrency:EUR"]
        if s["condition"] == "new":
            f.append("conditions:{NEW}")
        elif s["condition"] == "used":
            f.append("conditions:{USED}")
        return f

    @staticmethod
    def _location_filter(s: dict) -> str:
        return {
            "DE": "itemLocationCountry:DE",
            "EU": "itemLocationRegion:EUROPEAN_UNION",
        }.get(s["location"], "itemLocationRegion:WORLDWIDE")

    async def _search_shipped(self, s: dict, formats: list[str], plz: str, now: float) -> list[dict]:
        """Angebote mit Versand nach Deutschland. Bei „Beides“ möglichst mit einer Abfrage für beide
        Formate: passen alle Treffer auf eine Seite (bis 200), reicht sie. Sonst getrennt, Auktionen nach
        Ende sortiert, Sofortkauf nach Neuheit, je bis zu 3 Seiten."""
        def filters(fmts: list[str]) -> list[str]:
            f = self._base_filters(s) + [
                self._location_filter(s),
                "buyingOptions:{" + "|".join(fmts) + "}",
                "deliveryCountry:DE",  # nur Angebote, die nach Deutschland versendet werden
            ]
            if s["free_shipping_only"]:
                f.append("maxDeliveryCost:0")
            return f

        found: list[dict] = []
        if len(formats) > 1 and now >= self._split_until.get(s["id"], 0):
            together = await self.ebay.search(s["query"], filters(formats), None, plz, max_pages=1)
            if getattr(together, "complete", True):
                return list(together)
            self._split_until[s["id"]] = now + SPLIT_FOR   # zu viele Treffer: eine Weile gleich getrennt
            found.extend(together)
        for fmt in formats:
            sort = "endingSoonest" if fmt == "AUCTION" else "newlyListed"
            found.extend(await self.ebay.search(s["query"], filters([fmt]), sort, plz))
        return found

    async def discover(self, s: dict, settings: dict) -> None:
        now = time.time()
        plz = (settings.get("plz") or "").strip()
        radius = db.setting_int(settings, "radius_km", 50)
        formats = FORMATS.get(s["format"], ["AUCTION"])
        pickup_wanted = s["pickup_mode"] != "hide" and bool(plz)
        pickup_now = pickup_wanted and (s["last_run"] is None
                                        or now - self._pickup_runs.get(s["id"], 0) >= PICKUP_EVERY)
        keep_pickup = pickup_wanted and not pickup_now   # dazwischen gilt, was die letzte Abholsuche fand
        found: dict[str, tuple[dict, bool]] = {}

        try:
            for summary in await self._search_shipped(s, formats, plz, now):
                if summary.get("itemId"):
                    found[summary["itemId"]] = (summary, False)

            if pickup_now:
                search_radius = radius if s["pickup_mode"] == "radius" else PICKUP_RADIUS_ALL_KM
                filters = self._base_filters(s) + [
                    "buyingOptions:{" + "|".join(formats) + "}",
                    "deliveryOptions:{SELLER_ARRANGED_LOCAL_PICKUP}",
                    "pickupCountry:DE",
                    f"pickupPostalCode:{plz}",
                    f"pickupRadius:{search_radius}",
                    "pickupRadiusUnit:km",
                ]
                for summary in await self.ebay.search(s["query"], filters, None, plz):
                    item_id = summary.get("itemId")
                    if item_id:   # auch Angebote mit Versand: so kennt Heuhaufen die Entfernung
                        found[item_id] = (summary, True)
        except EbayError as e:
            log.warning("Gesuch %s: %s", s["id"], e)
            db.ex("UPDATE searches SET last_run = ?, last_error = ? WHERE id = ?", (now, str(e), s["id"]))
            return
        if pickup_now:
            self._pickup_runs[s["id"]] = now

        excluded = util.exclusion_words(s["exclude"])
        hidden = db.hidden_ids(s["id"])   # von Hand ausgeblendet: weiter aktualisieren, aber nie melden
        seen: set[str] = set()
        missing_end: list[str] = []
        new_fixed: list[dict] = []
        as_fixed = {**s, "format": "fixed"}

        for item_id, (summary, pickup) in found.items():
            existing = db.q1("SELECT * FROM items WHERE search_id = ? AND item_id = ?", (s["id"], item_id))
            if keep_pickup and existing and existing["pickup"]:
                pickup = True   # ohne Abholsuche: Abholung und Entfernung von der letzten übernehmen
            data = util.parse_listing(summary, pickup)
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
        # nächste Abholsuche.
        for row in db.q("SELECT item_id, pickup FROM items WHERE search_id = ? AND status = 'active'", (s["id"],)):
            if row["item_id"] not in seen and not (keep_pickup and row["pickup"]):
                db.ex("UPDATE items SET status = 'dropped' WHERE search_id = ? AND item_id = ?",
                      (s["id"], row["item_id"]))

        # Manche Suchergebnisse liefern bei Auktionen keine Endzeit mit
        for item_id in missing_end[:MAX_END_LOOKUPS]:
            try:
                detail = await self.ebay.get_item(item_id, plz)
            except EbayError:
                break
            if detail:
                details.store(item_id, detail)   # Beschreibung für die Galerie gleich mitnehmen
            end = util.parse_listing(detail)["end_time"] if detail else None
            if end:
                db.ex("UPDATE items SET end_time = ? WHERE search_id = ? AND item_id = ?", (end, s["id"], item_id))

        await self._notify_fixed(s, settings, new_fixed)
        db.ex("UPDATE searches SET last_run = ?, last_error = NULL, initialized = 1 WHERE id = ?",
              (now, s["id"]))

    # ------------------------------------------------------------ Endspurt

    async def check_endings(self, settings: dict) -> None:
        now = time.time()
        rows = db.q(
            """
            SELECT i.* FROM items i JOIN searches s ON s.id = i.search_id
            WHERE s.active = 1 AND s.format IN ('auction', 'both')
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
        plz = (settings.get("plz") or "").strip()

        for row in rows:
            s = db.q1("SELECT * FROM searches WHERE id = ?", (row["search_id"],))
            if not s:
                continue
            try:
                item = await self._fresh_item(row, plz, "endspurt")
            except EbayError as e:
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
                    self.last_ntfy_error = str(e)
                    log.warning("ntfy: %s", e)
                    continue  # nächster Takt versucht es erneut
                self.last_ntfy_error = None
            db.ex(
                "UPDATE items SET end_checked = 1, notified_end = ? WHERE search_id = ? AND item_id = ?",
                (1 if view["within"] else 0, *key),
            )

    async def refresh(self, row: dict, settings: dict) -> dict | None:
        """Galerie: aktueller Stand eines Angebots samt Beschreibung und Geboten (details.py)."""
        return await self._fresh_item(row, (settings.get("plz") or "").strip(), "galerie")

    async def _fresh_item(self, row: dict, plz: str, purpose: str) -> dict | None:
        """Aktueller Stand eines gemerkten Angebots über getItem (eine Abfrage). None: bei eBay nicht mehr da.
        Beschreibung, Merkmale und Gebote landen nebenbei in details.py."""
        detail = await self.ebay.get_item(row["item_id"], plz, purpose=purpose)
        if detail is None:
            db.ex("UPDATE items SET status = 'gone' WHERE search_id = ? AND item_id = ?",
                  (row["search_id"], row["item_id"]))
            return None
        details.store(row["item_id"], detail)
        fresh = util.parse_listing(detail, bool(row["pickup"]))
        # Was getItem nicht (zuverlässig) liefert, aus der Suche übernehmen
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

    async def check_reminders(self, settings: dict) -> None:
        """Angebote mit gedrücktem „1 Min vorher erinnern“: kurz vor Ende den Preis holen und, wenn er
        noch passt, auffällig melden. Passt er nicht mehr, kommt ein stiller Hinweis."""
        now = time.time()
        rows = db.q(
            """
            SELECT i.* FROM items i JOIN searches s ON s.id = i.search_id
            WHERE s.active = 1 AND i.remind = 1 AND i.reminded = 0 AND i.status = 'active'
              AND i.end_time IS NOT NULL AND i.end_time > ? AND i.end_time - ? <= ?
              AND NOT EXISTS (SELECT 1 FROM hidden_items h
                              WHERE h.search_id = i.search_id AND h.item_id = i.item_id)
            ORDER BY i.end_time
            """,
            (now, self.REMIND_BEFORE + self.REMIND_LEAD, now),
        )
        plz = (settings.get("plz") or "").strip()
        for row in rows:
            key = (row["search_id"], row["item_id"])
            db.ex("UPDATE items SET reminded = 1 WHERE search_id = ? AND item_id = ?", key)   # nur ein Versuch
            s = db.q1("SELECT * FROM searches WHERE id = ?", (row["search_id"],))
            try:
                item = await self._fresh_item(row, plz, "erinnerung")
            except EbayError as e:
                log.warning("Erinnerung %s: %s", row["item_id"], e)
                item = row   # lieber mit dem letzten bekannten Preis erinnern als gar nicht
            if item is None or not notify.configured(settings):
                continue
            view = util.item_view(item, s, time.time())
            try:
                await notify.send(settings, **self._reminder_push(s, view, settings))
                self.last_ntfy_error = None
            except notify.NtfyError as e:
                self.last_ntfy_error = str(e)
                log.warning("ntfy: %s", e)

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
            "actions": [{"action": "view", "label": "Jetzt bei eBay bieten", "url": v["url"], "clear": True}],
        }

    # ------------------------------------------------------------ Durchschnittspreis

    async def update_market(self, settings: dict) -> None:
        """Holt Vergleichsangebote: zuerst die tägliche Suche je Gesuch, dann gezielte Abfragen für
        Angebote unter dem Limit – erst die, denen der Vergleich fehlt, dann wöchentlich die übrigen,
        damit der Preisverlauf weiterwächst (siehe market.py)."""
        if not market.enabled(settings) or time.time() < self._market_next:
            return
        self._market_next = time.time() + MARKET_EVERY
        market.prune()
        plz = (settings.get("plz") or "").strip()
        searches = db.q("SELECT * FROM searches WHERE active = 1 AND last_run IS NOT NULL ORDER BY id")

        for s in searches:   # höchstens eine Gesuch-Suche pro Durchgang, nur wenn es Angebote gibt
            key = market.search_key(s["id"])
            has_items = db.q1("SELECT 1 AS x FROM items WHERE search_id = ? AND status = 'active' LIMIT 1", (s["id"],))
            if has_items and market.due(key):
                await self._fetch_market(key, s["query"], self._market_filters(s), plz, s["exclude"])
                return

        now = time.time()
        wanted: dict[str, tuple[int, float, dict, dict]] = {}
        in_use: set[str] = set()
        for s in searches:
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
                    wanted[key] = (0 if missing else 1, v["end_time"] or float("inf"), s, v)
        market.touch(in_use, now)

        budget = min(MARKET_LOOKUPS_PER_RUN, market.daily_lookups(settings) - market.lookups_today())
        for key, (_prio, _end, s, v) in sorted(wanted.items(), key=lambda w: w[1][:2])[:max(0, budget)]:
            if key.startswith("epid:"):
                await self._fetch_market(key, None, self._lookup_filters(), plz, extra={"epid": key[5:]})
            else:
                brand = (s["query"].split() or [""])[0]
                number = market.raw_number(v["title"], key[3:])
                await self._fetch_market(key, f"{brand} {number}".strip(), self._lookup_filters(), plz)

    async def _fetch_market(self, key: str, q: str | None, filters: list[str], plz: str,
                            exclude: str = "", extra: dict | None = None) -> None:
        try:
            summaries = await self.ebay.search(q, filters, None, plz, max_pages=1, extra=extra,
                                               purpose="durchschnitt")
        except EbayError as e:
            log.info("Durchschnittspreis %s: %s", key, e)
            market.store(key, None)
            return
        complete = len(summaries) < market.PAGE_SIZE   # vor dem Filtern: hat eBay alles geliefert?
        excluded = util.exclusion_words(exclude)
        if excluded:
            summaries = [x for x in summaries if not any(w in (x.get("title") or "").lower() for w in excluded)]
        market.store(key, summaries, complete=complete)

    def _market_filters(self, s: dict) -> list[str]:
        """Wie die Suche des Gesuchs, aber nur Sofortkauf und ohne Höchstpreis."""
        filters = ["priceCurrency:EUR", "buyingOptions:{FIXED_PRICE}", "deliveryCountry:DE", self._location_filter(s)]
        if s["condition"] == "new":
            filters.append("conditions:{NEW}")
        elif s["condition"] == "used":
            filters.append("conditions:{USED}")
        return filters

    @staticmethod
    def _lookup_filters() -> list[str]:
        """Gezielte Abfragen gelten für alle Gesuche: Sofortkauf aus Deutschland, jeder Zustand."""
        return ["priceCurrency:EUR", "buyingOptions:{FIXED_PRICE}", "deliveryCountry:DE", "itemLocationCountry:DE"]

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
                self.last_ntfy_error = None
            except notify.NtfyError as e:
                self.last_ntfy_error = str(e)
                log.warning("ntfy: %s", e)
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
        Heuhaufen eingetragen ist (das Handy muss sie erreichen), sonst direkt von eBay."""
        base = self._app_base(settings)
        path = images.url(s["id"], v["item_id"], v["image"])
        if base and path:
            return base + path
        return util.large_image(v["image"])

    def _actions(self, settings: dict, s: dict, v: dict, remind: bool = False) -> list[dict]:
        """Knöpfe der Benachrichtigung (ntfy erlaubt höchstens drei)."""
        actions = [{"action": "view", "label": "Bei eBay öffnen", "url": v["url"], "clear": True}]
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
        """z. B. „Zustand: Gebraucht“ (eBay-Wortlaut, ohne Zusatz in Klammern)."""
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

    async def test_ebay(self) -> None:
        try:
            limits = await self.ebay.rate_limits()
            self.last_ebay_check = {"ok": True, "at": time.time(), **limits}
        except EbayError as e:
            self.last_ebay_check = {"ok": False, "at": time.time(), "error": str(e)}
