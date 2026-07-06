"""Сбор через НАСТОЯЩИЙ браузер (Playwright/Chromium).

Надёжнее обычных HTTP-запросов против анти-бот защиты: браузер сам шлёт все
заголовки, исполняет JS и держит вашу залогиненную сессию.

Два способа авторизации:
  1) постоянный профиль — войдите один раз (scripts/browser_login.py), сессия
     сохранится в папке профиля и переиспользуется (headless);
  2) куки — строка Cookie внедряется в контекст (env CHECKO_COOKIE).

Установка на сервере (один раз):
    pip install playwright
    playwright install chromium
"""
from __future__ import annotations

import os
import time

from .models import Company
from .site import CARD_URL, CATALOG_URL, code6, parse_card, _OGRN_LINK_RE

DEFAULT_PROFILE = os.path.join(os.path.dirname(__file__), "..", "data", "browser_profile")
# Стабильная папка для скачанных браузеров Playwright — НЕ зависит от того, под
# каким аккаунтом (SYSTEM/служба/пользователь) запущено приложение. Сюда же
# ставить: PLAYWRIGHT_BROWSERS_PATH=<эта папка> playwright install chromium
DEFAULT_BROWSERS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "pw-browsers"))


class BrowserSiteClient:
    def __init__(self, cookie: str | None = None, user_data_dir: str | None = None,
                 headless: bool | None = None, delay: float = 1.5, timeout: float = 45000,
                 executable_path: str | None = None):
        self.cookie = cookie or os.environ.get("CHECKO_COOKIE") or None
        self.user_data_dir = user_data_dir or os.environ.get("CHECKO_PROFILE") or DEFAULT_PROFILE
        os.makedirs(self.user_data_dir, exist_ok=True)
        env_headless = os.environ.get("CHECKO_HEADLESS")
        self.headless = headless if headless is not None else (env_headless != "0")
        self.delay = delay
        self.timeout = timeout
        self.executable_path = executable_path or os.environ.get("PLAYWRIGHT_CHROME") or None
        self._last = 0.0
        self._start()

    def _start(self):
        # Пусть Playwright ищет браузеры в стабильной папке проекта (если путь
        # не задан явно) — иначе он смотрит в профиль запускающего аккаунта.
        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and os.path.isdir(DEFAULT_BROWSERS_DIR):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = DEFAULT_BROWSERS_DIR
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        args = ["--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"]
        kwargs = {"headless": self.headless, "args": args,
                  "locale": "ru-RU", "viewport": {"width": 1366, "height": 768}}
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
        self.ctx = self._pw.chromium.launch_persistent_context(self.user_data_dir, **kwargs)
        if self.cookie:
            self._inject_cookie()
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def _inject_cookie(self):
        cookies = []
        for part in self.cookie.split(";"):
            part = part.strip()
            if "=" in part:
                name, val = part.split("=", 1)
                cookies.append({"name": name.strip(), "value": val.strip(),
                                "domain": ".checko.ru", "path": "/"})
        if cookies:
            try:
                self.ctx.add_cookies(cookies)
            except Exception:  # noqa: BLE001
                pass

    def _throttle(self):
        el = time.monotonic() - self._last
        if el < self.delay:
            time.sleep(self.delay - el)

    def _content(self, url: str) -> str:
        self._throttle()
        self.page.goto(url, timeout=self.timeout, wait_until="domcontentloaded")
        self._last = time.monotonic()
        return self.page.content()

    def catalog_ogrns(self, dotted_code: str, page: int) -> list[str]:
        html = self._content(f"{CATALOG_URL}?code={code6(dotted_code)}&page={page}")
        seen, out = set(), []
        for ogrn in _OGRN_LINK_RE.findall(html):
            if ogrn not in seen:
                seen.add(ogrn)
                out.append(ogrn)
        return out

    def card(self, ogrn: str, okved_code: str = "") -> Company:
        html = self._content(CARD_URL.format(ident=ogrn))
        return parse_card(html, ogrn=ogrn, okved_code=okved_code)

    def resolve_ogrn(self, inn: str) -> str:
        """ИНН → ОГРН через строку поиска сайта (браузер исполняет JS SPA)."""
        import re as _re
        from .site import search_templates, _OGRN_LINK_RE, _OGRN_IN_URL_RE
        for tmpl in search_templates():
            try:
                html = self._content(tmpl.format(q=inn))
            except Exception:  # noqa: BLE001
                continue
            m = _OGRN_IN_URL_RE.search(self.page.url or "")   # редирект на карточку
            if m:
                return m.group(1)
            m = _OGRN_LINK_RE.search(html)                    # первая ссылка результата
            if m:
                return m.group(1)
        return ""

    def card_by_inn(self, inn: str, okved_code: str = "", ogrn: str = "") -> Company:
        """Карточка по ИНН: находим ОГРН через поиск, затем открываем /company/<ОГРН>."""
        ogrn = (ogrn or "").strip() or self.resolve_ogrn(inn)
        if not ogrn:
            return Company(inn=inn, okved_code=okved_code, enrich_source="site",
                           enrich_error="ОГРН не найден по ИНН (поиск не дал результата)")
        html = self._content(CARD_URL.format(ident=ogrn))
        c = parse_card(html, ogrn=ogrn, okved_code=okved_code)
        if not c.inn:
            c.inn = inn
        if not c.name:
            c.enrich_error = "карточка не найдена/страница без данных"
        return c

    def close(self):
        try:
            self.ctx.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
