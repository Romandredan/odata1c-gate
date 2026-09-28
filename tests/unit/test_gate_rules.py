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


def test_scan_поставки_снимает_только_названия_и_адреса():
    """Правило каталога проверяется раньше правил реквизитов, и `scan` на поле с ИНН оставил бы
    его одному детектору (10-значный ИНН без слова «ИНН» рядом тот не ловит). Каталог поставки
    вправе снимать `scan` только ложные ФИО, названия и адреса."""
    from odata1c.gate.rules import RuleCatalog

    пустой = RuleCatalog()
    каталог = package_rules()
    нарушения = []
    for имя, правило in каталог.names.items():
        if правило != "scan":
            continue
        без_каталога = classify_field("Document_Прочее", имя, "Edm.String", rules=пустой)
        if без_каталога is not None and без_каталога[0] not in ("person", "org", "addr"):
            нарушения.append((имя, без_каталога[0]))
    for ключ, правило in каталог.fields.items():
        сущность, _, поле = ключ.rpartition(".")
        без_каталога = classify_field(сущность, поле, "Edm.String", rules=пустой)
        if (
            правило == "scan"
            and без_каталога is not None
            and без_каталога[0]
            not in (
                "person",
                "org",
                "addr",
            )
        ):
            нарушения.append((ключ, без_каталога[0]))
    assert not нарушения, нарушения


def test_scan_поставки_не_снимает_слой_1_у_справочников_людей_и_организаций():
    """Ревью атакующим, находка 7: `names: <поле названия>: scan` снял бы `Description`/ФИО
    у справочников людей и организаций — проверка на нейтральной сущности этого не видит."""
    from odata1c.gate.rules import RuleCatalog

    каталог = package_rules()
    без_scan = RuleCatalog(people=каталог.people, orgs=каталог.orgs)
    # Слой 1 — класс, который поле получает только внутри справочника людей или организаций;
    # исключения слоя 2 (`ДолжностьРуководителя`, `ЮрФизЛицо`) дают класс в любой сущности и
    # сняты намеренно.
    нарушения = [
        (сущность, имя)
        for имя, правило in каталог.names.items()
        if правило == "scan"
        and classify_field("Document_Прочее", имя, "Edm.String", rules=без_scan) is None
        for сущность in sorted(каталог.names_for)
        if (найдено := classify_field(сущность, имя, "Edm.String", rules=без_scan)) is not None
        and найдено[0] in ("person", "org")
    ]
    assert not нарушения, нарушения


def test_строгий_режим_применяет_правила_fields_к_любой_сущности():
    """Ревью атакующим, находка 2: `raw_get` с именем, которого индекс не знает
    (`…ДокументыФизическихЛиц.` с точкой), идёт строгим режимом — правило `fields`, привязанное
    к точному имени сущности, там действует по имени поля."""
    искажённое = "InformationRegister_ДокументыФизическихЛиц."
    assert classify_field(искажённое, "Номер", "Edm.String") is None
    assert classify_field(искажённое, "Номер", "Edm.String", strict=True) == ("doc", "auto")
    assert classify_field(искажённое, "Представление", "Edm.String", strict=True)[0] == "doc"
    for другое in (
        "InformationRegister_ДокументыФизическихЛиц​",
        "informationregister_документыфизическихлиц",
        "InformationRegister_ДокументыФизическихЛи́ц",
        "InformationRegister_ДокументыФизическихЛиц ",
        "InformationRegister_ДокументыФизическихЛиц_SliceLast.",
    ):
        assert classify_field(другое, "Номер", "Edm.String", strict=True) == ("doc", "auto")


def test_строгий_режим_не_переносит_правило_на_постороннюю_сущность():
    """Повторное ревью, находка 2: `Номер` и `Представление` чужой неизвестной сущности не
    закрываются правилом регистра документов физлиц (инвариант 6)."""
    assert (
        classify_field("Document_НеизвестныйДокумент", "Номер", "Edm.String", strict=True) is None
    )


def test_пустой_каталог_поставки_ошибка(monkeypatch):
    """Ревью атакующим, находка 3: без файлов каталога поставки защита не должна молча
    ослабнуть — пустой каталог поставки останавливает загрузку ошибкой."""
    from odata1c.gate import rules

    monkeypatch.setattr(rules, "_файлы_поставки", lambda: [])
    rules._слой_поставки.cache_clear()
    try:
        with pytest.raises(PolicyError, match="каталог правил поставки пуст"):
            rules._слой_поставки()
    finally:
        monkeypatch.undo()
        rules._слой_поставки.cache_clear()
        assert rules._слой_поставки()


async def test_негодный_каталог_владельца_закрывает_тулы_policy_invalid(tmp_path, edmx_ut_real):
    """Ревью атакующим, находка 8: код отказа проверен сквозь `ToolService`, а не только
    исключением конструктора гейта. В тексте отказа — путь, ключ правила и причина; данных 1С
    в каталоге нет, и повторять из него нечего, кроме имён полей."""
    import json

    from odata1c.cli import main
    from odata1c.config.loader import load_config
    from odata1c.gate.service import refresh_policy
    from odata1c.index.edmx import parse_edmx
    from odata1c.index.reindex import index_path
    from odata1c.index.repository import IndexRepository
    from odata1c.registry.registry import SessionScope
    from odata1c.tools.service import ToolService

    дом = tmp_path / "home"
    main(["init", "--home", str(дом)])
    (дом / "bases.yaml").write_text(
        "default: ut\nbases:\n  ut:\n    label: УТ\n"
        "    url: http://localhost/ut/odata/standard.odata/\n    user: u\n    password: p\n"
        "    role: prod\n",
        encoding="utf-8",
    )
    хранилище = IndexRepository(index_path(дом, "ut"))
    хранилище.write(parse_edmx(edmx_ut_real))
    хранилище.close()
    refresh_policy(дом, load_config(дом).bases["ut"])
    (дом / "gate").mkdir()
    (дом / "gate" / "доработки.yaml").write_text(
        "names:\n  СекретноеПолеВладельца: keep\n", encoding="utf-8"
    )
    служба = ToolService(load_config(дом))
    try:
        ответ = json.loads(
            await служба.query(
                SessionScope(), base="ut", entity="Catalog_Контрагенты", select=["Ref_Key"]
            )
        )
    finally:
        await служба.aclose()
    assert ответ["error"]["code"] == "policy_invalid"
    assert "keep каталогу не доступен" in ответ["error"]["message"]


def test_нечитаемый_файл_владельца_ошибка_политики(tmp_path):
    (tmp_path / "gate").mkdir()
    (tmp_path / "gate" / "моё.yaml").write_bytes(b"names:\n  \xff\xfe: doc\n")
    with pytest.raises(PolicyError, match="не читается как UTF-8"):
        load_rules(tmp_path)


def test_мусорные_токены_снятых_полей_не_ищутся_в_тексте(tmp_path):
    """Ревью атакующим, находка 6: токен person, выданный до правки полю `СотрудникПол`
    («Мужской»), не должен заменять это слово в любом тексте ответа."""
    словарь = Dictionary(tmp_path / "словарь.db", СЕКРЕТ)
    try:
        словарь.token_for("person", "Мужской", base="bp", entity="E", field="СотрудникПол")
        словарь.token_for("person", "Иванов Иван", base="bp", entity="E", field="Сотрудник")
        варианты = словарь.name_variants()
    finally:
        словарь.close()
    assert "мужской" not in варианты
    assert "иванов иван" in варианты


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


# --- адрес физлица по имени поля (field_rules.АДРЕС_ФИЗЛИЦА) -------------------------------


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
    # Разведка ЗУП: класс addr по имени поля дают тот же шаблон, что и закрытие, — поле с
    # признаком адреса человека в любом месте имени доходит до закрытия.
    assert classify_field("Document_СведенияДляОплатыОтпускаСФР", поле, "Edm.String") == (
        "addr",
        "auto",
    )


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


# --- страж и короткие значения doc (ревью атакующим, находка 1) ----------------------------


@pytest.fixture
def страж(tmp_path):
    from odata1c.gate.guard import Guard

    словарь = Dictionary(tmp_path / "словарь.db", СЕКРЕТ)
    for значение in ("260826", "000123"):
        словарь.token_for(
            "doc",
            значение,
            base="bp",
            entity="InformationRegister_ДокументыФизическихЛиц",
            field="Номер",
        )
    словарь.token_for("doc", "45 03 123456", base="bp", entity="E", field="НомерПаспорта")
    yield Guard(словарь)
    словарь.close()


def test_короткий_номер_документа_не_портит_номера_и_даты(страж):
    import json

    текст = json.dumps(
        {"Number": "0000-000123", "Date": "2026-08-26T19:30:11", "Code": "00-000123"},
        ensure_ascii=False,
    )
    assert страж.check(текст, mode="identifiers").text == текст


def test_короткий_номер_документа_целой_серией_закрывается(страж):
    итог = страж.check('{"Комментарий": "паспорт № 260826"}', mode="identifiers")
    assert "260826" not in итог.text
    assert "[[doc:" in итог.text


def test_серия_и_номер_паспорта_ищутся_и_внутри_текста(страж):
    итог = страж.check('{"Комментарий": "паспорт 4503123456, выдан"}', mode="identifiers")
    assert "4503123456" not in итог.text


@pytest.mark.parametrize(
    "текст",
    [
        "Иванов И.И., 45 03 260826, прописан",
        "4503 260826",
        "паспорт 45 03 260826 01.02.2010",
        "пасп. 4503-260826",
        "паспорт 4503260826",
        # Третий раунд ревью: любой пробельный разделитель и разделитель внутри серии.
        "Иванов, 45 03\\n260826",
        "45 03\\t260826",
        "45 03  260826",
        "45 03 260826",
        "45-03 260826",
        "45.03 260826",
        "серия 4503 № 260826",
    ],
)
def test_номер_из_регистра_рядом_с_серией_закрывается(страж, текст):
    """Повторное ревью, находка 1: номер из регистра документов физлиц (шесть цифр) в записи
    «серия номер» закрывается, хотя цифровая серия текста длиннее номера."""
    итог = страж.check(f'{{"К": "{текст}"}}', mode="identifiers")
    assert "260826" not in итог.text


@pytest.mark.parametrize(
    "текст",
    [
        "ИНН 7707260826",
        "тел. 9161260826",
        "док 0000-260826",
        "Серия документа: 0000-260826",
        "+7 916 260826",
        "4 503 260 826",
    ],
)
def test_слитная_запись_без_слова_паспорта_не_трогается(страж, текст):
    """Слитно пишутся ИНН и телефоны, через дефис — номера документов 1С: без слова паспорта
    их последние шесть цифр не заменяются по совпадению с номером паспорта (инвариант 6)."""
    итог = страж.check(f'{{"К": "{текст}"}}', mode="identifiers")
    assert "[[doc:" not in итог.text
