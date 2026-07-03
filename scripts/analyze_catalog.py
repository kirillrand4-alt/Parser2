#!/usr/bin/env python3
"""Анализ сохранённого HTML каталога checko: печатает компактную сводку,
по которой калибруется парсер (без пересылки всего файла).

Запуск:
    .venv\\Scripts\\python scripts\\analyze_catalog.py catalog_25.62_1.html
"""
from __future__ import annotations

import re
import sys
from collections import Counter


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "catalog_25.62_1.html"
    html = open(path, encoding="utf-8", errors="replace").read()
    print(f"файл: {path}  размер: {len(html)}")

    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    print("title:", (m.group(1).strip()[:150] if m else "—"))

    h1 = re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
    if h1:
        print("h1:", re.sub(r"<[^>]+>", "", h1[0]).strip()[:150])

    # все ссылки, сгруппированные по первому сегменту пути
    hrefs = re.findall(r'href="([^"#]+)"', html)
    seg = Counter()
    for h in hrefs:
        if h.startswith("http"):
            h2 = re.sub(r"^https?://[^/]+", "", h) or "/"
        else:
            h2 = h
        parts = [p for p in h2.split("/") if p]
        seg["/" + (parts[0] if parts else "")] += 1
    print("\nссылки по сегментам:", dict(seg.most_common(15)))

    comp = [h for h in hrefs if "/company/" in h]
    print(f"\n/company/ ссылок: {len(comp)}")
    for h in comp[:10]:
        print("   ", h[:120])

    # 13-значные числа (ОГРН) в любом виде
    ogrns = set(re.findall(r"\b\d{13}\b", html))
    print(f"\n13-значных чисел (ОГРН?): {len(ogrns)}", list(ogrns)[:5])

    # встроенные JSON-данные (SPA)
    for marker in ("__NEXT_DATA__", "window.__", "application/json", "nuxt", "data-page"):
        cnt = html.count(marker)
        if cnt:
            print(f"маркер '{marker}': {cnt}")

    # ключевые слова состояния
    for kw in ("Войти", "войдите", "одписк", "апрос", "не найдено", "ничего не найдено",
               "Показать", "аптча", "captcha", "робот"):
        c = html.count(kw)
        if c:
            print(f"слово '{kw}': {c}")

    # фрагменты вокруг упоминаний кода
    code = re.search(r"catalog_([\d.]+)_", path)
    needle = code.group(1) if code else "25.62"
    idxs = [m.start() for m in re.finditer(re.escape(needle), html)][:3]
    print(f"\nупоминаний '{needle}': {len(re.findall(re.escape(needle), html))}")
    for i in idxs:
        frag = re.sub(r"\s+", " ", html[max(0, i - 200):i + 200])
        print("  …", frag[:380], "…\n")

    # формы (возможно, список за POST/фильтрами)
    forms = re.findall(r"<form[^>]*>", html, re.I)
    for f in forms[:5]:
        print("form:", f[:160])


if __name__ == "__main__":
    main()
