from datetime import datetime
from decimal import Decimal

from bankatakip.categories import CategoryResolver, pretty_name
from bankatakip.config import load_config
from bankatakip.parsers import get_parser
from bankatakip.parsers.notifications import parse_notification

AKBANK = ("Bu mail'i görüntüleyemiyorsanız lütfen tıklayınız. Değerli Akbanklı, 1206 ile biten Akbank "
          "Kart'ınla, 120,00 TL tutarında BENZIN ISTASYONU harcaması yapılmıştır. Detaylı hesap "
          "hareketlerinize ulaşmak için Akbank Mobil'e giriş yapabilirsiniz. Saygılarımızla, Akbank")


def test_akbank_sector_notification():
    tx = parse_notification(AKBANK, "Akbank Kart harcamanız", datetime(2026, 9, 24, 12, 26))
    assert tx.description == "BENZIN ISTASYONU" and tx.sector == "BENZIN ISTASYONU"
    assert tx.amount == Decimal("120.00") and tx.date.isoformat() == "2026-09-24"


def test_default_categories_cover_sector(tmp_path):
    config = load_config(tmp_path / "yok.yaml")
    resolver = CategoryResolver(get_parser("Akbank", config.categories), list(config.categories))
    assert resolver.resolve("BENZIN ISTASYONU", "BENZIN ISTASYONU") == "Akaryakıt"
    assert resolver.resolve("ECZANE", "ECZANE") == "Sağlık"


def test_unknown_sector_creates_category_once():
    resolver = CategoryResolver(get_parser("X", {"Market": ["migros"]}), ["Market", "Kuyumcu"])
    assert resolver.resolve("MIGROS", None) == "Market"
    assert resolver.resolve("KUYUMCU", "KUYUMCU") == "Kuyumcu"          # var olan yeniden kullanılır
    assert resolver.resolve("EVCIL HAYVAN", "EVCIL HAYVAN") == "Evcil Hayvan"
    assert resolver.resolve("PET SHOP", "evcil hayvan") == "Evcil Hayvan"  # farklı yazım, aynı kategori
    assert resolver.resolve("BILINMEYEN", None) is None                 # ipucu yoksa Diğer
    assert resolver.resolve("X", "Diğer") is None
    assert pretty_name("BENZIN ISTASYONU") == "Benzin İstasyonu"
