"""SQLite-Speicher. Eine Datei im Datenordner, keine externen Dienste nötig."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger("heuhaufen.db")

SCHEMA = """
-- Konten. role: admin (wer installiert hat, ADMIN_USER) oder user (eingeladen). active = 0: gesperrt.
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash   TEXT NOT NULL,
    totp_secret     TEXT,
    totp_enabled    INTEGER NOT NULL DEFAULT 0,
    totp_last_code  TEXT,
    email           TEXT,
    role            TEXT NOT NULL DEFAULT 'user',
    active          INTEGER NOT NULL DEFAULT 1,
    last_login      REAL,
    created_at      REAL NOT NULL
);

-- Einladungen und Links zum Zurücksetzen des Passworts. Gespeichert wird nur der Hash des Links.
-- invite: email = an wen, user_id = wer sie angenommen hat; reset: user_id = für wen
CREATE TABLE IF NOT EXISTS user_tokens (
    id          INTEGER PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    email       TEXT,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_by  INTEGER,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    used_at     REAL
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

-- Einstellungen der ganzen Installation (GLOBAL_KEYS) …
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- … und je Konto (alles andere: Standort, ntfy, Anzeige, Durchschnittspreis, Zugangsdaten der Plattformen)
CREATE TABLE IF NOT EXISTS user_settings (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key      TEXT NOT NULL,
    value    TEXT,
    PRIMARY KEY (user_id, key)
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
    platforms           TEXT NOT NULL DEFAULT 'ebay',      -- Plattformen, durch Komma getrennt (plattformen/)
    user_id             INTEGER REFERENCES users(id) ON DELETE CASCADE,   -- wem das Gesuch gehört
    active              INTEGER NOT NULL DEFAULT 1,
    initialized         INTEGER NOT NULL DEFAULT 0,
    last_run            REAL,
    last_error          TEXT,
    created_at          REAL NOT NULL
);

-- Gemerkte Angebote. Bewusst ohne Verkäufer- oder Käuferdaten.
CREATE TABLE IF NOT EXISTS items (
    search_id       INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    item_id         TEXT NOT NULL,                         -- eBay: v1|…|0, andere Plattformen: kürzel:nummer
    platform        TEXT NOT NULL DEFAULT 'ebay',
    title           TEXT,
    url             TEXT,
    image           TEXT,
    images          TEXT,                  -- alle Bildadressen bei der Plattform als JSON-Liste (Galerie)
    condition_text  TEXT,                  -- Zustand im Wortlaut der Plattform, z. B. „Gebraucht“
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
    epid            TEXT,                              -- Produkt im Katalog der Plattform (eBay: ePID)
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
    platform    TEXT NOT NULL DEFAULT 'ebay',
    title       TEXT,
    url         TEXT,
    image       TEXT,
    hidden_at   REAL NOT NULL,
    PRIMARY KEY (search_id, item_id)
);

-- Vergleichsangebote für den Durchschnittspreis (siehe market.py). Sie bleiben gespeichert, auch wenn
-- sie nicht mehr online sind, und bilden so über Monate einen Preisverlauf.
-- key: search:<Gesuch> | epid:<Katalognummer> | nr:<Artikelnummer>
-- Die Vergleichsangebote sind für alle Konten gemeinsam; user_id: wer zuletzt abgefragt hat (Tageslimit je Konto).
CREATE TABLE IF NOT EXISTS market_sources (
    key         TEXT PRIMARY KEY,
    fetched_at  REAL NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 1,
    last_used   REAL NOT NULL,
    user_id     INTEGER
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
    end_time    REAL,                    -- Ende laut Plattform, falls bekannt
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    gone_at     REAL,                    -- seitdem nicht mehr online
    sold        INTEGER NOT NULL DEFAULT 0,   -- vor dem Ende verschwunden, also vermutlich verkauft
    PRIMARY KEY (key, item_id)
);

-- Angaben, die nur getItem liefert (für die Seitenleiste der Galerie, siehe details.py).
-- Je Angebot einmal, auch wenn es in mehreren Gesuchen steht.
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

"""

# Abfragen je Tag, Konto und Plattform, dazu dieselben nach Zweck (plattformen/basis.PURPOSES). Jedes Konto
# hat eigene Zugangsdaten und damit ein eigenes Tageskontingent. user_id 0: ohne Konto (alte Zählung, Tests).
# Eigene Konstanten, weil _migrate die Tabellen aus älteren Versionen neu anlegt.
API_USAGE = """api_usage (
    day       TEXT NOT NULL,
    user_id   INTEGER NOT NULL DEFAULT 0,
    platform  TEXT NOT NULL DEFAULT 'ebay',
    calls     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_id, platform)
)"""
API_CALLS = """api_calls (
    day       TEXT NOT NULL,
    user_id   INTEGER NOT NULL DEFAULT 0,
    platform  TEXT NOT NULL DEFAULT 'ebay',
    purpose   TEXT NOT NULL,
    calls     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_id, platform, purpose)
)"""
SCHEMA += f"CREATE TABLE IF NOT EXISTS {API_USAGE};\nCREATE TABLE IF NOT EXISTS {API_CALLS};\n"

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


def _adopt_legacy_database() -> None:
    """Datenbank unter einem früheren Namen (z. B. ebay-watcher.db) unter dem neuen Namen übernehmen,
    mit allen Gesuchen, Einstellungen, Durchschnittspreisen und Anmeldungen. Vorher schreibt SQLite
    offene Änderungen aus der WAL-Datei in die Datenbank, damit nichts verloren geht."""
    if config.DB_PATH.exists():
        return
    for name in config.LEGACY_DB_NAMES:
        old = config.DATA_DIR / name
        if not old.exists():
            continue
        con = sqlite3.connect(old)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()
        old.rename(config.DB_PATH)
        for suffix in ("-wal", "-shm"):   # nach dem Checkpoint meist schon weg
            leftover = config.DATA_DIR / (name + suffix)
            if leftover.exists():
                leftover.rename(config.DATA_DIR / (config.DB_PATH.name + suffix))
        log.info("Datenbank %s übernommen als %s.", name, config.DB_PATH.name)
        return


def init() -> None:
    global _conn
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _adopt_legacy_database()
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.executescript(SCHEMA)
    _migrate()


def _columns(table: str) -> set[str]:
    return {r["name"] for r in q(f"PRAGMA table_info({table})")}


def _migrate() -> None:
    """Spalten, die nach der ersten Version dazugekommen sind, in bestehenden Datenbanken ergänzen."""
    # Mehrere Konten (seit Heuhaufen 0.4): das erste Konto ist der Admin, ihm gehört alles Bisherige
    columns = _columns("users")
    for name, kind in (("email", "TEXT"), ("role", "TEXT NOT NULL DEFAULT 'user'"),
                       ("active", "INTEGER NOT NULL DEFAULT 1"), ("last_login", "REAL")):
        if name not in columns:
            ex(f"ALTER TABLE users ADD COLUMN {name} {kind}")
    if not q1("SELECT 1 AS x FROM users WHERE role = 'admin'"):
        ex("UPDATE users SET role = 'admin' WHERE id = (SELECT MIN(id) FROM users)")
    ex("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email COLLATE NOCASE) WHERE email IS NOT NULL")
    if "user_id" not in _columns("searches"):
        ex("ALTER TABLE searches ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
    if "user_id" not in _columns("market_sources"):
        ex("ALTER TABLE market_sources ADD COLUMN user_id INTEGER")
    adopt_owner_data()
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
    # Plattformen (seit Heuhaufen 0.3): Gesuche, Angebote und ausgeblendete Angebote gehören bisher zu eBay
    for table, column in (("searches", "platforms"), ("items", "platform"), ("hidden_items", "platform")):
        if column not in {r["name"] for r in q(f"PRAGMA table_info({table})")}:
            ex(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT 'ebay'")
    # Abfragen je Konto und Plattform: Tabellen mit neuem Schlüssel anlegen. Bisherige Zahlen gelten für eBay
    # (Tabellen vor 0.3 hatten keine Plattform) und für den Admin.
    if "user_id" not in _columns("api_usage"):
        platform = "platform" if "platform" in _columns("api_usage") else "'ebay'"
        owner = owner_id() or 0
        with _lock:
            _conn.executescript(f"""
                BEGIN;
                ALTER TABLE api_usage RENAME TO api_usage_alt;
                ALTER TABLE api_calls RENAME TO api_calls_alt;
                CREATE TABLE {API_USAGE};
                CREATE TABLE {API_CALLS};
                INSERT INTO api_usage(day, user_id, platform, calls)
                    SELECT day, {owner}, {platform}, calls FROM api_usage_alt;
                INSERT INTO api_calls(day, user_id, platform, purpose, calls)
                    SELECT day, {owner}, {platform}, purpose, calls FROM api_calls_alt;
                DROP TABLE api_usage_alt;
                DROP TABLE api_calls_alt;
                COMMIT;
            """)
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

# Gelten für die ganze Installation; alles andere gehört zum Konto (user_settings)
GLOBAL_KEYS = frozenset({"app_url", "image_secret"})


def owner_id() -> int | None:
    """Das Konto des Admins (wer Heuhaufen installiert hat)."""
    try:
        row = q1("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
    except sqlite3.OperationalError:   # Datenbank vor 0.4, noch ohne Spalte role
        row = q1("SELECT MIN(id) AS id FROM users")
    return row["id"] if row and row["id"] is not None else None


def adopt_owner_data() -> None:
    """Was noch keinem Konto gehört, dem Admin geben: Gesuche ohne Konto und Einstellungen, die bis 0.3
    für die ganze Installation galten (Standort, ntfy, eBay-Zugangsdaten …). Ohne Konto: nichts zu tun."""
    owner = owner_id()
    if owner is None:
        return
    ex("UPDATE searches SET user_id = ? WHERE user_id IS NULL", (owner,))
    marks = ",".join("?" * len(GLOBAL_KEYS))
    rows = q(f"SELECT key, value FROM settings WHERE key NOT IN ({marks})", tuple(GLOBAL_KEYS))
    for row in rows:
        ex("INSERT INTO user_settings(user_id, key, value) VALUES (?, ?, ?) ON CONFLICT(user_id, key) DO NOTHING",
           (owner, row["key"], row["value"]))
    if rows:
        ex(f"DELETE FROM settings WHERE key NOT IN ({marks})", tuple(GLOBAL_KEYS))


def get_settings(user_id: int | None = None) -> dict:
    """Einstellungen eines Kontos: Vorgaben, darüber die der Installation, darüber die des Kontos.
    Ohne user_id: das Konto des Admins (so verhält sich Heuhaufen wie mit nur einem Konto).
    _user_id steht mit drin, damit Plattformen die Abfragen dem Konto zuordnen können."""
    s: dict = dict(DEFAULTS)
    for row in q("SELECT key, value FROM settings"):
        s[row["key"]] = row["value"] if row["value"] is not None else ""
    uid = user_id if user_id is not None else owner_id()
    if uid is not None:
        for row in q("SELECT key, value FROM user_settings WHERE user_id = ?", (uid,)):
            s[row["key"]] = row["value"] if row["value"] is not None else ""
    s["_user_id"] = uid
    return s


def set_settings(values: dict[str, object], user_id: int | None = None) -> None:
    """Speichert Einstellungen. GLOBAL_KEYS für die Installation, alles andere für das Konto
    (ohne user_id: Konto des Admins; gibt es noch keins, für die Installation)."""
    uid = user_id if user_id is not None else owner_id()
    for key, value in values.items():
        value = "" if value is None else str(value)
        if key in GLOBAL_KEYS or uid is None:
            ex("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (key, value))
        else:
            ex("INSERT INTO user_settings(user_id, key, value) VALUES(?, ?, ?) "
               "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value", (uid, key, value))


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


def count_api_call(purpose: str = "suche", n: int = 1, platform: str = "ebay", user_id: int | None = None) -> None:
    day, uid = _today(), user_id or 0
    with _lock:
        ex("INSERT INTO api_usage(day, user_id, platform, calls) VALUES(?, ?, ?, ?) "
           "ON CONFLICT(day, user_id, platform) DO UPDATE SET calls = calls + ?", (day, uid, platform, n, n))
        ex("INSERT INTO api_calls(day, user_id, platform, purpose, calls) VALUES(?, ?, ?, ?, ?) "
           "ON CONFLICT(day, user_id, platform, purpose) DO UPDATE SET calls = calls + ?",
           (day, uid, platform, purpose, n, n))


def api_calls_today(platform: str = "ebay", user_id: int | None = None) -> int:
    row = q1("SELECT calls FROM api_usage WHERE day = ? AND platform = ? AND user_id = ?",
             (_today(), platform, user_id or 0))
    return row["calls"] if row else 0


def api_calls_by_purpose(days_back: int = 0, platform: str = "ebay", user_id: int | None = None) -> dict[str, int]:
    """Abfragen eines Tages nach Zweck; „ohne Zuordnung“ sind Abfragen von vor dem Update."""
    day, uid = _today(days_back), user_id or 0
    result = {r["purpose"]: r["calls"] for r in q(
        "SELECT purpose, calls FROM api_calls WHERE day = ? AND platform = ? AND user_id = ?", (day, platform, uid))}
    row = q1("SELECT calls FROM api_usage WHERE day = ? AND platform = ? AND user_id = ?", (day, platform, uid))
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
    ex("DELETE FROM user_tokens WHERE expires_at < ?", (now - 30 * 86400,))
    ex("DELETE FROM api_usage WHERE day < date('now', '-60 day')")
    ex("DELETE FROM api_calls WHERE day < date('now', '-60 day')")
    ex("DELETE FROM item_details WHERE item_id NOT IN (SELECT item_id FROM items) "
       "AND item_id NOT IN (SELECT item_id FROM hidden_items)")
