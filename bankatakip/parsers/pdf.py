from __future__ import annotations

import io

import pdfplumber
from pypdf import PdfReader


class PdfPasswordError(Exception):
    pass


def extract_text(content: bytes, password: str | None = None) -> str:
    """PDF'i (gerekirse şifreyle) açıp tüm sayfaların metnini döndürür."""
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted:
        if not password or not reader.decrypt(password):
            raise PdfPasswordError(
                "PDF şifreli. config.yaml'daki pdf_password_env değişkenini .env içinde tanımlayın."
            )
    with pdfplumber.open(io.BytesIO(content), password=password or "") as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    return "\n".join(pages)
