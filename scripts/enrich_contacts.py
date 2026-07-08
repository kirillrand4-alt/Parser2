#!/usr/bin/env python3
"""Дообогащение контактов по ГОТОВОМУ списку компаний — без поиска/пагинации.

Вход: CSV со столбцом «ИНН» (напр. результат режима «только список») или txt
с ИНН по строке. Для каждого ИНН запрашивается /v2/company (контакты + поля),
параллельно, с ротацией ключей. Докачка: уже дообогащённых в выходном файле
пропускает.

Пример:
    python scripts/enrich_contacts.py --input data/list.csv --output data/full.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from metalparser.pipeline import PipelineConfig, iter_enrich                  # noqa: E402
from metalparser.export import CsvAppender, read_existing_keys, write_excel_from_csv  # noqa: E402
from metalparser.checko import read_keys_file                                 # noqa: E402

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
_KEYS_FILE = os.path.join(_DATA, "api_keys.txt")
_SECRETS = os.path.join(_DATA, "secrets.json")


def _saved(key):
    try:
        with open(_SECRETS, encoding="utf-8") as f:
            return json.load(f).get(key)
    except Exception:  # noqa: BLE001
        return None


def read_inns(path: str) -> list[str]:
    inns: list[str] = []
    if path.lower().endswith(".csv"):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                v = (row.get("ИНН") or row.get("inn") or "").strip()
                if v:
                    inns.append(v)
    else:
        for line in open(path, encoding="utf-8"):
            for tok in re.split(r"[,\s;]+", line.split("#", 1)[0]):
                if tok.strip():
                    inns.append(tok.strip())
    # уникализируем, сохраняя порядок
    seen, out = set(), []
    for i in inns:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _fmt(c, num) -> str:
    """Строка лога: вся собранная инфа по компании (ОКВЭД осн.+доп., контакты)."""
    tel = ", ".join(c.phones) if c.phones else "—"
    mail = ", ".join(c.emails) if c.emails else "—"
    site = ", ".join(c.websites) if c.websites else "—"
    extra = f" +доп.ОКВЭД: {len(c.okved_extra)}" if getattr(c, "okved_extra", None) else ""
    okved = c.okved_code or "—"
    status = f" [{c.status}]" if c.status else ""
    err = f"  ОШИБКА: {c.enrich_error}" if c.enrich_error else ""
    name = (c.name or "")[:40]
    return (f"[{num}] {c.inn:<12} {name:<40}{status} | осн.ОКВЭД {okved}{extra} | "
            f"тел: {tel} | почта: {mail} | сайт: {site}{err}")


def main():
    p = argparse.ArgumentParser(description="Дообогащение контактов по списку ИНН (без поиска).")
    p.add_argument("--input", required=True, help="CSV со столбцом ИНН или txt с ИНН по строке")
    p.add_argument("--output", default="data/enriched.csv")
    p.add_argument("--xlsx", default=None)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--all-statuses", action="store_true")
    p.add_argument("--main-okved-only", action="store_true")
    p.add_argument("--proxy", default=None,
                   help="прокси для запросов, напр. http://user:pass@host:port (или env CHECKO_PROXY)")
    args = p.parse_args()

    if not os.path.exists(args.input):
        print(f"Нет файла: {args.input}", file=sys.stderr)
        return 2
    inns = read_inns(args.input)
    skip = read_existing_keys(args.output)
    print(f"ИНН на входе: {len(inns)}; уже в {args.output}: {len(skip)} (пропущу).", flush=True)

    config = PipelineConfig(
        source="api", api_key=read_keys_file(_KEYS_FILE) or os.environ.get("CHECKO_API_KEY") or _saved("api_key"),
        only_active=not args.all_statuses, main_okved_only=args.main_okved_only,
        concurrency=args.concurrency, delay=args.delay,
        proxy=args.proxy or os.environ.get("CHECKO_PROXY"),
    )
    appender = CsvAppender(args.output)
    invalid_keys = set()
    n = 0
    try:
        for c in iter_enrich(config, inns, skip=skip, invalid_out=invalid_keys):
            appender.write(c)
            n += 1
            print(_fmt(c, len(skip) + n), flush=True)
    except KeyboardInterrupt:
        print("Остановлено — собранное сохранено.")
    finally:
        appender.close()
    if invalid_keys:                     # выкидываем битые ключи из файла
        from metalparser.checko import prune_keys_file
        removed = prune_keys_file(_KEYS_FILE, invalid_keys)
        if removed:
            print(f"Удалено недействительных ключей из api_keys.txt: {removed} "
                  f"(перенесены в data\\dead_keys.txt).")
    xlsx = args.xlsx or (args.output.rsplit(".", 1)[0] + ".xlsx")
    write_excel_from_csv(args.output, xlsx)
    print(f"Готово: +{n}, всего в {args.output}: {len(read_existing_keys(args.output))}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
