#!/usr/bin/env python3
"""Дообогащение контактов ЧЕРЕЗ САЙТ checko по ИНН (без API-лимита).

По каждому ИНН: поиск https://checko.ru/search?query=<ИНН> → ОГРН → карточка →
телефоны/почты/сайты. Работает через прокси. Докачиваемо (уже обработанные ИНН
пропускает). ОКВЭД/название/регион переносятся из входного CSV (сайт отдаёт в
основном контакты). Данные checko доступны без логина — куки не обязательны.

Примеры (PowerShell):
    .venv\\Scripts\\python scripts\\enrich_site.py --input data\\list.csv --output data\\full_site.csv ^
        --proxy "socks5h://user:pass@host:port" --delay 1.5

    # через настоящий браузер (если страница поиска рисуется только JS):
    .venv\\Scripts\\python scripts\\enrich_site.py --input data\\list.csv --output data\\full_site.csv --browser
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from metalparser.pipeline import PipelineConfig, iter_enrich_site               # noqa: E402
from metalparser.export import CsvAppender, read_existing_keys, write_excel_from_csv  # noqa: E402

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
_SECRETS = os.path.join(_DATA, "secrets.json")


def _saved(key):
    try:
        with open(_SECRETS, encoding="utf-8") as f:
            return json.load(f).get(key)
    except Exception:  # noqa: BLE001
        return None


def read_records(path: str) -> list[dict]:
    """Записи из входа: ИНН (+ ОГРН/ОКВЭД/название/регион, если есть — переносим)."""
    out, seen = [], set()
    if path.lower().endswith(".csv"):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                inn = (row.get("ИНН") or row.get("inn") or "").strip()
                if not inn or inn in seen:
                    continue
                seen.add(inn)
                out.append({
                    "inn": inn,
                    "ogrn": (row.get("ОГРН") or row.get("ogrn") or "").strip(),
                    "name": (row.get("Название") or row.get("Полное название") or "").strip(),
                    "okved_code": (row.get("Основной ОКВЭД") or "").strip(),
                    "okved_name": (row.get("Вид деятельности") or "").strip(),
                    "okved_extra": (row.get("Доп. ОКВЭД") or "").strip(),
                    "region": (row.get("Регион") or "").strip(),
                })
    else:  # txt: ИНН по строке
        for line in open(path, encoding="utf-8"):
            for tok in re.split(r"[,\s;]+", line.split("#", 1)[0]):
                tok = tok.strip()
                if tok and tok not in seen:
                    seen.add(tok)
                    out.append({"inn": tok})
    return out


def _fmt(c, num) -> str:
    tel = ", ".join(c.phones) if c.phones else "—"
    mail = ", ".join(c.emails) if c.emails else "—"
    site = ", ".join(c.websites) if c.websites else "—"
    okved = c.okved_code or "—"
    err = f"  ОШИБКА: {c.enrich_error}" if c.enrich_error else ""
    name = (c.name or "")[:38]
    return (f"[{num}] {c.inn:<12} {name:<38} | ОКВЭД {okved:<8} | "
            f"тел: {tel} | почта: {mail} | сайт: {site}{err}")


def main():
    p = argparse.ArgumentParser(description="Дообогащение через САЙТ checko по ИНН (поиск→карточка).")
    p.add_argument("--input", required=True, help="CSV со столбцом ИНН (или txt с ИНН по строке)")
    p.add_argument("--output", default="data/full_site.csv")
    p.add_argument("--xlsx", default=None)
    p.add_argument("--delay", type=float, default=1.5, help="пауза между компаниями, сек")
    p.add_argument("--proxy", default=None,
                   help="прокси, напр. socks5h://user:pass@host:port (или env CHECKO_PROXY)")
    p.add_argument("--browser", action="store_true",
                   help="через настоящий браузер (если поиск рисуется только JS)")
    p.add_argument("--cookie", default=None, help="куки (обычно не нужны — данные открыты)")
    p.add_argument("--all-statuses", action="store_true")
    args = p.parse_args()

    if not os.path.exists(args.input):
        print(f"Нет файла: {args.input}", file=sys.stderr)
        return 2
    recs = read_records(args.input)
    skip = read_existing_keys(args.output)
    print(f"На входе: {len(recs)} ИНН; уже в {args.output}: {len(skip)} — пропущу.", flush=True)

    config = PipelineConfig(
        source="site",
        cookie=args.cookie or os.environ.get("CHECKO_COOKIE") or _saved("cookie"),
        only_active=not args.all_statuses,
        browser=args.browser,
        user_agent=os.environ.get("CHECKO_UA") or _saved("ua"),
        delay=args.delay,
        proxy=args.proxy or os.environ.get("CHECKO_PROXY") or _saved("proxy"),
    )
    appender = CsvAppender(args.output)
    n = 0
    try:
        for c in iter_enrich_site(config, recs, skip=skip):
            appender.write(c)
            n += 1
            print(_fmt(c, len(skip) + n), flush=True)
    except KeyboardInterrupt:
        print("Остановлено — собранное сохранено.")
    finally:
        appender.close()
    xlsx = args.xlsx or (args.output.rsplit(".", 1)[0] + ".xlsx")
    try:
        write_excel_from_csv(args.output, xlsx)
    except Exception as exc:  # noqa: BLE001
        print(f"  (Excel не собран: {exc})")
    print(f"Готово: +{n}, всего в {args.output}: {len(read_existing_keys(args.output))}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
