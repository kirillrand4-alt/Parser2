"""Дообогащение компаний контактами через checko.ru.

Два режима:
  * API  — https://api.checko.ru/v2/company (нужен ключ, env CHECKO_API_KEY).
           Возвращает стабильный JSON, рекомендуется.
  * HTML — разбор публичной страницы /company/...  (best-effort: селекторы
           checko могут меняться, плюс домен жёстко режет частые запросы 429).

Контактные поля checko считает премиальными, поэтому в HTML они могут быть
скрыты за подпиской — API надёжнее.
"""
from __future__ import annotations

import os
import re
import time

import requests

from .models import Company

API_URL = "https://api.checko.ru/v2/company"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+7|8|7)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-()]*\d{2}[\s\-()]*\d{2}")
_TEL_HREF_RE = re.compile(r'href=["\']tel:([^"\']+)["\']', re.I)
_MAILTO_RE = re.compile(r'href=["\']mailto:([^"\'?]+)', re.I)
_SITE_RE = re.compile(
    r'\bhref=["\'](https?://(?!checko\.ru)[^"\']+)["\']', re.I
)


def _uniq(seq) -> list[str]:
    seen, out = set(), []
    for x in seq:
        x = (x or "").strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _norm_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
        return f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return raw.strip()


class CheckoClient:
    def __init__(
        self,
        api_key: str | None = None,
        prefer_api: bool = True,
        delay: float = 1.5,
        timeout: float = 20.0,
        max_retries: int = 4,
        user_agent: str = DEFAULT_UA,
    ):
        self.api_key = api_key or os.environ.get("CHECKO_API_KEY") or None
        self.prefer_api = prefer_api
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Language": "ru,en;q=0.8"})
        self._last_request = 0.0

    # --- сетевой слой с троттлингом и backoff на 429 ---
    def _throttle(self):
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        backoff = 2.0
        last_exc = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                self._last_request = time.monotonic()
                if resp.status_code == 429:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                return resp
            except requests.RequestException as exc:  # сетевые сбои
                last_exc = exc
                self._last_request = time.monotonic()
                time.sleep(backoff)
                backoff *= 2
        if last_exc:
            raise last_exc
        raise requests.RequestException(f"429 после {self.max_retries} попыток: {url}")

    # --- публичный метод ---
    def enrich(self, company: Company) -> Company:
        use_api = self.prefer_api and bool(self.api_key)
        try:
            if use_api:
                self._enrich_api(company)
                company.enrich_source = "api"
            else:
                self._enrich_html(company)
                company.enrich_source = "html"
            company.enriched = True
        except Exception as exc:  # noqa: BLE001 — фиксируем, не валим весь прогон
            company.enrich_error = f"{type(exc).__name__}: {exc}"
        return company

    # --- API ---
    def _enrich_api(self, company: Company):
        key = company.inn or company.ogrn
        if not key:
            raise ValueError("нет ИНН/ОГРН для запроса")
        resp = self._get(API_URL, params={"key": self.api_key, "inn": company.inn or None,
                                          "ogrn": None if company.inn else company.ogrn})
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") or payload.get("Данные") or payload
        phones, emails, sites = _extract_contacts_from_json(data)
        company.phones = _uniq(_norm_phone(p) for p in phones)
        company.emails = _uniq(emails)
        company.websites = _uniq(sites)

    # --- HTML ---
    def company_url(self, company: Company) -> str:
        # checko принимает ОГРН/ИНН в пути и редиректит на канонический slug
        ident = company.ogrn or company.inn
        return f"https://checko.ru/company/{ident}"

    def _enrich_html(self, company: Company):
        resp = self._get(self.company_url(company))
        if resp.status_code == 404 and company.inn and company.ogrn:
            resp = self._get(f"https://checko.ru/company/{company.inn}")
        resp.raise_for_status()
        phones, emails, sites = extract_contacts_from_html(resp.text)
        company.phones = _uniq(_norm_phone(p) for p in phones)
        company.emails = _uniq(emails)
        company.websites = _uniq(sites)


def _extract_contacts_from_json(data) -> tuple[list[str], list[str], list[str]]:
    """Толерантно вытаскивает контакты из JSON ответа API.

    Структура ответа зависит от тарифа; ищем блок 'Контакты' и типовые ключи,
    а также подстраховываемся регуляркой по строковым значениям этого блока."""
    phones: list[str] = []
    emails: list[str] = []
    sites: list[str] = []

    contacts = None
    if isinstance(data, dict):
        for k in ("Контакты", "Контакт", "contacts", "Contacts"):
            if k in data:
                contacts = data[k]
                break
    block = contacts if contacts is not None else data

    def walk(node, bucket_hint=None):
        if isinstance(node, dict):
            for key, val in node.items():
                kl = str(key).lower()
                if any(t in kl for t in ("тел", "phone")):
                    _collect(val, phones)
                elif any(t in kl for t in ("мэйл", "мейл", "почт", "email", "e-mail")):
                    _collect(val, emails)
                elif any(t in kl for t in ("сайт", "site", "web", "url")):
                    _collect(val, sites)
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    def _collect(val, bucket):
        if isinstance(val, str):
            bucket.append(val)
        elif isinstance(val, (list, tuple)):
            for v in val:
                if isinstance(v, str):
                    bucket.append(v)
                elif isinstance(v, dict):
                    # элементы вида {"Значение": "..."} / {"value": "..."}
                    for vk in ("Значение", "value", "Тел", "Емэйл", "Сайт"):
                        if vk in v and isinstance(v[vk], str):
                            bucket.append(v[vk])
        elif isinstance(val, dict):
            walk(val)

    walk(block)

    # подстраховка: регулярки по сериализованному блоку контактов
    import json as _json
    blob = _json.dumps(block, ensure_ascii=False) if block is not None else ""
    emails += _EMAIL_RE.findall(blob)
    return phones, emails, sites


def extract_contacts_from_html(html: str) -> tuple[list[str], list[str], list[str]]:
    """Достаёт контакты из HTML страницы компании checko.

    Best-effort: tel:/mailto: ссылки, внешние ссылки (сайты), плюс регулярки
    по тексту. Селекторы могут потребовать калибровки на реальной странице.
    """
    phones = list(_TEL_HREF_RE.findall(html))
    emails = list(_MAILTO_RE.findall(html))
    sites = list(_SITE_RE.findall(html))

    # текстовый fallback (на случай, если контакты не в ссылках)
    text = re.sub(r"<[^>]+>", " ", html)
    phones += _PHONE_RE.findall(text)
    emails += _EMAIL_RE.findall(text)

    # отсеиваем мусорные «сайты» (соцсети checko, картинки, ассеты)
    bad = ("googleapis", "gstatic", "yandex", "google.com/maps", ".png", ".jpg",
           ".svg", ".css", ".js", "checko.ru")
    sites = [s for s in sites if not any(b in s.lower() for b in bad)]
    return phones, emails, sites
