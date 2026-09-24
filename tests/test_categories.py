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


def test_akbank_sectors_map_to_readable_categories(tmp_path):
    config = load_config(tmp_path / "yok.yaml")
    resolver = CategoryResolver(get_parser("Akbank", config.categories), list(config.categories))
    expected = {
        "KUAFOR VE GUZELLIK MERKEZI": "Kişisel Bakım", "EGLENCE": "Eğlence", "SINEMA/TIYATRO": "Eğlence",
        "BILGISAYAR/TEKNOLOJI": "Elektronik", "TELEKOMUNIKASYON": "Fatura", "KAMU": "Vergi ve Kamu",
        "OTEL": "Seyahat", "HAVAYOLLARI": "Seyahat", "KITAP/KIRTASIYE": "Eğitim",
    }
    for sector, category in expected.items():
        assert resolver.resolve(sector, sector) == category, sector
    # "taksi" TAKSİTLİ'ye, "harç" harcamaya uymaz
    assert resolver.resolve("TAKSİTLİ AVANS HES.KULL Taksitli Avans Hesap") == "Transfer"
    assert resolver.resolve("TAKSI DURAGI") == "Ulaşım"
    assert resolver.resolve("Banka kartı harcaması") is None
    assert pretty_name("SINEMA/TIYATRO") == "Sinema / Tiyatro"


def test_recategorize_keeps_manual_choices(tmp_path):
    from datetime import date

    from bankatakip.categories import recategorizer
    from bankatakip.models import ParsedStatement, StatementSummary, Transaction
    from bankatakip.storage import Storage

    config = load_config(tmp_path / "yok.yaml")
    storage = Storage(tmp_path / "t.db")
    rows = [("EGLENCE", "Eglence"), ("TAKSİTLİ AVANS HES.KULL", "Ulaşım"), ("MIGROS", "Market"),
            ("ABC LTD", "Sağlık"), ("NAKLIYE", "Nakliye")]
    storage.save_statement(ParsedStatement(bank="Akbank", summary=StatementSummary(), transactions=[
        Transaction(date(2026, 9, 1), d, Decimal("10"), c) for d, c in rows]), "h", source="gmail")
    manual = storage.list_transactions(search="nakliye")[0]["id"]
    storage.update_transaction_category(manual, "Nakliye")

    parser = get_parser("Akbank", config.categories)
    assert storage.recategorize(recategorizer(parser, config.categories)) == 2
    got = {t["description"]: t["category"] for t in storage.list_transactions()}
    assert got == {"EGLENCE": "Eğlence", "TAKSİTLİ AVANS HES.KULL": "Transfer", "MIGROS": "Market",
                   "ABC LTD": "Sağlık", "NAKLIYE": "Nakliye"}
    storage.close()
