#!/usr/bin/env python3
"""Разбор сохранённой карточки компании checko (company.html): показывает,
где лежат название, ИНН, ОКВЭД (осн. вид деятельности), статус и сайт — для
калибровки парсера карточки.

Запуск:
    .venv\\Scripts\\python scripts\\analyze_card.py company.html
"""
from __future__ import annotations

import re
import sys


def ctx(html, needle, before=120, after=260, n=2):
    out = []
    for m in list(re.finditer(re.escape(needle), html, re.I))[:n]:
        i = m.start()
        frag = re.sub(r"\s+", " ", html[max(0, i - before):i + after])
        out.append(frag)
    return out


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "company.html"
    html = open(path, encoding="utf-8", errors="replace").read()
    print(f"файл: {path}  размер: {len(html)}")

    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    print("title:", (m.group(1).strip()[:160] if m else "—"))

    # ИНН/ОГРН в тексте
    print("ИНН(10/12):", list(dict.fromkeys(re.findall(r"\b(\d{10}|\d{12})\b", html)))[:4])
    print("ОГРН(13):", list(dict.fromkeys(re.findall(r"\b\d{13}\b", html)))[:4])

    # JSON-LD / встроенные данные
    for marker in ("application/ld+json", "__NEXT_DATA__", "itemprop"):
        c = html.count(marker)
        if c:
            print(f"маркер '{marker}': {c}")
    ld = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    if ld:
        print("LD+JSON фрагмент:", re.sub(r"\s+", " ", ld[0])[:400])

    print("\n--- контексты полей ---")
    for label in ("Основной вид", "вид деятельности", "ОКВЭД", "Статус",
                  "Действу", "Ликвидир", "Сайт", "Телефон", "лектронная почта",
                  "E-mail", "Контакты"):
        frags = ctx(html, label)
        if frags:
            print(f"\n[{label}]")
            for fr in frags:
                print("   …", fr[:340], "…")


if __name__ == "__main__":
    main()
