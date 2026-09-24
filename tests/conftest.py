import io
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from bankatakip.config import BankConfig, Config

SAMPLE_LINES = [
    "GARANTI BBVA KREDI KARTI HESAP OZETI",
    "Hesap Kesim Tarihi: 15.08.2026",
    "Son Odeme Tarihi: 25.08.2026",
    "Donem Borcu: 4.321,50 TL",
    "Asgari Odeme Tutari: 1.728,60 TL",
    "Tarih Aciklama Tutar",
    "02.08.2026 MIGROS KADIKOY ISTANBUL 845,30",
    "05.08.2026 NETFLIX.COM 229,99",
    "07.08.2026 TRENDYOL 3/6 TAKSIT 1.250,00",
    "10.08.2026 ONCEKI DONEM ODEMESI 2.000,00+",
    "12.08.2026 SHELL MASLAK 1.996,21",
]


def _dejavu() -> str | None:
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        if Path(p).exists():
            return p
    return None


def make_pdf(lines: list[str], password: str | None = None) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    font = "Helvetica"
    if (path := _dejavu()):
        pdfmetrics.registerFont(TTFont("DejaVu", path))
        font = "DejaVu"
    c.setFont(font, 10)
    y = 800
    for line in lines:
        c.drawString(40, y, line)
        y -= 16
    c.save()
    data = buf.getvalue()
    if password:
        writer = PdfWriter()
        for page in PdfReader(io.BytesIO(data)).pages:
            writer.add_page(page)
        writer.encrypt(password, algorithm="AES-256")
        out = io.BytesIO()
        writer.write(out)
        data = out.getvalue()
    return data


@pytest.fixture
def sample_pdf() -> bytes:
    return make_pdf(SAMPLE_LINES)


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        database=tmp_path / "test.db",
        attachments_dir=tmp_path / "ekstreler",
        lookback_days=30,
        accounts=[],
        banks=[BankConfig(name="Garanti BBVA", senders=["garantibbva.com.tr"],
                          subject_keywords=["ekstre"], pdf_password_env="TEST_PDF_PW")],
        categories={"Market": ["migros"], "Abonelik": ["netflix"], "Ulaşım": ["shell"],
                    "Alışveriş": ["trendyol"], "Ödeme": ["ödeme"]},
    )
