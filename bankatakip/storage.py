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

from .models import ParsedStatement, Transaction

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

CREATE TABLE IF NOT EXISTS accounts (
    id         {pk},
    bank       TEXT NOT NULL,
    key        TEXT NOT NULL,          -- banka içinde tekil: "kart:5839", "vadesiz:6644898"
    kind       TEXT NOT NULL,          -- "vadesiz" | "kredi_karti"
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (bank, key)
);

-- Bilinen bakiye/borç anları: dökümdeki bakiye sütunu, ekstre borcu, kalan limit, elle girilen bakiye
CREATE TABLE IF NOT EXISTS balance_snapshots (
    id              {pk},
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    as_of           TEXT NOT NULL,     -- tarih (YYYY-AA-GG) veya tarih-saat
    balance         TEXT,              -- vadesiz: hesap bakiyesi, kart: güncel borç
    available_limit TEXT,              -- kart: kullanılabilir limit
    source          TEXT NOT NULL,     -- "döküm" | "ekstre" | "bildirim" | "manuel"
    created_at      TEXT NOT NULL
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
    available_limit TEXT,
    kind            TEXT,
    account_id      INTEGER REFERENCES accounts(id),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id           {pk},
    statement_id INTEGER NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    bank         TEXT NOT NULL,
    date         TEXT NOT NULL,
    description  TEXT NOT NULL,
    amount       TEXT NOT NULL,
    category     TEXT,
    balance      TEXT,
    account_id   INTEGER REFERENCES accounts(id)
);

-- Taramada incelenen her mail ve sonucu (panelde "İncelenen mailler" listesi)
CREATE TABLE IF NOT EXISTS mail_log (
    id          {pk},
    account     TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    bank        TEXT,
    sender      TEXT,
    subject     TEXT,
    received_at TEXT,
    status      TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_statement ON transactions(statement_id);
"""


# Para hareketi olan ama harcama sayılmayan kategoriler
NON_SPENDING_CATEGORIES = {"Transfer", "Kart Ödemesi"}


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

            # prepare_threshold=None: Neon'un bağlantı havuzu (PgBouncer) ile uyumlu olsun
            self.conn = psycopg.connect(str(url), row_factory=dict_row, autocommit=False,
                                        prepare_threshold=None)
            if str(url) not in _SCHEMA_READY:
                schema = _TABLES.format(pk="BIGSERIAL PRIMARY KEY")
                with self.conn.cursor() as cur:
                    for stmt in filter(str.strip, schema.split(";")):
                        cur.execute(stmt)
                self.conn.commit()
                self._migrate()
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
            self._migrate()

    def close(self) -> None:
        self.conn.close()

    # Eski sürümlerde oluşturulmuş tablolara sonradan eklenen sütunlar
    _NEW_COLUMNS = [
        ("statements", "available_limit", "TEXT"),
        ("statements", "kind", "TEXT"),
        ("statements", "account_id", "INTEGER"),
        ("transactions", "balance", "TEXT"),
        ("transactions", "account_id", "INTEGER"),
    ]

    def _migrate(self) -> None:
        for table, column, ctype in self._NEW_COLUMNS:
            if self.is_postgres:
                self._execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ctype}")
            else:
                existing = {r["name"] for r in self._all(f"PRAGMA table_info({table})")}
                if column not in existing:
                    self._execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")
        self.conn.commit()

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

    # Tekrar denenebilecek (ekstre bulunamayan) mail durumları
    SKIPPED_STATUSES = ("konu_eslesmedi", "pdf_yok", "bildirim_okunamadi")

    def log_mail(self, account: str, message_id: str, status: str, bank: str | None = None,
                 sender: str | None = None, subject: str | None = None,
                 received_at: datetime | None = None, detail: str | None = None) -> None:
        self._write(
            """INSERT INTO mail_log (account, message_id, bank, sender, subject, received_at,
                   status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (account, message_id, bank, sender, subject, _iso(received_at), status, detail, _now()),
        )

    def list_mail_log(self, limit: int = 300) -> list[dict]:
        return self._all(
            "SELECT * FROM mail_log ORDER BY COALESCE(received_at, created_at) DESC, id DESC LIMIT ?",
            (limit,),
        )

    def reset_skipped_mails(self) -> int:
        """Ekstre eklenmemiş mailleri yeniden taranacak hale getirir (ör. anahtar kelime
        değiştikten sonra). Kaydı olmayan eski işaretler de temizlenir. Sıfırlanan mail
        sayısını döndürür."""
        placeholders = ", ".join("?" for _ in self.SKIPPED_STATUSES)
        try:
            cur = self._execute(
                f"""DELETE FROM processed_mails WHERE NOT EXISTS (
                        SELECT 1 FROM mail_log l
                        WHERE l.account = processed_mails.account
                          AND l.message_id = processed_mails.message_id
                          AND l.status NOT IN ({placeholders}))""",
                self.SKIPPED_STATUSES,
            )
            count = cur.rowcount
            self._execute(f"DELETE FROM mail_log WHERE status IN ({placeholders})", self.SKIPPED_STATUSES)
            # Son tarama tarihini sıfırla ki eski mailler de yeniden aransın
            self._execute("DELETE FROM meta WHERE key LIKE ?", ("last_sync:%",))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return count

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
        """Ekstreyi ve işlemlerini kaydeder. Aynı dosya daha önce kaydedildiyse None döner.

        Hesap dökümlerinde (vadesiz) aynı işlem başka bir dökümde zaten kayıtlıysa tekrar eklenmez;
        böylece çakışan tarih aralıklı dökümler ya da iki kez gelen aynı döküm işlemleri iki kat saymaz.
        Dökümdeki bakiye ve ekstredeki borç/limit, hesabın bakiye geçmişine yazılır.
        """
        if self.has_statement(content_hash):
            return None
        s = statement.summary
        try:
            account_id = self._account_id(statement.bank, statement.account) if statement.account else None
            cur = self._execute(
                """INSERT INTO statements (file_hash, bank, source, file_path, received_at,
                       statement_date, due_date, period_debt, minimum_payment, available_limit,
                       kind, account_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (
                    content_hash, statement.bank, source, file_path, _iso(received_at),
                    _iso(s.statement_date), _iso(s.due_date), _str(s.period_debt),
                    _str(s.minimum_payment), _str(s.available_limit), statement.kind, account_id, _now(),
                ),
            )
            statement_id = cur.fetchone()["id"]
            rows = []
            seen: dict[tuple, int] = {}
            for tx in statement.transactions:
                if statement.kind == "vadesiz" and account_id is not None:
                    key = (tx.date.isoformat(), tx.description, str(tx.amount), _str(tx.balance))
                    nth = seen.get(key, 0)
                    seen[key] = nth + 1
                    if self._count_tx(account_id, *key) > nth:
                        continue  # başka bir dökümden zaten kayıtlı
                rows.append((statement_id, statement.bank, tx.date.isoformat(), tx.description,
                             str(tx.amount), tx.category, _str(tx.balance), account_id))
            if rows:
                cur.executemany(
                    self._sql("""INSERT INTO transactions (statement_id, bank, date, description, amount,
                                     category, balance, account_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""),
                    rows,
                )
            if account_id is not None:
                self._snapshots_from_statement(account_id, statement, received_at)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        statement.saved_transactions = len(rows)
        return statement_id

    def _count_tx(self, account_id: int, day: str, description: str, amount: str,
                  balance: str | None) -> int:
        sql = ("SELECT COUNT(*) AS n FROM transactions WHERE account_id = ? AND date = ? "
               "AND description = ? AND amount = ? AND ")
        if balance is None:
            row = self._one(sql + "balance IS NULL", (account_id, day, description, amount))
        else:
            row = self._one(sql + "balance = ?", (account_id, day, description, amount, balance))
        return int(row["n"])

    def _snapshots_from_statement(self, account_id: int, statement: ParsedStatement,
                                  received_at: datetime | None) -> None:
        s = statement.summary
        if statement.kind == "vadesiz":
            dated = [tx for tx in statement.transactions if tx.balance is not None]
            if dated:
                # En yeni tarihli satırlardan, dökümdeki sırasına göre sonuncusu (en güncel bakiye)
                last_day = max(tx.date for tx in dated)
                same_day = [tx for tx in dated if tx.date == last_day]
                # Döküm yeniden eskiye sıralıysa günün en güncel satırı ilk, değilse son satırdır
                newest_first = dated[0].date > dated[-1].date
                latest = same_day[0] if newest_first else same_day[-1]
                self.add_snapshot(account_id, last_day.isoformat(), balance=latest.balance, source="döküm")
        elif s.period_debt is not None or s.available_limit is not None:
            as_of = s.statement_date or (received_at.date() if received_at else None) or s.due_date
            if as_of is not None:
                self.add_snapshot(account_id, as_of.isoformat(), balance=s.period_debt,
                                  available_limit=s.available_limit, source="ekstre")

    def has_same_summary(self, bank: str, due_date: date | None, period_debt: Decimal | None) -> bool:
        """Aynı bankanın aynı son ödeme tarihli ve aynı borçlu ekstresi zaten var mı?
        (Aynı dönem için gelen "hesap özeti" ve "ekstre borcu" maillerini tekilleştirmek için.)"""
        if due_date is None or period_debt is None:
            return False
        return self._one(
            "SELECT 1 AS x FROM statements WHERE bank = ? AND due_date = ? AND period_debt = ?",
            (bank, due_date.isoformat(), str(period_debt)),
        ) is not None

    def add_notification(self, bank: str, source: str, tx: "Transaction") -> int:
        """Anlık bildirimden gelen işlemi, o bankanın o ayki "bildirimler" kaydına ekler."""
        month = tx.date.strftime("%Y-%m")
        key = f"bildirim:{source}:{bank}:{month}"
        try:
            account_id = self._account_id(bank, tx.account) if tx.account else None
            row = self._one("SELECT id FROM statements WHERE file_hash = ?", (key,))
            if row:
                statement_id = row["id"]
            else:
                statement_id = self._execute(
                    """INSERT INTO statements (file_hash, bank, source, statement_date, kind, created_at)
                       VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
                    (key, bank, f"{source} (bildirim)", f"{month}-01", "bildirim", _now()),
                ).fetchone()["id"]
            self._execute(
                """INSERT INTO transactions (statement_id, bank, date, description, amount, category,
                       account_id) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (statement_id, bank, tx.date.isoformat(), tx.description, str(tx.amount), tx.category,
                 account_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return statement_id

    # --- hesaplar ve bakiye ---
    def _account_id(self, bank: str, ref) -> int:
        row = self._one("SELECT id FROM accounts WHERE bank = ? AND key = ?", (bank, ref.key))
        if row:
            return row["id"]
        return self._execute(
            "INSERT INTO accounts (bank, key, kind, name, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (bank, ref.key, ref.kind, ref.name, _now()),
        ).fetchone()["id"]

    def account_id(self, bank: str, ref) -> int:
        try:
            account_id = self._account_id(bank, ref)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return account_id

    def add_snapshot(self, account_id: int, as_of: str, balance: Decimal | None = None,
                     available_limit: Decimal | None = None, source: str = "manuel",
                     commit: bool = False) -> None:
        self._execute(
            """INSERT INTO balance_snapshots (account_id, as_of, balance, available_limit, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (account_id, as_of, _str(balance), _str(available_limit), source, _now()),
        )
        if commit:
            self.conn.commit()

    def rename_account(self, account_id: int, name: str) -> bool:
        cur = self._write("UPDATE accounts SET name = ? WHERE id = ?", (name, account_id))
        return cur.rowcount > 0

    def accounts_overview(self) -> list[dict]:
        """Her hesap için son bilinen bakiye/borç ve sonrasındaki giriş-çıkışlarla tahmini güncel durum."""
        result = []
        for acc in self._all("SELECT * FROM accounts ORDER BY bank, kind, name"):
            snaps = self._all(
                "SELECT * FROM balance_snapshots WHERE account_id = ? ORDER BY as_of DESC, id DESC",
                (acc["id"],),
            )
            balance_snap = next((x for x in snaps if x["balance"] is not None), None)
            limit_snap = next((x for x in snaps if x["available_limit"] is not None), None)
            since = balance_snap["as_of"][:10] if balance_snap else None
            flows = self._one(
                """SELECT COUNT(*) AS n,
                          SUM(CASE WHEN CAST(amount AS NUMERIC) > 0 THEN CAST(amount AS NUMERIC) ELSE 0 END) AS cikan,
                          SUM(CASE WHEN CAST(amount AS NUMERIC) < 0 THEN -CAST(amount AS NUMERIC) ELSE 0 END) AS giren
                   FROM transactions WHERE account_id = ?""" + (" AND date > ?" if since else ""),
                (acc["id"], since) if since else (acc["id"],),
            )
            out_ = Decimal(str(flows["cikan"] or 0))
            in_ = Decimal(str(flows["giren"] or 0))
            estimate = None
            if balance_snap is not None:
                base = Decimal(balance_snap["balance"])
                # vadesiz: bakiye + giren - çıkan; kart: borç + harcama - ödeme
                estimate = base + in_ - out_ if acc["kind"] == "vadesiz" else base + out_ - in_
            last_statement = self._one(
                """SELECT due_date, period_debt, minimum_payment FROM statements
                   WHERE account_id = ? AND due_date IS NOT NULL ORDER BY due_date DESC LIMIT 1""",
                (acc["id"],),
            )
            result.append({
                **acc,
                "snapshot": balance_snap,
                "limit": limit_snap,
                "since": since,
                "in_since": in_,
                "out_since": out_,
                "tx_since": int(flows["n"] or 0),
                "estimate": estimate,
                "last_statement": last_statement,
            })
        return result

    def monthly_flows(self, account_id: int | None = None) -> list[dict]:
        """Aylık giren/çıkan para (transferler dahil; bakiye hareketini gösterir)."""
        totals: dict[str, dict[str, Decimal]] = {}
        sql = "SELECT date, amount FROM transactions"
        params: tuple = ()
        if account_id is not None:
            sql += " WHERE account_id = ?"
            params = (account_id,)
        for row in self._all(sql, params):
            month = row["date"][:7]
            amount = Decimal(row["amount"])
            t = totals.setdefault(month, {"in": Decimal(0), "out": Decimal(0)})
            if amount > 0:
                t["out"] += amount
            else:
                t["in"] += -amount
        return [{"month": m, "in": v["in"], "out": v["out"]} for m, v in sorted(totals.items())]

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

    def used_categories(self) -> list[str]:
        rows = self._all("SELECT DISTINCT category FROM transactions WHERE category IS NOT NULL ORDER BY category")
        return [r["category"] for r in rows]

    def update_transaction_category(self, tx_id: int, category: str | None) -> bool:
        cur = self._write("UPDATE transactions SET category = ? WHERE id = ?", (category or None, tx_id))
        return cur.rowcount > 0

    def monthly_summary(self) -> list[tuple[str, str, Decimal]]:
        """(ay, kategori, toplam harcama) — sadece pozitif (harcama) tutarlar. Kendi hesaplar arası
        transferler ve kredi kartı ödemeleri harcama sayılmaz (kart harcamaları zaten ayrıca sayılır)."""
        totals: dict[tuple[str, str], Decimal] = {}
        for row in self._all("SELECT date, category, amount FROM transactions"):
            amount = Decimal(row["amount"])
            if amount <= 0 or row["category"] in NON_SPENDING_CATEGORIES:
                continue
            key = (row["date"][:7], row["category"] or "Diğer")
            totals[key] = totals.get(key, Decimal(0)) + amount
        return sorted(((m, c, v) for (m, c), v in totals.items()), key=lambda r: (r[0], -r[2]))
