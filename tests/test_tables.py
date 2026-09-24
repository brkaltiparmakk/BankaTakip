import io
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage

import pytest

from bankatakip.mail.imap_client import extract_statement_attachments
from bankatakip.parsers import UnsupportedDocument, detect_kind, extract_document_text, get_parser

HEADER = ["Tarih", "Açıklama", "Etiket", "Tutar", "Bakiye", "Dekont No"]
ROWS = [
    (datetime(2026, 8, 1), "MİGROS KADIKÖY", "", -845.30, 4154.70, 123456),
    (datetime(2026, 8, 2), "MAAŞ ÖDEMESİ", "", 25000, 29154.70, 123457),
    (datetime(2026, 8, 3), "ATM PARA ÇEKME", "", -500, 28654.70, 123458),
]


def make_xls() -> bytes:
    import xlwt
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Hareketler")
    ws.write(0, 0, "661 - 6644898 numaralı Vadesiz TL hesap hareketleri")
    for c, h in enumerate(HEADER):
        ws.write(2, c, h)
    date_style = xlwt.easyxf(num_format_str="DD.MM.YYYY")
    for r, row in enumerate(ROWS, start=3):
        for c, v in enumerate(row):
            ws.write(r, c, v, date_style if c == 0 else xlwt.Style.default_style)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_xlsx() -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for row in ROWS:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_html() -> bytes:
    cells = "".join(f"<th>{h}</th>" for h in HEADER)
    def tr(v):
        return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    body = "".join(
        f"<tr><td>{d:%d.%m.%Y}</td><td>{desc}</td><td></td><td>{tr(amt)}</td><td>{tr(bal)}</td><td>{no}</td></tr>"
        for d, desc, _, amt, bal, no in ROWS
    )
    html = f'<html><head><meta charset="windows-1254"></head><body><table><tr>{cells}</tr>{body}</table></body></html>'
    return html.encode("windows-1254")


@pytest.mark.parametrize("maker,kind", [(make_xls, "xls"), (make_xlsx, "xlsx"), (make_html, "html")])
def test_account_movements_from_tables(maker, kind):
    content = maker()
    assert detect_kind(content) == kind
    text = extract_document_text(content)
    st = get_parser("Garanti BBVA", {"Market": ["migros"]}).parse(text)
    txs = {t.description: t for t in st.transactions}
    assert set(txs) == {"MİGROS KADIKÖY", "MAAŞ ÖDEMESİ", "ATM PARA ÇEKME"}
    # vadesiz hesapta çıkan para harcama (artı), gelen para eksi olur
    assert txs["MİGROS KADIKÖY"].amount == Decimal("845.30")
    assert txs["MAAŞ ÖDEMESİ"].amount == Decimal("-25000.00")
    assert txs["ATM PARA ÇEKME"].amount == Decimal("500.00")
    assert txs["MİGROS KADIKÖY"].date == date(2026, 8, 1)
    assert txs["MİGROS KADIKÖY"].category == "Market"


def test_card_statement_sign_not_flipped():
    text = "Son Ödeme Tarihi: 30.09.2026\nDönem Borcu: 100,00\nBakiye: 0,00\n01.09.2026 MIGROS 100,00"
    st = get_parser("X").parse(text)
    assert st.transactions[0].amount == Decimal("100.00")


def test_enpara_summary_labels():
    text = "Ekstre borcu 69,90 TL\nMinimum ödeme tutarı 14,00 TL\nSon ödeme tarihi 30.09.2026"
    s = get_parser("Enpara").parse(text).summary
    assert s.period_debt == Decimal("69.90") and s.minimum_payment == Decimal("14.00")
    assert s.due_date == date(2026, 9, 30)


def test_unsupported_document():
    with pytest.raises(UnsupportedDocument):
        extract_document_text(b"merhaba")


def test_mail_attachment_types():
    msg = EmailMessage()
    msg.set_content("gövde")
    msg.add_alternative("<html><table><tr><td>gövde</td></tr></table></html>", subtype="html")
    msg.add_attachment(make_xls(), maintype="application", subtype="vnd.ms-excel", filename="hesaphareketleri.xls")
    msg.add_attachment(b"%PDF-1.4", maintype="application", subtype="octet-stream", filename="ekstre.PDF")
    msg.add_attachment(b"x", maintype="image", subtype="png", filename="logo.png")
    names = [a.filename for a in extract_statement_attachments(msg)]
    assert names == ["hesaphareketleri.xls", "ekstre.PDF"]
