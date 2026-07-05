import csv
import os

from metalparser.checko import extract_contacts_from_html, _extract_contacts_from_json, _norm_phone
from metalparser.export import write_csv, write_excel
from metalparser.models import Company


SAMPLE_HTML = """
<html><body>
  <div class="contacts">
    <a href="tel:+74951234567">+7 (495) 123-45-67</a>
    <a href="mailto:info@zavod.ru">info@zavod.ru</a>
    Доп. телефон: 8 (812) 765-43-21
    <a href="https://zavod.ru" rel="nofollow">сайт</a>
    <a href="https://checko.ru/company/x">checko</a>
    <img src="/assets/logo.png">
  </div>
</body></html>
"""


def test_html_extraction():
    phones, emails, sites = extract_contacts_from_html(SAMPLE_HTML)
    assert any("4951234567" in p.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
               for p in phones)
    assert "info@zavod.ru" in emails
    assert "https://zavod.ru" in sites
    assert all("checko.ru" not in s for s in sites)
    assert all(".png" not in s for s in sites)


def test_extract_extra_okved():
    from metalparser.checko import extract_extra_okved
    data = {"ОКВЭД": {"Код": "25.62"}, "ОКВЭДДоп": [
        {"Код": "46.72", "Наим": "Торговля"}, {"Код": "25.61", "Наим": "Покрытия"}]}
    assert extract_extra_okved(data) == ["46.72", "25.61"]
    assert extract_extra_okved({}) == []


def test_json_extraction():
    data = {
        "НаимСокр": "ООО ТЕСТ",
        "Контакты": {
            "Тел": ["+7 495 111 22 33", "84951112244"],
            "Емэйл": ["a@b.ru"],
            "ВебСайт": ["https://b.ru"],
        },
    }
    phones, emails, sites = _extract_contacts_from_json(data)
    assert len(phones) >= 2
    assert "a@b.ru" in emails
    assert "https://b.ru" in sites


def test_norm_phone():
    assert _norm_phone("84951234567") == "+7 (495) 123-45-67"
    assert _norm_phone("+7 495 123 45 67") == "+7 (495) 123-45-67"


def test_csv_export(tmp_path):
    companies = [
        Company(inn="111", name="ООО А", okved_code="25.62", phones=["+7 999"], emails=["a@a.ru"]),
        Company(inn="222", name="ООО Б", okved_code="24.10"),
    ]
    path = str(tmp_path / "out.csv")
    n = write_csv(companies, path)
    assert n == 2
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert rows[0]["ИНН"] == "111"
    assert rows[0]["Телефоны"] == "+7 999"
    assert rows[0]["Почты"] == "a@a.ru"


def test_excel_export(tmp_path):
    companies = [Company(inn="111", name="ООО А", okved_code="25.62")]
    path = str(tmp_path / "out.xlsx")
    n = write_excel(companies, path)
    assert n == 1
    assert os.path.getsize(path) > 0
