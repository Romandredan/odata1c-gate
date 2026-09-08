"""Разбор имени сущности и нормализация для поиска (SPEC §4.2, §4.4)."""

import pytest

from odata1c.index.naming import normalize, parse_entity_name, stems


def test_справочник():
    имя = parse_entity_name("Catalog_Контрагенты")
    assert имя.kind == "Catalog"
    assert имя.russian_kind == "Справочник"
    assert имя.base_name == "Контрагенты"
    assert имя.parent is None
    assert имя.is_tabular_part is False
    assert имя.is_virtual is False


def test_табличная_часть_документа():
    имя = parse_entity_name("Document_РеализацияТоваровУслуг_Товары")
    assert имя.kind == "Document"
    assert имя.base_name == "РеализацияТоваровУслуг"
    assert имя.parent == "Document_РеализацияТоваровУслуг"
    assert имя.is_tabular_part is True
    assert имя.is_virtual is False


def test_виртуальная_таблица_остатков():
    имя = parse_entity_name("AccumulationRegister_ТоварыНаСкладах_Balance")
    assert имя.kind == "AccumulationRegister"
    assert имя.parent == "AccumulationRegister_ТоварыНаСкладах"
    assert имя.is_virtual is True
    assert имя.virtual_kind == "Balance"
    assert имя.is_tabular_part is False


def test_виртуальная_таблица_среза_последних():
    имя = parse_entity_name("InformationRegister_КурсыВалют_SliceLast")
    assert имя.is_virtual is True
    assert имя.virtual_kind == "SliceLast"
    assert имя.parent == "InformationRegister_КурсыВалют"


def test_составной_суффикс_остатков_и_оборотов():
    имя = parse_entity_name("AccumulationRegister_Продажи_BalanceAndTurnovers")
    assert имя.virtual_kind == "BalanceAndTurnovers"
    assert имя.parent == "AccumulationRegister_Продажи"


def test_неизвестный_префикс_не_ломает_разбор():
    имя = parse_entity_name("СовершенноНовыйВид_Объект")
    assert имя.kind == "СовершенноНовыйВид"
    assert имя.russian_kind == "СовершенноНовыйВид"
    assert имя.parent is None


def test_имя_без_подчёркивания():
    имя = parse_entity_name("Catalog")
    assert имя.kind == "Catalog"
    assert имя.base_name == "Catalog"


@pytest.mark.parametrize(
    ("исходное", "ожидаемое"),
    [
        ("Catalog_Контрагенты", "catalog контрагенты"),
        ("РеализацияТоваровУслуг", "реализация товаров услуг"),
        ("ТоварыНаСкладах", "товары на складах"),
        ("Ёмкость", "емкость"),
        ("ут-11_Розница", "ут 11 розница"),
    ],
)
def test_нормализация(исходное, ожидаемое):
    """Подчёркивания и дефисы становятся пробелами, CamelCase разбивается, ё превращается в е."""
    assert normalize(исходное) == ожидаемое


def test_основы_слов_русские_и_английские():
    основы = stems("РеализацияТоваровУслуг")
    assert "реализац" in " ".join(основы)
    assert any("товар" in основа for основа in основы)
