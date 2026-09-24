from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage

from bankatakip import sync as sync_mod
from bankatakip.mail.imap_client import extract_body_text
from bankatakip.parsers.notifications import notification_sign, parse_notification
from bankatakip.parsers.tables import html_to_text
from bankatakip.storage import Storage

RECEIVED = datetime(2026, 9, 18, 14, 10)


def test_notification_sign():
    assert notification_sign("Akbank Kart harcamanız") == 1
    assert notification_sign("Kredi kartı harcamanız") == 1
    assert notification_sign("Akbank Kart harcamanız iptal edilmiştir") == -1
    assert notification_sign("Maaş ödemeniz gerçekleşmiştir") == -1
    assert notification_sign("Akbank Günlük Bülten") is None


def test_parse_notification_sentence_style():
    text = ("Sayın BURAK ALTIPARMAK, 5412 **** **** 1234 numaralı kartınız ile 18.09.2026 tarihinde "
            "saat 14:09'da MİGROS KADIKÖY işyerinden 845,30 TL tutarında harcama yapılmıştır.")
    tx = parse_notification(text, "Akbank Kart harcamanız", RECEIVED)
    assert tx.date == date(2026, 9, 18)
    assert tx.description == "MİGROS KADIKÖY"
    assert tx.amount == Decimal("845.30")


def test_parse_notification_table_style_and_cancel():
    text = "İşlem Tarihi  18/09/2026 14:09\nİşyeri: NETFLIX.COM\nTutar  229,99 TL"
    tx = parse_notification(text, "Akbank Kart harcamanız iptal edilmiştir", RECEIVED)
    assert tx.description == "NETFLIX.COM" and tx.amount == Decimal("-229.99")
    assert tx.date == date(2026, 9, 18)


def test_parse_notification_falls_back_to_received_date_and_subject():
    tx = parse_notification("Hesabınıza 25.000,00 TL maaş ödemesi yapılmıştır.",
                            "Maaş ödemeniz gerçekleşmiştir", RECEIVED)
    assert tx.date == date(2026, 9, 18)
    assert tx.amount == Decimal("-25000.00")
    assert tx.description == "Maaş ödemeniz gerçekleşmiştir"
    assert parse_notification("Tutar bilgisi yok", "Akbank Kart harcamanız", RECEIVED) is None


def test_html_body_to_text():
    msg = EmailMessage()
    msg.set_content("düz metin")
    msg.add_alternative(
        "<html><head><style>p{color:red}</style></head><body><p>Sayın Burak,</p>"
        "<table><tr><td>Dönem Borcu</td><td>4.321,50 TL</td></tr>"
        "<tr><td>Son Ödeme Tarihi</td><td>25.09.2026</td></tr></table></body></html>",
        subtype="html")
    text = extract_body_text(msg)
    assert "color" not in text
    assert "Dönem Borcu  4.321,50 TL" in text
    assert html_to_text("<p>a&nbsp;b</p><br>c") == "a b\nc"


class Client:
    def __init__(self, mails):
        self.mails = mails
        self.account = type("A", (), {"name": "icloud"})()

    def search(self, folder, sender, since):
        return list(self.mails)

    def fetch_headers(self, uids):
        from bankatakip.mail import MailHeader
        return {u: MailHeader(m.message_id, m.subject, m.sender, m.received) for u, m in self.mails.items()}

    def fetch(self, uid):
        return self.mails[uid]


def _mail(mid, subject, body, received=RECEIVED):
    from bankatakip.mail import FetchedMail
    return FetchedMail("icloud", mid, "hizmet@bilgi.akbank.com", subject, received, [], body)


def test_akbank_style_flow(config):
    from bankatakip.config import BankConfig
    bank = BankConfig("Akbank", ["akbank.com"], ["ekstre", "hesap özeti"])
    config.categories = {"Market": ["migros"]}
    storage = Storage(config.database)
    summary_body = "Dönem Borcu  4.321,50 TL\nAsgari Ödeme Tutarı  1.728,60 TL\nSon Ödeme Tarihi  25.09.2026"
    client = Client({
        b"5": _mail("<s1>", "Akbank Kart Hesap Özetiniz", summary_body),
        b"4": _mail("<s2>", "Kredi kartı ekstre borcunuz", summary_body),  # aynı dönem
        b"3": _mail("<n1>", "Akbank Kart harcamanız",
                    "18.09.2026 tarihinde MİGROS KADIKÖY işyerinden 845,30 TL harcama yapılmıştır."),
        b"2": _mail("<n2>", "Akbank Kart harcamanız", "Detaylar için Akbank Mobil'i ziyaret edin."),
        b"1": _mail("<m1>", "Axess'le Ekstrenizi Taksit Taksit Ödeyin", "Kampanya detayları..."),
    })
    report = sync_mod.SyncReport()
    sync_mod._sync_bank(client, "INBOX", bank, None, config, storage, report)
    status = {r["message_id"]: r["status"] for r in storage.list_mail_log()}
    assert status == {"<s1>": "eklendi", "<s2>": "zaten_var", "<n1>": "bildirim_eklendi",
                      "<n2>": "bildirim_okunamadi", "<m1>": "pdf_yok"}
    assert report.statements_added == 1 and report.transactions_added == 1

    statements = {s["source"]: s for s in storage.list_statements()}
    assert statements["icloud"]["due_date"] == "2026-09-25"
    assert Decimal(statements["icloud"]["period_debt"]) == Decimal("4321.50")
    assert statements["icloud (bildirim)"]["tx_count"] == 1
    [tx] = storage.list_transactions(category="Market")
    assert tx["description"] == "MİGROS KADIKÖY" and Decimal(tx["amount"]) == Decimal("845.30")

    # okunamayan bildirimin gövde örneği kaydedilir, yeniden denenebilir
    sample = [r for r in storage.list_mail_log() if r["message_id"] == "<n2>"][0]["detail"]
    assert "Akbank Mobil" in sample
    assert storage.reset_skipped_mails() == 2  # <n2> ve <m1>
