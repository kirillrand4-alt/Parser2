"""Выгрузка результатов в CSV и Excel."""
from __future__ import annotations

import csv
from typing import Iterable

from .models import Company

COLUMNS = [
    "ИНН", "ОГРН", "Название", "Полное название", "Основной ОКВЭД",
    "Вид деятельности", "Регион", "Статус", "Адрес",
    "Телефоны", "Почты", "Сайты", "Источник контактов", "Ошибка обогащения",
]


def write_csv(companies: Iterable[Company], path: str) -> int:
    n = 0
    # utf-8-sig — чтобы кириллица корректно открывалась в Excel
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, delimiter=";")
        writer.writeheader()
        for c in companies:
            writer.writerow(c.to_row())
            n += 1
    return n


def write_excel(companies: Iterable[Company], path: str) -> int:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Компании"
    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"

    n = 0
    for c in companies:
        row = c.to_row()
        ws.append([row[col] for col in COLUMNS])
        n += 1

    # ширина колонок (грубая автоподгонка)
    widths = {"Название": 40, "Полное название": 50, "Вид деятельности": 45,
              "Адрес": 50, "Телефоны": 25, "Почты": 30, "Сайты": 30}
    for i, col in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(col, 16)

    wb.save(path)
    return n
