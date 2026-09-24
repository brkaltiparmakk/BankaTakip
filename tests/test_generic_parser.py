from datetime import date
from decimal import Decimal

import pytest

from bankatakip.parsers.generic import GenericParser, parse_amount, parse_date, tr_fold


@pytest.mark.parametrize("text,expected", [
    ("1.234,56", Decimal("1234.56")),
    ("845,30 TL", Decimal("845.30")),
    ("-250,00", Decimal("-250.00")),
    ("2.000,00+", Decimal("-2000.00")),
    ("12.345.678,90", Decimal("12345678.90")),
    ("tutar yok", None),
])
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("15.08.2026", date(2026, 8, 15)),
    ("15/08/26", date(2026, 8, 15)),
    ("3 Ağustos 2026", date(2026, 8, 3)),
    ("1 ŞUBAT 2026", date(2026, 2, 1)),
    ("32.01.2026", None),
])
def test_parse_date(text, expected):
    assert parse_date(text) == expected


def test_tr_fold_keeps_length():
    text = "İSTANBUL ŞİŞLİ ÖDEME ığüşöç"
    assert len(tr_fold(text)) == len(text)
    assert tr_fold(text) == "istanbul sisli odeme igusoc"


def test_parse_line_variants():
    p = GenericParser("Test")
    tx = p.parse_line("02.08.2026 MIGROS KADIKOY 845,30")
    assert tx.date == date(2026, 8, 2) and tx.description == "MIGROS KADIKOY"
    assert tx.amount == Decimal("845.30")

    # işlem tarihi + valör tarihi + bakiye sütunu
    tx = p.parse_line("02.08.2026 03.08.2026 EFT GELEN   1.000,00   5.500,00")
    assert tx.description == "EFT GELEN" and tx.amount == Decimal("1000.00")

    # taksit bilgisi tutar sanılmamalı
    tx = p.parse_line("07.08.2026 TRENDYOL 3/6 TAKSIT 1.250,00")
    assert tx.amount == Decimal("1250.00")

    tx = p.parse_line("12 Ağustos 2026 İYZİCO ÖDEME -99,90")
    assert tx.date == date(2026, 8, 12) and tx.amount == Decimal("-99.90")

    assert p.parse_line("Son Ödeme Tarihi 25.08.2026") is None
    assert p.parse_line("Tarih Açıklama Tutar") is None


def test_summary_and_categories():
    text = "\n".join([
        "HESAP KESİM TARİHİ : 15.08.2026",
        "SON ÖDEME TARİHİ : 25.08.2026",
        "DÖNEM BORCU : 4.321,50 TL",
        "ASGARİ ÖDEME TUTARI : 1.728,60 TL",
        "02.08.2026 MİGROS KADIKÖY 845,30",
        "10.08.2026 ÖNCEKİ DÖNEM ÖDEMESİ 2.000,00+",
    ])
    st = GenericParser("Test", {"Market": ["migros"], "Ödeme": ["ödeme"]}).parse(text)
    assert st.summary.statement_date == date(2026, 8, 15)
    assert st.summary.due_date == date(2026, 8, 25)
    assert st.summary.period_debt == Decimal("4321.50")
    assert st.summary.minimum_payment == Decimal("1728.60")
    assert [t.category for t in st.transactions] == ["Market", "Ödeme"]
    assert st.transactions[1].amount == Decimal("-2000.00")


def test_categorize_word_start():
    p = GenericParser("Test", {"Market": ["bim", "şok"], "Ulaşım": ["bp", "shell"]})
    assert p.categorize("BİM") == "Market"
    assert p.categorize("BIM BIRLESIK MAGAZALAR") == "Market"
    assert p.categorize("ŞOK MARKET") == "Market"
    assert p.categorize("IBIMAX") is None
    assert p.categorize("BP ISTANBUL") == "Ulaşım"
    assert p.categorize("BPX") is None
    assert p.categorize("SHELLTR MASLAK") == "Ulaşım"


def test_mail_password_cleanup(monkeypatch):
    from bankatakip.config import MailAccount
    gmail = MailAccount("gmail", "gmail", "a@gmail.com", "T_GMAIL_PW", "imap.gmail.com")
    icloud = MailAccount("icloud", "icloud", "a@icloud.com", "T_ICLOUD_PW", "imap.mail.me.com")
    monkeypatch.setenv("T_GMAIL_PW", " abcd efgh ijkl mnop \n")
    monkeypatch.setenv("T_ICLOUD_PW", " abcd-efgh-ijkl-mnop ")
    assert gmail.password == "abcdefghijklmnop"
    assert icloud.password == "abcd-efgh-ijkl-mnop"


def test_clean_email():
    from bankatakip.config import clean_email
    assert clean_email(" brk@gmail.com \n") == "brk@gmail.com"
    assert clean_email("Burak Altıparmak <brk@gmail.com>") == "brk@gmail.com"
    assert clean_email("Burak brk@gmail.com") == "brk@gmail.com"
    assert clean_email("brk@gmail.com, diger@icloud.com") == "brk@gmail.com"
