"""Bild-Cache: Artikelbilder einmal bei eBay holen und danach selbst ausliefern.

* Seiten und Benachrichtigungen verweisen auf /bild/<Gesuch>/<Artikel>?v=…&s=… statt auf eBay.
  Die Galerie (Klick aufs Vorschaubild) nutzt dieselbe Adresse mit &n=<Nummer des Bildes>&g=1 für groß.
* s ist eine Signatur. Gültig sind nur Adressen, die Ebay Watcher selbst erzeugt hat. Deshalb kommt die
  Adresse ohne Login aus (ntfy auf dem Handy kann das Bild laden), lässt sich aber nicht für fremde
  Artikel oder Seiten missbrauchen. v ändert sich, wenn der Verkäufer das Bild austauscht.
* Die eBay-Adressen der Bilder stehen in der eigenen Datenbank. Geladen wird nur von eBay-Servern,
  für Listen und Benachrichtigungen in 500 px, für die Galerie in 1600 px.
* Die Dateien liegen im Datenordner unter cache/bilder. Die stündliche Pflege löscht Bilder, die
  14 Tage nicht angezeigt wurden, und hält den Ordner unter 500 MB.
* Klappt das Laden nicht, schickt Ebay Watcher den Browser direkt zu eBay (wie ohne Cache).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import json
import os
import secrets
import struct
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx

from . import config, db, signed, util

log = logging.getLogger("ebay-watcher.bilder")

CACHE_DIR = config.DATA_DIR / "cache" / "bilder"
KEEP_DAYS = 14                          # so lange nicht angezeigt, dann wird ein Bild gelöscht
MAX_CACHE_BYTES = 500 * 1024 * 1024     # Obergrenze für den ganzen Ordner …
TRIM_TO_BYTES = 450 * 1024 * 1024       # … beim Überschreiten bis hierhin aufräumen, die ältesten zuerst
MAX_IMAGE_BYTES = 5 * 1024 * 1024
RETRY_SECONDS = 3600                    # nach einem Fehlschlag frühestens nach einer Stunde neu versuchen
CLEANUP_SECONDS = 3600
BROWSER_CACHE_SECONDS = 7 * 86400
ALLOWED_HOSTS = (".ebayimg.com", ".ebaystatic.com")
MAX_PARALLEL = 6                        # gleichzeitige Downloads bei eBay
THUMB_SIZE = 500                        # Listen, Karten, Benachrichtigungen
GALLERY_SIZE = 1600                     # Galerie: so groß, wie eBay die Bilder höchstens herausgibt

_http: httpx.AsyncClient | None = None
_locks: dict[str, asyncio.Lock] = {}
_failed: dict[str, float] = {}
_parallel = asyncio.Semaphore(MAX_PARALLEL)
_last_cleanup = 0.0


# ---------------------------------------------------------------- Adressen

def url(search_id: int | None, item_id: str | None, image: str | None, n: int = 0, big: bool = False) -> str | None:
    """Adresse eines Artikelbildes in Ebay Watcher, z. B. /bild/3/v1%7C1234%7C0?v=…&s=…
    n: Nummer des Bildes (0 = Hauptbild), big: groß für die Galerie."""
    if not image or search_id is None or not item_id:
        return None
    version = hashlib.sha256(image.encode()).hexdigest()[:8]
    extra = (f"&n={n}" if n else "") + ("&g=1" if big else "")
    return (f"/bild/{search_id}/{quote(str(item_id), safe='')}"
            f"?v={version}&s={signed.sign(search_id, item_id)}{extra}")


def valid(search_id: int, item_id: str, signature: str | None) -> bool:
    return signed.valid(signature, search_id, item_id)


def originals(search_id: int, item_id: str) -> list[str]:
    """eBay-Adressen aller Bilder eines Angebots, das Hauptbild zuerst. Aus dem gemerkten Angebot,
    sonst aus der Kopie bei „Ausgeblendet“ (dort nur das Hauptbild)."""
    row = db.q1("SELECT image, images FROM items WHERE search_id = ? AND item_id = ?", (search_id, item_id))
    if row:
        try:
            found = [u for u in json.loads(row["images"] or "[]") if isinstance(u, str) and u]
        except ValueError:
            found = []
        if found:
            return found
        if row["image"]:
            return [row["image"]]
    row = db.q1("SELECT image FROM hidden_items WHERE search_id = ? AND item_id = ?", (search_id, item_id))
    return [row["image"]] if row and row["image"] else []


def source(search_id: int, item_id: str, n: int = 0, big: bool = False) -> str | None:
    """eBay-Adresse eines Bildes in der Größe, in der Ebay Watcher es lädt."""
    found = originals(search_id, item_id)
    if not 0 <= n < len(found):
        return None
    return util.large_image(found[n], GALLERY_SIZE if big else THUMB_SIZE)


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


def dimensions(path: Path) -> tuple[int, int] | None:
    """Breite und Höhe eines Bildes aus dem Dateikopf (die Galerie braucht sie vorab)."""
    try:
        with path.open("rb") as f:
            head = f.read(30)
            kind = _image_type(head[:16])
            if kind == "image/png":
                return struct.unpack(">II", head[16:24])
            if kind == "image/gif":
                return struct.unpack("<HH", head[6:10])
            if kind == "image/webp":
                return _webp_size(head)
            if kind == "image/jpeg":
                return _jpeg_size(f)
    except (OSError, struct.error, ValueError):
        pass
    return None


def _webp_size(head: bytes) -> tuple[int, int] | None:
    chunk = head[12:16]
    if chunk == b"VP8 ":     # verlustbehaftet
        w, h = struct.unpack("<HH", head[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L":     # verlustfrei
        bits = int.from_bytes(head[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":     # erweitert
        return int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
    return None


_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_size(f) -> tuple[int, int] | None:
    """JPEG-Abschnitte bis zum Frame-Kopf durchgehen. Dreht eine EXIF-Angabe das Bild um 90°,
    zeigt der Browser es gedreht, dann sind Breite und Höhe vertauscht."""
    f.seek(2)
    swap = False
    while True:
        byte = f.read(1)
        while byte and byte != b"\xff":
            byte = f.read(1)
        while byte == b"\xff":
            byte = f.read(1)
        if not byte:
            return None
        marker = byte[0]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue                       # Abschnitte ohne Länge
        length = struct.unpack(">H", f.read(2))[0]
        if marker in _SOF:
            h, w = struct.unpack(">HH", f.read(5)[1:5])
            return (h, w) if swap else (w, h)
        data = f.read(length - 2)
        if marker == 0xE1 and data[:6] == b"Exif\0\0":
            try:
                swap = _exif_orientation(data[6:]) in (5, 6, 7, 8)
            except struct.error:
                pass   # kaputte EXIF-Angaben: Maße wie gespeichert


def _exif_orientation(tiff: bytes) -> int | None:
    order = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if not order:
        return None
    offset = struct.unpack(order + "I", tiff[4:8])[0]
    count = struct.unpack(order + "H", tiff[offset:offset + 2])[0]
    for i in range(count):
        entry = tiff[offset + 2 + i * 12: offset + 14 + i * 12]
        if len(entry) == 12 and struct.unpack(order + "H", entry[:2])[0] == 0x0112:
            return struct.unpack(order + "H", entry[8:10])[0]
    return None


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
