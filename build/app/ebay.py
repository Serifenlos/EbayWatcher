"""Zugriff auf die eBay Browse API (Nachfolger der abgeschalteten Finding API)."""
from __future__ import annotations

import base64
import logging
import time
import urllib.parse

import httpx

from . import config, db

log = logging.getLogger("ebay-watcher.ebay")

API = "https://api.ebay.com"
SCOPE = "https://api.ebay.com/oauth/api_scope"


class EbayError(Exception):
    pass


def _error_text(r: httpx.Response) -> str:
    if r.status_code == 429:
        return "Das Tageslimit der eBay-API ist erreicht. Morgen geht es automatisch weiter."
    try:
        errors = r.json().get("errors") or []
        msg = errors[0].get("longMessage") or errors[0].get("message") if errors else ""
    except ValueError:
        msg = ""
    return f"eBay antwortet mit Fehler {r.status_code}" + (f": {msg[:200]}" if msg else ".")


class EbayClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._token_exp = 0.0
        self._http = httpx.AsyncClient(timeout=25.0, headers={"User-Agent": "ebay-watcher/1.0"})

    @property
    def configured(self) -> bool:
        return bool(config.EBAY_CLIENT_ID and config.EBAY_CLIENT_SECRET)

    async def close(self) -> None:
        await self._http.aclose()

    async def _token_value(self) -> str:
        if self._token and time.time() < self._token_exp - 120:
            return self._token
        if not self.configured:
            raise EbayError("Die eBay-Zugangsdaten fehlen. Trage EBAY_CLIENT_ID und EBAY_CLIENT_SECRET in Portainer ein.")
        basic = base64.b64encode(f"{config.EBAY_CLIENT_ID}:{config.EBAY_CLIENT_SECRET}".encode()).decode()
        try:
            r = await self._http.post(
                f"{API}/identity/v1/oauth2/token",
                headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials", "scope": SCOPE},
            )
        except httpx.HTTPError as e:
            raise EbayError(f"eBay ist nicht erreichbar ({e.__class__.__name__}).") from e
        if r.status_code != 200:
            raise EbayError(f"Die Anmeldung bei eBay ist fehlgeschlagen ({r.status_code}). Prüfe App ID und Cert ID.")
        data = r.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 7200))
        return self._token

    def _headers(self, token: str, plz: str) -> dict[str, str]:
        ctx = "country=DE" + (f",zip={plz}" if plz else "")
        return {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": config.EBAY_MARKETPLACE,
            "X-EBAY-C-ENDUSERCTX": "contextualLocation=" + urllib.parse.quote(ctx, safe=""),
            "Accept-Language": "de-DE",
        }

    async def _get(self, path: str, params: dict | None = None, plz: str = "", count: bool = True) -> httpx.Response:
        for attempt in (1, 2):
            token = await self._token_value()
            if count:
                db.count_api_call()
            try:
                r = await self._http.get(API + path, params=params, headers=self._headers(token, plz))
            except httpx.HTTPError as e:
                raise EbayError(f"eBay ist nicht erreichbar ({e.__class__.__name__}).") from e
            if r.status_code == 401 and attempt == 1:
                self._token = None  # Token abgelaufen oder widerrufen: einmal neu holen
                continue
            return r
        return r

    async def search(self, q: str, filters: list[str], sort: str | None = None,
                     plz: str = "", max_pages: int = 3) -> list[dict]:
        items: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            params = {"q": q, "filter": ",".join(filters), "limit": "200", "offset": str(offset)}
            if sort:
                params["sort"] = sort
            r = await self._get("/buy/browse/v1/item_summary/search", params, plz)
            if r.status_code != 200:
                raise EbayError(_error_text(r))
            data = r.json()
            batch = data.get("itemSummaries") or []
            items.extend(batch)
            offset += len(batch)
            if not batch or not data.get("next") or offset >= int(data.get("total") or 0):
                break
        return items

    async def get_item(self, item_id: str, plz: str = "") -> dict | None:
        r = await self._get("/buy/browse/v1/item/" + urllib.parse.quote(item_id, safe=""), plz=plz)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise EbayError(_error_text(r))
        return r.json()

    async def rate_limits(self) -> dict:
        """Kontingent der Browse API laut eBay (Developer Analytics API)."""
        r = await self._get("/developer/analytics/v1_beta/rate_limit/",
                            {"api_context": "buy", "api_name": "Browse"}, count=False)
        if r.status_code != 200:
            raise EbayError(_error_text(r))
        for limit in r.json().get("rateLimits") or []:
            if (limit.get("apiName") or "").lower() != "browse":
                continue
            for resource in limit.get("resources") or []:
                for rate in resource.get("rates") or []:
                    return {"limit": rate.get("limit"), "remaining": rate.get("remaining"),
                            "reset": rate.get("reset")}
        return {}
