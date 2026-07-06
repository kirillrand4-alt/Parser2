"""Сбор через САЙТ checko (HTML, без API и его лимита 100/день).

Каталог: https://checko.ru/company/select?code=<6 цифр>&page=<n>
  код ОКВЭД кодируется 6 знаками без точек: 25.62 -> 256200, 24.10 -> 241000.
  На странице ~50 компаний (ссылки /company/<slug>-<ОГРН>).
Карточка: https://checko.ru/company/<ОГРН> — имя, ИНН, контакты.

Для полноты (открытые контакты, полный каталог) нужен авторизованный аккаунт:
куки передаются строкой Cookie (env CHECKO_COOKIE или поле в форме).
"""
from __future__ import annotations

import html as _html
import os
import re
import time

import requests

from .checko import extract_contacts_from_html, _norm_phone, _uniq, DEFAULT_UA
from .models import Company
from .okved import OKVED_NAMES

CATALOG_URL = "https://checko.ru/company/select"
CARD_URL = "https://checko.ru/company/{ident}"
# Поиск по ИНН → страница компании (checko редиректит на /company/<slug>-<ОГРН>).
# Точный адрес поиска можно переопределить через env CHECKO_SEARCH_URL
# (шаблон с {q}), если у checko он отличается.
_DEFAULT_SEARCH_URLS = ("https://checko.ru/search?query={q}",
                        "https://checko.ru/company/search?query={q}")


def search_templates() -> tuple[str, ...]:
    env = os.environ.get("CHECKO_SEARCH_URL")
    if env:
        if "{q}" not in env:
            env = env.rstrip("&?") + ("&" if "?" in env else "?") + "query={q}"
        return (env,) + _DEFAULT_SEARCH_URLS
    return _DEFAULT_SEARCH_URLS


SEARCH_URLS = _DEFAULT_SEARCH_URLS   # обратная совместимость

_OGRN_LINK_RE = re.compile(r'href="(?:https?://checko\.ru)?/company/[^"]*?-(\d{13})"')
_OGRN_IN_URL_RE = re.compile(r"-(\d{13})(?:[/?#]|$)")
_INN_TITLE_RE = re.compile(r"ИНН\s*(\d{10}|\d{12})")
_INACTIVE_RE = re.compile(r"ликвидир|прекратил|в стадии ликвид|недейств|исключен", re.I)
_ACTIVE_RE = re.compile(r"действующ", re.I)


def code6(dotted: str) -> str:
    """25.62 -> 256200, 24.10 -> 241000, 25 -> 250000, 28.41 -> 284100."""
    digits = re.sub(r"\D", "", dotted)
    return (digits + "000000")[:6]


class CheckoSiteClient:
    def __init__(self, cookie: str | None = None, delay: float = 2.0,
                 timeout: float = 30.0, max_retries: int = 4, user_agent: str | None = None,
                 max_delay: float = 30.0):
        self.cookie = cookie or os.environ.get("CHECKO_COOKIE") or None
        self.delay = delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.max_retries = max_retries
        ua = user_agent or os.environ.get("CHECKO_UA") or DEFAULT_UA
        self.session = requests.Session()
        # Полный «браузерный» набор заголовков — чтобы запросы не выглядели ботом.
        self.session.headers.update({
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                      "image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Sec-Ch-Ua": '"Chromium";v="120", "Google Chrome";v="120", "Not?A_Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Referer": "https://checko.ru/",
            "Connection": "keep-alive",
        })
        if self.cookie:
            self.session.headers["Cookie"] = self.cookie.strip()
        self._last = 0.0

    def _throttle(self):
        el = time.monotonic() - self._last
        if el < self.delay:
            time.sleep(self.delay - el)

    def _get(self, url, params=None) -> requests.Response:
        import sys
        backoff = 3.0
        last_exc = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                self._last = time.monotonic()
                if r.status_code == 429:
                    # адаптивно замедляемся, чтобы выйти на устойчивый темп
                    self.delay = min(self.delay * 1.5, self.max_delay)
                    print(f"  [site] 429 (лимит), пауза {backoff:.0f} c; новый интервал "
                          f"{self.delay:.1f} c (попытка {attempt + 1}/{self.max_retries})", file=sys.stderr)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                return r
            except requests.RequestException as exc:
                last_exc = exc
                self._last = time.monotonic()
                print(f"  [site] сетевая ошибка ({type(exc).__name__}), повтор через {backoff:.0f} c",
                      file=sys.stderr)
                time.sleep(backoff)
                backoff *= 2
        if last_exc:
            raise last_exc
        raise requests.RequestException(f"429 после {self.max_retries} попыток: {url}")

    # --- каталог ---
    def catalog_ogrns(self, dotted_code: str, page: int) -> list[str]:
        """Список ОГРН со страницы каталога по коду ОКВЭД."""
        r = self._get(CATALOG_URL, params={"code": code6(dotted_code), "page": page})
        r.raise_for_status()
        seen, out = set(), []
        for ogrn in _OGRN_LINK_RE.findall(r.text):
            if ogrn not in seen:
                seen.add(ogrn)
                out.append(ogrn)
        return out

    # --- карточка ---
    def card(self, ogrn: str, okved_code: str = "") -> Company:
        r = self._get(CARD_URL.format(ident=ogrn))
        r.raise_for_status()
        return parse_card(r.text, ogrn=ogrn, okved_code=okved_code)

    def resolve_ogrn(self, inn: str) -> str:
        """ИНН → ОГРН через поиск на сайте (страница /company/<ИНН> даёт 404).
        Возвращает 13-значный ОГРН или '' если не нашли."""
        for tmpl in search_templates():
            try:
                r = self._get(tmpl.format(q=inn))
            except Exception:  # noqa: BLE001
                continue
            # checko часто редиректит прямо на карточку → ОГРН в финальном URL
            m = _OGRN_IN_URL_RE.search(getattr(r, "url", "") or "")
            if m:
                return m.group(1)
            if r.status_code == 200:
                m = _OGRN_LINK_RE.search(r.text)   # первая ссылка на компанию
                if m:
                    return m.group(1)
        return ""

    def card_by_inn(self, inn: str, okved_code: str = "", ogrn: str = "") -> Company:
        """Карточка по ИНН. Сначала находим ОГРН (по ИНН прямой URL даёт 404),
        затем открываем /company/<ОГРН>. Если ОГРН уже известен — сразу по нему."""
        ogrn = (ogrn or "").strip() or self.resolve_ogrn(inn)
        if not ogrn:
            return Company(inn=inn, okved_code=okved_code, enrich_source="site",
                           enrich_error="ОГРН не найден по ИНН (поиск не дал результата)")
        r = self._get(CARD_URL.format(ident=ogrn))
        r.raise_for_status()
        c = parse_card(r.text, ogrn=ogrn, okved_code=okved_code)
        if not c.inn:
            c.inn = inn
        if not c.name:
            c.enrich_error = "карточка не найдена/страница без данных"
        return c


def parse_card(html: str, ogrn: str = "", okved_code: str = "") -> Company:
    """Разбирает HTML карточки компании checko в Company."""
    c = Company(ogrn=ogrn, okved_code=okved_code)
    if okved_code:
        c.okved_name = OKVED_NAMES.get(okved_code, "")

    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if m:
        title = _html.unescape(m.group(1)).strip()
        segs = [s.strip() for s in re.split(r"\s[–—-]\s", title)]
        if segs:
            c.name = segs[0]
        # ИНН из заголовка
        mi = _INN_TITLE_RE.search(title)
        if mi:
            c.inn = mi.group(1)
        # город/регион — второй сегмент, если он не «ИНН …»
        if len(segs) > 1 and not segs[1].startswith("ИНН"):
            c.region = segs[1]

    # статус (действующая/ликвидирована)
    if _INACTIVE_RE.search(html):
        c.status = "Недействующая"
    elif _ACTIVE_RE.search(html):
        c.status = "Действующая"

    phones, emails, sites = extract_contacts_from_html(html)
    c.phones = _uniq(_norm_phone(p) for p in phones)
    c.emails = _uniq(emails)
    c.websites = _uniq(sites)
    c.enriched = True
    c.enrich_source = "site"
    return c
