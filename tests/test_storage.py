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
        conn.execute("DROP TABLE IF EXISTS transactions, statements, processed_mails, meta CASCADE")
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
