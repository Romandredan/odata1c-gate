"""Проверка файла владельца по индексу (`policy check`, ADR-0015, задача 4 плана M2b): неизвестные
сущности и поля, неизвестные классы, свой класс без правил, бесполезный `keep` на скрытой сущности,
работа без индекса. `check_policy` не редактирует файл — только читает и сообщает."""

import pathlib
import textwrap

import pytest

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
