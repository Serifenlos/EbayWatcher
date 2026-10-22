"""SQLite-Speicher. Eine Datei im Datenordner, keine externen Dienste nötig."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
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
    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',   -- active | dropped | ended | gone
    end_checked     INTEGER NOT NULL DEFAULT 0,
    notified_end    INTEGER NOT NULL DEFAULT 0,
    notified_bin    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (search_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_items_end ON items(status, end_checked, end_time);

CREATE TABLE IF NOT EXISTS api_usage (
    day    TEXT PRIMARY KEY,
    calls  INTEGER NOT NULL DEFAULT 0
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


# ---------------------------------------------------------------- API-Zähler

def _today() -> str:
    return datetime.now(ZoneInfo(config.TZ)).date().isoformat()


def count_api_call(n: int = 1) -> None:
    ex(
        "INSERT INTO api_usage(day, calls) VALUES(?, ?) "
        "ON CONFLICT(day) DO UPDATE SET calls = calls + ?",
        (_today(), n, n),
    )


def api_calls_today() -> int:
    row = q1("SELECT calls FROM api_usage WHERE day = ?", (_today(),))
    return row["calls"] if row else 0


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
    ex("DELETE FROM api_usage WHERE day < date('now', '-60 day')")
