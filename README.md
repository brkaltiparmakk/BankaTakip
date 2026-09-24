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
  SQLite (yerel) veya Neon Postgres (Vercel)
        │
        ├──► Web paneli (Google ile giriş, grafikler, son ödeme tarihleri)
        └──► CLI: statements / list / summary
```

## Vercel'e kurulum (web paneli)

Panel Vercel'de çalışır; veriler Neon Postgres'te durur, mailler her sabah otomatik taranır.
Panele sadece izin verdiğiniz Google hesaplarıyla veya belirlediğiniz panel şifresiyle girilebilir.

### 1. Projeyi Vercel'e bağlayın
[vercel.com/new](https://vercel.com/new) → bu GitHub deposunu seçin → Framework: **Other** → Deploy.
(İlk deploy ortam değişkenleri olmadığı için panelde "Giriş ayarlanmamış" gösterir; normal.)

### 2. Veritabanı (Neon)
Vercel projesi → **Storage** → **Create Database** → **Neon** → projeye bağlayın.
Bu, `DATABASE_URL` değişkenini otomatik ekler. Tablolar ilk istekte kendiliğinden oluşur.

### 3. Google ile giriş (isteğe bağlı)
Hızlı başlamak için bu adımı atlayıp `PANEL_PASSWORD` ile şifreli giriş kullanabilirsiniz;
ikisi birlikte de açık olabilir.

1. [Google Cloud Console](https://console.cloud.google.com/apis/credentials) → yeni proje →
   **OAuth consent screen**: External, uygulama adı "BankaTakip", test kullanıcısı olarak kendi adresiniz.
2. **Credentials → Create credentials → OAuth client ID** → Web application.
3. **Authorized redirect URIs**: `https://<proje-adınız>.vercel.app/auth/callback`
4. Oluşan Client ID ve Client Secret'ı bir sonraki adımda kullanın.

### 4. Ortam değişkenleri
Vercel projesi → **Settings → Environment Variables** (tam liste `.env.example` içinde):

| Değişken | Değer |
|---|---|
| `GMAIL_EMAIL`, `GMAIL_APP_PASSWORD` | Gmail adresi ve uygulama şifresi |
| `ICLOUD_EMAIL`, `ICLOUD_APP_PASSWORD` | iCloud adresi ve uygulamaya özel parola |
| `GARANTI_PDF_PASSWORD` vb. | Şifreli ekstreler için PDF şifreleri |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | 3. adımdan |
| `PANEL_PASSWORD` | Şifreyle giriş için en az 12 karakter (Google yerine veya yanında) |
| `SESSION_SECRET` | En az 32 karakterlik rastgele metin |
| `ALLOWED_EMAILS` | Panele girebilecek Google adres(ler)i |
| `CRON_SECRET` | Rastgele bir metin (otomatik taramayı korur) |
| `APP_URL` | İsteğe bağlı, ör. `https://bankatakip.vercel.app` |

Değişkenleri ekledikten sonra **Deployments → Redeploy** yapın.

### 5. Kullanım
- Panele girip **Tarama → Şimdi tara**'ya basın. İlk taramada bir yıllık mail varsa süre sınırı
  nedeniyle birkaç kez basmanız gerekebilir; her seferinde kaldığı yerden devam eder.
- Sonrasında `vercel.json`'daki cron her gün 09:00'da (TR) yeni ekstreleri kendisi çeker.
- Son ödeme tarihine 3 gün kala Gmail (veya iCloud) hesabınızdan kendinize hatırlatma maili
  gider. `REMINDER_DAYS` ile gün sayısını değiştirebilir (0 = kapalı), `REMINDER_EMAIL` ile
  başka bir adrese yönlendirebilirsiniz.
- Banka listesini veya kategorileri değiştirmek için `BANKATAKIP_CONFIG` değişkenine
  `config.example.yaml` biçiminde YAML yazabilirsiniz; yoksa varsayılanlar kullanılır.

### Güvenlik notları
- Mail uygulama şifreleri yalnızca Vercel ortam değişkenlerinde durur, veritabanına yazılmaz.
  Uygulama maillere salt okunur bağlanır.
- `ALLOWED_EMAILS` dışındaki hiçbir hesap panele giremez; giriş ayarları eksikse API tüm
  isteklere kapalıdır.
- Uygulama şifresini iptal etmek isterseniz Google/Apple hesabınızdan tek tıkla silebilirsiniz.

## Yerelde kurulum

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                 # mail adresleri ve şifreler
cp config.example.yaml config.yaml   # isteğe bağlı: banka/kategori listesini özelleştirmek için
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

## Paneli yerelde çalıştırma

```bash
pip install -r requirements-dev.txt
AUTH_DISABLED=1 uvicorn bankatakip.web.app:app --reload   # http://localhost:8000
```

## Geliştirme

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Postgres testleri için: `TEST_DATABASE_URL=postgresql://... python -m pytest`.
Testler gerçek mail sunucusu kullanmaz; örnek (şifreli/şifresiz) ekstre PDF'leri üretip tüm
akışı sahte bir IMAP istemcisiyle dener.
