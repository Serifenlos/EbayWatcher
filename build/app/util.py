"""Reine Hilfsfunktionen: Formatierung, Auswertung von eBay-Angeboten, Limit-Prüfung."""
from __future__ import annotations

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config

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


def large_image(url: str | None, size: int = 500) -> str | None:
    if not url:
        return None
    return re.sub(r"s-l\d+", f"s-l{size}", url)


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
    return ", ".join(parts)


def limit_text(s: dict) -> str:
    return f"Limit {eur(s['max_price'], cents=False)} {'inkl.' if s['incl_shipping'] else 'ohne'} Versand"


def exclusion_words(text: str) -> list[str]:
    return [w.lower() for w in re.split(r"[,;\s]+", text or "") if w.strip()]


# ---------------------------------------------------------------- eBay-Angebote auswerten

def _money(obj: dict | None) -> tuple[float | None, str | None]:
    if not obj or obj.get("value") in (None, ""):
        return None, None
    try:
        return float(obj["value"]), obj.get("currency")
    except (TypeError, ValueError):
        return None, None


def _iso_ts(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_listing(d: dict, pickup: bool = False) -> dict:
    """Wandelt eine eBay item_summary- oder getItem-Antwort in unser Format um.
    Verkäuferdaten werden absichtlich nicht übernommen."""
    buying = d.get("buyingOptions") or []
    is_auction = "AUCTION" in buying
    has_bin = "FIXED_PRICE" in buying

    price, currency = _money(d.get("price"))
    bin_price = price if has_bin else None
    if is_auction:
        bid, bid_cur = _money(d.get("currentBidPrice"))
        if bid is not None:
            price, currency = bid, bid_cur or currency

    ship_opts = d.get("shippingOptions") or []
    costs = []
    for opt in ship_opts:
        value, _cur = _money(opt.get("shippingCost"))
        if value is not None:
            costs.append(value)
    shipping, shipping_known = (min(costs), 1) if costs else (None, 0)
    pickup_only = 1 if pickup and not ship_opts else 0
    if pickup_only:
        shipping, shipping_known = 0.0, 1

    loc = d.get("itemLocation") or {}
    loc_text = " ".join(x for x in [loc.get("postalCode"), loc.get("city")] if x)

    distance = None
    dist = d.get("distanceFromPickupLocation") or {}
    try:
        if dist.get("value") is not None:
            distance = float(dist["value"])
            if (dist.get("unit") or "").upper().startswith("MI"):
                distance *= 1.609
    except (TypeError, ValueError):
        distance = None

    image = (d.get("image") or {}).get("imageUrl")
    if not image:
        thumbs = d.get("thumbnailImages") or d.get("additionalImages") or []
        image = thumbs[0].get("imageUrl") if thumbs else None

    return {
        "item_id": d.get("itemId"),
        "title": d.get("title") or "",
        "url": d.get("itemWebUrl") or d.get("itemAffiliateWebUrl") or "",
        "image": image,
        "is_auction": int(is_auction),
        "has_bin": int(has_bin),
        "price": price,
        "bin_price": bin_price,
        "currency": currency or "EUR",
        "shipping": shipping,
        "shipping_known": shipping_known,
        "bid_count": d.get("bidCount"),
        "end_time": _iso_ts(d.get("itemEndDate")),
        "country": loc.get("country"),
        "location_text": loc_text,
        "pickup": int(pickup),
        "pickup_only": pickup_only,
        "distance_km": distance,
    }


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
    base = relevant_price(item, search)
    if base is None:
        return None
    if search["incl_shipping"] and item["shipping_known"] and item["shipping"]:
        return base + item["shipping"]
    return base


def within_limit(item: dict, search: dict) -> bool:
    if item.get("currency") and item["currency"] != "EUR":
        return True  # nicht vergleichbar: lieber zeigen und markieren
    total = total_price(item, search)
    return total is not None and total <= search["max_price"] + 0.001


def item_flags(item: dict) -> list[str]:
    flags = []
    if item["pickup"]:
        label = "Nur Abholung" if item["pickup_only"] else "Abholung möglich"
        if item.get("distance_km") is not None:
            label += f", {round(item['distance_km'])} km entfernt"
        flags.append(label)
    elif not item["shipping_known"]:
        flags.append("Versandkosten unbekannt")
    country = item.get("country")
    if country and country not in EU:
        flags.append(f"Versand aus {COUNTRY_NAMES.get(country, country)}, Zoll möglich")
    if item.get("currency") and item["currency"] != "EUR":
        flags.append(f"Preis in {item['currency']}")
    return flags


def item_view(item: dict, search: dict, now: float) -> dict:
    total = total_price(item, search)
    maxp = search["max_price"] or 1
    auction = is_auction_for(item, search)
    return {
        **item,
        "kind": "Auktion" if auction else "Sofortkauf",
        "auction": auction,
        "rel_price": relevant_price(item, search),
        "total": total,
        "ratio": (total or 0) / maxp,
        "within": within_limit(item, search),
        "flags": item_flags(item),
        "show_shipping": bool(search["incl_shipping"] and item["shipping_known"] and item["shipping"]),
        "image_large": large_image(item.get("image")),
        "ends_in": (item["end_time"] - now) if item.get("end_time") else None,
    }


def search_overview(search: dict, items: list[dict], now: float) -> dict:
    views = [
        item_view(i, search, now)
        for i in items
        if i["status"] == "active" and (not i["end_time"] or i["end_time"] > now)
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
        "matches": auctions + fixed,
        "over": sorted((v for v in views if not v["within"]), key=lambda v: v["total"] or 0),
        "feature": feature,
        "count": len(within),
        "initial": (search["name"] or search["query"] or "?").strip()[:1].upper(),
    }


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
