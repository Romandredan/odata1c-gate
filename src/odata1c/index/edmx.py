"""Потоковый разбор $metadata (EDMX, OData v3) в структуры индекса (SPEC §4.1, §4.2).

Документ в ERP — десятки мегабайт, поэтому разбор идёт через iterparse с освобождением
обработанных элементов. Разбор двухпроходный по одному буферу: сначала EntityType (состав полей),
затем EntityContainer (какие из типов вообще опубликованы наборами и какие есть действия).
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import io
from collections.abc import Collection

from lxml import etree

from odata1c.index.naming import parse_entity_name

ПОЛЕ_ОПИСАНИЯ = "Description"

# Регистратор регистра, подчинённого документу: один тип регистратора — Edm.Guid с суффиксом
# _Key; несколько типов — Recorder без суффикса, составная ссылка
# (см. _пометить_ссылки_и_составные).
РЕГИСТРАТОРЫ = ("Recorder", "Recorder_Key")
НАБОР_ЗАПИСЕЙ = "RecordType"
КЛЮЧ_ТАБЛИЧНОЙ_ЧАСТИ = frozenset({"Ref_Key", "LineNumber"})

# Виды регистров — у них бывает набор записей (`_RecordType`), у остальных видов нет.
РЕГИСТР_ВИДЫ = frozenset(
    {"InformationRegister", "AccumulationRegister", "AccountingRegister", "CalculationRegister"}
)


# Версия разбора (SPEC §4.2) — повышать при любом изменении разбора, меняющем содержимое индекса
# при том же $metadata: индекс, построенный прежней версией, перестраивается без --force
# (см. reindex.py, _прежнее_состояние) даже если контрольная сумма документа не изменилась.
PARSER_VERSION = "2"

СОВЕТ_ПРИ_ОШИБКЕ_РАЗБОРА = (
    "проверьте, что по адресу базы опубликован именно интерфейс OData "
    "(URL оканчивается на /odata/standard.odata/), а не веб-страница — "
    "типичная причина именно в этом"
)


class EdmxError(Exception):
    """Не удалось разобрать $metadata — тот же протокол ошибок, что у OdataError/ConfigError
    (SPEC §5.2): код, сообщение, подсказка, чтобы вызывающая команда перехватывала её наравне
    с остальными, а не роняла процесс необработанным исключением."""

    def __init__(self, message: str, hint: str = СОВЕТ_ПРИ_ОШИБКЕ_РАЗБОРА) -> None:
        super().__init__(message)
        self.code = "odata_error"
        self.message = message
        self.hint = hint


@dataclasses.dataclass(slots=True)
class ParsedField:
    name: str
    edm_type: str
    nullable: bool
    is_key: bool = False
    is_ref: bool = False
    ref_targets: list[str] = dataclasses.field(default_factory=list)
    is_composite: bool = False


@dataclasses.dataclass(slots=True)
class ParsedAction:
    entity: str
    name: str
    params: dict[str, str]
    http_method: str
    returns: str | None = None
    side_effecting: bool = True


@dataclasses.dataclass(slots=True)
class ParsedEntity:
    name: str
    kind: str
    russian_kind: str
    base_name: str
    parent_entity: str | None
    is_tabular_part: bool
    is_records: bool
    is_virtual: bool
    virtual_kind: str | None
    key_fields: list[str]
    description_field: str | None
    has_posted: bool
    has_recorder: bool
    is_independent_register: bool
    fields: list[ParsedField]
    navigations: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(slots=True)
class ParsedMetadata:
    entities: list[ParsedEntity]
    actions: list[ParsedAction]
    edmx_sha256: str
    platform_hint: str | None = None
    unresolved_entity_sets: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    enums: dict[str, list[str]] = dataclasses.field(default_factory=dict)


def parse_edmx(data: bytes) -> ParsedMetadata:
    контрольная_сумма = hashlib.sha256(data).hexdigest()
    типы, навигации_по_типу, связи, перечисления = _разобрать_типы(data)
    наборы, сырые_действия, подсказка = _разобрать_контейнер(data)
    набор_по_типу = {имя_типа: имя_набора for имя_набора, имя_типа in наборы.items()}

    сущности: list[ParsedEntity] = []
    нераспознанные_наборы: list[str] = []
    предупреждения: list[str] = []
    for имя_набора, имя_типа in наборы.items():
        поля = типы.get(имя_типа) or типы.get(имя_набора)
        if поля is None:
            # Набор ссылается на тип, которого нет среди EntityType — испорченная ссылка
            # в описании, а не удалённая сущность. Отдаём список отдельно, чтобы обновление
            # индекса не приняло это молча за исчезновение объекта.
            нераспознанные_наборы.append(имя_набора)
            continue
        вид_набора = parse_entity_name(имя_набора)
        # Раунд правок 2 (Important, инвариант 3): для набора записей регистра поиск родителя по
        # опубликованному ПРЕФИКСУ (_родитель) не годится — короткий, но опубликованный, чужой
        # префикс (`InformationRegister_A` при настоящем основном `InformationRegister_A_B`,
        # который не опубликован) перехватывал бы родительство. Основной набор для `_RecordType`
        # — ровно имя без суффикса, тождество, а не поиск; _родитель остаётся только для
        # табличных частей ниже.
        это_набор_записей = вид_набора.kind in РЕГИСТР_ВИДЫ and имя_набора.endswith(
            "_" + НАБОР_ЗАПИСЕЙ
        )
        if это_набор_записей:
            основной = имя_набора.removesuffix("_" + НАБОР_ЗАПИСЕЙ)
            основной_опубликован = основной in наборы
            parent = основной if основной_опубликован else None
            табличная = False
            if основной_опубликован:
                # Набор опубликован, но его EntityType может не резолвиться (испорченная
                # ссылка — тот же набор уйдёт в unresolved_entity_sets). Тихая подстановка
                # чужих ключей на ключи самой записи однажды уже была причиной бага (см. ревью,
                # раунд 1): регистратор основного набора проверить нельзя — не выводим его
                # отсутствие из ключей записи, has_recorder остаётся консервативным (True).
                тип_основного = типы.get(наборы.get(основной, ""))
                has_recorder = (
                    True
                    if тип_основного is None
                    else any(ключ in РЕГИСТРАТОРЫ for ключ in тип_основного[1])
                )
            else:
                # Основной набор не опубликован вовсе — независимость не доказана, инвариант 3:
                # закрываемся при неопределённости, а не открываем физическое удаление.
                has_recorder = True
            base_name = parse_entity_name(основной).base_name
        else:
            родитель = _родитель(имя_набора, наборы)
            табличная = родитель is not None and set(поля[1]) == КЛЮЧ_ТАБЛИЧНОЙ_ЧАСТИ
            parent = родитель[0] if табличная else None
            has_recorder = any(ключ in РЕГИСТРАТОРЫ for ключ in поля[1])
            base_name = (
                parse_entity_name(родитель[0]).base_name if табличная else вид_набора.base_name
            )
        сущность = _собрать_сущность(
            имя_набора,
            поля,
            parent=parent,
            is_tabular_part=табличная,
            is_records=это_набор_записей,
            has_recorder=has_recorder,
            base_name=base_name,
        )
        сущность.navigations = _разрешить_навигации(
            сущность.fields, навигации_по_типу.get(имя_типа, []), связи, набор_по_типу
        )
        сущности.append(сущность)

    # Перечисления (SPEC §4.2, дополнение о перечислениях, задача 3 плана M1b-fix): EnumType не
    # сцеплен ни с одним набором данных — в описании нет ни EntitySet, ни связи поля с ним,
    # добавляем отдельной сущностью Enum_<Имя> с пустыми полями и ключами. Занятые имена считаем
    # от наборов — тот же набор проверяют ниже виртуальные таблицы (действия занимают имена уже
    # с учётом перечислений).
    занятые = set(наборы)
    for имя_перечисления in перечисления:
        имя_сущности = f"Enum_{имя_перечисления}"
        if имя_сущности in занятые:
            предупреждения.append(
                f"перечисление {имя_перечисления} не проиндексировано: "
                f"имя {имя_сущности} занято набором"
            )
            continue
        занятые.add(имя_сущности)
        сущности.append(
            _собрать_сущность(
                имя_сущности,
                ([], []),
                parent=None,
                is_tabular_part=False,
                is_records=False,
                has_recorder=False,
                base_name=имя_перечисления,
            )
        )

    # Действия и виртуальные таблицы (SPEC §4.2, поправка 2026-09-10): привязка — тип параметра
    # bindingParameter, атрибут EntitySet у FunctionImport в 1С не встречается.
    действия: list[ParsedAction] = []
    for сырое in сырые_действия:
        параметры = dict(сырое["params"])
        тип_привязки = _без_пространства(параметры.pop("bindingParameter", "") or "")
        набор = набор_по_типу.get(тип_привязки)
        if набор is None:
            предупреждения.append(
                f"действие {сырое['name']} не привязано: тип {тип_привязки} не опубликован"
            )
            continue
        результат = _тип_коллекции(сырое["returns"])
        if сырое["side_effecting"] or результат is None:
            действия.append(
                ParsedAction(
                    entity=набор,
                    name=сырое["name"],
                    params=параметры,
                    http_method="POST" if сырое["side_effecting"] else "GET",
                    returns=_без_пространства(сырое["returns"] or "") or None,
                    side_effecting=сырое["side_effecting"],
                )
            )
            continue
        # Виртуальная таблица: имя строится от ОСНОВНОГО набора регистра — привязка к
        # `X_RecordType` снимает суффикс (тот же принцип тождества, что у это_набор_записей выше,
        # а не поиск по опубликованному префиксу через _родитель); parent_entity остаётся
        # исходным набором привязки — по нему строится адрес вызова `<parent_entity>/<действие>`.
        вид_набора_привязки = parse_entity_name(набор)
        основной = (
            набор.removesuffix("_" + НАБОР_ЗАПИСЕЙ)
            if вид_набора_привязки.kind in РЕГИСТР_ВИДЫ and набор.endswith("_" + НАБОР_ЗАПИСЕЙ)
            else набор
        )
        имя = f"{основной}_{сырое['name']}"
        поля_результата = типы.get(результат)
        if имя in занятые or поля_результата is None:
            причина = f"имя {имя} занято" if имя in занятые else f"тип {результат} не описан"
            предупреждения.append(
                f"виртуальная таблица {набор}/{сырое['name']} не проиндексирована: {причина}"
            )
            continue
        занятые.add(имя)
        сущности.append(
            _собрать_сущность(
                имя,
                (copy.deepcopy(поля_результата[0]), []),
                parent=набор,
                is_tabular_part=False,
                is_records=False,
                has_recorder=False,
                base_name=parse_entity_name(основной).base_name,
                is_virtual=True,
                virtual_kind=сырое["name"],
            )
        )
        действия.append(
            ParsedAction(
                entity=имя,
                name=сырое["name"],
                params=параметры,
                http_method="GET",
                returns=результат,
                side_effecting=False,
            )
        )

    return ParsedMetadata(
        entities=сущности,
        actions=действия,
        edmx_sha256=контрольная_сумма,
        platform_hint=подсказка,
        unresolved_entity_sets=нераспознанные_наборы,
        warnings=предупреждения,
        enums=перечисления,
    )


# Узлы схемы, разбираемые в первом проходе (_разобрать_типы) и освобождаемые сразу после разбора:
# EntityType и ComplexType — состав полей (у ComplexType своих ключей и навигаций не бывает),
# Association — связи для последующего разрешения целей навигаций, EnumType — перечисления
# (задача 3 плана M1b-fix): не сцеплены ни с одним набором, разбираются тем же проходом.
_ОСВОБОЖДАЕМЫЕ_УЗЛЫ_СХЕМЫ = frozenset({"EntityType", "ComplexType", "Association", "EnumType"})


def _разобрать_типы(
    data: bytes,
) -> tuple[
    dict[str, tuple[list[ParsedField], list[str]]],
    dict[str, list[tuple[str, str, str]]],
    dict[str, dict[str, str]],
    dict[str, list[str]],
]:
    """Типы, навигации, связи и перечисления — один проход по документу (SPEC §4.2, дополнение
    о навигациях и о перечислениях).

    Возвращает: имя типа → (поля, ключевые поля) — и у `EntityType`, и у `ComplexType` (строки
    виртуальных таблиц и наборов записей); имя `EntityType` → список навигаций
    (имя свойства, имя `Relationship` без пространства имён, `ToRole`); имя `Association` →
    {Role: имя типа конца без пространства имён}; имя `EnumType` → список имён `Member`.
    Коллизия имён между `EntityType` и `ComplexType` невозможна — одно пространство имён схемы.
    """
    типы: dict[str, tuple[list[ParsedField], list[str]]] = {}
    навигации_по_типу: dict[str, list[tuple[str, str, str]]] = {}
    связи: dict[str, dict[str, str]] = {}
    перечисления: dict[str, list[str]] = {}
    try:
        поток = etree.iterparse(io.BytesIO(data), events=("end",), recover=False, huge_tree=True)
        for _, элемент in поток:
            имя_тега = etree.QName(элемент).localname
            if имя_тега in ("EntityType", "ComplexType"):
                имя = элемент.get("Name")
                ключи = [
                    ссылка.get("Name")
                    for ссылка in элемент.iter()
                    if etree.QName(ссылка).localname == "PropertyRef"
                ]
                поля = [
                    ParsedField(
                        name=свойство.get("Name"),
                        edm_type=свойство.get("Type", ""),
                        nullable=свойство.get("Nullable", "true") == "true",
                        is_key=свойство.get("Name") in ключи,
                    )
                    for свойство in элемент.iter()
                    if etree.QName(свойство).localname == "Property"
                ]
                _пометить_ссылки_и_составные(поля)
                типы[имя] = (поля, ключи)
                if имя_тега == "EntityType":
                    навигации_по_типу[имя] = [
                        (
                            навигация.get("Name", ""),
                            _без_пространства(навигация.get("Relationship", "")),
                            навигация.get("ToRole", ""),
                        )
                        for навигация in элемент.iter()
                        if etree.QName(навигация).localname == "NavigationProperty"
                    ]
            elif имя_тега == "Association":
                связи[элемент.get("Name", "")] = {
                    конец.get("Role", ""): _без_пространства(конец.get("Type", ""))
                    for конец in элемент.iter()
                    if etree.QName(конец).localname == "End"
                }
            elif имя_тега == "EnumType":
                перечисления[элемент.get("Name", "")] = [
                    член.get("Name")
                    for член in элемент.iter()
                    if etree.QName(член).localname == "Member"
                ]
            if имя_тега in _ОСВОБОЖДАЕМЫЕ_УЗЛЫ_СХЕМЫ:
                элемент.clear()
                while элемент.getprevious() is not None:
                    del элемент.getparent()[0]
    except etree.XMLSyntaxError as exc:
        raise EdmxError(f"не удалось разобрать $metadata: {exc}") from exc
    return типы, навигации_по_типу, связи, перечисления


def _разрешить_навигации(
    поля: list[ParsedField],
    навигации: list[tuple[str, str, str]],
    связи: dict[str, dict[str, str]],
    набор_по_типу: dict[str, str],
) -> dict[str, str]:
    """Имя навигации → набор-цель; попутно проставляет `ref_targets` парному ссылочному полю.

    Связь, чья цель не опубликована набором, или `Association`, которого нет среди собранных
    связей (урезанная публикация — SPEC §4.2, дополнение о навигациях), пропускается молча.
    """
    имена_полей = {поле.name: поле for поле in поля}
    итог: dict[str, str] = {}
    for имя, relationship, to_role in навигации:
        концы = связи.get(relationship)
        if концы is None:
            continue
        тип_цели = концы.get(to_role)
        if тип_цели is None:
            continue
        цель = набор_по_типу.get(тип_цели)
        if цель is None:
            continue
        итог[имя] = цель
        for кандидат in (f"{имя}_Key", имя):
            поле = имена_полей.get(кандидат)
            if поле is not None:
                поле.ref_targets = [цель]
                break
    return итог


def _пометить_ссылки_и_составные(поля: list[ParsedField]) -> None:
    """Ссылочные и составные поля (SPEC §4.2, поправка 2026-09-10; §9).

    `Edm.Guid` с суффиксом `_Key` — ссылка одного типа. Составное поле опознаётся по парному
    `<база>_Type` (суффикс `_Key` при сравнении отбрасывается): у ссылки составного типа в 1С тип
    `Edm.String` — значение приходит строкой GUID, в `_Type` имя набора; для примитивного типа
    в поле само значение (проба P4). Такое поле помечается ссылкой-кандидатом; решение по
    конкретному значению принимает гейт.
    """
    имена = {поле.name for поле in поля}
    for поле in поля:
        if поле.name.endswith("_Type"):
            continue
        базовое_имя = поле.name.removesuffix("_Key")
        составное = f"{базовое_имя}_Type" in имена
        if поле.edm_type == "Edm.Guid":
            поле.is_ref = поле.name.endswith("_Key") or составное
            поле.is_composite = составное
        elif поле.edm_type == "Edm.String" and составное:
            поле.is_ref = True
            поле.is_composite = True


# Узлы, которые ко времени завершения полностью разобраны в этом проходе и больше не нужны:
# EntityType здесь только читается заново по имени (состав полей уже взят первым проходом),
# EntitySet и FunctionImport уже дали всё нужное. Родителей (EntityContainer, Schema,
# DataServices, Edmx) не трогаем — они ещё разбираются, пока документ не дочитан до конца.
_ОСВОБОЖДАЕМЫЕ_УЗЛЫ_КОНТЕЙНЕРА = frozenset({"EntityType", "EntitySet", "FunctionImport"})


def _разобрать_контейнер(data: bytes) -> tuple[dict[str, str], list[dict], str | None]:
    """Наборы (имя набора → имя типа без пространства имён), сырые данные `FunctionImport` и
    подсказка платформы. Привязку действия к набору выполняет `parse_edmx`, когда уже известны
    все наборы (SPEC §4.2, поправка 2026-09-10: привязка — тип параметра `bindingParameter`, а не
    атрибут `EntitySet`, которого у действий 1С нет)."""
    наборы: dict[str, str] = {}
    сырые_действия: list[dict] = []
    подсказка: str | None = None
    try:
        поток = etree.iterparse(io.BytesIO(data), events=("end",), recover=False, huge_tree=True)
        for _, элемент in поток:
            имя_тега = etree.QName(элемент).localname
            if имя_тега == "EntitySet":
                наборы[элемент.get("Name")] = _без_пространства(элемент.get("EntityType", ""))
            elif имя_тега == "FunctionImport":
                сырые_действия.append(_сырое_действие(элемент))
            elif имя_тега == "DataServices":
                подсказка = элемент.get(
                    "{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}"
                    "DataServiceVersion"
                )
            if имя_тега in _ОСВОБОЖДАЕМЫЕ_УЗЛЫ_КОНТЕЙНЕРА:
                элемент.clear()
                while элемент.getprevious() is not None:
                    del элемент.getparent()[0]
    except etree.XMLSyntaxError as exc:
        raise EdmxError(f"не удалось разобрать $metadata: {exc}") from exc
    return наборы, сырые_действия, подсказка


def _сырое_действие(элемент) -> dict:
    """Сырые данные `FunctionImport` до привязки: имя, параметры (с `bindingParameter`,
    он попадёт в фильтр уже в `parse_edmx`), признак побочного эффекта, тип результата.

    Тип параметра — как в описании, без обработки: примитивные типы (`Edm.Boolean`, `Edm.String`)
    хранятся с пространством имён `Edm`, как и `ParsedField.edm_type` в остальном модуле.
    Пространство имён снимается только с `bindingParameter` — и только на время поиска набора
    в `parse_edmx`, в выходные `params` действия он не попадает вовсе."""
    параметры = [
        (параметр.get("Name"), параметр.get("Type", ""))
        for параметр in элемент.iter()
        if etree.QName(параметр).localname == "Parameter"
    ]
    return {
        "name": элемент.get("Name", ""),
        "side_effecting": элемент.get("IsSideEffecting", "true") != "false",
        "returns": элемент.get("ReturnType"),
        "params": параметры,
    }


def _тип_коллекции(значение: str | None) -> str | None:
    """`Collection(StandardODATA.X)` → `X` без пространства имён; не коллекция → `None`."""
    if not значение or not значение.startswith("Collection(") or not значение.endswith(")"):
        return None
    return _без_пространства(значение[len("Collection(") : -1])


def _без_пространства(значение: str) -> str:
    return значение.rsplit(".", 1)[-1] if значение else ""


def _родитель(имя: str, наборы: Collection[str]) -> tuple[str, str] | None:
    """Самый длинный опубликованный набор P, для которого имя == P + "_" + хвост.

    Поиск справа налево: `Catalog_A_B_C` сначала проверяет `Catalog_A_B`, затем `Catalog_A`.
    Имя объекта с подчёркиванием (`InformationRegister_пр_ОчередьДействий`) родителя не находит:
    набора `InformationRegister_пр` нет.
    """
    позиция = len(имя)
    while (позиция := имя.rfind("_", 0, позиция)) > 0:
        кандидат = имя[:позиция]
        if кандидат in наборы:
            хвост = имя[позиция + 1 :]
            return (кандидат, хвост) if хвост else None
    return None


def _собрать_сущность(
    имя: str,
    поля_и_ключи: tuple[list[ParsedField], list[str]],
    *,
    parent: str | None,
    is_tabular_part: bool,
    is_records: bool,
    has_recorder: bool,
    base_name: str,
    is_virtual: bool = False,
    virtual_kind: str | None = None,
) -> ParsedEntity:
    поля, ключи = поля_и_ключи
    вид = parse_entity_name(имя)
    имена_полей = {поле.name for поле in поля}
    return ParsedEntity(
        name=имя,
        kind=вид.kind,
        russian_kind=вид.russian_kind,
        base_name=base_name,
        parent_entity=parent,
        is_tabular_part=is_tabular_part,
        is_records=is_records,
        is_virtual=is_virtual,
        virtual_kind=virtual_kind,
        key_fields=list(ключи),
        description_field=ПОЛЕ_ОПИСАНИЯ if ПОЛЕ_ОПИСАНИЯ in имена_полей else None,
        has_posted="Posted" in имена_полей,
        has_recorder=has_recorder,
        # Независимость — свойство регистра, а не набора: у набора записей подчинённого регистра
        # регистратора в ключе может не быть (СтоимостьТоваров_RecordType), признак берётся
        # у основного набора через has_recorder. Инвариант 3: ложная независимость открывает
        # физическое удаление подчинённому регистру.
        is_independent_register=вид.kind == "InformationRegister"
        and not is_virtual
        and not has_recorder,
        fields=поля,
    )
