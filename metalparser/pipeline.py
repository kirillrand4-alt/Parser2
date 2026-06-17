"""Оркестрация: ЕГРЮЛ → фильтр → дообогащение контактов → результат."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator

from .checko import CheckoClient
from .egrul import iter_companies
from .models import Company
from .okved import OkvedMatcher, resolve_prefixes


@dataclass
class PipelineConfig:
    egrul_path: str
    okved_set: str = "core"
    extra_okved: list[str] = field(default_factory=list)
    only_active: bool = True
    enrich: bool = True
    api_key: str | None = None
    prefer_api: bool = True
    delay: float = 1.5
    limit: int = 0  # 0 = без ограничения


def run(
    config: PipelineConfig,
    on_company: Callable[[Company], None] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> list[Company]:
    """Прогоняет пайплайн и возвращает список компаний.

    on_company вызывается на каждую готовую (возможно дообогащённую) запись —
    удобно для стриминга в веб/прогресс-бар.
    """
    matcher = OkvedMatcher(resolve_prefixes(config.okved_set, config.extra_okved))
    client = (
        CheckoClient(api_key=config.api_key, prefer_api=config.prefer_api, delay=config.delay)
        if config.enrich else None
    )

    results: list[Company] = []
    for company in iter_companies(
        config.egrul_path, matcher,
        only_active=config.only_active,
        progress_every=5000, on_progress=on_progress,
    ):
        if client is not None:
            client.enrich(company)
        results.append(company)
        if on_company:
            on_company(company)
        if config.limit and len(results) >= config.limit:
            break
    return results


def iter_run(config: PipelineConfig) -> Iterator[Company]:
    """Генераторная версия — отдаёт компании по мере готовности."""
    matcher = OkvedMatcher(resolve_prefixes(config.okved_set, config.extra_okved))
    client = (
        CheckoClient(api_key=config.api_key, prefer_api=config.prefer_api, delay=config.delay)
        if config.enrich else None
    )
    count = 0
    for company in iter_companies(config.egrul_path, matcher, only_active=config.only_active):
        if client is not None:
            client.enrich(company)
        yield company
        count += 1
        if config.limit and count >= config.limit:
            break
