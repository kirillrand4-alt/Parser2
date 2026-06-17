import os

from metalparser.egrul import iter_companies
from metalparser.okved import OkvedMatcher, resolve_prefixes

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_egrul.xml")


def _run(prefixes, only_active=True):
    matcher = OkvedMatcher(prefixes)
    return list(iter_companies(FIXTURE, matcher, only_active=only_active))


def test_core_active_only():
    companies = _run(["24", "25", "28.4"], only_active=True)
    inns = {c.inn for c in companies}
    # действующие металлообработка: 25.62, 24.10, 28.41, 25.61 (старый тег)
    assert inns == {"2720011111", "7700000002", "7700000003", "7700000007"}


def test_excludes_non_metal_main_okved():
    companies = _run(["24", "25", "28.4"], only_active=True)
    # ИТ-компания с доп. ОКВЭД 25.62 не должна попасть (основной 62.01)
    assert "7700000004" not in {c.inn for c in companies}


def test_excludes_terminated_and_liquidating():
    companies = _run(["24", "25", "28.4"], only_active=True)
    inns = {c.inn for c in companies}
    assert "7700000005" not in inns  # прекращено
    assert "7700000006" not in inns  # ликвидация


def test_all_statuses_includes_terminated():
    companies = _run(["24", "25", "28.4"], only_active=False)
    inns = {c.inn for c in companies}
    assert "7700000005" in inns
    assert "7700000006" in inns


def test_prefix_boundary_false_positive():
    # ОКВЭД "2.50" не должен матчиться на префикс "25"
    companies = _run(["25"], only_active=False)
    assert "7700000008" not in {c.inn for c in companies}


def test_fields_extracted():
    companies = _run(["24", "25", "28.4"])
    by_inn = {c.inn: c for c in companies}
    c = by_inn["2720011111"]
    assert c.name == "ООО ДАЛТРЕЙДСЕРВИС"
    assert c.okved_code == "25.62"
    assert "механическая" in c.okved_name.lower()
    assert "Хабаровский" in c.region
    assert c.status == "Действующее"
    assert c.ogrn == "1062720011573"
