"""Разбор имени сущности и нормализация для поиска (SPEC §4.2, §4.4)."""

import pytest

from odata1c.index.naming import normalize, parse_entity_name, stems


def test_справочник():
    имя = parse_entity_name("Catalog_Контрагенты")
    assert имя.kind == "Catalog"
    assert имя.russian_kind == "Справочник"
    assert имя.base_name == "Контрагенты"


def test_неизвестный_префикс_не_ломает_разбор():
    имя = parse_entity_name("СовершенноНовыйВид_Объект")
    assert имя.kind == "СовершенноНовыйВид"
    assert имя.russian_kind == "СовершенноНовыйВид"


def test_имя_без_подчёркивания():
    имя = parse_entity_name("Catalog")
    assert имя.kind == "Catalog"
    assert имя.base_name == "Catalog"


def test_подчёркивание_в_имени_объекта_остаётся_в_имени():
    имя = parse_entity_name("InformationRegister_пр_ОчередьДействий")
    assert имя.kind == "InformationRegister"
    assert имя.base_name == "пр_ОчередьДействий"


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
