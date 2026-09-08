"""Прямая подмена: поля по классу, свободный текст, уровни, служебные поля (SPEC §6.2, §6.4, §5.1)."""  # noqa: E501

import pytest

from odata1c.gate.dictionary import Dictionary
from odata1c.gate.masking import Masker
from odata1c.gate.policy import load_policy

СЕКРЕТ = "секрет ровно для тестов подмены!!".encode()

ПОЛИТИКА_ТЕКСТ = """
version: 2
scan_free_text: true
auto:
  Catalog_Контрагенты.ИНН: inn
  Catalog_Контрагенты.КПП: kpp
  Catalog_Контрагенты.Description: org
  Catalog_Контрагенты.Комментарий: scan
"""


@pytest.fixture
def гейт(tmp_path):
    (tmp_path / "policy.yaml").write_text(ПОЛИТИКА_ТЕКСТ, encoding="utf-8")
    словарь = Dictionary(tmp_path / "gate.sqlite", СЕКРЕТ)

    def собрать(mode: str = "identifiers+names"):
        return Masker(словарь, load_policy(tmp_path / "policy.yaml"), mode=mode, base="ut")

    yield собрать
    словарь.close()


def test_поле_с_классом_заменяется_целиком(гейт):
    результат = гейт().mask({"ИНН": "7707083893"}, entity="Catalog_Контрагенты")
    assert результат.data["ИНН"].startswith("[[inn:")
    assert результат.masked_fields == ["ИНН"]


def test_равенство_сохраняется(гейт):
    маскировщик = гейт()
    первый = маскировщик.mask({"ИНН": "7707083893"}, entity="Catalog_Контрагенты")
    второй = маскировщик.mask({"ИНН": "7707083893"}, entity="Catalog_Контрагенты")
    assert первый.data["ИНН"] == второй.data["ИНН"]


def test_названия_заменяются_только_на_уровне_названий(гейт):
    с_названиями = гейт("identifiers+names").mask(
        {"Description": "ООО Ромашка"}, entity="Catalog_Контрагенты"
    )
    только_реквизиты = гейт("identifiers").mask(
        {"Description": "ООО Ромашка"}, entity="Catalog_Контрагенты"
    )
    assert с_названиями.data["Description"] == "[[org:1]]"
    assert только_реквизиты.data["Description"] == "ООО Ромашка"


def test_реквизит_в_поле_названия_заменяется_и_на_нижнем_уровне(гейт):
    """SPEC §6.2: на identifiers реквизиты ищутся по значению в любых строках, включая поля,
    которые на верхнем уровне заменялись бы целиком как название."""
    результат = гейт("identifiers").mask(
        {"Description": "ООО Ромашка, ИНН 7707083893"}, entity="Catalog_Контрагенты"
    )
    assert "7707083893" not in результат.data["Description"]
    assert "[[inn:" in результат.data["Description"]
    assert "ООО Ромашка" in результат.data["Description"]  # название на этом уровне открыто


def test_уровень_off_ничего_не_меняет(гейт):
    результат = гейт("off").mask(
        {"ИНН": "7707083893", "Description": "ООО Ромашка"}, entity="Catalog_Контрагенты"
    )
    assert результат.data == {"ИНН": "7707083893", "Description": "ООО Ромашка"}


def test_суммы_и_даты_остаются(гейт):
    исходное = {"СуммаДокумента": 145200.5, "Date": "2026-09-07T00:00:00", "Number": "ТД-004512"}
    результат = гейт().mask(исходное, entity="Document_РеализацияТоваровУслуг")
    assert результат.data == исходное


def test_guid_остаётся(гейт):
    исходное = {"Ref_Key": "8f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"}
    assert гейт().mask(исходное, entity="Catalog_Контрагенты").data == исходное


def test_реквизит_внутри_свободного_текста(гейт):
    результат = гейт().mask(
        {"Комментарий": "оплата, ИНН 7707083893, срочно"}, entity="Catalog_Контрагенты"
    )
    assert "7707083893" not in результат.data["Комментарий"]
    assert "[[inn:" in результат.data["Комментарий"]
    assert "оплата," in результат.data["Комментарий"]


def test_вложенные_структуры_и_табличные_части(гейт):
    исходное = {
        "Description": "ООО Ромашка",
        "Товары": [
            {"Номенклатура": "Гвозди", "Сумма": 100},
            {"Номенклатура": "Шурупы", "Сумма": 200},
        ],
        "КонтактнаяИнформация": [{"Представление": "тел. +7 495 123-45-67"}],
    }
    результат = гейт().mask(исходное, entity="Catalog_Контрагенты")
    assert результат.data["Товары"][0]["Сумма"] == 100
    assert "[[phone:" in результат.data["КонтактнаяИнформация"][0]["Представление"]


def test_реквизит_в_строковом_элементе_списка(гейт):
    """Табличная часть может прийти как список голых строк, а не объектов — реквизит внутри
    такого элемента обязан находиться так же, как в строке поля верхнего уровня."""
    результат = гейт().mask({"Теги": ["ИНН 7707083893", "прочее"]}, entity="Catalog_Контрагенты")
    assert "7707083893" not in результат.data["Теги"][0]
    assert "[[inn:" in результат.data["Теги"][0]
    assert результат.data["Теги"][1] == "прочее"


def test_служебные_поля_вырезаны(гейт):
    исходное = {
        "odata.metadata": "http://…",
        "odata.type": "StandardODATA.Catalog_Контрагенты",
        "DataVersion": "AAAAB",
        "Description": "ООО Ромашка",
    }
    результат = гейт().mask(исходное, entity="Catalog_Контрагенты")
    assert "odata.metadata" not in результат.data
    assert "odata.type" not in результат.data
    assert "DataVersion" not in результат.data


def test_хранилище_значения_вырезано(гейт):
    результат = гейт().mask(
        {"ХранилищеЗначения_Base64Data": "0J/RgNC40LLQtdGC"}, entity="Catalog_Файлы"
    )
    assert результат.data == {}


def test_пустая_строка_не_токенизируется(гейт):
    результат = гейт().mask({"ИНН": "", "Description": ""}, entity="Catalog_Контрагенты")
    assert результат.data == {"ИНН": "", "Description": ""}


def test_известное_название_заменяется_в_любой_строке(гейт):
    маскировщик = гейт()
    маскировщик.mask({"Description": "ООО Ромашка"}, entity="Catalog_Контрагенты")
    результат = маскировщик.mask(
        {"Комментарий": "звонили из ООО Ромашка вчера"}, entity="Catalog_Контрагенты"
    )
    assert "Ромашка" not in результат.data["Комментарий"]


def test_сообщение_об_ошибке_проходит_подмену(гейт):
    текст = гейт().mask_text(
        "Поле ИНН 7707083893 заполнено неверно", entity="Catalog_Контрагенты", field="message"
    )
    assert "7707083893" not in текст
