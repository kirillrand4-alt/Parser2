"""Определение и сопоставление ОКВЭД, связанных с металлообработкой.

ОКВЭД-2 (ОК 029-2014). Совпадение проверяется по префиксу кода:
код "25.62" попадает в группу префикса "25"; "28.41" — в "28.4".
"""
from __future__ import annotations

# Наборы префиксов основных ОКВЭД. Ключи используются в CLI (--okved-set).
OKVED_SETS: dict[str, list[str]] = {
    # Ядро: металлургия + готовые металлоизделия/мехобработка + металлообр. станки
    "core": ["24", "25", "28.4"],
    # Узко: только прямая обработка металла
    "narrow": ["25.5", "25.6", "25.7", "25.9"],
    # Широко: ядро + оптовая торговля металлами + ремонт металлоизделий
    "wide": ["24", "25", "28.4", "46.72", "33.11"],
}

DEFAULT_SET = "core"

# Человекочитаемые подписи для отчётов/веба.
OKVED_SET_LABELS: dict[str, str] = {
    "core": "Ядро: 24 (металлургия), 25 (металлоизделия/мехобработка), 28.4 (станки)",
    "narrow": "Узко: 25.5–25.9 (прямая обработка металла)",
    "wide": "Широко: ядро + 46.72 (опт. торговля металлами) + 33.11 (ремонт)",
}


def normalize_code(code: str) -> str:
    """Приводит код ОКВЭД к каноничному виду: цифры и точки, без пробелов.

    '25,62' -> '25.62'; ' 28.41 ' -> '28.41'.
    """
    if not code:
        return ""
    return code.strip().replace(",", ".")


def _matches_prefix(code: str, prefix: str) -> bool:
    """True, если код принадлежит иерархической группе префикса.

    Учитывает структуру ОКВЭД-2: класс — 2 цифры ('25'), далее точка;
    подкласс 'XX.X' ('25.6'), группа 'XX.XX' ('25.62') дополняет подкласс
    цифрой без новой точки; подгруппа 'XX.XX.X' добавляет точку.

    Правила продолжения после префикса:
      * префикс без точки (класс, '25'/'28') — дальше только точка,
        поэтому '25' матчит '25.62', но не '255'/'250';
      * префикс с точкой ('28.4', '25.6') — дальше цифра или точка,
        поэтому '28.4' матчит '28.41', а '25.6' матчит '25.62'.
    """
    if code == prefix:
        return True
    if not code.startswith(prefix):
        return False
    nxt = code[len(prefix)]
    if nxt == ".":
        return True
    return nxt.isdigit() and "." in prefix


def resolve_prefixes(okved_set: str | None, extra: list[str] | None = None) -> list[str]:
    """Возвращает список префиксов для заданного набора и доп. кодов."""
    prefixes: list[str] = []
    if okved_set:
        prefixes.extend(OKVED_SETS.get(okved_set, []))
    if extra:
        prefixes.extend(normalize_code(c) for c in extra if c.strip())
    # Уникализируем, сохраняя порядок.
    seen: set[str] = set()
    result: list[str] = []
    for p in prefixes:
        if p and p not in seen:
            seen.add(p)
            result.append(p)
    return result


class OkvedMatcher:
    """Проверяет, относится ли основной ОКВЭД к целевым группам."""

    def __init__(self, prefixes: list[str]):
        self.prefixes = [normalize_code(p) for p in prefixes]

    def matches(self, code: str) -> bool:
        code = normalize_code(code)
        if not code:
            return False
        return any(_matches_prefix(code, p) for p in self.prefixes)

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"OkvedMatcher({self.prefixes!r})"
