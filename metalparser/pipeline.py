"""Оркестрация. Два источника списка компаний:

  * source="egrul" — потоковый парсинг дампа ЕГРЮЛ + дообогащение контактов
                     через checko (API/HTML);
  * source="api"   — перечисление через checko /v2/search по ОКВЭД, затем
                     полные данные и контакты через /v2/company. Дамп не нужен.

В обоих случаях итог — компании с ОСНОВНЫМ ОКВЭД из целевых групп.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator

from .checko import (
    CheckoClient, company_from_search_record, fill_company_from_data,
)
from .egrul import iter_companies
from .models import Company
from .okved import OkvedMatcher, resolve_prefixes, search_codes

MAX_SEARCH_PAGES = 1000  # предохранитель от бесконечной пагинации

# Подстроки статусов, означающих НЕдействующее юрлицо (страховка к active=true)
_INACTIVE_STATUS = ("прекра", "ликвид", "исключ", "недейств", "реорганиз", "банкрот")


def _is_active_status(status: str) -> bool:
    s = (status or "").lower()
    return not any(k in s for k in _INACTIVE_STATUS)


@dataclass
class PipelineConfig:
    source: str = "egrul"              # 'egrul' | 'api' | 'site'
    egrul_path: str = ""
    okved_set: str = "core"
    extra_okved: list[str] = field(default_factory=list)
    only_active: bool = True
    enrich: bool = True                # для egrul: тянуть ли контакты
    api_key: str | None = None
    cookie: str | None = None          # для source='site': куки авторизации checko
    user_agent: str | None = None      # для source='site': UA как в браузере с куками
    browser: bool = False              # source='site' через настоящий браузер (Playwright)
    prefer_api: bool = True
    delay: float = 1.5
    limit: int = 0                     # 0 = без ограничения
    regions: list[str] = field(default_factory=list)  # пусто = вся РФ


def _matcher(config: PipelineConfig) -> OkvedMatcher:
    return OkvedMatcher(resolve_prefixes(config.okved_set, config.extra_okved))


def iter_run(
    config: PipelineConfig,
    on_progress: Callable[[int], None] | None = None,
    skip: set | None = None,
) -> Iterator[Company]:
    """Отдаёт подходящие компании по мере готовности (с контактами).

    skip — уже собранные ИНН/ОГРН (докачка, актуально для source='site')."""
    if config.source == "api":
        yield from _iter_api(config, on_progress)
    elif config.source == "site":
        yield from _iter_site(config, on_progress, skip=skip)
    else:
        yield from _iter_egrul(config, on_progress)


def _iter_site(config: PipelineConfig, on_progress, skip: set | None = None) -> Iterator[Company]:
    """Сбор через сайт checko (HTML): каталог по ОКВЭД -> карточки -> контакты.

    skip — множество уже собранных ИНН/ОГРН (для докачки: пропустить их)."""
    import sys
    matcher = _matcher(config)
    if config.browser:
        from .browser import BrowserSiteClient
        client = BrowserSiteClient(cookie=config.cookie, delay=config.delay)
    else:
        from .site import CheckoSiteClient
        client = CheckoSiteClient(cookie=config.cookie, delay=config.delay,
                                  user_agent=config.user_agent)
    codes = search_codes(matcher.prefixes)     # каталог тоже по коду-группе
    skip = skip or set()
    seen: set[str] = set()
    count = 0
    fails = 0                                   # подряд идущих ошибок (429/блок)
    MAX_FAILS = 15
    try:
        for code in codes:
            page = 1
            while page <= MAX_SEARCH_PAGES:
                try:
                    ogrns = client.catalog_ogrns(code, page)
                except Exception:  # noqa: BLE001 — страница недоступна/блок, к следующему коду
                    break
                fresh = [o for o in ogrns if o not in seen and o not in skip]
                for o in ogrns:
                    seen.add(o)
                for ogrn in fresh:
                    try:
                        comp = client.card(ogrn, okved_code=code)
                        fails = 0
                    except Exception as exc:  # noqa: BLE001
                        fails += 1
                        comp = Company(ogrn=ogrn, okved_code=code,
                                       enrich_source="site", enrich_error=f"{type(exc).__name__}: {exc}")
                        if fails >= MAX_FAILS:
                            print(f"  [site] {fails} ошибок подряд — сайт блокирует запросы, "
                                  f"останавливаюсь (собрано {count}). Повторите позже — докачает остальных.",
                                  file=sys.stderr)
                            yield comp
                            return
                    if config.only_active and comp.status and not _is_active_status(comp.status):
                        continue
                    yield comp
                    count += 1
                    if on_progress:
                        on_progress(count)
                    if config.limit and count >= config.limit:
                        return
                if len(ogrns) < 50:   # похоже, последняя страница по этому коду
                    break
                page += 1
    finally:
        closer = getattr(client, "close", None)
        if callable(closer):
            closer()


def _iter_egrul(config: PipelineConfig, on_progress) -> Iterator[Company]:
    matcher = _matcher(config)
    client = (
        CheckoClient(api_key=config.api_key, prefer_api=config.prefer_api, delay=config.delay)
        if config.enrich else None
    )
    count = 0
    for company in iter_companies(
        config.egrul_path, matcher, only_active=config.only_active,
        progress_every=5000, on_progress=on_progress,
    ):
        if client is not None:
            client.enrich(company)
        yield company
        count += 1
        if config.limit and count >= config.limit:
            break


def _iter_api(config: PipelineConfig, on_progress) -> Iterator[Company]:
    import sys
    matcher = _matcher(config)
    client = CheckoClient(api_key=config.api_key, prefer_api=True, delay=config.delay)
    # checko /v2/search ищет по точному коду-группе → разворачиваем префиксы
    queries = search_codes(matcher.prefixes)
    regions = config.regions or [None]               # None = вся РФ
    seen: set[str] = set()
    count = 0
    for region in regions:
        for query in queries:
            page = 1
            while page <= MAX_SEARCH_PAGES:
                try:
                    payload = client.search_page(query, region, config.only_active, page)
                except Exception as exc:  # noqa: BLE001 — напр. 403 (лимит/пагинация вне тарифа)
                    print(f"  [api] {query} стр.{page}: {exc}. Беру только доступное по этому коду.",
                          file=sys.stderr)
                    break
                # Ошибка API в теле ответа (напр. суточный лимит бесплатного тарифа)
                meta = payload.get("meta") if isinstance(payload, dict) else None
                if isinstance(meta, dict) and str(meta.get("status")).lower() == "error":
                    print(f"  [api] checko: {meta.get('message', 'ошибка')} "
                          f"(запросов сегодня: {meta.get('today_request_count')}). Останавливаюсь.",
                          file=sys.stderr)
                    return
                records = client.extract_search_records(payload)
                if not records:
                    break
                for rec in records:
                    stub = company_from_search_record(rec)
                    if not stub.inn or stub.inn in seen:
                        continue
                    seen.add(stub.inn)
                    # поиск идёт по точному осн. ОКВЭД → код известен заранее
                    if not stub.okved_code:
                        stub.okved_code = query
                    # Полные данные + контакты через /v2/company
                    try:
                        data = client.company_data(stub.inn)
                        fill_company_from_data(stub, data)
                        stub.enriched = True
                        stub.enrich_source = "api"
                    except Exception as exc:  # noqa: BLE001
                        stub.enrich_error = f"{type(exc).__name__}: {exc}"
                    # Пост-фильтр: основной ОКВЭД должен быть из целевых групп
                    if not matcher.matches(stub.okved_code):
                        continue
                    # Пост-фильтр: только действующие (если статус известен)
                    if config.only_active and stub.status and not _is_active_status(stub.status):
                        continue
                    yield stub
                    count += 1
                    if on_progress:
                        on_progress(count)
                    if config.limit and count >= config.limit:
                        return
                page += 1


def count_companies(config: PipelineConfig, delay: float | None = None):
    """Считает число компаний по каждому коду (через ЗапВсего в /v2/search),
    НЕ скачивая сами компании. Возвращает (список (код, кол-во), всего).

    Тратит по 1 лёгкому запросу поиска на код (× число регионов)."""
    client = CheckoClient(api_key=config.api_key, prefer_api=True,
                          delay=delay if delay is not None else config.delay)
    codes = search_codes(_matcher(config).prefixes)
    regions = config.regions or [None]
    per: list[tuple[str, int]] = []
    total = 0
    for code in codes:
        c = 0
        for region in regions:
            try:
                payload = client.search_page(code, region, config.only_active, 1)
                c += client.extract_search_total(payload)
            except Exception:  # noqa: BLE001 — код недоступен на тарифе/ошибка
                pass
        per.append((code, c))
        total += c
    return per, total


def run(
    config: PipelineConfig,
    on_company: Callable[[Company], None] | None = None,
    on_progress: Callable[[int], None] | None = None,
    skip: set | None = None,
) -> list[Company]:
    """Прогоняет пайплайн и возвращает список компаний."""
    results: list[Company] = []
    for company in iter_run(config, on_progress=on_progress, skip=skip):
        results.append(company)
        if on_company:
            on_company(company)
    return results
