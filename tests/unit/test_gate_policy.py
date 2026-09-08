"""Политика базы: приоритет разделов, скрытые сущности, слияние секции auto (SPEC §6.9)."""

import textwrap

import pytest
import yaml

from odata1c.gate.policy import PolicyError, generate_policy, load_policy, merge_auto
from odata1c.index.edmx import parse_edmx
from odata1c.index.repository import IndexRepository

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
      Catalog_Организации.ВодительскоеУдостоверение: keep
    custom:
      driver_license:
        fields: [ВодительскоеУдостоверение]
        regex: '\\d{2}\\s?\\d{2}\\s?\\d{6}'
    auto:
      Catalog_Контрагенты.КПП: kpp
      Catalog_Контрагенты.ИНН: keep
      Catalog_Контрагенты.КодПоОКПО: inn
      Catalog_БанковскиеСчета.НомерСчета: acc
      Catalog_БанковскиеСчета.КоррСчет: corr
      Catalog_Сотрудники.ВодительскоеУдостоверение: doc
""")

# Минимальный $metadata с двумя сущностями, у каждой строковое поле «Адрес»: физлицо (защищаем)
# и склад (не защищаем — адрес склада не персональные данные, SPEC §6.9). Нужен для теста, что
# defaults.addr в generate_policy собирается верно, а не подставлен вручную в YAML теста.
_EDMX_АДРЕСА = """<?xml version="1.0" encoding="UTF-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
  <edmx:DataServices m:DataServiceVersion="3.0"
                     xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <Schema Namespace="StandardODATA" xmlns="http://schemas.microsoft.com/ado/2009/11/edm">
      <EntityType Name="Catalog_ФизическиеЛица">
        <Key><PropertyRef Name="Ref_Key"/></Key>
        <Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
        <Property Name="Адрес" Type="Edm.String" Nullable="true"/>
      </EntityType>
      <EntityType Name="Catalog_Склады">
        <Key><PropertyRef Name="Ref_Key"/></Key>
        <Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
        <Property Name="Адрес" Type="Edm.String" Nullable="true"/>
      </EntityType>
      <EntityContainer Name="StandardODATA" m:IsDefaultEntityContainer="true">
        <EntitySet Name="Catalog_ФизическиеЛица" EntityType="StandardODATA.Catalog_ФизическиеЛица"/>
        <EntitySet Name="Catalog_Склады" EntityType="StandardODATA.Catalog_Склады"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>""".encode()


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
    """Поле одновременно в auto (защищено классом inn) и в fields (переведено в keep) —
    настоящий конфликт источников, побеждает ручной раздел."""
    assert политика.sensitivity_of("Catalog_Контрагенты", "КодПоОКПО") == "keep"


def test_defaults_переводят_класс_в_keep(политика):
    """corr и bic по умолчанию открыты (SPEC §6.4): поле реально классифицировано автоматикой
    как corr, но умолчание сводит результат к keep."""
    assert политика.sensitivity_of("Catalog_БанковскиеСчета", "КоррСчет") == "keep"


def test_скрытая_сущность(политика):
    assert политика.is_hidden("Catalog_ФизическиеЛица") is True
    assert политика.is_hidden("Catalog_Контрагенты") is False


def test_список_названий_переопределён(политика):
    assert политика.names_for() == {"Catalog_Контрагенты", "Catalog_Организации"}


def test_свой_класс(политика):
    свои = политика.custom_patterns()
    assert "custom:driver_license" in свои
    assert свои["custom:driver_license"].search("45 08 123456")


def test_свой_класс_защищает_поле_по_имени(политика):
    """SPEC §6.4: свой класс задаётся и по имени поля (fields в разделе custom), и по regex —
    здесь проверяется первая половина, до этой правки не работавшая вовсе."""
    assert (
        политика.sensitivity_of("Catalog_ФизическиеЛица", "ВодительскоеУдостоверение")
        == "custom:driver_license"
    )


def test_ручное_поле_главнее_своего_класса(политика):
    assert политика.sensitivity_of("Catalog_Организации", "ВодительскоеУдостоверение") == "keep"


def test_свой_класс_главнее_автоматики(политика):
    """То же имя поля есть в auto с другим классом (doc) — свой класс не задет автоматикой."""
    assert (
        политика.sensitivity_of("Catalog_Сотрудники", "ВодительскоеУдостоверение")
        == "custom:driver_license"
    )


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


def test_адрес_защищается_только_у_физлиц_после_генерации(tmp_path):
    """Политика собрана обычным способом — generate_policy по реальному индексу, а не руками
    вписанный auto в YAML теста: адрес физлица защищается, адрес склада — нет (SPEC §6.9)."""
    repository = IndexRepository(tmp_path / "metadata.sqlite")
    repository.write(parse_edmx(_EDMX_АДРЕСА))
    сгенерированная = generate_policy(repository)
    repository.close()

    путь = tmp_path / "policy.yaml"
    путь.write_text(yaml.safe_dump(сгенерированная, allow_unicode=True), encoding="utf-8")
    результат = load_policy(путь)

    assert результат.sensitivity_of("Catalog_ФизическиеЛица", "Адрес") == "addr"
    assert результат.sensitivity_of("Catalog_Склады", "Адрес") == "keep"


def test_адрес_защищается_если_умолчание_отсутствует(tmp_path):
    """Направление по умолчанию безопасное: без раздела defaults.addr адрес защищается везде,
    а не открывается (лучше лишняя защита, чем открытый личный адрес)."""
    путь = tmp_path / "policy.yaml"
    путь.write_text(
        textwrap.dedent("""
            version: 2
            defaults:
              corr: keep
              bic: keep
            auto:
              Catalog_Склады.Адрес: addr
        """),
        encoding="utf-8",
    )
    результат = load_policy(путь)
    assert результат.sensitivity_of("Catalog_Склады", "Адрес") == "addr"


def test_испорченный_yaml_даёт_понятную_ошибку(tmp_path):
    путь = tmp_path / "policy.yaml"
    путь.write_text("version: 2\n  scan_free_text: true\n", encoding="utf-8")  # неверный отступ
    with pytest.raises(PolicyError) as исключение:
        load_policy(путь)
    assert исключение.value.code == "policy_invalid"
    assert исключение.value.hint


def test_раздел_неожиданного_типа_даёт_понятную_ошибку(tmp_path):
    путь = tmp_path / "policy.yaml"
    путь.write_text(
        textwrap.dedent("""
            version: 2
            fields: [Catalog_Контрагенты.ИНН]
        """),
        encoding="utf-8",
    )
    with pytest.raises(PolicyError) as исключение:
        load_policy(путь)
    assert исключение.value.code == "policy_invalid"


def test_невалидный_regex_своего_класса_даёт_понятную_ошибку(tmp_path):
    путь = tmp_path / "policy.yaml"
    путь.write_text(
        textwrap.dedent("""
            version: 2
            custom:
              driver_license:
                fields: [ВодительскоеУдостоверение]
                regex: '(unclosed'
        """),
        encoding="utf-8",
    )
    with pytest.raises(PolicyError) as исключение:
        load_policy(путь)
    assert исключение.value.code == "policy_invalid"
    assert исключение.value.hint
