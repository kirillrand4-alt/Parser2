from metalparser.okved import OkvedMatcher, normalize_code, resolve_prefixes


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


def test_resolve_prefixes_dedup():
    pref = resolve_prefixes("core", ["25", "33.11"])
    assert pref.count("25") == 1
    assert "33.11" in pref
    assert pref[:3] == ["24", "25", "28.4"]
