from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class Transaction:
    date: date
    description: str
    amount: Decimal  # pozitif: harcama/borç, negatif: ödeme/iade
    category: str | None = None
    # Bankanın bildirdiği sektör veya yapay zekanın önerdiği kategori ("BENZIN ISTASYONU");
    # hiçbir kategoriye uymazsa bu adla yeni kategori açılır
    sector: str | None = None


@dataclass
class StatementSummary:
    period_debt: Decimal | None = None      # Dönem borcu
    minimum_payment: Decimal | None = None  # Asgari ödeme
    due_date: date | None = None            # Son ödeme tarihi
    statement_date: date | None = None      # Hesap kesim tarihi


@dataclass
class ParsedStatement:
    bank: str
    summary: StatementSummary = field(default_factory=StatementSummary)
    transactions: list[Transaction] = field(default_factory=list)
