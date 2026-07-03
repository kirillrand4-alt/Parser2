#!/usr/bin/env python3
"""Разовый вход в checko через настоящий браузер — сохраняет сессию в профиль.

После этого сбор в режиме браузера идёт headless под вашей сессией, без
копирования кук.

Запуск (нужен рабочий стол/RDP — откроется окно браузера):
    pip install playwright && playwright install chromium
    .venv\\Scripts\\python scripts\\browser_login.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from metalparser.browser import DEFAULT_PROFILE  # noqa: E402


def main():
    profile = os.environ.get("CHECKO_PROFILE") or DEFAULT_PROFILE
    os.makedirs(profile, exist_ok=True)
    exe = os.environ.get("PLAYWRIGHT_CHROME") or None
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        kwargs = {"headless": False, "locale": "ru-RU",
                  "args": ["--disable-blink-features=AutomationControlled"]}
        if exe:
            kwargs["executable_path"] = exe
        ctx = p.chromium.launch_persistent_context(profile, **kwargs)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://checko.ru/login", wait_until="domcontentloaded")
        print(f"\nПрофиль: {profile}")
        print("Войдите в аккаунт checko в открывшемся окне браузера.")
        input("Как войдёте — вернитесь сюда и нажмите Enter для сохранения...\n")
        ctx.close()
    print("Готово. Сессия сохранена — теперь сбор в режиме браузера работает без кук.")


if __name__ == "__main__":
    main()
