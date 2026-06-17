"""Модели данных пайплайна."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class Company:
    """Запись о компании.

    Поля из ЕГРЮЛ заполняются на этапе парсинга XML; контактные поля
    (phones/emails/websites) — на этапе дообогащения через checko.
    """

    inn: str = ""
    ogrn: str = ""
    name: str = ""                 # краткое наименование
    full_name: str = ""            # полное наименование
    okved_code: str = ""           # основной ОКВЭД
    okved_name: str = ""           # расшифровка основного ОКВЭД
    region: str = ""
    status: str = ""               # статус (Действующее и т.п.)
    address: str = ""

    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    websites: list[str] = field(default_factory=list)

    enriched: bool = False         # было ли дообогащение контактами
    enrich_source: str = ""        # 'api' | 'html' | '' (не обогащалось)
    enrich_error: str = ""         # текст ошибки дообогащения, если была

    def to_row(self) -> dict[str, str]:
        """Плоская строка для CSV/Excel — списки склеиваются через '; '."""
        return {
            "ИНН": self.inn,
            "ОГРН": self.ogrn,
            "Название": self.name,
            "Полное название": self.full_name,
            "Основной ОКВЭД": self.okved_code,
            "Вид деятельности": self.okved_name,
            "Регион": self.region,
            "Статус": self.status,
            "Адрес": self.address,
            "Телефоны": "; ".join(self.phones),
            "Почты": "; ".join(self.emails),
            "Сайты": "; ".join(self.websites),
            "Источник контактов": self.enrich_source,
            "Ошибка обогащения": self.enrich_error,
        }

    def to_dict(self) -> dict:
        return asdict(self)
