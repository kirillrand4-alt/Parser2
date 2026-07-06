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


def _proxy_dict(proxy: str | None):
    """Строка прокси → dict для Playwright launch(proxy=...). ВНИМАНИЕ: Chromium
    НЕ поддерживает авторизацию для socks5 — тогда логин/пароль игнорируются."""
    if not proxy:
        return None
    import re as _re
    m = _re.match(r"^(?P<scheme>\w+)://(?:(?P<user>[^:@/]+):(?P<pw>[^@/]+)@)?(?P<host>[^/]+)$",
                  proxy.strip())
    if not m:
        return {"server": proxy.strip()}
    scheme = m.group("scheme").lower()
    # Chromium НЕ умеет socks5 с авторизацией — такой прокси к браузеру не
    # применяем (иначе все запросы упадут). Вернём спец-значение.
    if scheme.startswith("socks") and m.group("user"):
        return "UNSUPPORTED_SOCKS_AUTH"
    d = {"server": f"{scheme}://{m.group('host')}"}
    if m.group("user"):
        d["username"] = m.group("user")
        d["password"] = m.group("pw")
    return d


class BrowserSiteClient:
    def __init__(self, cookie: str | None = None, user_data_dir: str | None = None,
                 headless: bool | None = None, delay: float = 1.5, timeout: float = 45000,
                 executable_path: str | None = None, persistent: bool | None = None,
                 proxy: str | None = None, max_delay: float = 30.0):
        self.cookie = cookie or os.environ.get("CHECKO_COOKIE") or None
        self.user_data_dir = user_data_dir or os.environ.get("CHECKO_PROFILE") or DEFAULT_PROFILE
        env_headless = os.environ.get("CHECKO_HEADLESS")
        self.headless = headless if headless is not None else (env_headless != "0")
        # persistent=True — постоянный профиль (для входа scripts/browser_login.py).
        # persistent=False — эфемерный браузер (для дообогащения по кукам): не
        # зависит от возможно повреждённого профиля.
        self.persistent = persistent if persistent is not None else (self.cookie is None)
        self.proxy = proxy or os.environ.get("CHECKO_PROXY") or None
        self.delay = delay
        self.max_delay = max_delay
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
        common = {"locale": "ru-RU", "viewport": {"width": 1366, "height": 768}}
        launch = {"headless": self.headless, "args": args}
        if self.executable_path:
            launch["executable_path"] = self.executable_path
        pd = _proxy_dict(self.proxy)
        if pd == "UNSUPPORTED_SOCKS_AUTH":
            import sys
            print("  [site] ВНИМАНИЕ: браузер НЕ умеет socks5 с логином/паролем — "
                  "прокси к браузеру не применён (запросы идут с IP сервера). "
                  "Для прокси используй HTTP-режим (сними галочку «браузер»).",
                  file=sys.stderr, flush=True)
        elif pd:
            launch["proxy"] = pd
        if self.persistent:
            os.makedirs(self.user_data_dir, exist_ok=True)
            self.ctx = self._pw.chromium.launch_persistent_context(
                self.user_data_dir, **launch, **common)
            self._browser = None
        else:
            # эфемерный браузер — без профиля на диске (устойчиво к порче профиля)
            self._browser = self._pw.chromium.launch(**launch)
            self.ctx = self._browser.new_context(**common)
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
        import sys
        backoff = 4.0
        for attempt in range(5):
            self._throttle()
            resp = self.page.goto(url, timeout=self.timeout, wait_until="domcontentloaded")
            self._last = time.monotonic()
            status = resp.status if resp is not None else 200
            if status == 429:
                # адаптивно замедляемся и ждём, как в HTTP-клиенте
                self.delay = min(self.delay * 1.5, self.max_delay)
                print(f"  [site] 429 (лимит сайта), пауза {backoff:.0f} c; новый интервал "
                      f"{self.delay:.1f} c (попытка {attempt + 1}/5)", file=sys.stderr, flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            return self.page.content()
        # не смогли пробиться сквозь 429 — вернём последнее содержимое (парсер даст пусто)
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
        from .site import search_templates, _extract_ogrn
        for tmpl in search_templates():
            try:
                html = self._content(tmpl.format(q=inn))
            except Exception:  # noqa: BLE001
                continue
            ogrn = _extract_ogrn(self.page.url or "", html)
            if ogrn:
                return ogrn
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
            if getattr(self, "_browser", None):
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
