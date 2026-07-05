"""CLI: парсинг ЕГРЮЛ по ОКВЭД металлообработки + выгрузка."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from .export import write_excel_from_csv, CsvAppender, read_existing_keys
from .okved import OKVED_SETS, OKVED_SET_LABELS, DEFAULT_SET
from .pipeline import PipelineConfig, iter_run

_SECRETS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "secrets.json")


def _saved(key: str) -> str | None:
    """Сохранённый секрет из data/secrets.json (общий с веб-интерфейсом)."""
    try:
        with open(_SECRETS_PATH, encoding="utf-8") as fh:
            return json.load(fh).get(key)
    except Exception:  # noqa: BLE001
        return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="metalparser",
        description="Сбор действующих компаний по основному ОКВЭД (металлообработка) "
                    "из дампа ЕГРЮЛ ФНС с дообогащением контактов через checko.ru.",
    )
    p.add_argument("egrul_path", nargs="?", default="",
                   help="путь к дампу ЕГРЮЛ (для --source egrul): файл .xml, архив .zip или папка")
    p.add_argument("--source", choices=("egrul", "api", "site"), default="egrul",
                   help="источник: egrul (дамп) | api (checko /v2/search) | site (каталог checko по кукам). По умолч.: %(default)s")
    p.add_argument("--region", action="append", default=[],
                   help="код региона для --source api (можно несколько; пусто = вся РФ)")
    p.add_argument("--cookie", default=None,
                   help="строка Cookie авторизованного checko для --source site (или env CHECKO_COOKIE)")
    p.add_argument("--browser", action="store_true",
                   help="для --source site: сбор через настоящий браузер (Playwright) — надёжнее против блокировок")
    p.add_argument("--okved-set", choices=sorted(OKVED_SETS) + ["none"], default=DEFAULT_SET,
                   help="набор основных ОКВЭД (none = только из --okved). По умолчанию: %(default)s")
    p.add_argument("--okved", action="append", default=[],
                   help="доп. префикс ОКВЭД (можно несколько раз), напр. --okved 28.41")
    p.add_argument("--all-statuses", action="store_true",
                   help="не фильтровать по статусу (по умолчанию только действующие)")
    p.add_argument("--no-enrich", action="store_true",
                   help="не дообогащать контактами через checko (только данные ЕГРЮЛ)")
    p.add_argument("--api-key", default=None, help="ключ API checko (или env CHECKO_API_KEY)")
    p.add_argument("--html", action="store_true",
                   help="принудительно HTML-режим checko, даже при наличии ключа")
    p.add_argument("--delay", type=float, default=1.5, help="пауза между запросами к checko, сек")
    p.add_argument("--limit", type=int, default=0, help="ограничить число компаний (0 = без лимита)")
    p.add_argument("--csv", default="companies.csv", help="путь к CSV (по умолчанию: %(default)s)")
    p.add_argument("--xlsx", default="companies.xlsx", help="путь к Excel (по умолчанию: %(default)s)")
    p.add_argument("--no-xlsx", action="store_true", help="не создавать Excel")
    p.add_argument("--count", action="store_true",
                   help="только посчитать число компаний по каждому ОКВЭД (без скачивания, --source api)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    api_key = args.api_key or os.environ.get("CHECKO_API_KEY") or _saved("api_key")
    cookie = args.cookie or os.environ.get("CHECKO_COOKIE") or _saved("cookie")

    # --okved можно передавать списком через запятую/пробел
    extra_okved = []
    for item in args.okved:
        extra_okved += [t.strip() for t in re.split(r"[,\s;]+", item) if t.strip()]

    if args.source == "egrul" and not args.egrul_path:
        print("Для --source egrul укажите путь к дампу ЕГРЮЛ.", file=sys.stderr)
        return 2
    if args.source == "api" and not api_key:
        print("Для --source api нужен ключ: --api-key, env CHECKO_API_KEY или сохранённый.", file=sys.stderr)
        return 2
    if args.source == "site" and not cookie and not args.browser:
        print("ВНИМАНИЕ: --source site без кук — контакты могут быть скрыты. "
              "Задайте --cookie, env CHECKO_COOKIE, сохраните через веб "
              "или используйте --browser с сохранённым профилем (browser_login.py).", file=sys.stderr)

    if args.count:
        from .pipeline import count_companies
        cfg = PipelineConfig(source="api", okved_set=args.okved_set, extra_okved=extra_okved,
                             only_active=not args.all_statuses, api_key=api_key,
                             regions=args.region, delay=args.delay)
        per, total = count_companies(cfg, delay=min(args.delay, 0.3))
        for code, n in per:
            print(f"  {code}: {n}")
        print(f"ВСЕГО: {total}")
        return 0

    print(f"Источник: {args.source}", file=sys.stderr)
    print(f"Набор ОКВЭД: {OKVED_SET_LABELS.get(args.okved_set, args.okved_set)}", file=sys.stderr)
    if extra_okved:
        print(f"Доп. ОКВЭД: {', '.join(extra_okved)}", file=sys.stderr)
    print(f"Статус: {'любой' if args.all_statuses else 'только действующие'}", file=sys.stderr)

    config = PipelineConfig(
        source=args.source,
        egrul_path=args.egrul_path,
        okved_set=args.okved_set,
        extra_okved=extra_okved,
        only_active=not args.all_statuses,
        enrich=not args.no_enrich,
        api_key=api_key,
        cookie=cookie,
        browser=args.browser,
        user_agent=os.environ.get("CHECKO_UA") or _saved("ua"),
        prefer_api=not args.html,
        delay=args.delay,
        limit=args.limit,
        regions=args.region,
    )

    start = time.monotonic()

    def line(c, i):
        tel = c.phones[0] if c.phones else "—"
        mail = c.emails[0] if c.emails else "—"
        site = c.websites[0] if c.websites else "—"
        err = f"  ОШИБКА: {c.enrich_error}" if c.enrich_error else ""
        return (f"[{i}] {c.inn:<12} {(c.name or '')[:45]:<45} | {c.okved_code:<7} | "
                f"тел: {tel}  почта: {mail}  сайт: {site}{err}")

    # Докачка: пропускаем уже собранные (по существующему CSV) для api/site
    skip = read_existing_keys(args.csv) if args.source in ("site", "api") else set()
    base = len(skip)
    if skip:
        print(f"Докачка: в {args.csv} уже {base} компаний — пропускаю их.", file=sys.stderr)

    # Потоковая запись: каждая компания сразу в CSV (не теряется при остановке)
    appender = CsvAppender(args.csv)
    companies = []
    try:
        for c in iter_run(config, skip=skip):
            appender.write(c)
            companies.append(c)
            print(line(c, base + len(companies)), file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        print("\nОстановлено вручную — собранное сохранено в CSV.", file=sys.stderr)
    finally:
        appender.close()

    total_in_csv = len(read_existing_keys(args.csv))
    print(f"CSV: {args.csv} (всего {total_in_csv} компаний)", file=sys.stderr)
    if not args.no_xlsx:
        n_xlsx = write_excel_from_csv(args.csv, args.xlsx)
        print(f"Excel: {args.xlsx} ({n_xlsx} строк)", file=sys.stderr)

    enriched = sum(1 for c in companies if c.enriched and not c.enrich_error)
    errors = sum(1 for c in companies if c.enrich_error)
    print(f"Готово за {time.monotonic() - start:.1f} c. За этот запуск: {len(companies)}; "
          f"с контактами: {enriched}; ошибок: {errors}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
