"""Reine Hilfsfunktionen: Formatierung, Anzeige von Angeboten, Limit-Prüfung."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config
from .plattformen import liste as plattformen

EU = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU",
    "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
}

COUNTRY_NAMES = {
    "DE": "Deutschland", "AT": "Österreich", "CH": "der Schweiz", "FR": "Frankreich",
    "IT": "Italien", "ES": "Spanien", "NL": "den Niederlanden", "BE": "Belgien",
    "PL": "Polen", "CZ": "Tschechien", "DK": "Dänemark", "SE": "Schweden",
    "FI": "Finnland", "IE": "Irland", "PT": "Portugal", "LU": "Luxemburg",
    "GB": "Großbritannien", "US": "den USA", "CA": "Kanada", "JP": "Japan",
    "CN": "China", "HK": "Hongkong", "TW": "Taiwan", "KR": "Südkorea",
    "AU": "Australien", "NO": "Norwegen", "UA": "der Ukraine", "TR": "der Türkei",
}

FORMAT_LABEL = {"auction": "Auktion", "fixed": "Sofortkauf", "both": "Auktion und Sofortkauf"}
LOCATION_LABEL = {"DE": "aus Deutschland", "EU": "aus der EU", "WORLD": "weltweit"}
CONDITION_LABEL = {"any": "", "new": "neu", "used": "gebraucht"}
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


# ---------------------------------------------------------------- Formatierung

def eur(value: float | None, cents: bool = True) -> str:
    if value is None:
        return "–"
    if not cents or float(value).is_integer():
        text = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
    else:
        text = f"{value:,.2f}"
    text = text.replace(",", "\u2009").replace(".", ",").replace("\u2009", ".")
    return f"{text}\u00a0€"


def countdown(end_ts: float | None, now: float | None = None) -> str:
    if not end_ts:
        return ""
    left = int(end_ts - (now or time.time()))
    if left <= 0:
        return "beendet"
    if left < 60:
        return "unter 1 Min"
    minutes = left // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days >= 1:
        return f"{days} {'Tag' if days == 1 else 'Tage'} {hours} Std"
    if hours >= 1:
        return f"{hours} Std {minutes} Min"
    return f"{minutes} Min"


def local_time(ts: float | None) -> str:
    if not ts:
        return ""
    d = datetime.fromtimestamp(ts, ZoneInfo(config.TZ))
    return f"{WEEKDAYS[d.weekday()]}, {d:%d.%m.} um {d:%H:%M}"


def date_time(ts: float | None) -> str:
    """z. B. „14.11.2026, 23:10“"""
    if not ts:
        return ""
    return f"{datetime.fromtimestamp(ts, ZoneInfo(config.TZ)):%d.%m.%Y, %H:%M}"


def ago(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "noch nie"
    sec = int((now or time.time()) - ts)
    if sec < 60:
        return "gerade eben"
    if sec < 3600:
        return f"vor {sec // 60} Min"
    if sec < 86400:
        return f"vor {sec // 3600} Std"
    return f"vor {sec // 86400} {'Tag' if sec // 86400 == 1 else 'Tagen'}"


def parse_price(text: str | None) -> float | None:
    """'1.234,50', '300', '300.5' -> float"""
    if text is None:
        return None
    t = str(text).strip().replace("€", "").replace(" ", "")
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v > 0 else None


def short(text: str, n: int = 70) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def describe_search(s: dict) -> str:
    parts = [f"{FORMAT_LABEL.get(s['format'], '')} {LOCATION_LABEL.get(s['location'], '')}".strip()]
    cond = CONDITION_LABEL.get(s.get("condition") or "any")
    if cond:
        parts.append(cond)
    if s.get("free_shipping_only"):
        parts.append("nur versandkostenfrei")
    if s.get("interval_minutes"):
        parts.append(interval_text(s["interval_minutes"]))
    return ", ".join(parts)


def interval_text(minutes: int) -> str:
    """Eigenes Suchintervall eines Gesuchs, z. B. „sucht stündlich“."""
    if minutes == 60:
        return "sucht stündlich"
    if minutes % 60 == 0:
        return f"sucht alle {minutes // 60} Std."
    return f"sucht alle {minutes} Min."


def limit_text(s: dict) -> str:
    return f"Limit {eur(s['max_price'], cents=False)} {'inkl.' if s['incl_shipping'] else 'ohne'} Versand"


def exclusion_words(text: str) -> list[str]:
    return [w.lower() for w in re.split(r"[,;\s]+", text or "") if w.strip()]


# ---------------------------------------------------------------- Limit-Prüfung

def is_auction_for(item: dict, search: dict) -> bool:
    return bool(item["is_auction"]) and search["format"] in ("auction", "both")


def relevant_price(item: dict, search: dict) -> float | None:
    if is_auction_for(item, search):
        return item["price"]
    if item["has_bin"] and item["bin_price"] is not None:
        return item["bin_price"]
    return item["price"]


def total_price(item: dict, search: dict) -> float | None:
    """Preis für das Limit. Abholbar im Umkreis: ohne Versand, man holt es ja ab."""
    base = relevant_price(item, search)
    if base is None:
        return None
    if search["incl_shipping"] and item["shipping_known"] and item["shipping"] and not item.get("near"):
        return base + item["shipping"]
    return base


def within_limit(item: dict, search: dict) -> bool:
    if item.get("currency") and item["currency"] != "EUR":
        return True  # nicht vergleichbar: lieber zeigen und markieren
    total = total_price(item, search)
    return total is not None and total <= search["max_price"] + 0.001


def item_flags(item: dict) -> list[str]:
    """Hinweise unter dem Titel. Der Standort erscheint nur, wenn er zählt: bei „Nur Abholung“
    (immer) und bei Angeboten, die man im eingestellten Umkreis abholen kann."""
    flags = []
    distance = f", {round(item['distance_km'])} km entfernt" if item.get("distance_km") is not None else ""
    if item["pickup_only"]:
        flags.append("Nur Abholung" + distance)
    elif item.get("near"):
        flags.append("Abholung möglich" + distance)
    if not item["pickup_only"] and not item["shipping_known"]:
        flags.append("Versandkosten unbekannt")
    country = item.get("country")
    if country and country not in EU:
        flags.append(f"Versand aus {COUNTRY_NAMES.get(country, country)}, Zoll möglich")
    if item.get("currency") and item["currency"] != "EUR":
        flags.append(f"Preis in {item['currency']}")
    return flags


def with_shipping(settings: dict) -> bool:
    """Preise mit Versand anzeigen (Einstellungen → Anzeige)? Gilt für die Anzeige, nicht für das Limit."""
    return settings.get("price_with_shipping", "1") == "1"


def gross_price(item: dict, search: dict) -> float | None:
    """Was man zahlt: Preis plus Versand, soweit bekannt. Abholbar im Umkreis: ohne Versand."""
    base = relevant_price(item, search)
    if base is None:
        return None
    if item.get("near"):
        return base
    return base + (item["shipping"] or 0) if item["shipping_known"] else base


def item_view(item: dict, search: dict, now: float) -> dict:
    total = total_price(item, search)
    maxp = search["max_price"] or 1
    auction = is_auction_for(item, search)
    return {
        **item,
        "kind": "Auktion" if auction else "Sofortkauf",
        "auction": auction,
        "rel_price": relevant_price(item, search),
        "total": total,                       # Preis für das Limit (mit oder ohne Versand je nach Gesuch)
        "gross": gross_price(item, search),   # Preis mit Versand für die Anzeige
        "ratio": (total or 0) / maxp,
        "within": within_limit(item, search),
        "flags": item_flags(item),
        "ends_in": (item["end_time"] - now) if item.get("end_time") else None,
        "image_count": image_count(item),
        "platform_name": plattformen.get(item.get("platform")).name,
        "condition": condition_label(item),
        "condition_full": condition_label(item, full=True),
        "defect": item.get("cond") == "parts",
    }


CONDITION_FALLBACK = {"new": "Neu", "used": "Gebraucht", "parts": "Als Ersatzteil/defekt"}


def condition_label(item: dict, full: bool = False) -> str | None:
    """Zustand im Wortlaut der Plattform. Kurz ohne Zusatz in Klammern:
    „Neu: Sonstige (siehe Artikelbeschreibung)“ → „Neu: Sonstige“."""
    text = (item.get("condition_text") or "").strip() or CONDITION_FALLBACK.get(item.get("cond") or "")
    if not text or full:
        return text or None
    return re.sub(r"\s*\([^)]*\)\s*$", "", text) or text


def image_count(item: dict) -> int:
    """Wie viele Bilder die Galerie zeigen kann (Hauptbild mitgezählt)."""
    try:
        found = json.loads(item.get("images") or "[]")
    except ValueError:
        found = []
    return len(found) if isinstance(found, list) and found else (1 if item.get("image") else 0)


def ending_first(v: dict) -> tuple:
    """Voreingestellte Sortierung „Endet zuerst“: nach Endzeit, Angebote ohne Endzeit zuletzt, dann nach Preis."""
    return (v.get("end_time") is None, v.get("end_time") or 0, v.get("total") or 0)


def search_overview(search: dict, items: list[dict], now: float, hidden: set[str] | None = None) -> dict:
    hidden = hidden or set()
    views = [
        item_view(i, search, now)
        for i in items
        if i["status"] == "active" and (not i["end_time"] or i["end_time"] > now)
        and i["item_id"] not in hidden
    ]
    within = [v for v in views if v["within"]]
    auctions = sorted((v for v in within if v["auction"] and v["end_time"]), key=lambda v: v["end_time"])
    fixed = sorted((v for v in within if not v["auction"]), key=lambda v: v["total"] or 0)
    feature = auctions[0] if auctions else (fixed[0] if fixed else None)
    return {
        **search,
        "title": search["name"] or search["query"],
        "description": describe_search(search),
        "limit": limit_text(search),
        "matches": sorted(within, key=ending_first),
        "over": sorted((v for v in views if not v["within"]), key=ending_first),
        "hidden_count": len(hidden),
        "has_distance": any(v.get("distance_km") is not None for v in within),
        "feature": feature,
        "count": len(within),
        "initial": (search["name"] or search["query"] or "?").strip()[:1].upper(),
    }


def dashboard_order(overview: dict) -> tuple:
    """Reihenfolge der Startseite: zuerst die Gesuche, deren nächste Auktion unter dem Limit am
    frühesten endet; dann aktive ohne laufende Auktion (neueste zuerst); pausierte zuletzt."""
    f = overview["feature"]
    end = f["end_time"] if f and f["auction"] and f.get("end_time") else None
    return (not overview["active"], end is None, end or 0, -(overview.get("created_at") or 0))


def hidden_view(row: dict, item: dict | None, search: dict, now: float) -> dict:
    """Eintrag für die Liste „Ausgeblendet“. Gibt es das Angebot nicht mehr, reicht die gespeicherte Kopie."""
    if item:
        view = item_view(item, search, now)
        if item["status"] != "active" or (item["end_time"] and item["end_time"] <= now):
            view["kind"] = "Beendet"
            view["end_time"] = None
    else:
        view = {
            "search_id": row["search_id"], "item_id": row["item_id"], "title": row["title"] or "",
            "url": row["url"] or "", "image": row["image"], "kind": "Beendet",
            "auction": False, "total": None, "rel_price": None, "ratio": 0.0, "within": False,
            "flags": [], "shipping_known": 0, "shipping": None, "gross": None,
            "pickup_only": 0, "near": 0, "bid_count": None, "location_text": "", "end_time": None,
            "first_seen": row["hidden_at"], "distance_km": None, "image_count": 1 if row["image"] else 0,
            "condition": None, "condition_full": None, "defect": False,
            "platform": row.get("platform"), "platform_name": plattformen.get(row.get("platform")).name,
        }
    view["hidden_at"] = row["hidden_at"]
    return view


def gauge(ratio: float) -> dict:
    """Werte für die Spielraum-Linie: Füllung bis zum Limit, Ton nach Nähe zum Limit."""
    fill = max(0.02, min(ratio, 1.0))
    tone = "calm" if ratio < 0.85 else ("tight" if ratio <= 1.0 else "over")
    return {"pct": round(fill * 100, 1), "tone": tone}


def device_label(ua: str | None) -> str:
    ua = ua or ""
    os_name = next((n for k, n in [("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                   ("Macintosh", "Mac"), ("Windows", "Windows"), ("Linux", "Linux")]
                    if k in ua), "Unbekanntes Gerät")
    browser = next((n for k, n in [("Edg/", "Edge"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
                                   ("Safari/", "Safari")] if k in ua), "")
    return f"{browser} auf {os_name}" if browser else os_name
