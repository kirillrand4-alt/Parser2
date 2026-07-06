#!/usr/bin/env python3
"""Проверка API-ключей checko: какие живые, какие в суточном лимите, какие битые.

Читает ключи из data/api_keys.txt (или из --keys-file / --key), по каждому
делает 1 лёгкий запрос к /v2/search и печатает статус. Ключи не показывает
целиком — только последние 4 символа.

    .venv\\Scripts\\python scripts\\check_keys.py
    .venv\\Scripts\\python scripts\\check_keys.py --keys-file data\\api_keys.txt
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from metalparser.checko import (read_keys_file, build_search_params,       # noqa: E402
                                _is_limit_meta, _parse_keys,
                                SEARCH_URL, DEFAULT_UA)

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
_KEYS_FILE = os.path.join(_DATA, "api_keys.txt")


_LIMIT_WORDS = ("лимит", "превыш", "тариф", "суточн", "limit", "exceed", "quota")


def check_one(session, key: str, debug: bool = False) -> tuple[str, str, str]:
    """→ (last4, категория, сообщение). Категория: alive / limit / invalid / net."""
    tail = f"…{key[-4:]}"
    params = {**build_search_params("25.62", None, True, 1), "key": key}
    try:
        r = session.get(SEARCH_URL, params=params, timeout=20)
    except Exception as exc:  # noqa: BLE001
        return tail, "net", f"сеть: {type(exc).__name__}: {exc}"
    raw = r.text or ""
    try:
        pl = r.json()
    except Exception:  # noqa: BLE001
        pl = {}
    if debug:
        print(f"  [debug] {tail}: HTTP {r.status_code} | тело: {raw[:200]}", file=sys.stderr)
    # ВАЖНО: сначала распознаём ЛИМИТ, и максимально широко — checko на
    # исчерпанной квоте отдаёт HTTP 403, а структура meta бывает разной.
    # Поэтому ищем ключевые слова про лимит по всему телу ответа.
    low = raw.lower()
    if _is_limit_meta(pl) or any(w in low for w in _LIMIT_WORDS):
        msg = ""
        if isinstance(pl, dict):
            msg = ((pl.get("meta") or {}).get("message")
                   or (pl.get("message") if isinstance(pl.get("message"), str) else ""))
        return tail, "limit", (msg or "суточный лимит/тариф").strip()
    if r.status_code in (401, 403):
        return tail, "invalid", f"HTTP {r.status_code} (невалиден/нет доступа)"
    meta = pl.get("meta") if isinstance(pl, dict) else None
    if isinstance(meta, dict) and str(meta.get("status")).lower() == "error":
        return tail, "invalid", str(meta.get("message") or "ошибка")
    return tail, "alive", "ок"


def main():
    p = argparse.ArgumentParser(description="Проверка API-ключей checko (живой/лимит/битый).")
    p.add_argument("--keys-file", default=_KEYS_FILE, help="файл с ключами (по одному на строку)")
    p.add_argument("--key", action="append", default=[], help="ключ(и) прямо в аргументе")
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--show-alive", action="store_true", help="печатать и живые ключи построчно")
    p.add_argument("--debug", action="store_true", help="показать сырой ответ сервера по каждому ключу")
    p.add_argument("--proxy", default=None,
                   help="прокси для запросов, напр. http://user:pass@host:port (или env CHECKO_PROXY)")
    args = p.parse_args()

    keys: list[str] = []
    for k in args.key:
        keys += _parse_keys(k)
    if args.keys_file and os.path.exists(args.keys_file):
        keys += read_keys_file(args.keys_file)
    # уникализируем, сохраняя порядок
    seen, uniq = set(), []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            uniq.append(k)
    if not uniq:
        print(f"Нет ключей: положи их в {args.keys_file} (по одному на строку).", file=sys.stderr)
        return 2

    print(f"Проверяю {len(uniq)} ключ(ей) из {args.keys_file} …", flush=True)
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Accept-Language": "ru,en;q=0.8"})
    proxy = args.proxy or os.environ.get("CHECKO_PROXY")
    if proxy:
        proxy = proxy.strip()
        if proxy.lower().startswith("socks"):
            try:
                import socks  # noqa: F401  (PySocks)
            except Exception:  # noqa: BLE001
                print("Для SOCKS-прокси нужен пакет PySocks. Установи:\n"
                      "    .venv\\Scripts\\pip install \"requests[socks]\"", file=sys.stderr)
                return 3
        session.proxies.update({"http": proxy, "https": proxy})
        print(f"Через прокси: {proxy}", flush=True)

    conc = 1 if args.debug else max(1, args.concurrency)   # в debug — по одному, чтобы лог был читаемым
    results = []
    with ThreadPoolExecutor(max_workers=conc) as ex:
        for res in ex.map(lambda k: check_one(session, k, args.debug), uniq):
            results.append(res)

    cats = {"alive": [], "limit": [], "invalid": [], "net": []}
    for tail, cat, msg in results:
        cats[cat].append((tail, msg))

    # Живые
    if args.show_alive:
        for tail, _ in cats["alive"]:
            print(f"  ЖИВОЙ   {tail}")
    # Проблемные — всегда построчно, чтобы было видно, какие менять
    for tail, msg in cats["limit"]:
        print(f"  ЛИМИТ   {tail}: {msg}")
    for tail, msg in cats["invalid"]:
        print(f"  БИТЫЙ   {tail}: {msg}")
    for tail, msg in cats["net"]:
        print(f"  СЕТЬ    {tail}: {msg}")

    print("─" * 50)
    print(f"Живых:   {len(cats['alive'])}")
    print(f"В лимите:{len(cats['limit'])}  (освободятся после сброса, ~00:00 МСК)")
    print(f"Битых:   {len(cats['invalid'])}  (заменить в {args.keys_file})")
    if cats["net"]:
        print(f"Сеть:    {len(cats['net'])}  (не удалось проверить — повтори)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
