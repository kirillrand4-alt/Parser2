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

_OGRN_LINK_RE = re.compile(r'href="(?:https?://checko\.ru)?/company/[^"]*?-(\d{13})"')
_INN_TITLE_RE = re.compile(r"ИНН\s*(\d{10}|\d{12})")
_INACTIVE_RE = re.compile(r"ликвидир|прекратил|в стадии ликвид|недейств|исключен", re.I)
_ACTIVE_RE = re.compile(r"действующ", re.I)


def code6(dotted: str) -> str:
    """25.62 -> 256200, 24.10 -> 241000, 25 -> 250000, 28.41 -> 284100."""
    digits = re.sub(r"\D", "", dotted)
    return (digits + "000000")[:6]


class CheckoSiteClient:
    def __init__(self, cookie: str | None = None, delay: float = 2.0,
                 timeout: float = 30.0, max_retries: int = 4, user_agent: str = DEFAULT_UA,
                 max_delay: float = 30.0):
        self.cookie = cookie or os.environ.get("CHECKO_COOKIE") or None
        self.delay = delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "ru,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml",
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
