"""Ekstre ayrıştırıcıları.

Bir banka için özel ayrıştırıcı yazmak isterseniz GenericParser'dan türetip
PARSERS sözlüğüne banka adıyla ekleyin (config.yaml'daki `name` ile aynı olmalı).
"""

from __future__ import annotations

from .generic import GenericParser
from .pdf import PdfPasswordError, extract_text

PARSERS: dict[str, type[GenericParser]] = {
    # "Garanti BBVA": GarantiParser,
}


def get_parser(bank_name: str, categories: dict[str, list[str]] | None = None) -> GenericParser:
    parser_cls = PARSERS.get(bank_name, GenericParser)
    return parser_cls(bank_name=bank_name, categories=categories)


__all__ = ["GenericParser", "PdfPasswordError", "extract_text", "get_parser", "PARSERS"]
