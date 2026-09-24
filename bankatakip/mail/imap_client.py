"""Gmail ve iCloud (veya herhangi bir IMAP sunucusu) üzerinden ekstre maillerini çeker."""

from __future__ import annotations

import email
import imaplib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime

from ..config import MailAccount

log = logging.getLogger(__name__)


@dataclass
class Attachment:
    filename: str
    content: bytes


@dataclass
class FetchedMail:
    account: str
    message_id: str
    sender: str
    subject: str
    received: datetime | None
    attachments: list[Attachment] = field(default_factory=list)


def decode_str(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _imap_date(d: date) -> str:
    # IMAP tarih formatı İngilizce ay kısaltması ister: 01-Jan-2026
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{d.day:02d}-{months[d.month - 1]}-{d.year}"


def extract_pdf_attachments(msg: Message) -> list[Attachment]:
    attachments = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = decode_str(part.get_filename())
        is_pdf = part.get_content_type() == "application/pdf" or filename.lower().endswith(".pdf")
        if not is_pdf:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            attachments.append(Attachment(filename or "ekstre.pdf", payload))
    return attachments


class MailClient:
    def __init__(self, account: MailAccount, timeout: float = 30):
        self.account = account
        self.timeout = timeout
        self.conn: imaplib.IMAP4_SSL | None = None

    def connect(self) -> None:
        log.info("%s (%s) bağlanılıyor...", self.account.name, self.account.host)
        password = self.account.password  # eksikse bağlanmadan hata ver
        self.conn = imaplib.IMAP4_SSL(self.account.host, self.account.port, timeout=self.timeout)
        self.conn.login(self.account.email, password)

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    def __enter__(self) -> "MailClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def search(self, folder: str, sender: str, since: date) -> list[bytes]:
        assert self.conn is not None
        status, _ = self.conn.select(f'"{folder}"', readonly=True)
        if status != "OK":
            log.warning("%s: '%s' klasörü açılamadı", self.account.name, folder)
            return []
        # Türkçe karakterli konu aramaları sunucuya göre sorun çıkarabildiği için
        # sunucuda sadece gönderen + tarih ile arıyoruz, konu filtresi istemci tarafında.
        status, data = self.conn.uid("SEARCH", None, "FROM", f'"{sender}"', "SINCE", _imap_date(since))
        if status != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def fetch_header(self, uid: bytes) -> tuple[str, str]:
        """(message_id, subject) — tüm maili indirmeden önce hızlı kontrol için."""
        assert self.conn is not None
        status, data = self.conn.uid(
            "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT)])"
        )
        raw = next((item[1] for item in data if isinstance(item, tuple)), b"") if status == "OK" else b""
        msg = email.message_from_bytes(raw or b"")
        message_id = (msg.get("Message-ID") or f"{self.account.name}-{uid.decode()}").strip()
        return message_id, decode_str(msg.get("Subject"))

    def fetch(self, uid: bytes) -> FetchedMail | None:
        assert self.conn is not None
        # BODY.PEEK[] maili okundu olarak işaretlemez; iCloud RFC822 ile boş döndürebiliyor.
        status, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if status != "OK" or not data:
            return None
        raw = next((item[1] for item in data if isinstance(item, tuple)), None)
        if raw is None:
            return None
        msg = email.message_from_bytes(raw)
        received = None
        if msg.get("Date"):
            try:
                received = parsedate_to_datetime(msg["Date"])
            except (TypeError, ValueError):
                pass
        message_id = (msg.get("Message-ID") or f"{self.account.name}-{uid.decode()}").strip()
        return FetchedMail(
            account=self.account.name,
            message_id=message_id,
            sender=parseaddr(decode_str(msg.get("From")))[1].lower(),
            subject=decode_str(msg.get("Subject")),
            received=received,
            attachments=extract_pdf_attachments(msg),
        )
