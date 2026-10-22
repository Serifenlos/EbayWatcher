"""Konfiguration aus Umgebungsvariablen (werden in Portainer gesetzt)."""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "ebay-watcher.db"

# eBay Production-Keyset: App ID (Client ID) und Cert ID (Client Secret)
EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "").strip()
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
EBAY_MARKETPLACE = os.environ.get("EBAY_MARKETPLACE", "EBAY_DE").strip()

# Erstes Benutzerkonto, wird nur beim allerersten Start angelegt
ADMIN_USER = os.environ.get("ADMIN_USER", "admin").strip() or "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# Notfall: setzt beim Start das Passwort auf ADMIN_PASSWORD zurück, schaltet 2FA ab und meldet alle Geräte ab
ADMIN_RESET = os.environ.get("ADMIN_RESET", "").strip().lower() in ("1", "true", "yes", "ja")

TZ = os.environ.get("TZ", "Europe/Berlin")

# "auto": Secure-Flag für Cookies setzen, sobald die Seite per HTTPS aufgerufen wird
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "auto").strip().lower()

APP_NAME = "Ebay Watcher"
