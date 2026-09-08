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

# SPEC §4.2: суффиксы виртуальных таблиц. Длинные проверяются раньше коротких,
# иначе BalanceAndTurnovers опознается как Balance.
VIRTUAL_SUFFIXES: tuple[str, ...] = (
    "BalanceAndTurnovers",
    "RecordsWithExtDimensions",
    "ActualActionPeriod",
    "DrCrTurnovers",
    "ExtDimensions",
    "ScheduleData",
    "SliceFirst",
    "Turnovers",
    "SliceLast",
    "Balance",
    "Base",
)

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
    parent: str | None = None
    is_tabular_part: bool = False
    is_virtual: bool = False
    virtual_kind: str | None = None


def parse_entity_name(name: str) -> EntityName:
    kind, _, остаток = name.partition("_")
    russian_kind = KINDS.get(kind, kind)
    if not остаток:
        return EntityName(full=name, kind=kind, russian_kind=russian_kind, base_name=name)

    for suffix in VIRTUAL_SUFFIXES:
        if остаток.endswith("_" + suffix):
            base_name = остаток[: -len(suffix) - 1]
            return EntityName(
                full=name,
                kind=kind,
                russian_kind=russian_kind,
                base_name=base_name,
                parent=f"{kind}_{base_name}",
                is_virtual=True,
                virtual_kind=suffix,
            )

    if "_" in остаток:
        base_name, _, _часть = остаток.partition("_")
        return EntityName(
            full=name,
            kind=kind,
            russian_kind=russian_kind,
            base_name=base_name,
            parent=f"{kind}_{base_name}",
            is_tabular_part=True,
        )

    return EntityName(full=name, kind=kind, russian_kind=russian_kind, base_name=остаток)


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
