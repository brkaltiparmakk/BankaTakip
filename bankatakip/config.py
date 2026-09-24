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
        value = os.environ.get(self.password_env)
        if not value:
            raise ConfigError(
                f"'{self.name}' hesabı için {self.password_env} ortam değişkeni (.env) tanımlı değil."
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


@dataclass
class Config:
    database: Path
    attachments_dir: Path
    lookback_days: int
    accounts: list[MailAccount]
    banks: list[BankConfig]
    categories: dict[str, list[str]]

    def bank_by_name(self, name: str) -> BankConfig | None:
        for bank in self.banks:
            if bank.name.casefold() == name.casefold():
                return bank
        return None


def load_config(path: str | Path = "config.yaml") -> Config:
    load_dotenv()
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"{path} bulunamadı. config.example.yaml dosyasını {path} olarak kopyalayın."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    accounts = []
    for acc in raw.get("accounts", []):
        provider = acc.get("provider", "custom")
        host, port = PROVIDER_HOSTS.get(provider, (acc.get("host"), acc.get("port", 993)))
        if not host:
            raise ConfigError(f"'{acc.get('name')}' hesabı için host belirtilmeli.")
        accounts.append(
            MailAccount(
                name=acc["name"],
                provider=provider,
                email=acc["email"],
                password_env=acc["password_env"],
                host=acc.get("host", host),
                port=int(acc.get("port", port)),
                folders=acc.get("folders", ["INBOX"]),
            )
        )

    banks = [
        BankConfig(
            name=b["name"],
            senders=b.get("senders", []),
            subject_keywords=b.get("subject_keywords", []),
            pdf_password_env=b.get("pdf_password_env"),
        )
        for b in raw.get("banks", [])
    ]

    return Config(
        database=Path(raw.get("database", "data/bankatakip.db")),
        attachments_dir=Path(raw.get("attachments_dir", "data/ekstreler")),
        lookback_days=int(raw.get("lookback_days", 365)),
        accounts=accounts,
        banks=banks,
        categories=raw.get("categories", {}) or {},
    )
