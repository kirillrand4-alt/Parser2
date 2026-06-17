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
    p.add_argument("egrul_path", help="путь к дампу ЕГРЮЛ: файл .xml, архив .zip или папка")
    p.add_argument("--okved-set", choices=sorted(OKVED_SETS), default=DEFAULT_SET,
                   help="набор основных ОКВЭД (по умолчанию: %(default)s)")
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    print(f"Набор ОКВЭД: {OKVED_SET_LABELS.get(args.okved_set, args.okved_set)}", file=sys.stderr)
    if args.okved:
        print(f"Доп. ОКВЭД: {', '.join(args.okved)}", file=sys.stderr)
    print(f"Статус: {'любой' if args.all_statuses else 'только действующие'}", file=sys.stderr)

    config = PipelineConfig(
        egrul_path=args.egrul_path,
        okved_set=args.okved_set,
        extra_okved=args.okved,
        only_active=not args.all_statuses,
        enrich=not args.no_enrich,
        api_key=args.api_key,
        prefer_api=not args.html,
        delay=args.delay,
        limit=args.limit,
    )

    start = time.monotonic()
    found = [0]

    def on_company(c):
        found[0] += 1
        if found[0] % 50 == 0:
            print(f"  найдено: {found[0]}", file=sys.stderr)

    def on_progress(scanned):
        print(f"  просмотрено записей ЕГРЮЛ: {scanned}", file=sys.stderr)

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
