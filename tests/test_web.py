import pytest
from fastapi.testclient import TestClient

from bankatakip.web import app as web_app
from bankatakip.web import auth

from .conftest import SAMPLE_LINES, make_pdf

H = {"X-Requested-With": "bankatakip"}


@pytest.fixture
def client(config, monkeypatch):
    for var in ["VERCEL", "AUTH_DISABLED", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                "SESSION_SECRET", "ALLOWED_EMAILS", "CRON_SECRET", "PANEL_PASSWORD"]:
        monkeypatch.delenv(var, raising=False)
    web_app.app.dependency_overrides[web_app.get_config] = lambda: config
    yield TestClient(web_app.app)
    web_app.app.dependency_overrides.clear()


def _login_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    monkeypatch.setenv("ALLOWED_EMAILS", "ben@gmail.com")


def test_index_is_public(client):
    r = client.get("/")
    assert r.status_code == 200 and "BankaTakip" in r.text


def test_api_fails_closed_without_auth_settings(client):
    assert client.get("/api/me").status_code == 503


def test_auth_disabled_ignored_on_vercel(client, monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "1")
    assert client.get("/api/me").status_code == 200
    monkeypatch.setenv("VERCEL", "1")
    assert client.get("/api/me").status_code == 503


def test_session_cookie(client, monkeypatch):
    _login_env(monkeypatch)
    assert client.get("/api/me").status_code == 401

    client.cookies.set(auth.SESSION_COOKIE, auth.sign_session("ben@gmail.com"))
    r = client.get("/api/me")
    assert r.status_code == 200 and r.json()["email"] == "ben@gmail.com"

    # imza bozulursa veya süre dolarsa geçersiz
    client.cookies.set(auth.SESSION_COOKIE, auth.sign_session("ben@gmail.com") + "x")
    assert client.get("/api/me").status_code == 401
    client.cookies.set(auth.SESSION_COOKIE, auth.sign_session("ben@gmail.com", now=0))
    assert client.get("/api/me").status_code == 401
    # izin listesinde olmayan adres
    client.cookies.set(auth.SESSION_COOKIE, auth.sign_session("baska@gmail.com"))
    assert client.get("/api/me").status_code == 401


def test_login_redirect_sets_state(client, monkeypatch):
    _login_env(monkeypatch)
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith(auth.GOOGLE_AUTH_URL)
    assert auth.STATE_COOKIE in r.cookies
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fauth%2Fcallback" in r.headers["location"]


def test_callback_rejects_bad_state(client, monkeypatch):
    _login_env(monkeypatch)
    client.cookies.set(auth.STATE_COOKIE, "dogru")
    assert client.get("/auth/callback?code=c&state=yanlis", follow_redirects=False).status_code == 400


def test_upload_and_browse(client, monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "1")
    pdf = make_pdf(SAMPLE_LINES)
    files = {"file": ("ekstre.pdf", pdf, "application/pdf")}

    # CSRF başlığı olmadan reddedilir
    assert client.post("/api/import", data={"bank": "Garanti BBVA"}, files=files).status_code == 403

    r = client.post("/api/import", data={"bank": "Garanti BBVA"}, files=files, headers=H)
    assert r.status_code == 200 and r.json()["transactions"] == 5
    r = client.post("/api/import", data={"bank": "Garanti BBVA"}, files=files, headers=H)
    assert r.json()["duplicate"] is True

    bad = {"file": ("x.pdf", b"merhaba", "application/pdf")}
    assert client.post("/api/import", data={"bank": "Garanti BBVA"}, files=bad, headers=H).status_code == 400
    assert client.post("/api/import", data={"bank": "Yok"}, files=files, headers=H).status_code == 400

    [st] = client.get("/api/statements").json()
    assert st["period_debt"] == 4321.5 and st["tx_count"] == 5

    txs = client.get("/api/transactions", params={"q": "netflix"}).json()
    assert len(txs) == 1 and txs[0]["category"] == "Abonelik"
    r = client.patch(f"/api/transactions/{txs[0]['id']}", json={"category": "Eğlence"}, headers=H)
    assert r.status_code == 200
    assert client.get("/api/transactions", params={"category": "Eğlence"}).json()[0]["id"] == txs[0]["id"]

    o = client.get("/api/overview").json()
    assert o["statement_count"] == 1
    assert o["monthly"][0]["month"] == "2026-08"

    assert client.delete(f"/api/statements/{st['id']}", headers=H).status_code == 200
    assert client.get("/api/statements").json() == []


def test_cron_requires_secret(client, monkeypatch):
    assert client.get("/api/cron/sync").status_code == 401
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    assert client.get("/api/cron/sync", headers={"Authorization": "Bearer yanlis"}).status_code == 401
    r = client.get("/api/cron/sync", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert r.json()["errors"] == ["Tanımlı mail hesabı yok (GMAIL_EMAIL / ICLOUD_EMAIL)."]
    assert r.json()["reminders_sent"] == []


def test_auth_info(client, monkeypatch):
    info = client.get("/api/auth-info").json()
    assert info["google"] is False and info["password"] is False and len(info["missing"]) == 2
    _login_env(monkeypatch)
    assert client.get("/api/auth-info").json() == {
        "google": True, "password": False, "disabled": False, "missing": []}


def test_password_login(client, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    monkeypatch.setenv("PANEL_PASSWORD", "kisa")
    assert client.get("/api/auth-info").json()["password"] is False  # 12 karakterden kısa

    monkeypatch.setenv("PANEL_PASSWORD", "cok-guclu-bir-sifre-123")
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    assert client.get("/api/me").status_code == 401
    # CSRF başlığı zorunlu
    assert client.post("/auth/password", json={"password": "cok-guclu-bir-sifre-123"}).status_code == 403
    assert client.post("/auth/password", json={"password": "yanlis"}, headers=H).status_code == 401
    r = client.post("/auth/password", json={"password": "cok-guclu-bir-sifre-123"}, headers=H)
    assert r.status_code == 200
    me = client.get("/api/me").json()
    assert me["email"] == "şifre ile giriş"
    assert "Mail hesabı tanımlı değil: GMAIL_EMAIL / ICLOUD_EMAIL ekleyin." in me["warnings"]

    # şifre değişince eski oturum geçersiz olur
    monkeypatch.setenv("PANEL_PASSWORD", "yeni-cok-guclu-sifre-456")
    assert client.get("/api/me").status_code == 401


def test_google_session_rejected_when_google_disabled(client, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    monkeypatch.setenv("PANEL_PASSWORD", "cok-guclu-bir-sifre-123")
    monkeypatch.setenv("ALLOWED_EMAILS", "ben@gmail.com")
    client.cookies.set(auth.SESSION_COOKIE, auth.sign_session("ben@gmail.com"))
    assert client.get("/api/me").status_code == 401
