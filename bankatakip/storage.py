"""Veritabanı katmanı.

Yerelde SQLite dosyası, Vercel gibi sunucusuz ortamlarda Postgres (ör. Neon) kullanılır.
Hangisinin kullanılacağını adres belirler: `postgres://...` / `postgresql://...` → Postgres,
diğer her şey → SQLite dosya yolu.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import ParsedStatement

_TABLES = """
CREATE TABLE IF NOT EXISTS processed_mails (
    account      TEXT NOT NULL,
    message_id   TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (account, message_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS statements (
    id              {pk},
    file_hash       TEXT NOT NULL UNIQUE,
    bank            TEXT NOT NULL,
    source          TEXT,          -- mail hesabı veya 'manuel'
    file_path       TEXT,
    received_at     TEXT,
    statement_date  TEXT,
    due_date        TEXT,
    period_debt     TEXT,
    minimum_payment TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id           {pk},
    statement_id INTEGER NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    bank         TEXT NOT NULL,
    date         TEXT NOT NULL,
    description  TEXT NOT NULL,
    amount       TEXT NOT NULL,
    category     TEXT
);

CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_statement ON transactions(statement_id);
"""


def file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None


def _str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# Sunucusuz ortamda her istek yeni bağlantı açar; şemayı örnek başına bir kez kurmak yeterli.
_SCHEMA_READY: set[str] = set()


def is_postgres_url(url: str | Path) -> bool:
    return str(url).startswith(("postgres://", "postgresql://"))


class Storage:
    def __init__(self, url: str | Path):
        self.is_postgres = is_postgres_url(url)
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row

            self.conn = psycopg.connect(str(url), row_factory=dict_row, autocommit=False)
            if str(url) not in _SCHEMA_READY:
                schema = _TABLES.format(pk="BIGSERIAL PRIMARY KEY")
                with self.conn.cursor() as cur:
                    for stmt in filter(str.strip, schema.split(";")):
                        cur.execute(stmt)
                self.conn.commit()
                _SCHEMA_READY.add(str(url))
        else:
            path = Path(url)
            if str(path) != ":memory:":
                path.parent.mkdir(parents=True, exist_ok=True)
            # FastAPI bağımlılıkları ve uç noktaları farklı thread'lerde çalışabilir;
            # bağlantı istek başına açıldığı için paylaşım sorunu yok.
            self.conn = sqlite3.connect(str(path), check_same_thread=False)
            self.conn.row_factory = lambda cur, row: {
                col[0]: row[i] for i, col in enumerate(cur.description)
            }
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.executescript(_TABLES.format(pk="INTEGER PRIMARY KEY AUTOINCREMENT"))

    def close(self) -> None:
        self.conn.close()

    # --- küçük yardımcılar: SQL'ler '?' ile yazılır, Postgres için '%s'e çevrilir ---
    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.is_postgres else sql

    def _execute(self, sql: str, params: tuple | list = ()) -> Any:
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), params)
        return cur

    def _all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        return list(self._execute(sql, params).fetchall())

    def _one(self, sql: str, params: tuple | list = ()) -> dict | None:
        return self._execute(sql, params).fetchone()

    def _write(self, sql: str, params: tuple | list = ()) -> Any:
        try:
            cur = self._execute(sql, params)
            self.conn.commit()
            return cur
        except Exception:
            self.conn.rollback()
            raise

    # --- ayarlar ---
    def get_meta(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --- mailler ---
    def is_mail_processed(self, account: str, message_id: str) -> bool:
        return self._one(
            "SELECT 1 AS x FROM processed_mails WHERE account = ? AND message_id = ?",
            (account, message_id),
        ) is not None

    def mark_mail_processed(self, account: str, message_id: str) -> None:
        self._write(
            "INSERT INTO processed_mails (account, message_id, processed_at) VALUES (?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            (account, message_id, _now()),
        )

    # --- ekstreler ---
    def has_statement(self, content_hash: str) -> bool:
        return self._one("SELECT 1 AS x FROM statements WHERE file_hash = ?", (content_hash,)) is not None

    def save_statement(
        self,
        statement: ParsedStatement,
        content_hash: str,
        source: str,
        file_path: str | None = None,
        received_at: datetime | None = None,
    ) -> int | None:
        """Ekstreyi ve işlemlerini kaydeder. Aynı dosya daha önce kaydedildiyse None döner."""
        if self.has_statement(content_hash):
            return None
        s = statement.summary
        try:
            cur = self._execute(
                """INSERT INTO statements (file_hash, bank, source, file_path, received_at,
                       statement_date, due_date, period_debt, minimum_payment, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (
                    content_hash, statement.bank, source, file_path, _iso(received_at),
                    _iso(s.statement_date), _iso(s.due_date), _str(s.period_debt),
                    _str(s.minimum_payment), _now(),
                ),
            )
            statement_id = cur.fetchone()["id"]
            cur.executemany(
                self._sql(
                    """INSERT INTO transactions (statement_id, bank, date, description, amount, category)
                       VALUES (?, ?, ?, ?, ?, ?)"""
                ),
                [
                    (statement_id, statement.bank, tx.date.isoformat(), tx.description,
                     str(tx.amount), tx.category)
                    for tx in statement.transactions
                ],
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return statement_id

    def delete_statement(self, statement_id: int) -> bool:
        try:
            self._execute("DELETE FROM transactions WHERE statement_id = ?", (statement_id,))
            cur = self._execute("DELETE FROM statements WHERE id = ?", (statement_id,))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return cur.rowcount > 0

    # --- sorgular ---
    def list_statements(self) -> list[dict]:
        return self._all(
            """SELECT s.id, s.bank, s.source, s.received_at, s.statement_date, s.due_date,
                      s.period_debt, s.minimum_payment, s.created_at, COUNT(t.id) AS tx_count
               FROM statements s
               LEFT JOIN transactions t ON t.statement_id = s.id
               GROUP BY s.id, s.bank, s.source, s.received_at, s.statement_date, s.due_date,
                        s.period_debt, s.minimum_payment, s.created_at
               ORDER BY COALESCE(s.statement_date, s.received_at, s.created_at) DESC"""
        )

    def list_transactions(
        self,
        since: date | None = None,
        until: date | None = None,
        bank: str | None = None,
        category: str | None = None,
        search: str | None = None,
        statement_id: int | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM transactions WHERE 1=1"
        params: list = []
        if since:
            sql += " AND date >= ?"
            params.append(since.isoformat())
        if until:
            sql += " AND date <= ?"
            params.append(until.isoformat())
        if bank:
            sql += " AND bank = ?"
            params.append(bank)
        if category:
            if category == "Diğer":
                sql += " AND category IS NULL"
            else:
                sql += " AND category = ?"
                params.append(category)
        if search:
            sql += " AND LOWER(description) LIKE ?"
            params.append(f"%{search.lower()}%")
        if statement_id is not None:
            sql += " AND statement_id = ?"
            params.append(statement_id)
        sql += " ORDER BY date DESC, id DESC"
        return self._all(sql, params)

    def update_transaction_category(self, tx_id: int, category: str | None) -> bool:
        cur = self._write("UPDATE transactions SET category = ? WHERE id = ?", (category or None, tx_id))
        return cur.rowcount > 0

    def monthly_summary(self) -> list[tuple[str, str, Decimal]]:
        """(ay, kategori, toplam harcama) — sadece pozitif (harcama) tutarlar."""
        totals: dict[tuple[str, str], Decimal] = {}
        for row in self._all("SELECT date, category, amount FROM transactions"):
            amount = Decimal(row["amount"])
            if amount <= 0:
                continue
            key = (row["date"][:7], row["category"] or "Diğer")
            totals[key] = totals.get(key, Decimal(0)) + amount
        return sorted(((m, c, v) for (m, c), v in totals.items()), key=lambda r: (r[0], -r[2]))
