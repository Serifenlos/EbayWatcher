"""Verzeichnis der Plattformen. Eine neue Plattform hier eintragen (Reihenfolge = Anzeige).

Der Ordner plattformen/ hat keine __init__.py: CodeKit würde Dateien mit _ am Anfang nicht nach
build/ kopieren. Python findet das Paket trotzdem (wie app/ selbst).
"""
from __future__ import annotations

from .. import db, geheim
from .basis import Plattform
from .ebay import EbayPlattform

ALL: dict[str, Plattform] = {p.key: p for p in (EbayPlattform(),)}
DEFAULT = "ebay"


def get(key: str | None) -> Plattform:
    """Plattform zu einem Kürzel; Unbekanntes (alte Daten) gilt als eBay."""
    return ALL.get(key or DEFAULT) or ALL[DEFAULT]


def keys_of(value: str | None) -> list[str]:
    """„ebay,etsy“ → ["ebay", "etsy"], nur bekannte, Vorgabe eBay."""
    keys = [k.strip() for k in (value or "").split(",") if k.strip() in ALL]
    return keys or [DEFAULT]


def of_search(s: dict) -> list[Plattform]:
    return [ALL[k] for k in keys_of(s.get("platforms"))]


def of_item(row: dict) -> Plattform:
    return get(row.get("platform") if hasattr(row, "get") else row["platform"])


def image_hosts() -> tuple[str, ...]:
    return tuple(h for p in ALL.values() for h in p.image_hosts)


def any_configured(settings: dict) -> bool:
    return any(p.configured(settings) for p in ALL.values())


async def close() -> None:
    for p in ALL.values():
        await p.close()


# ---------------------------------------------------------------- Zugangsdaten in den Einstellungen

def save_credentials(p: Plattform, form: dict, user_id: int | None = None) -> None:
    """Felder aus dem Formular für ein Konto speichern. Geheime nur, wenn etwas eingetragen ist (leer = unverändert)."""
    values = {}
    for c in p.credentials:
        value = str(form.get(c.key, "")).strip()
        if c.secret:
            if value:
                values[c.key] = geheim.encrypt(value)
        else:
            values[c.key] = value
    db.set_settings(values, user_id)


def secret_hint(settings: dict, key: str) -> str | None:
    """Anzeige für ein gespeichertes Geheimnis: nur die letzten vier Zeichen."""
    value = geheim.decrypt(settings.get(key))
    if not value:
        return None
    return "…" + value[-4:] if len(value) > 8 else "gespeichert"


def adopt_from_environment(env: dict[str, str], log) -> None:
    """Einmalig: eBay-Zugangsdaten aus der Docker-Konfiguration (EBAY_CLIENT_ID, EBAY_CLIENT_SECRET)
    in die Einstellungen des Admins übernehmen. Danach gelten nur noch die Einstellungen."""
    client_id, secret = env.get("EBAY_CLIENT_ID", "").strip(), env.get("EBAY_CLIENT_SECRET", "").strip()
    if not (client_id and secret):
        return
    settings = db.get_settings()
    stored_id = (settings.get("ebay_client_id") or "").strip()
    stored_secret = geheim.decrypt(settings.get("ebay_client_secret"))
    if not stored_id or not stored_secret:   # noch nichts gespeichert oder geheim.key verloren
        db.set_settings({"ebay_client_id": client_id, "ebay_client_secret": geheim.encrypt(secret)})
        log.info("eBay-Zugangsdaten aus der Docker-Konfiguration übernommen. Sie stehen jetzt unter "
                 "Einstellungen → eBay-Verbindung; EBAY_CLIENT_ID und EBAY_CLIENT_SECRET können aus der "
                 "Compose-Datei entfernt werden.")
    elif stored_id != client_id or stored_secret != secret:
        log.warning("EBAY_CLIENT_ID/EBAY_CLIENT_SECRET in der Docker-Konfiguration werden nicht mehr verwendet. "
                    "Es gelten die Zugangsdaten unter Einstellungen → eBay-Verbindung.")
