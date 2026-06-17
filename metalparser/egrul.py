"""Потоковый парсер открытых данных ЕГРЮЛ (ФНС, формат XML).

Дамп ЕГРЮЛ — это набор больших XML-файлов (часто упакованных в zip),
где каждое юрлицо описано элементом <СвЮЛ>. Файлы могут весить гигабайты,
поэтому используется потоковый разбор (iterparse) с очисткой элементов.

Формат тегов за годы менялся; парсер написан терпимо:
основной ОКВЭД ищется и в <СвОКВЭД><СвОКВЭДОсн>, и в старом <СвОснВидДеят>.

Источник дампа: платная подписка ФНС «Интеграция сведений ЕГРЮЛ/ЕГРИП»
(nalog.gov.ru) либо готовая база (напр. ofdata.ru). Контактов в ЕГРЮЛ нет —
они добавляются на этапе дообогащения (см. checko.py).
"""
from __future__ import annotations

import io
import os
import zipfile
from typing import Iterator

from lxml import etree

from .models import Company
from .okved import OkvedMatcher

# Ключевые слова статусов, означающих НЕдействующее юрлицо.
_INACTIVE_KEYWORDS = (
    "прекра",      # прекратило деятельность
    "ликвид",      # ликвидация
    "исключен",    # исключено из ЕГРЮЛ
    "исключён",
    "недейств",    # недействующее
    "реорганиз",   # в процессе реорганизации (присоединение и т.п.)
    "банкрот",
)


def _local(tag: str) -> str:
    """Локальное имя тега без namespace."""
    if isinstance(tag, str) and "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _find(elem, *names):
    """Первый прямой потомок с локальным именем из names (рекурсивно вглубь)."""
    for child in elem.iter():
        if _local(child.tag) in names and child is not elem:
            return child
    return None


def _find_all(elem, *names):
    for child in elem.iter():
        if _local(child.tag) in names and child is not elem:
            yield child


def _attr(elem, *names) -> str:
    """Значение первого подходящего атрибута (по локальному имени)."""
    if elem is None:
        return ""
    for k, v in elem.attrib.items():
        if _local(k) in names:
            return (v or "").strip()
    return ""


def _extract_name(sv) -> tuple[str, str]:
    """Возвращает (краткое, полное) наименование."""
    naim = _find(sv, "СвНаимЮЛ")
    full = _attr(naim, "НаимЮЛПолн", "НаимПолнЮЛ") if naim is not None else ""
    short = ""
    if naim is not None:
        sokr = _find(naim, "СвНаимЮЛСокр")
        short = _attr(sokr, "НаимСокр") or _attr(naim, "НаимЮЛСокр")
    return short or full, full or short


def _extract_region(sv) -> str:
    """Наименование/код региона из блока адреса."""
    addr = _find(sv, "СвАдресЮЛ", "СвМесторасп", "АдресРФ")
    if addr is None:
        return ""
    region = _find(addr, "Регион")
    if region is not None:
        name = _attr(region, "НаимРегион") or (region.text or "").strip()
        typ = _attr(region, "ТипРегион")
        if name:
            if not typ:
                return name
            # для городов тип идёт впереди («г Москва»), для краёв/областей — сзади
            if typ.lower().startswith("г"):
                return f"{typ} {name}"
            return f"{name} {typ}"
    # запасной вариант — код региона
    return _attr(addr, "КодРегион") or _attr(_find(sv, "АдрМНЮЛ") or sv, "КодРегион")


def _extract_address(sv) -> str:
    addr = _find(sv, "СвАдресЮЛ", "СвМесторасп", "АдресРФ")
    if addr is None:
        return ""
    full = _attr(addr, "АдресПолн", "Адрес")
    return full


def _extract_main_okved(sv) -> tuple[str, str]:
    """(код, наименование) основного ОКВЭД."""
    osn = _find(sv, "СвОКВЭДОсн", "СвОснВидДеят")
    if osn is not None:
        code = _attr(osn, "КодОКВЭД", "КодВидДеят", "Код")
        name = _attr(osn, "НаимОКВЭД", "Наим", "НаимВидДеят")
        if not code:
            inner = _find(osn, "КодОКВЭД")
            if inner is not None:
                code = (inner.text or "").strip()
        return code, name
    return "", ""


def _determine_status(sv) -> tuple[str, bool]:
    """Возвращает (текст статуса, активно?)."""
    if _find(sv, "СвПрекрЮЛ") is not None:
        st = _find(sv, "СвПрекрЮЛ")
        reason = _attr(st, "НаимСпЗапПрекрЮЛ") or "Прекратило деятельность"
        return reason, False

    status_el = _find(sv, "СвСтатус")
    if status_el is not None:
        text = _attr(status_el, "НаимСтатусЮЛ", "НаимСтатус")
        # вложенный элемент статуса (некоторые версии)
        if not text:
            inner = _find(status_el, "СвСтатус")
            text = _attr(inner, "НаимСтатусЮЛ", "НаимСтатус") if inner is not None else ""
        low = text.lower()
        if any(k in low for k in _INACTIVE_KEYWORDS):
            return text or "Недействующее", False
        if text:
            return text, True

    return "Действующее", True


def parse_company_element(sv, matcher: OkvedMatcher, only_active: bool) -> Company | None:
    """Разбирает один элемент <СвЮЛ>. Возвращает Company или None, если
    запись отфильтрована (не тот ОКВЭД / не действующее)."""
    code, okved_name = _extract_main_okved(sv)
    if not matcher.matches(code):
        return None

    status_text, active = _determine_status(sv)
    if only_active and not active:
        return None

    short, full = _extract_name(sv)
    return Company(
        inn=_attr(sv, "ИНН"),
        ogrn=_attr(sv, "ОГРН"),
        name=short,
        full_name=full,
        okved_code=code,
        okved_name=okved_name,
        region=_extract_region(sv),
        status=status_text,
        address=_extract_address(sv),
    )


def _iter_xml_streams(path: str) -> Iterator[tuple[str, io.BufferedReader]]:
    """Отдаёт пары (имя, файловый поток) для всех XML по пути.

    Поддержка: одиночный .xml, .zip (с .xml внутри), директория (рекурсивно
    .xml и .zip)."""
    if os.path.isdir(path):
        for root, _dirs, files in os.walk(path):
            for fn in sorted(files):
                fp = os.path.join(root, fn)
                low = fn.lower()
                if low.endswith(".xml"):
                    with open(fp, "rb") as f:
                        yield fn, f
                elif low.endswith(".zip"):
                    yield from _iter_zip(fp)
        return
    if path.lower().endswith(".zip"):
        yield from _iter_zip(path)
        return
    with open(path, "rb") as f:
        yield os.path.basename(path), f


def _iter_zip(zip_path: str) -> Iterator[tuple[str, io.BufferedReader]]:
    with zipfile.ZipFile(zip_path) as zf:
        for name in sorted(zf.namelist()):
            if name.lower().endswith(".xml"):
                with zf.open(name) as f:
                    yield name, f
            elif name.lower().endswith(".zip"):
                # вложенный zip (ФНС иногда упаковывает zip в zip)
                with zf.open(name) as inner:
                    data = io.BytesIO(inner.read())
                    with zipfile.ZipFile(data) as zf2:
                        for n2 in sorted(zf2.namelist()):
                            if n2.lower().endswith(".xml"):
                                with zf2.open(n2) as f2:
                                    yield n2, f2


def iter_companies(
    path: str,
    matcher: OkvedMatcher,
    only_active: bool = True,
    progress_every: int = 0,
    on_progress=None,
) -> Iterator[Company]:
    """Главный генератор: проходит дамп ЕГРЮЛ и отдаёт подходящие компании.

    path — файл .xml, архив .zip или директория с дампом.
    """
    scanned = 0
    for _name, stream in _iter_xml_streams(path):
        context = etree.iterparse(stream, events=("end",), recover=True, huge_tree=True)
        for _event, elem in context:
            if _local(elem.tag) != "СвЮЛ":
                continue
            scanned += 1
            company = parse_company_element(elem, matcher, only_active)
            # очистка памяти: удаляем разобранный элемент и предков-сиблингов
            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]
            if progress_every and on_progress and scanned % progress_every == 0:
                on_progress(scanned)
            if company is not None:
                yield company
        del context
    if on_progress:
        on_progress(scanned)
