"""Политика базы: приоритет разделов, скрытые сущности, слияние секции auto (SPEC §6.9)."""

import textwrap

import pytest

from odata1c.gate.policy import load_policy, merge_auto

ПОЛИТИКА = textwrap.dedent("""
    version: 2
    scan_free_text: true
    defaults:
      corr: keep
      bic: keep
      addr: { mask_for: [Catalog_ФизическиеЛица] }
    entities:
      Catalog_ФизическиеЛица: { hide: true }
    names_for: [Catalog_Контрагенты, Catalog_Организации]
    fields:
      Catalog_Контрагенты.ИНН: inn
      Catalog_Контрагенты.КодПоОКПО: keep
      Document_ПлатежноеПоручение.НазначениеПлатежа: scan
    custom:
      driver_license:
        fields: [ВодительскоеУдостоверение]
        regex: '\\d{2}\\s?\\d{2}\\s?\\d{6}'
    auto:
      Catalog_Контрагенты.КПП: kpp
      Catalog_Контрагенты.ИНН: keep
      Catalog_БанковскиеСчета.НомерСчета: acc
""")


@pytest.fixture
def политика(tmp_path):
    путь = tmp_path / "policy.yaml"
    путь.write_text(ПОЛИТИКА, encoding="utf-8")
    return load_policy(путь)


def test_раздел_fields_главнее_auto(политика):
    assert политика.sensitivity_of("Catalog_Контрагенты", "ИНН") == "inn"


def test_auto_работает_когда_нет_ручного(политика):
    assert политика.sensitivity_of("Catalog_БанковскиеСчета", "НомерСчета") == "acc"


def test_keep_отключает_защиту_поля(политика):
    assert политика.sensitivity_of("Catalog_Контрагенты", "КодПоОКПО") == "keep"


def test_defaults_переводят_класс_в_keep(политика):
    """corr и bic по умолчанию открыты (SPEC §6.4)."""
    assert политика.sensitivity_of("Catalog_БанковскиеСчета", "КоррСчет") is None
    # поле не классифицировано ни ручным разделом, ни auto → защиты нет


def test_скрытая_сущность(политика):
    assert политика.is_hidden("Catalog_ФизическиеЛица") is True
    assert политика.is_hidden("Catalog_Контрагенты") is False


def test_список_названий_переопределён(политика):
    assert политика.names_for() == {"Catalog_Контрагенты", "Catalog_Организации"}


def test_свой_класс(политика):
    свои = политика.custom_patterns()
    assert "custom:driver_license" in свои
    assert свои["custom:driver_license"].search("45 08 123456")


def test_неизвестное_поле_без_класса(политика):
    assert политика.sensitivity_of("Catalog_Номенклатура", "Артикул") is None


def test_слияние_auto_не_трогает_ручные_разделы():
    существующая = {
        "version": 2,
        "fields": {"Catalog_Контрагенты.ИНН": "inn"},
        "auto": {"Catalog_Контрагенты.КПП": "kpp"},
    }
    новая = merge_auto(существующая, {"Catalog_Контрагенты.ОГРН": "ogrn"})

    assert новая["fields"] == {"Catalog_Контрагенты.ИНН": "inn"}
    assert новая["auto"] == {"Catalog_Контрагенты.ОГРН": "ogrn"}


def test_политика_без_файла_даёт_умолчания(tmp_path):
    политика = load_policy(tmp_path / "нет.yaml")
    assert политика.scan_free_text is True
    assert политика.is_hidden("Catalog_Контрагенты") is False
    assert политика.sensitivity_of("Catalog_Контрагенты", "ИНН") is None
