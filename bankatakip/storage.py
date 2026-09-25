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

-- Aylık kategori bütçeleri
CREATE TABLE IF NOT EXISTS budgets (
    category   TEXT PRIMARY KEY,
    amount     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Panelden eklenen kurallar: açıklamada bu ifade geçerse bu kategori (anahtar kelimelerden önce)
CREATE TABLE IF NOT EXISTS category_rules (
    id         {pk},
    pattern    TEXT NOT NULL UNIQUE,
    category   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Krediler (banka başına): ödemeler "Kredi Ödemesi" işlemlerinden, taksit sayısı elle veya mailden
CREATE TABLE IF NOT EXISTS loans (
    id                     {pk},
    bank                   TEXT NOT NULL UNIQUE,
    name                   TEXT NOT NULL,
    total_installments     INTEGER,
    monthly_payment        TEXT,
    remaining_debt         TEXT,
    remaining_installments INTEGER,
    info_as_of             TEXT,
    created_at             TEXT NOT NULL
);

-- Maaş tutarı geçmişi: mailde tutar yazmayan maaş ödemeleri bu tutarla kaydedilir
CREATE TABLE IF NOT EXISTS salary (
    from_month TEXT PRIMARY KEY,       -- YYYY-AA: bu aydan itibaren geçerli
    amount     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_statement ON transactions(statement_id);
"""


# Para hareketi olan ama harcama sayılmayan kategoriler
NON_SPENDING_CATEGORIES = {"Transfer", "Kart Ödemesi"}
# Harcama raporlarına girmeyenler: yukarıdakiler + gelir ve kart ekstresindeki ödemeler
SPEND_EXCLUDED_CATEGORIES = NON_SPENDING_CATEGORIES | {"Maaş", "Ödeme"}


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
        ("transactions", "category_manual", "INTEGER"),
        ("transactions", "installments", "INTEGER"),
        ("transactions", "amount_auto", "INTEGER"),   # 1: tutar maaş ayarından hesaplanır
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

    def reset_skipped_mails(self, statuses: tuple[str, ...] | None = None) -> int:
        """Ekstre eklenmemiş mailleri yeniden taranacak hale getirir (ör. anahtar kelime
        değiştikten sonra). Kaydı olmayan eski işaretler de temizlenir. Sıfırlanan mail
        sayısını döndürür."""
        statuses = statuses or self.SKIPPED_STATUSES
        placeholders = ", ".join("?" for _ in statuses)
        try:
            cur = self._execute(
                f"""DELETE FROM processed_mails WHERE NOT EXISTS (
                        SELECT 1 FROM mail_log l
                        WHERE l.account = processed_mails.account
                          AND l.message_id = processed_mails.message_id
                          AND l.status NOT IN ({placeholders}))""",
                statuses,
            )
            count = cur.rowcount
            self._execute(f"DELETE FROM mail_log WHERE status IN ({placeholders})", statuses)
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

    def add_notification(self, bank: str, source: str, tx: "Transaction", amount_auto: bool = False) -> int:
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
                       account_id, installments, amount_auto) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (statement_id, bank, tx.date.isoformat(), tx.description, str(tx.amount), tx.category,
                 account_id, tx.installments, 1 if amount_auto else None),
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
        # Aynı mail yeniden okunursa aynı bilgi ikinci kez yazılmasın
        if self._one("""SELECT 1 FROM balance_snapshots WHERE account_id = ? AND as_of = ?
                         AND COALESCE(balance, '') = ? AND COALESCE(available_limit, '') = ?""",
                     (account_id, as_of, _str(balance) or "", _str(available_limit) or "")):
            return
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

    def requeue_statement_mails(self, wanted) -> int:
        """Yanlış türde (vadesiz sanılmış) kaydedilen ekstrelerin maillerini yeniden taranacak hale
        getirir: wanted(konu) doğru olan mailin ekstresi ve günlük kaydı silinir. Sayı döndürür."""
        rows = self._all("SELECT account, message_id, bank, subject, received_at, status FROM mail_log "
                         "WHERE status IN ('eklendi', 'zaten_var', 'hata')")
        count = 0
        try:
            for r in rows:
                if not wanted(r["subject"] or ""):
                    continue
                stmts = self._all("SELECT id FROM statements WHERE bank = ? AND received_at = ? AND kind = 'vadesiz'",
                                  (r["bank"], r["received_at"]))
                if not stmts and r["status"] != "hata":
                    continue
                for st in stmts:
                    self._execute("DELETE FROM transactions WHERE statement_id = ?", (st["id"],))
                    self._execute("DELETE FROM statements WHERE id = ?", (st["id"],))
                self._execute("DELETE FROM processed_mails WHERE account = ? AND message_id = ?",
                              (r["account"], r["message_id"]))
                self._execute("DELETE FROM mail_log WHERE account = ? AND message_id = ?",
                              (r["account"], r["message_id"]))
                count += 1
            # İşlemi kalmayan otomatik hesaplar (yanlış açılmış "Vadesiz") ve anlık görüntüleri
            self._execute(
                """DELETE FROM balance_snapshots WHERE source <> 'manuel' AND account_id IN (
                       SELECT a.id FROM accounts a WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.account_id = a.id)
                       AND NOT EXISTS (SELECT 1 FROM statements s WHERE s.account_id = a.id))""")
            self._execute(
                """DELETE FROM accounts WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.account_id = accounts.id)
                   AND NOT EXISTS (SELECT 1 FROM statements s WHERE s.account_id = accounts.id)
                   AND NOT EXISTS (SELECT 1 FROM balance_snapshots b WHERE b.account_id = accounts.id)""")
            if count:
                self._execute("DELETE FROM meta WHERE key LIKE ?", ("last_sync:%",))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return count

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
        # Elle seçilen kategori, kurallar değişince yapılan yeniden sınıflandırmada korunur
        cur = self._write("UPDATE transactions SET category = ?, category_manual = 1 WHERE id = ?",
                          (category or None, tx_id))
        return cur.rowcount > 0

    def recategorize(self, decide) -> int:
        """Elle değiştirilmemiş işlemlerin kategorisini decide(açıklama, eski kategori) ile
        yeniden belirler. Değişen işlem sayısını döndürür."""
        rows = self._all("SELECT id, description, category FROM transactions WHERE COALESCE(category_manual, 0) = 0")
        changed = 0
        try:
            for row in rows:
                new = decide(row["description"], row["category"])
                if new != row["category"]:
                    self._execute("UPDATE transactions SET category = ? WHERE id = ?", (new, row["id"]))
                    changed += 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return changed

    def _report_rows(self) -> list[dict]:
        return self._all(
            """SELECT t.date, t.description, t.amount, t.category, t.bank, s.kind, a.kind AS account_kind
               FROM transactions t LEFT JOIN statements s ON s.id = t.statement_id
               LEFT JOIN accounts a ON a.id = t.account_id"""
        )

    @staticmethod
    def _is_spending(category: str | None) -> bool:
        return category not in SPEND_EXCLUDED_CATEGORIES

    @staticmethod
    def _is_income(row: dict, amount: Decimal) -> bool:
        """Hesaplara gelen para: maaş ya da vadesiz hesaba girişler (döküm veya bildirim;
        kart ödemeleri hariç)."""
        if amount >= 0 or row["category"] == "Kart Ödemesi":
            return False
        return row["category"] == "Maaş" or "vadesiz" in (row["kind"], row.get("account_kind"))

    def monthly_summary(self) -> list[tuple[str, str, Decimal]]:
        """(ay, kategori, net harcama). İadeler kendi kategorisinden düşülür; kendi hesaplar arası
        transferler, kart ödemeleri ve gelirler harcama sayılmaz."""
        totals: dict[tuple[str, str], Decimal] = {}
        for row in self._report_rows():
            if not self._is_spending(row["category"]):
                continue
            key = (row["date"][:7], row["category"] or "Diğer")
            totals[key] = totals.get(key, Decimal(0)) + Decimal(row["amount"])
        return sorted(((m, c, v) for (m, c), v in totals.items() if v > 0), key=lambda r: (r[0], -r[2]))

    def report(self, month: str) -> dict:
        """Seçilen ayın harcama raporu: özet, kategoriler (önceki ay ve 3 ay ortalamasıyla),
        en çok harcanan yerler, günlük harcama ve son 6 ayın kategori tablosu."""
        cat_month: dict[tuple[str, str], Decimal] = {}
        cat_count: dict[str, int] = {}
        income: dict[str, Decimal] = {}
        merchants: dict[str, dict] = {}
        daily: dict[str, Decimal] = {}
        tx_count = 0
        for row in self._report_rows():
            amount = Decimal(row["amount"])
            m = row["date"][:7]
            if self._is_income(row, amount):
                income[m] = income.get(m, Decimal(0)) - amount
            if not self._is_spending(row["category"]):
                continue
            cat = row["category"] or "Diğer"
            cat_month[(m, cat)] = cat_month.get((m, cat), Decimal(0)) + amount
            if m == month:
                tx_count += 1
                cat_count[cat] = cat_count.get(cat, 0) + 1
                daily[row["date"]] = daily.get(row["date"], Decimal(0)) + amount
                entry = merchants.setdefault(row["description"], {"total": Decimal(0), "count": 0, "category": cat})
                entry["total"] += amount
                entry["count"] += 1

        months = sorted({m for m, _ in cat_month} | set(income))
        prev_months = [m for m in months if m < month]
        prev = prev_months[-1] if prev_months else None
        last3 = prev_months[-3:]

        def month_total(m: str) -> Decimal:
            return sum((v for (mm, _), v in cat_month.items() if mm == m and v > 0), Decimal(0))

        spend = month_total(month)
        categories = []
        for (m, cat), total in cat_month.items():
            if m != month or total <= 0:
                continue
            avg3 = sum((max(cat_month.get((mm, cat), Decimal(0)), Decimal(0)) for mm in last3), Decimal(0)) / len(last3) \
                if last3 else None
            categories.append({
                "name": cat, "total": total, "share": total / spend if spend else Decimal(0),
                "prev": max(cat_month.get((prev, cat), Decimal(0)), Decimal(0)) if prev else None,
                "avg3": avg3, "count": cat_count.get(cat, 0),
            })
        categories.sort(key=lambda c: -c["total"])

        pivot_months = [m for m in months if m <= month][-6:]
        pivot_cats: dict[str, list[Decimal]] = {}
        for (m, cat), total in cat_month.items():
            if m in pivot_months and total > 0:
                pivot_cats.setdefault(cat, [Decimal(0)] * len(pivot_months))[pivot_months.index(m)] = total
        pivot = sorted(({"name": c, "values": v, "total": sum(v, Decimal(0))} for c, v in pivot_cats.items()),
                       key=lambda r: -r["total"])

        year, mon = (int(x) for x in month.split("-"))
        days_in_month = (date(year + mon // 12, mon % 12 + 1, 1) - date(year, mon, 1)).days
        today = date.today()
        elapsed = today.day if (today.year, today.month) == (year, mon) else days_in_month
        return {
            "month": month,
            "months": months,
            "kpi": {
                "spend": spend,
                "spend_prev": month_total(prev) if prev else None,
                "spend_avg3": sum((month_total(m) for m in last3), Decimal(0)) / len(last3) if last3 else None,
                "income": income.get(month, Decimal(0)),
                "tx_count": tx_count,
                "daily_avg": spend / elapsed if elapsed else Decimal(0),
                "prev_month": prev,
            },
            "categories": categories,
            "merchants": sorted(({"description": d, **v} for d, v in merchants.items() if v["total"] > 0),
                                key=lambda x: -x["total"])[:10],
            "daily": [{"date": d, "total": v} for d, v in sorted(daily.items())],
            "pivot": {"months": pivot_months, "rows": pivot},
        }

    def category_trend(self, category: str, until: str, count: int = 12) -> list[dict]:
        """Bir kategorinin son `count` ayındaki net harcaması (boş aylar 0)."""
        totals: dict[str, Decimal] = {}
        for row in self._report_rows():
            if (row["category"] or "Diğer") == category and self._is_spending(row["category"]):
                m = row["date"][:7]
                totals[m] = totals.get(m, Decimal(0)) + Decimal(row["amount"])
        year, mon = (int(x) for x in until.split("-"))
        months = []
        for _ in range(count):
            months.append(f"{year:04d}-{mon:02d}")
            year, mon = (year, mon - 1) if mon > 1 else (year - 1, 12)
        return [{"month": m, "total": max(totals.get(m, Decimal(0)), Decimal(0))} for m in reversed(months)]

    # --- bütçeler ---
    def list_budgets(self) -> list[dict]:
        return [{"category": r["category"], "amount": Decimal(r["amount"])}
                for r in self._all("SELECT category, amount FROM budgets ORDER BY category")]

    def set_budget(self, category: str, amount: Decimal | None) -> None:
        """amount None veya 0 ise bütçe kaldırılır."""
        try:
            self._execute("DELETE FROM budgets WHERE category = ?", (category,))
            if amount:
                self._execute("INSERT INTO budgets (category, amount, created_at) VALUES (?, ?, ?)",
                              (category, str(amount), _now()))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def budget_status(self, month: str) -> list[dict]:
        """Bütçesi olan kategorilerin o ayki net harcaması ve doluluk oranı."""
        spent: dict[str, Decimal] = {}
        for row in self._report_rows():
            if row["date"][:7] == month and self._is_spending(row["category"]):
                cat = row["category"] or "Diğer"
                spent[cat] = spent.get(cat, Decimal(0)) + Decimal(row["amount"])
        result = []
        for b in self.list_budgets():
            used = max(spent.get(b["category"], Decimal(0)), Decimal(0))
            result.append({**b, "spent": used, "ratio": used / b["amount"] if b["amount"] else Decimal(0)})
        return sorted(result, key=lambda b: -b["ratio"])

    def uncategorized_groups(self, limit: int = 40) -> list[dict]:
        """Kategorisiz işlemler, benzer açıklamalar bir arada (rakamlar ve şube kodları hariç):
        panelde tek tıkla kural eklemek için. İşyeri yazmayan genel bildirimler dahil değil."""
        import re

        groups: dict[str, dict] = {}
        for r in self._all("SELECT description, amount FROM transactions WHERE category IS NULL"):
            desc = r["description"]
            if desc in GENERIC_DESCRIPTIONS:
                continue
            pattern = rule_pattern(desc)
            key = re.sub(r"\s+", " ", pattern.lower())
            g = groups.setdefault(key, {"pattern": pattern, "example": desc, "count": 0, "total": Decimal(0)})
            g["count"] += 1
            g["total"] += Decimal(r["amount"])
        # "KAHVEDE ART" kuralı "KAHVEDE ART İZMİT"i de kapsar: kısa ifadenin grubunda topla
        merged: dict[str, dict] = {}
        for key in sorted(groups, key=len):
            root = next((k for k in merged if key.startswith(k)), None)
            if root is None:
                merged[key] = groups[key]
            else:
                merged[root]["count"] += groups[key]["count"]
                merged[root]["total"] += groups[key]["total"]
        return sorted(merged.values(), key=lambda g: (-g["count"], -abs(g["total"])))[:limit]

    def delete_matching_transactions(self, match) -> int:
        """match(açıklama) doğru olan (işlem olmayan, ör. "gün sonu bakiyesi") satırları siler."""
        rows = self._all("SELECT id, description FROM transactions")
        ids = [r["id"] for r in rows if match(r["description"])]
        try:
            for i in ids:
                self._execute("DELETE FROM transactions WHERE id = ?", (i,))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return len(ids)

    def uncategorized_count(self) -> dict:
        row = self._one("SELECT COUNT(*) AS n FROM transactions WHERE category IS NULL")
        generic = self._one(
            "SELECT COUNT(*) AS n FROM transactions WHERE category IS NULL AND description IN ("
            + ", ".join("?" for _ in GENERIC_DESCRIPTIONS) + ")", tuple(GENERIC_DESCRIPTIONS))
        return {"total": int(row["n"]), "generic": int(generic["n"])}

    # --- panelden eklenen kategori kuralları ---
    def list_rules(self) -> list[dict]:
        return self._all("SELECT id, pattern, category FROM category_rules ORDER BY id")

    def rule_pairs(self) -> list[tuple[str, str]]:
        return [(r["pattern"], r["category"]) for r in self.list_rules()]

    def add_rule(self, pattern: str, category: str) -> None:
        try:
            self._execute("DELETE FROM category_rules WHERE pattern = ?", (pattern,))
            self._execute("INSERT INTO category_rules (pattern, category, created_at) VALUES (?, ?, ?)",
                          (pattern, category, _now()))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def delete_rule(self, rule_id: int) -> bool:
        return self._write("DELETE FROM category_rules WHERE id = ?", (rule_id,)).rowcount > 0

    # --- taksitler ---
    def installment_plan(self, today: date | None = None, months: int = 12) -> dict:
        """Taksitli alışverişler ve önümüzdeki aylara düşen taksit tutarları. İlk taksit alışverişin
        ertesi ayına yazılır (ekstre kesimi). İptal edilen (aynı banka ve tutarda eksi işlemi olan)
        alışverişler sayılmaz."""
        today = today or date.today()
        this_month = _month_index(today.isoformat())
        rows = self._all(
            "SELECT id, bank, date, description, amount, category, installments FROM transactions "
            "WHERE installments IS NOT NULL AND installments > 1 ORDER BY date DESC")
        refunds = {(r["bank"], abs(Decimal(r["amount"]))) for r in self._all(
            "SELECT bank, amount FROM transactions WHERE amount LIKE ?", ("-%",))}
        schedule = {this_month + i: Decimal(0) for i in range(months)}
        items = []
        for r in rows:
            total = Decimal(r["amount"])
            if total <= 0 or (r["bank"], total) in refunds:
                continue
            n = int(r["installments"])
            monthly = (total / n).quantize(Decimal("0.01"))
            first = _month_index(r["date"]) + 1
            last = first + n - 1
            paid = min(max(this_month - first, 0), n)  # bu aydan önceki aylar ödenmiş sayılır
            remaining = n - paid
            if remaining <= 0:
                continue
            for m in range(max(first, this_month), last + 1):
                if m in schedule:
                    schedule[m] += monthly
            items.append({"id": r["id"], "bank": r["bank"], "date": r["date"], "description": r["description"],
                          "category": r["category"], "total": total, "installments": n, "monthly": monthly,
                          "paid": paid, "remaining": remaining, "remaining_amount": monthly * remaining,
                          "last_month": _month_name(last)})
        return {
            "months": [{"month": _month_name(m), "total": v} for m, v in sorted(schedule.items())],
            "items": items,
            "this_month": schedule[this_month],
            "remaining_total": sum((i["remaining_amount"] for i in items), Decimal(0)),
        }

    def backfill_installments(self, parse) -> int:
        """Daha önce eklenmiş bildirimlerin taksit sayısını mail günlüğündeki gövde örneğinden
        doldurur (detay: "<açıklama>: <tutar> TL · ... 9 ay vadeli ...")."""
        import re
        from datetime import timedelta

        filled = 0
        rows = self._all("SELECT bank, received_at, detail FROM mail_log WHERE status = 'bildirim_eklendi' "
                         "AND detail LIKE ? ORDER BY received_at", ("%vadeli%",))
        try:
            for row in rows:
                n = parse(row["detail"] or "")
                m = re.search(r": (-?\d+(?:\.\d+)?) TL ·", row["detail"] or "")
                if not n or not m or not row["received_at"]:
                    continue
                received = date.fromisoformat(row["received_at"][:10])
                tx = self._one(
                    """SELECT id FROM transactions WHERE bank = ? AND amount = ? AND installments IS NULL
                       AND date >= ? AND date <= ? ORDER BY date LIMIT 1""",
                    (row["bank"], m.group(1), (received - timedelta(days=5)).isoformat(),
                     (received + timedelta(days=1)).isoformat()))
                if tx:
                    self._execute("UPDATE transactions SET installments = ? WHERE id = ?", (n, tx["id"]))
                    filled += 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return filled

    # --- maaş ---
    def list_salary(self) -> list[dict]:
        return [{"from_month": r["from_month"], "amount": Decimal(r["amount"])}
                for r in self._all("SELECT from_month, amount FROM salary ORDER BY from_month")]

    def salary_for(self, day: date, entries: list[dict] | None = None) -> Decimal:
        """O tarihteki maaş: başlangıcı o aydan önce/o ay olan en son kayıt; tarih ilk kayıttan
        önceyse ilk kayıt (geçmiş maaşlar için yaklaşık). Hiç kayıt yoksa 0."""
        entries = self.list_salary() if entries is None else entries
        if not entries:
            return Decimal(0)
        month = day.isoformat()[:7]
        valid = [e for e in entries if e["from_month"] <= month]
        return (valid[-1] if valid else entries[0])["amount"]

    def set_salary(self, from_month: str, amount: Decimal | None) -> int:
        """Maaş kaydı ekler/günceller (amount None: siler) ve otomatik maaş işlemlerini yeniden
        hesaplar. Güncellenen işlem sayısını döndürür."""
        try:
            self._execute("DELETE FROM salary WHERE from_month = ?", (from_month,))
            if amount:
                self._execute("INSERT INTO salary (from_month, amount, created_at) VALUES (?, ?, ?)",
                              (from_month, str(amount), _now()))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.apply_salary()

    def apply_salary(self) -> int:
        entries = self.list_salary()
        rows = self._all("SELECT id, date, amount FROM transactions WHERE amount_auto = 1")
        changed = 0
        try:
            for r in rows:
                amount = str(-self.salary_for(date.fromisoformat(r["date"][:10]), entries))
                if amount != r["amount"]:
                    self._execute("UPDATE transactions SET amount = ? WHERE id = ?", (amount, r["id"]))
                    changed += 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return changed

    def salary_overview(self) -> dict:
        rows = self._all("SELECT date, amount FROM transactions WHERE amount_auto = 1 ORDER BY date")
        return {
            "entries": self.list_salary(),
            "payments": len(rows),
            "first": rows[0]["date"] if rows else None,
            "last": rows[-1]["date"] if rows else None,
            "total": -sum((Decimal(r["amount"]) for r in rows), Decimal(0)),
        }

    def backfill_counterparties(self, parse) -> int:
        """Eski "Hesaba para girişi / Hesaptan para çıkışı" kayıtlarına mail günlüğündeki gövde
        örneğinden karşı tarafın adını yazar ("YAKUP CİVELEK · Havale")."""
        import re
        from datetime import timedelta

        generic = ("Hesaba para girişi", "Hesaptan para çıkışı")
        filled = 0
        rows = self._all("SELECT bank, received_at, detail FROM mail_log WHERE status = 'bildirim_eklendi' "
                         "AND (detail LIKE ? OR detail LIKE ?)", tuple(g + ":%" for g in generic))
        try:
            for row in rows:
                name = parse(row["detail"] or "")
                m = re.search(r": (-?\d+(?:\.\d+)?) TL ·", row["detail"] or "")
                if not name or not m or not row["received_at"]:
                    continue
                received = date.fromisoformat(row["received_at"][:10])
                tx = self._one(
                    """SELECT id FROM transactions WHERE bank = ? AND amount = ? AND description IN (?, ?)
                       AND date >= ? AND date <= ? ORDER BY date LIMIT 1""",
                    (row["bank"], m.group(1), *generic, (received - timedelta(days=2)).isoformat(),
                     (received + timedelta(days=1)).isoformat()))
                if tx:
                    self._execute("UPDATE transactions SET description = ? WHERE id = ?", (name, tx["id"]))
                    filled += 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return filled

    # --- krediler ---
    def _loan_payments(self) -> tuple[dict[str, dict[int, Decimal]], dict[str, dict[int, str]]]:
        """(banka → {ay: o ay ödenen kredi taksiti}, banka → {ay: o ayki son ödeme tarihi})."""
        amounts: dict[str, dict[int, Decimal]] = {}
        dates: dict[str, dict[int, str]] = {}
        for r in self._all("SELECT bank, date, amount FROM transactions WHERE category = 'Kredi Ödemesi'"):
            amount = Decimal(r["amount"])
            if amount > 0:
                m = _month_index(r["date"])
                months = amounts.setdefault(r["bank"], {})
                months[m] = months.get(m, Decimal(0)) + amount
                last = dates.setdefault(r["bank"], {})
                last[m] = max(last.get(m, ""), r["date"][:10])
        return amounts, dates

    def loans_overview(self, today: date | None = None) -> list[dict]:
        today = today or date.today()
        payments, pay_dates = self._loan_payments()
        for bank in payments:
            if not self._one("SELECT id FROM loans WHERE bank = ?", (bank,)):
                self._execute("INSERT INTO loans (bank, name, created_at) VALUES (?, ?, ?)",
                              (bank, f"{bank} kredisi", _now()))
        self.conn.commit()
        result = []
        for loan in self._all("SELECT * FROM loans ORDER BY bank"):
            months = payments.get(loan["bank"], {})
            paid = len(months)
            last_month = max(months) if months else None
            monthly = Decimal(loan["monthly_payment"]) if loan["monthly_payment"] else \
                (months[last_month] if last_month is not None else None)
            remaining = None
            if loan["remaining_installments"] is not None and loan["info_as_of"]:
                after = sum(1 for d in pay_dates.get(loan["bank"], {}).values() if d > loan["info_as_of"][:10])
                remaining = max(int(loan["remaining_installments"]) - after, 0)
            elif loan["total_installments"]:
                remaining = max(int(loan["total_installments"]) - paid, 0)
            next_month = last_month + 1 if last_month is not None else None
            if next_month is not None and next_month < _month_index(today.isoformat()):
                next_month = _month_index(today.isoformat())
            result.append({
                **loan,
                "paid": paid,
                "total_paid": sum(months.values(), Decimal(0)),
                "monthly": monthly,
                "remaining": remaining,
                "remaining_estimate": monthly * remaining if monthly is not None and remaining is not None else None,
                "last_payment_month": _month_name(last_month) if last_month is not None else None,
                "next_payment_month": _month_name(next_month) if next_month is not None and remaining != 0 else None,
                "end_month": _month_name(last_month + remaining) if last_month is not None and remaining else None,
            })
        return result

    def update_loan(self, loan_id: int, name: str | None = None, total_installments: int | None = None,
                    monthly_payment: Decimal | None = None) -> bool:
        cur = self._write(
            "UPDATE loans SET name = COALESCE(?, name), total_installments = ?, monthly_payment = ? WHERE id = ?",
            (name, total_installments or None, _str(monthly_payment) if monthly_payment else None, loan_id))
        return cur.rowcount > 0

    def update_loan_info(self, bank: str, as_of: str, remaining_debt: Decimal | None,
                         remaining_installments: int | None, monthly: Decimal | None) -> None:
        """Bankanın kredi borç bilgisi maili: kalan borç / kalan taksit."""
        try:
            if not self._one("SELECT id FROM loans WHERE bank = ?", (bank,)):
                self._execute("INSERT INTO loans (bank, name, created_at) VALUES (?, ?, ?)",
                              (bank, f"{bank} kredisi", _now()))
            self._execute(
                """UPDATE loans SET remaining_debt = COALESCE(?, remaining_debt),
                       remaining_installments = COALESCE(?, remaining_installments),
                       monthly_payment = COALESCE(monthly_payment, ?), info_as_of = ?
                   WHERE bank = ? AND (info_as_of IS NULL OR info_as_of <= ?)""",
                (_str(remaining_debt), remaining_installments, _str(monthly), as_of, bank, as_of))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- düzenli ödemeler ---
    def recurring(self, today: date | None = None, window: int = 6) -> list[dict]:
        """Son `window` ayın en az 3'ünde, benzer tutarla (±%15) tekrarlanan harcamalar
        (abonelik, fatura, kira ...). Krediler ayrı listelendiği için dahil değil."""
        import re
        import statistics

        today = today or date.today()
        start = _month_index(today.isoformat()) - window + 1
        groups: dict[tuple[str, str], list[dict]] = {}
        for row in self._report_rows():
            if not self._is_spending(row["category"]) or row["category"] == "Kredi Ödemesi":
                continue
            amount = Decimal(row["amount"])
            if amount <= 0 or _month_index(row["date"]) < start:
                continue
            key = re.sub(r"\s+", " ", re.sub(r"[\d*/.:#-]+", " ", row["description"].lower())).strip()
            groups.setdefault((row["bank"], key), []).append({**row, "amount": amount})
        result = []
        for rows in groups.values():
            months = {_month_index(r["date"]) for r in rows}
            if len(months) < 3 or len(rows) > len(months) * 1.5:
                continue
            amounts = [float(r["amount"]) for r in rows]
            mean = statistics.fmean(amounts)
            if statistics.pstdev(amounts) > mean * 0.15:
                continue
            rows.sort(key=lambda r: r["date"])
            last = rows[-1]
            recent = rows[-3:]
            result.append({
                "description": last["description"], "category": last["category"], "bank": last["bank"],
                "average": (sum((r["amount"] for r in recent), Decimal(0)) / len(recent)).quantize(Decimal("0.01")),
                "months": len(months), "last_date": last["date"],
                "next_month": _month_name(_month_index(last["date"]) + 1),
            })
        return sorted(result, key=lambda r: -r["average"])

    # --- haftalık özet ---
    def period_summary(self, start: date, end: date) -> dict:
        """[start, end] aralığındaki net harcama, kategori ve yer kırılımı."""
        cats: dict[str, Decimal] = {}
        places: dict[str, Decimal] = {}
        for row in self._report_rows():
            if not (start.isoformat() <= row["date"][:10] <= end.isoformat()):
                continue
            if not self._is_spending(row["category"]):
                continue
            amount = Decimal(row["amount"])
            cat = row["category"] or "Diğer"
            cats[cat] = cats.get(cat, Decimal(0)) + amount
            places[row["description"]] = places.get(row["description"], Decimal(0)) + amount
        return {
            "total": sum((v for v in cats.values() if v > 0), Decimal(0)),
            "categories": sorted(((c, v) for c, v in cats.items() if v > 0), key=lambda x: -x[1]),
            "places": sorted(((p, v) for p, v in places.items() if v > 0), key=lambda x: -x[1])[:5],
        }


# İşyeri yazmayan bildirimlerin açıklamaları: kural bunlara uygulanamaz (hepsi aynı olurdu)
GENERIC_DESCRIPTIONS = ("Banka kartı harcaması", "Kredi kartı harcaması", "Kredi kartı harcamanız",
                        "Akbank Kart harcamanız", "Hesaba para girişi", "Hesaptan para çıkışı")


def rule_pattern(description: str) -> str:
    """Kural için önerilen ifade: açıklamanın rakam içermeyen ilk (en fazla 3) kelimesi,
    sondaki şube/şehir ekleri olmadan. "SBX İZMİT ŞEKERPINAR DRI" → "SBX İZMİT ŞEKERPINAR"."""
    import re

    words = []
    for w in description.split():
        if re.search(r"\d", w) or len(words) == 3:
            break
        words.append(w)
    text = re.sub(r"[^\w\s.&'-]+$", "", " ".join(words)).strip(" .-")
    if len(text) >= 3:
        return text
    fallback = next((re.sub(r"[^\w]", "", w) for w in description.split()
                     if len(re.sub(r"[^\w]", "", w)) >= 3 and not re.search(r"\d", w)), "")
    return fallback or description[:20]


def _month_index(iso: str) -> int:
    return int(iso[:4]) * 12 + int(iso[5:7]) - 1


def _month_name(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"
