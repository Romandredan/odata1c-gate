"""Политика базы: приоритет разделов, скрытые сущности, сборка политики из двух файлов —
`policy.yaml` владельца и `policy.auto.yaml` авторазметки (SPEC §6.9, ADR-0015)."""

import textwrap

import pytest
import yaml
from conftest import политика_из_шаблона_с_дублем_entities

from odata1c.gate.policy import (
    PolicyError,
    generate_policy,
    load_policy,
    read_auto,
    strip_auto_section,
)
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


def test_политика_из_двух_файлов_приоритет_владельца(tmp_path):
    владелец = tmp_path / "policy.yaml"
    авто = tmp_path / "policy.auto.yaml"
    владелец.write_text(
        "version: 2\nfields:\n  Catalog_Контрагенты.КодПоОКПО: keep\n"
        "defaults:\n  addr:\n    mask_for: [Catalog_Партнеры]\n",
        encoding="utf-8",
    )
    авто.write_text(
        "version: 2\ndefaults:\n  corr: keep\n  bic: keep\n"
        "  addr:\n    mask_for: [Catalog_ФизическиеЛица]\n"
        "auto:\n  Catalog_Контрагенты.КодПоОКПО: org\n  Catalog_Контрагенты.ИНН: inn\n",
        encoding="utf-8",
    )
    политика = load_policy(владелец, авто)
    assert политика.sensitivity_of("Catalog_Контрагенты", "КодПоОКПО") == "keep"
    assert политика.sensitivity_of("Catalog_Контрагенты", "ИНН") == "inn"
    assert политика.addr_masked("Catalog_Партнеры")
    assert политика.addr_masked("Catalog_ФизическиеЛица")


def test_без_файла_авторазметки_политика_владельца_работает(tmp_path):
    владелец = tmp_path / "policy.yaml"
    владелец.write_text("version: 2\nentities:\n  Catalog_X: {hide: true}\n", encoding="utf-8")
    политика = load_policy(владелец, tmp_path / "policy.auto.yaml")
    assert политика.is_hidden("Catalog_X")


def test_strip_auto_section_сохраняет_комментарии(tmp_path):
    файл = tmp_path / "policy.yaml"
    файл.write_text(
        "# шапка владельца\nversion: 2\nfields: {}   # мои правила\nauto:\n  Catalog_A.B: inn\n",
        encoding="utf-8",
    )
    assert strip_auto_section(файл) is True
    текст = файл.read_text(encoding="utf-8")
    assert "# шапка владельца" in текст and "# мои правила" in текст
    assert "auto:" not in текст and "Catalog_A.B" not in текст
    assert strip_auto_section(файл) is False


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


ПОЛИТИКА_АДРЕСОВ = textwrap.dedent("""
    version: 2
    defaults:
      addr:
        mask_for: [Catalog_ФизическиеЛица, Catalog_Пользователи]
    auto:
      Catalog_ФизическиеЛица.Адрес: addr
      Catalog_ФизическиеЛица_ПрежниеАдреса.Адрес: addr
      Catalog_Склады.Адрес: addr
      Document_ЗаказКлиента.АдресДоставки: addr
      Document_ЗаказКлиента.АдресДоставкиЗначение: addr
      Document_ЗаказКлиента.АдресДоставкиПеревозчикаЗначенияПолей: addr
      Document_ТранспортнаяНакладная.АдресПогрузки: addr
      Document_ЗаданиеНаПеревозку_Маршрут.Адрес: addr
      Catalog_ДоговорыКонтрагентов.АдресДоставки: addr
      Document_КассоваяСмена.АдресРасчетов: addr
      Catalog_Сайты.АдресСайта: addr
""")


@pytest.fixture
def политика_адресов(tmp_path):
    путь = tmp_path / "policy.yaml"
    путь.write_text(ПОЛИТИКА_АДРЕСОВ, encoding="utf-8")
    return load_policy(путь)


def test_r34_mask_for_наследуется_табличной_частью(политика_адресов):
    """Ruling 34, пункт 1: адрес физлица закрыт и в табличной части его справочника — прежде
    `mask_for` сравнивался с именем сущности точно, и `Catalog_ФизическиеЛица_*` получал `keep`."""
    assert политика_адресов.sensitivity_of("Catalog_ФизическиеЛица", "Адрес") == "addr"
    assert (
        политика_адресов.sensitivity_of("Catalog_ФизическиеЛица_ПрежниеАдреса", "Адрес") == "addr"
    )
    assert политика_адресов.addr_masked("Catalog_ФизическиеЛица_КонтактнаяИнформация")
    assert политика_адресов.addr_masked("Catalog_Пользователи_КонтактнаяИнформация")
    # Префикс — только с разделителем: другой справочник с похожим началом имени не наследует.
    assert not политика_адресов.addr_masked("Catalog_ФизическиеЛицаАрхив")
    assert политика_адресов.sensitivity_of("Catalog_Склады", "Адрес") == "keep"


@pytest.mark.parametrize(
    ("сущность", "поле"),
    [
        ("Document_ЗаказКлиента", "АдресДоставки"),
        ("Document_ЗаказКлиента", "АдресДоставкиЗначение"),
        ("Document_ЗаказКлиента", "АдресДоставкиПеревозчикаЗначенияПолей"),
        ("Document_ТранспортнаяНакладная", "АдресПогрузки"),
        ("Document_ЗаданиеНаПеревозку_Маршрут", "Адрес"),
        ("Catalog_ДоговорыКонтрагентов", "АдресДоставки"),
    ],
)
def test_r34_адрес_доставки_закрыт_всегда(политика_адресов, сущность, поле):
    """Ruling 34, пункт 3: адрес доставки — всегда, в любой сущности, без исключения по
    контрагенту: по адресу склад организации от квартиры покупателя не отличить."""
    assert политика_адресов.sensitivity_of(сущность, поле) == "addr"


@pytest.mark.parametrize(
    ("сущность", "поле"),
    [("Document_КассоваяСмена", "АдресРасчетов"), ("Catalog_Сайты", "АдресСайта")],
)
def test_r34_прочие_адреса_документов_не_закрываются_правилом_доставки(
    политика_адресов, сущность, поле
):
    """Обратный сторож: правило доставки читается по имени поля, а не «любой адрес в документе»
    — адрес расчётов кассы и адрес сайта адресом лица не являются."""
    assert политика_адресов.sensitivity_of(сущность, поле) == "keep"


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


# --- тип значения класса (находка I2 итогового ревью M2b) -------------------------------------


def test_список_вместо_класса_в_fields_даёт_policyerror(tmp_path):
    """`fields: {Catalog_X.Поле: [a, b]}` проходил разбор, а падал позже: `check_policy` и
    `Masker.mask` ищут значение во множестве классов — `TypeError: unhashable type: 'list'`, то
    есть `internal` на каждый тул базы вместо честного `policy_invalid`."""
    путь = tmp_path / "policy.yaml"
    путь.write_text(
        textwrap.dedent("""
            version: 2
            fields:
              Catalog_Контрагенты.ИНН: [inn, keep]
        """),
        encoding="utf-8",
    )
    with pytest.raises(PolicyError) as исключение:
        load_policy(путь)
    assert исключение.value.code == "policy_invalid"
    assert "fields" in str(исключение.value)
    assert исключение.value.hint


def test_сообщение_о_типе_класса_не_цитирует_значение(tmp_path):
    """Раздел, порядковый номер записи и тип — как у соседних проверок; ни ключ, ни значение
    в сообщение не попадают (инвариант 1, тот же принцип, что у нестрокового ключа)."""
    путь = tmp_path / "policy.yaml"
    путь.write_text(
        textwrap.dedent("""
            version: 2
            fields:
              Catalog_Контрагенты.КодПоОКПО: keep
              Catalog_Контрагенты.ИНН: { класс: inn }
        """),
        encoding="utf-8",
    )
    with pytest.raises(PolicyError) as исключение:
        load_policy(путь)
    сообщение = str(исключение.value)
    assert "2-й записи" in сообщение
    assert "dict" in сообщение
    assert "Catalog_Контрагенты" not in сообщение
    assert "inn" not in сообщение


def test_число_вместо_класса_в_auto_файла_владельца_даёт_policyerror(tmp_path):
    путь = tmp_path / "policy.yaml"
    путь.write_text(
        textwrap.dedent("""
            version: 2
            auto:
              Catalog_Контрагенты.ИНН: 5
        """),
        encoding="utf-8",
    )
    with pytest.raises(PolicyError) as исключение:
        load_policy(путь)
    assert исключение.value.code == "policy_invalid"
    assert "auto" in str(исключение.value)


def test_список_вместо_класса_в_авторазметке_даёт_policyerror(tmp_path):
    """Файл авторазметки пишет машина, но читается он с диска: правка рукой, обрыв записи или
    чужая версия формата не должны доходить до `Masker` нестроковым классом."""
    путь = tmp_path / "policy.auto.yaml"
    путь.write_text(
        textwrap.dedent("""
            version: 2
            defaults: {}
            auto:
              Catalog_Контрагенты.ИНН: [inn]
        """),
        encoding="utf-8",
    )
    with pytest.raises(PolicyError) as исключение:
        read_auto(путь)
    assert исключение.value.code == "policy_invalid"
    assert "auto" in str(исключение.value)


def test_нестроковый_ключ_в_авторазметке_даёт_policyerror(tmp_path):
    """Отложенный минор задачи 4: ключи `auto` тоже обязаны быть строками — иначе `.partition`
    на числе роняет `AttributeError` в `effective_rows`."""
    путь = tmp_path / "policy.auto.yaml"
    путь.write_text(
        textwrap.dedent("""
            version: 2
            defaults: {}
            auto:
              123: inn
        """),
        encoding="utf-8",
    )
    with pytest.raises(PolicyError) as исключение:
        read_auto(путь)
    assert исключение.value.code == "policy_invalid"
    assert "123" not in str(исключение.value)


def test_строковый_класс_в_авторазметке_читается_как_прежде(tmp_path):
    путь = tmp_path / "policy.auto.yaml"
    путь.write_text(
        textwrap.dedent("""
            version: 2
            defaults: {}
            auto:
              Catalog_Контрагенты.ИНН: inn
        """),
        encoding="utf-8",
    )

    assert read_auto(путь) == {"defaults": {}, "auto": {"Catalog_Контрагенты.ИНН": "inn"}}


# --- повтор раздела верхнего уровня при обратимом разборе (находка I1 итогового ревью M2b) -----


def test_strip_auto_section_на_повторе_раздела_даёт_policyerror_а_не_ruamel(tmp_path):
    """Первый реиндекс новой версии зовёт `strip_auto_section` на файле владельца. Повтор раздела
    (заглушка шаблона плюс раскомментированный пример) для `ruamel` — `DuplicateKeyError`, и
    прежде он уходил голой трассировкой: `odata1c reindex` падал, тул `odata1c_reindex` отвечал
    `internal`."""
    путь = tmp_path / "policy.yaml"
    путь.write_text(политика_из_шаблона_с_дублем_entities(), encoding="utf-8")

    with pytest.raises(PolicyError) as исключение:
        strip_auto_section(путь)

    assert исключение.value.code == "policy_invalid"
    assert исключение.value.hint
    assert "строка" in str(исключение.value)


def test_сообщение_о_повторе_при_разборе_не_цитирует_содержимое(tmp_path):
    путь = tmp_path / "policy.yaml"
    путь.write_text(политика_из_шаблона_с_дублем_entities(), encoding="utf-8")

    with pytest.raises(PolicyError) as исключение:
        strip_auto_section(путь)

    сообщение = f"{исключение.value} {исключение.value.hint}"
    assert "Catalog_ФизическиеЛица" not in сообщение
    assert "hide" not in сообщение


def test_битый_yaml_при_обратимом_разборе_тоже_policyerror(tmp_path):
    путь = tmp_path / "policy.yaml"
    путь.write_text("version: 2\nfields: [не закрытый список\n", encoding="utf-8")

    with pytest.raises(PolicyError) as исключение:
        strip_auto_section(путь)

    assert исключение.value.code == "policy_invalid"
