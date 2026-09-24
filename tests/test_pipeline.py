from datetime import datetime
from decimal import Decimal
from email.message import EmailMessage

import pytest

from bankatakip import sync as sync_mod
from bankatakip.mail.imap_client import FetchedMail, extract_pdf_attachments
from bankatakip.parsers import PdfPasswordError, extract_text
from bankatakip.storage import Storage

from .conftest import SAMPLE_LINES, make_pdf


def test_extract_text_from_pdf(sample_pdf):
    text = extract_text(sample_pdf)
    assert "MIGROS KADIKOY" in text


def test_encrypted_pdf_requires_password():
    data = make_pdf(SAMPLE_LINES, password="123456")
    with pytest.raises(PdfPasswordError):
        extract_text(data)
    with pytest.raises(PdfPasswordError):
        extract_text(data, "yanlis")
    assert "NETFLIX" in extract_text(data, "123456")


def test_import_pdf_saves_and_dedupes(config, sample_pdf):
    storage = Storage(config.database)
    bank = config.banks[0]
    sid, count = sync_mod.import_pdf(sample_pdf, bank, config, storage, source="manuel")
    assert sid is not None and count == 5
    assert sync_mod.import_pdf(sample_pdf, bank, config, storage, source="manuel") == (None, 0)

    st = storage.list_statements()[0]
    assert st["due_date"] == "2026-08-25" and Decimal(st["period_debt"]) == Decimal("4321.50")
    txs = storage.list_transactions()
    assert {t["category"] for t in txs} == {"Market", "Abonelik", "Alışveriş", "Ödeme", "Ulaşım"}

    summary = {(m, c): v for m, c, v in storage.monthly_summary()}
    assert summary[("2026-08", "Market")] == Decimal("845.30")
    assert ("2026-08", "Ödeme") not in summary  # ödemeler harcama sayılmaz


def test_extract_pdf_attachments(sample_pdf):
    msg = EmailMessage()
    msg["Subject"] = "Ekstreniz"
    msg.set_content("Ekstreniz ektedir.")
    msg.add_attachment(sample_pdf, maintype="application", subtype="pdf", filename="Ağustos Ekstre.pdf")
    msg.add_attachment(b"x", maintype="image", subtype="png", filename="logo.png")
    atts = extract_pdf_attachments(msg)
    assert [a.filename for a in atts] == ["Ağustos Ekstre.pdf"]
    assert atts[0].content == sample_pdf


class FakeClient:
    """IMAP sunucusu yerine geçen sahte istemci."""

    def __init__(self, mails):
        self.mails = mails
        self.account = type("A", (), {"name": "gmail"})()

    def search(self, folder, sender, since):
        return [uid for uid, (s, _) in self.mails.items() if sender in s.sender]

    def fetch_headers(self, uids):
        from bankatakip.mail import MailHeader
        return {uid: MailHeader(self.mails[uid][0].message_id, self.mails[uid][0].subject,
                                self.mails[uid][0].sender, self.mails[uid][0].received)
                for uid in uids}

    def fetch(self, uid):
        return self.mails[uid][0]


def _mail(mid, subject, pdf):
    from bankatakip.mail.imap_client import Attachment
    return FetchedMail("gmail", mid, "bilgi@garantibbva.com.tr", subject,
                       datetime(2026, 8, 16), [Attachment("ekstre.pdf", pdf)])


def test_sync_bank_flow(config, sample_pdf, monkeypatch):
    storage = Storage(config.database)
    encrypted = make_pdf(SAMPLE_LINES[:3] + ["01.07.2026 A101 50,00"], password="9999")
    client = FakeClient({
        b"1": (_mail("<a>", "Ağustos Ekstreniz", sample_pdf), None),
        b"2": (_mail("<b>", "Kampanya fırsatı", sample_pdf), None),
        b"3": (_mail("<c>", "Temmuz ekstresi", encrypted), None),
    })
    report = sync_mod.SyncReport()
    monkeypatch.delenv("TEST_PDF_PW", raising=False)
    sync_mod._sync_bank(client, "INBOX", config.banks[0], None, config, storage, report)
    assert report.statements_added == 1 and report.transactions_added == 5
    assert len(report.errors) == 1  # şifreli PDF
    assert storage.is_mail_processed("gmail", "<a>")
    assert storage.is_mail_processed("gmail", "<b>")      # konu eşleşmedi, atlandı
    assert not storage.is_mail_processed("gmail", "<c>")  # şifre gelince tekrar denenecek

    monkeypatch.setenv("TEST_PDF_PW", "9999")
    report = sync_mod.SyncReport()
    sync_mod._sync_bank(client, "INBOX", config.banks[0], None, config, storage, report)
    assert report.statements_added == 1 and not report.errors
    assert storage.is_mail_processed("gmail", "<c>")


def test_turkish_characters_in_pdf(config):
    pytest.importorskip("reportlab")
    from .conftest import _dejavu
    if not _dejavu():
        pytest.skip("Türkçe karakterli font yok")
    pdf = make_pdf(["SON ÖDEME TARİHİ : 25.08.2026", "03.08.2026 ŞOK MARKET ÇANKAYA 112,40"])
    st = sync_mod.get_parser("X", {"Market": ["şok"]}).parse(extract_text(pdf))
    assert st.summary.due_date.isoformat() == "2026-08-25"
    assert st.transactions[0].description == "ŞOK MARKET ÇANKAYA"
    assert st.transactions[0].category == "Market"


def test_mail_log_and_reset(config, sample_pdf):
    storage = Storage(config.database)
    no_pdf = _mail("<np>", "Eylül ekstreniz", sample_pdf)
    no_pdf.attachments = []
    client = FakeClient({
        b"1": (_mail("<a>", "Ağustos Ekstreniz", sample_pdf), None),
        b"2": (_mail("<b>", "Kampanya fırsatı", sample_pdf), None),
        b"3": (no_pdf, None),
        b"4": (_mail("<d>", "Tekrar ekstre", sample_pdf), None),  # aynı PDF
    })
    sync_mod._sync_bank(client, "INBOX", config.banks[0], None, config, storage, sync_mod.SyncReport())
    statuses = {r["message_id"]: r["status"] for r in storage.list_mail_log()}
    assert statuses == {"<a>": "eklendi", "<b>": "konu_eslesmedi", "<np>": "pdf_yok", "<d>": "zaten_var"}
    assert all(r["sender"] == "bilgi@garantibbva.com.tr" for r in storage.list_mail_log())

    storage.set_meta("last_sync:gmail", "2026-09-01")
    storage.mark_mail_processed("gmail", "<eski>")  # kayıt tutulmadan işaretlenmiş eski mail
    assert storage.reset_skipped_mails() == 3
    assert not storage.is_mail_processed("gmail", "<eski>")
    assert not storage.is_mail_processed("gmail", "<b>")
    assert not storage.is_mail_processed("gmail", "<np>")
    assert storage.is_mail_processed("gmail", "<a>")
    assert storage.get_meta("last_sync:gmail") is None
    assert {r["message_id"] for r in storage.list_mail_log()} == {"<a>", "<d>"}


def test_fetch_headers_parses_imap_response():
    from bankatakip.config import MailAccount
    from bankatakip.mail import MailClient

    class Conn:
        def uid(self, cmd, uids, query):
            assert cmd == "FETCH" and uids == b"10,11"
            return "OK", [
                (b"1 (UID 10 BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)] {120}",
                 b"Message-ID: <x@banka>\r\nSubject: =?utf-8?q?Hesap_=C3=96zeti?=\r\n"
                 b"From: Banka <bilgi@banka.com.tr>\r\nDate: Mon, 15 Sep 2026 10:00:00 +0300\r\n\r\n"),
                b")",
                (b"2 (UID 11 BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)] {30}",
                 b"Subject: Kampanya\r\n\r\n"),
                b")",
            ]

    client = MailClient(MailAccount("gmail", "gmail", "a@gmail.com", "X", "imap.gmail.com"))
    client.conn = Conn()
    headers = client.fetch_headers([b"10", b"11"])
    assert headers[b"10"].subject == "Hesap Özeti"
    assert headers[b"10"].sender == "bilgi@banka.com.tr"
    assert headers[b"10"].message_id == "<x@banka>"
    assert headers[b"10"].received.day == 15
    assert headers[b"11"].message_id == "gmail-11"  # Message-ID yoksa yedek kimlik
