"""Построение запроса к 1С из аргументов тула и описания сущности индекса (SPEC §9, план M1d
задача 2). Фикстуры `индекс_ut` и `лимиты` — в conftest.py."""

import pytest

from odata1c.index.repository import EntityDescription
from odata1c.tools.odata_query import (
    QueryError,
    build_get,
    build_query,
    odata_literal,
    orderby_fields,
)


def test_простая_выборка(индекс_ut, лимиты):
    спец = build_query(
        индекс_ut.describe("Catalog_Контрагенты"),
        describe=индекс_ut.describe,
        limits=лимиты,
        virtual_timeout_s=180,
        select=["Ref_Key", "Description"],
        filter="DeletionMark eq false",
        top=10,
    )
    assert спец.path == "Catalog_Контрагенты"
    assert спец.params == {
        "$select": "Ref_Key,Description",
        "$filter": "DeletionMark eq false",
        "$top": "10",
    }
    assert спец.timeout_s is None


def test_top_ограничен_максимумом(индекс_ut, лимиты):
    спец = build_query(
        индекс_ut.describe("Catalog_Валюты"),
        describe=индекс_ut.describe,
        limits=лимиты,
        virtual_timeout_s=180,
        top=5000,
    )
    assert спец.params["$top"] == str(лимиты.top_max)
    assert спец.warnings


def test_отрицательный_top_запрещён(индекс_ut, лимиты):
    with pytest.raises(QueryError) as ошибка:
        build_query(
            индекс_ut.describe("Catalog_Валюты"),
            describe=индекс_ut.describe,
            limits=лимиты,
            virtual_timeout_s=180,
            top=-1,
        )
    assert ошибка.value.code == "params_invalid"


def test_отрицательный_skip_запрещён(индекс_ut, лимиты):
    with pytest.raises(QueryError) as ошибка:
        build_query(
            индекс_ut.describe("Catalog_Валюты"),
            describe=индекс_ut.describe,
            limits=лимиты,
            virtual_timeout_s=180,
            skip=-5,
        )
    assert ошибка.value.code == "params_invalid"


def test_expand_добавляет_путь_в_select(индекс_ut, лимиты):
    спец = build_query(
        индекс_ut.describe("Document_РеализацияТоваровУслуг"),
        describe=индекс_ut.describe,
        limits=лимиты,
        virtual_timeout_s=180,
        select=["Number"],
        expand=["Контрагент"],
    )
    поля = спец.params["$select"].split(",")
    assert "Контрагент/Ref_Key" in поля and "Контрагент/Description" in поля
    assert спец.params["$expand"] == "Контрагент"


def test_expand_без_select_не_добавляет_путь(индекс_ut, лимиты):
    спец = build_query(
        индекс_ut.describe("Document_РеализацияТоваровУслуг"),
        describe=индекс_ut.describe,
        limits=лимиты,
        virtual_timeout_s=180,
        expand=["Контрагент"],
    )
    assert "$select" not in спец.params
    assert спец.params["$expand"] == "Контрагент"


def test_expand_глубже_лимита(индекс_ut, лимиты):
    with pytest.raises(QueryError) as ошибка:
        build_query(
            индекс_ut.describe("Document_РеализацияТоваровУслуг"),
            describe=индекс_ut.describe,
            limits=лимиты,
            virtual_timeout_s=180,
            expand=["Контрагент/ГоловнойКонтрагент/ГоловнойКонтрагент"],
        )
    assert ошибка.value.code == "params_invalid"


def test_неизвестная_навигация(индекс_ut, лимиты):
    with pytest.raises(QueryError) as ошибка:
        build_query(
            индекс_ut.describe("Document_РеализацияТоваровУслуг"),
            describe=индекс_ut.describe,
            limits=лимиты,
            virtual_timeout_s=180,
            expand=["НетТакой"],
        )
    assert "Контрагент" in ошибка.value.hint


def test_остатки_требуют_период(индекс_ut, лимиты):
    остатки = индекс_ut.describe("AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance")
    with pytest.raises(QueryError) as ошибка:
        build_query(остатки, describe=индекс_ut.describe, limits=лимиты, virtual_timeout_s=180)
    assert ошибка.value.code == "params_invalid" and "Period" in ошибка.value.message


def test_остатки_на_дату_с_условием(индекс_ut, лимиты):
    остатки = индекс_ut.describe("AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance")
    спец = build_query(
        остатки,
        describe=индекс_ut.describe,
        limits=лимиты,
        virtual_timeout_s=180,
        params={
            "Period": "2026-09-01",
            "Condition": "Валюта_Key eq guid'00000000-0000-0000-0000-000000000000'",
        },
    )
    assert спец.path == (
        "AccumulationRegister_РасчетыСКлиентамиПланОплат/Balance("
        "Condition='Валюта_Key eq guid''00000000-0000-0000-0000-000000000000''',"
        "Period=datetime'2026-09-01T00:00:00')"
    )
    assert спец.timeout_s == 180


def test_неизвестный_параметр_виртуальной_таблицы(индекс_ut, лимиты):
    срез = индекс_ut.describe("InformationRegister_КурсыВалют_SliceLast")
    with pytest.raises(QueryError) as ошибка:
        build_query(
            срез,
            describe=индекс_ut.describe,
            limits=лимиты,
            virtual_timeout_s=180,
            params={"Периодичность": "Месяц"},
        )
    assert "Period" in ошибка.value.hint


def test_params_для_невиртуальной_сущности_запрещены(индекс_ut, лимиты):
    with pytest.raises(QueryError) as ошибка:
        build_query(
            индекс_ut.describe("Catalog_Контрагенты"),
            describe=индекс_ut.describe,
            limits=лимиты,
            virtual_timeout_s=180,
            params={"Что-то": "1"},
        )
    assert ошибка.value.code == "params_invalid"


def test_виртуальная_таблица_без_действия_в_индексе(индекс_ut, лимиты):
    # Защита от рассинхронизации индекса (не выводима из ut-real.edmx: там у виртуальной таблицы
    # всегда есть действие) — сконструирована вручную.
    сломанное_описание = EntityDescription(
        name="AccumulationRegister_Х_Balance",
        kind="AccumulationRegister",
        russian_kind="РегистрНакопления",
        parent_entity="AccumulationRegister_Х",
        is_tabular_part=False,
        is_records=False,
        is_virtual=True,
        virtual_kind="Balance",
        key_fields=[],
        description_field=None,
        fields=[],
        children=[],
        actions=[],
        members=[],
        navigations={},
        is_independent_register=False,
    )
    with pytest.raises(QueryError) as ошибка:
        build_query(
            сломанное_описание, describe=индекс_ut.describe, limits=лимиты, virtual_timeout_s=180
        )
    assert ошибка.value.code == "params_invalid"


def test_params_не_словарь_запрещён(индекс_ut, лимиты):
    with pytest.raises(QueryError) as ошибка:
        build_query(
            индекс_ut.describe("AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance"),
            describe=индекс_ut.describe,
            limits=лимиты,
            virtual_timeout_s=180,
            params=["Period", "2026-09-01"],
        )
    assert ошибка.value.code == "params_invalid"


def test_inlinecount_и_allowed_only(индекс_ut, лимиты):
    спец = build_query(
        индекс_ut.describe("Catalog_Валюты"),
        describe=индекс_ut.describe,
        limits=лимиты,
        virtual_timeout_s=180,
        inlinecount=True,
        allowed_only=True,
    )
    assert спец.params["$inlinecount"] == "allpages"
    assert спец.params["allowedOnly"] == "true"


def test_get_по_guid(индекс_ut, лимиты):
    спец = build_get(
        индекс_ut.describe("Catalog_Контрагенты"),
        "a103cb54-42ee-11ec-a7a0-f10ab59a067e",
        describe=индекс_ut.describe,
        limits=лимиты,
    )
    assert спец.path == "Catalog_Контрагенты(guid'a103cb54-42ee-11ec-a7a0-f10ab59a067e')"


def test_get_составной_ключ_неполный(индекс_ut, лимиты):
    # InformationRegister_КурсыВалют: key_fields ['Period', 'Валюта_Key'] (образец ut-real.edmx) —
    # передан только один из двух.
    with pytest.raises(QueryError) as ошибка:
        build_get(
            индекс_ut.describe("InformationRegister_КурсыВалют"),
            {"Period": "2026-01-01"},
            describe=индекс_ut.describe,
            limits=лимиты,
        )
    assert ошибка.value.code == "params_invalid"


def test_get_ключ_не_строка_и_не_словарь(индекс_ut, лимиты):
    with pytest.raises(QueryError) as ошибка:
        build_get(
            индекс_ut.describe("Catalog_Контрагенты"),
            123,
            describe=индекс_ut.describe,
            limits=лимиты,
        )
    assert ошибка.value.code == "params_invalid"


def test_get_составной_ключ_полный(индекс_ut, лимиты):
    спец = build_get(
        индекс_ut.describe("InformationRegister_КурсыВалют"),
        {
            "Period": "2026-01-01",
            "Валюта_Key": "00000000-0000-0000-0000-000000000000",
        },
        describe=индекс_ut.describe,
        limits=лимиты,
    )
    assert спец.path == (
        "InformationRegister_КурсыВалют(Period=datetime'2026-01-01T00:00:00',"
        "Валюта_Key=guid'00000000-0000-0000-0000-000000000000')"
    )


def test_литералы():
    assert odata_literal("Edm.String", "О'Брайен") == "'О''Брайен'"
    assert odata_literal("Edm.DateTime", "2026-09-01") == "datetime'2026-09-01T00:00:00'"
    assert odata_literal("Edm.Boolean", True) == "true"
    with pytest.raises(QueryError):
        odata_literal("Edm.Guid", "не-guid")


def test_orderby_fields():
    assert orderby_fields("Дата desc, Контрагент/Description asc") == [
        "Дата",
        "Контрагент/Description",
    ]
