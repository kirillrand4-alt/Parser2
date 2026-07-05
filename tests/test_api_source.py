"""Оффлайн-тест режима source=api: цикл поиска, дедуп, пост-фильтр по
основному ОКВЭД. Сетевые вызовы заменены фейковым клиентом."""
import metalparser.pipeline as pipeline
from metalparser.checko import CheckoClient
from metalparser.pipeline import PipelineConfig, run


# Поиск идёт по конкретным кодам-группам. Раскладываем записи по их кодам.
# 333 в выдаче по 25.62, но его ОСНОВНОЙ ОКВЭД на деле 62.01 -> отсев пост-фильтром.
SEARCH_PAGES = {
    ("25.62", 1): {"Записи": [
        {"ИНН": "111", "НаимСокр": "ООО А", "ОКВЭД": {"Код": "25.62", "Наим": "Мехобработка"}},
        {"ИНН": "333", "НаимСокр": "ООО ИТ", "ОКВЭД": {"Код": "25.62", "Наим": "..."}},
        {"ИНН": "111", "НаимСокр": "ООО А дубль", "ОКВЭД": {"Код": "25.62", "Наим": "..."}},
    ]},
    ("25.50", 1): {"Записи": [
        {"ИНН": "222", "НаимСокр": "ООО Б", "ОКВЭД": {"Код": "25.50", "Наим": "Ковка"}},
    ]},
    # 444 — недействующее (для проверки пост-фильтра по статусу)
    ("25.61", 1): {"Записи": [
        {"ИНН": "444", "НаимСокр": "ООО Ликвид", "ОКВЭД": {"Код": "25.61", "Наим": "..."}},
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
    # 444 — нужный ОКВЭД, но статус «прекращено» -> отсев при only_active
    "444": {"НаимСокр": "ООО Ликвид", "ОГРН": "4", "ОКВЭД": {"Код": "25.61", "Наим": "Покрытия"},
            "Регион": "Тула", "Статус": "Прекратило деятельность", "Контакты": {}},
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

    # По умолчанию берём и тех, у кого код дополнительный → 333 (осн. 62.01) остаётся;
    # 444 отсеян (недействующее); 111 не задублирован
    assert set(by_inn) == {"111", "222", "333"}
    a = by_inn["111"]
    assert a.name == "ООО А"
    assert a.okved_code == "25.62"
    assert a.region == "Москва"
    assert a.phones == ["+7 (495) 111-22-33"]
    assert a.emails == ["a@a.ru"]
    assert a.enrich_source == "api"


def test_api_main_okved_only(monkeypatch):
    monkeypatch.setattr(pipeline, "CheckoClient", FakeClient)
    # main_okved_only=True → 333 (осн. 62.01) отсеивается
    config = PipelineConfig(source="api", okved_set="core", api_key="x", delay=0,
                            main_okved_only=True)
    assert {c.inn for c in run(config)} == {"111", "222"}


def test_api_inactive_filtered_only_when_active(monkeypatch):
    monkeypatch.setattr(pipeline, "CheckoClient", FakeClient)
    # only_active=False -> недействующее 444 остаётся
    cfg = PipelineConfig(source="api", okved_set="core", api_key="x", delay=0, only_active=False)
    inns = {c.inn for c in run(cfg)}
    assert "444" in inns
    # only_active=True -> 444 отсеивается по статусу
    cfg2 = PipelineConfig(source="api", okved_set="core", api_key="x", delay=0, only_active=True)
    assert "444" not in {c.inn for c in run(cfg2)}


def test_api_limit(monkeypatch):
    monkeypatch.setattr(pipeline, "CheckoClient", FakeClient)
    config = PipelineConfig(source="api", okved_set="core", api_key="x", delay=0, limit=1)
    companies = run(config)
    assert len(companies) == 1
