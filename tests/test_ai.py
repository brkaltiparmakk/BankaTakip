import json
from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest

from bankatakip import ai as ai_mod
from bankatakip import sync as sync_mod
from bankatakip.ai import AIError, AIQuotaExceeded, GeminiClient
from bankatakip.storage import Storage


def gemini_reply(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})


def client_with(handler, model=None) -> GeminiClient:
    return GeminiClient(api_key="k", model=model, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_notifications_batch_and_request_shape():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-goog-api-key"]
        body = json.loads(request.content)
        seen["body"] = body
        return gemini_reply({"items": [
            {"index": 0, "is_transaction": True,
             "transaction": {"date": "2026-09-18", "description": "MİGROS", "amount": "845.30", "direction": "harcama"}},
            {"index": 1, "is_transaction": True,
             "transaction": {"date": None, "description": "NETFLIX", "amount": "229,99", "direction": "iade"}},
            {"index": 2, "is_transaction": False},
        ]})

    c = client_with(handler)
    rec = datetime(2026, 9, 20, 10, 0)
    txs = c.extract_notifications([("Kart harcamanız", "a", rec), ("İptal", "b", rec), ("Bülten", "c", rec)])
    assert "gemini-flash-latest:generateContent" in seen["url"] and seen["key"] == "k"
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert "[2] Konu: Bülten" in seen["body"]["contents"][0]["parts"][0]["text"]
    assert txs[0].amount == Decimal("845.30") and txs[0].date == date(2026, 9, 18)
    assert txs[1].amount == Decimal("-229.99") and txs[1].date == date(2026, 9, 20)  # tarih yoksa mail tarihi
    assert txs[2] is None


def test_statement_with_pdf_and_model_fallback():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "gemini-flash-latest" in str(request.url):
            return httpx.Response(404, json={"error": "not found"})
        parts = json.loads(request.content)["contents"][0]["parts"]
        assert parts[1]["inline_data"]["mime_type"] == "application/pdf"
        return gemini_reply({"is_statement": True, "kind": "kredi_karti", "due_date": "2026-09-30",
                             "period_debt": "4321.50", "minimum_payment": "1728.60", "statement_date": "2026-09-20",
                             "transactions": [{"date": "2026-09-02", "description": "SHELL", "amount": "1500", "direction": "harcama"}]})

    st = client_with(handler).extract_statement("Enpara", "ekstre", document=b"%PDF-1.4 ...")
    assert len(calls) == 2 and "gemini-2.5-flash" in calls[1]
    assert st.summary.due_date == date(2026, 9, 30) and st.summary.period_debt == Decimal("4321.50")
    assert st.transactions[0].amount == Decimal("1500")


def test_errors():
    with pytest.raises(AIQuotaExceeded):
        client_with(lambda r: httpx.Response(429)).extract_statement("X", "s", text="t")
    with pytest.raises(AIError):
        client_with(lambda r: httpx.Response(403)).extract_statement("X", "s", text="t")
    assert client_with(lambda r: gemini_reply({"is_statement": False, "transactions": []})
                       ).extract_statement("X", "s", text="kampanya") is None


class FakeAI:
    def __init__(self, quota_after=None):
        self.calls = 0
        self.quota_after = quota_after

    def extract_notifications(self, items):
        self.calls += 1
        if self.quota_after is not None and self.calls > self.quota_after:
            raise AIQuotaExceeded("sınır doldu")
        return [ai_mod.Transaction(date(2026, 9, 18), f"İŞYERİ {i}", Decimal("10")) for i, _ in enumerate(items)]

    def extract_statement(self, bank, subject, text=None, document=None, mime_type="application/pdf"):
        if "kampanya" in (text or ""):
            return None
        return ai_mod.ParsedStatement(bank, ai_mod.StatementSummary(period_debt=Decimal("99.90"),
                                                                    due_date=date(2026, 9, 30)))


def test_sync_uses_ai_as_fallback(config):
    from bankatakip.config import BankConfig
    from tests.test_notifications import Client, _mail

    bank = BankConfig("Akbank", ["akbank.com"], ["ekstre", "hesap özeti"])
    storage = Storage(config.database)
    client = Client({
        b"4": _mail("<s>", "Akbank Kart Hesap Özetiniz", "Borç bilgileriniz tabloda"),
        b"3": _mail("<k>", "Axess'le Ekstrenizi Taksit Taksit Ödeyin", "kampanya"),
        b"2": _mail("<n1>", "Akbank Kart harcamanız", "biçimi bilinmeyen bildirim"),
        b"1": _mail("<n2>", "Akbank Kart harcamanız", "biçimi bilinmeyen bildirim 2"),
    })
    fake = FakeAI()
    report = sync_mod.SyncReport()
    sync_mod._sync_bank(client, "INBOX", bank, None, config, storage, report, ai=fake)
    status = {r["message_id"]: (r["status"], r["detail"]) for r in storage.list_mail_log()}
    assert status["<s>"][0] == "eklendi" and status["<s>"][1].startswith("yapay zeka")
    assert status["<k>"][0] == "pdf_yok"
    assert status["<n1>"][0] == status["<n2>"][0] == "bildirim_eklendi"
    assert fake.calls == 1  # iki bildirim tek istekte
    assert report.ai_used == 4 and report.statements_added == 1 and report.transactions_added == 2


def test_sync_ai_quota_leaves_mails_for_next_run(config):
    from bankatakip.config import BankConfig
    from tests.test_notifications import Client, _mail

    bank = BankConfig("Akbank", ["akbank.com"], ["ekstre"])
    storage = Storage(config.database)
    client = Client({b"1": _mail("<n1>", "Akbank Kart harcamanız", "bilinmeyen")})
    report = sync_mod.SyncReport()
    sync_mod._sync_bank(client, "INBOX", bank, None, config, storage, report, ai=FakeAI(quota_after=0))
    assert report.ai_paused and report.errors == ["sınır doldu"]
    assert not storage.is_mail_processed("icloud", "<n1>")  # sonraki taramada tekrar denenecek

    sync_mod._sync_bank(client, "INBOX", bank, None, config, storage, sync_mod.SyncReport(), ai=FakeAI())
    assert storage.is_mail_processed("icloud", "<n1>")
