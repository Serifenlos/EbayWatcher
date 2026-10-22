"""Anmeldung: Argon2-Passwörter, serverseitige Sitzungen, Sperre nach Fehlversuchen, TOTP-2FA."""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import secrets
import time

import pyotp
import qrcode
import qrcode.image.svg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from . import config, db

log = logging.getLogger("ebay-watcher.auth")

SESSION_COOKIE = "ew_session"
PENDING_COOKIE = "ew_pending"
LOGIN_CSRF_COOKIE = "ew_login"

REMEMBER_SECONDS = 90 * 24 * 3600   # "Angemeldet bleiben": 3 Monate
SESSION_SECONDS = 12 * 3600         # normale Sitzung: höchstens 12 Stunden ...
SESSION_IDLE = 60 * 60              # ... und Abmeldung nach 60 Minuten ohne Aktivität
PENDING_SECONDS = 5 * 60            # Zeit für die Eingabe des 2FA-Codes
MIN_PASSWORD = 10

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- Passwörter

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(user: dict | None, password: str) -> bool:
    """Prüft immer einen Hash, auch bei unbekanntem Benutzer (gleiches Zeitverhalten)."""
    stored = user["password_hash"] if user else _DUMMY_HASH
    try:
        ok = _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        ok = False
    if ok and user and _hasher.check_needs_rehash(stored):
        db.ex("UPDATE users SET password_hash = ? WHERE id = ?", (_hasher.hash(password), user["id"]))
    return ok and user is not None


def ensure_admin() -> None:
    existing = db.q1("SELECT id, username FROM users ORDER BY id LIMIT 1")
    if existing:
        if config.ADMIN_RESET:
            if len(config.ADMIN_PASSWORD) < MIN_PASSWORD:
                log.error("ADMIN_RESET ist gesetzt, aber ADMIN_PASSWORD fehlt oder hat weniger als %d Zeichen.",
                          MIN_PASSWORD)
                return
            db.ex("UPDATE users SET password_hash = ?, totp_enabled = 0, totp_secret = NULL WHERE id = ?",
                  (hash_password(config.ADMIN_PASSWORD), existing["id"]))
            db.ex("DELETE FROM sessions WHERE user_id = ?", (existing["id"],))
            log.warning("Passwort für '%s' zurückgesetzt, 2FA ausgeschaltet. Entferne ADMIN_RESET jetzt wieder.",
                        existing["username"])
        return
    password = config.ADMIN_PASSWORD
    if not password:
        password = secrets.token_urlsafe(12)
        log.warning("Kein ADMIN_PASSWORD gesetzt. Erstes Passwort für '%s': %s  (bitte gleich ändern)",
                    config.ADMIN_USER, password)
    db.ex("INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
          (config.ADMIN_USER, hash_password(password), time.time()))
    log.info("Benutzerkonto '%s' angelegt.", config.ADMIN_USER)


# ---------------------------------------------------------------- Sperre nach Fehlversuchen

class Throttle:
    WINDOW = 15 * 60
    MAX_PER_IP = 5
    MAX_PER_USER = 20

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        now = time.time()
        recent = [t for t in self._fails.get(key, []) if now - t < self.WINDOW]
        self._fails[key] = recent
        return recent

    def wait_seconds(self, ip: str, user: str) -> int:
        for key, limit in ((f"ip:{ip}", self.MAX_PER_IP), (f"user:{user.lower()}", self.MAX_PER_USER)):
            recent = self._recent(key)
            if len(recent) >= limit:
                return int(self.WINDOW - (time.time() - recent[0])) + 1
        return 0

    def fail(self, ip: str, user: str) -> None:
        now = time.time()
        for key in (f"ip:{ip}", f"user:{user.lower()}"):
            self._fails.setdefault(key, []).append(now)

    def reset(self, ip: str) -> None:
        self._fails.pop(f"ip:{ip}", None)


throttle = Throttle()


# ---------------------------------------------------------------- Sitzungen

def create_session(user_id: int, remember: bool, user_agent: str, pending: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    lifetime = PENDING_SECONDS if pending else (REMEMBER_SECONDS if remember else SESSION_SECONDS)
    db.ex(
        "INSERT INTO sessions(token_hash, user_id, csrf, remember, pending_2fa, created_at, last_seen, "
        "expires_at, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_digest(token), user_id, secrets.token_urlsafe(24), int(remember), int(pending),
         now, now, now + lifetime, (user_agent or "")[:300]),
    )
    return token


def load_session(token: str | None, pending: bool = False) -> dict | None:
    if not token:
        return None
    row = db.q1("SELECT * FROM sessions WHERE token_hash = ? AND pending_2fa = ?", (_digest(token), int(pending)))
    if not row:
        return None
    now = time.time()
    idle_expired = not row["remember"] and not pending and now - row["last_seen"] > SESSION_IDLE
    if row["expires_at"] < now or idle_expired:
        db.ex("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
        return None
    if now - row["last_seen"] > 60:
        db.ex("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, row["token_hash"]))
    return row


def destroy_session(token: str | None) -> None:
    if token:
        db.ex("DELETE FROM sessions WHERE token_hash = ?", (_digest(token),))


def destroy_other_sessions(user_id: int, keep_hash: str | None) -> None:
    db.ex("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, keep_hash or ""))


def csrf_ok(sent: object, expected: str | None) -> bool:
    return bool(expected) and isinstance(sent, str) and secrets.compare_digest(sent, expected)


# ---------------------------------------------------------------- Zwei-Faktor (TOTP)

def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_qr_data_uri(secret: str, username: str) -> str:
    uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Ebay Watcher")
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def verify_totp(user: dict, code: str, secret: str | None = None) -> bool:
    secret = secret or user.get("totp_secret")
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if not secret or len(code) != 6:
        return False
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        return False
    stamp = f"{code}:{int(time.time() // 30)}"
    last = user.get("totp_last_code") or ""
    if last.split(":")[0] == code and abs(int(last.split(":")[1]) - int(time.time() // 30)) <= 2:
        return False  # denselben Code nicht zweimal akzeptieren
    db.ex("UPDATE users SET totp_last_code = ? WHERE id = ?", (stamp, user["id"]))
    return True
