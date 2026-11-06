"""Weboberfläche von Heuhaufen."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, db, details, images, market, notify, signed, util
from .plattformen import liste as plattformen
from .plattformen.basis import PURPOSES, PlatformError
from .watcher import Watcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("heuhaufen")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
watcher = Watcher()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    auth.ensure_admin()
    db.adopt_owner_data()   # alles, was noch keinem Konto gehört, dem Admin geben
    plattformen.adopt_from_environment(dict(os.environ), log)
    watcher.start()
    log.info("Heuhaufen %s läuft.", config.APP_VERSION)
    yield
    await watcher.stop()
    await images.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

_asset_versions: dict[str, tuple[float, str]] = {}


def asset(path: str) -> str:
    """Adresse einer Datei aus static/ mit Versionskennung, z. B. /static/css/app.css?v=3f2a9c1e0b.
    Ändert sich die Datei (neuer CodeKit-Build), ändert sich die Kennung und der Browser lädt sie neu."""
    file = STATIC_DIR / path
    try:
        mtime = file.stat().st_mtime
    except OSError:
        return f"/static/{path}"
    cached = _asset_versions.get(path)
    if not cached or cached[0] != mtime:
        cached = (mtime, hashlib.sha256(file.read_bytes()).hexdigest()[:10])
        _asset_versions[path] = cached
    return f"/static/{path}?v={cached[1]}"


def image_url(v: dict) -> str | None:
    """Adresse des Artikelbildes im eigenen Bild-Cache (siehe images.py)."""
    return images.url(v.get("search_id"), v.get("item_id"), v.get("image"))


def gallery_url(v: dict) -> str:
    """Bilder eines Angebots für die Galerie (Klick aufs Vorschaubild, siehe app.js)."""
    return f"/galerie/{v['search_id']}/{quote(str(v['item_id']), safe='')}"


templates.env.globals.update(
    eur=util.eur, countdown=util.countdown, local_time=util.local_time, ago=util.ago, date_time=util.date_time,
    gauge=util.gauge, asset=asset, image_url=image_url, gallery_url=gallery_url, app_name=config.APP_NAME,
    app_version=config.APP_VERSION, repo_url=config.REPO_URL,
)

MESSAGES = {
    "saved": "Gespeichert.",
    "created": "Gesuch angelegt. Die erste Suche startet gleich.",
    "updated": "Änderungen gespeichert. Die Suche startet gleich neu.",
    "deleted": "Gesuch gelöscht.",
    "paused": "Gesuch pausiert.",
    "resumed": "Gesuch läuft wieder.",
    "run": "Suche gestartet. Lade die Seite in einem Moment neu.",
    "ntfy_sent": "Testnachricht gesendet. Sie sollte gleich auf deinem Handy erscheinen.",
    "ntfy_failed": "Die Testnachricht ist nicht angekommen.",
    "password": "Passwort geändert. Alle anderen Geräte sind abgemeldet.",
    "2fa_on": "Die Zwei-Faktor-Anmeldung ist aktiv.",
    "2fa_off": "Die Zwei-Faktor-Anmeldung ist ausgeschaltet.",
    "sessions": "Alle anderen Geräte sind abgemeldet.",
    "checked": "Verbindung geprüft.",
    "credentials": "Zugangsdaten gespeichert.",
    "hidden": "Angebot ausgeblendet.",
    "market": "Gespeichert. Neue Vergleichsangebote holt Heuhaufen in den nächsten Minuten.",
    "display": "Gespeichert.",
    "unhidden": "Angebot wieder eingeblendet.",
    "logout": "Du bist abgemeldet.",
    "email": "E-Mail-Adresse gespeichert.",
    "welcome": "Willkommen bei Heuhaufen! Trage zuerst deine eBay-Zugangsdaten ein.",
    "reset": "Neues Passwort gespeichert. Melde dich jetzt damit an.",
    "invite_revoked": "Einladung zurückgezogen.",
    "user_locked": "Konto gesperrt. Seine Gesuche ruhen, bis du es wieder entsperrst.",
    "user_unlocked": "Konto entsperrt.",
    "user_deleted": "Konto mit allen Gesuchen gelöscht.",
}

# Bilder kommen aus dem eigenen Cache; von den Plattformen nur, falls der Cache ein Bild nicht laden konnte
CSP = ("default-src 'self'; img-src 'self' data: " + " ".join(f"https://*{h}" for h in plattformen.image_hosts())
       + "; style-src 'self'; script-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
# /bild/ und /erinnern/ ohne Login: Sie ruft das Handy aus der Benachrichtigung auf. Geschützt durch die Signatur.
# /einladung/ und /zuruecksetzen/: Links vom Admin, geschützt durch das Geheimnis im Link (auth.create_token).
PUBLIC_PREFIXES = ("/login", "/static/", "/healthz", "/favicon", "/bild/", "/erinnern/", "/einladung/",
                   "/zuruecksetzen/")
# Seiten eines Gesuchs: nur für das Konto, dem es gehört (Prüfung in guard)
SEARCH_PATH = re.compile(r"^/(?:searches|galerie)/(\d+)(?:/|$)")


# ---------------------------------------------------------------- Hilfen

class CsrfFailed(Exception):
    pass


def _secure_cookies(request: Request) -> bool:
    if config.COOKIE_SECURE in ("1", "true", "yes"):
        return True
    if config.COOKIE_SECURE in ("0", "false", "no"):
        return False
    return request.url.scheme == "https"


def set_cookie(resp: Response, request: Request, key: str, value: str, max_age: int | None = None) -> None:
    resp.set_cookie(key, value, max_age=max_age, httponly=True, samesite="lax",
                    secure=_secure_cookies(request), path="/")


def _with_headers(resp: Response) -> Response:
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unbekannt"


def safe_next(value: object) -> str:
    if isinstance(value, str) and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    sess = getattr(request.state, "session", None)
    ctx.setdefault("csrf", sess["csrf"] if sess else "")
    ctx.setdefault("flash_ok", MESSAGES.get(request.query_params.get("ok", "")))
    ctx.setdefault("flash_err", MESSAGES.get(request.query_params.get("err", "")))
    ctx.setdefault("active", "")
    ctx["signed_in"] = sess is not None
    user = getattr(request.state, "user", None)
    ctx["is_admin"] = bool(user and user["role"] == "admin")
    if sess is not None:
        ctx.setdefault("prices_with_shipping", util.with_shipping(db.get_settings(sess["user_id"])))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def not_found(request: Request) -> HTMLResponse:
    return render(request, "message.html", status_code=404, heading="Nicht gefunden",
                  text="Dieses Gesuch gibt es nicht mehr.", link="/", link_text="Zur Übersicht")


async def checked_form(request: Request):
    form = await request.form()
    sess = request.state.session
    if not sess or not auth.csrf_ok(form.get("csrf"), sess["csrf"]):
        raise CsrfFailed()
    return form


def current_user(request: Request) -> dict:
    return db.q1("SELECT * FROM users WHERE id = ?", (request.state.session["user_id"],))


def uid(request: Request) -> int:
    """Konto der angemeldeten Person."""
    return request.state.session["user_id"]


def user_settings(request: Request) -> dict:
    return db.get_settings(uid(request))


def back(path: str, key: str | None = None, err: bool = False, anchor: str = "") -> RedirectResponse:
    url = path + (f"?{'err' if err else 'ok'}={key}" if key else "") + (f"#{anchor}" if anchor else "")
    return RedirectResponse(url, 303)


# ---------------------------------------------------------------- Middleware

@app.middleware("http")
async def guard(request: Request, call_next):
    request.state.session = None
    path = request.url.path
    request.state.user = None
    legacy = None
    if not path.startswith(PUBLIC_PREFIXES):
        token, is_legacy = auth.session_token(request.cookies)
        sess = auth.load_session(token)
        user = db.q1("SELECT * FROM users WHERE id = ?", (sess["user_id"],)) if sess else None
        if user and not user["active"]:   # gesperrt: abmelden
            db.ex("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
            sess = user = None
        if not sess:
            target = "/login"
            if request.method == "GET" and path != "/":
                target += "?next=" + quote(path)
            return _with_headers(RedirectResponse(target, 303))
        request.state.session = sess
        request.state.user = user
        legacy = token if is_legacy else None
        match = SEARCH_PATH.match(path)
        if match:   # fremde Gesuche gibt es für dieses Konto nicht
            owner = db.q1("SELECT user_id FROM searches WHERE id = ?", (int(match.group(1)),))
            if owner and owner["user_id"] != user["id"] and not (owner["user_id"] is None and user["role"] == "admin"):
                return _with_headers(not_found(request))
    resp = await call_next(request)
    if legacy:   # Anmeldung aus „Ebay Watcher“: Cookie unter dem neuen Namen weiterführen
        remaining = int(request.state.session["expires_at"] - time.time())
        set_cookie(resp, request, auth.SESSION_COOKIE, legacy,
                   max_age=max(60, remaining) if request.state.session["remember"] else None)
        resp.delete_cookie(auth.LEGACY_SESSION_COOKIE, path="/")
    return _with_headers(resp)


@app.exception_handler(CsrfFailed)
async def csrf_failed(request: Request, _exc: CsrfFailed):
    return render(request, "message.html", status_code=400, heading="Formular abgelaufen",
                  text="Lade die Seite neu und versuche es noch einmal.", link="/", link_text="Zur Übersicht")


@app.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")


@app.post("/erinnern/{sid}/{item_id}")
async def remind_item(request: Request, sid: int, item_id: str):
    """Knopf „1 Min vorher erinnern“ in der Benachrichtigung (ntfy schickt dafür einen POST vom Handy)."""
    if not signed.valid(request.query_params.get("s"), "erinnern", sid, item_id):
        return PlainTextResponse("Ungültiger Link.", 403)
    item = db.q1("SELECT * FROM items WHERE search_id = ? AND item_id = ?", (sid, item_id))
    if not item or item["status"] != "active" or not item["end_time"]:
        return PlainTextResponse("Das Angebot gibt es nicht mehr.", 404)
    if item["end_time"] - time.time() <= Watcher.REMIND_BEFORE + 10:
        return PlainTextResponse("Dafür ist es zu spät, die Auktion endet gleich.", 409)
    db.ex("UPDATE items SET remind = 1, reminded = 0 WHERE search_id = ? AND item_id = ?", (sid, item_id))
    watcher.poke()
    return PlainTextResponse("Erinnerung gesetzt: 1 Minute vor Ende kommt ein Hinweis, "
                             "wenn das Angebot dann noch unter deinem Limit liegt.")


@app.api_route("/bild/{sid}/{item_id}", methods=["GET", "HEAD"])
async def article_image(request: Request, sid: int, item_id: str):
    """Artikelbild aus dem Bild-Cache. Beim ersten Abruf lädt Heuhaufen es einmal bei der Plattform.
    n: Nummer des Bildes (Galerie), g=1: groß."""
    if not images.valid(sid, item_id, request.query_params.get("s")):
        return PlainTextResponse("Ungültige Bildadresse.", 403)
    n = request.query_params.get("n", "0")
    if not n.isdigit():
        return PlainTextResponse("Bild nicht gefunden.", 404)
    address = images.source(sid, item_id, int(n), request.query_params.get("g") == "1")
    if not address or not images.allowed(address):
        return PlainTextResponse("Bild nicht gefunden.", 404)
    path = await images.cached(address)
    if not path:
        # Notlösung: der Browser holt das Bild selbst bei der Plattform, wie ohne Cache
        return RedirectResponse(address, 302, headers={"Cache-Control": "no-store"})
    return FileResponse(path, media_type=images.media_type(path),
                        headers={"Cache-Control": f"private, max-age={images.BROWSER_CACHE_SECONDS}"})


@app.get("/galerie/{sid}/{item_id}")
async def gallery(request: Request, sid: int, item_id: str):
    """Bilder eines Angebots für die Galerie (PhotoSwipe, siehe app.js), mit Breite und Höhe, dazu die
    Seitenleiste (panel) als fertiges HTML. Beim ersten Öffnen lädt Heuhaufen alle Bilder groß bei der
    Plattform und legt sie in den Bild-Cache. Beschreibung und Gebote holt es mit einer Einzelabfrage,
    höchstens alle 10 Minuten (details.py)."""
    row = db.q1("SELECT * FROM items WHERE search_id = ? AND item_id = ?", (sid, item_id))
    settings = user_settings(request)
    p = plattformen.of_item(row) if row else None
    if row and row["status"] == "active" and p.details and p.configured(settings) and details.due(item_id):
        try:
            await watcher.refresh(row, settings)
        except PlatformError as e:
            log.info("Galerie %s: %s", item_id, e)   # dann eben mit dem letzten bekannten Stand
    found = images.originals(sid, item_id)
    if not found:
        return JSONResponse({"items": []}, 404)
    paths = await asyncio.gather(*(images.cached(images.source(sid, item_id, n, big=True))
                                   for n in range(len(found))))
    items = []
    for n, (original, path) in enumerate(zip(found, paths)):
        size = images.dimensions(path) if path else None
        if size:   # Bilder, die die Plattform gerade nicht liefert, fehlen in der Galerie
            items.append({"src": images.url(sid, item_id, original, n, big=True), "width": size[0], "height": size[1]})
    return JSONResponse({"items": items, "panel": _gallery_panel(sid, item_id, settings)},
                        headers={"Cache-Control": "no-store"})


def _gallery_panel(sid: int, item_id: str, settings: dict) -> str:
    """Seitenleiste der Galerie: Preis, Zustand, Ø, Gebote, Merkmale und Beschreibung (includes/gallery_panel.html)."""
    s = db.q1("SELECT * FROM searches WHERE id = ?", (sid,))
    if not s:
        return ""
    now = time.time()
    overview = util.search_overview(s, db.q("SELECT * FROM items WHERE search_id = ?", (sid,)), now, db.hidden_ids(sid))
    v = None
    for group in ("matches", "over"):   # Ø wie in der Liste: im Vergleich mit den übrigen Angeboten
        if any(o["item_id"] == item_id for o in overview[group]):
            market.attach(s, settings, overview[group])
            v = next(o for o in overview[group] if o["item_id"] == item_id)
    if v is None:
        v = next((h for h in _hidden_views(s, now) if h["item_id"] == item_id), None)
    row = db.q1("SELECT * FROM items WHERE search_id = ? AND item_id = ?", (sid, item_id))
    if v is None and row:   # beendet oder bei der Plattform verschwunden
        v = {**util.item_view(row, s, now), "kind": "Beendet", "end_time": None, "market": None}
    if v is None:
        return ""
    return templates.get_template("includes/gallery_panel.html").render(
        v=v, d=details.load(item_id), now=now, gone=not row or row["status"] != "active",
        prices_with_shipping=util.with_shipping(settings))


# ---------------------------------------------------------------- Anmeldung

def _login_form(request: Request, next_: str = "/", error: str | None = None,
                username: str = "", status: int = 200) -> HTMLResponse:
    token = secrets.token_urlsafe(24)
    resp = render(request, "login.html", status_code=status, csrf=token, next=safe_next(next_),
                  error=error, username=username)
    set_cookie(resp, request, auth.LOGIN_CSRF_COOKIE, token, max_age=3600)
    return resp


def _complete_login(request: Request, user_id: int, remember: bool, next_: str) -> RedirectResponse:
    db.ex("UPDATE users SET last_login = ? WHERE id = ?", (time.time(), user_id))
    token = auth.create_session(user_id, remember, request.headers.get("user-agent", ""))
    resp = RedirectResponse(next_, 303)
    set_cookie(resp, request, auth.SESSION_COOKIE, token, max_age=auth.REMEMBER_SECONDS if remember else None)
    resp.delete_cookie(auth.LOGIN_CSRF_COOKIE, path="/")
    resp.delete_cookie(auth.PENDING_COOKIE, path="/")
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if auth.load_session(auth.session_token(request.cookies)[0]):
        return RedirectResponse("/", 303)
    return _login_form(request, request.query_params.get("next", "/"))


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    next_ = safe_next(form.get("next"))
    username = str(form.get("username", "")).strip()[:100]
    password = str(form.get("password", ""))
    remember = form.get("remember") == "1"

    if not auth.csrf_ok(form.get("csrf"), request.cookies.get(auth.LOGIN_CSRF_COOKIE)):
        return _login_form(request, next_, "Das Formular war abgelaufen. Bitte melde dich noch einmal an.", username, 400)

    ip = client_ip(request)
    wait = auth.throttle.wait_seconds(ip, username)
    if wait:
        return _login_form(request, next_, f"Zu viele Fehlversuche. Versuche es in {max(1, wait // 60)} Minuten wieder.",
                           username, 429)

    user = auth.find_login(username)   # Benutzername oder E-Mail-Adresse
    if not auth.verify_password(user, password):
        auth.throttle.fail(ip, username)
        return _login_form(request, next_, "Benutzername oder Passwort stimmt nicht.", username, 401)
    auth.throttle.reset(ip)
    if not user["active"]:
        return _login_form(request, next_, "Dieses Konto ist gesperrt. Wende dich an die Person, die Heuhaufen "
                                           "betreibt.", username, 403)

    if user["totp_enabled"]:
        token = auth.create_session(user["id"], remember, request.headers.get("user-agent", ""), pending=True)
        resp = RedirectResponse("/login/2fa?next=" + quote(next_), 303)
        set_cookie(resp, request, auth.PENDING_COOKIE, token, max_age=auth.PENDING_SECONDS)
        return resp
    return _complete_login(request, user["id"], remember, next_)


@app.get("/login/2fa", response_class=HTMLResponse)
async def login_2fa_page(request: Request):
    pending = auth.load_session(request.cookies.get(auth.PENDING_COOKIE), pending=True)
    if not pending:
        return RedirectResponse("/login", 303)
    return render(request, "login_2fa.html", csrf=pending["csrf"],
                  next=safe_next(request.query_params.get("next", "/")), error=None)


@app.post("/login/2fa")
async def login_2fa(request: Request):
    token = request.cookies.get(auth.PENDING_COOKIE)
    pending = auth.load_session(token, pending=True)
    if not pending:
        return RedirectResponse("/login", 303)
    form = await request.form()
    next_ = safe_next(form.get("next"))
    if not auth.csrf_ok(form.get("csrf"), pending["csrf"]):
        return RedirectResponse("/login", 303)

    user = db.q1("SELECT * FROM users WHERE id = ?", (pending["user_id"],))
    ip = client_ip(request)
    wait = auth.throttle.wait_seconds(ip, user["username"])
    if wait:
        auth.destroy_session(token)
        return _login_form(request, next_, f"Zu viele Fehlversuche. Versuche es in {max(1, wait // 60)} Minuten wieder.",
                           status=429)
    if not auth.verify_totp(user, str(form.get("code", ""))):
        auth.throttle.fail(ip, user["username"])
        return render(request, "login_2fa.html", status_code=401, csrf=pending["csrf"], next=next_,
                      error="Der Code stimmt nicht. Nimm den aktuellen Code aus deiner Authenticator-App.")
    auth.throttle.reset(ip)
    auth.destroy_session(token)
    return _complete_login(request, user["id"], bool(pending["remember"]), next_)


@app.post("/logout")
async def logout(request: Request):
    await checked_form(request)
    auth.destroy_session(auth.session_token(request.cookies)[0])
    resp = RedirectResponse("/login?ok=logout", 303)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    resp.delete_cookie(auth.LEGACY_SESSION_COOKIE, path="/")
    return resp


# ---------------------------------------------------------------- Übersicht

def _notices(request: Request) -> list[dict]:
    s = user_settings(request)
    out = []
    if not plattformen.any_configured(s):
        p = plattformen.get(plattformen.DEFAULT)
        out.append({"text": f"Die Zugangsdaten für {p.name} fehlen, deshalb sucht Heuhaufen noch nicht.",
                    "link": f"/settings#{p.key}", "action": "Eintragen"})
    if not notify.configured(s):
        out.append({"text": "Benachrichtigungen sind noch nicht eingerichtet.",
                    "link": "/settings#benachrichtigungen", "action": "Einrichten"})
    elif watcher.ntfy_error(uid(request)):
        out.append({"text": f"Die letzte Benachrichtigung ist nicht angekommen. {watcher.ntfy_error(uid(request))}",
                    "link": "/settings#benachrichtigungen", "action": "Prüfen"})
    if not s["plz"]:
        out.append({"text": "Ohne Postleitzahl findet Heuhaufen keine Abholangebote in deiner Nähe.",
                    "link": "/settings#standort", "action": "PLZ eintragen"})
    return out


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    now = time.time()
    searches = db.q("SELECT * FROM searches WHERE user_id = ? ORDER BY active DESC, created_at DESC", (uid(request),))
    hidden = db.hidden_ids_by_search()
    overviews = [
        util.search_overview(s, db.q("SELECT * FROM items WHERE search_id = ?", (s["id"],)), now,
                             hidden.get(s["id"]))
        for s in searches
    ]
    overviews.sort(key=util.dashboard_order)
    return render(request, "dashboard.html", active="searches", searches=overviews,
                  notices=_notices(request), last_tick=watcher.last_tick, now=now)


# ---------------------------------------------------------------- Gesuche

def _form_defaults(settings: dict) -> dict:
    return {
        "name": "", "query": "", "exclude": "", "format": "auction", "location": "DE",
        "max_price_raw": "", "incl_shipping": 1, "free_shipping_only": 0, "condition": "any",
        "lead_raw": str(db.setting_int(settings, "lead_minutes", 10)), "interval_raw": "",
        "pickup_mode": "radius", "active": 1,
    }


def _values_from_search(s: dict) -> dict:
    price = s["max_price"]
    raw = f"{price:.0f}" if float(price).is_integer() else f"{price:.2f}".replace(".", ",")
    return {**s, "max_price_raw": raw, "lead_raw": str(s["lead_minutes"]),
            "interval_raw": str(s["interval_minutes"] or "")}


def _parse_search_form(form) -> tuple[dict, dict]:
    def pick(name: str, allowed, default: str) -> str:
        value = form.get(name)
        return value if value in allowed else default

    v = {
        "name": str(form.get("name", "")).strip()[:80],
        "query": str(form.get("query", "")).strip()[:200],
        "exclude": str(form.get("exclude", "")).strip()[:300],
        "format": pick("format", util.FORMAT_LABEL, "auction"),
        "location": pick("location", util.LOCATION_LABEL, "DE"),
        "condition": pick("condition", util.CONDITION_LABEL, "any"),
        "pickup_mode": pick("pickup_mode", ("radius", "all", "hide"), "radius"),
        "incl_shipping": 1 if form.get("incl_shipping") else 0,
        "free_shipping_only": 1 if form.get("free_shipping_only") else 0,
        "active": 1 if form.get("active") else 0,
        "max_price_raw": str(form.get("max_price", "")).strip()[:20],
        "lead_raw": str(form.get("lead_minutes", "")).strip()[:5],
        "interval_raw": str(form.get("interval_minutes", "")).strip()[:5],
    }
    errors = {}
    if len(v["query"]) < 2:
        errors["query"] = "Gib mindestens zwei Zeichen ein."
    v["max_price"] = util.parse_price(v["max_price_raw"])
    if v["max_price"] is None:
        errors["max_price"] = "Gib einen Betrag über 0 € ein, zum Beispiel 250 oder 249,90."
    try:
        v["lead_minutes"] = int(v["lead_raw"])
        if not 1 <= v["lead_minutes"] <= 180:
            raise ValueError
    except ValueError:
        errors["lead_minutes"] = "Wähle 1 bis 180 Minuten."
        v["lead_minutes"] = 10
    v["interval_minutes"] = None   # leer: Suchintervall aus den Einstellungen
    if v["interval_raw"]:
        if v["interval_raw"].isdigit() and 5 <= int(v["interval_raw"]) <= 240:
            v["interval_minutes"] = int(v["interval_raw"])
        else:
            errors["interval_minutes"] = "Wähle 5 bis 240 Minuten oder lass das Feld leer."
    return v, errors


SEARCH_COLUMNS = ["name", "query", "exclude", "format", "location", "max_price", "incl_shipping",
                  "free_shipping_only", "condition", "lead_minutes", "interval_minutes", "pickup_mode", "active"]


def _render_form(request: Request, values: dict, errors: dict, mode: str, sid: int | None = None,
                 status: int = 200) -> HTMLResponse:
    settings = user_settings(request)
    return render(request, "search_form.html", status_code=status, active="searches", values=values,
                  errors=errors, mode=mode, sid=sid, has_plz=bool(settings["plz"]),
                  radius=db.setting_int(settings, "radius_km", 50),
                  interval_default=db.setting_int(settings, "interval_minutes", 15))


@app.get("/searches/new", response_class=HTMLResponse)
async def new_search_page(request: Request):
    return _render_form(request, _form_defaults(user_settings(request)), {}, "new")


@app.post("/searches/new")
async def create_search(request: Request):
    form = await checked_form(request)
    values, errors = _parse_search_form(form)
    if errors:
        return _render_form(request, values, errors, "new", status=422)
    sid = db.ex(
        f"INSERT INTO searches({', '.join(SEARCH_COLUMNS)}, user_id, created_at) "
        f"VALUES ({', '.join('?' * len(SEARCH_COLUMNS))}, ?, ?)",
        [*[values[c] for c in SEARCH_COLUMNS], uid(request), time.time()],
    )
    watcher.poke()
    return back(f"/searches/{sid}", "created")


@app.get("/searches/{sid}", response_class=HTMLResponse)
async def search_detail(request: Request, sid: int):
    s = db.q1("SELECT * FROM searches WHERE id = ?", (sid,))
    if not s:
        return not_found(request)
    now = time.time()
    overview = util.search_overview(s, db.q("SELECT * FROM items WHERE search_id = ?", (sid,)), now,
                                    db.hidden_ids(sid))
    settings = user_settings(request)
    market.attach(overview, settings, overview["matches"])
    if request.query_params.get("show") == "over":
        market.attach(overview, settings, overview["over"])
    # Ohne JavaScript lassen sich die eingeklappten Listen über ?show=over bzw. ?show=hidden aufklappen
    show = request.query_params.get("show")
    hidden = _hidden_views(s, now) if show == "hidden" else []
    return render(request, "search_detail.html", active="searches", s=overview, now=now,
                  show=show, hidden=hidden)


def _hidden_views(s: dict, now: float) -> list[dict]:
    rows = db.q("SELECT * FROM hidden_items WHERE search_id = ? ORDER BY hidden_at DESC", (s["id"],))
    items = {r["item_id"]: r for r in db.q(
        "SELECT * FROM items WHERE search_id = ? AND item_id IN "
        "(SELECT item_id FROM hidden_items WHERE search_id = ?)", (s["id"], s["id"]))}
    return [util.hidden_view(r, items.get(r["item_id"]), s, now) for r in rows]


@app.get("/searches/{sid}/list/{kind}", response_class=HTMLResponse)
async def search_list(request: Request, sid: int, kind: str):
    """Eingeklappte Listen („zu teuer“, „ausgeblendet“) werden erst beim Aufklappen geladen."""
    s = db.q1("SELECT * FROM searches WHERE id = ?", (sid,))
    if not s or kind not in ("over", "hidden"):
        return not_found(request)
    now = time.time()
    if kind == "over":
        offers = util.search_overview(s, db.q("SELECT * FROM items WHERE search_id = ?", (sid,)), now,
                                      db.hidden_ids(sid))["over"]
        market.attach(s, user_settings(request), offers)
    else:
        offers = _hidden_views(s, now)
    return render(request, "includes/offer_list.html", offers=offers, kind=kind, sid=sid, now=now)


@app.post("/searches/{sid}/hide")
async def hide_item(request: Request, sid: int):
    form = await checked_form(request)
    item_id = str(form.get("item_id", ""))[:100]
    item = db.q1("SELECT * FROM items WHERE search_id = ? AND item_id = ?", (sid, item_id))
    if not item:
        return not_found(request)
    db.ex(
        "INSERT INTO hidden_items(search_id, item_id, platform, title, url, image, hidden_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(search_id, item_id) DO NOTHING",
        (sid, item_id, item["platform"], item["title"], item["url"], item["image"], time.time()),
    )
    return back(f"/searches/{sid}", "hidden", anchor="treffer")


@app.post("/searches/{sid}/unhide")
async def unhide_item(request: Request, sid: int):
    form = await checked_form(request)
    item_id = str(form.get("item_id", ""))[:100]
    db.ex("DELETE FROM hidden_items WHERE search_id = ? AND item_id = ?", (sid, item_id))
    return back(f"/searches/{sid}", "unhidden", anchor="treffer")


@app.get("/searches/{sid}/edit", response_class=HTMLResponse)
async def edit_search_page(request: Request, sid: int):
    s = db.q1("SELECT * FROM searches WHERE id = ?", (sid,))
    if not s:
        return not_found(request)
    return _render_form(request, _values_from_search(s), {}, "edit", sid)


@app.post("/searches/{sid}/edit")
async def update_search(request: Request, sid: int):
    form = await checked_form(request)
    if not db.q1("SELECT id FROM searches WHERE id = ?", (sid,)):
        return not_found(request)
    values, errors = _parse_search_form(form)
    if errors:
        return _render_form(request, values, errors, "edit", sid, status=422)
    db.ex(
        f"UPDATE searches SET {', '.join(f'{c} = ?' for c in SEARCH_COLUMNS)}, last_run = NULL, "
        "last_error = NULL WHERE id = ?",
        [*[values[c] for c in SEARCH_COLUMNS], sid],
    )
    market.forget_search(sid)   # Suchbegriff oder Filter können sich geändert haben
    watcher.poke()
    return back(f"/searches/{sid}", "updated")


@app.post("/searches/{sid}/toggle")
async def toggle_search(request: Request, sid: int):
    await checked_form(request)
    s = db.q1("SELECT active FROM searches WHERE id = ?", (sid,))
    if not s:
        return not_found(request)
    db.ex("UPDATE searches SET active = ?, last_run = NULL WHERE id = ?", (0 if s["active"] else 1, sid))
    watcher.poke()
    return back(f"/searches/{sid}", "paused" if s["active"] else "resumed")


@app.post("/searches/{sid}/run")
async def run_search(request: Request, sid: int):
    await checked_form(request)
    db.ex("UPDATE searches SET last_run = NULL WHERE id = ?", (sid,))
    watcher.poke()
    return back(f"/searches/{sid}", "run")


@app.post("/searches/{sid}/delete")
async def delete_search(request: Request, sid: int):
    await checked_form(request)
    db.ex("DELETE FROM searches WHERE id = ?", (sid,))
    return back("/", "deleted")


# ---------------------------------------------------------------- Einstellungen

def _settings_page(request: Request, errors: dict | None = None, values: dict | None = None,
                   status: int = 200) -> HTMLResponse:
    user = current_user(request)
    current = request.state.session["token_hash"]
    sessions = [
        {**x, "device": util.device_label(x["user_agent"]), "current": x["token_hash"] == current}
        for x in db.q("SELECT * FROM sessions WHERE user_id = ? AND pending_2fa = 0 ORDER BY last_seen DESC",
                      (user["id"],))
    ]
    stored = db.get_settings(user["id"])
    s = {**stored, **(values or {})}
    return render(request, "settings.html", status_code=status, active="settings", s=s, user=user,
                  errors=errors or {}, sessions=sessions, platforms=_platform_panels(stored),
                  ntfy_error=watcher.ntfy_error(user["id"]), has_token=bool(s.get("ntfy_token")),
                  image_cache=images.stats() if user["role"] == "admin" else None,   # Cache: für alle gemeinsam
                  market_lookups_today=market.lookups_today(user["id"]))


def _platform_panels(settings: dict) -> list[dict]:
    """Je Plattform: Zugangsdaten, Abfragen heute nach Zweck (feste Reihenfolge), gestern, letzte Prüfung –
    alles für das Konto, dem diese Einstellungen gehören."""
    labels = {**PURPOSES, "ohne": "Vor dem Update, ohne Zuordnung"}
    user_id = settings.get("_user_id")
    out = []
    for p in plattformen.ALL.values():
        calls = db.api_calls_by_purpose(platform=p.key, user_id=user_id)
        out.append({
            "key": p.key, "name": p.name, "credentials": p.credentials, "configured": p.configured(settings),
            "check": watcher.last_checks.get((user_id, p.key)), "calls": db.api_calls_today(p.key, user_id),
            "purposes": [(label, calls[key]) for key, label in labels.items() if calls.get(key)],
            "calls_yesterday": sum(db.api_calls_by_purpose(1, p.key, user_id).values()),
            "hints": {c.key: plattformen.secret_hint(settings, c.key) for c in p.credentials if c.secret},
        })
    return out


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return _settings_page(request)


@app.post("/settings/general")
async def save_general(request: Request):
    form = await checked_form(request)
    admin = request.state.user["role"] == "admin"
    keys = ("radius_km", "lead_minutes", "interval_minutes") + (("app_url",) if admin else ())   # Adresse: nur Admin
    values = {k: str(form.get(k, "")).strip() for k in keys}
    values["plz"] = str(form.get("mittelpunkt", "")).strip()   # Feldname ohne „plz“, siehe settings.html
    errors = {}
    if values["plz"] and not re.fullmatch(r"\d{5}", values["plz"]):
        errors["plz"] = "Bitte fünf Ziffern eingeben, zum Beispiel 10115."
    for key, lo, hi in (("radius_km", 1, 500), ("lead_minutes", 1, 180), ("interval_minutes", 5, 240)):
        if not values[key].isdigit() or not lo <= int(values[key]) <= hi:
            errors[key] = f"Wähle eine Zahl von {lo} bis {hi}."
    if values.get("app_url") and not values["app_url"].startswith(("http://", "https://")):
        errors["app_url"] = "Die Adresse beginnt mit http:// oder https://."
    if errors:
        return _settings_page(request, errors, values, 422)
    old = user_settings(request)
    if (old["plz"], old["radius_km"]) != (values["plz"], values["radius_km"]):
        watcher.forget_pickup(uid(request))   # Abholangebote gleich neu suchen, nicht erst nach einer Stunde
    db.set_settings(values, uid(request))
    watcher.poke()
    return back("/settings", "saved", anchor="standort")


@app.post("/settings/display")
async def save_display(request: Request):
    form = await checked_form(request)
    db.set_settings({"price_with_shipping": "1" if form.get("price_with_shipping") else "0"}, uid(request))
    return back("/settings", "display", anchor="anzeige")


@app.post("/settings/market")
async def save_market(request: Request):
    form = await checked_form(request)
    values = {k: str(form.get(k, "")).strip() for k in ("market_threshold", "market_lookups", "market_months")}
    errors = {}
    for key, lo, hi in (("market_threshold", 1, 90), ("market_lookups", 0, 500),
                        ("market_months", 1, market.MAX_MONTHS)):
        if not values[key].isdigit() or not lo <= int(values[key]) <= hi:
            errors[key] = f"Wähle eine Zahl von {lo} bis {hi}."
    values["market_enabled"] = "1" if form.get("market_enabled") else "0"
    values["market_split"] = "1" if form.get("market_split") else "0"
    if errors:
        return _settings_page(request, errors, values, 422)
    db.set_settings(values, uid(request))
    watcher.poke()
    return back("/settings", "market", anchor="durchschnitt")


@app.post("/settings/ntfy")
async def save_ntfy(request: Request):
    form = await checked_form(request)
    values = {k: str(form.get(k, "")).strip() for k in ("ntfy_url", "ntfy_topic")}
    errors = {}
    if values["ntfy_url"] and not values["ntfy_url"].startswith(("http://", "https://")):
        errors["ntfy_url"] = "Die Adresse beginnt mit https://, zum Beispiel https://ntfy.deine-domain.de."
    if values["ntfy_topic"] and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", values["ntfy_topic"]):
        errors["ntfy_topic"] = "Erlaubt sind Buchstaben, Ziffern, Binde- und Unterstrich."
    if errors:
        return _settings_page(request, errors, values, 422)
    token = str(form.get("ntfy_token", "")).strip()
    if form.get("clear_token"):
        values["ntfy_token"] = ""
    elif token:
        values["ntfy_token"] = token
    db.set_settings(values, uid(request))
    watcher.ntfy_errors.pop(uid(request), None)
    return back("/settings", "saved", anchor="benachrichtigungen")


@app.post("/settings/ntfy/test")
async def test_ntfy(request: Request):
    await checked_form(request)
    try:
        await notify.send(user_settings(request), title="Heuhaufen ist verbunden",
                          message="So sehen deine Benachrichtigungen aus. Alles bereit.",
                          tags=["white_check_mark"], priority=3)
        watcher.ntfy_errors.pop(uid(request), None)
        return back("/settings", "ntfy_sent", anchor="benachrichtigungen")
    except notify.NtfyError as e:
        watcher.ntfy_errors[uid(request)] = str(e)
        return back("/settings", "ntfy_failed", err=True, anchor="benachrichtigungen")


@app.post("/settings/password")
async def change_password(request: Request):
    form = await checked_form(request)
    user = current_user(request)
    current, new, repeat = (str(form.get(k, "")) for k in ("current", "new", "repeat"))
    errors = {}
    if not auth.verify_password(user, current):
        errors["current"] = "Das aktuelle Passwort stimmt nicht."
    elif len(new) < auth.MIN_PASSWORD:
        errors["new"] = f"Das neue Passwort braucht mindestens {auth.MIN_PASSWORD} Zeichen."
    elif new != repeat:
        errors["repeat"] = "Die beiden Eingaben stimmen nicht überein."
    if errors:
        return _settings_page(request, errors, status=422)
    db.ex("UPDATE users SET password_hash = ? WHERE id = ?", (auth.hash_password(new), user["id"]))
    auth.destroy_other_sessions(user["id"], request.state.session["token_hash"])
    return back("/settings", "password", anchor="sicherheit")


@app.post("/settings/2fa/start", response_class=HTMLResponse)
async def start_2fa(request: Request):
    await checked_form(request)
    user = current_user(request)
    if user["totp_enabled"]:
        return back("/settings", anchor="sicherheit")
    secret = auth.new_totp_secret()
    db.ex("UPDATE users SET totp_secret = ? WHERE id = ?", (secret, user["id"]))
    return render(request, "twofactor.html", active="settings", secret=secret,
                  qr=auth.totp_qr_data_uri(secret, user["username"]), error=None)


@app.post("/settings/2fa/enable", response_class=HTMLResponse)
async def enable_2fa(request: Request):
    form = await checked_form(request)
    user = current_user(request)
    if not user["totp_secret"]:
        return back("/settings", anchor="sicherheit")
    if not auth.verify_totp(user, str(form.get("code", ""))):
        return render(request, "twofactor.html", status_code=422, active="settings", secret=user["totp_secret"],
                      qr=auth.totp_qr_data_uri(user["totp_secret"], user["username"]),
                      error="Der Code stimmt nicht. Nimm den aktuellen Code aus deiner Authenticator-App.")
    db.ex("UPDATE users SET totp_enabled = 1 WHERE id = ?", (user["id"],))
    return back("/settings", "2fa_on", anchor="sicherheit")


@app.post("/settings/2fa/disable")
async def disable_2fa(request: Request):
    form = await checked_form(request)
    user = current_user(request)
    if not auth.verify_password(user, str(form.get("password", ""))):
        return _settings_page(request, {"disable_2fa": "Das Passwort stimmt nicht."}, status=422)
    db.ex("UPDATE users SET totp_enabled = 0, totp_secret = NULL WHERE id = ?", (user["id"],))
    return back("/settings", "2fa_off", anchor="sicherheit")


@app.post("/settings/sessions")
async def revoke_sessions(request: Request):
    await checked_form(request)
    auth.destroy_other_sessions(request.state.session["user_id"], request.state.session["token_hash"])
    return back("/settings", "sessions", anchor="sicherheit")


@app.post("/settings/email")
async def save_email(request: Request):
    form = await checked_form(request)
    email = str(form.get("email", "")).strip()[:200]
    error = auth.check_email(email, exclude_id=uid(request)) if email else None
    if error:
        return _settings_page(request, {"email": error}, {"email": email}, 422)
    db.ex("UPDATE users SET email = ? WHERE id = ?", (email or None, uid(request)))
    return back("/settings", "email", anchor="sicherheit")


@app.post("/settings/plattform/{key}")
async def save_platform(request: Request, key: str):
    """Zugangsdaten einer Plattform speichern und gleich prüfen."""
    form = await checked_form(request)
    p = plattformen.ALL.get(key)
    if not p:
        return not_found(request)
    plattformen.save_credentials(p, form, uid(request))
    watcher.last_checks.pop((uid(request), p.key), None)
    if p.configured(user_settings(request)):
        await watcher.check_platform(p.key, uid(request))
        watcher.poke()
    return back("/settings", "credentials", anchor=p.key)


@app.post("/settings/plattform/{key}/test")
async def test_platform(request: Request, key: str):
    await checked_form(request)
    if key not in plattformen.ALL:
        return not_found(request)
    await watcher.check_platform(key, uid(request))
    return back("/settings", "checked", anchor=key)


# ---------------------------------------------------------------- Konten (nur für den Admin)
#
# Jedes Konto ist völlig getrennt: eigene Gesuche, Einstellungen und Zugangsdaten. Der Admin sieht hier
# nur die Konten selbst. Heuhaufen verschickt keine E-Mails: Einladung und Zurücksetzen erzeugen einen
# Link mit fertigem Text, den der Admin aus seinem Mailprogramm schickt (mailto oder kopieren).

def _is_admin(request: Request) -> bool:
    return bool(request.state.user and request.state.user["role"] == "admin")


def _base_url(request: Request) -> str:
    """Adresse für Links: die eingetragene Adresse von Heuhaufen, sonst die, über die der Admin gerade da ist."""
    return (db.get_settings().get("app_url") or "").strip().rstrip("/") or str(request.base_url).rstrip("/")


def _mail(request: Request, kind: str, token: str, expires: float, email: str | None,
          username: str | None = None) -> dict:
    """Link und fertiger Text für die E-Mail an die eingeladene bzw. zurückzusetzende Person."""
    admin = request.state.user["username"]
    until = util.date_time(expires)
    if kind == "invite":
        link = f"{_base_url(request)}/einladung/{token}"
        subject = "Einladung zu Heuhaufen"
        body = (f"Hallo,\n\nich habe dich zu Heuhaufen eingeladen. Heuhaufen beobachtet eBay-Suchen für dich und "
                f"meldet sich per Push-Nachricht aufs Handy, wenn etwas unter deinem Höchstpreis auftaucht.\n\n"
                f"Mit diesem Link legst du dein Konto an (gültig bis {until}, nur einmal):\n{link}\n\n"
                f"Dafür brauchst du:\n"
                f"- ein eigenes Konto im eBay Developer Program mit einem Production-Keyset (App ID und Cert ID), "
                f"Anleitung: {config.REPO_URL}#voraussetzungen\n"
                f"- die App ntfy auf dem Handy für die Benachrichtigungen\n\nViele Grüße\n{admin}")
    else:
        link = f"{_base_url(request)}/zuruecksetzen/{token}"
        subject = "Neues Passwort für Heuhaufen"
        body = (f"Hallo {username},\n\nmit diesem Link legst du ein neues Passwort für Heuhaufen fest "
                f"(gültig bis {until}, nur einmal):\n{link}\n\nDie Zwei-Faktor-Anmeldung ist danach ausgeschaltet. "
                f"Du kannst sie in den Einstellungen wieder einschalten.\n\nViele Grüße\n{admin}")
    mailto = None
    if email:
        mailto = (f"mailto:{quote(email, safe='@')}?subject={quote(subject, safe='')}"
                  f"&body={quote(body.replace(chr(10), chr(13) + chr(10)), safe='')}")
    return {"kind": kind, "email": email, "username": username, "link": link, "until": until,
            "subject": subject, "body": body, "mailto": mailto}


def _users_page(request: Request, created: dict | None = None, errors: dict | None = None,
                values: dict | None = None, status: int = 200) -> HTMLResponse:
    users = db.q("SELECT id, username, email, role, active, last_login, created_at, totp_enabled FROM users "
                 "ORDER BY role = 'admin' DESC, username COLLATE NOCASE")
    invites = db.q("SELECT id, email, created_at, expires_at FROM user_tokens WHERE kind = 'invite' "
                   "AND used_at IS NULL ORDER BY created_at DESC")
    return render(request, "users.html", status_code=status, active="users", users=users, invites=invites,
                  created=created, errors=errors or {}, values=values or {}, now=time.time(), me=uid(request),
                  has_app_url=bool((db.get_settings().get("app_url") or "").strip()))


def _other_user(request: Request, user_id: int) -> dict | None:
    """Ein anderes Konto als das eigene, aber nicht das des Admins."""
    row = db.q1("SELECT * FROM users WHERE id = ?", (user_id,))
    return row if row and row["id"] != uid(request) and row["role"] != "admin" else None


@app.get("/nutzer", response_class=HTMLResponse)
async def users_page(request: Request):
    if not _is_admin(request):
        return not_found(request)
    return _users_page(request)


@app.post("/nutzer/einladen", response_class=HTMLResponse)
async def invite_user(request: Request):
    form = await checked_form(request)
    if not _is_admin(request):
        return not_found(request)
    email = str(form.get("email", "")).strip()[:200]
    error = auth.check_email(email)
    if error:
        return _users_page(request, errors={"email": error}, values={"email": email}, status=422)
    token, expires = auth.create_token("invite", uid(request), email=email)
    return _users_page(request, created=_mail(request, "invite", token, expires, email))


@app.post("/nutzer/einladung/{tid}/neu", response_class=HTMLResponse)
async def renew_invite(request: Request, tid: int):
    await checked_form(request)
    row = db.q1("SELECT * FROM user_tokens WHERE id = ? AND kind = 'invite' AND used_at IS NULL", (tid,))
    if not _is_admin(request) or not row:
        return not_found(request)
    token, expires = auth.create_token("invite", uid(request), email=row["email"])
    return _users_page(request, created=_mail(request, "invite", token, expires, row["email"]))


@app.post("/nutzer/einladung/{tid}/zurueckziehen")
async def revoke_invite(request: Request, tid: int):
    await checked_form(request)
    if not _is_admin(request):
        return not_found(request)
    db.ex("DELETE FROM user_tokens WHERE id = ? AND kind = 'invite' AND used_at IS NULL", (tid,))
    return back("/nutzer", "invite_revoked")


@app.post("/nutzer/{user_id}/zuruecksetzen", response_class=HTMLResponse)
async def reset_link(request: Request, user_id: int):
    await checked_form(request)
    user = _other_user(request, user_id) if _is_admin(request) else None
    if not user:
        return not_found(request)
    token, expires = auth.create_token("reset", uid(request), email=user["email"], user_id=user["id"])
    return _users_page(request, created=_mail(request, "reset", token, expires, user["email"], user["username"]))


@app.post("/nutzer/{user_id}/sperren")
async def lock_user(request: Request, user_id: int):
    await checked_form(request)
    user = _other_user(request, user_id) if _is_admin(request) else None
    if not user:
        return not_found(request)
    db.ex("UPDATE users SET active = ? WHERE id = ?", (0 if user["active"] else 1, user["id"]))
    if user["active"]:
        db.ex("DELETE FROM sessions WHERE user_id = ?", (user["id"],))   # sofort abmelden
    return back("/nutzer", "user_locked" if user["active"] else "user_unlocked", anchor="konten")


@app.post("/nutzer/{user_id}/loeschen")
async def delete_user(request: Request, user_id: int):
    await checked_form(request)
    user = _other_user(request, user_id) if _is_admin(request) else None
    if not user:
        return not_found(request)
    db.ex("DELETE FROM users WHERE id = ?", (user["id"],))   # Gesuche, Einstellungen, Sitzungen folgen
    db.ex("DELETE FROM api_usage WHERE user_id = ?", (user["id"],))
    db.ex("DELETE FROM api_calls WHERE user_id = ?", (user["id"],))
    return back("/nutzer", "user_deleted", anchor="konten")


# ---------------------------------------------------------------- Einladung annehmen, Passwort zurücksetzen
# Ohne Anmeldung erreichbar (PUBLIC_PREFIXES). Formular-Schutz wie bei der Anmeldung (LOGIN_CSRF_COOKIE).

def _account_form(request: Request, mode: str, token: str, row: dict, errors: dict | None = None,
                  values: dict | None = None, status: int = 200) -> HTMLResponse:
    csrf = secrets.token_urlsafe(24)
    user = db.q1("SELECT username FROM users WHERE id = ?", (row["user_id"],)) if mode == "reset" else None
    resp = render(request, "account.html", status_code=status, csrf=csrf, mode=mode, token=token, row=row,
                  username=user["username"] if user else None, errors=errors or {}, values=values or {})
    set_cookie(resp, request, auth.LOGIN_CSRF_COOKIE, csrf, max_age=3600)
    return resp


def _link_invalid(request: Request) -> HTMLResponse:
    return render(request, "message.html", status_code=410, heading="Link nicht mehr gültig",
                  text="Dieser Link ist abgelaufen oder wurde schon benutzt. Bitte um einen neuen.",
                  link="/login", link_text="Zur Anmeldung")


@app.get("/einladung/{token}", response_class=HTMLResponse)
async def invite_page(request: Request, token: str):
    row = auth.load_token(token, "invite")
    return _account_form(request, "invite", token, row) if row else _link_invalid(request)


@app.post("/einladung/{token}", response_class=HTMLResponse)
async def accept_invite(request: Request, token: str):
    row = auth.load_token(token, "invite")
    if not row:
        return _link_invalid(request)
    form = await request.form()
    username = str(form.get("username", "")).strip()[:40]
    password, repeat = str(form.get("password", "")), str(form.get("repeat", ""))
    if not auth.csrf_ok(form.get("csrf"), request.cookies.get(auth.LOGIN_CSRF_COOKIE)):
        return _account_form(request, "invite", token, row, {"form": "Das Formular war abgelaufen. Bitte noch einmal."},
                             {"username": username}, 400)
    errors = {k: v for k, v in (("username", auth.check_username(username)),
                                ("email", auth.check_email(row["email"])),
                                ("password", auth.check_password(password, repeat))) if v}
    if errors:
        return _account_form(request, "invite", token, row, errors, {"username": username}, 422)
    user_id = auth.create_user(username, row["email"], password)
    auth.use_token(row, user_id)
    log.info("Konto '%s' über eine Einladung angelegt.", username)
    return _complete_login(request, user_id, False, "/settings?ok=welcome#ebay")


@app.get("/zuruecksetzen/{token}", response_class=HTMLResponse)
async def reset_page(request: Request, token: str):
    row = auth.load_token(token, "reset")
    return _account_form(request, "reset", token, row) if row else _link_invalid(request)


@app.post("/zuruecksetzen/{token}", response_class=HTMLResponse)
async def reset_submit(request: Request, token: str):
    row = auth.load_token(token, "reset")
    if not row:
        return _link_invalid(request)
    form = await request.form()
    password, repeat = str(form.get("password", "")), str(form.get("repeat", ""))
    if not auth.csrf_ok(form.get("csrf"), request.cookies.get(auth.LOGIN_CSRF_COOKIE)):
        return _account_form(request, "reset", token, row, {"form": "Das Formular war abgelaufen. Bitte noch einmal."},
                             status=400)
    error = auth.check_password(password, repeat)
    if error:
        return _account_form(request, "reset", token, row, {"password": error}, status=422)
    auth.reset_password(row["user_id"], password)
    auth.use_token(row)
    resp = RedirectResponse("/login?ok=reset", 303)
    resp.delete_cookie(auth.LOGIN_CSRF_COOKIE, path="/")
    return resp
