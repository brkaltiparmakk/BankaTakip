"""Bankaların anlık işlem bildirim maillerinden ("Kart harcamanız" vb.) işlem çıkarır.

Bazı bankalar (ör. Akbank) ekstreyi ek olarak göndermez ama her harcama için ayrı bir
bildirim maili atar. Bu mailler tek bir işlem içerir: tutar, işyeri ve tarih.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from ..models import AccountRef, Transaction
from .generic import AMOUNT_RE, DATE_RE, _amount_from_match, detect_account, parse_date, tr_fold

# (konuda aranacak ifade, işaret): +1 harcama, -1 iade/iptal/gelir. Sıra önemli: özelden genele.
NOTIFICATION_RULES = [
    ("harcamaniz iptal", -1),
    ("harcama iptal", -1),
    ("harcamaniz", 1),
    ("maas odemeniz", -1),
    # Vadesiz hesap hareketleri (Akbank): "Hesabınıza nakit girişi olmuştur", "ATM'den para çekme işleminiz"
    ("nakit girisi", -1),
    ("nakit cikisi", 1),
    ("para yatirma", -1),
    ("para cekme", 1),
]
# Hesap hareketi bildirimleri: işyeri yoktur, açıklama yoksa bu adlar kullanılır ve harcama sayılmaz
MOVEMENT_DESCRIPTIONS = {
    "nakit girisi": "Hesaba para girişi",
    "nakit cikisi": "Hesaptan para çıkışı",
    "para yatirma": "ATM para yatırma",
    "para cekme": "ATM para çekme",
}
MOVEMENT_CATEGORY = "Transfer"

# Tutarın hemen yanında para birimi olan eşleşmeler önceliklidir ("1.250,00 TL")
AMOUNT_WITH_CURRENCY = re.compile(AMOUNT_RE.pattern + r"\s*(?:tl|try|₺)", re.IGNORECASE)

# "... MIGROS KADIKOY isyerinden ..." kalıbında işyeri adı "isyerinden" kelimesinin hemen öncesindedir;
# başlangıcı, geriye doğru en yakın ayraçtır (virgül, satır sonu, saat, "tarihinde", "ile" ...).
MERCHANT_BEFORE = re.compile(r"\s+(?:uye\s+)?isyerin(?:de|den)\b")
MERCHANT_START = re.compile(r"(?:[,;\n]|\btarihinde\b|\bitibariyla\b|\bile\b|\d{1,2}:\d{2}(?:'?[a-z]+)?)\s*")
# Akbank işyeri yerine sektör verir:
#   "... 120,00 TL tutarında BENZIN ISTASYONU harcaması yapılmıştır."
#   "... 512,40 TL tutarında GIDA VE MARKET kategorisinde temassız ödeme işlemi yapılmıştır."
#   "... yapılan 360,00 TL tutarlı EGLENCE harcaması iptal edilmiştir."
SECTOR_RE = re.compile(r"tutar(?:inda|li)\s+([^\n,.]{2,60}?)\s+(?:harcamasi|kategorisinde)")
# Kart türünü belirten, sektör olmayan ifadeler
GENERIC_SECTORS = {"banka karti", "kredi karti", "kart"}
# İngilizce biçimli tutar: "1,899.00 TL" (Akbank kredi kartı bildirimleri)
AMOUNT_EN = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})(?!\d)\s*(?:tl|try|₺)", re.IGNORECASE)
LIMIT_RE = re.compile(r"(\S+)\s*tl\s+limitiniz\s+kalmistir")
# "... güncel bakiyeniz 12.345,67 TL" (kullanılabilir bakiye ek hesap limitini içerebileceği için alınmaz)
BALANCE_RE = re.compile(r"(?<!kullanilabilir )\bbakiye\w*[^0-9]{0,30}?(?=[-+]?\d)")
# "Isyeri: MIGROS" / "Uye isyeri: MIGROS" / "Aciklama: MIGROS"
MERCHANT_LABEL = re.compile(r"(?:uye\s+isyeri|isyeri(?:\s+adi)?|aciklama)\s*[:\-]\s*([^\n]{2,60})")


def notification_sign(subject: str) -> int | None:
    folded = tr_fold(subject)
    for phrase, sign in NOTIFICATION_RULES:
        if phrase in folded:
            return sign
    return None


def _parse_any_amount(text: str) -> Decimal | None:
    """Türkçe ("1.899,00 TL") veya İngilizce ("1,899.00 TL") biçimli ilk tutar."""
    m = AMOUNT_WITH_CURRENCY.search(text)
    if m:
        value = _amount_from_match(m)
        return abs(value) if value is not None else None
    m = AMOUNT_EN.search(text)
    if m:
        return Decimal(f"{m.group(1).replace(',', '')}.{m.group(2)}")
    m = AMOUNT_RE.search(text)
    if m:
        value = _amount_from_match(m)
        return abs(value) if value is not None else None
    return None


def _amount(text: str) -> Decimal | None:
    return _parse_any_amount(text)


def has_amount(text: str) -> bool:
    return _parse_any_amount(text) is not None


def remaining_limit(text: str) -> Decimal | None:
    """"199,387.16 TL limitiniz kalmıştır" → 199387.16"""
    folded = tr_fold(text)
    m = LIMIT_RE.search(folded)
    return _parse_any_amount(text[m.start(1): m.end(0)]) if m else None


def account_balance(text: str) -> Decimal | None:
    """"... hesabınızın güncel bakiyesi 12.345,67 TL" → 12345.67"""
    m = BALANCE_RE.search(tr_fold(text))
    if not m:
        return None
    window = text[m.end(): m.end() + 30]
    value = _parse_any_amount(window) if re.search(r"(?:tl|try|₺)", tr_fold(window)) else None
    if value is not None and window.lstrip().startswith("-"):
        value = -value
    return value


def movement_kind(subject: str) -> str | None:
    """Vadesiz hesap hareketi bildirimi mi ("nakit girisi", "para cekme" ...)?"""
    folded = tr_fold(subject)
    return next((k for k in MOVEMENT_DESCRIPTIONS if k in folded), None)


def notification_account(text: str, subject: str, bank: str) -> AccountRef:
    """Bildirimin ait olduğu hesap: kredi kartı harcaması kartın kendisine, banka kartı
    harcaması ve hesap hareketleri bankanın vadesiz hesabına yazılır. Bildirimlerde hesap
    numarası tutarlı yazılmadığı için vadesiz hareketler tek hesapta toplanır."""
    folded = tr_fold(subject + " " + text)
    is_credit = "kredi karti" in folded or "axess" in folded or "limitiniz" in folded
    if is_credit and not movement_kind(subject):
        return detect_account(text, "kredi_karti", bank)
    return AccountRef("vadesiz", "vadesiz", f"{bank} Vadesiz")


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


def _sector(text: str) -> str | None:
    m = SECTOR_RE.search(tr_fold(text))
    if not m:
        return None
    value = _clean(text[m.start(1): m.end(1)])
    return None if not value or tr_fold(value) in GENERIC_SECTORS else value


def _generic_description(text: str) -> str | None:
    folded = tr_fold(text)
    if "kredi karti harcamasi" in folded or "kredi karti kategorisinde" in folded:
        return "Kredi kartı harcaması"
    if "banka karti" in folded:
        return "Banka kartı harcaması"
    return None


def parse_notification(text: str, subject: str, received: datetime | None,
                       bank: str = "") -> Transaction | None:
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
    movement = movement_kind(subject)
    if movement:
        # Hesap hareketinde kategori ipucu "Transfer": açıklama başka bir kurala uymazsa harcama sayılmaz
        sector = MOVEMENT_CATEGORY
        description = _merchant(text) or MOVEMENT_DESCRIPTIONS[movement]
    else:
        sector = _sector(text)
        description = sector or _merchant(text) or _generic_description(text)
    tx = Transaction(date=tx_date, description=description or subject.strip(), amount=amount * sign,
                     sector=sector, account=notification_account(text, subject, bank) if bank else None)
    # İşyeri/sektör bulunamadıysa sonuç zayıftır: yapay zeka açıksa ona sorulur
    tx.weak = description is None
    return tx
