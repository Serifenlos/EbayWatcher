"""Signierte Adressen für Aufrufe ohne Login.

Bilder für ntfy (/bild/…) und Knöpfe in Benachrichtigungen (/erinnern/…) werden vom Handy aufgerufen,
ohne angemeldet zu sein. Gültig sind sie nur mit einer Signatur, die nur Ebay Watcher erzeugen kann.
Der geheime Schlüssel entsteht beim ersten Gebrauch und bleibt in der Datenbank (settings „image_secret“,
so heißt er, weil ihn zuerst nur der Bild-Cache brauchte).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from . import db

_secret: bytes | None = None


def key() -> bytes:
    global _secret
    if _secret is None:
        db.ex("INSERT INTO settings(key, value) VALUES ('image_secret', ?) ON CONFLICT(key) DO NOTHING",
              (secrets.token_hex(32),))
        _secret = db.q1("SELECT value FROM settings WHERE key = 'image_secret'")["value"].encode()
    return _secret


def sign(*parts: object) -> str:
    """Signatur über die Teile, z. B. sign("erinnern", 3, "v1|123|0")."""
    return hmac.new(key(), "/".join(str(p) for p in parts).encode(), hashlib.sha256).hexdigest()[:24]


def valid(signature: str | None, *parts: object) -> bool:
    return hmac.compare_digest(sign(*parts).encode(), (signature or "").encode())
