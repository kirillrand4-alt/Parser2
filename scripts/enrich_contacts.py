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


def main():
    p = argparse.ArgumentParser(description="Дообогащение контактов по списку ИНН (без поиска).")
    p.add_argument("--input", required=True, help="CSV со столбцом ИНН или txt с ИНН по строке")
    p.add_argument("--output", default="data/enriched.csv")
    p.add_argument("--xlsx", default=None)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--all-statuses", action="store_true")
    p.add_argument("--main-okved-only", action="store_true")
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
    )
    appender = CsvAppender(args.output)
    n = 0
    try:
        for c in iter_enrich(config, inns, skip=skip):
            appender.write(c)
            n += 1
            tel = c.phones[0] if c.phones else "—"
            print(f"[{len(skip) + n}] {c.inn} {(c.name or '')[:40]} | тел: {tel}", flush=True)
    except KeyboardInterrupt:
        print("Остановлено — собранное сохранено.")
    finally:
        appender.close()
    xlsx = args.xlsx or (args.output.rsplit(".", 1)[0] + ".xlsx")
    write_excel_from_csv(args.output, xlsx)
    print(f"Готово: +{n}, всего в {args.output}: {len(read_existing_keys(args.output))}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
