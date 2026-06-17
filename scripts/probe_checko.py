#!/usr/bin/env python3
"""Зонд для калибровки API checko.

Печатает реальный JSON ответов /v2/search и /v2/company, чтобы свериться
с именами параметров и структурой под конкретный ключ. Тратит 1–2 запроса.

Примеры:
    export CHECKO_API_KEY=ваш_ключ
    python scripts/probe_checko.py
    # переопределить параметры поиска под доку:
    python scripts/probe_checko.py --param by=оквэд --param query=25 --param region=77
    # проверить конкретную компанию:
    python scripts/probe_checko.py --inn 7700000002
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests

SEARCH_URL = "https://api.checko.ru/v2/search"
COMPANY_URL = "https://api.checko.ru/v2/company"


def show(title, resp):
    print(f"\n===== {title} =====")
    print(f"HTTP {resp.status_code}  {resp.url}")
    try:
        print(json.dumps(resp.json(), ensure_ascii=False, indent=2)[:6000])
    except Exception:
        print(resp.text[:3000])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=os.environ.get("CHECKO_API_KEY"))
    ap.add_argument("--param", action="append", default=[],
                    help="доп./переопределяющий параметр поиска вида key=value")
    ap.add_argument("--inn", default=None, help="проверить /v2/company по ИНН")
    ap.add_argument("--no-search", action="store_true")
    args = ap.parse_args()

    if not args.key:
        print("Нет ключа: задайте --key или env CHECKO_API_KEY", file=sys.stderr)
        return 2

    # значения по умолчанию (подтверждены докой checko); переопределяются через --param
    search_params = {"key": args.key, "by": "okved", "obj": "org",
                     "query": "25", "active": "true", "page": "1"}
    for p in args.param:
        if "=" in p:
            k, v = p.split("=", 1)
            search_params[k.strip()] = v.strip()

    sess = requests.Session()

    search_summary = None
    first_inn = args.inn
    if not args.no_search:
        r = sess.get(SEARCH_URL, params=search_params, timeout=30)
        show("SEARCH /v2/search", r)
        try:
            data = r.json()
            block = data.get("data", data)
            if isinstance(block, dict):
                search_summary = (
                    f"query={search_params.get('query')}  "
                    f"ЗапВсего={block.get('ЗапВсего')}  "
                    f"СтрВсего={block.get('СтрВсего')}  "
                    f"записей_на_стр={len(block.get('Записи') or [])}"
                )
            records = block.get("Записи") if isinstance(block, dict) else None
            print("\n----- КАЛИБРОВКА: поиск -----")
            if isinstance(block, dict):
                print("data ключи:", list(block.keys()))
            if records:
                print("записей на странице:", len(records))
                print("ключи записи:", list(records[0].keys()))
                print("первая запись:", json.dumps(records[0], ensure_ascii=False)[:1500])
            if first_inn is None:
                first_inn = _find_first_inn(data)
                if first_inn:
                    print("[авто] первый ИНН:", first_inn)
        except Exception as e:
            print("разбор поиска не удался:", e)

    if first_inn:
        r = sess.get(COMPANY_URL, params={"key": args.key, "inn": first_inn}, timeout=30)
        show("COMPANY /v2/company", r)
        try:
            data = r.json().get("data", {})
            print("\n----- КАЛИБРОВКА: компания -----")
            print("data ключи:", list(data.keys()))
            for f in ("ИНН", "ОГРН", "НаимСокр", "НаимПолн", "ОКВЭД", "Статус",
                      "Регион", "ЮрАдрес", "Адрес", "Контакты"):
                if f in data:
                    print(f"  {f} =", json.dumps(data[f], ensure_ascii=False)[:400])
        except Exception as e:
            print("разбор компании не удался:", e)
    else:
        print("\n[i] ИНН для company-зонда не найден — задайте --inn вручную.")

    if search_summary:
        print("\n========== ИТОГ ПОИСКА ==========")
        print(search_summary)
    return 0


def _find_first_inn(node):
    """Рекурсивно ищет первое значение похожее на ИНН (10–12 цифр) по ключу ИНН/inn."""
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).lower() in ("инн", "inn") and isinstance(v, (str, int)):
                s = str(v)
                if s.isdigit() and 10 <= len(s) <= 12:
                    return s
        for v in node.values():
            r = _find_first_inn(v)
            if r:
                return r
    elif isinstance(node, list):
        for item in node:
            r = _find_first_inn(item)
            if r:
                return r
    return None


if __name__ == "__main__":
    raise SystemExit(main())
