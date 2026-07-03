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
SEARCH_URL = "https://api.checko.ru/v2/search"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# --- Калибруемая схема /v2/search ---------------------------------------
# Имена параметров запроса и пути в ответе вынесены сюда, т.к. в доке checko
# под конкретный ключ они могут отличаться. scripts/probe_checko.py печатает
# реальный JSON — по нему правится ровно этот блок.
SEARCH_BY_OKVED = "okved"          # значение параметра "by" (латиницей!) — поиск по осн. ОКВЭД
SEARCH_OBJ = "org"                 # тип объекта: организации (обязательный параметр)
SEARCH_PARAM = {
    "by": "by",                    # «искать по»
    "obj": "obj",                  # тип объекта (org)
    "query": "query",              # значение (код ОКВЭД)
    "region": "region",            # код региона (None = вся РФ)
    "active": "active",            # фильтр действующих
    "page": "page",                # номер страницы
}
SEARCH_ACTIVE_VALUE = "true"       # значение для active при only_active
# где в ответе лежит список найденных записей (пробуем по очереди):
SEARCH_LIST_KEYS = ("Записи", "data", "records", "items", "Результаты")
# где в записи лежит ИНН / наименование / основной ОКВЭД:
SEARCH_INN_KEYS = ("ИНН", "inn")
SEARCH_NAME_KEYS = ("НаимСокр", "НаимПолн", "name", "Наим")
SEARCH_OKVED_KEYS = ("ОКВЭД", "okved", "КодОКВЭД")

# Поля в ответе /v2/company (тоже калибруются по probe):
COMPANY_NAME_KEYS = ("НаимСокр", "НаимСокрЮЛ", "name")
COMPANY_FULL_KEYS = ("НаимПолн", "НаимПолнЮЛ")
COMPANY_OGRN_KEYS = ("ОГРН", "ogrn")
COMPANY_OKVED_KEYS = ("ОКВЭД", "okved")
COMPANY_REGION_KEYS = ("Регион", "region")
COMPANY_STATUS_KEYS = ("Статус", "status")
OKVED_CODE_KEYS = ("Код", "КодОКВЭД", "code")
OKVED_NAME_KEYS = ("Наим", "НаимОКВЭД", "name")

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

    # --- поиск компаний по ОКВЭД (/v2/search) ---
    def search_page(self, query: str, region: str | None, active: bool, page: int) -> dict:
        """Одна страница выдачи /v2/search. Возвращает разобранный JSON."""
        params = {"key": self.api_key}
        params[SEARCH_PARAM["by"]] = SEARCH_BY_OKVED
        params[SEARCH_PARAM["obj"]] = SEARCH_OBJ
        params[SEARCH_PARAM["query"]] = query
        params[SEARCH_PARAM["page"]] = page
        if region:
            params[SEARCH_PARAM["region"]] = region
        if active:
            params[SEARCH_PARAM["active"]] = SEARCH_ACTIVE_VALUE
        resp = self._get(SEARCH_URL, params=params)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def extract_search_total(payload: dict) -> int:
        """Всего записей по запросу (поле ЗапВсего)."""
        block = payload.get("data", payload) if isinstance(payload, dict) else {}
        if isinstance(block, dict):
            for k in ("ЗапВсего", "total", "Всего", "totalCount"):
                v = block.get(k)
                if isinstance(v, int):
                    return v
                if isinstance(v, str) and v.isdigit():
                    return int(v)
        return 0

    @staticmethod
    def extract_search_records(payload: dict) -> list[dict]:
        """Достаёт список записей из ответа поиска (терпимо к ключам)."""
        node = payload
        if isinstance(payload, dict):
            for k in SEARCH_LIST_KEYS:
                if isinstance(payload.get(k), list):
                    return payload[k]
            data = payload.get("data") or payload.get("Данные")
            if isinstance(data, dict):
                for k in SEARCH_LIST_KEYS:
                    if isinstance(data.get(k), list):
                        return data[k]
            if isinstance(data, list):
                return data
        return node if isinstance(node, list) else []

    def company_data(self, inn: str) -> dict:
        """Полный ответ /v2/company по ИНН (data-блок)."""
        resp = self._get(API_URL, params={"key": self.api_key, "inn": inn})
        resp.raise_for_status()
        payload = resp.json()
        return payload.get("data") or payload.get("Данные") or payload

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


def _first(d, keys):
    if not isinstance(d, dict):
        return ""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return ""


def extract_main_okved(data) -> tuple[str, str]:
    """(код, наименование) основного ОКВЭД из ответа /v2/company."""
    okved = _first(data, COMPANY_OKVED_KEYS)
    if isinstance(okved, dict):
        return str(_first(okved, OKVED_CODE_KEYS)), str(_first(okved, OKVED_NAME_KEYS))
    if isinstance(okved, str):
        return okved, ""
    return "", ""


def fill_company_from_data(company: Company, data: dict) -> Company:
    """Заполняет поля Company из ответа /v2/company (имя, ОКВЭД, регион, контакты)."""
    company.name = str(_first(data, COMPANY_NAME_KEYS) or company.name)
    company.full_name = str(_first(data, COMPANY_FULL_KEYS) or company.full_name)
    company.ogrn = str(_first(data, COMPANY_OGRN_KEYS) or company.ogrn)
    code, name = extract_main_okved(data)
    company.okved_code = code or company.okved_code
    company.okved_name = name or company.okved_name
    region = _first(data, COMPANY_REGION_KEYS)
    company.region = (region if isinstance(region, str) else _first(region, ("Наим", "name"))) or company.region
    status = _first(data, COMPANY_STATUS_KEYS)
    company.status = (status if isinstance(status, str) else _first(status, ("Наим", "name"))) or company.status
    addr = _first(data, ("ЮрАдрес", "Адрес", "АдресЮЛ"))
    if isinstance(addr, dict):
        company.address = str(_first(addr, ("АдресРФ", "Адрес", "НасПункт")) or company.address)
    elif isinstance(addr, str):
        company.address = addr or company.address
    phones, emails, sites = _extract_contacts_from_json(data)
    company.phones = _uniq(_norm_phone(p) for p in phones)
    company.emails = _uniq(emails)
    company.websites = _uniq(sites)
    return company


def _deep_find_inn(node) -> str:
    """Рекурсивно ищет ИНН (10–12 цифр) по ключу ИНН/inn — подстраховка."""
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).lower() in ("инн", "inn") and isinstance(v, (str, int)):
                s = str(v)
                if s.isdigit() and 10 <= len(s) <= 12:
                    return s
        for v in node.values():
            r = _deep_find_inn(v)
            if r:
                return r
    elif isinstance(node, list):
        for item in node:
            r = _deep_find_inn(item)
            if r:
                return r
    return ""


def company_from_search_record(rec: dict) -> Company:
    """Создаёт Company-заготовку из записи /v2/search (нужен прежде всего ИНН)."""
    okved = _first(rec, SEARCH_OKVED_KEYS)
    if isinstance(okved, dict):
        okved_code = str(_first(okved, OKVED_CODE_KEYS))
        okved_name = str(_first(okved, OKVED_NAME_KEYS))
    else:
        okved_code, okved_name = str(okved or ""), ""
    inn = str(_first(rec, SEARCH_INN_KEYS) or "") or _deep_find_inn(rec)
    return Company(
        inn=inn,
        name=str(_first(rec, SEARCH_NAME_KEYS) or ""),
        okved_code=okved_code,
        okved_name=okved_name,
    )


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

    # отсеиваем мусорные «сайты»: ассеты, аналитику, гос-/справочные сервисы
    bad = (
        "googleapis", "gstatic", "yastatic", "mc.yandex", "yandex.ru/clck",
        "google.com/maps", "google-analytics", "doubleclick", "clarity.ms",
        ".png", ".jpg", ".jpeg", ".svg", ".css", ".js", ".ico", ".woff",
        "checko.ru", "fips.ru", "fips_serv", "zakupki.gov", "gov.ru", "nalog.",
        "rusprofile", "list-org", "sbis.ru", "audit-it", "datanewton",
        "arbitr.ru", "kad.arbitr", "fedresurs", "sudrf",
    )
    sites = [s for s in _uniq(sites) if not any(b in s.lower() for b in bad)]
    return phones, emails, sites
