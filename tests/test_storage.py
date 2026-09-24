"""Storage testleri: SQLite her zaman, Postgres TEST_DATABASE_URL verilirse çalışır.

Örnek: TEST_DATABASE_URL=postgresql://postgres@localhost:5432/bt_test python -m pytest
"""

import os
from datetime import date
from decimal import Decimal

import pytest

from bankatakip.models import ParsedStatement, StatementSummary, Transaction
from bankatakip.storage import Storage

PG_URL = os.environ.get("TEST_DATABASE_URL")


def _reset_pg(url):
    import psycopg
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS transactions, statements, processed_mails, meta, mail_log, balance_snapshots, accounts, budgets, category_rules, loans CASCADE")
    from bankatakip import storage as storage_mod
    storage_mod._SCHEMA_READY.discard(url)


@pytest.fixture(params=["sqlite", "postgres"])
def storage(request, tmp_path):
    if request.param == "postgres":
        if not PG_URL:
            pytest.skip("TEST_DATABASE_URL tanımlı değil")
        _reset_pg(PG_URL)
        s = Storage(PG_URL)
    else:
        s = Storage(tmp_path / "t.db")
    yield s
    s.close()


def _statement(bank="Banka A"):
    return ParsedStatement(
        bank=bank,
        summary=StatementSummary(period_debt=Decimal("1500.25"), due_date=date(2026, 9, 30)),
        transactions=[
            Transaction(date(2026, 9, 1), "MIGROS", Decimal("100.50"), "Market"),
            Transaction(date(2026, 9, 2), "NETFLIX", Decimal("229.99"), None),
            Transaction(date(2026, 9, 3), "ODEME", Decimal("-500"), "Ödeme"),
        ],
    )


def test_roundtrip(storage):
    sid = storage.save_statement(_statement(), "hash1", source="gmail")
    assert sid is not None
    assert storage.save_statement(_statement(), "hash1", source="gmail") is None

    [st] = storage.list_statements()
    assert st["tx_count"] == 3 and st["due_date"] == "2026-09-30"
    assert Decimal(st["period_debt"]) == Decimal("1500.25")

    assert len(storage.list_transactions(search="migr")) == 1
    assert len(storage.list_transactions(category="Diğer")) == 1
    assert len(storage.list_transactions(since=date(2026, 9, 2), until=date(2026, 9, 2))) == 1
    assert len(storage.list_transactions(statement_id=sid)) == 3

    tx = storage.list_transactions(search="netflix")[0]
    assert storage.update_transaction_category(tx["id"], "Abonelik")
    summary = {(m, c): v for m, c, v in storage.monthly_summary()}
    assert summary == {("2026-09", "Market"): Decimal("100.50"), ("2026-09", "Abonelik"): Decimal("229.99")}

    assert storage.delete_statement(sid)
    assert storage.list_transactions() == [] and storage.list_statements() == []


def test_meta_and_mails(storage):
    assert storage.get_meta("x") is None
    storage.set_meta("x", "1")
    storage.set_meta("x", "2")
    assert storage.get_meta("x") == "2"

    assert not storage.is_mail_processed("gmail", "<m1>")
    storage.mark_mail_processed("gmail", "<m1>")
    storage.mark_mail_processed("gmail", "<m1>")
    assert storage.is_mail_processed("gmail", "<m1>")


def test_schema_comments_have_no_statement_separator():
    # Postgres şeması ';' ile bölünerek çalıştırılır; yorumlardaki ';' ifadeyi yarıda keser
    import re
    from bankatakip.storage import _TABLES
    assert not any(";" in c for c in re.findall(r"--[^\n]*", _TABLES))


def _spend_statement(month, items, bank="Banka A"):
    return ParsedStatement(
        bank=bank,
        summary=StatementSummary(),
        transactions=[Transaction(date(2026, month, day), desc, Decimal(amount), cat) for day, desc, amount, cat in items],
    )


def test_report(storage):
    storage.save_statement(_spend_statement(6, [(5, "MIGROS", "300", "Market")]), "h6", source="gmail")
    storage.save_statement(_spend_statement(7, [(5, "MIGROS", "100", "Market")]), "h7", source="gmail")
    storage.save_statement(_spend_statement(8, [
        (1, "MIGROS", "150", "Market"),
        (2, "MIGROS IADE", "-50", "Market"),          # iade harcamadan düşülür
        (3, "SHELL", "400", "Akaryakıt"),
        (3, "KART ODEMESI", "-1000", "Kart Ödemesi"),  # harcama sayılmaz
        (4, "ABC LTD", "90", None),
    ]), "h8", source="gmail")

    r = storage.report("2026-08")
    assert r["months"] == ["2026-06", "2026-07", "2026-08"]
    k = r["kpi"]
    assert k["spend"] == Decimal("590") and k["spend_prev"] == Decimal("100")
    assert k["spend_avg3"] == Decimal("200") and k["prev_month"] == "2026-07"
    assert k["daily_avg"] == Decimal("590") / 31
    cats = {c["name"]: c for c in r["categories"]}
    assert list(cats) == ["Akaryakıt", "Market", "Diğer"]
    assert cats["Market"]["total"] == Decimal("100") and cats["Market"]["count"] == 2
    assert cats["Market"]["prev"] == Decimal("100") and cats["Market"]["avg3"] == Decimal("200")
    assert cats["Akaryakıt"]["prev"] == 0
    assert r["merchants"][0]["description"] == "SHELL"
    assert {d["date"]: d["total"] for d in r["daily"]}["2026-08-03"] == Decimal("400")
    assert r["pivot"]["months"] == ["2026-06", "2026-07", "2026-08"]
    market = next(row for row in r["pivot"]["rows"] if row["name"] == "Market")
    assert market["values"] == [Decimal("300"), Decimal("100"), Decimal("100")]

    trend = storage.category_trend("Market", "2026-08", count=3)
    assert trend == [{"month": "2026-06", "total": Decimal("300")}, {"month": "2026-07", "total": Decimal("100")},
                     {"month": "2026-08", "total": Decimal("100")}]
    assert storage.category_trend("Diğer", "2026-08", count=1) == [{"month": "2026-08", "total": Decimal("90")}]


def test_planning_tables(storage):
    from datetime import datetime

    from bankatakip.parsers.notifications import installments

    storage.save_statement(_spend_statement(8, [(22, "KREDI TAHS", "5000", "Kredi Ödemesi"),
                                                (3, "NETFLIX", "229.99", "Abonelik")]), "p8", source="gmail")
    storage.add_notification("Akbank", "icloud", Transaction(date(2026, 8, 5), "TEKNOLOJI", Decimal("900"), "Elektronik"))
    storage.log_mail("icloud", "<m>", "bildirim_eklendi", bank="Akbank", received_at=datetime(2026, 8, 5),
                     detail="TEKNOLOJI: 900 TL · 3 ay vadeli")
    assert storage.backfill_installments(installments) == 1
    assert storage.installment_plan(date(2026, 9, 24))["this_month"] == Decimal("300")

    [loan] = storage.loans_overview(date(2026, 9, 24))
    assert storage.update_loan(loan["id"], "Konut", 10, None)
    storage.update_loan_info("Banka A", "2026-08-01", Decimal("40000"), 8, None)
    [loan] = storage.loans_overview(date(2026, 9, 24))
    assert loan["name"] == "Konut" and loan["remaining"] == 7

    storage.set_budget("Abonelik", Decimal("100"))
    assert storage.budget_status("2026-08")[0]["spent"] == Decimal("229.99")
    storage.add_rule("netf", "Eğlence")
    storage.add_rule("netf", "Abonelik")          # aynı ifade güncellenir
    assert storage.rule_pairs() == [("netf", "Abonelik")]
    assert storage.recategorize(lambda d, old: "X" if d == "NETFLIX" else old) == 1
