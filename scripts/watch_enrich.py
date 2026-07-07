#!/usr/bin/env python3
"""Автозапуск дообогащения по API при появлении НОВОГО ключа в data/api_keys.txt.

Следит за файлом ключей. Как только там появляется новый ключ — запускает один
проход дообогащения (enrich по ИНН из --input в --output) со ВСЕМИ текущими
ключами через прокси. Докачиваемо: уже обогащённые ИНН пропускает. Работает в
фоне, пока не остановишь (Ctrl+C).

Пример (PowerShell):
    .venv\\Scripts\\python scripts\\watch_enrich.py --input data\\list.csv --output data\\full.csv ^
        --proxy "socks5h://user:pass@host:port" --concurrency 5
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from metalparser.pipeline import PipelineConfig, iter_enrich                    # noqa: E402
from metalparser.export import CsvAppender, read_existing_keys, write_excel_from_csv  # noqa: E402
from metalparser.checko import read_keys_file, _parse_keys                      # noqa: E402

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
    inns, seen = [], set()
    if path.lower().endswith(".csv"):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                v = (row.get("ИНН") or row.get("inn") or "").strip()
                if v and v not in seen:
                    seen.add(v); inns.append(v)
    else:
        for line in open(path, encoding="utf-8"):
            for tok in re.split(r"[,\s;]+", line.split("#", 1)[0]):
                tok = tok.strip()
                if tok and tok not in seen:
                    seen.add(tok); inns.append(tok)
    return inns


def current_keys() -> set:
    return set(_parse_keys(read_keys_file(_KEYS_FILE)))


def run_pass(args, keys_str: str) -> int:
    inns = read_inns(args.input)
    skip = read_existing_keys(args.output)
    remaining = [i for i in inns if i not in skip]
    stamp = time.strftime("%Y-%m-%d %H:%M")
    print(f"[{stamp}] проход: в базе {len(skip)}, осталось дообогатить {len(remaining)}. "
          f"Ключей: {len(_parse_keys(keys_str))}.", flush=True)
    if not remaining:
        print("  всё уже дообогащено — ждём.", flush=True)
        return 0
    config = PipelineConfig(
        source="api", api_key=keys_str,
        only_active=not args.all_statuses, main_okved_only=args.main_okved_only,
        concurrency=args.concurrency, delay=args.delay,
        proxy=args.proxy or os.environ.get("CHECKO_PROXY") or _saved("proxy"),
    )
    app = CsvAppender(args.output)
    n = 0
    try:
        for c in iter_enrich(config, remaining, skip=skip):
            app.write(c)
            n += 1
            tel = c.phones[0] if c.phones else "—"
            print(f"  [{len(skip) + n}] {c.inn} {(c.name or '')[:36]:<36} | ОКВЭД {c.okved_code or '—':<8} | тел: {tel}", flush=True)
    except KeyboardInterrupt:
        raise
    finally:
        app.close()
    try:
        write_excel_from_csv(args.output, args.output.rsplit(".", 1)[0] + ".xlsx")
    except Exception:  # noqa: BLE001
        pass
    print(f"  проход завершён: +{n}. Всего в базе {len(read_existing_keys(args.output))}.", flush=True)
    return n


def main():
    p = argparse.ArgumentParser(description="Автозапуск API-дообогащения при появлении нового ключа.")
    p.add_argument("--input", required=True, help="CSV со столбцом ИНН")
    p.add_argument("--output", default="data/full.csv")
    p.add_argument("--proxy", default=None, help="прокси (или env CHECKO_PROXY)")
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--interval", type=float, default=30.0, help="как часто проверять файл ключей, сек")
    p.add_argument("--main-okved-only", action="store_true")
    p.add_argument("--all-statuses", action="store_true")
    p.add_argument("--run-now", action="store_true", help="сразу сделать проход на старте (не ждать нового ключа)")
    args = p.parse_args()

    if not os.path.exists(args.input):
        print(f"Нет файла: {args.input}", file=sys.stderr)
        return 2

    print(f"Слежу за {os.path.abspath(_KEYS_FILE)}. Новый ключ → запускаю дообогащение. "
          f"Проверка каждые {args.interval:g} c. (Ctrl+C — стоп)", flush=True)
    seen = current_keys()
    print(f"Сейчас ключей в файле: {len(seen)}", flush=True)
    if args.run_now and seen:
        run_pass(args, ",".join(sorted(seen)))

    try:
        while True:
            time.sleep(max(5.0, args.interval))
            cur = current_keys()
            new = cur - seen
            if new:
                print(f"\n>>> Обнаружены новые ключи: {len(new)} (всего в файле {len(cur)}). "
                      f"Запускаю дообогащение.", flush=True)
                seen = cur
                run_pass(args, ",".join(sorted(cur)))
            # ключи не менялись — молча ждём дальше
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
