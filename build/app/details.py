"""Angaben zu einem Angebot, die nur die Einzelabfrage getItem liefert: Beschreibung des Verkäufers,
Zustandsbeschreibung, Artikelmerkmale und der Stand der Gebote. Die Galerie zeigt sie in der Seitenleiste.

* Gespeichert wird bei jeder Einzelabfrage, auch bei Endspurt und Erinnerung (dann ohne zusätzliche
  Abfrage). Öffnet man die Galerie, fragt Ebay Watcher nur nach, wenn der Stand älter als MAX_AGE ist.
* Die Beschreibung ist HTML von fremden Verkäufern. Gespeichert und angezeigt wird nur bereinigter Text:
  Absätze, Listenpunkte und Zwischenüberschriften, ohne Bilder, Links, Stile und Skripte. Sehr lange
  Beschreibungen werden gekürzt, die ganze steht bei eBay.
"""
from __future__ import annotations

import json
import re
import time
from html.parser import HTMLParser

from . import db

MAX_AGE = 600              # Galerie: höchstens alle 10 Minuten neu bei eBay nachfragen
MAX_CHARS = 4000           # so viel Beschreibung zeigt die Seitenleiste höchstens …
MAX_BLOCKS = 60            # … in höchstens so vielen Absätzen
MAX_ASPECTS = 12
MAX_NOTE = 600

_BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "aside", "blockquote", "pre",
          "table", "tbody", "thead", "tr", "td", "th", "ul", "ol", "dl", "dt", "dd", "center", "form",
          "figure", "figcaption", "hr", "address", "fieldset"}
_HEADING = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP = {"script", "style", "noscript", "head", "title", "iframe", "object", "embed", "svg", "template",
         "select", "option", "button", "textarea"}


class _Text(HTMLParser):
    """Zerlegt HTML in Absätze: [{"t": "p" | "li" | "h", "text": …}]."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self.kind = "p"
        self.buf: list[str] = []
        self.skip = 0

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        self.buf = []
        if text and any(c.isalnum() for c in text):
            self.blocks.append({"t": self.kind, "text": text})
        self.kind = "p"

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag == "br":
            self._flush()
        elif tag == "li":
            self._flush()
            self.kind = "li"
        elif tag in _HEADING:
            self._flush()
            self.kind = "h"
        elif tag in _BLOCK:
            self._flush()
        elif tag == "img":
            self.buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK or tag in _HEADING or tag == "li":
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def text_blocks(html: str | None) -> tuple[list[dict], bool]:
    """Beschreibung als Absätze, höchstens MAX_CHARS Zeichen. Zweiter Wert: gekürzt?"""
    if not html:
        return [], False
    parser = _Text()
    try:
        parser.feed(html)
        parser.close()
    except Exception:   # kaputtes HTML: lieber gar keine Beschreibung als eine falsche
        return [], False
    blocks, total, truncated = [], 0, False
    for block in parser.blocks:
        if blocks and block == blocks[-1]:
            continue   # Vorlagen wiederholen gern Zeilen
        if len(blocks) >= MAX_BLOCKS or total + len(block["text"]) > MAX_CHARS:
            room = MAX_CHARS - total
            if room > 200 and len(blocks) < MAX_BLOCKS:
                cut = block["text"][:room].rsplit(" ", 1)[0]
                blocks.append({"t": block["t"], "text": cut + " …"})
            truncated = True
            break
        blocks.append(block)
        total += len(block["text"])
    return blocks, truncated


def _money(obj: dict | None) -> float | None:
    try:
        return float((obj or {}).get("value"))
    except (TypeError, ValueError):
        return None


def from_item(detail: dict) -> dict:
    """Was aus einer getItem-Antwort gespeichert wird."""
    blocks, truncated = text_blocks(detail.get("description"))
    aspects = []
    for a in detail.get("localizedAspects") or []:
        name, value = (a.get("name") or "").strip(), (a.get("value") or "").strip()
        if name and value and len(value) <= 120:
            aspects.append([name, value])
        if len(aspects) >= MAX_ASPECTS:
            break
    note = re.sub(r"\s+", " ", detail.get("conditionDescription") or "").strip()
    reserve = detail.get("reservePriceMet")
    return {
        "description": blocks, "truncated": truncated,
        "condition_note": note[:MAX_NOTE] or None,
        "aspects": aspects,
        "bidders": detail.get("uniqueBidderCount"),
        "min_bid": _money(detail.get("minimumPriceToBid")),
        "reserve_met": None if reserve is None else int(bool(reserve)),
    }


def store(item_id: str, detail: dict, now: float | None = None) -> None:
    d = from_item(detail)
    db.ex(
        "INSERT INTO item_details(item_id, description, truncated, condition_note, aspects, bidders, min_bid, "
        "reserve_met, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(item_id) DO UPDATE SET "
        "description = excluded.description, truncated = excluded.truncated, condition_note = excluded.condition_note, "
        "aspects = excluded.aspects, bidders = excluded.bidders, min_bid = excluded.min_bid, "
        "reserve_met = excluded.reserve_met, fetched_at = excluded.fetched_at",
        (item_id, json.dumps(d["description"], ensure_ascii=False), int(d["truncated"]), d["condition_note"],
         json.dumps(d["aspects"], ensure_ascii=False), d["bidders"], d["min_bid"], d["reserve_met"],
         now or time.time()),
    )


def load(item_id: str) -> dict | None:
    row = db.q1("SELECT * FROM item_details WHERE item_id = ?", (item_id,))
    if not row:
        return None
    for key in ("description", "aspects"):
        try:
            row[key] = json.loads(row[key] or "[]")
        except ValueError:
            row[key] = []
    return row


def due(item_id: str, now: float | None = None) -> bool:
    """Galerie: bei eBay nachfragen? Ja, wenn noch nichts da ist oder der Stand älter als MAX_AGE ist."""
    row = db.q1("SELECT fetched_at FROM item_details WHERE item_id = ?", (item_id,))
    return not row or (now or time.time()) - row["fetched_at"] > MAX_AGE
