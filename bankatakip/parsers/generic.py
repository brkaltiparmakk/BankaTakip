"""Türk bankalarının ekstreleri için genel (sezgisel) ayrıştırıcı.

Ekstre metnindeki her satırı tarar; tarih ile başlayıp Türkçe biçimli bir tutar
(1.234,56) içeren satırları işlem olarak kabul eder. Bankaya özel format gerekirse
GenericParser'dan türetilip parse_line / parse_summary ezilebilir.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from ..models import AccountRef, ParsedStatement, StatementSummary, Transaction

_TR_FOLD = str.maketrans("İIıÖöÜüŞşÇçĞğ", "iiioouussccgg")

MONTHS_TR = {
    "ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6,
    "temmuz": 7, "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12,
}

DATE_NUMERIC = r"\d{1,2}[./-]\d{1,2}[./-](?:\d{4}|\d{2})"
DATE_TEXT = r"\d{1,2}\s+(?:" + "|".join(MONTHS_TR) + r")\s+\d{4}"
DATE_RE = re.compile(rf"(?:{DATE_NUMERIC}|{DATE_TEXT})")
AMOUNT_RE = re.compile(r"(?<![\d,.])([-+]?)(\d{1,3}(?:\.\d{3})*|\d+),(\d{2})(?![\d])\s*([-+])?")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?\s+")
# Kredi kartı ekstresinde olan, vadesiz hesap dökümünde olmayan ifadeler. ("asgari ödeme" gibi
# ifadeler hesap dökümündeki işlem açıklamalarında da geçebildiği için burada yok.)
CARD_MARKERS = ("son odeme tarihi", "donem borcu", "ekstre borcu", "hesap kesim tarihi")
CARD_LAST4_RE = re.compile(r"(?:\*{4}\s*|\*{2,}\s*)(\d{4})\b|\b(\d{4})\s*(?:'?(?:le|la|ile|ila))?\s+(?:ile\s+)?biten")
IBAN_RE = re.compile(r"\btr\s?\d{2}(?:\s?\d){20,24}")
ACCOUNT_NO_RE = re.compile(r"\b(\d{3,4})\s*-\s*(\d{5,8})\b")
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
    "period_debt": [r"donem\s+borcu", r"ekstre\s+borcu", r"toplam\s+borc", r"hesap\s+ozeti\s+borcu"],
    "minimum_payment": [r"asgari\s+odeme(?:\s+tutari)?", r"minimum\s+odeme(?:\s+tutari)?",
                        r"en\s+az\s+odeme(?:\s+tutari)?"],
    "available_limit": [r"kullanilabilir\s+(?:kart\s+)?limit(?:iniz)?"],
    "due_date": [r"son\s+odeme\s+tarihi"],
    "statement_date": [r"hesap\s+kesim\s+tarihi", r"ekstre\s+tarihi", r"kesim\s+tarihi"],
}


def infer_account_flip(transactions: list[Transaction]) -> bool | None:
    """Bakiye sütunu varsa tutarların işaret yönünü bakiyeden doğrular.

    Hesap dökümlerinde bakiye_i = bakiye_(i-1) + tutar_i olur (çıkan para eksi). Satırlar eskiden
    yeniye ya da yeniden eskiye sıralı olabilir; iki durum da denenir. True: çıkan para eksi yazılmış
    (çevrilmeli), False: çıkan para artı yazılmış, None: bakiyeden anlaşılamadı.
    """
    signed_out_negative = signed_out_positive = 0
    for prev, cur in zip(transactions, transactions[1:]):
        if prev.balance is None or cur.balance is None:
            continue
        diff = cur.balance - prev.balance
        if diff in (cur.amount, -prev.amount):
            signed_out_negative += 1
        elif diff in (-cur.amount, prev.amount):
            signed_out_positive += 1
    if signed_out_negative == signed_out_positive:
        return None
    return signed_out_negative > signed_out_positive


def detect_account(text: str, kind: str, bank: str) -> AccountRef:
    """Metinden hesap/kart numarasını bulur; bulamazsa bankanın varsayılan hesabı döner."""
    folded = tr_fold(text)
    if kind == "kredi_karti":
        m = CARD_LAST4_RE.search(folded)
        if m:
            last4 = m.group(1) or m.group(2)
            return AccountRef("kredi_karti", f"kart:{last4}", f"{bank} Kredi Kartı •{last4}")
        return AccountRef("kredi_karti", "kart", f"{bank} Kredi Kartı")
    m = IBAN_RE.search(folded)
    if m:
        digits = re.sub(r"\D", "", m.group(0))
        return AccountRef("vadesiz", f"vadesiz:{digits[-7:]}", f"{bank} Vadesiz •{digits[-4:]}")
    m = ACCOUNT_NO_RE.search(folded)
    if m:
        return AccountRef("vadesiz", f"vadesiz:{m.group(2)}", f"{bank} Vadesiz •{m.group(2)[-4:]}")
    return AccountRef("vadesiz", "vadesiz", f"{bank} Vadesiz")


def _keyword_pattern(keyword: str) -> re.Pattern:
    """Anahtar kelime bir kelimenin başında eşleşmeli ("bim" → "BİM", "BIM A.Ş." ama
    "IBIMAX" değil). 3 harf ve daha kısa kelimeler tam kelime olarak aranır ("bp")."""
    keyword = tr_fold(keyword.strip())
    end = r"(?![a-z0-9])" if len(keyword) <= 3 else ""
    return re.compile(r"(?<![a-z0-9])" + re.escape(keyword) + end)


class GenericParser:
    bank_name = "Bilinmeyen"

    def __init__(self, bank_name: str | None = None, categories: dict[str, list[str]] | None = None):
        if bank_name:
            self.bank_name = bank_name
        self.categories = {
            cat: [_keyword_pattern(k) for k in keywords if k.strip()]
            for cat, keywords in (categories or {}).items()
        }

    def parse(self, text: str) -> ParsedStatement:
        statement = ParsedStatement(bank=self.bank_name, summary=self.parse_summary(text))
        for line in text.splitlines():
            tx = self.parse_line(line)
            if tx is not None:
                statement.transactions.append(tx)

        flip = infer_account_flip(statement.transactions)
        if flip is None:  # bakiye sütunundan anlaşılamadı: metindeki ifadelere bak
            flip = self.is_account_statement(text)
        if self.is_account_statement(text) or flip:
            statement.kind = "vadesiz"
            # Hesap dökümünde "dönem borcu" gibi kart alanları anlamsızdır
            statement.summary = StatementSummary(statement_date=statement.summary.statement_date)
        if flip:
            # Vadesiz hesapta çıkan para eksi yazılır; uygulamada harcama artı olduğu için çevir
            for tx in statement.transactions:
                tx.amount = -tx.amount
        for tx in statement.transactions:
            tx.category = self.categorize(tx.description)
        statement.account = detect_account(text, statement.kind, self.bank_name)
        return statement

    @staticmethod
    def is_account_statement(text: str) -> bool:
        """Vadesiz hesap hareket dökümü mü (kredi kartı ekstresi değil)?"""
        folded = tr_fold(text)
        return "bakiye" in folded and not any(marker in folded for marker in CARD_MARKERS)

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
        rest = TIME_RE.sub("", rest, count=1)  # "04.08.2026 12:50 ..." gibi satırlarda saat
        amount_match = AMOUNT_RE.search(rest)
        if not amount_match:
            return None
        description = rest[: amount_match.start()].strip(" -:|")
        if not description or tr_fold(description).startswith(("son odeme", "hesap kesim")):
            return None
        amount = _amount_from_match(amount_match)
        if amount is None:
            return None
        # Hesap dökümlerinde tutarın ardından gelen ilk tutar işlem sonrası bakiyedir
        balance_match = AMOUNT_RE.search(rest, amount_match.end())
        balance = _amount_from_match(balance_match) if balance_match else None
        return Transaction(date=tx_date, description=" ".join(description.split()), amount=amount,
                           balance=balance)

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
        for category, patterns in self.categories.items():
            if any(p.search(folded) for p in patterns):
                return category
        return None
