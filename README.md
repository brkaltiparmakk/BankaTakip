# BankaTakip

Gmail ve iCloud mail kutularınıza gelen banka ekstrelerini otomatik bulur, PDF eklerini
(şifreliyse şifresiyle) açar, içindeki işlemleri okuyup SQLite veritabanına kaydeder.
Sonra harcamalarınızı kategori ve ay bazında görebilirsiniz.

```
Gmail / iCloud (IMAP)
        │  gönderen + konu filtresi ("ekstre", "hesap özeti")
        ▼
  PDF ekleri indirilir ──► data/ekstreler/<Banka>/...
        │  pypdf + pdfplumber (şifreli PDF desteği)
        ▼
  Ayrıştırıcı (tarih, açıklama, tutar, dönem borcu, son ödeme tarihi)
        │  anahtar kelimeyle otomatik kategori
        ▼
  SQLite (data/bankatakip.db) ──► CLI: statements / list / summary
```

## Kurulum

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml   # mail adreslerinizi ve bankalarınızı yazın
cp .env.example .env                 # şifreleri yazın
```

### Mail erişimi (uygulama şifresi)

Normal hesap şifreniz IMAP'te çalışmaz, **uygulamaya özel şifre** gerekir:

- **Gmail:** Google Hesabı → Güvenlik → 2 Adımlı Doğrulama'yı açın →
  [Uygulama şifreleri](https://myaccount.google.com/apppasswords) → oluşan 16 haneli şifreyi
  `.env` içindeki `GMAIL_APP_PASSWORD` alanına yazın. Gmail ayarlarında IMAP açık olmalı.
- **iCloud:** [appleid.apple.com](https://appleid.apple.com) → Oturum Açma ve Güvenlik →
  Uygulamaya özel parolalar → oluşan şifreyi `ICLOUD_APP_PASSWORD` alanına yazın.
  `email` alanına @icloud.com (veya @me.com) adresinizi yazın.

Bağlantı salt okunur yapılır; mailler "okundu" olarak işaretlenmez, silinmez.

### Şifreli ekstreler

Çoğu banka ekstre PDF'ini şifreler (genelde TC kimlik no'nun bir kısmı veya doğum tarihi).
Her bankanın şifresini `.env` içine yazın; `config.yaml`'daki `pdf_password_env` ile eşleşir.
Şifre tanımlı değilse o mail işlenmiş sayılmaz, şifreyi ekleyip `sync`'i tekrar çalıştırınca işlenir.

## Kullanım

```bash
python -m bankatakip sync                      # mailleri tara, yeni ekstreleri işle
python -m bankatakip sync --since 2026-01-01   # belirli tarihten itibaren
python -m bankatakip statements                # ekstreler: dönem borcu, son ödeme tarihi
python -m bankatakip list --since 2026-08-01   # işlemler
python -m bankatakip list --category Market
python -m bankatakip summary                   # aylık, kategori bazında harcama
python -m bankatakip import ekstre.pdf --bank "Garanti BBVA"   # elle PDF ekleme
```

Aynı mail veya aynı PDF iki kez işlenmez; `sync`'i istediğiniz sıklıkla çalıştırabilirsiniz
(ör. cron ile her sabah).

## Bankaya özel ayrıştırıcı

`GenericParser`, tarih ile başlayıp Türkçe biçimli tutar (`1.234,56`) içeren satırları işlem
olarak okur; "Dönem Borcu", "Asgari Ödeme", "Son Ödeme Tarihi", "Hesap Kesim Tarihi"
alanlarını da bulur. Bir bankanın ekstresi bu yapıya uymuyorsa:

1. Ekstrenin metnine bakın:
   `python -c "from bankatakip.parsers import extract_text; print(extract_text(open('x.pdf','rb').read(), 'ŞİFRE'))"`
2. `bankatakip/parsers/` altında `GenericParser`'dan türeyen bir sınıf yazıp `parse_line`
   (ve gerekirse `parse_summary`) metodunu ezin.
3. `bankatakip/parsers/__init__.py` içindeki `PARSERS` sözlüğüne banka adıyla ekleyin.

Tutarlarda işaret: pozitif = harcama, negatif = ödeme/iade (`2.000,00+` veya `-2.000,00`).

## Geliştirme

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Testler gerçek mail sunucusu kullanmaz; örnek (şifreli/şifresiz) ekstre PDF'leri üretip tüm
akışı sahte bir IMAP istemcisiyle dener.
