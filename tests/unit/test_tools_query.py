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


def test_top_булево_запрещено(индекс_ut, лимиты):
    # bool — подкласс int в Python; без явной проверки top=True тихо ушёл бы в 1С как $top=True.
    with pytest.raises(QueryError) as ошибка:
        build_query(
            индекс_ut.describe("Catalog_Валюты"),
            describe=индекс_ut.describe,
            limits=лимиты,
            virtual_timeout_s=180,
            top=True,
        )
    assert ошибка.value.code == "params_invalid"


def test_skip_строка_запрещена(индекс_ut, лимиты):
    with pytest.raises(QueryError) as ошибка:
        build_query(
            индекс_ut.describe("Catalog_Валюты"),
            describe=индекс_ut.describe,
            limits=лимиты,
            virtual_timeout_s=180,
            skip="5",
        )
    assert ошибка.value.code == "params_invalid"


def test_top_ноль_допустим(индекс_ut, лимиты):
    # Проба P4: $top=0 отвечает 200 с пустым value — не то же самое, что «top не передан».
    спец = build_query(
        индекс_ut.describe("Catalog_Валюты"),
        describe=индекс_ut.describe,
        limits=лимиты,
        virtual_timeout_s=180,
        top=0,
    )
    assert спец.params["$top"] == "0"
    assert спец.top == 0


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


# Раунд правок 2 (факты живой базы 1С за IIS 10, 2026-09-10): значения, попадающие в путь
# запроса (Condition/Dimensions виртуальной таблицы, литералы ключа), не переносят `?`, `+`,
# `\`, управляющие символы — даже закодированными процентами (IIS искажает или отклоняет их
# раньше, чем запрос доходит до 1С). Четыре класса символов проверены и для Condition, и для
# ключа.


@pytest.mark.parametrize(
    "condition",
    [
        pytest.param("Валюта_Key eq guid'00000000?0000'", id="вопрос"),
        pytest.param("Валюта_Key eq guid'00000000+0000'", id="плюс"),
        pytest.param("Валюта_Key eq guid'00000000\\0000'", id="слэш"),
        pytest.param("Валюта_Key eq guid'00000000\n0000'", id="управляющий-перевод-строки"),
        pytest.param("Валюта_Key eq guid'00000000\x7f0000'", id="управляющий-del"),
    ],
)
def test_condition_с_запрещённым_символом(индекс_ut, лимиты, condition):
    остатки = индекс_ut.describe("AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance")
    with pytest.raises(QueryError) as ошибка:
        build_query(
            остатки,
            describe=индекс_ut.describe,
            limits=лимиты,
            virtual_timeout_s=180,
            params={"Period": "2026-09-01", "Condition": condition},
        )
    assert ошибка.value.code == "params_invalid"
    assert "IIS" in ошибка.value.hint


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


@pytest.mark.parametrize(
    "recorder",
    [
        pytest.param("a?b", id="вопрос"),
        pytest.param("a+b", id="плюс"),
        pytest.param("a\\b", id="слэш"),
        pytest.param("a\nb", id="управляющий-перевод-строки"),
        pytest.param("a\x7fb", id="управляющий-del"),
    ],
)
def test_ключ_с_запрещённым_символом(индекс_ut, лимиты, recorder):
    # Recorder регистра — Edm.String (не Edm.Guid): формат odata_literal его не отклонит, значит
    # символ должен быть пойман отдельной проверкой раунда правок 2, а не GUID-регэкспом.
    with pytest.raises(QueryError) as ошибка:
        build_get(
            индекс_ut.describe("AccumulationRegister_РасчетыСКлиентамиПланОплат"),
            {"Recorder": recorder, "Recorder_Type": "StandardODATA.Document_Y"},
            describe=индекс_ut.describe,
            limits=лимиты,
        )
    assert ошибка.value.code == "params_invalid"
    assert "IIS" in ошибка.value.hint


def test_литералы():
    assert odata_literal("Edm.String", "О'Брайен") == "'О''Брайен'"
    assert odata_literal("Edm.DateTime", "2026-09-01") == "datetime'2026-09-01T00:00:00'"
    assert odata_literal("Edm.Boolean", True) == "true"
    with pytest.raises(QueryError):
        odata_literal("Edm.Guid", "не-guid")


ССЫЛКА = "0c4320aa-1b2c-11ee-8d4f-00155d000001"


@pytest.mark.parametrize(
    "ключ",
    [
        pytest.param(ССЫЛКА, id="голый"),
        pytest.param(f"guid'{ССЫЛКА}'", id="синтаксис-OData"),
        pytest.param(f"GUID'{ССЫЛКА}'", id="синтаксис-OData-заглавными"),
    ],
)
def test_ключ_guid_в_обоих_написаниях(индекс_ut, лимиты, ключ):
    """Находка П5: модели пишут ключ в синтаксисе OData — `guid'…'` — и получали отказ «не похоже
    на GUID» с пустой подсказкой. Обёртка снимается; литерал в пути один, не двойной."""
    спец = build_get(
        индекс_ut.describe("Catalog_Контрагенты"), ключ, describe=индекс_ut.describe, limits=лимиты
    )
    assert спец.path == f"Catalog_Контрагенты(guid'{ССЫЛКА}')"


def test_составной_ключ_в_синтаксисе_OData(индекс_ut, лимиты):
    """Обёртки снимаются в `odata_literal`, поэтому части составного ключа получают то же даром:
    `guid'…'` у поля ссылки и `datetime'…'` у периода."""
    спец = build_get(
        индекс_ut.describe("InformationRegister_КурсыВалют"),
        {"Period": "datetime'2026-01-01T00:00:00'", "Валюта_Key": f"guid'{ССЫЛКА}'"},
        describe=индекс_ut.describe,
        limits=лимиты,
    )
    assert спец.path == (
        f"InformationRegister_КурсыВалют(Period=datetime'2026-01-01T00:00:00',"
        f"Валюта_Key=guid'{ССЫЛКА}')"
    )


@pytest.mark.parametrize("ключ", ["не-гуид", "guid'не-гуид'", f"guid'{ССЫЛКА}", f"{ССЫЛКА}'"])
def test_нераспознанный_ключ_подсказывает_форму(индекс_ut, лимиты, ключ):
    """Отказ по-прежнему не повторяет значение (Ruling 20, пункт 2: здесь оно уже раскрыто), но
    подсказка больше не пустая — показывает, чего ждут."""
    with pytest.raises(QueryError) as ошибка:
        build_get(
            индекс_ut.describe("Catalog_Контрагенты"),
            ключ,
            describe=индекс_ut.describe,
            limits=лимиты,
        )
    assert ошибка.value.code == "params_invalid"
    assert "guid'" in ошибка.value.hint and "xxxxxxxx-" in ошибка.value.hint
    assert ключ not in ошибка.value.message and ключ not in ошибка.value.hint


# --- Находка П7: `get` с `expand` — 1С не раскрывает связи у одиночной сущности -----------------


def test_get_с_expand_идёт_отбором_по_ключу(индекс_ut, лимиты):
    """1С: «Опция $expand не поддерживается при запросе одиночных сущностей». Для сущности с
    простым ключом `get` с `expand` выполняется выборкой с отбором по `Ref_Key`, где 1С `$expand`
    поддерживает (проверено на живой базе)."""
    спец = build_get(
        индекс_ut.describe("Catalog_Контрагенты"),
        f"guid'{ССЫЛКА}'",
        describe=индекс_ut.describe,
        limits=лимиты,
        select=["Ref_Key", "ИНН"],
        expand=["ГоловнойКонтрагент"],
    )

    assert спец.path == "Catalog_Контрагенты"
    assert спец.params["$filter"] == f"Ref_Key eq guid'{ССЫЛКА}'"
    assert спец.params["$expand"] == "ГоловнойКонтрагент"
    assert спец.params["$top"] == "1"
    assert "ГоловнойКонтрагент/Ref_Key" in спец.params["$select"]


def test_get_без_expand_остаётся_обращением_по_ключу(индекс_ut, лимиты):
    спец = build_get(
        индекс_ut.describe("Catalog_Контрагенты"),
        ССЫЛКА,
        describe=индекс_ut.describe,
        limits=лимиты,
    )
    assert спец.path == f"Catalog_Контрагенты(guid'{ССЫЛКА}')"
    assert "$filter" not in спец.params


def test_get_с_expand_строки_табличной_части_отклоняется_с_подсказкой(индекс_ut, лимиты):
    """Строка табличной части: `$expand` по пути ключа 1С не выполняет (501), а отбор по полям
    её ключа — тоже (500 «Операция не разрешена в предложении ГДЕ»; живая проба). Выполнить
    нечем — отказ до обращения к 1С, с подсказкой, как получить связанный объект."""
    with pytest.raises(QueryError) as ошибка:
        build_get(
            индекс_ut.describe("Document_РеализацияТоваровУслуг_Товары"),
            {"Ref_Key": ССЫЛКА, "LineNumber": 1},
            describe=индекс_ut.describe,
            limits=лимиты,
            expand=["Серия"],
        )
    assert ошибка.value.code == "params_invalid"
    assert "_Key" in ошибка.value.hint


def test_get_с_expand_записи_регистра_остаётся_обращением_по_ключу(индекс_ut, лимиты):
    """Составной ключ записи регистра: отбор по его полям 1С отклоняет (живая проба), а `$expand`
    по пути ключа у записи регистра выполняет (200). Путь не меняется."""
    спец = build_get(
        индекс_ut.describe("AccumulationRegister_РасчетыСКлиентамиПланОплат_RecordType"),
        {"Recorder": ССЫЛКА, "LineNumber": 1, "Recorder_Type": "StandardODATA.Document_Y"},
        describe=индекс_ut.describe,
        limits=лимиты,
        expand=["Валюта"],
    )
    assert спец.path.startswith("AccumulationRegister_РасчетыСКлиентамиПланОплат_RecordType(")
    assert спец.params["$expand"] == "Валюта"
    assert "$filter" not in спец.params


def test_orderby_fields():
    assert orderby_fields("Дата desc, Контрагент/Description asc") == [
        "Дата",
        "Контрагент/Description",
    ]
