"""Команды CLI: init, base list, base test, base add, base import."""

import pathlib

import httpx
import pytest
import respx
import yaml

import odata1c.cli as cli
from odata1c.cli import main
from odata1c.config.loader import ConfigError, load_config
from odata1c.config.writer import ensure_policy_template

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


def test_init_создаёт_каталог_и_шаблоны(tmp_path, capsys):
    код = main(["init", "--home", str(tmp_path / "home")])
    вывод = capsys.readouterr().out

    assert код == 0
    assert (tmp_path / "home" / "bases.yaml").exists()
    assert (tmp_path / "home" / "daemon.yaml").exists()
    assert (tmp_path / "home" / "bases").is_dir()
    assert "bases.yaml" in вывод


def test_init_не_затирает_существующие_настройки(tmp_path):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    main(["init", "--home", str(home)])
    assert "УТ 11, тестовая" in (home / "bases.yaml").read_text(encoding="utf-8")


def test_base_list_показывает_роль_и_уровень_гейта(tmp_path, capsys):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")

    код = main(["base", "list", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "ut" in вывод
    assert "test" in вывод
    assert "identifiers" in вывод


def test_base_list_без_баз_подсказывает_куда_писать(tmp_path, capsys):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text("bases: {}\n", encoding="utf-8")

    main(["base", "list", "--home", str(home)])
    вывод = capsys.readouterr().out
    assert "bases.yaml" in вывод
    assert "base import" in вывод


@respx.mock
def test_base_test_докладывает_успех(tmp_path, capsys):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    respx.get(f"{URL}$metadata").mock(
        return_value=httpx.Response(
            200, text="<edmx:Edmx/>", headers={"Content-Type": "application/xml"}
        )
    )
    # завершение сеанса при client.close() пойдёт на этот же адрес без хвоста пути
    # (см. тот же приём в tests/unit/test_client1c.py)
    respx.get(URL).mock(return_value=httpx.Response(200, json={"value": []}))

    код = main(["base", "test", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "соединение установлено" in вывод.lower()


@respx.mock
def test_base_test_докладывает_отказ_аутентификации(tmp_path, capsys):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    respx.get(f"{URL}$metadata").mock(return_value=httpx.Response(401, text="Unauthorized"))

    код = main(["base", "test", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "auth_failed" in вывод


def test_base_test_неизвестной_базы(tmp_path, capsys):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")

    код = main(["base", "test", "нет_такой", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "base_unknown" in вывод
    assert "ut" in вывод  # подсказка со списком доступных


@pytest.mark.parametrize(
    "argv",
    [
        ["--home", "X", "init"],
        ["init", "--home", "X"],
        ["base", "--home", "X", "list"],
        ["base", "list", "--home", "X"],
        ["base", "test", "--home", "X", "ut"],
        ["base", "test", "ut", "--home", "X"],
    ],
    ids=[
        "--home перед init",
        "--home после init",
        "--home между base и list",
        "--home после list",
        "--home между test и именем базы",
        "--home после имени базы",
    ],
)
def test_home_разбирается_в_любой_позиции(argv, monkeypatch):
    """Регресс: argparse копирует пространство имён подпарсера ЦЕЛИКОМ поверх пространства
    имён родителя (`_SubParsersAction.__call__`). Без `default=argparse.SUPPRESS` подпарсер,
    в чей хвост --home не попал, подставляет свой default (None) и затирает уже
    распознанное родителем значение — независимо от того, добавлен ли --home через
    `parents=` на этом уровне. Проверяем именно разобранное значение, не выполняя команду.
    """
    увиденный: dict[str, pathlib.Path] = {}

    def записать_init(home):
        увиденный["home"] = home
        return 0

    def записать_list(home):
        увиденный["home"] = home
        return 0

    def записать_test(home, name):
        увиденный["home"] = home
        увиденный["name"] = name
        return 0

    monkeypatch.setattr(cli, "cmd_init", записать_init)
    monkeypatch.setattr(cli, "cmd_base_list", записать_list)
    monkeypatch.setattr(cli, "cmd_base_test", записать_test)

    код = cli.main(argv)

    assert код == 0
    assert увиденный["home"] == pathlib.Path("X")


def test_base_list_несуществующий_домашний_каталог_даёт_ошибку_а_не_трейсбек(tmp_path, capsys):
    home = tmp_path / "нет_такого_каталога"

    код = main(["base", "list", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "config_invalid" in вывод
    assert str(home) in вывод
    assert "init" in вывод


def test_base_test_несуществующий_домашний_каталог_даёт_ошибку_а_не_трейсбек(tmp_path, capsys):
    home = tmp_path / "нет_такого_каталога"

    код = main(["base", "test", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "config_invalid" in вывод
    assert str(home) in вывод
    assert "init" in вывод


def test_load_config_несуществующий_домашний_каталог(tmp_path):
    """Дефект жил в загрузчике настроек (задача 3): попытка создать daemon.yaml внутри
    ещё не существующего каталога роняла load_config необработанным FileNotFoundError."""
    home = tmp_path / "нет_такого_каталога"
    with pytest.raises(ConfigError) as ошибка:
        load_config(home)
    assert str(home) in str(ошибка.value)
    assert "init" in (ошибка.value.hint or "")


def test_load_config_домашний_путь_указывает_на_файл(tmp_path):
    """Тот же класс дефекта на шаг дальше: путь существует, но это обычный файл (опечатка
    в --home), а не каталог. home / "daemon.yaml" в этом случае роняет NotADirectoryError,
    если проверка входа использует exists() вместо is_dir()."""
    файл = tmp_path / "не_каталог.txt"
    файл.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError) as ошибка:
        load_config(файл)
    assert str(файл) in str(ошибка.value)


def _ввод_для_add(monkeypatch, url="http://localhost/x/odata/standard.odata/", label="Подпись"):
    """Подставляет ответы на вопросы cmd_base_add через input()/getpass.getpass(), не трогая
    настоящий терминал; пароль — заведомо не встречающийся больше нигде маркер."""
    ответы = iter([url, label, "пользователь"])
    monkeypatch.setattr("builtins.input", lambda *_: next(ответы))
    monkeypatch.setattr("getpass.getpass", lambda *_: "секретный_пароль_только_для_теста")


def test_base_list_после_init_баз_не_описано(tmp_path, capsys):
    """Регресс: шаблон bases.yaml содержал незакомментированную демонстрационную базу ut —
    сразу после создания каталога odata1c_bases показывал её как настоящую (SPEC §11.4)."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])

    код = main(["base", "list", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "баз не описано" in вывод
    assert "роль" not in вывод  # заголовок таблицы баз печатается, только когда базы есть


def test_base_add_дважды_одним_именем_отказывает(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    _ввод_для_add(monkeypatch)
    код1 = main(["base", "add", "ut", "--home", str(home)])
    assert код1 == 0

    код2 = main(["base", "add", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код2 == 1
    assert "уже описана" in вывод
    # вторая попытка не должна была даже спросить пароль — дубликат проверяется до ввода.
    # Считаем только активную запись "  ut:" (без "#"), а не совпадения внутри
    # закомментированного демонстрационного блока шаблона (там тоже есть "# ut:").
    текст = (home / "bases.yaml").read_text(encoding="utf-8")
    assert текст.count("\n  ut:\n") == 1


def test_base_add_недопустимое_имя_даёт_ошибку_а_не_трейсбек(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    _ввод_для_add(monkeypatch)

    код = main(["base", "add", "Недопустимое Имя", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "config_invalid" in вывод
    assert "секретный_пароль_только_для_теста" not in вывод


def test_base_add_пароль_не_попадает_в_вывод(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    _ввод_для_add(monkeypatch)

    код = main(["base", "add", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "секретный_пароль_только_для_теста" not in вывод
    config = load_config(home)
    assert config.bases["ut"].password == "секретный_пароль_только_для_теста"


def test_base_add_создаёт_файл_политики_владельца_из_шаблона(tmp_path, monkeypatch, capsys):
    """ADR-0015, задача 3: `base add` создаёт `bases/<имя>/policy.yaml` из шаблона, шапка
    которого называет настоящую базу, а не образец `{{base}}`. Повторное обеспечение (например,
    следующим реиндексом той же базы) файл не трогает."""
    home = tmp_path / "home"
    _ввод_для_add(monkeypatch)

    код = main(["base", "add", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    путь_политики = home / "bases" / "ut" / "policy.yaml"
    assert путь_политики.exists()
    текст = путь_политики.read_text(encoding="utf-8")
    assert "ut" in текст
    assert "{{base}}" not in текст
    assert "создан файл политики владельца" in вывод
    assert str(путь_политики) in вывод

    assert ensure_policy_template(home, "ut") is False
    assert путь_политики.read_text(encoding="utf-8") == текст


def test_base_import_базу_ut_переносит_несмотря_на_шаблон(tmp_path, capsys):
    """Регресс: незакомментированная демонстрационная база ut в шаблоне заставляла перенос
    базы с тем же именем печатать «уже описана, пропускаю» и терять настоящие учётные данные."""
    home = tmp_path / "home"
    env = tmp_path / "1c-odata.env"
    env.write_text(
        "ODATA_DB_UT_BASE_URL=http://real.invalid/ut/odata/standard.odata/\n"
        "ODATA_DB_UT_USERNAME=настоящий_пользователь\n"
        "ODATA_DB_UT_PASSWORD=настоящий_пароль\n",
        encoding="utf-8",
    )

    код = main(["base", "import", str(env), "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "уже описана" not in вывод
    assert "перенесена база «ut»" in вывод
    config = load_config(home)
    assert config.bases["ut"].url == "http://real.invalid/ut/odata/standard.odata/"


def test_base_import_переносит_базу_по_умолчанию_из_env(tmp_path):
    """База по умолчанию в env-файле — не первая в списке; раньше запись всегда обрывалась,
    потому что шаблон уже содержал активную строку default: ut."""
    home = tmp_path / "home"
    env = tmp_path / "1c-odata.env"
    env.write_text(
        "ODATA_DEFAULT_DB=buh\n"
        "ODATA_DB_UT_BASE_URL=http://a.invalid/ut/odata/standard.odata/\n"
        "ODATA_DB_BUH_BASE_URL=http://b.invalid/buh/odata/standard.odata/\n",
        encoding="utf-8",
    )

    main(["base", "import", str(env), "--home", str(home)])
    config = load_config(home)

    assert config.default == "buh"


# --- секрет гейта: создаётся командой создания каталога, не чтением настроек ---


def test_init_кладёт_непустой_секрет_гейта_в_daemon_yaml(tmp_path):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])

    данные = yaml.safe_load((home / "daemon.yaml").read_text(encoding="utf-8"))
    assert данные.get("gate_secret")


def test_init_повторный_вызов_не_меняет_секрет(tmp_path):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    секрет_1 = yaml.safe_load((home / "daemon.yaml").read_text(encoding="utf-8"))["gate_secret"]

    main(["init", "--home", str(home)])
    секрет_2 = yaml.safe_load((home / "daemon.yaml").read_text(encoding="utf-8"))["gate_secret"]

    assert секрет_1 == секрет_2


def test_init_комментарии_daemon_yaml_переживают_повторный_init_и_base_list(tmp_path):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    исходный_daemon_yaml = (home / "daemon.yaml").read_text(encoding="utf-8")
    assert "gate_secret заполняется автоматически" in исходный_daemon_yaml

    main(["base", "list", "--home", str(home)])

    assert (home / "daemon.yaml").read_text(encoding="utf-8") == исходный_daemon_yaml


# --- пять входов, дававших необработанный след стека вместо сообщения (правка 2) ---


def test_base_import_кодировка_cp1251_переносит_кириллическую_подпись(tmp_path, capsys):
    """Регресс: файл окружения прежнего сервера 1c-odata-mcp мог остаться в cp1251 (типично
    для старых версий на локализованной Windows) — чтение как utf-8 роняло
    UnicodeDecodeError на первой же кириллической подписи базы, а команда переноса ради
    этого входа и существует."""
    home = tmp_path / "home"
    env = tmp_path / "1c-odata.env"
    содержимое = (
        "ODATA_DB_UT_BASE_URL=http://x.invalid/ut/odata/standard.odata/\n"
        "ODATA_DB_UT_LABEL=Управление торговлей, боевая\n"
    )
    env.write_bytes(содержимое.encode("cp1251"))

    код = main(["base", "import", str(env), "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "перенесена база" in вывод
    config = load_config(home)
    assert config.bases["ut"].label == "Управление торговлей, боевая"


def test_base_import_неизвестная_кодировка_даёт_ошибку_а_не_трейсбек(tmp_path, capsys):
    home = tmp_path / "home"
    env = tmp_path / "1c-odata.env"
    env.write_bytes(b"\x98ODATA_DB_UT_BASE_URL=http://x.invalid/ut/odata/standard.odata/\n")

    код = main(["base", "import", str(env), "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "config_invalid" in вывод


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_base_test_сертификат_не_найден_даёт_ошибку_а_не_трейсбек(tmp_path, capsys):
    """filterwarnings глушит отдельный от этой правки долг httpx (verify=<строка> устарел
    как API) — без него DeprecationWarning, ставший ошибкой в настройках тестов проекта,
    перехватывает выполнение раньше, чем код доходит до проверяемого поведения."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    сертификат = (tmp_path / "нет_такого.pem").as_posix()
    (home / "bases.yaml").write_text(
        "bases:\n"
        "  ut:\n"
        "    label: УТ\n"
        f"    url: {URL}\n"
        "    user: u\n"
        "    password: p\n"
        "    role: test\n"
        f'    verify_tls: "{сертификат}"\n',
        encoding="utf-8",
    )

    код = main(["base", "test", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "odata_error" in вывод
    assert "нет_такого.pem" in вывод


def test_base_list_ключ_name_внутри_записи_базы_даёт_ошибку_а_не_трейсбек(tmp_path, capsys):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(
        "bases:\n"
        "  ut:\n"
        "    name: другое\n"
        "    label: УТ\n"
        f"    url: {URL}\n"
        "    user: u\n"
        "    password: p\n"
        "    role: prod\n",
        encoding="utf-8",
    )

    код = main(["base", "list", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "config_invalid" in вывод


def test_base_list_default_списком_даёт_ошибку_а_не_трейсбек(tmp_path, capsys):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(
        "default: [ut, buh]\n"
        "bases:\n"
        "  ut:\n"
        "    label: УТ\n"
        f"    url: {URL}\n"
        "    user: u\n"
        "    password: p\n"
        "    role: prod\n",
        encoding="utf-8",
    )

    код = main(["base", "list", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "config_invalid" in вывод


def test_base_list_запись_базы_строкой_даёт_ошибку_а_не_трейсбек(tmp_path, capsys):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text("bases:\n  ut: просто_строка\n", encoding="utf-8")

    код = main(["base", "list", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "config_invalid" in вывод


# --- правка 8: адрес без учётных данных в выводе, предупреждение о правах у всех команд ---


def test_base_import_печатает_адрес_без_учётных_данных(tmp_path, capsys):
    home = tmp_path / "home"
    env = tmp_path / "1c-odata.env"
    env.write_text(
        "ODATA_DB_UT_BASE_URL=https://имя:пароль@1c.corp.local/ut/odata/standard.odata/\n",
        encoding="utf-8",
    )

    код = main(["base", "import", str(env), "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "пароль" not in вывод
    assert "имя:пароль" not in вывод
    assert "1c.corp.local" in вывод
    config = load_config(home)
    assert config.bases["ut"].url == "https://имя:пароль@1c.corp.local/ut/odata/standard.odata/"


@respx.mock
def test_base_test_печатает_предупреждение_о_правах(tmp_path, capsys, monkeypatch):
    """Регресс: предупреждение о широких правах на bases.yaml собиралось верно, но печатала
    его только base list — base test (и остальные команды, читающие настройки) молчали."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    исходная_load_config = cli.load_config

    def с_предупреждением(путь):
        конфигурация = исходная_load_config(путь)
        конфигурация.warnings.append("bases.yaml доступен другим учётным записям")
        return конфигурация

    monkeypatch.setattr(cli, "load_config", с_предупреждением)
    respx.get(f"{URL}$metadata").mock(
        return_value=httpx.Response(
            200, text="<edmx:Edmx/>", headers={"Content-Type": "application/xml"}
        )
    )
    respx.get(URL).mock(return_value=httpx.Response(200, json={"value": []}))

    код = main(["base", "test", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "предупреждение" in вывод
    assert "доступен другим учётным записям" in вывод
