from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .models import ParsedStatement

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_mails (
    account     TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (account, message_id)
);

CREATE TABLE IF NOT EXISTS statements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    statement_id INTEGER NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    bank         TEXT NOT NULL,
    date         TEXT NOT NULL,
    description  TEXT NOT NULL,
    amount       TEXT NOT NULL,
    category     TEXT
);

CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
"""


def file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None


def _str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


class Storage:
    def __init__(self, path: str | Path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- mailler ---
    def is_mail_processed(self, account: str, message_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM processed_mails WHERE account = ? AND message_id = ?",
            (account, message_id),
        ).fetchone()
        return row is not None

    def mark_mail_processed(self, account: str, message_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO processed_mails VALUES (?, ?, ?)",
            (account, message_id, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    # --- ekstreler ---
    def has_statement(self, content_hash: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM statements WHERE file_hash = ?", (content_hash,)
        ).fetchone() is not None

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
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO statements (file_hash, bank, source, file_path, received_at,
                       statement_date, due_date, period_debt, minimum_payment, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    content_hash, statement.bank, source, file_path, _iso(received_at),
                    _iso(s.statement_date), _iso(s.due_date), _str(s.period_debt),
                    _str(s.minimum_payment), datetime.now().isoformat(timespec="seconds"),
                ),
            )
            statement_id = cur.lastrowid
            self.conn.executemany(
                """INSERT INTO transactions (statement_id, bank, date, description, amount, category)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (statement_id, statement.bank, tx.date.isoformat(), tx.description,
                     str(tx.amount), tx.category)
                    for tx in statement.transactions
                ],
            )
        return statement_id

    # --- sorgular ---
    def list_statements(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT s.*, COUNT(t.id) AS tx_count FROM statements s
               LEFT JOIN transactions t ON t.statement_id = s.id
               GROUP BY s.id ORDER BY COALESCE(s.statement_date, s.received_at) DESC"""
        ).fetchall()

    def list_transactions(
        self, since: date | None = None, bank: str | None = None, category: str | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM transactions WHERE 1=1"
        params: list = []
        if since:
            sql += " AND date >= ?"
            params.append(since.isoformat())
        if bank:
            sql += " AND bank = ?"
            params.append(bank)
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY date DESC, id DESC"
        return self.conn.execute(sql, params).fetchall()

    def monthly_summary(self) -> list[tuple[str, str, Decimal]]:
        """(ay, kategori, toplam harcama) — sadece pozitif (harcama) tutarlar."""
        totals: dict[tuple[str, str], Decimal] = {}
        for row in self.conn.execute("SELECT date, category, amount FROM transactions"):
            amount = Decimal(row["amount"])
            if amount <= 0:
                continue
            key = (row["date"][:7], row["category"] or "Diğer")
            totals[key] = totals.get(key, Decimal(0)) + amount
        return sorted(((m, c, v) for (m, c), v in totals.items()), key=lambda r: (r[0], -r[2]), reverse=False)
