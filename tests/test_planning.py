"""Taksit, kredi, düzenli ödeme, bütçe, kural, bağlantı testi ve özet mailleri."""

from datetime import date, datetime
from decimal import Decimal

from bankatakip import sync as sync_mod
from bankatakip.config import MailAccount, load_config
from bankatakip.diagnostics import check_account, hint_for
from bankatakip.models import ParsedStatement, StatementSummary, Transaction
from bankatakip.parsers.notifications import (
    card_debt, info_kind, installments, loan_info, parse_notification,
)
from bankatakip.reminders import deliver, send_budget_alerts, send_weekly_summary, weekly_summary_text
from bankatakip.storage import Storage

TODAY = date(2026, 9, 24)
AXESS = ("Değerli Akbanklı, 5839 ile biten BURAK ALTIPARMAK adına ait Axess Asıl kartınızla 9 ay vadeli "
         "72,699.01 TL tutarında BILGISAYAR/TEKNOLOJI harcaması yapılmıştır. 180,389.55 TL limitiniz kalmıştır.")


def _statement(storage, rows, h, bank="Garanti BBVA"):
    storage.save_statement(ParsedStatement(bank=bank, summary=StatementSummary(), transactions=[
        Transaction(d, desc, Decimal(a), c) for d, desc, a, c in rows]), h, source="gmail")


def test_notification_installments_and_info_mails():
    tx = parse_notification(AXESS, "Kredi kartı harcamanız", datetime(2026, 3, 8), "Akbank")
    assert tx.installments == 9 and tx.amount == Decimal("72699.01")
    assert installments("tek çekim 100 TL") is None and installments("6 taksitli alışveriş") == 6

    assert info_kind("Kredi kartı güncel dönem borcunuz") == "kart_borcu"
    assert info_kind("Bireysel Kredi Borç Bilgileriniz Hakkında") == "kredi"
    assert info_kind("Akbank Günlük Bülten") is None
    assert card_debt("5839 ile biten kartınızın güncel dönem borcu 12.345,67 TL'dir.") == Decimal("12345.67")
    info = loan_info("Kalan anapara borcunuz: 250.000,00 TL. Kalan taksit sayısı: 14. "
                     "Aylık taksit tutarı 50.172,75 TL")
    assert info == {"remaining_debt": Decimal("250000.00"), "remaining_installments": 14,
                    "monthly": Decimal("50172.75")}


def test_installment_plan(tmp_path):
    storage = Storage(tmp_path / "p.db")
    for d, amount, n in [(date(2026, 3, 8), "900", 9), (date(2026, 8, 20), "300", 3), (date(2025, 1, 1), "600", 6)]:
        tx = Transaction(d, "ALIŞVERİŞ", Decimal(amount), "Elektronik", installments=n)
        storage.add_notification("Akbank", "icloud", tx)
    # iptal edilen taksitli alışveriş sayılmaz
    storage.add_notification("Akbank", "icloud", Transaction(date(2026, 9, 1), "İPTAL", Decimal("1200"), None, installments=4))
    storage.add_notification("Akbank", "icloud", Transaction(date(2026, 9, 2), "İPTAL", Decimal("-1200"), None))

    plan = storage.installment_plan(TODAY, months=4)
    # Mart alışverişi: Nisan-Aralık 9 taksit, Eylül'e kadar 5'i ödendi. Ağustos: Eylül-Kasım 3 taksit.
    items = {i["total"]: i for i in plan["items"]}
    assert set(items) == {Decimal("900"), Decimal("300")}          # 2025'teki bitmiş
    assert items[Decimal("900")]["paid"] == 5 and items[Decimal("900")]["remaining"] == 4
    assert items[Decimal("900")]["last_month"] == "2026-12"
    assert items[Decimal("300")]["paid"] == 0
    assert [m["total"] for m in plan["months"]] == [Decimal("200"), Decimal("200"), Decimal("200"), Decimal("100")]
    assert plan["this_month"] == Decimal("200") and plan["remaining_total"] == Decimal("700")


def test_loans(tmp_path):
    storage = Storage(tmp_path / "l.db")
    _statement(storage, [(date(2026, m, 22), f"D4-(BİREYSEL AMAÇLI KREDİ TAHS.) {m}", "50172.75", "Kredi Ödemesi")
                         for m in (6, 7, 8)], "k")
    [loan] = storage.loans_overview(TODAY)
    assert loan["paid"] == 3 and loan["monthly"] == Decimal("50172.75") and loan["remaining"] is None
    assert loan["next_payment_month"] == "2026-09"

    assert storage.update_loan(loan["id"], None, 12, None)
    [loan] = storage.loans_overview(TODAY)
    assert loan["remaining"] == 9 and loan["end_month"] == "2027-05"
    assert loan["remaining_estimate"] == Decimal("50172.75") * 9

    # bankanın borç bilgisi maili taksit sayısından önceliklidir; sonraki ödemeler düşülür
    storage.update_loan_info("Garanti BBVA", "2026-07-01", Decimal("300000"), 10, None)
    [loan] = storage.loans_overview(TODAY)
    assert loan["remaining"] == 8 and loan["remaining_debt"] == "300000"


def test_recurring_and_budgets(tmp_path):
    storage = Storage(tmp_path / "r.db")
    rows = []
    for m in (5, 6, 7, 8, 9):
        rows += [(date(2026, m, 3), f"NETFLIX.COM {m}123", "229.99", "Abonelik"),
                 (date(2026, m, 10), "YEMEK", str(100 * m), "Yeme-İçme"),         # tutar değişken
                 (date(2026, m, 22), "KREDI TAHS", "5000", "Kredi Ödemesi")]       # krediler ayrı
    _statement(storage, rows, "r")
    [rec] = storage.recurring(TODAY)
    assert rec["category"] == "Abonelik" and rec["average"] == Decimal("229.99")
    assert rec["months"] == 5 and rec["next_month"] == "2026-10"

    storage.set_budget("Yeme-İçme", Decimal("1000"))
    storage.set_budget("Abonelik", Decimal("200"))
    status = {b["category"]: b for b in storage.budget_status("2026-09")}
    assert status["Yeme-İçme"]["spent"] == Decimal("900") and status["Abonelik"]["ratio"] > 1

    sent = []
    config = load_config(tmp_path / "yok.yaml")
    config.accounts = [MailAccount("icloud", "icloud", "ben@icloud.com", "X_PW", "imap.mail.me.com",
                                   password_value="abcd-efgh-ijkl-mnop")]
    assert send_budget_alerts(config, storage, TODAY, send=lambda a, m: sent.append(m)) == ["Abonelik", "Yeme-İçme"]
    assert "BÜTÇE AŞILDI" in sent[0].get_content()
    assert send_budget_alerts(config, storage, TODAY, send=lambda a, m: sent.append(m)) == []  # bir kez
    storage.set_budget("Abonelik", None)
    assert [b["category"] for b in storage.budget_status("2026-09")] == ["Yeme-İçme"]


def test_weekly_summary_and_sender_fallback(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "w.db")
    _statement(storage, [(date(2026, 9, 15), "MIGROS", "400", "Market"),
                         (date(2026, 9, 9), "MIGROS", "200", "Market")], "w")
    subject, body = weekly_summary_text(storage, date(2026, 9, 21))   # pazartesi
    assert subject == "Haftalık özet: 400,00 TL harcama"
    assert "Önceki haftaya göre: +100%" in body and "- Market: 400,00 TL" in body

    config = load_config(tmp_path / "yok.yaml")
    config.accounts = [MailAccount("gmail", "gmail", "ben@gmail.com", "G_PW", "imap.gmail.com", password_value="x"),
                       MailAccount("icloud", "icloud", "ben@icloud.com", "I_PW", "imap.mail.me.com", password_value="y")]
    used = []

    def send(account, message):
        if account.provider == "gmail":
            raise OSError("535 Username and Password not accepted")
        used.append((account.name, message["From"], message["To"]))

    assert send_weekly_summary(config, storage, date(2026, 9, 22), send=send) is None     # pazartesi değil
    assert send_weekly_summary(config, storage, date(2026, 9, 21), send=send).startswith("Haftalık özet")
    assert used == [("icloud", "ben@icloud.com", "ben@gmail.com")]                          # gmail yerine icloud
    assert send_weekly_summary(config, storage, date(2026, 9, 21), send=send) is None      # haftada bir
    assert send_weekly_summary(config, storage, date(2026, 9, 21), send=send, force=True)

    def fail(account, message):
        raise OSError("kapalı")
    try:
        deliver(config, lambda s, t: None, fail)
        raise AssertionError("hata bekleniyordu")
    except RuntimeError as exc:
        assert "icloud" in str(exc)


def test_swapped_env_account_and_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_EMAIL", "abcdefghijklmnop")          # şifre adres alanına girilmiş
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "ben@gmail.com")
    monkeypatch.delenv("ICLOUD_EMAIL", raising=False)
    [acc] = load_config(tmp_path / "yok.yaml").accounts
    assert acc.email == "ben@gmail.com" and acc.password == "abcdefghijklmnop"
    assert "yer değiştirmiş" in acc.notes[0]

    result = check_account(acc, imap=lambda a: "ok", smtp=lambda a: None)
    assert result["password_length"] == 16 and result["imap"]["ok"] and result["smtp"]["ok"]
    assert result["warnings"] == acc.notes

    def bad(a):
        raise Exception("b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'")
    monkeypatch.setenv("GMAIL_EMAIL", "ben@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "normal-sifrem")
    [acc] = load_config(tmp_path / "yok.yaml").accounts
    result = check_account(acc, imap=bad, smtp=bad)
    assert not result["imap"]["ok"] and "uygulama şifresi" in result["imap"]["hint"]
    assert any("16 harf" in w for w in result["warnings"])
    assert hint_for("Application-specific password required") is not None


def test_rules_and_backfill(tmp_path, config):
    storage = Storage(tmp_path / "k.db")
    _statement(storage, [(date(2026, 9, 1), "ABC KIRTASIYE LTD", "50", None),
                         (date(2026, 9, 2), "MIGROS", "10", "Market")], "k")
    storage.add_rule("abc kırtasiye", "Eğitim")
    parser = sync_mod.get_parser("Garanti BBVA", config.categories)
    resolver = sync_mod.make_resolver(parser, config, storage)
    assert resolver.resolve("ABC KIRTASIYE LTD SUBE 2") == "Eğitim"
    assert resolver.resolve("MIGROS") == "Market"

    # eski bildirimlerin taksit sayısı mail günlüğünden doldurulur
    storage.add_notification("Akbank", "icloud", Transaction(date(2026, 3, 8), "BILGISAYAR/TEKNOLOJI", Decimal("72699.01")))
    storage.log_mail("icloud", "<m>", "bildirim_eklendi", bank="Akbank", received_at=datetime(2026, 3, 8, 2, 38),
                     detail="BILGISAYAR/TEKNOLOJI: 72699.01 TL · Elektronik · " + AXESS)
    assert storage.backfill_installments(installments) == 1
    assert storage.installment_plan(TODAY)["items"][0]["installments"] == 9


def test_counterparty_and_card_debt_real_formats(tmp_path):
    from bankatakip.parsers.notifications import counterparty

    text = ("Değerli Akbanklı, 0729 Şube 5***8 no.lu hesabınıza YAKUP CİVELEK tarafından 19.500,00 TL "
            "HAVALE girişi olmuştur.")
    tx = parse_notification(text, "Hesabınıza nakit girişi olmuştur", datetime(2026, 8, 12, 12), "Akbank")
    assert tx.description == "YAKUP CİVELEK · Havale" and tx.amount == Decimal("-19500.00")
    assert counterparty("hesabınızdan BÜŞRA METİN tarafına 5.800,00 TL HAVALE çıkışı") == "BÜŞRA METİN · Havale"
    assert card_debt("5839 ile biten Axess kredi kartınızın 02.10.2026 tarihinde kesilecek ekstresine ait "
                     "güncel borç tutarı 19,395.90 TL'ye ulaşmıştır.") == Decimal("19395.90")

    # eski kayıtlar mail günlüğündeki örnekten düzeltilir
    storage = Storage(tmp_path / "c.db")
    storage.add_notification("Akbank", "icloud", Transaction(date(2026, 8, 12), "Hesaba para girişi", Decimal("-19500.00"), "Transfer"))
    storage.log_mail("icloud", "<n>", "bildirim_eklendi", bank="Akbank", received_at=datetime(2026, 8, 12, 12, 57),
                     detail="Hesaba para girişi: -19500.00 TL · Transfer · " + text)
    assert storage.backfill_counterparties(counterparty) == 1
    assert storage.list_transactions()[0]["description"] == "YAKUP CİVELEK · Havale"


def test_salary_api(tmp_path, config, monkeypatch):
    from fastapi.testclient import TestClient

    from bankatakip.web import app as web_app

    monkeypatch.setenv("AUTH_DISABLED", "1")
    monkeypatch.delenv("VERCEL", raising=False)
    storage = Storage(config.database)
    storage.add_notification("Akbank", "icloud", Transaction(date(2025, 6, 11), "Maaş ödemesi", Decimal(0), "Maaş"),
                             amount_auto=True)
    storage.add_notification("Akbank", "icloud", Transaction(date(2026, 9, 11), "Maaş ödemesi", Decimal(0), "Maaş"),
                             amount_auto=True)
    web_app.app.dependency_overrides[web_app.get_config] = lambda: config
    client = TestClient(web_app.app)
    h = {"X-Requested-With": "bankatakip"}
    try:
        assert client.get("/api/salary").json()["payments"] == 2
        assert client.put("/api/salary", json={"from_month": "2026-13", "amount": 1}, headers=h).status_code == 422
        assert client.put("/api/salary", json={"from_month": "2026-01", "amount": 80000}, headers=h).json()["updated"] == 2
        assert client.put("/api/salary", json={"from_month": "2024-01", "amount": 50000}, headers=h).json()["updated"] == 1
        s = client.get("/api/salary").json()
        assert s["total"] == 130000 and [e["from_month"] for e in s["entries"]] == ["2024-01", "2026-01"]
        report = client.get("/api/report?month=2026-09").json()
        assert report["kpi"]["income"] == 80000
    finally:
        web_app.app.dependency_overrides.clear()


ENPARA_BODY = ("Sayın Burak Altıparmak, Enpara.com Kredi Kartınızın 20/08/2026 tarihli ekstresini ekte bulabilirsiniz. "
               "Özet borç bilgileriniz ise aşağıdaki gibidir: | Ekstre borcu | 142,91 TL | | Minimum ödeme tutarı | "
               "29,00 TL | | Son ödeme tarihi | 31/08/2026 |")


def test_card_statement_mail_hints():
    from bankatakip.parsers.generic import GenericParser, apply_mail_hints

    parser = GenericParser("Enpara", {"Ödeme": ["ödeme"]})
    # PDF metni bozuk: kart işaretleri okunamıyor, "bakiye" geçtiği için hesap dökümü sanılıyor
    text = ("Kart No: **** **** **** 4321\nÖnceki dönem bakiye 0,00\n"
            "02.08.2026 KAHVEDE ART 110,00\n05.08.2026 Ödeme - Enpara.com Cep Şubesi 2.400,00\n"
            "06.08.2026 gün sonu bakiyesi 1.033,00\n")
    st = parser.parse(text)
    assert st.kind == "vadesiz" and len(st.transactions) == 2      # bakiye satırı işlem sayılmaz
    apply_mail_hints(st, parser, text, "20.08.2026 tarihli Enpara.com Kredi Kartı ekstreniz", ENPARA_BODY)
    assert st.kind == "kredi_karti" and st.account.key == "kart:4321"
    s = st.summary
    assert (s.period_debt, s.minimum_payment, s.due_date, s.statement_date) == (
        Decimal("142.91"), Decimal("29.00"), date(2026, 8, 31), date(2026, 8, 20))
    amounts = {t.description: t.amount for t in st.transactions}
    assert amounts == {"KAHVEDE ART": Decimal("110.00"), "Ödeme - Enpara.com Cep Şubesi": Decimal("-2400.00")}

    # hesap özeti maili (kart değil) değişmez
    other = parser.parse(text)
    apply_mail_hints(other, parser, text, "2024 Aralık ayı hesap özetiniz", "")
    assert other.kind == "vadesiz"


def test_requeue_misread_card_statements_and_uncategorized(tmp_path):
    from bankatakip.storage import rule_pattern

    storage = Storage(tmp_path / "q.db")
    received = datetime(2026, 8, 21, 10, 9)
    st = ParsedStatement(bank="Enpara", summary=StatementSummary(), kind="vadesiz", transactions=[
        Transaction(date(2026, 8, 2), "KAHVEDE ART", Decimal("110"), None),
        Transaction(date(2026, 8, 3), "KAHVEDE ART İZMİT", Decimal("60"), None),
        Transaction(date(2026, 8, 4), "SBX İZMİT ŞEKERPINAR DRI", Decimal("90"), None)])
    storage.save_statement(st, "h1", source="gmail", received_at=received)
    storage.mark_mail_processed("gmail", "<e>")
    storage.log_mail("gmail", "<e>", "eklendi", bank="Enpara", received_at=received,
                     subject="20.08.2026 tarihli Enpara.com Kredi Kartı ekstreniz")
    storage.add_notification("Akbank", "icloud", Transaction(date(2026, 8, 5), "Banka kartı harcaması", Decimal("50")))

    groups = storage.uncategorized_groups()
    assert [(g["pattern"], g["count"], g["total"]) for g in groups] == [
        ("KAHVEDE ART", 2, Decimal("170")), ("SBX İZMİT ŞEKERPINAR", 1, Decimal("90"))]
    assert storage.uncategorized_count() == {"total": 4, "generic": 1}
    assert rule_pattern("FAST8994-BURAK ALTIPARMAK- Para Transferi") == "ALTIPARMAK"

    wanted = lambda subject: "Kredi Kartı" in subject
    assert storage.requeue_statement_mails(wanted) == 1
    assert not storage.is_mail_processed("gmail", "<e>") and storage.list_statements() != []  # sadece Akbank kaldı
    assert all(t["bank"] == "Akbank" for t in storage.list_transactions())
    assert storage.requeue_statement_mails(wanted) == 0
