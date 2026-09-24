from datetime import date
from decimal import Decimal

from bankatakip.config import MailAccount
from bankatakip.models import ParsedStatement, StatementSummary
from bankatakip.reminders import send_due_reminders
from bankatakip.storage import Storage


def _add(storage, bank, due, h):
    st = ParsedStatement(bank=bank, summary=StatementSummary(
        period_debt=Decimal("1234.50"), minimum_payment=Decimal("500"), due_date=due))
    storage.save_statement(st, h, source="gmail")


def test_reminders(config, monkeypatch):
    storage = Storage(config.database)
    config.accounts = [MailAccount("gmail", "gmail", "ben@gmail.com", "GMAIL_APP_PASSWORD", "imap.gmail.com")]
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    monkeypatch.delenv("REMINDER_EMAIL", raising=False)
    monkeypatch.delenv("REMINDER_DAYS", raising=False)
    today = date(2026, 9, 24)
    _add(storage, "Garanti BBVA", date(2026, 9, 26), "a")   # 2 gün kaldı → hatırlat
    _add(storage, "Akbank", date(2026, 10, 10), "b")        # çok erken
    _add(storage, "Yapı Kredi", date(2026, 9, 20), "c")     # geçmiş

    sent = []
    send = lambda account, msg: sent.append(msg)
    subjects = send_due_reminders(config, storage, today=today, send=send)
    assert subjects == ["Garanti BBVA kart ödemesi 2 gün sonra: 1.234,50 TL"]
    assert sent[0]["To"] == "ben@gmail.com"
    assert "Asgari ödeme: 500,00 TL" in sent[0].get_content()

    # aynı ekstre için ikinci kez gönderilmez
    assert send_due_reminders(config, storage, today=today, send=send) == []

    monkeypatch.setenv("REMINDER_DAYS", "20")
    monkeypatch.setenv("REMINDER_EMAIL", "baska@ornek.com")
    assert len(send_due_reminders(config, storage, today=today, send=send)) == 1
    assert sent[-1]["To"] == "baska@ornek.com"


def test_reminders_skip_without_account_or_on_failure(config, monkeypatch):
    storage = Storage(config.database)
    _add(storage, "Garanti BBVA", date(2026, 9, 25), "a")
    assert send_due_reminders(config, storage, today=date(2026, 9, 24)) == []  # hesap yok

    config.accounts = [MailAccount("gmail", "gmail", "ben@gmail.com", "GMAIL_APP_PASSWORD", "imap.gmail.com")]
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")

    def fail(account, msg):
        raise OSError("smtp kapalı")
    assert send_due_reminders(config, storage, today=date(2026, 9, 24), send=fail) == []
    # başarısız gönderim işaretlenmez, sonraki sefer tekrar denenir
    assert len(send_due_reminders(config, storage, today=date(2026, 9, 24), send=lambda a, m: None)) == 1
