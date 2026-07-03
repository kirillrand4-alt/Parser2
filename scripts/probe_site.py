#!/usr/bin/env python3
"""Зонд для сбора через САЙТ checko (без API).

Проверяет:
  1) страницу каталога по коду ОКВЭД (список компаний, пагинация);
  2) карточку одной компании (контакты).
Сохраняет HTML в файлы и печатает, что удалось распознать. По этому выводу
калибруются селекторы в metalparser/site.py.

Запуск (Windows PowerShell, из папки проекта):
    .venv\\Scripts\\python scripts\\probe_site.py --code 25.62
    .venv\\Scripts\\python scripts\\probe_site.py --code 25.62 --page 2
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from metalparser.checko import extract_contacts_from_html, DEFAULT_UA  # noqa: E402

# Кандидаты URL каталога по коду ОКВЭД (проверим по очереди)
CATALOG_URLS = [
    "https://checko.ru/company/select?code={code}&page={page}",
    "https://checko.ru/company/select?code={code}",
    "https://checko.ru/okved/{code}?page={page}",
]

OGRN_RE = re.compile(r'href="(/company/[^"]*?(\d{13}))"')
INN_RE = re.compile(r'\b(\d{10}|\d{12})\b')


def fetch(sess, url):
    try:
        r = sess.get(url, timeout=30)
        return r
    except requests.RequestException as e:
        print(f"  сетевая ошибка: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="25.62", help="код ОКВЭД для каталога")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--inn", default=None, help="проверить карточку по ИНН/ОГРН напрямую")
    ap.add_argument("--cookie", default=os.environ.get("CHECKO_COOKIE"),
                    help="строка Cookie авторизованного аккаунта (или env CHECKO_COOKIE)")
    args = ap.parse_args()

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept-Language": "ru,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml",
    })
    if args.cookie:
        sess.headers["Cookie"] = args.cookie.strip()
        print("[i] использую переданные куки авторизации\n")

    print("===== КАТАЛОГ =====")
    company_links = []
    for tmpl in CATALOG_URLS:
        url = tmpl.format(code=args.code, page=args.page)
        r = fetch(sess, url)
        if r is None:
            continue
        print(f"\nURL: {url}\nHTTP: {r.status_code}  размер: {len(r.text)}")
        if r.status_code != 200:
            print("  (пропускаю, не 200)")
            continue
        with open(f"catalog_{args.code}_{args.page}.html", "w", encoding="utf-8") as f:
            f.write(r.text)
        links = OGRN_RE.findall(r.text)
        uniq = []
        seen = set()
        for href, ogrn in links:
            if ogrn not in seen:
                seen.add(ogrn)
                uniq.append((href, ogrn))
        print(f"  найдено ссылок на компании: {len(uniq)} (сохранено в catalog_{args.code}_{args.page}.html)")
        for href, ogrn in uniq[:10]:
            print(f"    ОГРН {ogrn}  ->  {href}")
        if uniq:
            company_links = uniq
            break
        else:
            print("  ССЫЛКИ НЕ НАЙДЕНЫ — пришлите фрагмент HTML, где перечислены компании")

    print("\n===== КАРТОЧКА КОМПАНИИ =====")
    target = args.inn
    if not target and company_links:
        target = company_links[0][1]  # ОГРН первой компании
    if not target:
        print("нет компании для проверки (каталог пуст) — задайте --inn")
        return 0

    for url in (f"https://checko.ru/company/{target}",):
        r = fetch(sess, url)
        if r is None:
            continue
        print(f"URL: {url}\nHTTP: {r.status_code}  размер: {len(r.text)}")
        if r.status_code == 200:
            with open("company.html", "w", encoding="utf-8") as f:
                f.write(r.text)
            phones, emails, sites = extract_contacts_from_html(r.text)
            print("  сохранено в company.html")
            print("  телефоны:", phones[:5])
            print("  почты:", emails[:5])
            print("  сайты:", sites[:5])
            # заголовок компании (для распознавания названия)
            m = re.search(r"<title>(.*?)</title>", r.text, re.S)
            if m:
                print("  <title>:", m.group(1).strip()[:120])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
