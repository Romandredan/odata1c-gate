"""Разбор имён сущностей OData 1С и нормализация текста для поиска (SPEC §4.2, §4.4)."""

from __future__ import annotations

import dataclasses
import functools
import re

import snowballstemmer

# SPEC §4.2: вид выводится из префикса имени. Перечень открыт — неизвестный префикс не ошибка.
KINDS: dict[str, str] = {
    "Catalog": "Справочник",
    "Document": "Документ",
    "DocumentJournal": "ЖурналДокументов",
    "Constant": "Константа",
    "ExchangePlan": "ПланОбмена",
    "ChartOfCharacteristicTypes": "ПланВидовХарактеристик",
    "ChartOfAccounts": "ПланСчетов",
    "ChartOfCalculationTypes": "ПланВидовРасчета",
    "InformationRegister": "РегистрСведений",
    "AccumulationRegister": "РегистрНакопления",
    "CalculationRegister": "РегистрРасчета",
    "AccountingRegister": "РегистрБухгалтерии",
    "BusinessProcess": "БизнесПроцесс",
    "Task": "Задача",
    "Enum": "Перечисление",
}

_CAMEL = re.compile(
    r"(?<=[a-zа-яё0-9])(?=[A-ZА-Я])|(?<=[A-ZА-Я])(?=[A-ZА-Я][a-zа-яё])"
    r"|(?<=[a-zA-Z])(?=[а-яА-ЯёЁ])|(?<=[а-яА-ЯёЁ])(?=[a-zA-Z])"
)
_РАЗДЕЛИТЕЛИ = re.compile(r"[_\-\s]+")


@dataclasses.dataclass(slots=True)
class EntityName:
    full: str
    kind: str
    russian_kind: str
    base_name: str


def parse_entity_name(name: str) -> EntityName:
    """Вид — префикс до первого `_`; всё остальное — имя объекта целиком.

    Табличную часть, набор записей регистра и виртуальную таблицу по имени отделить нельзя:
    подчёркивания и цифры бывают в именах объектов (`InformationRegister_пр_ОчередьДействий`,
    `ExchangePlan_…_11_0_…`, проба P4). Структуру определяет разбор описания (edmx.py) по списку
    наборов, ключам и действиям.
    """
    kind, _, остаток = name.partition("_")
    return EntityName(
        full=name, kind=kind, russian_kind=KINDS.get(kind, kind), base_name=остаток or name
    )


def normalize(text: str) -> str:
    """Нижний регистр, ё→е, разделители убраны, CamelCase разбит пробелами (SPEC §4.4)."""
    без_разделителей = _РАЗДЕЛИТЕЛИ.sub(" ", text)
    разбитое = _CAMEL.sub(" ", без_разделителей)
    return " ".join(разбитое.lower().replace("ё", "е").split())


@functools.lru_cache(maxsize=2)
def _стеммер(язык: str):
    return snowballstemmer.stemmer(язык)


def stems(text: str) -> list[str]:
    """Основы слов: русский и английский Snowball по каждому слову нормализованного текста."""
    слова = normalize(text).split()
    русский, английский = _стеммер("russian"), _стеммер("english")
    основы = []
    for слово in слова:
        основа = русский.stemWord(слово) if _кириллица(слово) else английский.stemWord(слово)
        основы.append(основа)
    return основы


def _кириллица(слово: str) -> bool:
    return any("а" <= символ <= "я" or символ == "ё" for символ in слово)
