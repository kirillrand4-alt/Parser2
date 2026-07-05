"""Тест ротации API-ключей: при исчерпании лимита переключаемся на следующий."""
import pytest
import requests

from metalparser.checko import CheckoClient, CheckoLimit, _parse_keys


class FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


LIMIT = {"meta": {"status": "error", "message": "Превышен суточный лимит запросов"}}
OK = {"data": {"Записи": [{"ИНН": "1"}]}, "meta": {"status": "ok"}}


def test_parse_keys():
    assert _parse_keys("k1, k2  k3") == ["k1", "k2", "k3"]
    assert _parse_keys(["a", "a", "b"]) == ["a", "b"]
    assert _parse_keys("") == []


def test_rotation_switches(monkeypatch):
    c = CheckoClient(api_key="k1,k2", delay=0)
    assert c.keys == ["k1", "k2"]
    used = []

    def fake_get(url, params=None):
        used.append(params["key"])
        return FakeResp(LIMIT if params["key"] == "k1" else OK)

    monkeypatch.setattr(c, "_get", fake_get)
    payload = c.search_page("25.62", None, True, 1)
    assert c.extract_search_records(payload) == [{"ИНН": "1"}]
    assert used == ["k1", "k2"]      # переключился на второй
    assert c.api_key == "k2"


def test_rotation_on_401(monkeypatch):
    c = CheckoClient(api_key="bad,good", delay=0)
    used = []

    def fake_get(url, params=None):
        used.append(params["key"])
        if params["key"] == "bad":
            return FakeResp({"meta": {"status": "error", "message": "неверный ключ"}}, status=401)
        return FakeResp(OK)

    monkeypatch.setattr(c, "_get", fake_get)
    payload = c.search_page("25.62", None, True, 1)
    assert c.extract_search_records(payload) == [{"ИНН": "1"}]
    assert used == ["bad", "good"]      # 401 → перешли на следующий ключ


def test_rotation_all_exhausted(monkeypatch):
    c = CheckoClient(api_key="k1,k2", delay=0)

    def fake_get(url, params=None):
        return FakeResp(LIMIT)

    monkeypatch.setattr(c, "_get", fake_get)
    with pytest.raises(CheckoLimit):
        c.company_data("7700000000")
    assert c.api_key is None          # оба ключа исчерпаны
