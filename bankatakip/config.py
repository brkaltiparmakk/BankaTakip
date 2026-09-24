from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROVIDER_HOSTS = {
    "gmail": ("imap.gmail.com", 993),
    "icloud": ("imap.mail.me.com", 993),
}


class ConfigError(Exception):
    pass


@dataclass
class MailAccount:
    name: str
    provider: str
    email: str
    password_env: str
    host: str
    port: int = 993
    folders: list[str] = field(default_factory=lambda: ["INBOX"])

    @property
    def password(self) -> str:
        value = (os.environ.get(self.password_env) or "").strip()
        if self.provider == "gmail":
            # Google uygulama şifresini "abcd efgh ijkl mnop" diye gösterir; boşluklar şifreye dahil değil
            value = value.replace(" ", "")
        if not value:
            raise ConfigError(
                f"'{self.name}' hesabı için {self.password_env} tanımlı değil "
                "(.env dosyası veya Vercel ortam değişkenleri)."
            )
        return value


@dataclass
class BankConfig:
    name: str
    senders: list[str]
    subject_keywords: list[str] = field(default_factory=list)
    pdf_password_env: str | None = None

    @property
    def pdf_password(self) -> str | None:
        if not self.pdf_password_env:
            return None
        return os.environ.get(self.pdf_password_env) or None

    def matches_sender(self, sender: str) -> bool:
        sender = sender.lower()
        return any(s.lower() in sender for s in self.senders)

    def matches_subject(self, subject: str) -> bool:
        if not self.subject_keywords:
            return True
        subject = subject.casefold()
        return any(k.casefold() in subject for k in self.subject_keywords)


DEFAULTS_FILE = Path(__file__).with_name("defaults.yaml")

# Vercel Neon entegrasyonu DATABASE_URL, eski Vercel Postgres POSTGRES_URL tanımlar.
DATABASE_ENV_VARS = ("DATABASE_URL", "POSTGRES_URL")

# config.yaml olmadan, sadece ortam değişkenleriyle hesap tanımlamak için
ENV_ACCOUNTS = {
    "gmail": ("GMAIL_EMAIL", "GMAIL_APP_PASSWORD"),
    "icloud": ("ICLOUD_EMAIL", "ICLOUD_APP_PASSWORD"),
}


@dataclass
class Config:
    database: str
    attachments_dir: Path | None  # None: PDF'ler diske kaydedilmez (Vercel)
    lookback_days: int
    accounts: list[MailAccount]
    banks: list[BankConfig]
    categories: dict[str, list[str]]

    def bank_by_name(self, name: str) -> BankConfig | None:
        for bank in self.banks:
            if bank.name.casefold() == name.casefold():
                return bank
        return None


def _read_raw(path: Path) -> dict:
    """Ayarları sırasıyla config.yaml dosyasından veya BANKATAKIP_CONFIG değişkeninden okur."""
    if path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if os.environ.get("BANKATAKIP_CONFIG"):
        return yaml.safe_load(os.environ["BANKATAKIP_CONFIG"]) or {}
    return {}


def _parse_account(acc: dict) -> MailAccount:
    provider = acc.get("provider", "custom")
    host, port = PROVIDER_HOSTS.get(provider, (acc.get("host"), acc.get("port", 993)))
    if not host:
        raise ConfigError(f"'{acc.get('name')}' hesabı için host belirtilmeli.")
    return MailAccount(
        name=acc["name"],
        provider=provider,
        email=str(acc["email"]).strip(),
        password_env=acc["password_env"],
        host=acc.get("host", host),
        port=int(acc.get("port", port)),
        folders=acc.get("folders", ["INBOX"]),
    )


def load_config(path: str | Path = "config.yaml") -> Config:
    load_dotenv()
    raw = _read_raw(Path(path))
    defaults = yaml.safe_load(DEFAULTS_FILE.read_text(encoding="utf-8"))

    accounts = [_parse_account(acc) for acc in raw.get("accounts", []) or []]
    names = {a.name for a in accounts}
    for provider, (email_env, password_env) in ENV_ACCOUNTS.items():
        if provider not in names and os.environ.get(email_env):
            accounts.append(_parse_account({
                "name": provider, "provider": provider,
                "email": os.environ[email_env], "password_env": password_env,
            }))

    banks = [
        BankConfig(
            name=b["name"],
            senders=b.get("senders", []),
            subject_keywords=b.get("subject_keywords", []),
            pdf_password_env=b.get("pdf_password_env"),
        )
        for b in (raw.get("banks") or defaults["banks"])
    ]

    database = next((os.environ[v] for v in DATABASE_ENV_VARS if os.environ.get(v)), None)
    database = database or str(raw.get("database", "data/bankatakip.db"))

    attachments = raw.get("attachments_dir", "data/ekstreler")
    if os.environ.get("VERCEL"):  # sunucusuz ortamda dosya sistemi kalıcı değil
        attachments = None

    return Config(
        database=database,
        attachments_dir=Path(attachments) if attachments else None,
        lookback_days=int(os.environ.get("LOOKBACK_DAYS") or raw.get("lookback_days", 365)),
        accounts=accounts,
        banks=banks,
        categories=raw.get("categories") or defaults["categories"],
    )
