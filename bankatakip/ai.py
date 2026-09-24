"""Kuralların okuyamadığı ekstre ve bildirim maillerini Google Gemini (ücretsiz plan) ile okur.

Sadece GEMINI_API_KEY tanımlıysa çalışır; anahtar Google AI Studio'dan ücretsiz alınır
(https://aistudio.google.com/apikey). Ücretsiz planda günlük/dakikalık istek sınırı vardır:
bildirimler toplu gönderilir, sınır dolunca kalan mailler bir sonraki taramaya bırakılır.

Ortam değişkenleri:
  GEMINI_API_KEY  zorunlu (yoksa yapay zeka kapalıdır)
  GEMINI_MODEL    isteğe bağlı model adı; varsayılan olarak MODELS sırayla denenir
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from .models import ParsedStatement, StatementSummary, Transaction

log = logging.getLogger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# "gemini-flash-latest" Google'ın güncel Flash modeline işaret eden takma addır
MODELS = ("gemini-flash-latest", "gemini-2.5-flash")
BATCH_SIZE = 20
MAX_TEXT = 30_000  # mail başına gönderilecek en fazla karakter (bildirim/özet mailleri çok daha kısa)

# Harcama (+) / iade, ödeme, gelir (-)
POSITIVE = {"harcama", "cikis"}
NEGATIVE = {"iade", "odeme", "gelir", "giris"}


class AIQuotaExceeded(Exception):
    """Ücretsiz planın sınırı doldu; kalan işler sonraki taramaya bırakılmalı."""


class AIError(Exception):
    pass


def ai_enabled() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


# --- şemalar (Gemini responseSchema: OpenAPI alt kümesi) ---

_TX_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "date": {"type": "STRING", "description": "İşlem tarihi, YYYY-AA-GG", "nullable": True},
        "description": {"type": "STRING", "description": "İşyeri / açıklama"},
        "amount": {"type": "STRING", "description": "Pozitif tutar, nokta ondalıklı: 1234.56"},
        "direction": {"type": "STRING", "enum": ["harcama", "cikis", "iade", "odeme", "gelir", "giris"]},
        "category": {"type": "STRING", "nullable": True,
                     "description": "Harcama kategorisi: mevcutlardan biri ya da kısa yeni bir Türkçe ad"},
    },
    "required": ["description", "amount", "direction"],
}

NOTIFICATION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "index": {"type": "INTEGER"},
                    "is_transaction": {"type": "BOOLEAN"},
                    "transaction": {**_TX_SCHEMA, "nullable": True},
                },
                "required": ["index", "is_transaction"],
            },
        }
    },
    "required": ["items"],
}

STATEMENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_statement": {"type": "BOOLEAN"},
        "kind": {"type": "STRING", "enum": ["kredi_karti", "hesap_hareketi", "diger"]},
        "statement_date": {"type": "STRING", "nullable": True},
        "due_date": {"type": "STRING", "nullable": True},
        "period_debt": {"type": "STRING", "nullable": True},
        "minimum_payment": {"type": "STRING", "nullable": True},
        "transactions": {"type": "ARRAY", "items": _TX_SCHEMA},
    },
    "required": ["is_statement", "transactions"],
}

NOTIFICATION_PROMPT = """Aşağıda bir Türk bankasından gelen bildirim mailleri var. Her mail için:
- Tek bir para hareketi bildiriyorsa (kart harcaması, iptal/iade, maaş, para girişi/çıkışı) is_transaction=true yap ve transaction alanını doldur.
- Kampanya, bülten, bilgilendirme gibi işlem içermeyen maillerde is_transaction=false yap.
Kurallar: amount her zaman pozitif ve nokta ondalıklı ("1.250,50 TL" -> "1250.50"). direction: kart harcaması veya hesaptan çıkan para "harcama"/"cikis"; iptal/iade "iade"; maaş veya hesaba giren para "gelir"/"giris". date YYYY-AA-GG; mailde yoksa null. description: işyeri adı (yoksa kısa açıklama). index, maildeki [n] numarasıdır.

"""

STATEMENT_PROMPT = """Bu, {bank} bankasından gelen bir mail veya ekidir (konu: "{subject}").
Kredi kartı ekstresi ya da hesap hareket dökümü ise is_statement=true yap ve bilgileri çıkar; kampanya veya bilgilendirme ise is_statement=false yap.
- kind: kredi kartı ekstresi "kredi_karti", vadesiz hesap dökümü "hesap_hareketi".
- Tarihler YYYY-AA-GG; tutarlar pozitif ve nokta ondalıklı ("4.321,50" -> "4321.50").
- transactions: dökümdeki her işlem. direction: harcama/hesaptan çıkan para "harcama"/"cikis"; ödeme, iade, iptal "odeme"/"iade"; hesaba giren para "gelir"/"giris".
- Ekstre borcu/dönem borcu period_debt, asgari/minimum ödeme minimum_payment, son ödeme tarihi due_date, hesap kesim tarihi statement_date.
"""


def _categories_hint(categories: list[str] | None) -> str:
    names = ", ".join(c for c in (categories or []) if c)
    return ("\nHer işleme category ver: şu kategorilerden uygun olanı seç"
            + (f" ({names})" if names else "")
            + "; hiçbiri uymuyorsa kısa yeni bir Türkçe kategori adı yaz (ör. \"Akaryakıt\", \"Sağlık\").\n")


# --- dönüşümler ---

def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return abs(Decimal(str(value).replace(",", ".").replace(" ", "")))
    except InvalidOperation:
        return None


def _date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _transaction(data: dict, fallback_date: date | None) -> Transaction | None:
    amount = _decimal(data.get("amount"))
    tx_date = _date(data.get("date")) or fallback_date
    if amount is None or tx_date is None:
        return None
    direction = str(data.get("direction", "harcama"))
    sign = -1 if direction in NEGATIVE else 1
    description = " ".join(str(data.get("description") or "").split()) or "İşlem"
    category = " ".join(str(data.get("category") or "").split()) or None
    return Transaction(date=tx_date, description=description[:200], amount=amount * sign,
                       sector=category)


# --- istemci ---

class GeminiClient:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 http: httpx.Client | None = None, timeout: float = 40):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        configured = model or os.environ.get("GEMINI_MODEL")
        self.models = [configured] if configured else list(MODELS)
        self.http = http or httpx.Client(timeout=timeout)

    def _generate(self, parts: list[dict], schema: dict) -> dict:
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0,
            },
        }
        last_error = None
        for model in self.models:
            resp = self.http.post(API_URL.format(model=model), json=body,
                                  headers={"x-goog-api-key": self.api_key})
            if resp.status_code == 404:  # model adı bu hesapta yok; sıradakini dene
                last_error = f"{model} bulunamadı"
                continue
            if resp.status_code == 429:
                raise AIQuotaExceeded("Gemini ücretsiz kullanım sınırı doldu; kalanlar sonraki taramada okunacak.")
            if resp.status_code in (401, 403):
                raise AIError("GEMINI_API_KEY geçersiz veya yetkisiz.")
            if resp.status_code >= 400:
                raise AIError(f"Gemini hatası ({resp.status_code}): {resp.text[:200]}")
            data = resp.json()
            try:
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            except (KeyError, IndexError, json.JSONDecodeError) as exc:
                raise AIError(f"Gemini yanıtı okunamadı: {exc}") from exc
        raise AIError(f"Kullanılabilir Gemini modeli bulunamadı ({last_error}).")

    def extract_notifications(self, items: list[tuple[str, str, datetime | None]],
                              categories: list[str] | None = None) -> list[Transaction | None]:
        """items: (konu, gövde metni, alınma zamanı). Aynı sırada işlem veya None döner."""
        results: list[Transaction | None] = [None] * len(items)
        for start in range(0, len(items), BATCH_SIZE):
            chunk = items[start:start + BATCH_SIZE]
            text = NOTIFICATION_PROMPT + _categories_hint(categories) + "\n\n".join(
                f"[{i}] Konu: {subject}\n{body[:MAX_TEXT]}" for i, (subject, body, _) in enumerate(chunk)
            )
            data = self._generate([{"text": text}], NOTIFICATION_SCHEMA)
            for item in data.get("items", []):
                i = item.get("index")
                if not isinstance(i, int) or not 0 <= i < len(chunk) or not item.get("is_transaction"):
                    continue
                received = chunk[i][2]
                tx = _transaction(item.get("transaction") or {}, received.date() if received else None)
                results[start + i] = tx
        return results

    def extract_statement(self, bank: str, subject: str, text: str | None = None,
                          document: bytes | None = None, mime_type: str = "application/pdf",
                          categories: list[str] | None = None) -> ParsedStatement | None:
        """Mail gövdesinden (text) veya ekten (document) ekstre çıkarır; ekstre değilse None."""
        parts: list[dict] = [{"text": STATEMENT_PROMPT.format(bank=bank, subject=subject)
                              + _categories_hint(categories)}]
        if document is not None:
            parts.append({"inline_data": {"mime_type": mime_type,
                                          "data": base64.b64encode(document).decode()}})
        if text:
            parts.append({"text": text[:MAX_TEXT]})
        data = self._generate(parts, STATEMENT_SCHEMA)
        if not data.get("is_statement"):
            return None
        summary = StatementSummary(
            period_debt=_decimal(data.get("period_debt")),
            minimum_payment=_decimal(data.get("minimum_payment")),
            due_date=_date(data.get("due_date")),
            statement_date=_date(data.get("statement_date")),
        )
        transactions = [t for t in (_transaction(tx, summary.statement_date)
                                    for tx in data.get("transactions") or []) if t]
        return ParsedStatement(bank=bank, summary=summary, transactions=transactions)
