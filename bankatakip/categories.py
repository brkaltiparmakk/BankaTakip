"""İşlemlere kategori atama; uygun kategori yoksa bankanın sektöründen yeni kategori açma."""

from __future__ import annotations

from .parsers.generic import GenericParser, _keyword_pattern, tr_fold

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
    """"BENZIN ISTASYONU" → "Benzin İstasyonu", "GİYİM MAĞAZASI" → "Giyim Mağazası",
    "SINEMA/TIYATRO" → "Sinema / Tiyatro"."""
    parts = [" ".join(_tr_capitalize(w) for w in part.split()) for part in text.split("/")]
    return " / ".join(p for p in parts if p)[:40]


def compile_rules(rules) -> list[tuple]:
    """Panelden eklenen kurallar [(ifade, kategori)] → derlenmiş desenler. Anahtar kelime
    kurallarından önce uygulanır."""
    return [(_keyword_pattern(p), c) for p, c in rules if p and p.strip() and c]


def match_rules(compiled: list[tuple], description: str) -> str | None:
    folded = tr_fold(description)
    return next((c for pattern, c in compiled if pattern.search(folded)), None)


class CategoryResolver:
    """Önce anahtar kelime kuralları, sonra sektör/öneri; hiçbiri yoksa kategorisiz ("Diğer").

    known: şu ana kadar kullanılan kategori adları (yeni açılanlar da eklenir), aynı kategorinin
    farklı yazımlarla ("Benzin Istasyonu" / "BENZİN İSTASYONU") iki kez açılmasını önler.
    """

    def __init__(self, parser: GenericParser, known: list[str], rules=()):
        self.parser = parser
        self.known = {tr_fold(name): name for name in known if name}
        self.rules = compile_rules(rules)
        for _, category in self.rules:
            self.known.setdefault(tr_fold(category), category)

    def resolve(self, description: str, hint: str | None = None) -> str | None:
        category = match_rules(self.rules, description) or self.parser.categorize(description)
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


def recategorizer(parser: GenericParser, categories: dict[str, list[str]], rules=()):
    """Kurallar değiştiğinde eski kayıtlar için karar fonksiyonu (Storage.recategorize ile).

    - Anahtar kelimeye uyan işlem o kategoriye geçer.
    - Tanımlı bir kategorideyken artık hiçbir kurala uymuyorsa: eski eşleşme gevşek alt-dize
      kuralından geldiyse ("taksi" → "TAKSİTLİ") kategorisi kaldırılır, yoksa (ör. yapay zeka
      ataması) korunur.
    - Bankanın sektöründen açılmış kategoride açıklama sektör adıdır; yeni kurallarla yeniden
      adlandırılır ("EGLENCE" → "Eğlence" kategorisi).
    """
    resolver = CategoryResolver(parser, list(categories))
    compiled = compile_rules(rules)

    def decide(description: str, old: str | None) -> str | None:
        new = match_rules(compiled, description) or parser.categorize(description)
        if new is not None or old is None:
            return new
        if old in categories:
            folded = tr_fold(description)
            loose = any(tr_fold(k.strip()) in folded for k in categories[old] if k.strip())
            return None if loose else old
        return resolver.resolve(description, description)

    return decide
