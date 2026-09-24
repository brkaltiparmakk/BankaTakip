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
    balance: Decimal | None = None   # işlemden sonraki hesap bakiyesi (dökümde varsa)
    account: "AccountRef | None" = None  # bildirimlerde işlemin ait olduğu hesap/kart
    weak: bool = False  # kurallar tutarı buldu ama açıklamayı bulamadı
    installments: int | None = None  # taksitli alışverişte taksit sayısı ("9 ay vadeli")


@dataclass
class AccountRef:
    """Bir banka hesabı veya kart. key banka içinde tekildir (ör. "kart:5839", "vadesiz:6644898")."""
    kind: str  # "vadesiz" | "kredi_karti"
    key: str
    name: str


@dataclass
class StatementSummary:
    period_debt: Decimal | None = None      # Dönem borcu
    minimum_payment: Decimal | None = None  # Asgari ödeme
    due_date: date | None = None            # Son ödeme tarihi
    statement_date: date | None = None      # Hesap kesim tarihi
    available_limit: Decimal | None = None  # Kullanılabilir kart limiti


@dataclass
class ParsedStatement:
    bank: str
    summary: StatementSummary = field(default_factory=StatementSummary)
    transactions: list[Transaction] = field(default_factory=list)
    account: AccountRef | None = None
    kind: str = "kredi_karti"  # "kredi_karti" ekstresi veya "vadesiz" hesap dökümü
    saved_transactions: int | None = None  # kaydedilirken tekrar olmayan işlem sayısı
