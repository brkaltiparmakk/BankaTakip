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


def _smtp(account: MailAccount, timeout: float = 20) -> smtplib.SMTP:
    host, port, mode = SMTP_HOSTS[account.provider]
    if mode == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=timeout)
    else:
        smtp = smtplib.SMTP(host, port, timeout=timeout)
        smtp.starttls()
    smtp.login(account.email, account.password)
    return smtp


def smtp_login(account: MailAccount) -> None:
    """Sadece giriş dener (bağlantı testi için)."""
    with _smtp(account, timeout=10):
        pass


def smtp_send(account: MailAccount, message: EmailMessage) -> None:
    with _smtp(account) as smtp:
        smtp.send_message(message)


def _sender_accounts(config: Config) -> list[MailAccount]:
    return [a for a in config.accounts
            if a.provider in SMTP_HOSTS and (a.password_value or os.environ.get(a.password_env))]


def _sender_account(config: Config) -> MailAccount | None:
    accounts = _sender_accounts(config)
    return accounts[0] if accounts else None


def recipient_for(config: Config) -> str | None:
    account = _sender_account(config)
    return os.environ.get("REMINDER_EMAIL") or (account.email if account else None)


def deliver(config: Config, build: Callable[[str, str], EmailMessage],
            send: Sender = smtp_send) -> EmailMessage:
    """Maili tanımlı hesapların sırayla ilk çalışanından gönderir (ör. Gmail şifresi yanlışsa
    iCloud'dan). build(gönderen, alıcı) mesajı üretir. Hiçbiri çalışmazsa son hata yükselir."""
    accounts = _sender_accounts(config)
    if not accounts:
        raise RuntimeError("Mail gönderecek hesap yok (GMAIL_* veya ICLOUD_* tanımlı değil).")
    recipient = recipient_for(config)
    last_error: Exception | None = None
    for account in accounts:
        message = build(account.email, recipient)
        try:
            send(account, message)
            return message
        except Exception as exc:  # sıradaki hesabı dene
            log.warning("%s hesabından mail gönderilemedi: %s", account.name, exc)
            last_error = RuntimeError(f"{account.name}: {exc}")
    raise last_error


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
    if days <= 0 or not _sender_accounts(config):
        return []
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
        try:
            message = deliver(config, lambda sender, to, s=statement, d=(due - today).days:
                              build_message(s, d, sender, to), send)
        except Exception as exc:
            log.warning("Hatırlatma gönderilemedi: %s", exc)
            continue
        storage.set_meta(key, today.isoformat())
        sent.append(message["Subject"])
    return sent


def _message(subject: str, body: str) -> Callable[[str, str], EmailMessage]:
    def build(sender: str, recipient: str) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient
        msg.set_content(body + "\n\nBu mail BankaTakip tarafından otomatik gönderildi.")
        return msg
    return build


def send_budget_alerts(config: Config, storage: Storage, today: date | None = None,
                       send: Sender = smtp_send) -> list[str]:
    """Bütçesinin %80'ini veya tamamını geçen kategoriler için (ay ve eşik başına bir kez) mail."""
    if not _sender_accounts(config):
        return []
    today = today or date.today()
    month = today.strftime("%Y-%m")
    crossed = []
    for b in storage.budget_status(month):
        level = 100 if b["ratio"] >= 1 else 80 if b["ratio"] >= Decimal("0.8") else 0
        if level and not storage.get_meta(f"budget:{month}:{b['category']}:{level}"):
            crossed.append((b, level))
    if not crossed:
        return []
    lines = [f"- {b['category']}: {_tl(b['spent'])} / {_tl(b['amount'])} (%{int(b['ratio'] * 100)})"
             + ("  BÜTÇE AŞILDI" if level == 100 else "") for b, level in crossed]
    names = ", ".join(b["category"] for b, _ in crossed)
    try:
        deliver(config, _message(f"Bütçe uyarısı: {names}",
                                 f"{today:%m.%Y} ayında bütçe sınırına yaklaşan/aşan kategoriler:\n\n"
                                 + "\n".join(lines)), send)
    except Exception as exc:
        log.warning("Bütçe uyarısı gönderilemedi: %s", exc)
        return []
    for b, level in crossed:
        storage.set_meta(f"budget:{month}:{b['category']}:{level}", today.isoformat())
        if level == 100:  # %100'ü geçen, aynı ay %80 uyarısını ayrıca almasın
            storage.set_meta(f"budget:{month}:{b['category']}:80", today.isoformat())
    return [b["category"] for b, _ in crossed]


def weekly_summary_text(storage: Storage, today: date) -> tuple[str, str]:
    """Geçen haftanın (Pzt-Paz) özeti: (konu, gövde)."""
    end = today - timedelta(days=today.weekday() + 1)      # geçen pazar
    start = end - timedelta(days=6)
    week = storage.period_summary(start, end)
    prev = storage.period_summary(start - timedelta(days=7), end - timedelta(days=7))
    lines = [f"{start:%d.%m} – {end:%d.%m.%Y} haftasında toplam harcama: {_tl(week['total'])}"]
    if prev["total"]:
        diff = (week["total"] - prev["total"]) / prev["total"] * 100
        lines.append(f"Önceki haftaya göre: {'+' if diff >= 0 else ''}{diff:.0f}% ({_tl(prev['total'])})")
    if week["categories"]:
        lines += ["", "Kategoriler:"] + [f"- {c}: {_tl(v)}" for c, v in week["categories"]]
    if week["places"]:
        lines += ["", "En çok harcanan yerler:"] + [f"- {p}: {_tl(v)}" for p, v in week["places"]]

    upcoming = [s for s in storage.list_statements()
                if s["due_date"] and today <= date.fromisoformat(s["due_date"]) <= today + timedelta(days=14)]
    if upcoming:
        lines += ["", "Yaklaşan ödemeler (14 gün):"] + [
            f"- {s['bank']}: {_tl(s['period_debt'])}, son gün {date.fromisoformat(s['due_date']):%d.%m}"
            for s in upcoming]
    plan = storage.installment_plan(today)
    if plan["this_month"]:
        lines += ["", f"Bu ay karta düşecek taksitler: {_tl(plan['this_month'])} "
                      f"(kalan toplam {_tl(plan['remaining_total'])})"]
    loans = [loan for loan in storage.loans_overview(today) if loan["monthly"] and loan["remaining"] != 0]
    if loans:
        lines += ["", "Krediler:"] + [
            f"- {loan['name']}: aylık {_tl(loan['monthly'])}"
            + (f", kalan {loan['remaining']} taksit" if loan["remaining"] is not None else "") for loan in loans]
    budgets = storage.budget_status(today.strftime("%Y-%m"))
    if budgets:
        lines += ["", "Bu ayın bütçeleri:"] + [
            f"- {b['category']}: {_tl(b['spent'])} / {_tl(b['amount'])} (%{int(b['ratio'] * 100)})" for b in budgets]
    return f"Haftalık özet: {_tl(week['total'])} harcama", "\n".join(lines)


def send_weekly_summary(config: Config, storage: Storage, today: date | None = None,
                        send: Sender = smtp_send, force: bool = False) -> str | None:
    """Pazartesi günleri (haftada bir kez) geçen haftanın özetini gönderir. force: hemen gönder.
    WEEKLY_SUMMARY=0 ile kapatılır."""
    today = today or date.today()
    if not force and (os.environ.get("WEEKLY_SUMMARY", "1") == "0" or today.weekday() != 0):
        return None
    key = f"weekly:{today.isocalendar()[0]}-{today.isocalendar()[1]}"
    if not force and storage.get_meta(key):
        return None
    subject, body = weekly_summary_text(storage, today)
    message = deliver(config, _message(subject, body), send)
    storage.set_meta(key, today.isoformat())
    return message["Subject"]


def send_test_mail(config: Config, send: Sender = smtp_send) -> str:
    message = deliver(config, _message("BankaTakip test maili",
                                       "Mail gönderimi çalışıyor. Hatırlatmalar, bütçe uyarıları ve haftalık "
                                       "özet bu adrese gelecek."), send)
    return f"{message['From']} → {message['To']}"
