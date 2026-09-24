"""Google ile giriş (OAuth 2.0) ve imzalı oturum çerezi.

İki giriş yöntemi var, en az biri ayarlanmalı:
  Google:  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET (Google Cloud Console > OAuth istemcisi)
           ve ALLOWED_EMAILS (panele girebilecek adresler, virgülle ayrılmış)
  Şifre:   PANEL_PASSWORD (en az 12 karakter)
Her durumda:
  SESSION_SECRET  çerezleri imzalamak için en az 32 karakterlik rastgele bir metin
İsteğe bağlı:
  APP_URL        ör. https://bankatakip.vercel.app (verilmezse istekten çıkarılır)
  AUTH_DISABLED  =1 ise yerelde girişsiz çalışır; Vercel'de her zaman yok sayılır.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

SESSION_COOKIE = "bt_session"
STATE_COOKIE = "bt_oauth_state"
SESSION_TTL = 7 * 24 * 3600

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def on_vercel() -> bool:
    return bool(os.environ.get("VERCEL"))


def auth_disabled() -> bool:
    return os.environ.get("AUTH_DISABLED") == "1" and not on_vercel()


def allowed_emails() -> set[str]:
    raw = os.environ.get("ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


MIN_PANEL_PASSWORD = 12


def google_enabled() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET")
                and allowed_emails())


def password_enabled() -> bool:
    return len(os.environ.get("PANEL_PASSWORD", "")) >= MIN_PANEL_PASSWORD


def missing_settings() -> list[str]:
    """Girişin çalışması için eksik olan ayarlar (boşsa giriş hazır)."""
    missing = []
    if len(os.environ.get("SESSION_SECRET", "")) < 32:
        missing.append("SESSION_SECRET (en az 32 karakter)")
    if not google_enabled() and not password_enabled():
        panel_password = os.environ.get("PANEL_PASSWORD", "")
        if panel_password:
            missing.append(f"PANEL_PASSWORD çok kısa: {len(panel_password)} karakter, "
                           f"en az {MIN_PANEL_PASSWORD} olmalı")
        elif os.environ.get("GOOGLE_CLIENT_ID") and not allowed_emails():
            missing.append("ALLOWED_EMAILS")
        else:
            missing.append(f"GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + ALLOWED_EMAILS "
                           f"veya PANEL_PASSWORD (en az {MIN_PANEL_PASSWORD} karakter)")
    return missing


def _password_fingerprint() -> str:
    # Şifre değişince eski şifreyle açılmış oturumlar geçersiz olsun
    pw = os.environ.get("PANEL_PASSWORD", "").encode()
    return _b64(hmac.new(_secret(), b"pw:" + pw, hashlib.sha256).digest())[:16]


def _secret() -> bytes:
    secret = os.environ.get("SESSION_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(503, "SESSION_SECRET en az 32 karakter olmalı.")
    return secret.encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_session(email: str, now: float | None = None, method: str = "google") -> str:
    data = {"email": email, "method": method,
            "exp": int((time.time() if now is None else now) + SESSION_TTL)}
    if method == "password":
        data["pw"] = _password_fingerprint()
    payload = _b64(json.dumps(data).encode())
    sig = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_session(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    expected = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if data.get("exp", 0) < time.time():
        return None
    if data.get("method") == "password":
        if not password_enabled() or not hmac.compare_digest(str(data.get("pw", "")), _password_fingerprint()):
            return None
        return "şifre ile giriş"
    email = str(data.get("email", "")).lower()
    # İzin listesinden çıkarılan biri eski çereziyle girmeye devam edemesin
    return email if google_enabled() and email in allowed_emails() else None


def current_user(request: Request) -> str:
    """API uç noktaları için: giriş yapılmamışsa 401 döner."""
    if auth_disabled():
        return "yerel"
    if missing_settings():
        raise HTTPException(503, "Giriş ayarlanmamış: " + ", ".join(missing_settings()))
    email = read_session(request.cookies.get(SESSION_COOKIE))
    if not email:
        raise HTTPException(401, "Giriş yapmanız gerekiyor.")
    return email


def require_same_origin(request: Request) -> None:
    """Veri değiştiren isteklerde CSRF koruması: sadece panelin kendi fetch çağrıları geçer."""
    if request.headers.get("x-requested-with") != "bankatakip":
        raise HTTPException(403, "Geçersiz istek.")


def _base_url(request: Request) -> str:
    if os.environ.get("APP_URL"):
        return os.environ["APP_URL"].rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


def _cookie_secure(request: Request) -> bool:
    return _base_url(request).startswith("https://")


def login_redirect(request: Request) -> RedirectResponse:
    if missing_settings() or not google_enabled():
        raise HTTPException(503, "Google ile giriş ayarlanmamış.")
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": f"{_base_url(request)}/auth/callback",
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "prompt": "select_account",
    }
    response = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)
    response.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True,
                        secure=_cookie_secure(request), samesite="lax")
    return response


async def handle_callback(request: Request) -> RedirectResponse:
    state = request.query_params.get("state")
    code = request.query_params.get("code")
    expected = request.cookies.get(STATE_COOKIE)
    if not code or not state or not expected or not hmac.compare_digest(state, expected):
        raise HTTPException(400, "Geçersiz giriş isteği, lütfen tekrar deneyin.")

    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": f"{_base_url(request)}/auth/callback",
            "grant_type": "authorization_code",
        })
        if token_resp.status_code != 200:
            raise HTTPException(400, "Google girişi doğrulanamadı.")
        access_token = token_resp.json().get("access_token")
        info_resp = await client.get(GOOGLE_USERINFO_URL,
                                     headers={"Authorization": f"Bearer {access_token}"})
    if info_resp.status_code != 200:
        raise HTTPException(400, "Google hesap bilgisi alınamadı.")
    info = info_resp.json()
    email = str(info.get("email", "")).lower()
    if not info.get("email_verified") or not google_enabled() or email not in allowed_emails():
        raise HTTPException(403, f"{email or 'Bu hesap'} panele erişim iznine sahip değil.")

    response = RedirectResponse("/", status_code=302)
    response.set_cookie(SESSION_COOKIE, sign_session(email), max_age=SESSION_TTL, httponly=True,
                        secure=_cookie_secure(request), samesite="lax")
    response.delete_cookie(STATE_COOKIE)
    return response


def password_login(request: Request, password: str) -> JSONResponse:
    if missing_settings() or not password_enabled():
        raise HTTPException(503, "Şifreyle giriş ayarlanmamış.")
    expected = os.environ["PANEL_PASSWORD"]
    if not hmac.compare_digest(password.encode(), expected.encode()):
        time.sleep(1)  # deneme-yanılmayı yavaşlat
        raise HTTPException(401, "Şifre yanlış.")
    response = JSONResponse({"ok": True})
    response.set_cookie(SESSION_COOKIE, sign_session("panel", method="password"), max_age=SESSION_TTL,
                        httponly=True, secure=_cookie_secure(request), samesite="lax")
    return response


def auth_info() -> dict:
    """Giriş ekranı için herkese açık bilgi: hangi yöntemler açık, ne eksik."""
    if auth_disabled():
        return {"google": False, "password": False, "disabled": True, "missing": []}
    return {"google": google_enabled(), "password": password_enabled(),
            "disabled": False, "missing": missing_settings()}


def logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


def check_cron(request: Request) -> None:
    """Vercel Cron istekleri 'Authorization: Bearer <CRON_SECRET>' başlığıyla gelir."""
    secret = os.environ.get("CRON_SECRET")
    header = request.headers.get("authorization", "")
    if not secret or not hmac.compare_digest(header, f"Bearer {secret}"):
        raise HTTPException(401, "Yetkisiz.")
