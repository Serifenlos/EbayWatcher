"""Geheime Einstellungen verschlüsselt speichern (z. B. die Cert ID von eBay).

Der Schlüssel liegt in einer eigenen Datei im Datenordner (geheim.key, nur für den Besitzer lesbar) und
entsteht beim ersten Gebrauch. Wer nur die Datenbank in die Hände bekommt, kann die Geheimnisse damit
nicht lesen. Die Datei gehört deshalb mit ins Backup: Ohne sie müssen die Zugangsdaten neu eingetragen
werden. Verfahren: Fernet (AES-128 im CBC-Modus mit HMAC-SHA256) aus dem Paket cryptography.
"""
from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from . import config

log = logging.getLogger("heuhaufen.geheim")

PREFIX = "enc1:"      # so beginnen verschlüsselte Werte in der Datenbank
_fernet: Fernet | None = None
_warned = False


def key_file():
    return config.DATA_DIR / "geheim.key"


def _key() -> Fernet:
    global _fernet
    if _fernet is None:
        path = key_file()
        if not path.exists():
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
                log.info("Schlüssel für geheime Einstellungen angelegt (%s).", path.name)
            except FileExistsError:
                pass
        _fernet = Fernet(path.read_bytes().strip())
    return _fernet


def encrypt(text: str) -> str:
    return PREFIX + _key().encrypt(text.encode()).decode() if text else ""


def decrypt(value: str | None) -> str:
    """Klartext zu einem gespeicherten Wert. Unverschlüsselte Werte kommen unverändert zurück."""
    if not value:
        return ""
    if not value.startswith(PREFIX):
        return value
    try:
        return _key().decrypt(value[len(PREFIX):].encode()).decode()
    except (InvalidToken, ValueError):
        global _warned
        if not _warned:   # nur einmal melden, nicht bei jedem Prüfzyklus
            _warned = True
            log.error("Ein geheimer Wert lässt sich nicht entschlüsseln: geheim.key fehlt oder passt nicht. "
                      "Zugangsdaten bitte neu eintragen.")
        return ""
