from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from .config import ConfigError, load_config
from .parsers import PdfPasswordError
from .storage import Storage
from .sync import import_pdf, sync


def _tl(value: Decimal | str | None) -> str:
    if value is None:
        return "-"
    value = Decimal(value)
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} TL"


def cmd_sync(args, config, storage) -> int:
    since = date.fromisoformat(args.since) if args.since else None
    report = sync(config, storage, since=since)
    print(f"Taranan mail: {report.mails_checked}")
    print(f"Yeni ekstre: {report.statements_added}  |  Yeni işlem: {report.transactions_added}")
    for err in report.errors:
        print(f"  ! {err}", file=sys.stderr)
    return 1 if report.errors and not report.statements_added else 0


def cmd_import(args, config, storage) -> int:
    bank = config.bank_by_name(args.bank)
    if bank is None:
        names = ", ".join(b.name for b in config.banks)
        print(f"'{args.bank}' bankası config.yaml'da yok. Tanımlı bankalar: {names}", file=sys.stderr)
        return 2
    path = Path(args.file)
    try:
        statement_id, count = import_pdf(path.read_bytes(), bank, config, storage,
                                         source="manuel", filename=path.name)
    except PdfPasswordError as exc:
        print(exc, file=sys.stderr)
        return 1
    if statement_id is None:
        print("Bu ekstre zaten kayıtlı.")
    else:
        print(f"Ekstre #{statement_id} eklendi: {count} işlem.")
    return 0


def cmd_statements(args, config, storage) -> int:
    rows = storage.list_statements()
    if not rows:
        print("Henüz ekstre yok. 'python -m bankatakip sync' ile başlayın.")
        return 0
    print(f"{'#':>3}  {'Banka':<14} {'Kesim':<10} {'Son Ödeme':<10} {'Dönem Borcu':>16} {'Asgari':>14} {'İşlem':>6}")
    for r in rows:
        print(f"{r['id']:>3}  {r['bank'][:14]:<14} {r['statement_date'] or '-':<10} "
              f"{r['due_date'] or '-':<10} {_tl(r['period_debt']):>16} "
              f"{_tl(r['minimum_payment']):>14} {r['tx_count']:>6}")
    return 0


def cmd_list(args, config, storage) -> int:
    since = date.fromisoformat(args.since) if args.since else None
    rows = storage.list_transactions(since=since, bank=args.bank, category=args.category)
    for r in rows[: args.limit]:
        print(f"{r['date']}  {r['bank'][:12]:<12} {(r['category'] or '-')[:12]:<12} "
              f"{r['description'][:40]:<40} {_tl(r['amount']):>16}")
    total = sum((Decimal(r["amount"]) for r in rows), Decimal(0))
    print(f"\n{len(rows)} işlem, toplam: {_tl(total)}")
    return 0


def cmd_summary(args, config, storage) -> int:
    current = None
    month_total = Decimal(0)
    for month, category, total in storage.monthly_summary():
        if month != current:
            if current is not None:
                print(f"  {'TOPLAM':<20} {_tl(month_total):>16}\n")
            print(month)
            current, month_total = month, Decimal(0)
        month_total += total
        print(f"  {category:<20} {_tl(total):>16}")
    if current is not None:
        print(f"  {'TOPLAM':<20} {_tl(month_total):>16}")
    else:
        print("Henüz harcama yok.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bankatakip", description="Mail'deki banka ekstrelerini takip eder.")
    p.add_argument("-c", "--config", default="config.yaml", help="yapılandırma dosyası")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sync", help="Gmail/iCloud'dan yeni ekstreleri çek")
    s.add_argument("--since", help="bu tarihten itibaren (YYYY-AA-GG)")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("import", help="bilgisayardaki bir ekstre PDF'ini içe aktar")
    s.add_argument("file")
    s.add_argument("--bank", required=True, help="config.yaml'daki banka adı")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("statements", help="ekstreleri listele (borç, son ödeme tarihi)")
    s.set_defaults(func=cmd_statements)

    s = sub.add_parser("list", help="işlemleri listele")
    s.add_argument("--since")
    s.add_argument("--bank")
    s.add_argument("--category")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("summary", help="aylık kategori bazında harcama özeti")
    s.set_defaults(func=cmd_summary)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    storage = Storage(config.database)
    try:
        return args.func(args, config, storage)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    finally:
        storage.close()
