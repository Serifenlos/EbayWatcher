"""Bild-Cache: Artikelbilder einmal bei eBay holen und danach selbst ausliefern.

* Seiten und Benachrichtigungen verweisen auf /bild/<Gesuch>/<Artikel>?v=…&s=… statt auf eBay.
* s ist eine Signatur. Gültig sind nur Adressen, die Ebay Watcher selbst erzeugt hat. Deshalb kommt die
  Adresse ohne Login aus (ntfy auf dem Handy kann das Bild laden), lässt sich aber nicht für fremde
  Artikel oder Seiten missbrauchen. v ändert sich, wenn der Verkäufer das Bild austauscht.
* Die eBay-Adresse des Bildes steht in der eigenen Datenbank. Geladen wird nur von eBay-Servern,
  immer in 500 px Breite.
* Die Dateien liegen im Datenordner unter cache/bilder. Die stündliche Pflege löscht Bilder, die
  14 Tage nicht angezeigt wurden, und hält den Ordner unter 200 MB.
* Klappt das Laden nicht, schickt Ebay Watcher den Browser direkt zu eBay (wie ohne Cache).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx

from . import config, db, signed, util

log = logging.getLogger("ebay-watcher.bilder")

CACHE_DIR = config.DATA_DIR / "cache" / "bilder"
KEEP_DAYS = 14                          # so lange nicht angezeigt, dann wird ein Bild gelöscht
MAX_CACHE_BYTES = 200 * 1024 * 1024     # Obergrenze für den ganzen Ordner …
TRIM_TO_BYTES = 180 * 1024 * 1024       # … beim Überschreiten bis hierhin aufräumen, die ältesten zuerst
MAX_IMAGE_BYTES = 5 * 1024 * 1024
RETRY_SECONDS = 3600                    # nach einem Fehlschlag frühestens nach einer Stunde neu versuchen
CLEANUP_SECONDS = 3600
BROWSER_CACHE_SECONDS = 7 * 86400
ALLOWED_HOSTS = (".ebayimg.com", ".ebaystatic.com")
MAX_PARALLEL = 6                        # gleichzeitige Downloads bei eBay

_http: httpx.AsyncClient | None = None
_locks: dict[str, asyncio.Lock] = {}
_failed: dict[str, float] = {}
_parallel = asyncio.Semaphore(MAX_PARALLEL)
_last_cleanup = 0.0


# ---------------------------------------------------------------- Adressen

def url(search_id: int | None, item_id: str | None, image: str | None) -> str | None:
    """Adresse eines Artikelbildes in Ebay Watcher, z. B. /bild/3/v1%7C1234%7C0?v=…&s=…"""
    if not image or search_id is None or not item_id:
        return None
    version = hashlib.sha256(image.encode()).hexdigest()[:8]
    return (f"/bild/{search_id}/{quote(str(item_id), safe='')}"
            f"?v={version}&s={signed.sign(search_id, item_id)}")


def valid(search_id: int, item_id: str, signature: str | None) -> bool:
    return signed.valid(signature, search_id, item_id)


def source(search_id: int, item_id: str) -> str | None:
    """eBay-Adresse des Bildes, aus dem gemerkten Angebot oder der Kopie bei „Ausgeblendet“."""
    for table in ("items", "hidden_items"):
        row = db.q1(f"SELECT image FROM {table} WHERE search_id = ? AND item_id = ?", (search_id, item_id))
        if row and row["image"]:
            return util.large_image(row["image"])
    return None


def allowed(address: str) -> bool:
    parts = urlsplit(address)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and host.endswith(ALLOWED_HOSTS)


# ---------------------------------------------------------------- Cache

def _path(address: str) -> Path:
    return CACHE_DIR / hashlib.sha256(address.encode()).hexdigest()[:32]


def _image_type(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def media_type(path: Path) -> str:
    with path.open("rb") as f:
        return _image_type(f.read(16)) or "application/octet-stream"


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=10.0, headers={"User-Agent": f"ebay-watcher/{config.APP_VERSION}"})
    return _http


async def close() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


async def _download(address: str) -> bytes | None:
    for _ in range(3):   # Weiterleitungen nur folgen, solange sie bei eBay bleiben
        async with _client().stream("GET", address) as r:
            if r.is_redirect:
                address = urljoin(address, r.headers.get("location", ""))
                if not allowed(address):
                    return None
                continue
            if r.status_code != 200:
                log.info("Bild bei eBay nicht abrufbar (Fehler %s)", r.status_code)
                return None
            data = bytearray()
            async for chunk in r.aiter_bytes():
                data += chunk
                if len(data) > MAX_IMAGE_BYTES:
                    return None
            return bytes(data)
    return None


def _store(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)   # erst die fertige Datei erscheint im Cache


def _touch(path: Path) -> None:
    """Merkt sich, dass das Bild angezeigt wurde (höchstens einmal am Tag, spart Schreibzugriffe)."""
    try:
        if time.time() - path.stat().st_mtime > 86400:
            os.utime(path)
    except OSError:
        pass


async def cached(address: str) -> Path | None:
    """Datei im Cache; beim ersten Mal wird das Bild bei eBay geladen. None, wenn das nicht klappt."""
    if not allowed(address):
        return None
    path = _path(address)
    if path.exists():
        _touch(path)
        return path
    if time.time() - _failed.get(path.name, 0) < RETRY_SECONDS:
        return None
    lock = _locks.setdefault(path.name, asyncio.Lock())
    try:
        async with lock:
            if path.exists():   # eine gleichzeitige Anfrage war schneller
                return path
            if time.time() - _failed.get(path.name, 0) < RETRY_SECONDS:
                return None
            try:
                async with _parallel:
                    data = await _download(address)
            except httpx.HTTPError as e:
                log.info("Bild bei eBay nicht abrufbar (%s)", e.__class__.__name__)
                data = None
            if not data or not _image_type(data[:16]):
                _failed[path.name] = time.time()
                return None
            _store(path, data)
            _failed.pop(path.name, None)
            return path
    finally:
        _locks.pop(path.name, None)


# ---------------------------------------------------------------- Aufräumen

def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _files() -> list[tuple[Path, os.stat_result]]:
    found = []
    try:
        for path in CACHE_DIR.iterdir():
            try:
                found.append((path, path.stat()))
            except OSError:
                pass
    except FileNotFoundError:
        pass
    return found


def cleanup(now: float | None = None) -> None:
    now = now or time.time()
    keep = []
    for path, st in _files():
        if path.name.endswith(".tmp"):
            if now - st.st_mtime > 3600:   # Rest eines abgebrochenen Downloads
                _remove(path)
        elif now - st.st_mtime > KEEP_DAYS * 86400:
            _remove(path)
        else:
            keep.append((st.st_mtime, st.st_size, path))
    total = sum(entry[1] for entry in keep)
    if total > MAX_CACHE_BYTES:
        for _mtime, size, path in sorted(keep):
            if total <= TRIM_TO_BYTES:
                break
            _remove(path)
            total -= size
    for name, failed_at in list(_failed.items()):
        if now - failed_at > RETRY_SECONDS:
            del _failed[name]


def cleanup_if_due() -> None:
    global _last_cleanup
    if time.time() - _last_cleanup >= CLEANUP_SECONDS:
        _last_cleanup = time.time()
        cleanup()


def stats() -> dict:
    files = [st for path, st in _files() if not path.name.endswith(".tmp")]
    return {"count": len(files), "megabytes": sum(st.st_size for st in files) / (1024 * 1024)}
