"""Оффлайн-тесты источника «сайт»: конвертация кода, разбор карточки,
цикл каталог→карточки с пост-фильтром статуса. Сеть не используется."""
import metalparser.site as site
from metalparser.site import code6, parse_card, _OGRN_LINK_RE
from metalparser.models import Company
from metalparser.pipeline import PipelineConfig, run
import metalparser.pipeline as pipeline


def test_code6():
    assert code6("25.62") == "256200"
    assert code6("24.10") == "241000"
    assert code6("25") == "250000"
    assert code6("28.41") == "284100"


def test_catalog_link_regex():
    html = ('<a href="/company/evraz-market-1026102571505">ЕВРАЗ</a>'
            '<a href="https://checko.ru/company/select?code=all">все</a>'
            '<a href="/company/vkm-1053109263459">ВКМ</a>')
    ogrns = _OGRN_LINK_RE.findall(html)
    assert ogrns == ["1026102571505", "1053109263459"]


CARD_HTML = """
<html><head><title>АО "ЕВРАЗ МАРКЕТ" - Таганрог - ИНН 6154062128 - Гендиректор Береза Н. В.</title></head>
<body>
  <span>Статус: Действующая</span>
  <a href="tel:+74951234567">+7 495 123-45-67</a>
  <a href="mailto:info@evraz.com">info@evraz.com</a>
  <a href="https://evraz.com">сайт компании</a>
  <a href="https://zakupki.gov.ru/epz/contract">закупки</a>
  <a href="https://checko.ru/company/x">checko</a>
</body></html>
"""


def test_parse_card():
    c = parse_card(CARD_HTML, ogrn="1026102571505", okved_code="25.62")
    assert c.name == 'АО "ЕВРАЗ МАРКЕТ"'
    assert c.inn == "6154062128"
    assert c.region == "Таганрог"
    assert c.okved_code == "25.62"
    assert "механическ" in c.okved_name.lower()   # подставилось из справочника
    assert c.status == "Действующая"
    assert any("4951234567" in p.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
               for p in c.phones)
    assert "info@evraz.com" in c.emails
    assert "https://evraz.com" in c.websites
    assert all("zakupki" not in s and "checko" not in s for s in c.websites)


class FakeSiteClient:
    def __init__(self, *a, **k):
        pass

    def catalog_ogrns(self, code, page):
        return {("25.62", 1): ["111", "222", "333"]}.get((code, page), [])

    def card(self, ogrn, okved_code=""):
        d = {
            "111": ("ООО А", "Действующая", ["+7 (495) 111-22-33"]),
            "222": ("ООО Б", "Ликвидирована", []),   # отсев по статусу
            "333": ("ООО В", "Действующая", []),
        }
        name, status, phones = d[ogrn]
        return Company(inn=ogrn, ogrn=ogrn, name=name, okved_code=okved_code,
                       status=status, phones=phones, enrich_source="site")


def test_iter_site(monkeypatch):
    monkeypatch.setattr(site, "CheckoSiteClient", FakeSiteClient)
    cfg = PipelineConfig(source="site", okved_set="none", extra_okved=["25.62"],
                         cookie="x", delay=0, only_active=True)
    companies = run(cfg)
    inns = {c.inn for c in companies}
    assert inns == {"111", "333"}    # 222 (ликвидирована) отсеяна
    assert all(c.enrich_source == "site" for c in companies)


def test_iter_site_all_statuses(monkeypatch):
    monkeypatch.setattr(site, "CheckoSiteClient", FakeSiteClient)
    cfg = PipelineConfig(source="site", okved_set="none", extra_okved=["25.62"],
                         cookie="x", delay=0, only_active=False)
    assert {c.inn for c in run(cfg)} == {"111", "222", "333"}


def test_iter_site_resume_skip(monkeypatch):
    monkeypatch.setattr(site, "CheckoSiteClient", FakeSiteClient)
    cfg = PipelineConfig(source="site", okved_set="none", extra_okved=["25.62"],
                         cookie="x", delay=0, only_active=True)
    # 111 уже собран ранее -> пропускаем; 222 ликвидирована -> отсев; остаётся 333
    got = {c.inn for c in run(cfg, skip={"111"})}
    assert got == {"333"}


def test_csv_appender_resume(tmp_path):
    from metalparser.export import CsvAppender, read_existing_keys
    from metalparser.models import Company
    p = str(tmp_path / "out.csv")
    a = CsvAppender(p)
    a.write(Company(inn="111", name="A"))
    a.close()
    assert read_existing_keys(p) == {"111"}
    # дозапись не дублирует заголовок
    a2 = CsvAppender(p)
    a2.write(Company(inn="222", name="B"))
    a2.close()
    assert read_existing_keys(p) == {"111", "222"}
    with open(p, encoding="utf-8-sig") as f:
        assert f.read().count("ИНН;ОГРН") == 1
