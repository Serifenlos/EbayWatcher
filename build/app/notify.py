"""Push-Benachrichtigungen über eine ntfy-Instanz (JSON-Veröffentlichung, Umlaute problemlos)."""
from __future__ import annotations

import httpx


class NtfyError(Exception):
    pass


def configured(settings: dict) -> bool:
    return bool(settings.get("ntfy_url") and settings.get("ntfy_topic"))


async def send(settings: dict, *, title: str, message: str, click: str | None = None,
               image: str | None = None, priority: int = 3, tags: list[str] | None = None,
               actions: list[dict] | None = None) -> None:
    base = (settings.get("ntfy_url") or "").strip().rstrip("/")
    topic = (settings.get("ntfy_topic") or "").strip()
    if not base or not topic:
        raise NtfyError("ntfy ist noch nicht eingerichtet.")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base

    payload: dict = {"topic": topic, "title": title, "message": message, "priority": priority}
    if tags:
        payload["tags"] = tags
    if click:
        payload["click"] = click
    if image:
        payload["attach"] = image
        payload["filename"] = "artikel.jpg"
    if actions:
        payload["actions"] = actions

    headers = {}
    token = (settings.get("ntfy_token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(base, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise NtfyError(f"ntfy unter {base} ist nicht erreichbar ({e.__class__.__name__}).") from e
    if r.status_code in (401, 403):
        raise NtfyError("ntfy lehnt den Zugriff ab. Prüfe den Access-Token und die Rechte für das Topic.")
    if r.status_code >= 300:
        raise NtfyError(f"ntfy antwortet mit Fehler {r.status_code}.")
