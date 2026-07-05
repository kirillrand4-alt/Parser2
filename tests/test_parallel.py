"""Параллельный API-режим: пул ключей, выбывание исчерпавшего лимит, сбор всех."""
import threading

import metalparser.pipeline as pipeline
from metalparser.pipeline import PipelineConfig, run
from metalparser.checko import KeyPool


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def test_key_pool_exhaustion():
    pool = KeyPool(["a", "b", "c"])
    assert pool.alive() == 3
    pool.mark_dead("b")
    assert pool.alive() == 2
    got = {pool.acquire() for _ in range(20)}
    assert "b" not in got and got <= {"a", "c"}
    pool.mark_dead("a"); pool.mark_dead("c")
    assert pool.acquire() is None


def test_parallel_collect(monkeypatch):
    # Поиск по 25.62 → 4 ИНН; ключ k1 исчерпан (лимит), k2 работает.
    search = {"data": {"Записи": [{"ИНН": str(i)} for i in (11, 22, 33, 44)]}}
    companies = {str(i): {"НаимСокр": f"ООО {i}", "ОКВЭД": {"Код": "25.62", "Наим": "мех"},
                          "Статус": {"Наим": "Действует"}} for i in (11, 22, 33, 44)}
    lock = threading.Lock()

    class FakeSession:
        def __init__(self): self.headers = {}
        def get(self, url, params=None, timeout=None):
            key = params.get("key")
            with lock:
                if key == "k1":   # первый ключ всегда «исчерпан»
                    return FakeResp({"meta": {"status": "error", "message": "Превышен суточный лимит"}})
            if "search" in url:
                return FakeResp(search)
            inn = params.get("inn")
            return FakeResp({"data": companies[inn]})

    monkeypatch.setattr(pipeline, "search_codes", lambda p: ["25.62"])
    import requests as _r
    monkeypatch.setattr(_r, "Session", lambda: FakeSession())

    cfg = PipelineConfig(source="api", okved_set="none", extra_okved=["25.62"],
                         api_key="k1,k2", delay=0, concurrency=3)
    got = {c.inn for c in run(cfg)}
    assert got == {"11", "22", "33", "44"}   # все собраны, несмотря на мёртвый k1


def test_iter_enrich(monkeypatch):
    from metalparser.pipeline import iter_enrich, PipelineConfig
    companies = {
        "11": {"НаимСокр": "ООО 11", "ОКВЭД": {"Код": "25.62"}, "Статус": {"Наим": "Действует"},
               "Контакты": {"Тел": ["+74950000000"]}},
        "22": {"НаимСокр": "ООО 22", "ОКВЭД": {"Код": "25.62"}, "Статус": {"Наим": "Действует"}},
    }

    class FakeSession:
        def __init__(self): self.headers = {}
        def get(self, url, params=None, timeout=None):
            return FakeResp({"data": companies[params["inn"]]})

    import requests as _r
    monkeypatch.setattr(_r, "Session", lambda: FakeSession())
    cfg = PipelineConfig(source="api", api_key="k1,k2", concurrency=2, delay=0)
    got = {c.inn: c for c in iter_enrich(cfg, ["11", "22"])}
    assert set(got) == {"11", "22"}
    assert got["11"].phones == ["+7 (495) 000-00-00"]
    assert got["11"].name == "ООО 11"
