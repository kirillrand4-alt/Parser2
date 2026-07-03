"""CLI: парсинг ЕГРЮЛ по ОКВЭД металлообработки + выгрузка."""
from __future__ import annotations

import argparse
import sys
import time

from .export import write_csv, write_excel
from .okved import OKVED_SETS, OKVED_SET_LABELS, DEFAULT_SET
from .pipeline import PipelineConfig, run


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

    if args.source == "egrul" and not args.egrul_path:
        print("Для --source egrul укажите путь к дампу ЕГРЮЛ.", file=sys.stderr)
        return 2
    if args.source == "api" and not (args.api_key or __import__("os").environ.get("CHECKO_API_KEY")):
        print("Для --source api нужен ключ: --api-key или env CHECKO_API_KEY.", file=sys.stderr)
        return 2
    if args.source == "site" and not (args.cookie or __import__("os").environ.get("CHECKO_COOKIE")):
        print("ВНИМАНИЕ: --source site без кук — контакты могут быть скрыты. "
              "Задайте --cookie или env CHECKO_COOKIE.", file=sys.stderr)

    if args.count:
        from .pipeline import count_companies
        cfg = PipelineConfig(source="api", okved_set=args.okved_set, extra_okved=args.okved,
                             only_active=not args.all_statuses, api_key=args.api_key,
                             regions=args.region, delay=args.delay)
        per, total = count_companies(cfg, delay=min(args.delay, 0.3))
        for code, n in per:
            print(f"  {code}: {n}")
        print(f"ВСЕГО: {total}")
        return 0

    print(f"Источник: {args.source}", file=sys.stderr)
    print(f"Набор ОКВЭД: {OKVED_SET_LABELS.get(args.okved_set, args.okved_set)}", file=sys.stderr)
    if args.okved:
        print(f"Доп. ОКВЭД: {', '.join(args.okved)}", file=sys.stderr)
    print(f"Статус: {'любой' if args.all_statuses else 'только действующие'}", file=sys.stderr)

    config = PipelineConfig(
        source=args.source,
        egrul_path=args.egrul_path,
        okved_set=args.okved_set,
        extra_okved=args.okved,
        only_active=not args.all_statuses,
        enrich=not args.no_enrich,
        api_key=args.api_key,
        cookie=args.cookie or __import__("os").environ.get("CHECKO_COOKIE"),
        prefer_api=not args.html,
        delay=args.delay,
        limit=args.limit,
        regions=args.region,
    )

    start = time.monotonic()
    found = [0]

    def on_company(c):
        found[0] += 1
        if found[0] % 50 == 0:
            print(f"  найдено: {found[0]}", file=sys.stderr)

    def on_progress(n):
        label = "найдено через API" if args.source == "api" else "просмотрено записей ЕГРЮЛ"
        print(f"  {label}: {n}", file=sys.stderr)

    companies = run(config, on_company=on_company, on_progress=on_progress)

    n_csv = write_csv(companies, args.csv)
    print(f"CSV: {args.csv} ({n_csv} строк)", file=sys.stderr)
    if not args.no_xlsx:
        n_xlsx = write_excel(companies, args.xlsx)
        print(f"Excel: {args.xlsx} ({n_xlsx} строк)", file=sys.stderr)

    enriched = sum(1 for c in companies if c.enriched and not c.enrich_error)
    errors = sum(1 for c in companies if c.enrich_error)
    print(f"Готово за {time.monotonic() - start:.1f} c. Компаний: {len(companies)}; "
          f"с контактами: {enriched}; ошибок обогащения: {errors}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
