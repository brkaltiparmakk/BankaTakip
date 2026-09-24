"""Mail hesaplarını tarayıp ekstre PDF'lerini indirir, ayrıştırır ve veritabanına kaydeder."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import BankConfig, Config
from .mail import MailClient
from .parsers import PdfPasswordError, extract_document_text, get_parser
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


def import_statement(
    content: bytes,
    bank: BankConfig,
    config: Config,
    storage: Storage,
    source: str,
    filename: str = "ekstre.pdf",
    received_at: datetime | None = None,
) -> tuple[int | None, int]:
    """Tek bir ekstre dosyasını (PDF/Excel/HTML) işler. (statement_id, işlem sayısı) döndürür;
    zaten varsa (None, 0)."""
    digest = file_hash(content)
    if storage.has_statement(digest):
        return None, 0

    text = extract_document_text(content, bank.pdf_password)
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


def _account_since(config: Config, storage: Storage, account: str) -> date | None:
    """Geçmiş taraması bitmemişse tüm geçmişe (veya lookback_days kadar) bakılır; bittiyse
    son başarılı taramadan bir hafta öncesine kadar (geç gelen mailleri kaçırmamak için)."""
    last = storage.get_meta(f"last_sync:{account}")
    if last:
        return date.fromisoformat(last) - timedelta(days=7)
    if config.lookback_days > 0:
        return date.today() - timedelta(days=config.lookback_days)
    return None


def history_done(storage: Storage, account: str) -> bool:
    """Bu hesabın geçmiş mailleri bir kez baştan sona tarandı mı?"""
    return storage.get_meta(f"last_sync:{account}") is not None


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
            for folder in account.folders or client.default_folders():
                for bank in config.banks:
                    _sync_bank(client, folder, bank, account_since, config, storage, report, deadline)
        finally:
            client.close()
        if not report.incomplete and len(report.errors) == errors_before:
            storage.set_meta(f"last_sync:{account.name}", date.today().isoformat())
    storage.set_meta("last_run", datetime.now().isoformat(timespec="seconds"))
    return report


def _sync_bank(client: MailClient, folder: str, bank: BankConfig, since: date | None,
               config: Config, storage: Storage, report: SyncReport,
               deadline: float | None = None) -> None:
    account = client.account.name
    uids: list[bytes] = []
    for sender in bank.senders:
        try:
            found = client.search(folder, sender, since)
        except Exception as exc:
            report.errors.append(f"{account}/{folder}: {bank.name} araması başarısız ({exc})")
            continue
        uids.extend(u for u in found if u not in uids)
    if not uids:
        return

    # UID'ler geliş sırasına göre artar; en yeni maillerden başla ki güncel ekstreler önce gelsin
    uids.sort(key=int, reverse=True)
    headers = client.fetch_headers(uids)
    for uid in uids:
        if deadline and time.monotonic() > deadline:
            report.incomplete = True
            return
        header = headers.get(uid)
        if header is None:
            continue
        report.mails_checked += 1
        if storage.is_mail_processed(account, header.message_id):
            continue

        def record(status: str, detail: str | None = None) -> None:
            storage.log_mail(account, header.message_id, status, bank=bank.name,
                             sender=header.sender, subject=header.subject,
                             received_at=header.received, detail=detail)

        if not bank.matches_subject(header.subject):
            storage.mark_mail_processed(account, header.message_id)
            record("konu_eslesmedi")
            continue

        mail = client.fetch(uid)
        if mail is None:
            continue
        if not mail.attachments:
            storage.mark_mail_processed(account, header.message_id)
            record("pdf_yok")
            continue

        retry_later = False
        results = []
        for att in mail.attachments:
            try:
                statement_id, count = import_statement(
                    att.content, bank, config, storage, source=account,
                    filename=att.filename, received_at=mail.received,
                )
            except PdfPasswordError as exc:
                retry_later = True  # şifre tanımlanınca tekrar denensin
                report.errors.append(f"{bank.name} - '{mail.subject}': {exc}")
                results.append(("sifreli", att.filename))
                continue
            except Exception as exc:
                report.errors.append(f"{bank.name} - '{mail.subject}': ayrıştırılamadı ({exc})")
                results.append(("hata", f"{att.filename}: {exc}"))
                continue
            if statement_id is None:
                results.append(("zaten_var", att.filename))
            else:
                report.statements_added += 1
                report.transactions_added += count
                results.append(("eklendi", f"{att.filename}: {count} işlem"))
                log.info("%s: %s eklendi (%d işlem)", bank.name, att.filename, count)

        # Mail başına tek kayıt: en anlamlı sonuç öne çıkar
        for status in ("eklendi", "sifreli", "hata", "zaten_var"):
            details = [d for st, d in results if st == status]
            if details:
                record(status, "; ".join(details))
                break
        if not retry_later:
            storage.mark_mail_processed(account, header.message_id)


# Eski ad; geriye dönük uyumluluk için
import_pdf = import_statement
