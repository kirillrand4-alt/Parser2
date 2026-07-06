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
from .okved import OkvedMatcher, resolve_prefixes, search_codes, OKVED_NAMES


def _okved_name(code: str) -> str:
    return OKVED_NAMES.get(code, OKVED_NAMES.get(code[:5], "")) if code else ""

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
    enrich_contacts: bool = True       # source=api: тянуть ли контакты (/v2/company). False = только список
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
    proxy: str | None = None           # прокси для запросов, напр. http://user:pass@host:port
    key_check: bool = True             # предварительная проверка ключей перед сбором


def _matcher(config: PipelineConfig) -> OkvedMatcher:
    return OkvedMatcher(resolve_prefixes(config.okved_set, config.extra_okved))


def _proxies(proxy: str | None) -> dict | None:
    """dict для requests.Session().proxies из одной строки прокси (http/https/socks5)."""
    if not proxy:
        return None
    p = proxy.strip()
    return {"http": p, "https": p}


def iter_run(
    config: PipelineConfig,
    on_progress: Callable[[int], None] | None = None,
    skip: set | None = None,
    done_codes: set | None = None,
    on_code_done: Callable[[str], None] | None = None,
) -> Iterator[Company]:
    """Отдаёт подходящие компании по мере готовности (с контактами).

    skip — уже собранные ИНН/ОГРН (докачка, актуально для source='site').
    done_codes — коды ОКВЭД, уже полностью пройденные в прошлых прогонах:
        их поиск пропускаем целиком (не пролистываем заново).
    on_code_done(code) — колбэк, когда код пройден до конца (для чекпоинта)."""
    if config.source == "api":
        if config.concurrency and config.concurrency > 1:
            yield from _iter_api_parallel(config, on_progress, skip=skip,
                                          done_codes=done_codes, on_code_done=on_code_done)
        else:
            yield from _iter_api(config, on_progress, skip=skip,
                                 done_codes=done_codes, on_code_done=on_code_done)
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


def _iter_api_parallel(config: PipelineConfig, on_progress, skip: set | None = None,
                       done_codes: set | None = None, on_code_done=None) -> Iterator[Company]:
    """Параллельный сбор через API: до config.concurrency карточек одновременно,
    каждый запрос берёт живой ключ из пула; исчерпавший лимит ключ выбывает."""
    import os
    import sys
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import requests as _requests
    from .checko import (CheckoLimit, KeyPool, CheckoClient, pooled_api_get, _is_limit_meta,
                         build_search_params, _parse_keys, API_URL, SEARCH_URL, DEFAULT_UA)
    extract_search_records = CheckoClient.extract_search_records

    debug = os.environ.get("CHECKO_DEBUG") == "1"
    matcher = _matcher(config)
    pool = KeyPool(_parse_keys(config.api_key))
    conc = min(max(2, config.concurrency), max(1, pool.total()))
    session = _requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Accept-Language": "ru,en;q=0.8"})
    if _proxies(config.proxy):
        session.proxies.update(_proxies(config.proxy))
        print(f"  [api] через прокси: {config.proxy}", file=sys.stderr, flush=True)
    print(f"  [api] параллельно: {conc} одновременных запросов, ключей: {pool.total()}",
          file=sys.stderr, flush=True)

    queries = search_codes(matcher.prefixes)
    regions = config.regions or [None]

    if not config.key_check:
        print(f"  [api] без предварительной проверки ключей — нерабочие отсеются "
              f"по ходу сбора (ключей: {pool.total()})", file=sys.stderr, flush=True)
    # Быстрая параллельная проверка ключей — отсеять мёртвые/невалидные заранее,
    # чтобы в процессе не было пауз на переборе (живой ключ тратит 1 лёгкий запрос).
    if config.key_check and pool.total() > 5 and queries:
        print(f"  [api] проверяю {pool.total()} ключ(ей) "
              f"{'через прокси ' if config.proxy else ''}(до 15с на ключ)…",
              file=sys.stderr, flush=True)
        _pp = build_search_params(queries[0], regions[0], config.only_active, 1)
        _reasons = {}          # ключ (обрезанный) → почему выбыл
        _rlock = __import__("threading").Lock()

        def _probe(k):
            reason = None
            try:
                r = session.get(SEARCH_URL, params={**_pp, "key": k}, timeout=15)
                try:
                    pl = r.json()
                except Exception:  # noqa: BLE001
                    pl = {}
                # сначала лимит/тариф: checko на исчерпанной квоте отдаёт 403,
                # но это исчерпанный ключ, а не невалидный.
                if _is_limit_meta(pl):
                    meta = pl.get("meta") if isinstance(pl, dict) else None
                    reason = (meta or {}).get("message") or "лимит/тариф"
                elif r.status_code in (401, 403):
                    reason = f"HTTP {r.status_code} (ключ невалиден/нет доступа)"
            except Exception as exc:  # noqa: BLE001
                reason = f"сеть: {type(exc).__name__}"      # не считаем мёртвым по сети
            if reason and "сеть:" not in reason:
                pool.mark_dead(k)
                with _rlock:
                    _reasons[f"…{k[-4:]}"] = reason

        with ThreadPoolExecutor(max_workers=min(10, pool.total())) as _pex:
            list(_pex.map(_probe, pool.keys_snapshot()))
        print(f"  [api] живых ключей: {pool.alive()} из {pool.total()}", file=sys.stderr, flush=True)
        if _reasons:
            # сводка причин выбывания: одинаковые сообщения группируем
            by_msg = {}
            for msg in _reasons.values():
                by_msg[msg] = by_msg.get(msg, 0) + 1
            for msg, cnt in sorted(by_msg.items(), key=lambda x: -x[1]):
                print(f"    ↳ {cnt} ключ(ей) выбыло: {msg}", file=sys.stderr, flush=True)
        if pool.alive() == 0:
            print("  [api] нет живых ключей (исчерпаны/невалидны). Завтра докачает.",
                  file=sys.stderr, flush=True)
            return
    skip = skip or set()
    seen: set[str] = set()
    count = 0
    stats = {"fetched": 0, "drop_okved": 0, "drop_inactive": 0, "errors": 0, "search": 0}

    def fetch_card(inn, query):
        c = Company(inn=inn, okved_code=query, enrich_source="api")
        payload = pooled_api_get(session, pool, API_URL, {"inn": inn}, delay=config.delay)
        data = payload.get("data") or payload.get("Данные") or payload
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
            done = done_codes or set()
            extract_total = CheckoClient.extract_search_total
            todo = [q for q in queries if q not in done]
            print(f"  [api] кодов к обработке: {len(todo)} "
                  f"(пропущено пройденных: {len(queries) - len(todo)})", file=sys.stderr, flush=True)
            for region in regions:
                for qi, query in enumerate(queries, 1):
                    # Код уже полностью пройден в прошлом прогоне — пропускаем целиком,
                    # не пролистывая заново (экономим запросы при докачке).
                    if query in done:
                        continue
                    # 1) перечисляем ИНН по коду (поиск, через пул, последовательно).
                    #    Стоп, если страница не принесла НИ ОДНОГО нового ИНН
                    #    (checko повторяет записи на «лишних» страницах) ИЛИ уже
                    #    увидели все ЗапВсего записей.
                    inns = []
                    page_all: set[str] = set()
                    new_for_code = 0
                    total = None
                    page = 1
                    search_ok = True     # код пройден до конца (не оборван лимитом/ошибкой)
                    print(f"  [api] [{qi}/{len(queries)}] код {query}: листаю…", file=sys.stderr, flush=True)
                    while page <= MAX_SEARCH_PAGES:
                        try:
                            stats["search"] += 1
                            payload = pooled_api_get(session, pool, SEARCH_URL,
                                                     build_search_params(query, region, config.only_active, page))
                        except CheckoLimit:
                            print(f"  [api] лимит всех ключей исчерпан на поиске "
                                  f"(код {query}, стр.{page}, собрано {count}). Завтра докачает.",
                                  file=sys.stderr, flush=True)
                            return
                        except Exception as exc:  # noqa: BLE001
                            print(f"  [api] {query} стр.{page}: {exc}", file=sys.stderr)
                            search_ok = False
                            break
                        if total is None:
                            total = extract_total(payload) or 0
                        recs = extract_search_records(payload)
                        if not recs:
                            break
                        fresh_page = 0
                        for rec in recs:
                            stub = company_from_search_record(rec)
                            if not stub.inn or stub.inn in page_all:
                                continue
                            page_all.add(stub.inn)
                            fresh_page += 1
                            if stub.inn not in seen and stub.inn not in skip:
                                seen.add(stub.inn)
                                inns.append(stub.inn)
                                # режим «только список» — сразу отдаём без карточки
                                if not config.enrich_contacts:
                                    # искали по точному коду query → он и есть ОКВЭД записи
                                    stub.okved_code = query
                                    stub.okved_name = _okved_name(query) or stub.okved_name
                                    stub.enrich_source = "api-list"
                                    new_for_code += 1
                                    yield stub
                                    count += 1
                                    if on_progress:
                                        on_progress(count)
                                    if config.limit and count >= config.limit:
                                        return
                        if fresh_page == 0:      # повтор/конец — дальше листать бессмысленно
                            break
                        # уже увидели все записи по коду (ЗапВсего) — дальше только повторы
                        if total and len(page_all) >= total:
                            break
                        if page % 25 == 0:       # heartbeat на длинных кодах
                            tot = f"/~{total}" if total else ""
                            print(f"  [api]   {query}: стр.{page}, увидено {len(page_all)}{tot}, "
                                  f"новых {new_for_code}, живых ключей {pool.alive()}",
                                  file=sys.stderr, flush=True)
                        page += 1
                    if not config.enrich_contacts:
                        # список по коду собран целиком — отмечаем чекпоинт
                        if search_ok and on_code_done:
                            on_code_done(query)
                        print(f"  [api] [{qi}/{len(queries)}] код {query}: готов "
                              f"(+{new_for_code} новых, увидено {len(page_all)}"
                              f"{'/' + str(total) if total else ''})", file=sys.stderr, flush=True)
                        continue
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
                    # карточки по коду разобраны целиком — чекпоинт
                    if search_ok and on_code_done:
                        on_code_done(query)
    finally:
        summary()


def iter_enrich(config: PipelineConfig, inns, on_progress=None, skip: set | None = None) -> Iterator[Company]:
    """Дообогащение контактами по ГОТОВОМУ списку ИНН — без поиска и пагинации.
    Параллельно (config.concurrency), с пулом ключей."""
    import sys
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import requests as _requests
    from .checko import (CheckoLimit, KeyPool, pooled_api_get, _parse_keys, API_URL, DEFAULT_UA)

    matcher = _matcher(config)
    pool = KeyPool(_parse_keys(config.api_key))
    conc = min(max(2, config.concurrency), max(1, pool.total()))
    session = _requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Accept-Language": "ru,en;q=0.8"})
    if _proxies(config.proxy):
        session.proxies.update(_proxies(config.proxy))
        print(f"  [api] через прокси: {config.proxy}", file=sys.stderr)
    skip = skip or set()
    todo = [str(i).strip() for i in inns if str(i).strip() and str(i).strip() not in skip]
    print(f"  [api] дообогащение: {len(todo)} ИНН, параллельно {conc}, ключей {pool.total()}",
          file=sys.stderr)

    def fetch(inn):
        c = Company(inn=inn, enrich_source="api")
        payload = pooled_api_get(session, pool, API_URL, {"inn": inn}, delay=config.delay)
        data = payload.get("data") or payload.get("Данные") or payload
        fill_company_from_data(c, data)
        c.enriched = True
        return c

    count = 0
    errors = 0
    try:
        with ThreadPoolExecutor(max_workers=conc) as ex:
            futures = {ex.submit(fetch, inn): inn for inn in todo}
            for fut in as_completed(futures):
                try:
                    c = fut.result()
                except CheckoLimit:
                    print(f"  [api] лимит всех ключей исчерпан (дообогащено {count}). Продолжите позже.",
                          file=sys.stderr)
                    return
                except Exception:  # noqa: BLE001
                    errors += 1
                    continue
                if config.main_okved_only and not matcher.matches(c.okved_code):
                    continue
                if config.only_active and c.status and not _is_active_status(c.status):
                    continue
                yield c
                count += 1
                if on_progress:
                    on_progress(count)
    finally:
        print(f"  [api] дообогащено: {count}, ошибок: {errors}", file=sys.stderr)


def iter_enrich_site(config: PipelineConfig, inns, on_progress=None,
                     skip: set | None = None) -> Iterator[Company]:
    """Дообогащение по ГОТОВОМУ списку ИНН ЧЕРЕЗ САЙТ checko (HTML, без API-лимита).

    Ходит по страницам /company/<ИНН>, тянет контакты из HTML. Последовательно
    и с задержкой (сайт чувствителен к частоте). skip — уже обогащённые ИНН.
    Для открытых контактов нужны куки авторизованного аккаунта (config.cookie).
    config.browser=True — через настоящий браузер (Playwright, профиль/куки):
    надёжнее против анти-бот защиты.

    Ошибочные ИНН НЕ отдаются (не попадают в выходной CSV) — при следующем
    прогоне они повторятся автоматически."""
    import sys
    if config.browser:
        print("  [site] запускаю браузер (Playwright/Chromium)… это может занять "
              "10–30 сек при первом старте.", file=sys.stderr, flush=True)
        try:
            from .browser import BrowserSiteClient
            client = BrowserSiteClient(cookie=config.cookie, delay=config.delay or 2.0)
        except Exception as exc:  # noqa: BLE001
            print(f"  [site] НЕ удалось запустить браузер: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            print("  [site] установи один раз:  .venv\\Scripts\\pip install playwright  и  "
                  ".venv\\Scripts\\playwright install chromium  — или сними галочку «браузер» "
                  "(тогда обычные HTTP-запросы).", file=sys.stderr, flush=True)
            return
        print("  [site] браузер запущен: профиль/куки из настроек", file=sys.stderr, flush=True)
    else:
        from .site import CheckoSiteClient
        client = CheckoSiteClient(cookie=config.cookie, delay=config.delay or 2.0,
                                  user_agent=config.user_agent)
        if _proxies(config.proxy):
            client.session.proxies.update(_proxies(config.proxy))
            print(f"  [site] через прокси: {config.proxy}", file=sys.stderr, flush=True)
    skip = skip or set()
    todo, _seen = [], set()
    for it in inns:
        inn, ogrn = (it if isinstance(it, (tuple, list)) else (it, ""))
        inn = str(inn or "").strip()
        ogrn = str(ogrn or "").strip()
        if inn and inn not in skip and inn not in _seen:
            _seen.add(inn)
            todo.append((inn, ogrn))
    print(f"  [site] дообогащение через сайт: {len(todo)} ИНН"
          f"{' (с куки)' if config.cookie else ' (без куки — контакты могут быть скрыты)'}"
          f"{' [браузер]' if config.browser else ''}. Поиск ОГРН по ИНН → карточка.",
          file=sys.stderr, flush=True)

    count = 0
    errors = 0
    fails = 0                      # ошибок подряд (блок/429)
    MAX_FAILS = 15
    try:
        for inn, ogrn in todo:
            try:
                c = client.card_by_inn(inn, ogrn=ogrn)
                if c.enrich_error:              # 200, но страница без данных (капча/заглушка)
                    raise RuntimeError(c.enrich_error)
                fails = 0
            except Exception as exc:  # noqa: BLE001
                fails += 1
                errors += 1
                # первые ошибки показываем подробно, дальше — каждую 25-ю
                if errors <= 10 or errors % 25 == 0:
                    print(f"  [site] {inn}: ОШИБКА {type(exc).__name__}: {exc}",
                          file=sys.stderr, flush=True)
                if fails >= MAX_FAILS:
                    print(f"  [site] {fails} ошибок подряд — сайт блокирует запросы, "
                          f"останавливаюсь (обогащено {count}). Ошибочные ИНН в базу не "
                          f"записаны — при следующем запуске повторятся.", file=sys.stderr, flush=True)
                    print("  [site] что попробовать: 1) увеличить задержку (4–5 с); "
                          "2) куки + UA из ОДНОГО браузера; 3) режим браузера (галочка); "
                          "4) сменить IP/прокси.", file=sys.stderr, flush=True)
                    return
                continue
            if config.only_active and c.status and not _is_active_status(c.status):
                continue
            yield c
            count += 1
            if on_progress:
                on_progress(count)
            if config.limit and count >= config.limit:
                return
    finally:
        closer = getattr(client, "close", None)
        if callable(closer):
            closer()
        print(f"  [site] обогащено через сайт: {count}, ошибок: {errors}",
              file=sys.stderr, flush=True)


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


def _iter_api(config: PipelineConfig, on_progress, skip: set | None = None,
              done_codes: set | None = None, on_code_done=None) -> Iterator[Company]:
    import os
    import sys
    from .checko import CheckoLimit
    debug = os.environ.get("CHECKO_DEBUG") == "1"
    matcher = _matcher(config)
    client = CheckoClient(api_key=config.api_key, prefer_api=True, delay=config.delay,
                          proxy=config.proxy)
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

    done = done_codes or set()
    try:
        for region in regions:
            for query in queries:
                if query in done:            # код уже пройден целиком — пропускаем
                    continue
                page = 1
                page_all: set[str] = set()
                search_ok = True             # код пройден до конца (не оборван)
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
                        search_ok = False
                        break
                    records = client.extract_search_records(payload)
                    if not records:
                        break
                    fresh_page = 0
                    for rec in records:
                        stub = company_from_search_record(rec)
                        if not stub.inn or stub.inn in page_all:
                            continue
                        page_all.add(stub.inn)
                        fresh_page += 1
                        if stub.inn in seen or stub.inn in skip:
                            continue
                        seen.add(stub.inn)
                        if not config.enrich_contacts:   # режим «только список»
                            # искали по точному коду query → он и есть ОКВЭД записи
                            stub.okved_code = query
                            stub.okved_name = _okved_name(query) or stub.okved_name
                            stub.enrich_source = "api-list"
                            yield stub
                            count += 1
                            if on_progress:
                                on_progress(count)
                            if config.limit and count >= config.limit:
                                return
                            continue
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
                    if fresh_page == 0:       # страница без новых ИНН — повтор/конец
                        break
                    page += 1
                # код разобран целиком — чекпоинт для докачки
                if search_ok and on_code_done:
                    on_code_done(query)
    finally:
        summary()


def count_companies(config: PipelineConfig, delay: float | None = None):
    """Считает число компаний по каждому коду (через ЗапВсего в /v2/search),
    НЕ скачивая сами компании. Возвращает (список (код, кол-во), всего).

    Тратит по 1 лёгкому запросу поиска на код (× число регионов)."""
    import sys
    client = CheckoClient(api_key=config.api_key, prefer_api=True,
                          delay=delay if delay is not None else config.delay,
                          proxy=config.proxy)
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
