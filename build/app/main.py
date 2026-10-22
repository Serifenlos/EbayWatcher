"""Weboberfläche von Ebay Watcher."""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, db, notify, util
from .watcher import Watcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ebay-watcher")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
watcher = Watcher()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    auth.ensure_admin()
    watcher.start()
    log.info("Ebay Watcher läuft.")
    yield
    await watcher.stop()


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


templates.env.globals.update(
    eur=util.eur, countdown=util.countdown, local_time=util.local_time, ago=util.ago,
    gauge=util.gauge, asset=asset, app_name=config.APP_NAME,
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
    "ebay": "Verbindung zu eBay geprüft.",
    "logout": "Du bist abgemeldet.",
}

CSP = ("default-src 'self'; img-src 'self' data: https:; style-src 'self'; script-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
PUBLIC_PREFIXES = ("/login", "/static/", "/healthz", "/favicon")


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


def back(path: str, key: str | None = None, err: bool = False, anchor: str = "") -> RedirectResponse:
    url = path + (f"?{'err' if err else 'ok'}={key}" if key else "") + (f"#{anchor}" if anchor else "")
    return RedirectResponse(url, 303)


# ---------------------------------------------------------------- Middleware

@app.middleware("http")
async def guard(request: Request, call_next):
    request.state.session = None
    path = request.url.path
    if not path.startswith(PUBLIC_PREFIXES):
        sess = auth.load_session(request.cookies.get(auth.SESSION_COOKIE))
        if not sess:
            target = "/login"
            if request.method == "GET" and path != "/":
                target += "?next=" + quote(path)
            return _with_headers(RedirectResponse(target, 303))
        request.state.session = sess
    return _with_headers(await call_next(request))


@app.exception_handler(CsrfFailed)
async def csrf_failed(request: Request, _exc: CsrfFailed):
    return render(request, "message.html", status_code=400, heading="Formular abgelaufen",
                  text="Lade die Seite neu und versuche es noch einmal.", link="/", link_text="Zur Übersicht")


@app.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")


# ---------------------------------------------------------------- Anmeldung

def _login_form(request: Request, next_: str = "/", error: str | None = None,
                username: str = "", status: int = 200) -> HTMLResponse:
    token = secrets.token_urlsafe(24)
    resp = render(request, "login.html", status_code=status, csrf=token, next=safe_next(next_),
                  error=error, username=username)
    set_cookie(resp, request, auth.LOGIN_CSRF_COOKIE, token, max_age=3600)
    return resp


def _complete_login(request: Request, user_id: int, remember: bool, next_: str) -> RedirectResponse:
    token = auth.create_session(user_id, remember, request.headers.get("user-agent", ""))
    resp = RedirectResponse(next_, 303)
    set_cookie(resp, request, auth.SESSION_COOKIE, token, max_age=auth.REMEMBER_SECONDS if remember else None)
    resp.delete_cookie(auth.LOGIN_CSRF_COOKIE, path="/")
    resp.delete_cookie(auth.PENDING_COOKIE, path="/")
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if auth.load_session(request.cookies.get(auth.SESSION_COOKIE)):
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

    user = db.q1("SELECT * FROM users WHERE username = ?", (username,))
    if not auth.verify_password(user, password):
        auth.throttle.fail(ip, username)
        return _login_form(request, next_, "Benutzername oder Passwort stimmt nicht.", username, 401)
    auth.throttle.reset(ip)

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
    auth.destroy_session(request.cookies.get(auth.SESSION_COOKIE))
    resp = RedirectResponse("/login?ok=logout", 303)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


# ---------------------------------------------------------------- Übersicht

def _notices() -> list[dict]:
    s = db.get_settings()
    out = []
    if not watcher.ebay.configured:
        out.append({"text": "Die eBay-Zugangsdaten fehlen. Trage EBAY_CLIENT_ID und EBAY_CLIENT_SECRET "
                            "im Portainer-Stack ein und starte den Container neu."})
    if not notify.configured(s):
        out.append({"text": "Benachrichtigungen sind noch nicht eingerichtet.",
                    "link": "/settings#benachrichtigungen", "action": "Einrichten"})
    elif watcher.last_ntfy_error:
        out.append({"text": f"Die letzte Benachrichtigung ist nicht angekommen. {watcher.last_ntfy_error}",
                    "link": "/settings#benachrichtigungen", "action": "Prüfen"})
    if not s["plz"]:
        out.append({"text": "Ohne Postleitzahl findet Ebay Watcher keine Abholangebote in deiner Nähe.",
                    "link": "/settings#standort", "action": "PLZ eintragen"})
    return out


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    now = time.time()
    searches = db.q("SELECT * FROM searches ORDER BY active DESC, created_at DESC")
    overviews = [
        util.search_overview(s, db.q("SELECT * FROM items WHERE search_id = ?", (s["id"],)), now)
        for s in searches
    ]
    return render(request, "dashboard.html", active="searches", searches=overviews,
                  notices=_notices(), last_tick=watcher.last_tick, now=now)


# ---------------------------------------------------------------- Gesuche

def _form_defaults(settings: dict) -> dict:
    return {
        "name": "", "query": "", "exclude": "", "format": "auction", "location": "DE",
        "max_price_raw": "", "incl_shipping": 1, "free_shipping_only": 0, "condition": "any",
        "lead_raw": str(db.setting_int(settings, "lead_minutes", 10)), "pickup_mode": "radius", "active": 1,
    }


def _values_from_search(s: dict) -> dict:
    price = s["max_price"]
    raw = f"{price:.0f}" if float(price).is_integer() else f"{price:.2f}".replace(".", ",")
    return {**s, "max_price_raw": raw, "lead_raw": str(s["lead_minutes"])}


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
    return v, errors


SEARCH_COLUMNS = ["name", "query", "exclude", "format", "location", "max_price", "incl_shipping",
                  "free_shipping_only", "condition", "lead_minutes", "pickup_mode", "active"]


def _render_form(request: Request, values: dict, errors: dict, mode: str, sid: int | None = None,
                 status: int = 200) -> HTMLResponse:
    settings = db.get_settings()
    return render(request, "search_form.html", status_code=status, active="searches", values=values,
                  errors=errors, mode=mode, sid=sid, has_plz=bool(settings["plz"]),
                  radius=db.setting_int(settings, "radius_km", 50))


@app.get("/searches/new", response_class=HTMLResponse)
async def new_search_page(request: Request):
    return _render_form(request, _form_defaults(db.get_settings()), {}, "new")


@app.post("/searches/new")
async def create_search(request: Request):
    form = await checked_form(request)
    values, errors = _parse_search_form(form)
    if errors:
        return _render_form(request, values, errors, "new", status=422)
    sid = db.ex(
        f"INSERT INTO searches({', '.join(SEARCH_COLUMNS)}, created_at) "
        f"VALUES ({', '.join('?' * len(SEARCH_COLUMNS))}, ?)",
        [*[values[c] for c in SEARCH_COLUMNS], time.time()],
    )
    watcher.poke()
    return back(f"/searches/{sid}", "created")


@app.get("/searches/{sid}", response_class=HTMLResponse)
async def search_detail(request: Request, sid: int):
    s = db.q1("SELECT * FROM searches WHERE id = ?", (sid,))
    if not s:
        return not_found(request)
    now = time.time()
    overview = util.search_overview(s, db.q("SELECT * FROM items WHERE search_id = ?", (sid,)), now)
    return render(request, "search_detail.html", active="searches", s=overview, now=now)


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
    s = {**db.get_settings(), **(values or {})}
    return render(request, "settings.html", status_code=status, active="settings", s=s, user=user,
                  errors=errors or {}, sessions=sessions, ebay_configured=watcher.ebay.configured,
                  ebay_check=watcher.last_ebay_check, ntfy_error=watcher.last_ntfy_error,
                  api_calls=db.api_calls_today(), has_token=bool(s.get("ntfy_token")))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return _settings_page(request)


@app.post("/settings/general")
async def save_general(request: Request):
    form = await checked_form(request)
    values = {k: str(form.get(k, "")).strip() for k in ("plz", "radius_km", "lead_minutes", "interval_minutes", "app_url")}
    errors = {}
    if values["plz"] and not re.fullmatch(r"\d{5}", values["plz"]):
        errors["plz"] = "Eine deutsche Postleitzahl hat fünf Ziffern."
    for key, lo, hi in (("radius_km", 1, 500), ("lead_minutes", 1, 180), ("interval_minutes", 5, 240)):
        if not values[key].isdigit() or not lo <= int(values[key]) <= hi:
            errors[key] = f"Wähle eine Zahl von {lo} bis {hi}."
    if values["app_url"] and not values["app_url"].startswith(("http://", "https://")):
        errors["app_url"] = "Die Adresse beginnt mit http:// oder https://."
    if errors:
        return _settings_page(request, errors, values, 422)
    db.set_settings(values)
    watcher.poke()
    return back("/settings", "saved", anchor="standort")


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
    db.set_settings(values)
    watcher.last_ntfy_error = None
    return back("/settings", "saved", anchor="benachrichtigungen")


@app.post("/settings/ntfy/test")
async def test_ntfy(request: Request):
    await checked_form(request)
    try:
        await notify.send(db.get_settings(), title="Ebay Watcher ist verbunden",
                          message="So sehen deine Benachrichtigungen aus. Alles bereit.",
                          tags=["white_check_mark"], priority=3)
        watcher.last_ntfy_error = None
        return back("/settings", "ntfy_sent", anchor="benachrichtigungen")
    except notify.NtfyError as e:
        watcher.last_ntfy_error = str(e)
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


@app.post("/settings/ebay/test")
async def test_ebay(request: Request):
    await checked_form(request)
    await watcher.test_ebay()
    return back("/settings", "ebay", anchor="ebay")
