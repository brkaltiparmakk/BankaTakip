from datetime import date, datetime
from decimal import Decimal

import pytest

from bankatakip import sync as sync_mod
from bankatakip.config import BankConfig, load_config
from bankatakip.models import AccountRef
from bankatakip.parsers import get_parser
from bankatakip.storage import Storage

# Garanti "Hesap Hareketleri" biçimi: çıkan para eksi, her satırda işlem sonrası bakiye
ROWS_ASC = [
    "01.07.2026  QR ILE PARA YATIRMA  27.800,00  30.000,00  1001",
    "05.07.2026  K.Kartı Ödeme 5549 **** **** 1046 Kart Ödemesi  -7.500,00  22.500,00  1002",
    "10.07.2026  MIGROS KADIKOY  -500,00  22.000,00  1003",
    "22.07.2026  CEP ŞUBE-HVL- -YILMAZ Para Transferi  -15.000,00  7.000,00  1004",
]


def account_text(rows):
    return "Tarih  Açıklama  Tutar  Bakiye  Dekont No\n" + "\n".join(rows)


@pytest.mark.parametrize("rows", [ROWS_ASC, list(reversed(ROWS_ASC))])
def test_account_statement_signs_from_balance(rows, tmp_path):
    config = load_config(tmp_path / "yok.yaml")
    st = get_parser("Garanti BBVA", config.categories).parse(account_text(rows))
    assert st.kind == "vadesiz" and st.summary.period_debt is None
    tx = {t.description.split()[0]: t for t in st.transactions}
    assert tx["QR"].amount == Decimal("-27800.00")      # para girişi
    assert tx["K.Kartı"].amount == Decimal("7500.00")   # para çıkışı
    assert tx["K.Kartı"].category == "Kart Ödemesi"
    assert tx["CEP"].category == "Transfer"
    assert tx["MIGROS"].balance == Decimal("22000.00")


def test_duplicate_account_statement_and_snapshot(config, tmp_path):
    storage = Storage(config.database)
    parser = get_parser("Garanti BBVA", load_config(tmp_path / "yok.yaml").categories)
    first = parser.parse(account_text(ROWS_ASC))
    assert storage.save_statement(first, "h1", "icloud") is not None
    assert first.saved_transactions == 4
    # aynı döküm başka bir dosya olarak tekrar gelirse işlemler iki kat sayılmaz
    second = parser.parse(account_text(list(reversed(ROWS_ASC))))
    storage.save_statement(second, "h2", "icloud")
    assert second.saved_transactions == 0
    assert len(storage.list_transactions()) == 4

    [acc] = storage.accounts_overview()
    assert acc["kind"] == "vadesiz" and acc["name"] == "Garanti BBVA Vadesiz"
    assert acc["snapshot"]["as_of"] == "2026-07-22" and Decimal(acc["snapshot"]["balance"]) == Decimal("7000.00")
    assert acc["estimate"] == Decimal("7000.00")

    # sonrasında gelen bir çıkış tahmini bakiyeyi düşürür
    from bankatakip.models import Transaction
    storage.add_notification("Garanti BBVA", "icloud", Transaction(
        date(2026, 7, 25), "BENZIN", Decimal("1000"), account=AccountRef("vadesiz", "vadesiz", "x")))
    [acc] = storage.accounts_overview()
    assert acc["out_since"] == Decimal("1000") and acc["estimate"] == Decimal("6000.00")

    # harcama özetinde transfer ve kart ödemesi yok
    cats = {c for _, c, _ in storage.monthly_summary()}
    assert "Transfer" not in cats and "Kart Ödemesi" not in cats
    flows = {f["month"]: f for f in storage.monthly_flows()}
    assert flows["2026-07"]["in"] == Decimal("27800.00")
    assert flows["2026-07"]["out"] == Decimal("7500") + Decimal("500") + Decimal("15000") + Decimal("1000")


def test_card_statement_body_and_limit_notification(config, tmp_path):
    from tests.test_notifications import Client, _mail

    config.categories = load_config(tmp_path / "yok.yaml").categories
    bank = BankConfig("Akbank", ["akbank.com"], ["ekstre", "hesap özeti"])
    storage = Storage(config.database)
    body = ("Değerli Akbanklı, 5839'le biten Troy dönem borcunuz 104.773,02 TL, en az ödeme tutarı "
            "41.909,21 TL, son ödeme tarihi 09.02.2026, kullanılabilir kart limitiniz 232.968,98 TL'dir.")
    spend = ("Değerli Akbanklı, 5839 ile biten BURAK adına ait Axess Asıl kartınızla 1,899.00 TL tutarında "
             "KREDI KARTI harcaması yapılmıştır. 199,387.16 TL limitiniz kalmıştır.")
    debit = ("Değerli Akbanklı, 8387 ile biten Akbank Kart'ınla 110,00 TL tutarında BENZIN ISTASYONU "
             "kategorisinde temassız ödeme işlemi yapılmıştır. İşlem tarihi: 12/08/2026")
    maas = "Değerli Akbanklı, 5***8 no.lu hesabınıza MAAŞ ÖDEMESİ açıklamasıyla ödeme yapılmıştır."
    client = Client({
        b"4": _mail("<e>", "Kredi kartı ekstre bilgileri", body, datetime(2026, 1, 30, 9)),
        b"3": _mail("<k>", "Kredi kartı harcamanız", spend, datetime(2026, 2, 3, 12)),
        b"2": _mail("<d>", "Akbank Kart harcamanız", debit, datetime(2026, 8, 12, 9)),
        b"1": _mail("<m>", "Maaş ödemeniz gerçekleşmiştir", maas),
    })
    sync_mod._sync_bank(client, "INBOX", bank, None, config, storage, sync_mod.SyncReport())
    status = {r["message_id"]: r["status"] for r in storage.list_mail_log()}
    assert status == {"<e>": "eklendi", "<k>": "bildirim_eklendi", "<d>": "bildirim_eklendi", "<m>": "bilgi"}

    [st] = [s for s in storage.list_statements() if s["due_date"]]
    assert Decimal(st["minimum_payment"]) == Decimal("41909.21")

    accounts = {a["name"]: a for a in storage.accounts_overview()}
    card = accounts["Akbank Kredi Kartı •5839"]
    assert card["kind"] == "kredi_karti"
    assert Decimal(card["limit"]["available_limit"]) == Decimal("199387.16")   # en güncel: bildirim
    assert Decimal(card["snapshot"]["balance"]) == Decimal("104773.02")         # ekstre borcu
    assert card["last_statement"]["due_date"] == "2026-02-09"
    tx = {t["description"]: t for t in storage.list_transactions()}
    assert tx["Kredi kartı harcaması"]["account_id"] == card["id"]
    assert tx["BENZIN ISTASYONU"]["category"] == "Akaryakıt"
    assert tx["BENZIN ISTASYONU"]["account_id"] == accounts["Akbank Vadesiz"]["id"]
    assert accounts["Akbank Vadesiz"]["estimate"] is None  # bakiye bilinmiyor → elle girilecek


def test_manual_balance_api(config, monkeypatch):
    from fastapi.testclient import TestClient
    from bankatakip.web import app as web_app
    from bankatakip.models import Transaction

    monkeypatch.setenv("AUTH_DISABLED", "1")
    monkeypatch.delenv("VERCEL", raising=False)
    storage = Storage(config.database)
    acc_ref = AccountRef("vadesiz", "vadesiz", "Akbank Vadesiz")
    storage.add_notification("Akbank", "icloud", Transaction(date(2026, 9, 10), "MARKET", Decimal("250"), account=acc_ref))
    storage.add_notification("Akbank", "icloud", Transaction(date(2026, 9, 1), "ESKI", Decimal("999"), account=acc_ref))
    web_app.app.dependency_overrides[web_app.get_config] = lambda: config
    client = TestClient(web_app.app)
    H = {"X-Requested-With": "bankatakip"}
    try:
        [acc] = client.get("/api/accounts").json()["accounts"]
        assert acc["estimate"] is None
        r = client.post(f"/api/accounts/{acc['id']}/balance", json={"balance": "10000", "as_of": "2026-09-05"}, headers=H)
        assert r.status_code == 200
        [acc] = client.get("/api/accounts").json()["accounts"]
        assert acc["estimate"] == 9750.0 and acc["out_since"] == 250.0
        assert client.patch(f"/api/accounts/{acc['id']}", json={"name": "Maaş hesabı"}, headers=H).status_code == 200
        data = client.get("/api/accounts").json()
        assert data["accounts"][0]["name"] == "Maaş hesabı"
        assert data["flows"][-1]["month"] == "2026-09"
        assert client.post(f"/api/accounts/{acc['id']}/balance", json={"balance": "abc"}, headers=H).status_code == 422
    finally:
        web_app.app.dependency_overrides.clear()
