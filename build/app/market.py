"""Durchschnittspreis (Ø) je Angebot.

Ebay Watcher vergleicht jedes Angebot mit aktuellen Sofortkauf-Angeboten desselben Produkts und
zeigt den Median ihrer Preise. Verkaufte Preise liefert die eBay-API nicht, es sind also Angebotspreise.

Woher die Vergleichsangebote kommen (Tabellen market_sources und market_items):
* search:<Gesuch>  einmal am Tag je Gesuch: Sofortkauf-Angebote zum Suchbegriff, ohne Höchstpreis.
* epid:<Nummer>    gezielt alle Angebote eines Produkts aus dem eBay-Katalog.
* nr:<Nummer>      gezielt nach einer Artikelnummer im Titel, z. B. Knipex 87 01 250 oder Festool 576982.
Gezielt gefragt wird nur für Angebote unter dem Limit, wenn die Gesuch-Suche zu wenig Vergleich bringt.
Die Antworten gelten 7 Tage, und es gibt ein Tageslimit (Einstellungen).

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
UNUSED_DAYS = 14              # nicht mehr gebrauchte Abfragen danach löschen
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
    fetched_at: float
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
        self.ignore = set(tokens(search.get("query") or ""))
        keys = {search_key(search["id"])} | {k for k in (lookup_key(v) for v in views) if k}
        self.listings: dict[str, list[Listing]] = {}
        for key, rows in _load(keys).items():
            self.listings[key] = [Listing(
                item_id=r["item_id"], title=r["title"] or "", epid=r["epid"], number=r["number"],
                cond=r["cond"], price=r["price"], shipping=r["shipping"], fetched_at=r["fetched_at"],
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
        total = v.get("total")
        if total is None or cond == "parts" or (v.get("currency") and v["currency"] != "EUR"):
            return None
        epid = v.get("epid")
        title = v.get("title") or ""
        number = article_number(title)
        sig = signature(title, self.ignore) if not (epid or number) else frozenset()

        candidates: dict[str, Listing] = {}
        keys = [search_key(self.search["id"])] + ([lookup_key(v)] if lookup_key(v) else [])
        for key in keys:
            for listing in self.listings.get(key, []):
                candidates.setdefault(listing.item_id, listing)

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

        with_shipping = bool(self.search.get("incl_shipping"))
        prices = [c.price + (c.shipping or 0) if with_shipping else c.price for c in same]
        median, low, high = _median_parts(prices)
        if median <= 0:
            return None
        delta = round((total - median) / median * 100)
        tone = "good" if delta <= -self.threshold else ("high" if delta >= self.threshold else "normal")
        return {
            "median": median, "low": low, "high": high, "n": len(same), "delta": delta, "tone": tone,
            "delta_text": "±0 %" if delta == 0 else f"{'−' if delta < 0 else '+'}{abs(delta)} %",
            "hint": _hint(basis, number and raw_number(title, number), len(same), low, high,
                          cond if self.split else None, min(c.fetched_at for c in same), with_shipping),
        }


def _hint(basis: str, number: str | None, n: int, low: float, high: float, cond: str | None,
          fetched_at: float, with_shipping: bool) -> str:
    what = {"Katalog": "desselben Produkts im eBay-Katalog",
            "Nummer": f"mit der Artikelnummer {number}",
            "Titel": "mit sehr ähnlichem Titel"}[basis]
    kind = {"new": "neuen ", "used": "gebrauchten "}.get(cond or "", "")
    range_ = f"{util.eur(low, cents=False)} bis {util.eur(high, cents=False)}"
    return (f"Ø = Median aus {n} {kind}Sofortkauf-Angeboten {what}"
            f"{', mit Versand' if with_shipping else ''}. Mittlere Hälfte {range_}. "
            f"Stand {util.ago(fetched_at)}.")


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

def _load(keys: set[str]) -> dict[str, list[dict]]:
    if not keys:
        return {}
    marks = ",".join("?" * len(keys))
    rows = db.q(f"SELECT i.*, s.fetched_at FROM market_items i JOIN market_sources s ON s.key = i.key "
                f"WHERE i.key IN ({marks})", list(keys))
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


def store(key: str, summaries: list[dict] | None, now: float | None = None) -> int:
    """Speichert die Antwort einer Abfrage. summaries=None heißt: Abfrage fehlgeschlagen."""
    now = now or time.time()
    db.ex("INSERT INTO market_sources(key, fetched_at, ok, last_used) VALUES (?, ?, ?, ?) "
          "ON CONFLICT(key) DO UPDATE SET fetched_at = excluded.fetched_at, ok = excluded.ok",
          (key, now, 0 if summaries is None else 1, now))
    if summaries is None:
        return 0
    db.ex("DELETE FROM market_items WHERE key = ?", (key,))
    count = 0
    for summary in summaries:
        data = util.parse_listing(summary)
        price = data["bin_price"] if data["bin_price"] is not None else data["price"]
        if not data["item_id"] or price is None or data["currency"] != "EUR":
            continue
        db.ex("INSERT OR REPLACE INTO market_items(key, item_id, title, epid, number, cond, price, shipping) "
              "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (key, data["item_id"], data["title"], data["epid"], article_number(data["title"]),
               data["cond"], price, data["shipping"] if data["shipping_known"] else None))
        count += 1
    return count


def touch(keys: set[str], now: float | None = None) -> None:
    for key in keys:
        db.ex("UPDATE market_sources SET last_used = ? WHERE key = ?", (now or time.time(), key))


def forget_search(search_id: int) -> None:
    """Nach dem Bearbeiten eines Gesuchs: Vergleichssuche beim nächsten Durchgang neu holen."""
    db.ex("DELETE FROM market_sources WHERE key = ?", (search_key(search_id),))


def lookups_today() -> int:
    start = datetime.now(ZoneInfo(config.TZ)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    row = db.q1("SELECT COUNT(*) AS n FROM market_sources WHERE key NOT LIKE 'search:%' AND fetched_at >= ?",
                (start,))
    return row["n"] if row else 0


def prune(now: float | None = None) -> None:
    now = now or time.time()
    db.ex("DELETE FROM market_sources WHERE key LIKE 'search:%' AND CAST(substr(key, 8) AS INTEGER) "
          "NOT IN (SELECT id FROM searches)")
    db.ex("DELETE FROM market_sources WHERE key NOT LIKE 'search:%' AND last_used < ?",
          (now - UNUSED_DAYS * 86400,))
