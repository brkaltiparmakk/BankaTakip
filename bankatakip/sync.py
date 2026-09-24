"""Mail hesaplarını tarayıp ekstre PDF'lerini indirir, ayrıştırır ve veritabanına kaydeder."""

from __future__ import annotations

import logging
import re
import time
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
    incomplete: bool = False  # süre doldu; tekrar çalıştırınca kaldığı yerden devam eder

    def as_dict(self) -> dict:
        return {
            "mails_checked": self.mails_checked,
            "statements_added": self.statements_added,
            "transactions_added": self.transactions_added,
            "errors": self.errors,
            "incomplete": self.incomplete,
        }


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

    text = extract_text(content, bank.pdf_password)
    statement = get_parser(bank.name, config.categories).parse(text)

    target = None
    if config.attachments_dir is not None:
        stamp = (received_at or datetime.now()).strftime("%Y-%m-%d")
        target_dir = config.attachments_dir / _safe_name(bank.name)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{stamp}_{digest[:8]}_{_safe_name(filename)}"
        target.write_bytes(content)

    statement_id = storage.save_statement(
        statement, digest, source=source, file_path=str(target) if target else None,
        received_at=received_at,
    )
    return statement_id, len(statement.transactions)


def _account_since(config: Config, storage: Storage, account: str) -> date:
    """İlk taramada lookback_days kadar geriye, sonrakilerde son başarılı taramadan
    bir hafta öncesine kadar bakılır (geç gelen mailleri kaçırmamak için)."""
    last = storage.get_meta(f"last_sync:{account}")
    if last:
        return date.fromisoformat(last) - timedelta(days=7)
    return date.today() - timedelta(days=config.lookback_days)


def sync(config: Config, storage: Storage, since: date | None = None,
         time_budget: float | None = None) -> SyncReport:
    """Tüm hesapları tarar. time_budget (saniye) verilirse süre dolunca durur ve
    report.incomplete=True döner; işlenen mailler kaydedildiği için sonraki çalıştırma
    kaldığı yerden devam eder."""
    report = SyncReport()
    deadline = time.monotonic() + time_budget if time_budget else None
    if not config.accounts:
        report.errors.append("Tanımlı mail hesabı yok (GMAIL_EMAIL / ICLOUD_EMAIL).")

    for account in config.accounts:
        if deadline and time.monotonic() > deadline:
            report.incomplete = True
            break
        account_since = since or _account_since(config, storage, account.name)
        errors_before = len(report.errors)
        client = MailClient(account)
        try:
            client.connect()
        except Exception as exc:  # bağlantı/kimlik hatası diğer hesapları durdurmasın
            report.errors.append(f"{account.name}: bağlanılamadı ({exc})")
            continue
        try:
            for folder in account.folders:
                for bank in config.banks:
                    _sync_bank(client, folder, bank, account_since, config, storage, report, deadline)
        finally:
            client.close()
        if not report.incomplete and len(report.errors) == errors_before:
            storage.set_meta(f"last_sync:{account.name}", date.today().isoformat())
    storage.set_meta("last_run", datetime.now().isoformat(timespec="seconds"))
    return report


def _sync_bank(client: MailClient, folder: str, bank: BankConfig, since: date,
               config: Config, storage: Storage, report: SyncReport,
               deadline: float | None = None) -> None:
    account = client.account.name
    seen: set[bytes] = set()
    for sender in bank.senders:
        try:
            uids = client.search(folder, sender, since)
        except Exception as exc:
            report.errors.append(f"{account}/{folder}: {bank.name} araması başarısız ({exc})")
            continue
        for uid in uids:
            if deadline and time.monotonic() > deadline:
                report.incomplete = True
                return
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
