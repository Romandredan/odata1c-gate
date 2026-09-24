"""Каталог правил сущностей (ADR-0016) и класс `sfr` (SPEC §6.4)."""

import pathlib

import pytest

from odata1c.gate.dictionary import Dictionary
from odata1c.gate.errors import PolicyError
from odata1c.gate.field_rules import classify_field
from odata1c.gate.policy import load_policy
from odata1c.gate.rules import (
    load_rules,
    owner_rules_stamp,
    package_rules,
    parse_rules_text,
)
from odata1c.gate.tokens import normalize_value

СЕКРЕТ = "секрет ровно для тестов каталога!".encode()


def класс(сущность, поле, *, rules=None):
    решение = classify_field(сущность, поле, "Edm.String", rules=rules)
    return решение[0] if решение else None


def дом_с_каталогом(tmp_path: pathlib.Path, **файлы: str) -> pathlib.Path:
    каталог = tmp_path / "gate"
    каталог.mkdir()
    for имя, текст in файлы.items():
        (каталог / f"{имя}.yaml").write_text(текст, encoding="utf-8")
    return tmp_path


# --- формат файла ---------------------------------------------------------------------------


def test_каталог_поставки_разбирается_и_не_пуст():
    каталог = package_rules()
    assert "Catalog_ФизическиеЛица" in каталог.people
    assert "Catalog_Контрагенты" in каталог.orgs
    assert каталог.name_rule("юрфизлицо") == "scan"


@pytest.mark.parametrize(
    ("текст", "фрагмент"),
    [
        ("people: Catalog_X", "список имён"),
        ("fields: {Поле: doc}", "Сущность.Поле"),
        ("names: {Поле: keep}", "keep каталогу не доступен"),
        ("names: {Поле: паспорт}", "класс не из списка"),
        ("правила: {}", "неизвестные разделы"),
        ("version: 2", "version: 1"),
        ("[1, 2]", "не словарь"),
        ("names: {Поле: [doc", "не разбирается"),
    ],
)
def test_негодный_файл_называет_путь_и_причину(текст, фрагмент):
    with pytest.raises(PolicyError) as ошибка:
        parse_rules_text(текст, "дом/gate/моя.yaml")
    assert "дом/gate/моя.yaml" in str(ошибка.value)
    assert фрагмент in str(ошибка.value)


def test_ошибка_разбора_не_повторяет_содержимое_файла():
    with pytest.raises(PolicyError) as ошибка:
        parse_rules_text("names: {СекретноеИмя: [doc", "x.yaml")
    assert "СекретноеИмя" not in str(ошибка.value)


def test_имена_раздела_names_без_учёта_регистра():
    разобрано = parse_rules_text("names: {СерияПаспорта: doc}", "x.yaml")
    assert разобрано["names"] == {"серияпаспорта": "doc"}


# --- порядок классификации ------------------------------------------------------------------


def test_scan_снимает_ложное_срабатывание_общего_правила():
    assert класс("Document_Прочее", "ЮрФизЛицо") is None


def test_правило_поля_действует_на_набор_записей_и_срез_регистра(tmp_path):
    дом = дом_с_каталогом(tmp_path, моё="fields:\n  InformationRegister_ап_Паспорта.Номер: doc\n")
    каталог = load_rules(дом)
    for сущность in (
        "InformationRegister_ап_Паспорта",
        "InformationRegister_ап_Паспорта_RecordType",
        "InformationRegister_ап_Паспорта_SliceLast",
    ):
        assert класс(сущность, "Номер", rules=каталог) == "doc"
    assert класс("Document_ап_Заказ", "Номер", rules=каталог) is None


def test_правило_поля_справочника_не_переходит_на_табличную_часть(tmp_path):
    дом = дом_с_каталогом(tmp_path, моё="fields:\n  Catalog_ап_Анкеты.Номер: doc\n")
    каталог = load_rules(дом)
    assert класс("Catalog_ап_Анкеты", "Номер", rules=каталог) == "doc"
    assert класс("Catalog_ап_Анкеты_Строки", "Номер", rules=каталог) is None


def test_служебные_поля_правилом_каталога_не_классифицируются(tmp_path):
    """Инвариант 6 держит классификатор до каталога: `Number`, ключи и типы ссылок."""
    дом = дом_с_каталогом(tmp_path, моё="names:\n  Number: doc\n  Ref_Key: doc\n")
    каталог = load_rules(дом)
    assert класс("Document_Любой", "Number", rules=каталог) is None
    assert класс("Document_Любой", "Ref_Key", rules=каталог) is None


def test_каталог_владельца_главнее_поставки(tmp_path):
    дом = дом_с_каталогом(tmp_path, моё="names:\n  ЮрФизЛицо: person\n")
    assert класс("Document_Прочее", "ЮрФизЛицо", rules=load_rules(дом)) == "person"


def test_справочник_людей_владельца_закрывает_description_и_адрес(tmp_path):
    дом = дом_с_каталогом(tmp_path, моё="people:\n  - Catalog_ап_Курьеры\n")
    каталог = load_rules(дом)
    assert класс("Catalog_ап_Курьеры", "Description", rules=каталог) == "person"


def test_одно_правило_с_разными_классами_в_двух_файлах_слоя_ошибка(tmp_path):
    дом = дом_с_каталогом(
        tmp_path, первый="names:\n  ап_Номер: doc\n", второй="names:\n  ап_Номер: sfr\n"
    )
    with pytest.raises(PolicyError, match="задано дважды"):
        load_rules(дом)


def test_без_каталога_владельца_каталог_поставки(tmp_path):
    assert load_rules(tmp_path) is package_rules()
    assert load_rules(None) is package_rules()


def test_отпечаток_каталога_владельца_меняется_с_файлом(tmp_path):
    дом = дом_с_каталогом(tmp_path, моё="names:\n  ап_Номер: doc\n")
    до = owner_rules_stamp(дом)
    (дом / "gate" / "второй.YAML").write_text("names:\n  ап_Серия: doc\n", encoding="utf-8")
    assert owner_rules_stamp(дом) != до


def test_политика_несёт_каталог_для_запасной_классификации(tmp_path):
    дом = дом_с_каталогом(tmp_path, моё="names:\n  ап_НомерУдостоверения: doc\n")
    политика = load_policy(tmp_path / "policy.yaml", rules=load_rules(дом))
    assert политика.rules.name_rule("ап_НомерУдостоверения") == "doc"


# --- класс sfr ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "поле",
    [
        "РегистрационныйНомерПФР",
        "РегистрационныйНомерФСС",
        "РегистрационныйНомерСФР",
        "РегНомерПФР",
        "РегномерСФРГоловнойОрганизации",
        "ИПРегистрационныйНомерФСС",
        "ПрежнийСтраховательРегистрационныйНомерПФР",
        "РегистрационныйНомерПФРвКорректируемыйПериод",
        "НомерСтрахователяФСС",
        "ИнаяОрганизацияРегистрационныйНомер",
    ],
)
def test_номер_страхователя_класс_sfr(поле):
    assert класс("Catalog_Организации", поле) == "sfr"


@pytest.mark.parametrize(
    "поле", ["КодПодчиненностиФСС", "ДополнительныйКодФСС", "РегистрационныйНомер"]
)
def test_код_отделения_и_прочие_регистрационные_номера_не_sfr(поле):
    assert класс("Catalog_Организации", поле) is None


def test_номер_пфр_в_двух_написаниях_один_токен(tmp_path):
    assert normalize_value("sfr", "087-104-012345") == "087104012345"
    словарь = Dictionary(tmp_path / "словарь.db", СЕКРЕТ)
    try:
        с_дефисами = словарь.token_for(
            "sfr", "087-104-012345", base="bp", entity="E", field="РегНомерПФР"
        )
        подряд = словарь.token_for(
            "sfr", "087104012345", base="bp", entity="E2", field="РегистрационныйНомерПФР"
        )
    finally:
        словарь.close()
    assert с_дефисами == подряд
    assert с_дефисами.startswith("[[sfr:")


# --- адрес физлица по имени поля (policy.АДРЕС_ФИЗЛИЦА) -----------------------------------


@pytest.mark.parametrize(
    "поле",
    [
        "АдресПроживания",
        "АдресПроживанияУлица",
        "АдресРегистрацииПоМестуЖительства",
        "АдресФактическогоПроживания",
        "АдресМестаПроживания",
        "АдресПоПрописке",
        "АдресЗарубежом",
        "ПредставительАдресМестаЖительства",
        "МестоРождения",
    ],
)
def test_адрес_физлица_по_имени_поля_закрыт_вне_mask_for(tmp_path, поле):
    политика = load_policy(tmp_path / "policy.yaml")
    политика._defaults = {"addr": {"mask_for": ["Catalog_ФизическиеЛица"]}}
    assert политика.addr_masked("Document_СведенияДляОплатыОтпускаСФР", поле)


@pytest.mark.parametrize("поле", ["АдресРегистрацииУстройства", "АдресЮридический", "Адрес"])
def test_прочие_адреса_вне_mask_for_открыты(tmp_path, поле):
    политика = load_policy(tmp_path / "policy.yaml")
    политика._defaults = {"addr": {"mask_for": ["Catalog_ФизическиеЛица"]}}
    assert not политика.addr_masked("Catalog_Контрагенты", поле)


# --- каталог владельца в гейте базы -------------------------------------------------------


def _гейт(tmp_path, дом):
    from odata1c.config.models import BaseConfig, GateSettings
    from odata1c.gate.guard import Guard
    from odata1c.gate.pipeline import BaseGate

    (tmp_path / "policy.yaml").write_text("version: 2\n", encoding="utf-8")
    словарь = Dictionary(tmp_path / "словарь.db", СЕКРЕТ)
    try:
        гейт = BaseGate(
            base=BaseConfig(
                name="bp",
                label="bp",
                url="http://host/base/odata/standard.odata/",
                user="agent",
                gate=GateSettings(mode="identifiers+names"),
            ),
            dictionary=словарь,
            guard=Guard(словарь),
            policy_path=tmp_path / "policy.yaml",
            home=дом,
        )
    except Exception:
        словарь.close()
        raise
    return гейт, словарь


def test_гейт_базы_видит_каталог_владельца_и_перечитывает_его(tmp_path):
    дом = tmp_path / "дом"
    дом.mkdir()
    гейт, словарь = _гейт(tmp_path, дом)
    try:
        assert гейт._policy.rules is package_rules()
        (дом / "gate").mkdir()
        (дом / "gate" / "доработки.yaml").write_text(
            "names:\n  ап_ПаспортСтрокой: doc\n", encoding="utf-8"
        )
        гейт.refresh()
        assert гейт._policy.rules.name_rule("ап_ПаспортСтрокой") == "doc"
    finally:
        словарь.close()


def test_негодный_каталог_владельца_останавливает_гейт_ошибкой_политики(tmp_path):
    дом = tmp_path / "дом"
    (дом / "gate").mkdir(parents=True)
    (дом / "gate" / "доработки.yaml").write_text("names:\n  ап_Поле: keep\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="keep каталогу не доступен"):
        _гейт(tmp_path, дом)
