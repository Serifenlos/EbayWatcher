"""SQLite-Speicher. Eine Datei im Datenordner, keine externen Dienste nötig."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash   TEXT NOT NULL,
    totp_secret     TEXT,
    totp_enabled    INTEGER NOT NULL DEFAULT 0,
    totp_last_code  TEXT,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf         TEXT NOT NULL,
    remember     INTEGER NOT NULL DEFAULT 0,
    pending_2fa  INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    expires_at   REAL NOT NULL,
    user_agent   TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS searches (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL DEFAULT '',
    query               TEXT NOT NULL,
    exclude             TEXT NOT NULL DEFAULT '',
    format              TEXT NOT NULL DEFAULT 'auction',   -- auction | fixed | both
    location            TEXT NOT NULL DEFAULT 'DE',        -- DE | EU | WORLD
    max_price           REAL NOT NULL,
    incl_shipping       INTEGER NOT NULL DEFAULT 1,
    free_shipping_only  INTEGER NOT NULL DEFAULT 0,
    condition           TEXT NOT NULL DEFAULT 'any',       -- any | new | used
    lead_minutes        INTEGER NOT NULL DEFAULT 10,
    interval_minutes    INTEGER,                           -- eigenes Suchintervall, leer: Einstellung
    pickup_mode         TEXT NOT NULL DEFAULT 'radius',    -- radius | all | hide
    active              INTEGER NOT NULL DEFAULT 1,
    initialized         INTEGER NOT NULL DEFAULT 0,
    last_run            REAL,
    last_error          TEXT,
    created_at          REAL NOT NULL
);

-- Gemerkte Angebote. Bewusst ohne Verkäufer- oder Käuferdaten.
CREATE TABLE IF NOT EXISTS items (
    search_id       INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    item_id         TEXT NOT NULL,
    title           TEXT,
    url             TEXT,
    image           TEXT,
    images          TEXT,                  -- alle Bildadressen bei eBay als JSON-Liste (für die Galerie)
    condition_text  TEXT,                  -- Zustand im Wortlaut von eBay, z. B. „Gebraucht“
    is_auction      INTEGER NOT NULL DEFAULT 0,
    has_bin         INTEGER NOT NULL DEFAULT 0,
    price           REAL,
    bin_price       REAL,
    currency        TEXT,
    shipping        REAL,
    shipping_known  INTEGER NOT NULL DEFAULT 0,
    bid_count       INTEGER,
    end_time        REAL,
    country         TEXT,
    location_text   TEXT,
    pickup          INTEGER NOT NULL DEFAULT 0,
    pickup_only     INTEGER NOT NULL DEFAULT 0,
    distance_km     REAL,
    near            INTEGER NOT NULL DEFAULT 0,       -- abholbar im eingestellten Umkreis
    remind          INTEGER NOT NULL DEFAULT 0,       -- Knopf „1 Min vorher erinnern“ gedrückt
    reminded        INTEGER NOT NULL DEFAULT 0,       -- Erinnerung ist erledigt
    epid            TEXT,                              -- Produkt im eBay-Katalog, falls verknüpft
    cond            TEXT,                              -- new | used | parts (für den Durchschnittspreis)
    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',   -- active | dropped | ended | gone
    end_checked     INTEGER NOT NULL DEFAULT 0,
    notified_end    INTEGER NOT NULL DEFAULT 0,
    notified_bin    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (search_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_items_end ON items(status, end_checked, end_time);

-- Von Hand ausgeblendete Angebote, je Gesuch. Titel, Link und Bild als Kopie,
-- damit die Liste „Ausgeblendet“ auch nach dem Ende des Angebots noch etwas zeigt.
CREATE TABLE IF NOT EXISTS hidden_items (
    search_id   INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    item_id     TEXT NOT NULL,
    title       TEXT,
    url         TEXT,
    image       TEXT,
    hidden_at   REAL NOT NULL,
    PRIMARY KEY (search_id, item_id)
);

-- Vergleichsangebote für den Durchschnittspreis (siehe market.py). Sie bleiben gespeichert, auch wenn
-- sie nicht mehr online sind, und bilden so über Monate einen Preisverlauf.
-- key: search:<Gesuch> | epid:<Katalognummer> | nr:<Artikelnummer>
CREATE TABLE IF NOT EXISTS market_sources (
    key         TEXT PRIMARY KEY,
    fetched_at  REAL NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 1,
    last_used   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS market_items (
    key       TEXT NOT NULL REFERENCES market_sources(key) ON DELETE CASCADE,
    item_id   TEXT NOT NULL,
    title     TEXT,
    epid      TEXT,
    number    TEXT,
    cond      TEXT,
    price       REAL NOT NULL,           -- zuletzt gesehener Preis
    shipping    REAL,
    end_time    REAL,                    -- Ende laut eBay, falls bekannt
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    gone_at     REAL,                    -- seitdem nicht mehr online
    sold        INTEGER NOT NULL DEFAULT 0,   -- vor dem Ende verschwunden, also vermutlich verkauft
    PRIMARY KEY (key, item_id)
);

-- Angaben, die nur getItem liefert (für die Seitenleiste der Galerie, siehe details.py).
-- Je eBay-Angebot einmal, auch wenn es in mehreren Gesuchen steht.
CREATE TABLE IF NOT EXISTS item_details (
    item_id         TEXT PRIMARY KEY,
    description     TEXT,      -- Beschreibung des Verkäufers als bereinigter Text (JSON-Liste von Absätzen)
    truncated       INTEGER NOT NULL DEFAULT 0,
    condition_note  TEXT,      -- Zustandsbeschreibung des Verkäufers
    aspects         TEXT,      -- Artikelmerkmale als JSON-Liste [[Name, Wert], …]
    bidders         INTEGER,   -- verschiedene Bieter
    min_bid         REAL,      -- nächstes Gebot ab
    reserve_met     INTEGER,   -- Mindestpreis erreicht (nur bei Auktionen mit Mindestpreis)
    fetched_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
    day    TEXT PRIMARY KEY,
    calls  INTEGER NOT NULL DEFAULT 0
);

-- dieselben Abfragen nach Zweck (ebay.PURPOSES)
CREATE TABLE IF NOT EXISTS api_calls (
    day      TEXT NOT NULL,
    purpose  TEXT NOT NULL,
    calls    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, purpose)
);
"""

DEFAULTS: dict[str, str] = {
    "plz": "",
    "radius_km": "50",
    "lead_minutes": "10",
    "interval_minutes": "15",
    "ntfy_url": "",
    "ntfy_topic": "",
    "ntfy_token": "",
    "app_url": "",
    # Anzeige: groß der Preis mit Versand, darunter Artikelpreis + Versand
    "price_with_shipping": "1",
    # Durchschnittspreis (market.py)
    "market_enabled": "1",
    "market_threshold": "15",
    "market_split": "1",
    "market_lookups": "50",
    "market_months": "6",
}

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()


def init() -> None:
    global _conn
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.executescript(SCHEMA)
    _migrate()


def _migrate() -> None:
    """Spalten, die nach der ersten Version dazugekommen sind, in bestehenden Datenbanken ergänzen."""
    if "interval_minutes" not in {r["name"] for r in q("PRAGMA table_info(searches)")}:
        ex("ALTER TABLE searches ADD COLUMN interval_minutes INTEGER")
    columns = {r["name"] for r in q("PRAGMA table_info(items)")}
    for name in ("epid", "cond", "images", "condition_text"):
        if name not in columns:
            ex(f"ALTER TABLE items ADD COLUMN {name} TEXT")
    for name in ("remind", "reminded"):
        if name not in columns:
            ex(f"ALTER TABLE items ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
    if "near" not in columns:
        ex("ALTER TABLE items ADD COLUMN near INTEGER NOT NULL DEFAULT 0")
        radius = setting_int(get_settings(), "radius_km", 50)
        ex("UPDATE items SET near = 1 WHERE pickup = 1 AND (distance_km IS NULL OR distance_km <= ?)", (radius,))
    # Durchschnittspreis: aus der Momentaufnahme wird ein Preisverlauf
    columns = {r["name"] for r in q("PRAGMA table_info(market_items)")}
    for name, kind in (("end_time", "REAL"), ("first_seen", "REAL"), ("last_seen", "REAL"),
                       ("gone_at", "REAL"), ("sold", "INTEGER NOT NULL DEFAULT 0")):
        if name not in columns:
            ex(f"ALTER TABLE market_items ADD COLUMN {name} {kind}")
    if "last_seen" not in columns:
        ex("UPDATE market_items SET first_seen = "
           "(SELECT fetched_at FROM market_sources s WHERE s.key = market_items.key)")
        ex("UPDATE market_items SET last_seen = first_seen")


def q(sql: str, params: tuple | list = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def q1(sql: str, params: tuple | list = ()) -> dict | None:
    with _lock:
        row = _conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def ex(sql: str, params: tuple | list = ()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        return cur.lastrowid


# ---------------------------------------------------------------- Einstellungen

def get_settings() -> dict[str, str]:
    s = dict(DEFAULTS)
    for row in q("SELECT key, value FROM settings"):
        s[row["key"]] = row["value"] if row["value"] is not None else ""
    return s


def set_settings(values: dict[str, object]) -> None:
    for key, value in values.items():
        ex(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, "" if value is None else str(value)),
        )


def setting_int(settings: dict[str, str], key: str, fallback: int) -> int:
    try:
        return int(float(settings.get(key) or fallback))
    except ValueError:
        return fallback


# ---------------------------------------------------------------- Ausgeblendete Angebote

def hidden_ids(search_id: int) -> set[str]:
    return {r["item_id"] for r in q("SELECT item_id FROM hidden_items WHERE search_id = ?", (search_id,))}


def hidden_ids_by_search() -> dict[int, set[str]]:
    result: dict[int, set[str]] = {}
    for r in q("SELECT search_id, item_id FROM hidden_items"):
        result.setdefault(r["search_id"], set()).add(r["item_id"])
    return result


# ---------------------------------------------------------------- API-Zähler

def _today(days_back: int = 0) -> str:
    return (datetime.now(ZoneInfo(config.TZ)).date() - timedelta(days=days_back)).isoformat()


def count_api_call(purpose: str = "suche", n: int = 1) -> None:
    day = _today()
    with _lock:
        ex("INSERT INTO api_usage(day, calls) VALUES(?, ?) ON CONFLICT(day) DO UPDATE SET calls = calls + ?",
           (day, n, n))
        ex("INSERT INTO api_calls(day, purpose, calls) VALUES(?, ?, ?) "
           "ON CONFLICT(day, purpose) DO UPDATE SET calls = calls + ?", (day, purpose, n, n))


def api_calls_today() -> int:
    row = q1("SELECT calls FROM api_usage WHERE day = ?", (_today(),))
    return row["calls"] if row else 0


def api_calls_by_purpose(days_back: int = 0) -> dict[str, int]:
    """Abfragen eines Tages nach Zweck; „ohne Zuordnung“ sind Abfragen von vor dem Update."""
    day = _today(days_back)
    result = {r["purpose"]: r["calls"] for r in q("SELECT purpose, calls FROM api_calls WHERE day = ?", (day,))}
    row = q1("SELECT calls FROM api_usage WHERE day = ?", (day,))
    rest = (row["calls"] if row else 0) - sum(result.values())
    if rest > 0:
        result["ohne"] = rest
    return result


# ---------------------------------------------------------------- Aufräumen

def prune() -> None:
    now = time.time()
    ex("DELETE FROM sessions WHERE expires_at < ?", (now,))
    ex(
        "UPDATE items SET status = 'ended' "
        "WHERE status = 'active' AND end_time IS NOT NULL AND end_time < ?",
        (now,),
    )
    ex("DELETE FROM items WHERE status != 'active' AND last_seen < ?", (now - 7 * 86400,))
    # Ausgeblendete Angebote vergessen, wenn das Angebot selbst schon gelöscht ist
    ex(
        "DELETE FROM hidden_items WHERE hidden_at < ? AND NOT EXISTS ("
        "SELECT 1 FROM items i WHERE i.search_id = hidden_items.search_id AND i.item_id = hidden_items.item_id)",
        (now - 30 * 86400,),
    )
    ex("DELETE FROM api_usage WHERE day < date('now', '-60 day')")
    ex("DELETE FROM api_calls WHERE day < date('now', '-60 day')")
    ex("DELETE FROM item_details WHERE item_id NOT IN (SELECT item_id FROM items) "
       "AND item_id NOT IN (SELECT item_id FROM hidden_items)")
