"""Команды CLI `policy show`, `policy check` и `reveal`: код и подсказка на всех путях ошибок
(SPEC §3.5, §14.5).

Правки по итогам ревью задачи 9 (Important): у обеих команд не было ни одного автотеста — именно
поэтому падение на повреждённом словаре нашлось только ручной проверкой. Здесь же проверены
остальные названные ревьюером случаи: политика без индекса, без файла политики вовсе, на
несуществующей базе, на испорченном YAML; раскрытие известного токена (с указанием базы и поля и
без него), неизвестного, испорченного (не соответствующего формату), токена реквизита и — на
повреждённом файле словаря.

Задача 4 плана M2b (ADR-0015) добавляет источник строки к `policy show` (`владелец` | `авто`) и
новую команду `policy check` — тесты обеих ниже, в собственных разделах."""

import pathlib

from odata1c.cli import main
from odata1c.config.loader import load_config
from odata1c.gate.service import auto_policy_path, gate_db_path, open_dictionary, policy_path
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository

URL = "http://localhost/ut/odata/standard.odata/"
BASES = f"""
default: ut
bases:
  ut:
    label: УТ 11, тестовая
    url: {URL}
    user: u
    password: p
    role: test
"""


def _домашний_с_базой(tmp_path) -> pathlib.Path:
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    return home


def _построить_индекс(home: pathlib.Path, edmx_ut_real: bytes) -> None:
    """Настоящий индекс на урезанном образце УТ (проба P4) — для тестов, которым нужны
    существующие сущности/поля (счётчик дочерних у скрытых, проверка имён в `policy check`)."""
    хранилище = IndexRepository(index_path(home, "ut"))
    хранилище.write(parse_edmx(edmx_ut_real))
    хранилище.close()


# --- policy show ------------------------------------------------------------------------------


def test_policy_show_без_файла_политики(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)

    код = main(["policy", "show", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "политика ещё не создана" in вывод
    assert "odata1c reindex ut" in вывод


def test_policy_show_без_построенного_индекса(tmp_path, capsys):
    """cmd_policy_show читает только файл политики — индекс (metadata.sqlite) ему не нужен."""
    home = _домашний_с_базой(tmp_path)
    путь = policy_path(home, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text(
        "version: 2\nscan_free_text: true\nauto:\n  Catalog_Контрагенты.ИНН: inn\n",
        encoding="utf-8",
    )
    assert not (home / "bases" / "ut" / "metadata.sqlite").exists()

    код = main(["policy", "show", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "Catalog_Контрагенты.ИНН: inn" in вывод


def test_policy_show_несуществующей_базы(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)

    код = main(["policy", "show", "нет_такой", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "base_unknown" in вывод


def test_policy_show_испорченной_политики(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)
    путь = policy_path(home, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text("fields: [не закрытый список\n", encoding="utf-8")

    код = main(["policy", "show", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "policy_invalid" in вывод
    assert "подсказка" in вывод.lower()
    assert "traceback" not in вывод.lower()


def test_policy_show_печатает_источник_строк(tmp_path, capsys):
    """Задача 4: за файлом владельца следует объединённый вид — каждая строка помечена
    источником, `владелец` или `авто` (ADR-0015: auto живёт в `policy.auto.yaml`)."""
    home = _домашний_с_базой(tmp_path)
    путь = policy_path(home, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text(
        "version: 2\nscan_free_text: true\nfields:\n  Catalog_Контрагенты.КодПоОКПО: keep\n",
        encoding="utf-8",
    )
    auto_policy_path(home, "ut").write_text(
        "version: 2\ndefaults: {}\nauto:\n  Catalog_Контрагенты.ИНН: inn\n", encoding="utf-8"
    )

    код = main(["policy", "show", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "действующая политика" in вывод
    assert "Catalog_Контрагенты.ИНН: inn" in вывод
    assert "# авто" in вывод
    assert "Catalog_Контрагенты.КодПоОКПО: keep" in вывод
    assert "# владелец" in вывод


def test_policy_show_со_скрытыми_показывает_имена_владельцу(tmp_path, capsys, edmx_ut_real):
    """`policy show` — локальная команда владельца, не MCP: в отличие от ресурса
    `odata1c://policy/{base}` (задача 4, `names_visible=False`), здесь имя скрытой сущности
    показывается как есть — это его собственный файл, и он его и так видит целиком выше."""
    home = _домашний_с_базой(tmp_path)
    _построить_индекс(home, edmx_ut_real)
    путь = policy_path(home, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text(
        "version: 2\nentities:\n  Catalog_Контрагенты: { hide: true }\n", encoding="utf-8"
    )

    код = main(["policy", "show", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "# скрыты: Catalog_Контрагенты" in вывод
    assert "дочерних" in вывод  # индекс есть — счётчик поддерева печатается


def test_policy_show_без_индекса_без_счётчика_дочерних(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)
    путь = policy_path(home, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text(
        "version: 2\nentities:\n  Catalog_Контрагенты: { hide: true }\n", encoding="utf-8"
    )
    assert not index_path(home, "ut").exists()

    код = main(["policy", "show", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "# скрыты: Catalog_Контрагенты" in вывод
    assert "дочерних" not in вывод


# --- policy check -----------------------------------------------------------------------------


def test_policy_check_без_файла_политики(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)

    код = main(["policy", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "политика ещё не создана" in вывод


def test_policy_check_без_индекса_только_предупреждение(tmp_path, capsys):
    """Без индекса имена не проверяются — одна `warning`, но она не даёт код 1: код возврата
    зависит только от `error` (уточнение брифа задачи 4)."""
    home = _домашний_с_базой(tmp_path)
    путь = policy_path(home, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text("version: 2\nscan_free_text: true\n", encoding="utf-8")

    код = main(["policy", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "warning: index: индекса нет" in вывод


def test_policy_check_ошибка_даёт_код_1(tmp_path, capsys, edmx_ut_real):
    home = _домашний_с_базой(tmp_path)
    _построить_индекс(home, edmx_ut_real)
    путь = policy_path(home, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text("version: 2\nfields:\n  Catalog_Контрагенты.ИНН: secret\n", encoding="utf-8")

    код = main(["policy", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "error: fields.Catalog_Контрагенты.ИНН" in вывод
    assert "неизвестен" in вывод


def test_policy_check_без_замечаний(tmp_path, capsys, edmx_ut_real):
    home = _домашний_с_базой(tmp_path)
    _построить_индекс(home, edmx_ut_real)
    путь = policy_path(home, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text("version: 2\nfields:\n  Catalog_Контрагенты.ИНН: inn\n", encoding="utf-8")

    код = main(["policy", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "замечаний нет" in вывод


def test_policy_check_испорченной_политики(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)
    путь = policy_path(home, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text("fields: [не закрытый список\n", encoding="utf-8")

    код = main(["policy", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "policy_invalid" in вывод


def test_policy_check_нестроковый_ключ_в_fields_даёт_policy_invalid_а_не_traceback(
    tmp_path, capsys
):
    """Находка 2 ревью задачи 4 (Important): `fields: {123: keep}` — YAML разбирает `123` как
    число, и без проверки типа `check_policy` падал `AttributeError` мимо перехвата `main()`.
    Теперь это `PolicyError` (код `policy_invalid`), тем же путём, что и любой другой битый
    файл владельца."""
    home = _домашний_с_базой(tmp_path)
    путь = policy_path(home, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text("version: 2\nfields:\n  123: keep\n", encoding="utf-8")

    код = main(["policy", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "policy_invalid" in вывод
    assert "traceback" not in вывод.lower()


def test_policy_show_нестроковый_ключ_в_fields_даёт_policy_invalid_а_не_traceback(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)
    путь = policy_path(home, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text("version: 2\nfields:\n  123: keep\n", encoding="utf-8")

    код = main(["policy", "show", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "policy_invalid" in вывод
    assert "traceback" not in вывод.lower()


# --- reveal ------------------------------------------------------------------------------------


def _записать_токен(home, type_, raw_value, *, base, entity, field) -> str:
    config = load_config(home)
    словарь = open_dictionary(home, config.daemon.gate_secret)
    try:
        return словарь.token_for(type_, raw_value, base=base, entity=entity, field=field)
    finally:
        словарь.close()


def test_reveal_известного_токена_без_base_и_field_показывает_исходное_написание(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)
    токен = _записать_токен(
        home, "org", 'ООО "Ромашка"', base="ut", entity="Catalog_Контрагенты", field="Description"
    )
    capsys.readouterr()  # сбросить вывод odata1c init из _домашний_с_базой/_записать_токен

    код = main(["reveal", токен, "--home", str(home)])
    вывод = capsys.readouterr().out.strip()

    assert код == 0
    assert вывод == 'ООО "Ромашка"'  # не нормализованное "ооо ромашка"


def test_reveal_с_base_и_field_показывает_написание_именно_этой_базы(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)
    config = load_config(home)
    словарь = open_dictionary(home, config.daemon.gate_secret)
    try:
        токен = словарь.token_for(
            "org", "ооо ромашка", base="ut", entity="Catalog_Контрагенты", field="Description"
        )
        словарь.token_for(
            "org", "ООО Ромашка", base="buh", entity="Catalog_Контрагенты", field="Description"
        )
    finally:
        словарь.close()
    capsys.readouterr()  # сбросить вывод odata1c init из _домашний_с_базой

    код = main(["reveal", токен, "--home", str(home), "--base", "buh", "--field", "Description"])
    вывод = capsys.readouterr().out.strip()

    assert код == 0
    assert вывод == "ООО Ромашка"


def test_reveal_токена_реквизита_показывает_написание_а_не_только_цифры(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)
    токен = _записать_токен(
        home,
        "phone",
        "+7 (999) 123-45-67",
        base="ut",
        entity="Catalog_Контрагенты",
        field="Телефон",
    )
    capsys.readouterr()  # сбросить вывод odata1c init из _домашний_с_базой/_записать_токен

    код = main(["reveal", токен, "--home", str(home)])
    вывод = capsys.readouterr().out.strip()

    assert код == 0
    assert вывод == "+7 (999) 123-45-67"


def test_reveal_неизвестного_токена(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)

    код = main(["reveal", "[[org:9999999999]]", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "token_unknown" in вывод


def test_reveal_испорченного_токена_не_роняет_команду(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)

    код = main(["reveal", "[[org:не-токен", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "token_unknown" in вывод
    assert "traceback" not in вывод.lower()


def test_reveal_при_повреждённом_словаре(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)
    gate_db_path(home).write_bytes(b"not a real sqlite database, just random junk bytes")

    код = main(["reveal", "[[org:1]]", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "dictionary_corrupt" in вывод
    assert "подсказка" in вывод.lower()
    assert "traceback" not in вывод.lower()
