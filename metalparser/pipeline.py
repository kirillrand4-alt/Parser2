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
    main_okved_only: bool = False      # True — оставлять только тех, у кого код ОСНОВНОЙ
    enrich: bool = True                # для egrul: тянуть ли контакты
    api_key: str | None = None
    cookie: str | None = None          # для source='site': куки авторизации checko
    user_agent: str | None = None      # для source='site': UA как в браузере с куками
    browser: bool = False              # source='site' через настоящий браузер (Playwright)
    prefer_api: bool = True
    delay: float = 1.5
    limit: int = 0                     # 0 = без ограничения
    concurrency: int = 1               # source=api: одновременных запросов (по ключам)
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
        if config.concurrency and config.concurrency > 1:
            yield from _iter_api_parallel(config, on_progress, skip=skip)
        else:
            yield from _iter_api(config, on_progress, skip=skip)
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


def _iter_api_parallel(config: PipelineConfig, on_progress, skip: set | None = None) -> Iterator[Company]:
    """Параллельный сбор через API: до config.concurrency карточек одновременно,
    каждый запрос берёт живой ключ из пула; исчерпавший лимит ключ выбывает."""
    import os
    import sys
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import requests as _requests
    from .checko import (CheckoLimit, KeyPool, CheckoClient, pooled_api_get,
                         build_search_params, _parse_keys, API_URL, SEARCH_URL, DEFAULT_UA)
    extract_search_records = CheckoClient.extract_search_records

    debug = os.environ.get("CHECKO_DEBUG") == "1"
    matcher = _matcher(config)
    pool = KeyPool(_parse_keys(config.api_key))
    conc = min(max(2, config.concurrency), max(1, pool.total()))
    session = _requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Accept-Language": "ru,en;q=0.8"})
    print(f"  [api] параллельно: {conc} одновременных запросов, ключей: {pool.total()}", file=sys.stderr)

    queries = search_codes(matcher.prefixes)
    regions = config.regions or [None]
    skip = skip or set()
    seen: set[str] = set()
    count = 0
    stats = {"fetched": 0, "drop_okved": 0, "drop_inactive": 0, "errors": 0, "search": 0}

    def fetch_card(inn, query):
        c = Company(inn=inn, okved_code=query, enrich_source="api")
        data = pooled_api_get(session, pool, API_URL, {"inn": inn}, delay=config.delay)
        fill_company_from_data(c, data)
        if not c.okved_code:
            c.okved_code = query
        c.enriched = True
        return c

    def keep(c):
        if config.main_okved_only and not matcher.matches(c.okved_code):
            stats["drop_okved"] += 1
            return False
        if config.only_active and c.status and not _is_active_status(c.status):
            stats["drop_inactive"] += 1
            return False
        return True

    def summary():
        print(f"  [api] карточек запрошено: {stats['fetched']}, выдано: {count}, "
              f"отфильтровано по ОКВЭД: {stats['drop_okved']}, по статусу: {stats['drop_inactive']}, "
              f"ошибок: {stats['errors']}, страниц поиска: {stats['search']}", file=sys.stderr)

    try:
        with ThreadPoolExecutor(max_workers=conc) as ex:
            for region in regions:
                for query in queries:
                    # 1) перечисляем ИНН по коду (поиск, через пул, последовательно)
                    inns = []
                    page = 1
                    while page <= MAX_SEARCH_PAGES:
                        try:
                            stats["search"] += 1
                            payload = pooled_api_get(session, pool, SEARCH_URL,
                                                     build_search_params(query, region, config.only_active, page))
                        except CheckoLimit:
                            print(f"  [api] лимит всех ключей исчерпан на поиске (собрано {count}). "
                                  f"Завтра докачает.", file=sys.stderr)
                            return
                        except Exception as exc:  # noqa: BLE001
                            print(f"  [api] {query} стр.{page}: {exc}", file=sys.stderr)
                            break
                        recs = extract_search_records(payload)
                        if not recs:
                            break
                        for rec in recs:
                            stub = company_from_search_record(rec)
                            if stub.inn and stub.inn not in seen and stub.inn not in skip:
                                seen.add(stub.inn)
                                inns.append(stub.inn)
                        page += 1
                    # 2) параллельно тянем карточки
                    futures = {ex.submit(fetch_card, inn, query): inn for inn in inns}
                    for fut in as_completed(futures):
                        stats["fetched"] += 1
                        try:
                            c = fut.result()
                        except CheckoLimit:
                            print(f"  [api] лимит всех ключей исчерпан (собрано {count}). "
                                  f"Завтра докачает.", file=sys.stderr)
                            return
                        except Exception as exc:  # noqa: BLE001
                            stats["errors"] += 1
                            continue
                        if not keep(c):
                            if debug:
                                print(f"  [api DEBUG] {c.inn} осн.ОКВЭД={c.okved_code} → ОТСЕЯН", file=sys.stderr)
                            continue
                        yield c
                        count += 1
                        if on_progress:
                            on_progress(count)
                        if config.limit and count >= config.limit:
                            return
    finally:
        summary()


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


def _iter_api(config: PipelineConfig, on_progress, skip: set | None = None) -> Iterator[Company]:
    import os
    import sys
    from .checko import CheckoLimit
    debug = os.environ.get("CHECKO_DEBUG") == "1"
    matcher = _matcher(config)
    client = CheckoClient(api_key=config.api_key, prefer_api=True, delay=config.delay)
    if len(getattr(client, "keys", []) or []) > 1:
        print(f"  [api] ключей в ротации: {len(client.keys)}", file=sys.stderr)
    # checko /v2/search ищет по точному коду-группе → разворачиваем префиксы
    queries = search_codes(matcher.prefixes)
    regions = config.regions or [None]               # None = вся РФ
    skip = skip or set()
    seen: set[str] = set()
    count = 0
    stats = {"fetched": 0, "drop_okved": 0, "drop_inactive": 0, "errors": 0, "search": 0}

    def summary():
        print(f"  [api] карточек запрошено: {stats['fetched']}, выдано: {count}, "
              f"отфильтровано по ОКВЭД: {stats['drop_okved']}, по статусу: {stats['drop_inactive']}, "
              f"ошибок карточек: {stats['errors']}, страниц поиска: {stats['search']}", file=sys.stderr)

    try:
        for region in regions:
            for query in queries:
                page = 1
                while page <= MAX_SEARCH_PAGES:
                    try:
                        stats["search"] += 1
                        payload = client.search_page(query, region, config.only_active, page)
                    except CheckoLimit as exc:
                        print(f"  [api] {exc} — лимит ВСЕХ ключей на сегодня исчерпан, останавливаюсь "
                              f"(собрано {count}). Завтра после сброса докачает.", file=sys.stderr)
                        return
                    except Exception as exc:  # noqa: BLE001 — 403/пагинация вне тарифа по коду
                        print(f"  [api] {query} стр.{page}: {exc}. Беру доступное по этому коду.",
                              file=sys.stderr)
                        break
                    records = client.extract_search_records(payload)
                    if not records:
                        break
                    for rec in records:
                        stub = company_from_search_record(rec)
                        if not stub.inn or stub.inn in seen or stub.inn in skip:
                            continue
                        seen.add(stub.inn)
                        if not stub.okved_code:      # поиск по точному осн. ОКВЭД → код известен
                            stub.okved_code = query
                        try:
                            stats["fetched"] += 1
                            data = client.company_data(stub.inn)
                            fill_company_from_data(stub, data)
                            stub.enriched = True
                            stub.enrich_source = "api"
                        except CheckoLimit as exc:
                            print(f"  [api] {exc} — лимит ВСЕХ ключей на сегодня исчерпан "
                                  f"(собрано {count}). Завтра докачает.", file=sys.stderr)
                            return
                        except Exception as exc:  # noqa: BLE001
                            stub.enrich_error = f"{type(exc).__name__}: {exc}"
                            stats["errors"] += 1
                        # Пост-фильтр по основному ОКВЭД — ТОЛЬКО если явно включён.
                        # По умолчанию оставляем и тех, у кого код дополнительный
                        # (карточка уже оплачена — запрос не пропадает зря).
                        if config.main_okved_only and not matcher.matches(stub.okved_code):
                            stats["drop_okved"] += 1
                            if debug:
                                print(f"  [api DEBUG] {stub.inn} осн.ОКВЭД={stub.okved_code} "
                                      f"(искали {query}) → ОТСЕЯН: код не основной", file=sys.stderr)
                            continue
                        if config.only_active and stub.status and not _is_active_status(stub.status):
                            stats["drop_inactive"] += 1
                            if debug:
                                print(f"  [api DEBUG] {stub.inn} статус '{stub.status}' → ОТСЕЯН",
                                      file=sys.stderr)
                            continue
                        if debug:
                            print(f"  [api DEBUG] {stub.inn} осн.ОКВЭД={stub.okved_code} → ВЗЯТ",
                                  file=sys.stderr)
                        yield stub
                        count += 1
                        if on_progress:
                            on_progress(count)
                        if config.limit and count >= config.limit:
                            return
                    page += 1
    finally:
        summary()


def count_companies(config: PipelineConfig, delay: float | None = None):
    """Считает число компаний по каждому коду (через ЗапВсего в /v2/search),
    НЕ скачивая сами компании. Возвращает (список (код, кол-во), всего).

    Тратит по 1 лёгкому запросу поиска на код (× число регионов)."""
    import sys
    client = CheckoClient(api_key=config.api_key, prefer_api=True,
                          delay=delay if delay is not None else config.delay)
    print(f"  ключей в ротации: {len(client.keys)}", file=sys.stderr)
    if not client.keys:
        print("  [api] НЕТ КЛЮЧЕЙ — задайте --api-key, env CHECKO_API_KEY или сохраните в вебе.",
              file=sys.stderr)
    codes = search_codes(_matcher(config).prefixes)
    regions = config.regions or [None]
    per: list[tuple[str, int]] = []
    total = 0
    from .checko import CheckoLimit
    for code in codes:
        c = 0
        for region in regions:
            try:
                payload = client.search_page(code, region, config.only_active, 1)
                c += client.extract_search_total(payload)
            except CheckoLimit as exc:
                print(f"  [api] остановка на {code}: {exc}", file=sys.stderr)
                return per, total          # все ключи исчерпаны — отдаём что есть
            except Exception as exc:  # noqa: BLE001 — код недоступен/ошибка по коду
                print(f"  [api] {code}: {exc}", file=sys.stderr)
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
