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
# запасные шаблоны: ссылка без слага, ОГРН в JSON (SSR-данные Nuxt/Vue), любой /company/-ОГРН
_OGRN_LINK2_RE = re.compile(r'/company/[^"\'<> ]*?(\d{13})')
_OGRN_JSON_RE = re.compile(r'"(?:ОГРН|ogrn|Ogrn)"\s*:\s*"?(\d{13})"?')


def _extract_ogrn(final_url: str, html: str) -> str:
    """Достаёт 13-значный ОГРН из финального URL (редирект на карточку) или из
    HTML/SSR-данных страницы поиска (ссылка на компанию или JSON)."""
    m = _OGRN_IN_URL_RE.search(final_url or "")
    if m:
        return m.group(1)
    for rx in (_OGRN_LINK_RE, _OGRN_LINK2_RE, _OGRN_JSON_RE):
        m = rx.search(html or "")
        if m:
            return m.group(1)
    return ""
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
            ogrn = _extract_ogrn(getattr(r, "url", "") or "", r.text if r.status_code == 200 else "")
            if ogrn:
                return ogrn
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


_OKVED_SECTION_RE = re.compile(r'<section id="activity".*?</section>', re.S)
_OKVED_ROW_RE = re.compile(r'<td[^>]*>\s*(\d{2}(?:\.\d+)*)\s*</td>\s*<td[^>]*>(.*?)</td>', re.S)
_ACTIVITY_CODE_RE = re.compile(r'id="activity-name"[^>]*>\s*([\d.]+)\s*<')
_ACTIVITY_TEXT_RE = re.compile(r"text_to_cb\('([^']*)',\s*'activity-name'\)")


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def extract_okved_from_card(html: str) -> tuple[str, str, list[str]]:
    """→ (осн_код, осн_наименование, [доп_коды]) из карточки checko.
    Основной помечен «Основной вид деятельности»; иначе — первая строка таблицы."""
    main_code = main_name = ""
    extra: list[str] = []
    sec = _OKVED_SECTION_RE.search(html)
    if sec:
        rows = _OKVED_ROW_RE.findall(sec.group(0))
        for code, name_html in rows:
            if "Основной вид деятельности" in name_html:
                main_code = code
                main_name = _strip_tags(_html.unescape(name_html))
            else:
                extra.append(code)
        if not main_code and rows:            # маркер не найден — первый = основной
            main_code = rows[0][0]
            main_name = _strip_tags(_html.unescape(rows[0][1]))
            extra = [c for c, _ in rows[1:]]
    if not main_code:                         # запасной путь: блок «Вид деятельности»
        m = _ACTIVITY_CODE_RE.search(html)
        if m:
            main_code = m.group(1)
        mn = _ACTIVITY_TEXT_RE.search(html)
        if mn:
            main_name = _html.unescape(mn.group(1))
    return main_code, main_name, extra


def parse_card(html: str, ogrn: str = "", okved_code: str = "") -> Company:
    """Разбирает HTML карточки компании checko в Company."""
    c = Company(ogrn=ogrn)
    # ОКВЭД берём ПРЯМО из карточки (точный код + все дополнительные)
    mc, mn, extra = extract_okved_from_card(html)
    if mc:
        c.okved_code = mc
        c.okved_name = mn or OKVED_NAMES.get(mc, "")
        if extra:
            c.okved_extra = extra
    elif okved_code:                          # карта не дала — используем переданный
        c.okved_code = okved_code
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
