#!/usr/bin/env python3
"""Ежедневный автосбор базы по списку ОКВЭД с продолжением (докачкой).

Каждый прогон дособирает НОВЫЕ компании в тот же CSV (уже собранные —
пропускает), пока не упрётся в суточный лимит; на следующий день продолжает
с того же места. Поддерживает ротацию API-ключей.

Ключи/куки — из env или data/secrets.json (как в вебе). Несколько ключей —
через запятую в CHECKO_API_KEY.

Разовый прогон (для Планировщика заданий Windows):
    .venv\\Scripts\\python scripts\\daily_collect.py --okved-file data\\okved.txt --csv data\\base.csv

Непрерывный режим (сам ждёт и повторяет каждые 24 ч):
    .venv\\Scripts\\python scripts\\daily_collect.py --okved-file data\\okved.txt --loop
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from metalparser.pipeline import PipelineConfig, iter_run          # noqa: E402
from metalparser.export import CsvAppender, read_existing_keys, write_excel_from_csv  # noqa: E402
from metalparser.checko import read_keys_file                       # noqa: E402

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
_SECRETS = os.path.join(_DATA, "secrets.json")
_KEYS_FILE = os.path.join(_DATA, "api_keys.txt")


def _saved(key):
    try:
        with open(_SECRETS, encoding="utf-8") as f:
            return json.load(f).get(key)
    except Exception:  # noqa: BLE001
        return None


def load_codes(args) -> list[str]:
    tokens = []
    for item in args.okved:
        tokens += re.split(r"[,\s;]+", item)
    if args.okved_file and os.path.exists(args.okved_file):
        for line in open(args.okved_file, encoding="utf-8"):
            line = line.split("#", 1)[0]        # комментарии
            tokens += re.split(r"[,\s;]+", line)
    seen, out = set(), []
    for t in tokens:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _fmt(c, n, total_before):
    """Строка лога по одной компании: номер, ИНН, название, ОКВЭД, контакты."""
    tel = c.phones[0] if c.phones else "—"
    mail = c.emails[0] if c.emails else "—"
    site = c.websites[0] if c.websites else "—"
    extra = ""
    if len(c.phones) > 1 or len(c.emails) > 1 or len(c.websites) > 1:
        extra = f" (+{max(0,len(c.phones)-1)}тел/+{max(0,len(c.emails)-1)}почт/+{max(0,len(c.websites)-1)}сайт)"
    err = f"  ОШИБКА: {c.enrich_error}" if c.enrich_error else ""
    name = (c.name or "")[:45]
    return (f"[{total_before + n}] {c.inn:<12} {name:<45} | {c.okved_code:<7} | "
            f"тел: {tel}  почта: {mail}  сайт: {site}{extra}{err}")


def run_once(config: PipelineConfig, csv_path: str, xlsx_path: str | None) -> int:
    skip = read_existing_keys(csv_path)
    total_before = len(skip)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[{stamp}] старт. В базе уже {total_before} — пропускаю их, добираю новых.", flush=True)
    app = CsvAppender(csv_path)
    n = 0
    try:
        for c in iter_run(config, skip=skip):
            app.write(c)
            n += 1
            print(_fmt(c, n, total_before), flush=True)
    except KeyboardInterrupt:
        print("Остановлено вручную — собранное сохранено.")
    finally:
        app.close()
    if xlsx_path:
        try:
            write_excel_from_csv(csv_path, xlsx_path)
        except Exception as exc:  # noqa: BLE001
            print(f"  (Excel не собран: {exc})")
    total = len(read_existing_keys(csv_path))
    print(f"[{datetime.now().strftime('%H:%M')}] прогон завершён: +{n} новых, всего в базе {total}.")
    return n


def build_parser():
    p = argparse.ArgumentParser(description="Ежедневный автосбор базы по ОКВЭД с докачкой.")
    p.add_argument("--okved", action="append", default=[], help="коды ОКВЭД (через запятую)")
    p.add_argument("--okved-file", default="data/okved.txt", help="файл со списком ОКВЭД")
    p.add_argument("--source", choices=("api", "site"), default="api")
    p.add_argument("--csv", default="data/base.csv", help="файл базы (докачивается)")
    p.add_argument("--xlsx", default="data/base.xlsx")
    p.add_argument("--no-xlsx", action="store_true")
    p.add_argument("--delay", type=float, default=1.5)
    p.add_argument("--all-statuses", action="store_true")
    p.add_argument("--browser", action="store_true", help="site: через настоящий браузер")
    p.add_argument("--loop", action="store_true", help="повторять автоматически")
    p.add_argument("--interval", type=float, default=24.0, help="часы между прогонами в --loop")
    return p


def main():
    args = build_parser().parse_args()
    codes = load_codes(args)
    if not codes:
        print("Не задан список ОКВЭД: --okved 25.62,24.10 или --okved-file data\\okved.txt",
              file=sys.stderr)
        return 2
    print(f"Источник: {args.source}. Кодов ОКВЭД: {len(codes)}. База: {args.csv}")

    config = PipelineConfig(
        source=args.source, okved_set="none", extra_okved=codes,
        only_active=not args.all_statuses,
        api_key=(read_keys_file(_KEYS_FILE) or os.environ.get("CHECKO_API_KEY") or _saved("api_key")),
        cookie=os.environ.get("CHECKO_COOKIE") or _saved("cookie"),
        browser=args.browser,
        user_agent=os.environ.get("CHECKO_UA") or _saved("ua"),
        delay=args.delay,
    )
    xlsx = None if args.no_xlsx else args.xlsx

    if not args.loop:
        run_once(config, args.csv, xlsx)
        return 0

    while True:
        run_once(config, args.csv, xlsx)
        secs = max(60.0, args.interval * 3600)
        print(f"Следующий прогон через {args.interval} ч. (Ctrl+C — остановить)")
        try:
            time.sleep(secs)
        except KeyboardInterrupt:
            print("Остановлено.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
