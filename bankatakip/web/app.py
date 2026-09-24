"""Web paneli ve API (FastAPI). Vercel'de api/index.py üzerinden çalışır.

Yerelde:  AUTH_DISABLED=1 uvicorn bankatakip.web.app:app --reload
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from ..config import Config, ConfigError, load_config
from ..parsers import PdfPasswordError
from ..storage import Storage, is_postgres_url
from ..reminders import send_due_reminders
from ..sync import import_pdf, sync
from . import auth

STATIC_DIR = Path(__file__).with_name("static")
MAX_UPLOAD = 4 * 1024 * 1024  # Vercel istek gövdesi sınırı ~4.5 MB

app = FastAPI(title="BankaTakip", docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(ConfigError)
async def _config_error(request: Request, exc: ConfigError):
    return JSONResponse({"detail": str(exc)}, status_code=500)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def get_config() -> Config:
    return load_config(os.environ.get("BANKATAKIP_CONFIG_FILE", "config.yaml"))


def get_storage(config: Config = Depends(get_config)) -> Iterator[Storage]:
    storage = Storage(config.database)
    try:
        yield storage
    finally:
        storage.close()


User = Depends(auth.current_user)
SameOrigin = Depends(auth.require_same_origin)


def _dec(value) -> float | None:
    return float(Decimal(value)) if value is not None else None


def _statement_json(row: dict) -> dict:
    return {**row, "period_debt": _dec(row["period_debt"]), "minimum_payment": _dec(row["minimum_payment"])}


def _tx_json(row: dict) -> dict:
    return {**row, "amount": _dec(row["amount"])}


# --- sayfa ve giriş ---

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/auth/login", include_in_schema=False)
def login(request: Request):
    return auth.login_redirect(request)


@app.get("/auth/callback", include_in_schema=False)
async def callback(request: Request):
    return await auth.handle_callback(request)


class PasswordLogin(BaseModel):
    password: str


@app.post("/auth/password", include_in_schema=False, dependencies=[SameOrigin])
def password_login(request: Request, body: PasswordLogin):
    return auth.password_login(request, body.password)


@app.get("/api/auth-info")
def auth_info():
    return auth.auth_info()


@app.get("/auth/logout", include_in_schema=False)
def logout():
    return auth.logout()


# --- API ---

def setup_warnings(config: Config) -> list[str]:
    """Panelde gösterilecek kurulum eksikleri (değerler değil, sadece adlar)."""
    warnings = []
    if auth.on_vercel() and not is_postgres_url(config.database):
        warnings.append("DATABASE_URL tanımlı değil: veriler kalıcı olarak saklanamaz.")
    if not config.accounts:
        warnings.append("Mail hesabı tanımlı değil: GMAIL_EMAIL / ICLOUD_EMAIL ekleyin.")
    for account in config.accounts:
        if not os.environ.get(account.password_env):
            warnings.append(f"{account.name} için {account.password_env} tanımlı değil.")
    if auth.on_vercel() and not os.environ.get("CRON_SECRET"):
        warnings.append("CRON_SECRET tanımlı değil: otomatik günlük tarama çalışmaz.")
    return warnings


@app.get("/api/me")
def me(user: str = User, config: Config = Depends(get_config)):
    return {
        "email": user,
        "warnings": setup_warnings(config),
        "accounts": [{"name": a.name, "email": a.email} for a in config.accounts],
        "banks": [
            {"name": b.name, "senders": b.senders, "has_pdf_password": bool(b.pdf_password)}
            for b in config.banks
        ],
        "categories": list(config.categories) + ["Diğer"],
    }


@app.get("/api/overview")
def overview(user: str = User, storage: Storage = Depends(get_storage)):
    today = date.today()
    this_month = today.strftime("%Y-%m")
    last_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    monthly: dict[str, dict[str, float]] = {}
    for month, category, total in storage.monthly_summary():
        monthly.setdefault(month, {})[category] = float(total)
    months = sorted(monthly)[-12:]

    upcoming = [
        _statement_json(s) for s in storage.list_statements()
        if s["due_date"] and s["due_date"] >= today.isoformat()
    ]
    upcoming.sort(key=lambda s: s["due_date"])

    return {
        "this_month": sum(monthly.get(this_month, {}).values()),
        "last_month": sum(monthly.get(last_month, {}).values()),
        "this_month_by_category": monthly.get(this_month, {}),
        "monthly": [{"month": m, "categories": monthly[m]} for m in months],
        "upcoming": upcoming,
        "statement_count": len(storage.list_statements()),
        "last_run": storage.get_meta("last_run"),
    }


@app.get("/api/statements")
def statements(user: str = User, storage: Storage = Depends(get_storage)):
    return [_statement_json(s) for s in storage.list_statements()]


@app.delete("/api/statements/{statement_id}", dependencies=[SameOrigin])
def delete_statement(statement_id: int, user: str = User, storage: Storage = Depends(get_storage)):
    if not storage.delete_statement(statement_id):
        raise HTTPException(404, "Ekstre bulunamadı.")
    return {"ok": True}


@app.get("/api/transactions")
def transactions(
    since: date | None = None,
    until: date | None = None,
    bank: str | None = None,
    category: str | None = None,
    q: str | None = None,
    statement_id: int | None = None,
    user: str = User,
    storage: Storage = Depends(get_storage),
):
    rows = storage.list_transactions(since=since, until=until, bank=bank or None,
                                     category=category or None, search=q or None,
                                     statement_id=statement_id)
    return [_tx_json(r) for r in rows[:1000]]


class CategoryUpdate(BaseModel):
    category: str | None


@app.patch("/api/transactions/{tx_id}", dependencies=[SameOrigin])
def update_category(tx_id: int, body: CategoryUpdate, user: str = User,
                    storage: Storage = Depends(get_storage)):
    category = None if body.category in (None, "", "Diğer") else body.category
    if not storage.update_transaction_category(tx_id, category):
        raise HTTPException(404, "İşlem bulunamadı.")
    return {"ok": True}


def _time_budget() -> float:
    return float(os.environ.get("SYNC_TIME_BUDGET", "45"))


@app.post("/api/sync", dependencies=[SameOrigin])
def run_sync(user: str = User, config: Config = Depends(get_config),
             storage: Storage = Depends(get_storage)):
    return sync(config, storage, time_budget=_time_budget()).as_dict()


@app.get("/api/cron/sync", include_in_schema=False)
def cron_sync(request: Request, config: Config = Depends(get_config),
              storage: Storage = Depends(get_storage)):
    auth.check_cron(request)
    report = sync(config, storage, time_budget=_time_budget()).as_dict()
    report["reminders_sent"] = send_due_reminders(config, storage)
    return report


@app.post("/api/import", dependencies=[SameOrigin])
async def upload(
    file: UploadFile = File(...),
    bank: str = Form(...),
    user: str = User,
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
):
    bank_cfg = config.bank_by_name(bank)
    if bank_cfg is None:
        raise HTTPException(400, f"'{bank}' bankası tanımlı değil.")
    content = await file.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, "Dosya 4 MB'tan büyük olamaz.")
    if not content.startswith(b"%PDF"):
        raise HTTPException(400, "Sadece PDF dosyası yüklenebilir.")
    try:
        statement_id, count = import_pdf(content, bank_cfg, config, storage,
                                         source="manuel", filename=file.filename or "ekstre.pdf")
    except PdfPasswordError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(400, f"PDF okunamadı: {exc}")
    if statement_id is None:
        return {"ok": True, "duplicate": True, "transactions": 0}
    return {"ok": True, "duplicate": False, "statement_id": statement_id, "transactions": count}
