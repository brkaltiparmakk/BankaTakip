"""Gmail ve iCloud (veya herhangi bir IMAP sunucusu) üzerinden ekstre maillerini çeker."""

from __future__ import annotations

import email
import imaplib
import logging
import re
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
class MailHeader:
    message_id: str
    subject: str
    sender: str
    received: datetime | None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


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


STATEMENT_TYPES = {
    "application/pdf", "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
STATEMENT_EXTENSIONS = (".pdf", ".xls", ".xlsx", ".htm", ".html")


def extract_statement_attachments(msg: Message) -> list[Attachment]:
    """Ekstre olabilecek ekler: PDF, Excel ve dosya olarak eklenmiş HTML tablolar."""
    attachments = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = decode_str(part.get_filename())
        ctype = part.get_content_type()
        # Mailin kendi HTML gövdesi ek değildir; sadece dosya adı olan HTML parçaları alınır
        if not (ctype in STATEMENT_TYPES or (filename and filename.lower().endswith(STATEMENT_EXTENSIONS))):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            attachments.append(Attachment(filename or "ekstre", payload))
    return attachments


# Eski ad; geriye dönük uyumluluk için
extract_pdf_attachments = extract_statement_attachments


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

    def list_folders(self) -> list[tuple[str, set[str]]]:
        """(klasör adı, bayraklar) listesi. Adlar sunucunun gönderdiği (kodlanmış) haliyle döner."""
        assert self.conn is not None
        status, data = self.conn.list()
        folders = []
        for line in data or [] if status == "OK" else []:
            if not isinstance(line, bytes):
                continue
            m = re.match(rb'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
            if not m:
                continue
            name = m.group("name").decode("utf-8", "replace").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"')
            flags = {f.decode().lower() for f in m.group("flags").split()}
            folders.append((name, flags))
        return folders

    def default_folders(self) -> list[str]:
        """Gmail: tüm mailleri içeren "Tüm Postalar" (dil ayarından bağımsız, \\All bayrağıyla bulunur).
        Diğerleri: Gelen Kutusu + varsa Arşiv klasörü."""
        try:
            folders = self.list_folders()
        except Exception:
            return ["INBOX"]
        if self.account.provider == "gmail":
            for name, flags in folders:
                if "\\all" in flags:
                    return [name]
            return ["INBOX"]
        result = ["INBOX"]
        for name, flags in folders:
            if "\\archive" in flags or name.lower() in ("archive", "arşiv"):
                if name not in result:
                    result.append(name)
        return result

    def search(self, folder: str, sender: str, since: date | None) -> list[bytes]:
        assert self.conn is not None
        status, _ = self.conn.select(f'"{folder}"', readonly=True)
        if status != "OK":
            log.warning("%s: '%s' klasörü açılamadı", self.account.name, folder)
            return []
        # Türkçe karakterli konu aramaları sunucuya göre sorun çıkarabildiği için
        # sunucuda sadece gönderen + tarih ile arıyoruz, konu filtresi istemci tarafında.
        criteria = ["FROM", f'"{sender}"']
        if since is not None:
            criteria += ["SINCE", _imap_date(since)]
        status, data = self.conn.uid("SEARCH", None, *criteria)
        if status != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def fetch_headers(self, uids: list[bytes]) -> dict[bytes, "MailHeader"]:
        """Birden çok mailin başlığını tek istekte okur (mail başına ayrı istekten çok daha hızlı)."""
        assert self.conn is not None
        headers: dict[bytes, MailHeader] = {}
        for start in range(0, len(uids), 100):
            chunk = uids[start:start + 100]
            status, data = self.conn.uid(
                "FETCH", b",".join(chunk),
                "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)])",
            )
            if status != "OK" or not data:
                continue
            for item in data:
                if not isinstance(item, tuple):
                    continue
                m = re.search(rb"UID (\d+)", item[0])
                if not m:
                    continue
                uid = m.group(1)
                msg = email.message_from_bytes(item[1] or b"")
                headers[uid] = MailHeader(
                    message_id=(msg.get("Message-ID") or f"{self.account.name}-{uid.decode()}").strip(),
                    subject=decode_str(msg.get("Subject")),
                    sender=parseaddr(decode_str(msg.get("From")))[1].lower(),
                    received=_parse_date(msg.get("Date")),
                )
        return headers

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
            attachments=extract_statement_attachments(msg),
        )
