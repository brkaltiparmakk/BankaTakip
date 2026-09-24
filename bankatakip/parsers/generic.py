"""Türk bankalarının ekstreleri için genel (sezgisel) ayrıştırıcı.

Ekstre metnindeki her satırı tarar; tarih ile başlayıp Türkçe biçimli bir tutar
(1.234,56) içeren satırları işlem olarak kabul eder. Bankaya özel format gerekirse
GenericParser'dan türetilip parse_line / parse_summary ezilebilir.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from ..models import ParsedStatement, StatementSummary, Transaction

_TR_FOLD = str.maketrans("İIıÖöÜüŞşÇçĞğ", "iiioouussccgg")

MONTHS_TR = {
    "ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6,
    "temmuz": 7, "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12,
}

DATE_NUMERIC = r"\d{1,2}[./-]\d{1,2}[./-](?:\d{4}|\d{2})"
DATE_TEXT = r"\d{1,2}\s+(?:" + "|".join(MONTHS_TR) + r")\s+\d{4}"
DATE_RE = re.compile(rf"(?:{DATE_NUMERIC}|{DATE_TEXT})")
AMOUNT_RE = re.compile(r"(?<![\d,.])([-+]?)(\d{1,3}(?:\.\d{3})*|\d+),(\d{2})(?![\d])\s*([-+])?")
LINE_RE = re.compile(
    rf"^\s*(?P<date>{DATE_NUMERIC}|{DATE_TEXT})"
    rf"(?:\s+(?:{DATE_NUMERIC}))?"  # bazı ekstrelerde valör/ikinci tarih sütunu
    r"\s+(?P<rest>.+)$"
)


def tr_fold(text: str) -> str:
    """Türkçe karakterleri ASCII karşılığına indirip küçük harfe çevirir.

    Karakter karakter eşleme yaptığı için metin uzunluğu korunur; katlanmış metinde
    bulunan konumlar orijinal metinde de geçerlidir.
    """
    return text.translate(_TR_FOLD).lower()


def parse_amount(text: str) -> Decimal | None:
    m = AMOUNT_RE.search(text)
    if not m:
        return None
    return _amount_from_match(m)


def _amount_from_match(m: re.Match) -> Decimal | None:
    lead, integer, frac, trail = m.groups()
    try:
        value = Decimal(f"{integer.replace('.', '')}.{frac}")
    except InvalidOperation:
        return None
    if lead == "-" or trail == "-":
        value = -value
    elif trail == "+":
        # Kredi kartı ekstrelerinde "+" ile işaretlenen tutarlar ödeme/iadedir.
        value = -value
    return value


def parse_date(text: str) -> date | None:
    text = text.strip()
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2}|\d{4})", text)
    if m:
        day, month, year = (int(x) for x in m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})\s+(\w+)\s+(\d{4})", tr_fold(text))
        if not m or m.group(2) not in MONTHS_TR:
            return None
        day, month, year = int(m.group(1)), MONTHS_TR[m.group(2)], int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


SUMMARY_PATTERNS = {
    "period_debt": [r"donem\s+borcu", r"toplam\s+borc", r"hesap\s+ozeti\s+borcu"],
    "minimum_payment": [r"asgari\s+odeme(?:\s+tutari)?"],
    "due_date": [r"son\s+odeme\s+tarihi"],
    "statement_date": [r"hesap\s+kesim\s+tarihi", r"ekstre\s+tarihi", r"kesim\s+tarihi"],
}


class GenericParser:
    bank_name = "Bilinmeyen"

    def __init__(self, bank_name: str | None = None, categories: dict[str, list[str]] | None = None):
        if bank_name:
            self.bank_name = bank_name
        self.categories = {
            cat: [tr_fold(k) for k in keywords] for cat, keywords in (categories or {}).items()
        }

    def parse(self, text: str) -> ParsedStatement:
        statement = ParsedStatement(bank=self.bank_name, summary=self.parse_summary(text))
        for line in text.splitlines():
            tx = self.parse_line(line)
            if tx is not None:
                tx.category = self.categorize(tx.description)
                statement.transactions.append(tx)
        return statement

    def parse_line(self, line: str) -> Transaction | None:
        # Ay isimleri katlanmış metinle eşleştiği için regex katlanmış satırda çalışır,
        # değerler aynı konumlardan orijinal satırdan alınır.
        m = LINE_RE.match(tr_fold(line))
        if not m:
            return None
        tx_date = parse_date(m.group("date"))
        if tx_date is None:
            return None
        rest = line[m.start("rest"):]
        amount_match = AMOUNT_RE.search(rest)
        if not amount_match:
            return None
        description = rest[: amount_match.start()].strip(" -:|")
        if not description or tr_fold(description).startswith(("son odeme", "hesap kesim")):
            return None
        amount = _amount_from_match(amount_match)
        if amount is None:
            return None
        return Transaction(date=tx_date, description=" ".join(description.split()), amount=amount)

    def parse_summary(self, text: str) -> StatementSummary:
        folded = tr_fold(text)
        summary = StatementSummary()
        for field_name, patterns in SUMMARY_PATTERNS.items():
            for pattern in patterns:
                m = re.search(pattern + r"\s*[:\-]?\s*(?:\(?tl\)?\s*[:\-]?\s*)?", folded)
                if not m:
                    continue
                # Değeri orijinal metinden, etiketten sonraki kısa pencerede ara
                window = text[m.end(): m.end() + 40]
                if field_name.endswith("date"):
                    dm = DATE_RE.search(tr_fold(window))
                    value = parse_date(window[dm.start(): dm.end()]) if dm else None
                else:
                    value = parse_amount(window)
                if value is not None:
                    setattr(summary, field_name, value)
                    break
        return summary

    def categorize(self, description: str) -> str | None:
        folded = tr_fold(description)
        for category, keywords in self.categories.items():
            if any(k in folded for k in keywords):
                return category
        return None
