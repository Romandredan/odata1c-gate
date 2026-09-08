"""Потоковый разбор $metadata (EDMX, OData v3) в структуры индекса (SPEC §4.1, §4.2).

Документ в ERP — десятки мегабайт, поэтому разбор идёт через iterparse с освобождением
обработанных элементов. Разбор двухпроходный по одному буферу: сначала EntityType (состав полей),
затем EntityContainer (какие из типов вообще опубликованы наборами и какие есть действия).
"""

from __future__ import annotations

import dataclasses
import hashlib
import io

from lxml import etree

from odata1c.index.naming import parse_entity_name

ПОЛЕ_ОПИСАНИЯ = "Description"
СЛУЖЕБНЫЕ_ТИПЫ = ("Edm.Guid",)


class EdmxError(Exception):
    """Не удалось разобрать $metadata."""


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


def parse_edmx(data: bytes) -> ParsedMetadata:
    контрольная_сумма = hashlib.sha256(data).hexdigest()
    типы = _разобрать_типы(data)
    наборы, действия, подсказка = _разобрать_контейнер(data)

    сущности: list[ParsedEntity] = []
    for имя_набора, имя_типа in наборы.items():
        поля = типы.get(имя_типа) or типы.get(имя_набора)
        if поля is None:
            continue
        сущности.append(_собрать_сущность(имя_набора, поля))
    return ParsedMetadata(
        entities=сущности, actions=действия, edmx_sha256=контрольная_сумма, platform_hint=подсказка
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
    имена = {поле.name for поле in поля}
    for поле in поля:
        if поле.name.endswith("_Key") and поле.edm_type in СЛУЖЕБНЫЕ_ТИПЫ:
            поле.is_ref = True
            # Составной тип: рядом лежит парное поле *_Type (SPEC §9).
            поле.is_composite = поле.name[: -len("_Key")] + "_Type" in имена


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


def _собрать_сущность(имя: str, поля_и_ключи: tuple[list[ParsedField], list[str]]) -> ParsedEntity:
    поля, ключи = поля_и_ключи
    разобранное_имя = parse_entity_name(имя)
    имена_полей = {поле.name for поле in поля}
    has_recorder = "Recorder" in ключи
    регистр_сведений = разобранное_имя.kind == "InformationRegister"
    return ParsedEntity(
        name=имя,
        kind=разобранное_имя.kind,
        russian_kind=разобранное_имя.russian_kind,
        base_name=разобранное_имя.base_name,
        parent_entity=разобранное_имя.parent,
        is_tabular_part=разобранное_имя.is_tabular_part,
        is_virtual=разобранное_имя.is_virtual,
        virtual_kind=разобранное_имя.virtual_kind,
        key_fields=list(ключи),
        description_field=ПОЛЕ_ОПИСАНИЯ if ПОЛЕ_ОПИСАНИЯ in имена_полей else None,
        has_posted="Posted" in имена_полей,
        has_recorder=has_recorder,
        is_independent_register=регистр_сведений
        and not has_recorder
        and not разобранное_имя.is_virtual,
        fields=поля,
    )
