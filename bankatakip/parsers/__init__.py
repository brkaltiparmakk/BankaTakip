"""Ekstre ayrıştırıcıları.

Bir banka için özel ayrıştırıcı yazmak isterseniz GenericParser'dan türetip
PARSERS sözlüğüne banka adıyla ekleyin (config.yaml'daki `name` ile aynı olmalı).
"""

from __future__ import annotations

from .generic import GenericParser
from .pdf import PdfPasswordError, extract_text
from .tables import detect_kind, extract_table_text


class UnsupportedDocument(Exception):
    pass


SUPPORTED_EXTENSIONS = (".pdf", ".xls", ".xlsx", ".htm", ".html")


def extract_document_text(content: bytes, password: str | None = None) -> str:
    """PDF, Excel (.xls/.xlsx) veya HTML tablo ekinin metnini döndürür."""
    kind = detect_kind(content)
    if kind == "pdf":
        return extract_text(content, password)
    if kind in ("xls", "xlsx", "html"):
        return extract_table_text(content, kind)
    raise UnsupportedDocument("Desteklenmeyen dosya türü (PDF, Excel veya HTML tablo bekleniyordu).")

PARSERS: dict[str, type[GenericParser]] = {
    # "Garanti BBVA": GarantiParser,
}


def get_parser(bank_name: str, categories: dict[str, list[str]] | None = None) -> GenericParser:
    parser_cls = PARSERS.get(bank_name, GenericParser)
    return parser_cls(bank_name=bank_name, categories=categories)


__all__ = ["GenericParser", "PdfPasswordError", "SUPPORTED_EXTENSIONS", "UnsupportedDocument",
           "detect_kind", "extract_document_text", "extract_text", "get_parser", "PARSERS"]
