from metalparser.okved import OkvedMatcher, normalize_code, resolve_prefixes, search_codes


def test_normalize():
    assert normalize_code(" 25,62 ") == "25.62"
    assert normalize_code("28.41") == "28.41"
    assert normalize_code("") == ""


def test_prefix_matching():
    m = OkvedMatcher(["24", "25", "28.4"])
    assert m.matches("25.62")
    assert m.matches("25")
    assert m.matches("24.10")
    assert m.matches("28.41")
    assert m.matches("28.4")


def test_prefix_boundaries():
    m = OkvedMatcher(["25", "28.4"])
    # не должны матчиться соседи по строковому префиксу
    assert not m.matches("255.0")
    assert not m.matches("250")
    assert not m.matches("2.50")
    assert m.matches("28.49")  # 28.49 относится к подклассу 28.4
    assert not m.matches("28.5")


def test_search_codes_expansion():
    codes = search_codes(["24", "25", "28.4"])
    # развёрнуты конкретные группы, нет «голых» префиксов
    assert "25.62" in codes and "24.10" in codes and "28.41" in codes and "28.49" in codes
    assert "24" not in codes and "25" not in codes and "28.4" not in codes
    # не затронуты чужие классы
    assert all(not c.startswith("46.") for c in codes)
    assert all(not c.startswith("33.") for c in codes)


def test_search_codes_wide_and_custom():
    wide = search_codes(["46.72", "33.11"])
    assert "46.72" in wide and "33.11" in wide
    # любой класс разворачивается в свои группы из полного справочника ОКВЭД-2
    it = search_codes(["62"])
    assert "62.01" in it and "62.02" in it and "62" not in it
    # произвольный код вне справочника добавляется как есть
    assert "99.01" in search_codes(["99.01"]) or search_codes(["99.01"]) == ["99.01"]


def test_resolve_prefixes_dedup():
    pref = resolve_prefixes("core", ["25", "33.11"])
    assert pref.count("25") == 1
    assert "33.11" in pref
    assert pref[:3] == ["24", "25", "28.4"]
