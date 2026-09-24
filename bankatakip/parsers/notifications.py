"""Bankaların anlık işlem bildirim maillerinden ("Kart harcamanız" vb.) işlem çıkarır.

Bazı bankalar (ör. Akbank) ekstreyi ek olarak göndermez ama her harcama için ayrı bir
bildirim maili atar. Bu mailler tek bir işlem içerir: tutar, işyeri ve tarih.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from ..models import Transaction
from .generic import AMOUNT_RE, DATE_RE, _amount_from_match, parse_date, tr_fold

# (konuda aranacak ifade, işaret): +1 harcama, -1 iade/iptal/gelir. Sıra önemli: özelden genele.
NOTIFICATION_RULES = [
    ("harcamaniz iptal", -1),
    ("harcama iptal", -1),
    ("harcamaniz", 1),
    ("maas odemeniz", -1),
]

# Tutarın hemen yanında para birimi olan eşleşmeler önceliklidir ("1.250,00 TL")
AMOUNT_WITH_CURRENCY = re.compile(AMOUNT_RE.pattern + r"\s*(?:tl|try|₺)", re.IGNORECASE)

# "... MIGROS KADIKOY isyerinden ..." kalıbında işyeri adı "isyerinden" kelimesinin hemen öncesindedir;
# başlangıcı, geriye doğru en yakın ayraçtır (virgül, satır sonu, saat, "tarihinde", "ile" ...).
MERCHANT_BEFORE = re.compile(r"\s+(?:uye\s+)?isyerin(?:de|den)\b")
MERCHANT_START = re.compile(r"(?:[,;\n]|\btarihinde\b|\bitibariyla\b|\bile\b|\d{1,2}:\d{2}(?:'?[a-z]+)?)\s*")
# "Isyeri: MIGROS" / "Uye isyeri: MIGROS" / "Aciklama: MIGROS"
MERCHANT_LABEL = re.compile(r"(?:uye\s+isyeri|isyeri(?:\s+adi)?|aciklama)\s*[:\-]\s*([^\n]{2,60})")


def notification_sign(subject: str) -> int | None:
    folded = tr_fold(subject)
    for phrase, sign in NOTIFICATION_RULES:
        if phrase in folded:
            return sign
    return None


def _amount(text: str) -> Decimal | None:
    m = AMOUNT_WITH_CURRENCY.search(text) or AMOUNT_RE.search(text)
    if not m:
        return None
    value = _amount_from_match(m)
    return abs(value) if value is not None else None


def _clean(value: str) -> str:
    return " ".join(value.split()).strip(" :-.")


def _merchant(text: str) -> str | None:
    folded = tr_fold(text)  # konumlar orijinal metinle aynı
    m = MERCHANT_LABEL.search(folded)
    if m:
        value = _clean(text[m.start(1): m.end(1)])
        if value:
            return value
    m = MERCHANT_BEFORE.search(folded)
    if m:
        prefix = folded[: m.start()]
        starts = [d.end() for d in MERCHANT_START.finditer(prefix)]
        begin = max(starts[-1] if starts else 0, m.start() - 60)
        value = _clean(text[begin: m.start()])
        if value:
            return value
    return None


def parse_notification(text: str, subject: str, received: datetime | None) -> Transaction | None:
    sign = notification_sign(subject)
    if sign is None:
        return None
    amount = _amount(text)
    if amount is None:
        return None
    dm = DATE_RE.search(tr_fold(text))
    tx_date = parse_date(text[dm.start(): dm.end()]) if dm else None
    if tx_date is None and received is not None:
        tx_date = received.date()
    if tx_date is None:
        return None
    description = _merchant(text) or subject.strip()
    return Transaction(date=tx_date, description=description, amount=amount * sign)
