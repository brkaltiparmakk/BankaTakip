"""Mail hesaplarını tarayıp ekstre PDF'lerini indirir, ayrıştırır ve veritabanına kaydeder."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import BankConfig, Config
from .mail import MailClient
from .parsers import PdfPasswordError, extract_text, get_parser
from .storage import Storage, file_hash

log = logging.getLogger(__name__)


@dataclass
class SyncReport:
    mails_checked: int = 0
    statements_added: int = 0
    transactions_added: int = 0
    errors: list[str] = field(default_factory=list)


def _safe_name(text: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", text).strip("_") or "ekstre"


def import_pdf(
    content: bytes,
    bank: BankConfig,
    config: Config,
    storage: Storage,
    source: str,
    filename: str = "ekstre.pdf",
    received_at: datetime | None = None,
) -> tuple[int | None, int]:
    """Tek bir PDF'i işler. (statement_id, işlem sayısı) döndürür; zaten varsa (None, 0)."""
    digest = file_hash(content)
    if storage.has_statement(digest):
        return None, 0

    stamp = (received_at or datetime.now()).strftime("%Y-%m-%d")
    target_dir = config.attachments_dir / _safe_name(bank.name)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{stamp}_{digest[:8]}_{_safe_name(filename)}"
    target.write_bytes(content)

    text = extract_text(content, bank.pdf_password)
    statement = get_parser(bank.name, config.categories).parse(text)
    statement_id = storage.save_statement(
        statement, digest, source=source, file_path=str(target), received_at=received_at
    )
    return statement_id, len(statement.transactions)


def sync(config: Config, storage: Storage, since: date | None = None) -> SyncReport:
    report = SyncReport()
    since = since or (date.today() - timedelta(days=config.lookback_days))

    for account in config.accounts:
        client = MailClient(account)
        try:
            client.connect()
        except Exception as exc:  # bağlantı/kimlik hatası diğer hesapları durdurmasın
            report.errors.append(f"{account.name}: bağlanılamadı ({exc})")
            continue
        try:
            for folder in account.folders:
                for bank in config.banks:
                    _sync_bank(client, folder, bank, since, config, storage, report)
        finally:
            client.close()
    return report


def _sync_bank(client: MailClient, folder: str, bank: BankConfig, since: date,
               config: Config, storage: Storage, report: SyncReport) -> None:
    account = client.account.name
    seen: set[bytes] = set()
    for sender in bank.senders:
        try:
            uids = client.search(folder, sender, since)
        except Exception as exc:
            report.errors.append(f"{account}/{folder}: {bank.name} araması başarısız ({exc})")
            continue
        for uid in uids:
            if uid in seen:
                continue
            seen.add(uid)
            report.mails_checked += 1

            message_id, subject = client.fetch_header(uid)
            if storage.is_mail_processed(account, message_id):
                continue
            if not bank.matches_subject(subject):
                storage.mark_mail_processed(account, message_id)
                continue

            mail = client.fetch(uid)
            if mail is None:
                continue
            retry_later = False
            for att in mail.attachments:
                try:
                    statement_id, count = import_pdf(
                        att.content, bank, config, storage, source=account,
                        filename=att.filename, received_at=mail.received,
                    )
                except PdfPasswordError as exc:
                    retry_later = True  # şifre tanımlanınca tekrar denensin
                    report.errors.append(f"{bank.name} - '{mail.subject}': {exc}")
                    continue
                except Exception as exc:
                    report.errors.append(f"{bank.name} - '{mail.subject}': ayrıştırılamadı ({exc})")
                    continue
                if statement_id is not None:
                    report.statements_added += 1
                    report.transactions_added += count
                    log.info("%s: %s eklendi (%d işlem)", bank.name, att.filename, count)
            if not retry_later:
                storage.mark_mail_processed(account, message_id)
