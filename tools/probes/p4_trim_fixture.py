"""Урезать полный $metadata пробы P4 до образца для тестов (tests/fixtures/edmx/ut-real.edmx).

    uv run python tools/probes/p4_trim_fixture.py

Оставляет по представителю на каждый случай, найденный пробой: табличные части, подчинённые и
независимые регистры сведений, регистр с одним типом регистратора (Recorder_Key), регистры остатков
и оборотов с виртуальными таблицами-действиями, составные поля (*_Type), имена объектов с
подчёркиванием и цифрами, бизнес-процесс и задачу. Клиентские префиксы имён заменяются на
нейтральные: образец идёт в git.
"""

import copy
import pathlib
import re

from lxml import etree

SOURCE = pathlib.Path("tests/fixtures/edmx/probe.full.edmx")
TARGET = pathlib.Path("tests/fixtures/edmx/ut-real.edmx")
NS = "StandardODATA."

SETS = [
    "Catalog_Валюты",
    "Catalog_Контрагенты",
    "Catalog_Контрагенты_КонтактнаяИнформация",
    "Catalog_БанковскиеСчетаКонтрагентов",
    "Catalog_ФизическиеЛица",
    "Catalog_СерииНоменклатуры",
    "Catalog_СерииНоменклатуры_ДополнительныеРеквизиты",
    "Document_РеализацияТоваровУслуг",
    "Document_РеализацияТоваровУслуг_Товары",
    "Document_ВводОстатковРасчетовПоЭквайрингу",
    "Document_ВводОстатковРасчетовПоЭквайрингу_РасчетыПоЭквайрингу",
    "InformationRegister_КурсыВалют",
    "InformationRegister_ПоследнийОбменСБанками",
    "InformationRegister_ДокументыФизическихЛиц",
    "InformationRegister_СтоимостьТоваров",
    "InformationRegister_СтоимостьТоваров_RecordType",
    "InformationRegister_ЖурналУчетаСчетовФактур",
    "InformationRegister_ЖурналУчетаСчетовФактур_RecordType",
    "InformationRegister_см_ОчередьДействий",
    "InformationRegister_см_ОчередьДействий_RecordType",
    "AccumulationRegister_РасчетыСКлиентамиПланОплат",
    "AccumulationRegister_РасчетыСКлиентамиПланОплат_RecordType",
    "AccumulationRegister_ДвиженияДенежныеСредстваКонтрагент",
    "AccumulationRegister_ДвиженияДенежныеСредстваКонтрагент_RecordType",
    "AccumulationRegister_вт_ВыпускПроукцииПоСменам",
    "AccumulationRegister_вт_ВыпускПроукцииПоСменам_RecordType",
    "Constant_SbSh_АвтоматическиСоздаватьАктыРасхождений",
    "ExchangePlan_Удалить_ОбменУправлениеТорговлей_11_0_РозничнаяТорговля_1_0",
    "DocumentJournal_СогласияНаОбработкуПерсональныхДанных",
    "BusinessProcess_см_БизнесПроцессСогласованияОрдеров",
    "BusinessProcess_см_БизнесПроцессСогласованияОрдеров_РезультатыСогласования",
    "Task_см_ЗадачаСогласования",
    "Task_см_ЗадачаСогласования_ОбъектыСогласования",
    "ChartOfCharacteristicTypes_СтатьиДоходов",
]
ENUMS = ["ХозяйственныеОперации", "СтатусыТаможенныхДеклараций"]

# Клиентские префиксы: только на границе имени, чтобы не задеть «Ответ_Key» и подобные.
RENAMES = [(re.compile(r'(?<=[_."/])(?:см|вт)_'), "пр_"), (re.compile(r'(?<=[_."/])SbSh_'), "Xx_")]


def local(element) -> str:
    return etree.QName(element).localname


def main() -> None:
    root = etree.parse(str(SOURCE), etree.XMLParser(huge_tree=True, remove_blank_text=True)).getroot()
    schema = next(e for e in root.iter() if isinstance(e.tag, str) and local(e) == "Schema")
    container = next(e for e in schema if local(e) == "EntityContainer")

    sets = {e.get("Name"): e for e in container if local(e) == "EntitySet"}
    missing = [n for n in SETS if n not in sets]
    if missing:
        raise SystemExit(f"нет в $metadata: {missing}")
    kept_types = {sets[n].get("EntityType").removeprefix(NS) for n in SETS}

    imports = [fi for fi in container if local(fi) == "FunctionImport"
               and any(p.get("Name") == "bindingParameter"
                       and p.get("Type").removeprefix(NS) in kept_types for p in fi)]
    complex_needed = set()
    for fi in imports:
        returns = (fi.get("ReturnType") or "").removeprefix("Collection(").removesuffix(")")
        complex_needed.add(returns.removeprefix(NS))
    for element in schema:
        if local(element) == "EntityType" and element.get("Name") in kept_types:
            for prop in element:
                if local(prop) == "Property" and "Collection(" in prop.get("Type", ""):
                    inner = prop.get("Type").removeprefix("Collection(").removesuffix(")")
                    complex_needed.add(inner.removeprefix(NS))

    associations = {}
    for element in schema:
        if local(element) == "Association":
            ends = [end.get("Type").removeprefix(NS) for end in element if local(end) == "End"]
            if all(t in kept_types for t in ends):
                associations[element.get("Name")] = element

    new_schema = etree.Element(schema.tag, nsmap=schema.nsmap, attrib=dict(schema.attrib))
    for element in schema:
        name, tag = element.get("Name"), local(element)
        if tag == "EntityType" and name in kept_types:
            clone = copy.deepcopy(element)
            for nav in [p for p in clone if local(p) == "NavigationProperty"]:
                if nav.get("Relationship").removeprefix(NS) not in associations:
                    clone.remove(nav)
            new_schema.append(clone)
        elif (tag == "ComplexType" and name in complex_needed) or (tag == "EnumType" and name in ENUMS):
            new_schema.append(copy.deepcopy(element))
    for association in associations.values():
        new_schema.append(copy.deepcopy(association))
    new_container = etree.SubElement(new_schema, container.tag, attrib=dict(container.attrib))
    for name in SETS:
        new_container.append(copy.deepcopy(sets[name]))
    for fi in imports:
        new_container.append(copy.deepcopy(fi))

    data_services = schema.getparent()
    data_services.remove(schema)
    data_services.append(new_schema)
    text = etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True).decode()
    for pattern, replacement in RENAMES:
        text = pattern.sub(replacement, text)
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"{TARGET}: {len(text.encode()) / 1024:.0f} КБ; наборов {len(SETS)}, действий {len(imports)}, "
          f"сложных типов {len(complex_needed)}, связей {len(associations)}")


if __name__ == "__main__":
    main()
