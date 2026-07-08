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
import threading
import time

import requests

from .models import Company

API_URL = "https://api.checko.ru/v2/company"
SEARCH_URL = "https://api.checko.ru/v2/search"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
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
SEARCH_OGRN_KEYS = ("ОГРН", "ogrn", "ОГРНИП")
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


class CheckoLimit(Exception):
    """Исчерпан лимит всех ключей (суточный лимит/недоступно на тарифе)."""


def read_keys_file(path: str) -> str:
    """Читает ключи из txt-файла (по ключу в строке, '#' — комментарий).
    Возвращает строку ключей через запятую (пусто, если файла нет)."""
    try:
        toks = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0]
                toks += re.split(r"[,\s;]+", line)
        return ",".join(t.strip() for t in toks if t.strip())
    except Exception:  # noqa: BLE001
        return ""


def _parse_keys(api_key) -> list[str]:
    """Список ключей из строки (через запятую/пробел/перенос) или списка."""
    if isinstance(api_key, (list, tuple)):
        raw = list(api_key)
    else:
        raw = re.split(r"[,\s;]+", api_key or "")
    seen, out = set(), []
    for k in raw:
        k = (k or "").strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _is_limit_meta(payload) -> bool:
    """True, если тело ответа — ошибка лимита/доступа (суточный лимит и т.п.)."""
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if not isinstance(meta, dict) or str(meta.get("status")).lower() != "error":
        return False
    msg = str(meta.get("message", "")).lower()
    return any(w in msg for w in ("лимит", "тариф", "limit", "превыш", "доступ", "forbidden"))


def _is_invalid_key(payload) -> bool:
    """True, если ключ НЕДЕЙСТВИТЕЛЕН (битый навсегда), а не просто исчерпан лимит."""
    meta = payload.get("meta") if isinstance(payload, dict) else None
    msg = str((meta or {}).get("message", "")).lower() if isinstance(meta, dict) else ""
    return any(w in msg for w in ("не действ", "недейств", "неверн", "invalid", "not valid",
                                  "не найден ключ", "unknown key"))


def prune_keys_file(path: str, invalid_keys) -> int:
    """Удаляет НЕДЕЙСТВИТЕЛЬНЫЕ ключи из файла ключей, перенося их в
    <path рядом>/dead_keys.txt. Возвращает, сколько удалено."""
    invalid = {str(k).strip() for k in invalid_keys if str(k).strip()}
    if not invalid or not os.path.exists(path):
        return 0
    kept, removed = [], []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            raw = line.rstrip("\n")
            tok = raw.split("#", 1)[0].strip()
            if tok and tok in invalid:
                removed.append(tok)
            else:
                kept.append(raw)
    if not removed:
        return 0
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + ("\n" if kept else ""))
    os.replace(tmp, path)
    dead = os.path.join(os.path.dirname(path) or ".", "dead_keys.txt")
    with open(dead, "a", encoding="utf-8") as fh:
        for k in removed:
            fh.write(k + "\n")
    return len(removed)


class KeyPool:
    """Потокобезопасный пул ключей: раздаёт по кругу, исключённые (исчерпавшие
    лимит) больше не выдаёт до конца прогона."""

    def __init__(self, keys):
        self._keys = list(keys)
        self._dead: set[str] = set()
        self._invalid: set[str] = set()    # ключи «не действителен» (битые навсегда)
        self._lock = threading.Lock()
        self._rr = 0

    def total(self) -> int:
        return len(self._keys)

    def keys_snapshot(self) -> list:
        return list(self._keys)

    def alive(self) -> int:
        with self._lock:
            return len([k for k in self._keys if k not in self._dead])

    def acquire(self) -> str | None:
        with self._lock:
            alive = [k for k in self._keys if k not in self._dead]
            if not alive:
                return None
            self._rr = (self._rr + 1) % len(alive)
            return alive[self._rr]

    def mark_dead(self, key: str, invalid: bool = False):
        with self._lock:
            self._dead.add(key)
            if invalid:                    # битый навсегда — на удаление из файла
                self._invalid.add(key)

    def invalid_snapshot(self) -> list:
        with self._lock:
            return list(self._invalid)


def build_search_params(query, region, active, page) -> dict:
    params = {SEARCH_PARAM["by"]: SEARCH_BY_OKVED, SEARCH_PARAM["obj"]: SEARCH_OBJ,
              SEARCH_PARAM["query"]: query, SEARCH_PARAM["page"]: page}
    if region:
        params[SEARCH_PARAM["region"]] = region
    if active:
        params[SEARCH_PARAM["active"]] = SEARCH_ACTIVE_VALUE
    return params


def pooled_api_get(session, pool: "KeyPool", url: str, params: dict,
                   timeout: float = 25.0, delay: float = 0.0) -> dict:
    """Запрос к API через пул ключей (для параллельного режима). При лимите/401/403
    ключ помечается мёртвым и берётся следующий; если живых нет — CheckoLimit."""
    import sys
    rate_retries = 0
    while True:
        key = pool.acquire()
        if key is None:
            raise CheckoLimit("все ключи исчерпали лимит")
        p = dict(params)
        p["key"] = key
        try:
            resp = session.get(url, params=p, timeout=timeout)
        except requests.RequestException as exc:
            rate_retries += 1
            if rate_retries > 6:
                raise
            print(f"  [api] сетевая ошибка ({type(exc).__name__}), повтор {rate_retries}/6", file=sys.stderr)
            time.sleep(1.0)
            continue
        if resp.status_code == 429:
            rate_retries += 1
            if rate_retries > 8:
                raise requests.RequestException("429 не проходит после 8 попыток")
            print(f"  [api] 429 (частота), пауза 1.5с ({rate_retries}/8)", file=sys.stderr)
            time.sleep(1.5)
            continue
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if (payload is not None and _is_limit_meta(payload)) or resp.status_code in (401, 403):
            msg = (((payload or {}).get("meta") or {}).get("message") or f"HTTP {resp.status_code}")
            # различаем: битый ключ (не действителен) vs исчерпан лимит
            invalid = _is_invalid_key(payload) or (
                resp.status_code == 401 and not _is_limit_meta(payload))
            pool.mark_dead(key, invalid=invalid)
            tag = "НЕ ДЕЙСТВИТЕЛЕН (удалю из файла)" if invalid else "исчерпан"
            print(f"  [api] ключ {tag} ({msg}); живых ключей осталось: {pool.alive()}",
                  file=sys.stderr)
            continue
        resp.raise_for_status()
        if payload is None:
            raise requests.RequestException(f"не-JSON ответ от {url}")
        if delay:
            time.sleep(delay)
        return payload


class CheckoClient:
    def __init__(
        self,
        api_key: str | None = None,
        prefer_api: bool = True,
        delay: float = 1.5,
        timeout: float = 20.0,
        max_retries: int = 4,
        user_agent: str = DEFAULT_UA,
        proxy: str | None = None,
    ):
        self.keys = _parse_keys(api_key or os.environ.get("CHECKO_API_KEY"))
        self.ki = 0                       # индекс текущего ключа (ротация)
        self.prefer_api = prefer_api
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Language": "ru,en;q=0.8"})
        proxy = proxy or os.environ.get("CHECKO_PROXY")
        if proxy:
            self.session.proxies.update({"http": proxy.strip(), "https": proxy.strip()})
        self._last_request = 0.0

    # --- ротация ключей ---
    @property
    def api_key(self) -> str | None:
        return self.keys[self.ki] if self.ki < len(self.keys) else None

    def advance_key(self) -> bool:
        """Переключиться на следующий ключ. True — есть ещё ключ."""
        self.ki += 1
        return self.ki < len(self.keys)

    def _api_get(self, url: str, params: dict) -> dict:
        """GET к API с подстановкой ключа и ротацией при исчерпании лимита/401/403."""
        import sys
        debug = os.environ.get("CHECKO_DEBUG") == "1"
        attempts = max(1, len(self.keys)) + 1
        for _ in range(attempts):
            params = dict(params)
            params["key"] = self.api_key
            resp = self._get(url, params=params)
            try:
                payload = resp.json()
            except ValueError:
                payload = None
            meta = (payload or {}).get("meta") if isinstance(payload, dict) else None
            if debug:
                data = (payload or {}).get("data") if isinstance(payload, dict) else None
                okv = data.get("ОКВЭД") if isinstance(data, dict) else None
                if okv is not None:
                    print(f"  [api DEBUG] ключ #{self.ki + 1} HTTP {resp.status_code} | "
                          f"ОКВЭД={okv}", file=sys.stderr)
                else:
                    import json as _json
                    body = _json.dumps(payload, ensure_ascii=False)[:300] if payload is not None else resp.text[:300]
                    print(f"  [api DEBUG] ключ #{self.ki + 1} HTTP {resp.status_code} → {body}", file=sys.stderr)
            # ротация ТОЛЬКО при реальном отказе: лимит в теле или 401/403
            is_reject = (payload is not None and _is_limit_meta(payload)) or resp.status_code in (401, 403)
            if is_reject:
                srv = ""
                if isinstance(meta, dict):
                    srv = str(meta.get("message", "")).strip()
                    trc = meta.get("today_request_count")
                    if trc is not None:
                        srv += f"; запросов у ключа: {trc}"
                reason = srv or f"HTTP {resp.status_code}"
                failed = self.ki + 1
                if self.advance_key():
                    print(f"  [api] ключ #{failed}: ОТВЕТ СЕРВЕРА → {reason}; перехожу "
                          f"к #{self.ki + 1}/{len(self.keys)}", file=sys.stderr)
                    continue
                raise CheckoLimit(reason)
            # иная ошибка в теле (не лимит) — покажем и не будем молча глотать
            if isinstance(meta, dict) and str(meta.get("status")).lower() == "error":
                print(f"  [api] ОТВЕТ СЕРВЕРА (ошибка, не лимит) → {meta.get('message')}", file=sys.stderr)
            resp.raise_for_status()
            if payload is None:
                raise requests.RequestException(f"не-JSON ответ от {url}")
            return payload
        raise CheckoLimit("все ключи исчерпаны/недействительны")

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
        """Одна страница выдачи /v2/search. Возвращает разобранный JSON (с ротацией ключей)."""
        params = {SEARCH_PARAM["by"]: SEARCH_BY_OKVED, SEARCH_PARAM["obj"]: SEARCH_OBJ,
                  SEARCH_PARAM["query"]: query, SEARCH_PARAM["page"]: page}
        if region:
            params[SEARCH_PARAM["region"]] = region
        if active:
            params[SEARCH_PARAM["active"]] = SEARCH_ACTIVE_VALUE
        return self._api_get(SEARCH_URL, params)

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
        """Полный ответ /v2/company по ИНН (data-блок), с ротацией ключей."""
        payload = self._api_get(API_URL, {"inn": inn})
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
        params = {"inn": company.inn} if company.inn else {"ogrn": company.ogrn}
        payload = self._api_get(API_URL, params)
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


def extract_extra_okved(data) -> list[str]:
    """Коды дополнительных ОКВЭД из ответа /v2/company (поле ОКВЭДДоп)."""
    extra = _first(data, ("ОКВЭДДоп", "ОКВЭДдоп", "okvedDop"))
    out = []
    if isinstance(extra, list):
        for it in extra:
            if isinstance(it, dict):
                c = str(_first(it, OKVED_CODE_KEYS) or "").strip()
                if c:
                    out.append(c)
            elif isinstance(it, str) and it.strip():
                out.append(it.strip())
    return out


def fill_company_from_data(company: Company, data: dict) -> Company:
    """Заполняет поля Company из ответа /v2/company (имя, ОКВЭД, регион, контакты)."""
    company.name = str(_first(data, COMPANY_NAME_KEYS) or company.name)
    company.full_name = str(_first(data, COMPANY_FULL_KEYS) or company.full_name)
    company.ogrn = str(_first(data, COMPANY_OGRN_KEYS) or company.ogrn)
    code, name = extract_main_okved(data)
    company.okved_code = code or company.okved_code
    company.okved_name = name or company.okved_name
    company.okved_extra = extract_extra_okved(data) or company.okved_extra
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
        # В /v2/search ОКВЭД иногда приходит одной строкой — это может быть
        # как код (25.62), так и НАИМЕНОВАНИЕ. Код кладём в okved_code,
        # текст — в okved_name, чтобы не смешивать их в одной колонке.
        s = str(okved or "").strip()
        if re.match(r"^\d{2}(\.\d+)*$", s):
            okved_code, okved_name = s, ""
        else:
            okved_code, okved_name = "", s
    inn = str(_first(rec, SEARCH_INN_KEYS) or "") or _deep_find_inn(rec)
    ogrn = str(_first(rec, SEARCH_OGRN_KEYS) or "")
    return Company(
        inn=inn,
        ogrn=ogrn,
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

    # отсеиваем мусорные «сайты»: ассеты, аналитику, карты, магазины расширений,
    # рекламные сети, соцсети-заглушки, гос-/справочные сервисы
    bad = (
        "googleapis", "gstatic", "yastatic", "mc.yandex", "yandex.ru/clck",
        "an.yandex", "yandex.ru/maps", "yandex.ru/profile", "maps.yandex",
        "google.com/maps", "google.com/webstore", "chrome.google.com",
        "play.google.com", "apps.apple.com", "webstore", "market.yandex",
        "google-analytics", "doubleclick", "clarity.ms", "googletagmanager",
        "2gis.ru", "yell.ru", "flamp.ru", "zoon.ru", "orgpage",
        ".png", ".jpg", ".jpeg", ".svg", ".css", ".js", ".ico", ".woff", ".webp",
        "checko.ru", "fips.ru", "fips_serv", "zakupki.gov", "gov.ru", "nalog.",
        "rusprofile", "list-org", "sbis.ru", "audit-it", "datanewton",
        "arbitr.ru", "kad.arbitr", "fedresurs", "sudrf", "rostrud", "consultant.ru",
    )
    sites = [s for s in _uniq(sites) if not any(b in s.lower() for b in bad)]

    # мусорные почты: одноразовые/трекинговые домены (повторяются у всех карточек)
    bad_mail = ("trashlify", "example.com", "sentry", "wixpress", "domain.com",
                "email.com", "noreply", "no-reply", ".png", ".jpg")
    emails = [e for e in _uniq(emails) if not any(b in e.lower() for b in bad_mail)]
    return phones, emails, sites
