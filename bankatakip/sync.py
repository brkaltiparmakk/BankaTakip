"""Mail hesaplarını tarayıp ekstre PDF'lerini indirir, ayrıştırır ve veritabanına kaydeder."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import BankConfig, Config
from .mail import MailClient
from .ai import BATCH_SIZE, AIError, AIQuotaExceeded, GeminiClient, ai_enabled
from .parsers import PdfPasswordError, detect_kind, extract_document_text, get_parser
from .parsers.notifications import notification_sign, parse_notification
from .storage import Storage, file_hash

log = logging.getLogger(__name__)


@dataclass
class SyncReport:
    mails_checked: int = 0
    statements_added: int = 0
    transactions_added: int = 0
    errors: list[str] = field(default_factory=list)
    incomplete: bool = False  # süre doldu; tekrar çalıştırınca kaldığı yerden devam eder
    ai_used: int = 0          # yapay zekanın okuduğu mail/ek sayısı
    ai_paused: bool = False   # yapay zeka sınırı doldu; kalanlar sonraki taramada

    def as_dict(self) -> dict:
        return {
            "mails_checked": self.mails_checked,
            "statements_added": self.statements_added,
            "transactions_added": self.transactions_added,
            "errors": self.errors,
            "incomplete": self.incomplete,
            "ai_used": self.ai_used,
            "ai_paused": self.ai_paused,
        }

    def ai_limit_reached(self, message: str) -> None:
        if not self.ai_paused:
            self.ai_paused = True
            self.errors.append(message)


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
    ai: "GeminiClient | None" = None,
) -> tuple[int | None, int]:
    """Tek bir ekstre dosyasını (PDF/Excel/HTML) işler. (statement_id, işlem sayısı) döndürür;
    zaten varsa (None, 0)."""
    digest = file_hash(content)
    if storage.has_statement(digest):
        return None, 0

    parser = get_parser(bank.name, config.categories)
    text = extract_document_text(content, bank.pdf_password)
    statement = parser.parse(text)
    if _is_empty(statement) and ai is not None:
        # Kurallar okuyamadı: PDF'i doğrudan (taranmış olsa bile), diğer türleri metin olarak gönder
        is_pdf = detect_kind(content) == "pdf"
        ai_statement = ai.extract_statement(bank.name, filename,
                                            text=None if is_pdf else text,
                                            document=content if is_pdf else None)
        if ai_statement is not None:
            _categorize(ai_statement, parser)
            statement = ai_statement

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


def _sample(text: str, limit: int = 800) -> str:
    """Kayıt için gövde örneği: okunamayan mailin biçimini sonradan inceleyebilmek için."""
    return " ".join(text.split())[:limit]


def _is_empty(statement) -> bool:
    s = statement.summary
    return s.due_date is None and s.period_debt is None and not statement.transactions


def _categorize(statement, parser) -> None:
    for tx in statement.transactions:
        tx.category = parser.categorize(tx.description)


def _import_body(mail, bank: BankConfig, config: Config, storage: Storage, account: str,
                 report: SyncReport, ai: "GeminiClient | None" = None) -> tuple[str, str | None]:
    text = mail.body_text
    parser = get_parser(bank.name, config.categories)
    statement = parser.parse(text)
    via = "mail gövdesinden"
    if _is_empty(statement):
        if ai is None or not text.strip():
            return "pdf_yok", _sample(text)
        ai_statement = ai.extract_statement(bank.name, mail.subject, text=text)  # AIQuotaExceeded yukarı çıkar
        report.ai_used += 1
        if ai_statement is None or _is_empty(ai_statement):
            return "pdf_yok", "yapay zeka: ekstre değil · " + _sample(text, 300)
        _categorize(ai_statement, parser)
        statement, via = ai_statement, "yapay zeka (mail gövdesi)"
    s = statement.summary
    if not statement.transactions and storage.has_same_summary(bank.name, s.due_date, s.period_debt):
        return "zaten_var", "aynı dönem ekstresi zaten kayıtlı"
    digest = file_hash(b"body:" + mail.message_id.encode())
    statement_id = storage.save_statement(statement, digest, source=account, received_at=mail.received)
    if statement_id is None:
        return "zaten_var", None
    report.statements_added += 1
    report.transactions_added += len(statement.transactions)
    return "eklendi", f"{via}: {len(statement.transactions)} işlem · {_sample(text, 300)}"


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
    ai = GeminiClient() if ai_enabled() else None
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
                    _sync_bank(client, folder, bank, account_since, config, storage, report,
                               deadline, ai)
        finally:
            client.close()
        if not report.incomplete and len(report.errors) == errors_before:
            storage.set_meta(f"last_sync:{account.name}", date.today().isoformat())
    storage.set_meta("last_run", datetime.now().isoformat(timespec="seconds"))
    return report


def _sync_bank(client: MailClient, folder: str, bank: BankConfig, since: date | None,
               config: Config, storage: Storage, report: SyncReport,
               deadline: float | None = None, ai: "GeminiClient | None" = None) -> None:
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

    def record(header, status: str, detail: str | None = None) -> None:
        storage.log_mail(account, header.message_id, status, bank=bank.name,
                         sender=header.sender, subject=header.subject,
                         received_at=header.received, detail=detail)

    def ai_usable() -> bool:
        return ai is not None and not report.ai_paused

    # Kuralların okuyamadığı bildirimler yapay zekaya toplu gönderilir (ücretsiz plan sınırı için)
    pending: list[tuple] = []

    def flush_pending() -> None:
        if not pending:
            return
        batch = pending[:]
        pending.clear()
        try:
            txs = ai.extract_notifications([(m.subject, m.body_text, m.received) for _, m in batch])
        except AIQuotaExceeded as exc:
            report.ai_limit_reached(str(exc))
            return  # işlenmiş işaretlenmedi: sonraki taramada tekrar denenir
        except AIError as exc:
            report.errors.append(str(exc))
            txs = [None] * len(batch)
        report.ai_used += len(batch)
        parser = get_parser(bank.name, config.categories)
        for (header, mail), tx in zip(batch, txs):
            if tx is None:
                record(header, "bildirim_okunamadi", "yapay zeka: işlem bulunamadı · " + _sample(mail.body_text))
            else:
                tx.category = parser.categorize(tx.description)
                storage.add_notification(bank.name, account, tx)
                report.transactions_added += 1
                record(header, "bildirim_eklendi", f"yapay zeka · {tx.description}: {tx.amount} TL")
            storage.mark_mail_processed(account, header.message_id)

    # UID'ler geliş sırasına göre artar; en yeni maillerden başla ki güncel ekstreler önce gelsin
    uids.sort(key=int, reverse=True)
    headers = client.fetch_headers(uids)
    for uid in uids:
        if deadline and time.monotonic() > deadline:
            flush_pending()
            report.incomplete = True
            return
        header = headers.get(uid)
        if header is None:
            continue
        report.mails_checked += 1
        if storage.is_mail_processed(account, header.message_id):
            continue

        is_statement = bank.matches_subject(header.subject)
        is_notification = notification_sign(header.subject) is not None
        if not is_statement and not is_notification:
            storage.mark_mail_processed(account, header.message_id)
            record(header, "konu_eslesmedi")
            continue

        mail = client.fetch(uid)
        if mail is None:
            continue

        if is_notification and not is_statement:
            tx = parse_notification(mail.body_text, mail.subject, mail.received)
            if tx is None and ai_usable():
                pending.append((header, mail))
                if len(pending) >= BATCH_SIZE:
                    flush_pending()
                continue
            if tx is None:
                record(header, "bildirim_okunamadi", _sample(mail.body_text))
            else:
                tx.category = get_parser(bank.name, config.categories).categorize(tx.description)
                storage.add_notification(bank.name, account, tx)
                report.transactions_added += 1
                record(header, "bildirim_eklendi", f"{tx.description}: {tx.amount} TL · {_sample(mail.body_text, 300)}")
            storage.mark_mail_processed(account, header.message_id)
            continue

        if not mail.attachments:
            # Ekstreyi ek yerine mail gövdesinde gönderen bankalar (ör. Akbank)
            try:
                status, detail = _import_body(mail, bank, config, storage, account, report,
                                              ai if ai_usable() else None)
            except AIQuotaExceeded as exc:
                report.ai_limit_reached(str(exc))
                continue  # sonraki taramada tekrar denenir
            except AIError as exc:
                report.errors.append(str(exc))
                status, detail = "pdf_yok", _sample(mail.body_text)
            record(header, status, detail)
            storage.mark_mail_processed(account, header.message_id)
            continue

        retry_later = False
        results = []
        for att in mail.attachments:
            try:
                statement_id, count = import_statement(
                    att.content, bank, config, storage, source=account,
                    filename=att.filename, received_at=mail.received,
                    ai=ai if ai_usable() else None,
                )
            except PdfPasswordError as exc:
                retry_later = True  # şifre tanımlanınca tekrar denensin
                report.errors.append(f"{bank.name} - '{mail.subject}': {exc}")
                results.append(("sifreli", att.filename))
                continue
            except AIQuotaExceeded as exc:
                retry_later = True
                report.ai_limit_reached(str(exc))
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
                record(header, status, "; ".join(details))
                break
        if not retry_later:
            storage.mark_mail_processed(account, header.message_id)

    flush_pending()


# Eski ad; geriye dönük uyumluluk için
import_pdf = import_statement
