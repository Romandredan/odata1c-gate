"""Потоковый разбор $metadata (EDMX, OData v3) в структуры индекса (SPEC §4.1, §4.2).

Документ в ERP — десятки мегабайт, поэтому разбор идёт через iterparse с освобождением
обработанных элементов. Разбор двухпроходный по одному буферу: сначала EntityType (состав полей),
затем EntityContainer (какие из типов вообще опубликованы наборами и какие есть действия).
"""

from __future__ import annotations

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


@dataclasses.dataclass(slots=True)
class ParsedMetadata:
    entities: list[ParsedEntity]
    actions: list[ParsedAction]
    edmx_sha256: str
    platform_hint: str | None = None
    unresolved_entity_sets: list[str] = dataclasses.field(default_factory=list)


def parse_edmx(data: bytes) -> ParsedMetadata:
    контрольная_сумма = hashlib.sha256(data).hexdigest()
    типы = _разобрать_типы(data)
    наборы, действия, подсказка = _разобрать_контейнер(data)

    сущности: list[ParsedEntity] = []
    нераспознанные_наборы: list[str] = []
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
        сущности.append(
            _собрать_сущность(
                имя_набора,
                поля,
                parent=parent,
                is_tabular_part=табличная,
                is_records=это_набор_записей,
                has_recorder=has_recorder,
                base_name=base_name,
            )
        )
    return ParsedMetadata(
        entities=сущности,
        actions=действия,
        edmx_sha256=контрольная_сумма,
        platform_hint=подсказка,
        unresolved_entity_sets=нераспознанные_наборы,
    )


def _разобрать_типы(data: bytes) -> dict[str, tuple[list[ParsedField], list[str]]]:
    """Имя типа → (поля, ключевые поля). Ходим по документу один раз."""
    типы: dict[str, tuple[list[ParsedField], list[str]]] = {}
    try:
        поток = etree.iterparse(io.BytesIO(data), events=("end",), recover=False, huge_tree=True)
        for _, элемент in поток:
            if etree.QName(элемент).localname != "EntityType":
                continue
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
            элемент.clear()
            while элемент.getprevious() is not None:
                del элемент.getparent()[0]
    except etree.XMLSyntaxError as exc:
        raise EdmxError(f"не удалось разобрать $metadata: {exc}") from exc
    return типы


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


def _разобрать_контейнер(data: bytes) -> tuple[dict[str, str], list[ParsedAction], str | None]:
    наборы: dict[str, str] = {}
    действия: list[ParsedAction] = []
    подсказка: str | None = None
    try:
        поток = etree.iterparse(io.BytesIO(data), events=("end",), recover=False, huge_tree=True)
        for _, элемент in поток:
            имя_тега = etree.QName(элемент).localname
            if имя_тега == "EntitySet":
                наборы[элемент.get("Name")] = _без_пространства(элемент.get("EntityType", ""))
            elif имя_тега == "FunctionImport":
                действия.append(_разобрать_действие(элемент))
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
    return наборы, действия, подсказка


def _разобрать_действие(элемент) -> ParsedAction:
    параметры = {
        параметр.get("Name"): параметр.get("Type", "")
        for параметр in элемент.iter()
        if etree.QName(параметр).localname == "Parameter"
    }
    метод = элемент.get(
        "{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}HttpMethod"
    )
    return ParsedAction(
        entity=элемент.get("EntitySet", ""),
        name=элемент.get("Name", ""),
        params=параметры,
        http_method=(метод or "POST").upper(),
        returns=_без_пространства(элемент.get("ReturnType", "")) or None,
    )


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
