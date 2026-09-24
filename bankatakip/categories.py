"""İşlemlere kategori atama; uygun kategori yoksa bankanın sektöründen yeni kategori açma."""

from __future__ import annotations

from .parsers.generic import GenericParser, tr_fold

OTHER = "Diğer"


def _tr_lower(word: str) -> str:
    # Bankalar çoğu zaman Türkçe karakterleri ASCII'ye çevirir ("ISTASYONU"); o durumda I → i kabul edilir
    if word.isascii():
        return word.lower()
    return word.replace("İ", "i").replace("I", "ı").lower()


def _tr_capitalize(word: str) -> str:
    low = _tr_lower(word)
    first = "İ" if low[:1] == "i" else low[:1].upper()
    return first + low[1:]


def pretty_name(text: str) -> str:
    """"BENZIN ISTASYONU" → "Benzin İstasyonu", "GİYİM MAĞAZASI" → "Giyim Mağazası"."""
    return " ".join(_tr_capitalize(w) for w in text.split())[:40]


class CategoryResolver:
    """Önce anahtar kelime kuralları, sonra sektör/öneri; hiçbiri yoksa kategorisiz ("Diğer").

    known: şu ana kadar kullanılan kategori adları (yeni açılanlar da eklenir), aynı kategorinin
    farklı yazımlarla ("Benzin Istasyonu" / "BENZİN İSTASYONU") iki kez açılmasını önler.
    """

    def __init__(self, parser: GenericParser, known: list[str]):
        self.parser = parser
        self.known = {tr_fold(name): name for name in known if name}

    def resolve(self, description: str, hint: str | None = None) -> str | None:
        category = self.parser.categorize(description)
        if category is None and hint:
            category = self.parser.categorize(hint)
        if category is not None or not hint:
            return category
        key = tr_fold(hint.strip())
        if key in ("", tr_fold(OTHER)):
            return None
        if key not in self.known:
            self.known[key] = pretty_name(hint)
        return self.known[key]

    def apply(self, tx) -> None:
        tx.category = self.resolve(tx.description, tx.sector)
