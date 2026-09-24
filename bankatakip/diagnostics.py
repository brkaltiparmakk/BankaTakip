"""Mail hesaplarının bağlantı testi: adres/şifre biçimi, IMAP (okuma) ve SMTP (gönderme) girişi."""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from .config import EMAIL_RE, Config, ConfigError, MailAccount
from .mail import MailClient
from .reminders import SMTP_HOSTS, smtp_login

ICLOUD_APP_PASSWORD = re.compile(r"^[a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4}$")

# Sunucu hata mesajı → kullanıcıya ne yapması gerektiği
HINTS = [
    ("application-specific password required",
     "Normal şifre kabul edilmiyor: hesabın için bir uygulama şifresi oluşturup onu girin."),
    ("invalid credentials",
     "Adres veya şifre kabul edilmedi. Uygulama şifresinin bu adrese ait olduğundan ve iki adımlı "
     "doğrulamanın açık olduğundan emin olun; gerekirse yeni uygulama şifresi oluşturun."),
    ("username and password not accepted",
     "Adres veya şifre kabul edilmedi: yeni bir uygulama şifresi oluşturup deneyin."),
    ("authenticationfailed", "Adres veya uygulama şifresi hatalı."),
    ("authentication failed", "Adres veya uygulama şifresi hatalı."),
    ("too many arguments", "Şifrede boşluk var: boşluksuz yazın."),
    ("timed out", "Sunucuya ulaşılamadı (zaman aşımı); biraz sonra tekrar deneyin."),
]


def hint_for(error: str) -> str | None:
    low = error.lower()
    return next((hint for key, hint in HINTS if key in low), None)


def format_warnings(account: MailAccount) -> list[str]:
    warnings = list(account.notes)
    try:
        password = account.password
    except ConfigError as exc:
        return warnings + [str(exc)]
    if not EMAIL_RE.fullmatch(account.email):
        warnings.append(f"Adres alanı geçerli bir e-posta değil: '{account.email[:3]}…'")
    if EMAIL_RE.search(password):
        warnings.append("Şifre alanında bir e-posta adresi var: adres ve şifre karışmış olabilir.")
    if account.provider == "gmail" and not (len(password) == 16 and password.isalpha()):
        warnings.append(f"Gmail uygulama şifresi boşluksuz 16 harftir; girilen değer {len(password)} "
                        "karakter. Normal Google şifresi çalışmaz.")
    if account.provider == "icloud" and not ICLOUD_APP_PASSWORD.match(password.lower()):
        warnings.append("iCloud uygulama şifresi 'abcd-efgh-ijkl-mnop' biçimindedir.")
    return warnings


def _try(fn: Callable[[], object]) -> dict:
    try:
        detail = fn()
        return {"ok": True, "message": detail or "Giriş başarılı"}
    except Exception as exc:
        text = str(exc)
        return {"ok": False, "message": text[:300], "hint": hint_for(text)}


TIMEOUT = 10  # Vercel isteği 60 sn ile sınırlı; tüm denemeler paralel çalışır


def _imap_check(account: MailAccount) -> str:
    client = MailClient(account, timeout=TIMEOUT)
    client.connect()
    try:
        folders = client.default_folders()
    finally:
        client.close()
    return "Giriş başarılı · taranacak klasörler: " + ", ".join(folders)


def check_account(account: MailAccount, imap: Callable[[MailAccount], object] = _imap_check,
                  smtp: Callable[[MailAccount], object] = smtp_login) -> dict:
    try:
        length = len(account.password)
    except ConfigError:
        length = 0
    missing = {"ok": False, "message": "Şifre tanımlı değil"}
    with ThreadPoolExecutor(max_workers=2) as pool:  # IMAP ve SMTP aynı anda denenir
        imap_f = pool.submit(_try, lambda: imap(account)) if length else None
        smtp_f = pool.submit(_try, lambda: smtp(account)) if length and account.provider in SMTP_HOSTS else None
        imap_r = imap_f.result() if imap_f else missing
        smtp_r = smtp_f.result() if smtp_f else (missing if account.provider in SMTP_HOSTS else None)
    return {
        "name": account.name,
        "provider": account.provider,
        "email": account.email,
        "password_env": account.password_env,
        "password_length": length,
        "warnings": format_warnings(account),
        "imap": imap_r,
        "smtp": smtp_r,
    }


def check_accounts(config: Config, **kwargs) -> list[dict]:
    if not config.accounts:
        return []
    with ThreadPoolExecutor(max_workers=len(config.accounts)) as pool:
        return list(pool.map(lambda a: check_account(a, **kwargs), config.accounts))
