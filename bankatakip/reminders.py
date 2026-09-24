"""Son ödeme tarihi yaklaşan ekstreler için hatırlatma maili.

Günlük cron taramasından sonra çalışır. Mail, tanımlı ilk Gmail/iCloud hesabının
uygulama şifresiyle SMTP üzerinden gönderilir; ek bir ayar gerekmez.

Ortam değişkenleri (isteğe bağlı):
  REMINDER_DAYS   son ödeme tarihinden kaç gün önce hatırlatılsın (varsayılan 3, 0 = kapalı)
  REMINDER_EMAIL  hatırlatmanın gideceği adres (varsayılan: gönderen hesabın kendisi)
"""

from __future__ import annotations

import logging
import os
import smtplib
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from email.message import EmailMessage

from .config import Config, MailAccount
from .storage import Storage

log = logging.getLogger(__name__)

SMTP_HOSTS = {
    "gmail": ("smtp.gmail.com", 465, "ssl"),
    "icloud": ("smtp.mail.me.com", 587, "starttls"),
}

Sender = Callable[[MailAccount, EmailMessage], None]


def _tl(value: str | None) -> str:
    if value is None:
        return "-"
    text = f"{Decimal(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} TL"


def smtp_send(account: MailAccount, message: EmailMessage) -> None:
    host, port, mode = SMTP_HOSTS[account.provider]
    if mode == "ssl":
        with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
            smtp.login(account.email, account.password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(account.email, account.password)
            smtp.send_message(message)


def _sender_account(config: Config) -> MailAccount | None:
    for account in config.accounts:
        if account.provider in SMTP_HOSTS and os.environ.get(account.password_env):
            return account
    return None


def build_message(statement: dict, days_left: int, sender: str, recipient: str) -> EmailMessage:
    when = "bugün" if days_left == 0 else f"{days_left} gün sonra"
    msg = EmailMessage()
    msg["Subject"] = f"{statement['bank']} kart ödemesi {when}: {_tl(statement['period_debt'])}"
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(
        f"{statement['bank']} ekstrenizin son ödeme tarihi {statement['due_date']} ({when}).\n\n"
        f"Dönem borcu:  {_tl(statement['period_debt'])}\n"
        f"Asgari ödeme: {_tl(statement['minimum_payment'])}\n\n"
        "Bu mail BankaTakip tarafından otomatik gönderildi."
    )
    return msg


def send_due_reminders(config: Config, storage: Storage, today: date | None = None,
                       send: Sender = smtp_send) -> list[str]:
    """Hatırlatılması gereken ekstreler için mail gönderir; gönderilenlerin açıklamasını döndürür.

    Her ekstre için yalnızca bir kez mail atılır (meta tablosunda işaretlenir).
    """
    days = int(os.environ.get("REMINDER_DAYS", "3"))
    if days <= 0:
        return []
    account = _sender_account(config)
    if account is None:
        return []
    recipient = os.environ.get("REMINDER_EMAIL") or account.email
    today = today or date.today()
    last_day = today + timedelta(days=days)

    sent = []
    for statement in storage.list_statements():
        if not statement["due_date"]:
            continue
        due = date.fromisoformat(statement["due_date"])
        if not today <= due <= last_day:
            continue
        key = f"reminded:{statement['id']}"
        if storage.get_meta(key):
            continue
        message = build_message(statement, (due - today).days, account.email, recipient)
        try:
            send(account, message)
        except Exception as exc:
            log.warning("Hatırlatma gönderilemedi: %s", exc)
            continue
        storage.set_meta(key, today.isoformat())
        sent.append(message["Subject"])
    return sent
