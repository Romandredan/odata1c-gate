"""Модель рецептов и чтение `recipes.yaml` (SPEC §8, план M1d задача 8).

Рецепт — именованный параметризованный запрос к одной сущности базы: имя регистра, набор полей,
условия и объявленные параметры. Это ДАННЫЕ, а не код: ни одно выражение из файла не исполняется,
значения параметров попадают в запрос только литералами OData (`render` в `recipes/render.py`),
а подстановка `{имя}` разрешена лишь там, где в готовом запросе стоит литерал. Всё остальное —
имя сущности, список полей, порядок сортировки — параметризации не подлежит: иначе рецепт стал бы
способом подставить в запрос произвольное имя поля мимо построителя и гейта.

Проверки этого модуля выполняются ОДИН РАЗ при чтении файла, а не при каждом вызове тула: файл
пишет владелец машины, и ошибка в нём должна быть видна целиком и сразу, а не всплывать на
отдельном сочетании параметров.
"""

from __future__ import annotations

import pathlib
import re
from typing import Literal

import pydantic
import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from odata1c.config.home import base_dir
from odata1c.config.loader import format_validation_error
from odata1c.config.models import BaseConfig

ParamType = Literal["datetime", "date", "guid", "string", "int", "decimal", "bool"]

# Тип параметра рецепта → тип EDM, по которому `odata_literal` строит литерал (SPEC §8).
# `date` и `datetime` дают один тип EDM: в OData 1С отдельного типа даты нет, а `odata_literal`
# сам дописывает `T00:00:00` к значению вида `ГГГГ-ММ-ДД`. Разница между ними — только в описании,
# которое видит модель: «дата» и «дата с временем» подсказывают разный формат значения.
ТИПЫ_EDM: dict[str, str] = {
    "datetime": "Edm.DateTime",
    "date": "Edm.DateTime",
    "guid": "Edm.Guid",
    "string": "Edm.String",
    "int": "Edm.Int64",
    "decimal": "Edm.Decimal",
    "bool": "Edm.Boolean",
}

# Параметр виртуальной таблицы, который несёт ВЫРАЖЕНИЕ отбора, а не одно значение: только он
# собирается из списка условий и склеивается через `and`. Остальные параметры виртуальной таблицы
# (`Period`, `StartPeriod`, `EndPeriod`, `Dimensions`) — одно значение каждый.
УСЛОВИЕ = "Condition"

_ПЛЕЙСХОЛДЕР = re.compile(r"\{([^{}]*)\}")
_ИМЯ_ПАРАМЕТРА = re.compile(r"^[^\W\d]\w*$")

# Как параметр сравнивается с полем — от этого зависят анти-оракульные правила гейта
# (`BaseGate.inbound_param`): равенство, сравнение по величине, поиск вхождения.
РАВЕНСТВО = "eq"
ПО_ВЕЛИЧИНЕ = "ordered"
ВХОЖДЕНИЕ = "substring"

_УПОРЯДОЧЕННЫЕ_ОПЕРАТОРЫ = frozenset({"gt", "ge", "lt", "le"})
_ФУНКЦИИ_ПОИСКА = "substringof|startswith|endswith"

# Поле слева от подстановки: «Склад_Key eq {warehouse}». Имя поля может быть путём через
# связанный объект («Контрагент/ИНН») — отсюда «всё, кроме пробелов, скобок и запятых».
_ПОЛЕ_СЛЕВА = re.compile(r"([^\s(),]+)\s+(eq|ne|gt|ge|lt|le)\s+$", re.IGNORECASE)
# Поле слева внутри вызова поиска: «startswith(Наименование, {q})».
_ПОЛЕ_В_ВЫЗОВЕ_СЛЕВА = re.compile(
    rf"\b(?:{_ФУНКЦИИ_ПОИСКА})\s*\(\s*([^\s(),]+)\s*,\s*$", re.IGNORECASE
)
# Поле справа от подстановки внутри вызова поиска: «substringof({q}, Наименование)».
_ПОЛЕ_В_ВЫЗОВЕ_СПРАВА = re.compile(r"^\s*,\s*([^\s(),]+)\s*\)")

# Символы, между которыми подстановка стоит В ПОЗИЦИИ ЛИТЕРАЛА: начало/конец условия, пробел,
# скобка, запятая. Кавычка в этот набор не входит намеренно — `guid'{organization}'` из примера
# SPEC §8 отклоняется: значение вставляется УЖЕ литералом (`guid'…'`), и обёртка в файле дала бы
# двойную обёртку, а разрешение подставлять «внутрь литерала» вернуло бы строковую интерполяцию,
# то есть внедрение условия через значение параметра.
_СЛЕВА_ОТ_ЛИТЕРАЛА = frozenset(" (,")
_СПРАВА_ОТ_ЛИТЕРАЛА = frozenset(" ),")


class RecipeError(Exception):
    """Ошибка рецепта: неизвестное имя (`recipe_unknown`), негодные параметры вызова
    (`recipe_param`) или неверно описанный файл рецептов (`config_invalid`). Тот же протокол
    атрибутов (code, message, hint), что у остальных ошибок слоя тулов."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


class Param(BaseModel):
    """Объявление параметра рецепта: тип литерала, обязательность и описание для модели."""

    model_config = ConfigDict(extra="forbid")

    type: ParamType = "string"
    required: bool = False
    description: str = ""


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    description: str = ""
    entity: str
    params: dict[str, Param] = Field(default_factory=dict)
    virtual: dict[str, str | list[str]] = Field(default_factory=dict)
    select: list[str] = Field(default_factory=list)
    filter: str | list[str] = Field(default_factory=list)
    orderby: str | None = None
    top: int | None = None

    # Имя параметра → (поле, вид сравнения) в условиях. Считается один раз при чтении файла
    # (`_проверить`), потому что тем же проходом проверяется и однозначность пары.
    _поля_параметров: dict[str, tuple[str, str]] = PrivateAttr(default_factory=dict)

    @property
    def conditions(self) -> list[str]:
        """Условия отбора результата (`filter`) — всегда списком, как их видит `render`."""
        return [self.filter] if isinstance(self.filter, str) else list(self.filter)

    @property
    def virtual_conditions(self) -> list[str]:
        """Условия внутри виртуальной таблицы (`virtual.Condition`) — всегда списком."""
        значение = self.virtual.get(УСЛОВИЕ)
        if значение is None:
            return []
        return [значение] if isinstance(значение, str) else list(значение)

    @property
    def virtual_values(self) -> dict[str, str]:
        """Параметры виртуальной таблицы, кроме `Condition`: имя → шаблон одного значения."""
        return {
            имя: значение
            for имя, значение in self.virtual.items()
            if имя != УСЛОВИЕ and isinstance(значение, str)
        }

    @model_validator(mode="after")
    def _проверить(self) -> Recipe:
        for имя in self.params:
            if not _ИМЯ_ПАРАМЕТРА.match(имя):
                raise ValueError(
                    f"имя параметра «{имя}» не годится: буква или подчёркивание в начале, "
                    "дальше буквы, цифры и подчёркивание"
                )
        if self.top is not None and self.top <= 0:
            raise ValueError(f"top должен быть положительным, получено {self.top}")

        # Подстановка запрещена всюду, кроме условий и значений параметров виртуальной таблицы:
        # в `entity`, `select` и `orderby` стоят ИМЕНА, а имя не литерал — значение параметра там
        # прошло бы в запрос текстом, мимо `odata_literal`.
        for место, текст in (
            ("entity", self.entity),
            ("orderby", self.orderby or ""),
            *(("select", поле) for поле in self.select),
        ):
            if _ПЛЕЙСХОЛДЕР.search(текст):
                raise ValueError(
                    f"подстановка параметра в «{место}» недопустима: там имя, а не значение"
                )

        for имя, значение in self.virtual.items():
            if имя != УСЛОВИЕ and not isinstance(значение, str):
                raise ValueError(
                    f"параметр виртуальной таблицы «{имя}» — одно значение, а не список"
                )

        for шаблон in self.virtual_values.values():
            места = _ПЛЕЙСХОЛДЕР.findall(шаблон)
            if места and шаблон.strip() != "{" + места[0] + "}":
                raise ValueError(
                    f"значение параметра виртуальной таблицы «{шаблон}» должно быть либо целиком "
                    "подстановкой «{имя}», либо постоянным значением без подстановок"
                )

        поля: dict[str, tuple[str, str]] = {}
        for условие in (*self.conditions, *self.virtual_conditions):
            self._проверить_условие(условие, поля)

        неизвестные = {
            имя
            for условие in (*self.conditions, *self.virtual_conditions)
            for имя in _ПЛЕЙСХОЛДЕР.findall(условие)
        } | set(_ПЛЕЙСХОЛДЕР.findall(" ".join(self.virtual_values.values())))
        неизвестные -= set(self.params)
        if неизвестные:
            raise ValueError(
                f"подстановка необъявленных параметров: {', '.join(sorted(неизвестные))}"
            )
        self._поля_параметров = поля
        return self

    def _проверить_условие(self, условие: str, поля: dict[str, tuple[str, str]]) -> None:
        """Проверить одно условие: подстановки стоят в позиции литерала, поле и вид сравнения
        определяются, и один параметр сравнивается всегда с одним и тем же полем одинаково.

        Поле и вид сравнения нужны не запросу, а гейту: `BaseGate.inbound_param` берёт по имени
        поля класс защиты, а по виду сравнения — анти-оракульное правило (упорядоченное сравнение
        с защищаемым полем запрещено, открытый образец поиска по вхождению — тоже, кроме названий
        и ФИО). Условие, в котором поле определить нельзя, отклоняется при чтении файла: иначе
        параметр молча получил бы проверку «класс неизвестен, всё разрешено».
        """
        for совпадение in _ПЛЕЙСХОЛДЕР.finditer(условие):
            имя = совпадение.group(1)
            слева = условие[: совпадение.start()]
            справа = условие[совпадение.end() :]
            if (слева and слева[-1] not in _СЛЕВА_ОТ_ЛИТЕРАЛА) or (
                справа and справа[0] not in _СПРАВА_ОТ_ЛИТЕРАЛА
            ):
                raise ValueError(
                    f"подстановка «{{{имя}}}» в условии «{условие}» стоит не в позиции литерала: "
                    "значение подставляется уже готовым литералом, кавычки и обёртку "
                    "«guid'…'» вокруг подстановки писать не нужно"
                )
            сравнение = _ПОЛЕ_СЛЕВА.search(слева)
            вызов_слева = _ПОЛЕ_В_ВЫЗОВЕ_СЛЕВА.search(слева)
            вызов_справа = _ПОЛЕ_В_ВЫЗОВЕ_СПРАВА.match(справа)
            if сравнение is not None:
                оператор = сравнение.group(2).lower()
                опознано = (
                    сравнение.group(1),
                    ПО_ВЕЛИЧИНЕ if оператор in _УПОРЯДОЧЕННЫЕ_ОПЕРАТОРЫ else РАВЕНСТВО,
                )
            elif вызов_слева is not None:
                опознано = (вызов_слева.group(1), ВХОЖДЕНИЕ)
            elif вызов_справа is not None:
                опознано = (вызов_справа.group(1), ВХОЖДЕНИЕ)
            else:
                raise ValueError(
                    f"в условии «{условие}» не видно поля, с которым сравнивается «{{{имя}}}»: "
                    "пишите «Поле eq {имя}», «substringof({имя}, Поле)» или "
                    "«startswith(Поле, {имя})»"
                )
            прежнее = поля.setdefault(имя, опознано)
            if прежнее != опознано:
                raise ValueError(
                    f"параметр «{имя}» сравнивается по-разному ({прежнее[0]} {прежнее[1]} и "
                    f"{опознано[0]} {опознано[1]}): у поля свой класс защиты и свои правила "
                    "сравнения, заведите отдельный параметр на каждое"
                )

    def param_field(self, name: str) -> str:
        """Поле, с которым сравнивается параметр, — для `gate.inbound_param` (класс защиты
        берётся по имени поля). Пустая строка, если параметр в условиях не используется вовсе
        (только как значение параметра виртуальной таблицы — `Period` и подобные)."""
        return self._поля_параметров.get(name, ("", РАВЕНСТВО))[0]

    def param_comparison(self, name: str) -> str:
        """Вид сравнения параметра с полем: `eq` (равенство/неравенство), `ordered` (по величине)
        или `substring` (поиск вхождения) — для анти-оракульных правил гейта."""
        return self._поля_параметров.get(name, ("", РАВЕНСТВО))[1]


class RecipeBook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    recipes: dict[str, Recipe] = Field(default_factory=dict)


def recipes_path(home: pathlib.Path, base: BaseConfig) -> pathlib.Path:
    """Путь к файлу рецептов базы: `recipes` из `bases.yaml` (относительно домашнего каталога,
    абсолютный — как есть) либо `bases/<имя>/recipes.yaml` по умолчанию.

    Умолчание обязательно, а не «удобно»: `odata1c base add --recipes ut` кладёт шаблон именно в
    `bases/<имя>/recipes.yaml`, но ключ `recipes` в `bases.yaml` не дописывает (он там показан
    закомментированным) — без запасного пути рецепты, скопированные штатной командой, не нашёл бы
    никто.
    """
    указанный = base.recipes
    if not указанный:
        return base_dir(home, base.name) / "recipes.yaml"
    путь = pathlib.Path(указанный)
    return путь if путь.is_absolute() else home / путь


def load_recipes(path: pathlib.Path) -> RecipeBook:
    """Прочитать `recipes.yaml`. Любая ошибка файла — `RecipeError` с кодом `config_invalid`
    (SPEC §5.2, поправка 2026-09-08: код для ошибок файлов настроек)."""
    путь = pathlib.Path(path)
    if not путь.exists():
        raise RecipeError(
            "config_invalid",
            f"файл рецептов не найден: {путь}",
            "скопируйте шаблон: odata1c base add --recipes ut|bp|zup",
        )
    try:
        данные = yaml.safe_load(путь.read_text(encoding="utf-8"))
    except yaml.YAMLError as ошибка:
        # Из исключения PyYAML берётся только позиция, но не его текст: библиотека вклеивает
        # в сообщение фрагмент самого файла (тот же приём, что в config/loader.py).
        отметка = getattr(ошибка, "problem_mark", None)
        место = (
            f"строка {отметка.line + 1}, колонка {отметка.column + 1}"
            if отметка is not None
            else "точное место в файле не определено"
        )
        raise RecipeError(
            "config_invalid",
            f"файл рецептов {путь.name} не разбирается как YAML: {место}",
            f"проверьте синтаксис файла {путь}",
        ) from ошибка
    except OSError as ошибка:
        raise RecipeError("config_invalid", f"файл рецептов {путь} не читается") from ошибка

    if данные is None:
        данные = {}
    if not isinstance(данные, dict):
        raise RecipeError(
            "config_invalid",
            f"файл рецептов {путь.name} должен быть набором полей version/recipes",
        )
    try:
        return RecipeBook(**данные)
    except pydantic.ValidationError as ошибка:
        raise RecipeError(
            "config_invalid",
            f"рецепты в {путь.name} описаны неверно: {format_validation_error(ошибка)}",
        ) from ошибка
