"""Hintergrundprüfung.

Zwei getrennte Abläufe:
* Entdecken: jedes aktive Gesuch wird im eingestellten Intervall nach neuen Angeboten durchsucht.
* Endspurt: jede gemerkte Auktion wird genau einmal X Minuten vor ihrem Ende geprüft.
Sofortkauf-Angebote unter dem Limit werden direkt beim Entdecken gemeldet.
Dazu alle paar Minuten: Vergleichsangebote für den Durchschnittspreis holen (market.py).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from . import db, images, market, notify, util
from .ebay import EbayClient, EbayError

log = logging.getLogger("ebay-watcher")

TICK_SECONDS = 30
MAX_SINGLE_PUSHES = 5          # mehr neue Sofortkauf-Treffer auf einmal -> eine Sammelnachricht
PICKUP_RADIUS_ALL_KM = 1000    # Abholmodus "alle anzeigen": praktisch ganz Deutschland
MAX_END_LOOKUPS = 15           # fehlende Endzeiten pro Lauf über getItem nachladen
MARKET_EVERY = 300             # Durchschnittspreis: alle 5 Minuten nachsehen, ob etwas zu holen ist …
MARKET_LOOKUPS_PER_RUN = 3     # … und dann höchstens so viele gezielte Abfragen auf einmal

ITEM_FIELDS = [
    "title", "url", "image", "is_auction", "has_bin", "price", "bin_price", "currency",
    "shipping", "shipping_known", "bid_count", "end_time", "country", "location_text",
    "pickup", "pickup_only", "distance_km", "epid", "cond",
]
FORMATS = {"auction": ["AUCTION"], "fixed": ["FIXED_PRICE"], "both": ["AUCTION", "FIXED_PRICE"]}


class Watcher:
    def __init__(self) -> None:
        self.ebay = EbayClient()
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.last_tick: float | None = None
        self.last_ebay_check: dict | None = None
        self.last_ntfy_error: str | None = None
        self._market_next = 0.0

    # ------------------------------------------------------------ Lebenszyklus

    def start(self) -> None:
        self.task = asyncio.create_task(self._loop(), name="ebay-watcher")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        await self.ebay.close()

    def poke(self) -> None:
        self.wake.set()

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
                await asyncio.wait_for(self.wake.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def tick(self) -> None:
        self.last_tick = time.time()
        db.prune()
        images.cleanup_if_due()
        if not self.ebay.configured:
            return
        settings = db.get_settings()
        await self.check_endings(settings)
        interval = max(5, db.setting_int(settings, "interval_minutes", 15)) * 60
        for s in db.q("SELECT * FROM searches WHERE active = 1 ORDER BY id"):
            if s["last_run"] is None or time.time() - s["last_run"] >= interval:
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

    async def discover(self, s: dict, settings: dict) -> None:
        now = time.time()
        plz = (settings.get("plz") or "").strip()
        formats = FORMATS.get(s["format"], ["AUCTION"])
        found: dict[str, tuple[dict, bool]] = {}

        try:
            for fmt in formats:
                filters = self._base_filters(s) + [
                    self._location_filter(s),
                    f"buyingOptions:{{{fmt}}}",
                    "deliveryCountry:DE",  # nur Angebote, die nach Deutschland versendet werden
                ]
                if s["free_shipping_only"]:
                    filters.append("maxDeliveryCost:0")
                sort = "endingSoonest" if fmt == "AUCTION" else "newlyListed"
                for summary in await self.ebay.search(s["query"], filters, sort, plz):
                    if summary.get("itemId"):
                        found[summary["itemId"]] = (summary, False)

            if s["pickup_mode"] != "hide" and plz:
                radius = (db.setting_int(settings, "radius_km", 50)
                          if s["pickup_mode"] == "radius" else PICKUP_RADIUS_ALL_KM)
                filters = self._base_filters(s) + [
                    "buyingOptions:{" + "|".join(formats) + "}",
                    "deliveryOptions:{SELLER_ARRANGED_LOCAL_PICKUP}",
                    "pickupCountry:DE",
                    f"pickupPostalCode:{plz}",
                    f"pickupRadius:{radius}",
                    "pickupRadiusUnit:km",
                ]
                for summary in await self.ebay.search(s["query"], filters, None, plz):
                    item_id = summary.get("itemId")
                    if item_id and item_id not in found:
                        found[item_id] = (summary, True)
        except EbayError as e:
            log.warning("Gesuch %s: %s", s["id"], e)
            db.ex("UPDATE searches SET last_run = ?, last_error = ? WHERE id = ?", (now, str(e), s["id"]))
            return

        excluded = util.exclusion_words(s["exclude"])
        hidden = db.hidden_ids(s["id"])   # von Hand ausgeblendet: weiter aktualisieren, aber nie melden
        seen: set[str] = set()
        missing_end: list[str] = []
        new_fixed: list[dict] = []
        as_fixed = {**s, "format": "fixed"}

        for item_id, (summary, pickup) in found.items():
            data = util.parse_listing(summary, pickup)
            title = data["title"].lower()
            if any(word in title for word in excluded):
                continue
            seen.add(item_id)

            existing = db.q1("SELECT * FROM items WHERE search_id = ? AND item_id = ?", (s["id"], item_id))
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

        # Nicht mehr gefunden: über Limit, beendet oder Filter geändert
        for row in db.q("SELECT item_id FROM items WHERE search_id = ? AND status = 'active'", (s["id"],)):
            if row["item_id"] not in seen:
                db.ex("UPDATE items SET status = 'dropped' WHERE search_id = ? AND item_id = ?",
                      (s["id"], row["item_id"]))

        # Manche Suchergebnisse liefern bei Auktionen keine Endzeit mit
        for item_id in missing_end[:MAX_END_LOOKUPS]:
            try:
                detail = await self.ebay.get_item(item_id, plz)
            except EbayError:
                break
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
                detail = await self.ebay.get_item(row["item_id"], plz)
            except EbayError as e:
                log.warning("Endspurt-Prüfung %s: %s", row["item_id"], e)
                return  # beim nächsten Takt erneut versuchen

            key = (s["id"], row["item_id"])
            if detail is None:
                db.ex("UPDATE items SET status = 'gone', end_checked = 1 WHERE search_id = ? AND item_id = ?", key)
                continue

            fresh = util.parse_listing(detail, bool(row["pickup"]))
            # Was getItem nicht (zuverlässig) liefert, aus der Suche übernehmen
            for field in ("pickup", "pickup_only", "distance_km"):
                fresh[field] = row[field]
            if fresh["pickup_only"]:
                fresh["shipping"], fresh["shipping_known"] = 0.0, 1
            elif not fresh["shipping_known"] and row["shipping_known"]:
                fresh["shipping"], fresh["shipping_known"] = row["shipping"], 1
            for field in ("end_time", "image", "url", "title", "epid", "cond"):
                if not fresh[field]:
                    fresh[field] = row[field]
            self._update_item(s["id"], row["item_id"], fresh, time.time(), reset_end=False)

            item = {**row, **fresh, "status": "active"}
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

    # ------------------------------------------------------------ Durchschnittspreis

    async def update_market(self, settings: dict) -> None:
        """Holt Vergleichsangebote: zuerst die tägliche Suche je Gesuch, dann gezielte Abfragen für
        Angebote unter dem Limit, denen noch der Vergleich fehlt (siehe market.py)."""
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
        wanted: dict[str, tuple[float, dict, dict]] = {}
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
                if key not in wanted and comparison.compare(v) is None and market.due(key, now):
                    wanted[key] = (v["end_time"] or float("inf"), s, v)   # bald endende Auktionen zuerst
        market.touch(in_use, now)

        budget = min(MARKET_LOOKUPS_PER_RUN, market.daily_lookups(settings) - market.lookups_today())
        for key, (_end, s, v) in sorted(wanted.items(), key=lambda w: w[1][0])[:max(0, budget)]:
            if key.startswith("epid:"):
                await self._fetch_market(key, None, self._lookup_filters(), plz, extra={"epid": key[5:]})
            else:
                brand = (s["query"].split() or [""])[0]
                number = market.raw_number(v["title"], key[3:])
                await self._fetch_market(key, f"{brand} {number}".strip(), self._lookup_filters(), plz)

    async def _fetch_market(self, key: str, q: str | None, filters: list[str], plz: str,
                            exclude: str = "", extra: dict | None = None) -> None:
        try:
            summaries = await self.ebay.search(q, filters, None, plz, max_pages=1, extra=extra)
        except EbayError as e:
            log.info("Durchschnittspreis %s: %s", key, e)
            market.store(key, None)
            return
        excluded = util.exclusion_words(exclude)
        if excluded:
            summaries = [x for x in summaries if not any(w in (x.get("title") or "").lower() for w in excluded)]
        market.store(key, summaries)

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
                    await notify.send(
                        settings,
                        title=f"{util.short(name, 40)}: {len(views)} Sofortkauf-Angebote unter Limit",
                        message=(f"Das günstigste kostet {util.eur(cheapest['total'])}. "
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
        Ebay Watcher eingetragen ist (das Handy muss sie erreichen), sonst direkt von eBay."""
        base = self._app_base(settings)
        path = images.url(s["id"], v["item_id"], v["image"])
        if base and path:
            return base + path
        return util.large_image(v["image"])

    def _actions(self, settings: dict, s: dict, v: dict) -> list[dict]:
        actions = [{"action": "view", "label": "Bei eBay öffnen", "url": v["url"], "clear": True}]
        link = self._app_link(settings, s)
        if link:
            actions.append({"action": "view", "label": "In Ebay Watcher öffnen", "url": link})
        return actions

    @staticmethod
    def _price_line(v: dict, label: str) -> str:
        line = f"{label} {util.eur(v['rel_price'])}"
        if v["show_shipping"]:
            return f"{line} + {util.eur(v['shipping'])} Versand = {util.eur(v['total'])}"
        if v["shipping_known"] and not v["shipping"] and not v["pickup_only"]:
            return f"{line}, Versand kostenlos"
        return line

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
            f"{self._price_line(v, 'Aktuell')}, {bids} {'Gebot' if bids == 1 else 'Gebote'}",
            *self._market_line(s, v, settings),
            util.limit_text(s),
            *v["flags"],
        ]
        return {
            "title": f"Noch {minutes} Min: {util.short(s['name'] or s['query'], 40)}",
            "message": "\n".join(lines),
            "click": v["url"],
            "image": self._image(settings, s, v),
            "priority": 5,
            "tags": ["hourglass_flowing_sand"],
            "actions": self._actions(settings, s, v),
        }

    def _fixed_push(self, s: dict, v: dict, settings: dict) -> dict:
        lines = [util.short(v["title"], 120), self._price_line(v, "Preis"), *self._market_line(s, v, settings),
                 util.limit_text(s), *v["flags"]]
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
        extra = ", end_checked = 0, notified_end = 0" if reset_end else ""
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
