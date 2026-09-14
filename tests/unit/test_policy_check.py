"""Проверка файла владельца по индексу (`policy check`, ADR-0015, задача 4 плана M2b): неизвестные
сущности и поля, неизвестные классы, свой класс без правил, бесполезный `keep` на скрытой сущности,
работа без индекса. `check_policy` не редактирует файл — только читает и сообщает."""

import pathlib
import textwrap

import pytest
from conftest import политика_из_шаблона_с_дублем_entities

from odata1c.gate.policy import PolicyError, load_policy, parse_owner_file
from odata1c.gate.policy_check import Finding, check_policy, render_effective


def _владелец(tmp_path: pathlib.Path, текст: str) -> pathlib.Path:
    путь = tmp_path / "policy.yaml"
    путь.write_text(textwrap.dedent(текст), encoding="utf-8")
    return путь


def test_неизвестная_сущность_в_entities(tmp_path, индекс_ut):
    путь = _владелец(
        tmp_path,
        """
        version: 2
        entities:
          Catalog_Контрагенты1: { hide: true }
        """,
    )

    находки = check_policy(путь, индекс_ut)

    ошибки = [н for н in находки if н.where == "entities.Catalog_Контрагенты1"]
    assert len(ошибки) == 1
    assert ошибки[0].level == "error"
    assert "Catalog_Контрагенты1" in ошибки[0].message
    assert "Catalog_Контрагенты" in ошибки[0].hint  # ближайшее известное имя — подсказка


def test_неизвестное_поле_в_fields(tmp_path, индекс_ut):
    путь = _владелец(
        tmp_path,
        """
        version: 2
        fields:
          Catalog_Контрагенты.ИННН: inn
        """,
    )

    находки = check_policy(путь, индекс_ut)

    ошибки = [н for н in находки if н.where == "fields.Catalog_Контрагенты.ИННН"]
    assert len(ошибки) == 1
    assert ошибки[0].level == "error"
    assert "ИННН" in ошибки[0].message
    assert "ИНН" in ошибки[0].hint  # difflib.get_close_matches по repo.field_names


def test_неизвестный_класс(tmp_path, индекс_ut):
    путь = _владелец(
        tmp_path,
        """
        version: 2
        fields:
          Catalog_Контрагенты.ИНН: secret
        """,
    )

    находки = check_policy(путь, индекс_ut)

    ошибки = [н for н in находки if н.where == "fields.Catalog_Контрагенты.ИНН"]
    assert len(ошибки) == 1
    assert ошибки[0].level == "error"
    assert "secret" in ошибки[0].message
    assert "неизвестен" in ошибки[0].message
    assert "inn" in ошибки[0].message  # список допустимых классов


def test_custom_класс_в_fields_без_раздела_custom(tmp_path, индекс_ut):
    путь = _владелец(
        tmp_path,
        """
        version: 2
        fields:
          Catalog_Контрагенты.ИНН: custom:tab_number
        """,
    )

    находки = check_policy(путь, индекс_ut)

    ошибки = [н for н in находки if н.where == "fields.Catalog_Контрагенты.ИНН"]
    assert len(ошибки) == 1
    assert ошибки[0].level == "error"
    assert "custom.tab_number" in ошибки[0].message


def test_свой_класс_без_fields_и_regex(tmp_path, индекс_ut):
    путь = _владелец(
        tmp_path,
        """
        version: 2
        custom:
          tab_number: {}
        """,
    )

    находки = check_policy(путь, индекс_ut)

    ошибки = [н for н in находки if н.where == "custom.tab_number"]
    assert len(ошибки) == 1
    assert ошибки[0].level == "error"


def test_keep_у_сущности_из_entities_hide_даёт_предупреждение(tmp_path, индекс_ut):
    путь = _владелец(
        tmp_path,
        """
        version: 2
        entities:
          Catalog_Контрагенты: { hide: true }
        fields:
          Catalog_Контрагенты.КодПоОКПО: keep
        """,
    )

    находки = check_policy(путь, индекс_ut)

    предупреждения = [н for н in находки if н.where == "fields.Catalog_Контрагенты.КодПоОКПО"]
    assert len(предупреждения) == 1
    assert предупреждения[0].level == "warning"
    assert "скрыта" in предупреждения[0].message


def test_неизвестное_имя_в_names_for(tmp_path, индекс_ut):
    путь = _владелец(
        tmp_path,
        """
        version: 2
        names_for: [Catalog_Контрагенты1]
        """,
    )

    находки = check_policy(путь, индекс_ut)

    ошибки = [н for н in находки if н.where == "names_for[0]"]
    assert len(ошибки) == 1
    assert ошибки[0].level == "error"
    assert "Catalog_Контрагенты" in ошибки[0].hint


def test_без_индекса_одно_предупреждение_и_остальные_проверки_идут(tmp_path):
    путь = _владелец(
        tmp_path,
        """
        version: 2
        fields:
          Catalog_Контрагенты.ИНН: secret
        custom:
          tab_number: {}
        """,
    )

    находки = check_policy(путь, None)

    предупреждения_индекса = [
        н for н in находки if н.level == "warning" and "индекса нет" in н.message
    ]
    assert len(предупреждения_индекса) == 1
    # Проверки, не требующие индекса (класс, свой класс без fields/regex), всё равно выполняются.
    assert any(н.where == "fields.Catalog_Контрагенты.ИНН" and н.level == "error" for н in находки)
    assert any(н.where == "custom.tab_number" and н.level == "error" for н in находки)
    # Проверки сущностей/полей по индексу без repo не выполняются — никаких ошибок про
    # существование Catalog_Контрагенты не появляется.
    assert not any("не найдена в индексе" in н.message for н in находки)


def test_валидная_политика_без_замечаний(tmp_path, индекс_ut):
    путь = _владелец(
        tmp_path,
        """
        version: 2
        fields:
          Catalog_Контрагенты.ИНН: inn
          Catalog_Контрагенты.КодПоОКПО: keep
        custom:
          driver_license:
            fields: [ВодительскоеУдостоверение]
            regex: '\\\\d{2}\\\\s?\\\\d{2}\\\\s?\\\\d{6}'
        """,
    )

    находки = check_policy(путь, индекс_ut)

    assert находки == []


def test_битый_yaml_даёт_policyerror_а_не_finding(tmp_path, индекс_ut):
    путь = tmp_path / "policy.yaml"
    путь.write_text("fields: [не закрытый список\n", encoding="utf-8")

    with pytest.raises(PolicyError):
        check_policy(путь, индекс_ut)


def test_finding_атрибуты_по_умолчанию():
    находка = Finding(level="warning", where="x", message="сообщение")

    assert находка.hint == ""


# --- ревью задачи 4 (Important) ------------------------------------------------------------


def test_нестроковый_ключ_в_fields_даёт_policyerror_а_не_голое_исключение(tmp_path):
    """Находка 2 ревью задачи 4 (Important): YAML без кавычек разбирает `123: keep` как ключ-
    число — без проверки типа `check_policy` падал `AttributeError` на `ключ.partition(".")`,
    а `effective_rows` — `TypeError` при сортировке смешанных строк и чисел. Теперь это ошибка
    политики (`PolicyError`, код `policy_invalid`), как и любой другой неверный файл владельца —
    не исключение посреди работы тула/команды."""
    путь = tmp_path / "policy.yaml"
    путь.write_text("version: 2\nfields:\n  123: keep\n", encoding="utf-8")

    with pytest.raises(PolicyError) as ошибка:
        check_policy(путь, None)

    assert ошибка.value.code == "policy_invalid"
    assert "fields" in str(ошибка.value)


def test_нестроковый_элемент_names_for_даёт_policyerror(tmp_path):
    путь = tmp_path / "policy.yaml"
    путь.write_text("version: 2\nnames_for: [Catalog_Контрагенты, 42]\n", encoding="utf-8")

    with pytest.raises(PolicyError):
        parse_owner_file(путь)


def test_нестроковый_элемент_mask_for_даёт_policyerror(tmp_path):
    путь = tmp_path / "policy.yaml"
    путь.write_text(
        "version: 2\ndefaults:\n  addr:\n    mask_for: [Catalog_ФизическиеЛица, 1]\n",
        encoding="utf-8",
    )

    with pytest.raises(PolicyError):
        parse_owner_file(путь)


# --- ревью задачи 4, повторный проход (Important) ------------------------------------------


def test_mask_for_не_список_даёт_policyerror_у_parse_owner_file(tmp_path):
    """Находка 3 повторного ревью задачи 4 (Important): в отличие от `names_for`
    (`isinstance(..., list)` проверялся до итерации), у `defaults.addr.mask_for` проверки типа
    контейнера не было вовсе — `mask_for: 5` итерировался бы как число, `TypeError: 'int' object
    is not iterable` вместо честной `PolicyError`."""
    путь = tmp_path / "policy.yaml"
    путь.write_text("version: 2\ndefaults:\n  addr:\n    mask_for: 5\n", encoding="utf-8")

    with pytest.raises(PolicyError) as ошибка:
        parse_owner_file(путь)

    assert ошибка.value.code == "policy_invalid"
    assert "mask_for" in str(ошибка.value)


def test_mask_for_не_список_даёт_policyerror_у_check_policy(tmp_path, индекс_ut):
    путь = tmp_path / "policy.yaml"
    путь.write_text("version: 2\ndefaults:\n  addr:\n    mask_for: 5\n", encoding="utf-8")

    with pytest.raises(PolicyError):
        check_policy(путь, индекс_ut)


def test_нестроковый_ключ_сообщение_не_цитирует_значение(tmp_path):
    """Находка 4 повторного ревью задачи 4 (Important): сообщение называет тип и порядковый
    номер записи в разделе, а не буквальное значение ключа/элемента — тот же принцип, что у
    `_разобрать_yaml` (файл не цитируется) и у соседней `_проверить_тип_раздела` (печатает
    только тип)."""
    путь = tmp_path / "policy.yaml"
    путь.write_text(
        "version: 2\nfields:\n  Catalog_Контрагенты.ИНН: inn\n  123: keep\n", encoding="utf-8"
    )

    with pytest.raises(PolicyError) as ошибка:
        parse_owner_file(путь)

    сообщение = str(ошибка.value)
    assert "123" not in сообщение  # само значение ключа не процитировано
    assert "fields" in сообщение
    assert "2-й записи" in сообщение  # позиция по порядку в разделе
    assert "int" in сообщение  # тип вместо значения


def test_нестроковый_элемент_списка_сообщение_не_цитирует_значение(tmp_path):
    путь = tmp_path / "policy.yaml"
    путь.write_text(
        "version: 2\nnames_for: [Catalog_Контрагенты, 42]\n",
        encoding="utf-8",
    )

    with pytest.raises(PolicyError) as ошибка:
        parse_owner_file(путь)

    сообщение = str(ошибка.value)
    assert "42" not in сообщение
    assert "names_for" in сообщение
    assert "2-й записи" in сообщение
    assert "int" in сообщение


def test_render_effective_скрывает_строку_потомка_независимо_от_names_visible(tmp_path):
    """Находка 1 ревью задачи 4 (Important): `render_effective` больше не пересчитывает набор
    скрытых сама (из `policy.hidden_entities()` и необязательного `repo`) — полный набор
    (корень + поддерево) передаёт вызывающий через обязательный `hidden`. Здесь имитируется
    вызывающий, который посчитал набор верно (как `ToolService._скрытые`/`repo.descendants`):
    строка авторазметки дочернего объекта (`…_Товары.Номенклатура`) не должна появиться ни во
    владельческом виде (`names_visible=True`), ни в виде для модели (`names_visible=False`).

    Раздел `auto` — в ОТДЕЛЬНОМ файле авторазметки (ADR-0015), а не в теле владельца: иначе сама
    строка `Document_X_Товары.Номенклатура: org` лежала бы в `owner_text` буквально (владелец
    печатается как есть, без редактирования), и тест проверял бы не фильтр `render_effective`,
    а совсем другое — что уже отфильтровал вызывающий до него."""
    путь = tmp_path / "policy.yaml"
    путь.write_text("version: 2\nentities:\n  Document_X: { hide: true }\n", encoding="utf-8")
    путь_авто = tmp_path / "policy.auto.yaml"
    путь_авто.write_text(
        "version: 2\ndefaults: {}\nauto:\n  Document_X_Товары.Номенклатура: org\n",
        encoding="utf-8",
    )
    owner_data = parse_owner_file(путь)
    policy = load_policy(путь, путь_авто)
    корни = policy.hidden_entities()
    assert корни == {"Document_X"}
    полный_набор = корни | {"Document_X_Товары"}  # то, что вернул бы repo.descendants(корни)

    для_владельца = render_effective(
        путь.read_text(encoding="utf-8"), policy, owner_data, hidden=полный_набор
    )
    для_модели = render_effective(
        путь.read_text(encoding="utf-8"),
        policy,
        owner_data,
        hidden=полный_набор,
        names_visible=False,
    )

    assert "Document_X_Товары.Номенклатура" not in для_владельца
    assert "Document_X_Товары.Номенклатура" not in для_модели
    assert "Document_X_Товары" not in для_модели  # и сам потомок не назван модели по имени


# --- повтор раздела верхнего уровня (находка I1 итогового ревью M2b) ---------------------------


def test_повтор_раздела_из_шаблона_даёт_error_с_номером_строки(tmp_path):
    """Владелец раскомментировал пример `# entities:` и оставил заглушку `entities: {}` выше —
    файл, который `pyyaml` разбирает молча (побеждает последний раздел), а `ruamel` (реиндекс и
    конструктор политики) разобрать не может вовсе. `policy check` обязан назвать раздел."""
    путь = tmp_path / "policy.yaml"
    путь.write_text(политика_из_шаблона_с_дублем_entities(), encoding="utf-8")

    находки = check_policy(путь, None)

    повторы = [н for н in находки if н.where == "entities"]
    assert len(повторы) == 1
    assert повторы[0].level == "error"
    assert "дважды" in повторы[0].message
    assert "entities" in повторы[0].hint


def test_сообщение_о_повторе_не_цитирует_содержимое_раздела(tmp_path):
    """Инвариант 1: текст `DuplicateKeyError` ruamel несёт значение повторённого ключа
    («found duplicate key … with value …»). В находку идут только имя раздела и номер строки."""
    путь = tmp_path / "policy.yaml"
    путь.write_text(политика_из_шаблона_с_дублем_entities(), encoding="utf-8")

    повторы = [н for н in check_policy(путь, None) if н.where == "entities"]

    assert "Catalog_ФизическиеЛица" not in повторы[0].message
    assert "Catalog_ФизическиеЛица" not in повторы[0].hint
    assert "hide" not in повторы[0].message


def test_повтор_раздела_не_мешает_остальным_проверкам(tmp_path):
    путь = _владелец(
        tmp_path,
        """
        version: 2
        fields:
          Catalog_Контрагенты.ИНН: secret
        fields:
          Catalog_Контрагенты.ИНН: secret
        """,
    )

    находки = check_policy(путь, None)

    assert any(н.where == "fields" and н.level == "error" for н in находки)
    assert any(
        н.where == "fields.Catalog_Контрагенты.ИНН" and "неизвестен" in н.message for н in находки
    )


def test_без_повторов_находки_о_повторе_нет(tmp_path, индекс_ut):
    путь = _владелец(
        tmp_path,
        """
        version: 2
        entities:
          Catalog_ФизическиеЛица: { hide: true }
        """,
    )

    находки = check_policy(путь, индекс_ut)

    assert not any(н.where == "entities" for н in находки)


# --- формат ключа fields без индекса (находка I3 итогового ревью M2b) --------------------------


def test_ключ_fields_без_точки_даёт_error_и_без_индекса(tmp_path):
    """`fields: {ДопИдентификатор: inn}` — мёртвое правило при любом состоянии индекса:
    `Policy.sensitivity_of` ищет ключ вида `Сущность.Поле`. Прежде проверка формата стояла внутри
    ветки `repo is not None`, и без индекса `policy check` молчал."""
    путь = _владелец(
        tmp_path,
        """
        version: 2
        fields:
          ДопИдентификатор: inn
        """,
    )

    находки = check_policy(путь, None)

    ошибки = [н for н in находки if н.where == "fields.ДопИдентификатор"]
    assert len(ошибки) == 1
    assert ошибки[0].level == "error"
    assert "Сущность.Поле" in ошибки[0].message


def test_ключ_fields_без_точки_с_индексом_называет_формат_а_не_пропажу_имени(tmp_path, индекс_ut):
    путь = _владелец(
        tmp_path,
        """
        version: 2
        fields:
          ДопИдентификатор: inn
        """,
    )

    ошибки = [н for н in check_policy(путь, индекс_ut) if н.where == "fields.ДопИдентификатор"]

    assert len(ошибки) == 1
    assert "Сущность.Поле" in ошибки[0].message
    assert "не найдена в индексе" not in ошибки[0].message


def test_пустая_часть_ключа_fields_тоже_error(tmp_path):
    путь = _владелец(
        tmp_path,
        """
        version: 2
        fields:
          Catalog_Контрагенты.: inn
        """,
    )

    assert any(
        н.where == "fields.Catalog_Контрагенты." and н.level == "error"
        for н in check_policy(путь, None)
    )
