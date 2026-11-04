"""Durchschnittspreis (Ø) je Angebot.

Ebay Watcher vergleicht jedes Angebot mit Sofortkauf-Angeboten desselben Produkts und zeigt den Median
ihrer Preise. Verkaufte Preise liefert die eBay-API nicht, es sind also Angebotspreise.

Woher die Vergleichsangebote kommen (Tabellen market_sources und market_items):
* search:<Gesuch>  einmal am Tag je Gesuch: Sofortkauf-Angebote zum Suchbegriff, ohne Höchstpreis.
* epid:<Nummer>    gezielt alle Angebote eines Produkts aus dem eBay-Katalog.
* nr:<Nummer>      gezielt nach einer Artikelnummer im Titel, z. B. Knipex 87 01 250 oder Festool 576982.
Gezielt gefragt wird für Angebote unter dem Limit: zuerst, wenn ihnen der Vergleich fehlt, sonst
wöchentlich, damit der Verlauf weiterwächst. Es gibt ein Tageslimit (Einstellungen).

Preisverlauf: Jedes Vergleichsangebot bleibt gespeichert, mit dem zuletzt gesehenen Preis und dem
ersten und letzten Tag, an dem es zu sehen war. Für den Ø zählen alle Angebote, die im eingestellten
Zeitraum (Vorgabe 6 Monate) zu sehen waren, jedes nur einmal. So zählt ein überteuertes Angebot, das
monatelang online steht, nicht öfter als ein günstiges, das schnell weg ist.
Verschwindet ein Angebot vor seinem Ende, ist es vermutlich verkauft (sold). Das lässt sich nur sagen,
wenn eine Antwort vollständig war (unter 200 Angeboten) und das Gesuch zwischendurch nicht geändert wurde.

Wann zwei Angebote dasselbe Produkt sind:
1. gleiche eBay-Katalognummer (ePID), sonst
2. gleiche Artikelnummer, und zwar die erste im Titel (spätere Nummern stehen oft bei Sets oder
   „passend für …“), sonst
3. ohne Nummern: sehr ähnliche Titel. Verglichen werden die Wörter ohne Suchbegriff, Füllwörter und
   Maßangaben. Wörter, die mehrere, aber nicht fast alle Titel haben („Compact“, „Wireless“), zählen am
   meisten; Wörter aus nur einem Titel („optisch“) wenig.
"""
from __future__ import annotations

import math
import re
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, db, util

SEARCH_TTL = 86400            # Gesuch-Suche: einmal am Tag neu
LOOKUP_TTL = 7 * 86400        # gezielte Abfrage: eine Woche gültig
RETRY_SECONDS = 86400         # fehlgeschlagene Abfrage frühestens morgen wiederholen
PAGE_SIZE = 200               # eine Antwort (ebay.search mit max_pages=1); weniger = vollständig
MAX_MONTHS = 24               # längster einstellbarer Zeitraum; ältere Vergleichsangebote werden gelöscht
MONTH = 30.44 * 86400
MIN_COMPARABLES = 3           # weniger Vergleichsangebote: kein Ø
SIMILAR = 0.65                # Mindest-Ähnlichkeit der Titel ohne Nummern (0 bis 1)

# Wörter, die nichts über das Produkt sagen. Bewusst nicht dabei: „defekt“, „wireless“, „pro“ …
STOPWORDS = frozenset("""
neu neue neuer neues neuwertig neuwertige neuwertiger gebraucht gebrauchte gebrauchter gebrauchtes
ovp top topzustand wie sehr gut guter gute gutem zustand original originalverpackt orig unbenutzt
mit und oder für fur fr ohne inkl incl inklusive der die das den dem des ein eine einer eines im in
von vom zu zum zur auf aus bei bis nur nicht auch je
stück stk st teilig tlg farbe schwarz weiß weiss silber grau grey gray rot blau grün gruen gelb orange
braun beige pink lila violett usb kabel kabelgebunden kabellos maus mouse tasten made germany
privat privatverkauf verkauf verkaufe versand versandkostenfrei kostenlos rechnung mwst garantie
gewährleistung ware art nr no artikel artikelnummer ca rar rarität vintage selten alt alte np uvp
ean sb the and with for of new used box oem 3d voll funktionsfähig funktionsfaehig funktioniert getestet
geprüft gepflegt sauber leichte gebrauchsspuren""".split())

_UNIT = re.compile(r"^\d+(?:[.,]\d+)?(?:mm|cm|m|kg|g|v|w|ah|mah|l|ml|gb|tb|hz|khz|mhz|ghz|x|er|"
                   r"tlg|teilig|stk|st|qmm|mm2|hrc|nm|lm|zoll|bit|tasten)$")
_GROUPS3 = re.compile(r"(?<!\w)(\d{2})[ -](\d{2})[ -](\d{2,3})(?!\w)")   # Knipex: 87 01 250
_GROUPS2 = re.compile(r"(?<!\w)(\d{4}) (\d{3})(?!\w)")                     # 8701 250
_PREFIX = re.compile(r"(?<!\w)([a-z0-9]{1,5})-(\d{3,})(?!\w)")             # 3DX-700059
_TOKEN = re.compile(r"[a-z0-9äöüß]+")


# ---------------------------------------------------------------- Produkt erkennen

def normalize(title: str) -> str:
    t = (title or "").lower().replace("®", " ").replace("™", " ").replace("©", " ")
    t = _GROUPS3.sub(r"\1\2\3", t)
    t = _GROUPS2.sub(r"\1\2", t)
    return _PREFIX.sub(r"\1\2", t)


def tokens(title: str) -> list[str]:
    return _TOKEN.findall(normalize(title))


def _is_number(token: str) -> bool:
    """Artikelnummer, Katalog- oder EAN-Nummer: mindestens fünf Ziffern, keine Maßangabe."""
    return sum(c.isdigit() for c in token) >= 5 and not _UNIT.match(token) and not re.search(r"\dx\d", token)


def article_number(title: str) -> str | None:
    """Die erste Artikelnummer im Titel, vereinheitlicht (87 01 250 → 8701250)."""
    return next((t for t in tokens(title) if _is_number(t)), None)


def raw_number(title: str, number: str) -> str:
    """Die Nummer so geschrieben, wie sie im Titel steht (für die Suche bei eBay)."""
    pattern = r"[\s\-]?".join(map(re.escape, number))
    m = re.search(pattern, title or "", re.IGNORECASE)
    return m.group(0) if m else number


def signature(title: str, ignore: set[str]) -> frozenset[str]:
    return frozenset(t for t in tokens(title)
                     if len(t) > 1 and t not in STOPWORDS and t not in ignore
                     and not _UNIT.match(t) and not _is_number(t))


# ---------------------------------------------------------------- Einstellungen

def enabled(settings: dict) -> bool:
    return settings.get("market_enabled", "1") == "1"


def threshold(settings: dict) -> int:
    return max(1, min(90, db.setting_int(settings, "market_threshold", 15)))


def split_condition(settings: dict) -> bool:
    return settings.get("market_split", "1") == "1"


def daily_lookups(settings: dict) -> int:
    return max(0, min(500, db.setting_int(settings, "market_lookups", 50)))


def months(settings: dict) -> int:
    return max(1, min(MAX_MONTHS, db.setting_int(settings, "market_months", 6)))


# ---------------------------------------------------------------- Vergleich

@dataclass
class Listing:
    item_id: str
    title: str
    epid: str | None
    number: str | None
    cond: str | None
    price: float
    shipping: float | None
    fetched_at: float           # wann die Quelle zuletzt abgefragt wurde
    last_seen: float
    gone: bool                  # nicht mehr online
    sold: bool                  # vermutlich verkauft
    sig: frozenset[str] = frozenset()


def _median_parts(values: list[float]) -> tuple[float, float, float]:
    values = sorted(values)
    if len(values) < 4:
        return statistics.median(values), values[0], values[-1]
    q1, q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return q2, q1, q3


def search_key(search_id: int) -> str:
    return f"search:{search_id}"


def lookup_key(v: dict) -> str | None:
    """Schlüssel für eine gezielte Abfrage zu diesem Angebot, falls es eine Nummer hat."""
    if v.get("epid"):
        return f"epid:{v['epid']}"
    number = article_number(v.get("title") or "")
    return f"nr:{number}" if number else None


class Comparison:
    """Vergleichsangebote für ein Gesuch. Einmal aufbauen, dann für jedes Angebot compare() aufrufen."""

    def __init__(self, search: dict, settings: dict, views: list[dict]) -> None:
        self.search = search
        self.threshold = threshold(settings)
        self.split = split_condition(settings)
        self.months = months(settings)
        self.with_shipping = util.with_shipping(settings)   # wie die Anzeige: Ø neben dem angezeigten Preis
        self.ignore = set(tokens(search.get("query") or ""))
        keys = {search_key(search["id"])} | {k for k in (lookup_key(v) for v in views) if k}
        self.listings: dict[str, list[Listing]] = {}
        for key, rows in _load(keys, time.time() - self.months * MONTH).items():
            self.listings[key] = [Listing(
                item_id=r["item_id"], title=r["title"] or "", epid=r["epid"], number=r["number"],
                cond=r["cond"], price=r["price"], shipping=r["shipping"], fetched_at=r["fetched_at"],
                last_seen=r["last_seen"] or r["fetched_at"], gone=r["gone_at"] is not None, sold=bool(r["sold"]),
                sig=signature(r["title"] or "", self.ignore)) for r in rows]
        # Gewichte der Wörter: je seltener in der Gesuch-Suche, desto wichtiger
        base = self.listings.get(search_key(search["id"]), [])
        self._count = len(base)
        self._df: dict[str, int] = {}
        for listing in base:
            for word in listing.sig:
                self._df[word] = self._df.get(word, 0) + 1

    def _weight(self, word: str) -> float:
        """Wörter, die in mehreren Angeboten vorkommen, aber nicht in fast allen, unterscheiden Modelle
        („Compact“, „Wireless“). Wörter aus nur einem Titel sind meist Beschreibung („optisch“) und zählen wenig."""
        df = self._df.get(word, 0)
        if df <= 1:
            return 0.5
        return math.log((self._count + 1) / (df + 1)) + 1

    def _similarity(self, a: frozenset[str], b: frozenset[str]) -> float:
        union = a | b
        if not union:
            return 0.0
        total = sum(self._weight(w) for w in union)
        return sum(self._weight(w) for w in a & b) / total

    def compare(self, v: dict) -> dict | None:
        """Ø für ein Angebot oder None, wenn es nicht genug Vergleich gibt."""
        cond = v.get("cond")
        total = v.get("gross") if self.with_shipping else v.get("rel_price")
        if total is None or cond == "parts" or (v.get("currency") and v["currency"] != "EUR"):
            return None
        epid = v.get("epid")
        title = v.get("title") or ""
        number = article_number(title)
        sig = signature(title, self.ignore) if not (epid or number) else frozenset()

        # Jedes Angebot nur einmal. Steht es in mehreren Quellen, gilt der jüngste Stand
        # (in der einen schon verschwunden, in der anderen noch zu sehen = noch online).
        candidates: dict[str, Listing] = {}
        keys = [search_key(self.search["id"])] + ([lookup_key(v)] if lookup_key(v) else [])
        for key in keys:
            for listing in self.listings.get(key, []):
                known = candidates.get(listing.item_id)
                if known is None or listing.last_seen > known.last_seen:
                    candidates[listing.item_id] = listing

        if epid:
            basis = "Katalog"
            same = [c for c in candidates.values() if c.epid == epid or (number and c.number == number)]
        elif number:
            basis = "Nummer"
            same = [c for c in candidates.values() if c.number == number]
        elif sig:
            basis = "Titel"
            same = [c for c in candidates.values() if c.sig and self._similarity(sig, c.sig) >= SIMILAR]
        else:
            return None

        same = [c for c in same if c.item_id != v.get("item_id") and c.cond != "parts"
                and (not self.split or not cond or c.cond == cond)]
        if len(same) < MIN_COMPARABLES:
            return None

        with_shipping = self.with_shipping

        def price(c: Listing) -> float:
            return c.price + (c.shipping or 0) if with_shipping else c.price

        median, low, high = _median_parts([price(c) for c in same])
        if median <= 0:
            return None
        sold = [price(c) for c in same if c.sold]
        delta = round((total - median) / median * 100)
        tone = "good" if delta <= -self.threshold else ("high" if delta >= self.threshold else "normal")
        return {
            "median": median, "low": low, "high": high, "n": len(same), "delta": delta, "tone": tone,
            "online": sum(not c.gone for c in same), "sold": len(sold),
            "sold_median": statistics.median(sold) if len(sold) >= MIN_COMPARABLES else None,
            "delta_text": "±0 %" if delta == 0 else f"{'−' if delta < 0 else '+'}{abs(delta)} %",
            "hint": _hint(basis, number and raw_number(title, number), same, low, high, sold,
                          cond if self.split else None, self.months, with_shipping),
        }


def _hint(basis: str, number: str | None, same: list[Listing], low: float, high: float, sold: list[float],
          cond: str | None, period: int, with_shipping: bool) -> str:
    what = {"Katalog": "desselben Produkts im eBay-Katalog",
            "Nummer": f"mit der Artikelnummer {number}",
            "Titel": "mit sehr ähnlichem Titel"}[basis]
    kind = {"new": "neuen ", "used": "gebrauchten "}.get(cond or "", "")
    online = sum(not c.gone for c in same)
    parts = [f"{online} noch online"]
    if sold:
        parts.append(f"{len(sold)} vermutlich verkauft"
                     + (f" (deren Ø {util.eur(statistics.median(sold))})" if len(sold) >= MIN_COMPARABLES else ""))
    range_ = f"{util.eur(low, cents=False)} bis {util.eur(high, cents=False)}"
    when = "im letzten Monat" if period == 1 else f"in den letzten {period} Monaten"
    return (f"Ø = Median aus {len(same)} {kind}Sofortkauf-Angeboten {what}, die {when} zu sehen waren"
            f"{', mit Versand' if with_shipping else ''}. Davon {' und '.join(parts)}. "
            f"Mittlere Hälfte {range_}. Zuletzt aktualisiert {util.ago(max(c.fetched_at for c in same))}.")


def attach(search: dict, settings: dict, views: list[dict]) -> None:
    """Schreibt den Vergleich als v["market"] in jedes Angebot (oder None)."""
    if not views:
        return
    if not enabled(settings):
        for v in views:
            v["market"] = None
        return
    comparison = Comparison(search, settings, views)
    for v in views:
        v["market"] = comparison.compare(v)


# ---------------------------------------------------------------- Speicher

def _load(keys: set[str], since: float) -> dict[str, list[dict]]:
    """Vergleichsangebote dieser Quellen, die seit `since` zu sehen waren."""
    if not keys:
        return {}
    marks = ",".join("?" * len(keys))
    rows = db.q(f"SELECT i.*, s.fetched_at FROM market_items i JOIN market_sources s ON s.key = i.key "
                f"WHERE i.key IN ({marks}) AND COALESCE(i.last_seen, s.fetched_at) >= ?", [*keys, since])
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["key"], []).append(r)
    return out


def source(key: str) -> dict | None:
    return db.q1("SELECT * FROM market_sources WHERE key = ?", (key,))


def due(key: str, now: float | None = None) -> bool:
    """Muss diese Quelle (neu) abgefragt werden?"""
    now = now or time.time()
    s = source(key)
    if not s:
        return True
    ttl = SEARCH_TTL if key.startswith("search:") else LOOKUP_TTL
    return now - s["fetched_at"] >= (ttl if s["ok"] else RETRY_SECONDS)


def store(key: str, summaries: list[dict] | None, now: float | None = None,
          complete: bool | None = None) -> int:
    """Ergänzt den Verlauf um die Antwort einer Abfrage. summaries=None heißt: Abfrage fehlgeschlagen.
    complete: Enthält die Antwort alle Angebote? Nur dann gelten fehlende als verschwunden."""
    now = now or time.time()
    before = source(key)
    db.ex("INSERT INTO market_sources(key, fetched_at, ok, last_used) VALUES (?, ?, ?, ?) "
          "ON CONFLICT(key) DO UPDATE SET fetched_at = excluded.fetched_at, ok = excluded.ok",
          (key, now, 0 if summaries is None else 1, now))
    if summaries is None:
        return 0
    count = 0
    for summary in summaries:
        data = util.parse_listing(summary)
        price = data["bin_price"] if data["bin_price"] is not None else data["price"]
        if not data["item_id"] or price is None or data["currency"] != "EUR":
            continue
        db.ex("INSERT INTO market_items(key, item_id, title, epid, number, cond, price, shipping, end_time, "
              "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
              "ON CONFLICT(key, item_id) DO UPDATE SET title = excluded.title, epid = excluded.epid, "
              "number = excluded.number, cond = excluded.cond, price = excluded.price, shipping = excluded.shipping, "
              "end_time = excluded.end_time, last_seen = excluded.last_seen, gone_at = NULL, sold = 0",
              (key, data["item_id"], data["title"], data["epid"], article_number(data["title"]), data["cond"],
               price, data["shipping"] if data["shipping_known"] else None, data["end_time"], now, now))
        count += 1
    if complete is None:
        complete = len(summaries) < PAGE_SIZE
    # Nach dem Bearbeiten eines Gesuchs (fetched_at = 0) fehlen Angebote, weil die Suche anders ist,
    # nicht weil sie verkauft sind.
    if complete and before and before["fetched_at"] > 0 and before["ok"]:
        for r in db.q("SELECT item_id, end_time FROM market_items WHERE key = ? AND gone_at IS NULL "
                      "AND last_seen < ?", (key, now)):
            ended = r["end_time"] is not None and r["end_time"] <= now + 3600
            db.ex("UPDATE market_items SET gone_at = ?, sold = ? WHERE key = ? AND item_id = ?",
                  (now, 0 if ended else 1, key, r["item_id"]))
    return count


def touch(keys: set[str], now: float | None = None) -> None:
    for key in keys:
        db.ex("UPDATE market_sources SET last_used = ? WHERE key = ?", (now or time.time(), key))


def forget_search(search_id: int) -> None:
    """Nach dem Bearbeiten eines Gesuchs: Vergleichssuche beim nächsten Durchgang neu holen.
    Der Verlauf bleibt; fehlende Angebote gelten beim nächsten Mal aber nicht als verschwunden."""
    db.ex("UPDATE market_sources SET fetched_at = 0 WHERE key = ?", (search_key(search_id),))


def lookups_today() -> int:
    start = datetime.now(ZoneInfo(config.TZ)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    row = db.q1("SELECT COUNT(*) AS n FROM market_sources WHERE key NOT LIKE 'search:%' AND fetched_at >= ?",
                (start,))
    return row["n"] if row else 0


def prune(now: float | None = None) -> None:
    """Verlauf aufräumen: gelöschte Gesuche, Angebote älter als der längste Zeitraum, Quellen ohne Angebote."""
    now = now or time.time()
    oldest = now - MAX_MONTHS * MONTH
    db.ex("DELETE FROM market_sources WHERE key LIKE 'search:%' AND CAST(substr(key, 8) AS INTEGER) "
          "NOT IN (SELECT id FROM searches)")
    db.ex("DELETE FROM market_items WHERE COALESCE(last_seen, 0) < ?", (oldest,))
    db.ex("DELETE FROM market_sources WHERE key NOT LIKE 'search:%' AND last_used < ? "
          "AND NOT EXISTS (SELECT 1 FROM market_items i WHERE i.key = market_sources.key)", (oldest,))
