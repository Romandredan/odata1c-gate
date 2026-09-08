"""Команды CLI: init, base list, base test."""

import httpx
import respx

from odata1c.cli import main

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
