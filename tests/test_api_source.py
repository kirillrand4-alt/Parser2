"""Оффлайн-тест режима source=api: цикл поиска, дедуп, пост-фильтр по
основному ОКВЭД. Сетевые вызовы заменены фейковым клиентом."""
import metalparser.pipeline as pipeline
from metalparser.checko import CheckoClient
from metalparser.pipeline import PipelineConfig, run


# Выдача поиска: две компании с ОКВЭД 25.*, одна с 62.01 (должна отсеяться)
SEARCH_PAGES = {
    ("25", 1): {"Записи": [
        {"ИНН": "111", "НаимСокр": "ООО А", "ОКВЭД": {"Код": "25.62", "Наим": "Мехобработка"}},
        {"ИНН": "222", "НаимСокр": "ООО Б", "ОКВЭД": {"Код": "25.50", "Наим": "Ковка"}},
        {"ИНН": "333", "НаимСокр": "ООО ИТ", "ОКВЭД": {"Код": "25.62", "Наим": "..."}},
        {"ИНН": "111", "НаимСокр": "ООО А дубль", "ОКВЭД": {"Код": "25.62", "Наим": "..."}},
    ]},
}

COMPANY = {
    "111": {"НаимСокр": "ООО А", "ОГРН": "1", "ОКВЭД": {"Код": "25.62", "Наим": "Мехобработка"},
            "Регион": "Москва", "Статус": "Действующее",
            "Контакты": {"Тел": ["+74951112233"], "Емэйл": ["a@a.ru"], "ВебСайт": ["a.ru"]}},
    "222": {"НаимСокр": "ООО Б", "ОГРН": "2", "ОКВЭД": {"Код": "25.50", "Наим": "Ковка"},
            "Регион": "Тула", "Статус": "Действующее", "Контакты": {}},
    # У 333 основной ОКВЭД на самом деле 62.01 (ОКВЭД 25.62 — дополнительный) -> отсев
    "333": {"НаимСокр": "ООО ИТ", "ОГРН": "3", "ОКВЭД": {"Код": "62.01", "Наим": "ПО"},
            "Регион": "Москва", "Статус": "Действующее", "Контакты": {}},
}


class FakeClient:
    def __init__(self, *a, **k):
        pass

    def search_page(self, query, region, active, page):
        return SEARCH_PAGES.get((query, page), {"Записи": []})

    extract_search_records = staticmethod(CheckoClient.extract_search_records)

    def company_data(self, inn):
        return COMPANY[inn]


def test_api_source(monkeypatch):
    monkeypatch.setattr(pipeline, "CheckoClient", FakeClient)
    config = PipelineConfig(source="api", okved_set="core", api_key="x", delay=0)
    companies = run(config)
    by_inn = {c.inn: c for c in companies}

    # 333 отсеян (основной ОКВЭД 62.01); 111 не задублирован
    assert set(by_inn) == {"111", "222"}
    a = by_inn["111"]
    assert a.name == "ООО А"
    assert a.okved_code == "25.62"
    assert a.region == "Москва"
    assert a.phones == ["+7 (495) 111-22-33"]
    assert a.emails == ["a@a.ru"]
    assert a.enrich_source == "api"


def test_api_limit(monkeypatch):
    monkeypatch.setattr(pipeline, "CheckoClient", FakeClient)
    config = PipelineConfig(source="api", okved_set="core", api_key="x", delay=0, limit=1)
    companies = run(config)
    assert len(companies) == 1
