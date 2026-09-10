"""`odata1c mcp` (план M1d, задача 6, раунд правок 1): разбор аргументов
`--bases`/`--default`/`--url` и их передача в `run_launcher` — без реального запуска лаунчера
(тот проверяется в памяти `tests/unit/test_launcher.py` и по-настоящему
`tests/integration/test_end_to_end.py`). `run_launcher` подменяется, чтобы этот файл проверял
ровно разбор argparse и сборку `cmd_mcp`, а не сеть/stdio/демон.

Раунд правок 1 добавил сюда: валидацию `--bases`/`--default` до сети (находки 4 и 6, Ruling 11)
и то, что ЛЮБАЯ ошибка этой команды уходит в stderr, а не в stdout — тот у `mcp` является каналом
JSON-RPC клиента (находки 3б, 4)."""

import httpx2
import pytest
from mcp.shared.exceptions import MCPError

import odata1c.cli as cli
from odata1c.cli import main


def _подменить_run_launcher(monkeypatch):
    вызовы = []

    async def поддельный(home, *, bases, default, url):
        вызовы.append({"home": home, "bases": bases, "default": default, "url": url})

    monkeypatch.setattr(cli, "run_launcher", поддельный)
    return вызовы


def test_mcp_без_аргументов_bases_и_default_не_заданы(tmp_path, monkeypatch):
    home = tmp_path / "home"
    вызовы = _подменить_run_launcher(monkeypatch)

    код = main(["--home", str(home), "mcp"])

    assert код == 0
    assert вызовы == [{"home": home, "bases": None, "default": None, "url": None}]


def test_mcp_bases_через_запятую_разбирается_в_список_с_обрезкой_пробелов(tmp_path, monkeypatch):
    home = tmp_path / "home"
    вызовы = _подменить_run_launcher(monkeypatch)

    код = main(["--home", str(home), "mcp", "--bases", "ut, buh", "--default", "ut"])

    assert код == 0
    assert вызовы[0]["bases"] == ["ut", "buh"]
    assert вызовы[0]["default"] == "ut"


def test_mcp_url_передаётся_как_есть(tmp_path, monkeypatch):
    home = tmp_path / "home"
    вызовы = _подменить_run_launcher(monkeypatch)

    код = main(["--home", str(home), "mcp", "--url", "http://127.0.0.1:7171/mcp"])

    assert код == 0
    assert вызовы[0]["url"] == "http://127.0.0.1:7171/mcp"
    assert вызовы[0]["bases"] is None


# -------------------------------------------------------------------------------------------
# Находка 6 / Ruling 11: --bases задан, но ни одного имени не распознано — ошибка, а не
# «видно всё» и не «видно ничего» (старый код `[...] if bases else None` трактовал
# `--bases ","` как «видно ничего», `--bases ""` как «видно всё» — оба случая теперь делают
# `run_launcher` недостижимым вовсе, что уже отличает новое поведение от старого).
# -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("сырое", ["", ",", " , ", ",,"])
def test_mcp_bases_пустой_после_разбора_это_ошибка_а_не_молчаливая_трактовка(
    сырое, tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    вызовы = _подменить_run_launcher(monkeypatch)

    код = main(["--home", str(home), "mcp", "--bases", сырое])

    assert код == 1
    assert вызовы == []  # run_launcher вообще не вызван — отказ до сети
    вывод = capsys.readouterr()
    assert вывод.out == ""  # ни строки об ошибке в stdout (канал JSON-RPC клиента)
    assert "config_invalid" in вывод.err or "не содержит ни одного имени" in вывод.err


def test_mcp_bases_none_попрежнему_означает_видимость_не_сужена(tmp_path, monkeypatch):
    """Отсутствие `--bases` совсем — не тот же случай, что пустое значение: видимость не
    сужена, `run_launcher` вызывается с `bases=None` (регрессия сюда бы означала, что правка
    Ruling 11 задела и этот, незатронутый находкой, случай)."""
    home = tmp_path / "home"
    вызовы = _подменить_run_launcher(monkeypatch)

    код = main(["--home", str(home), "mcp"])

    assert код == 0
    assert вызовы[0]["bases"] is None


# -------------------------------------------------------------------------------------------
# Находка 4, проявление 3: не-ASCII/иначе неверное имя в --bases или --default — понятная
# ошибка до сети, а не UnicodeEncodeError внутри конструктора httpx2.AsyncClient.
# -------------------------------------------------------------------------------------------


def test_mcp_bases_нелатинское_имя_отклоняется_до_сети(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    вызовы = _подменить_run_launcher(monkeypatch)

    код = main(["--home", str(home), "mcp", "--bases", "ют"])

    assert код == 1
    assert вызовы == []
    вывод = capsys.readouterr()
    assert вывод.out == ""
    assert "не похоже на имя базы" in вывод.err


def test_mcp_default_нелатинское_имя_отклоняется_до_сети(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    вызовы = _подменить_run_launcher(monkeypatch)

    код = main(["--home", str(home), "mcp", "--default", "УТ"])

    assert код == 1
    assert вызовы == []
    вывод = capsys.readouterr()
    assert вывод.out == ""
    assert "не похоже на имя базы" in вывод.err


# -------------------------------------------------------------------------------------------
# Находка 3б: диагностика команды mcp — только stderr, никогда stdout (тот занят под JSON-RPC).
# -------------------------------------------------------------------------------------------


def test_mcp_ошибка_запуска_печатается_в_stderr_а_не_в_stdout(tmp_path, monkeypatch, capsys):
    async def падающий(home, *, bases, default, url):
        raise MCPError(code=-32000, message="Server returned an error response")

    monkeypatch.setattr(cli, "run_launcher", падающий)

    код = main(["--home", str(tmp_path / "home"), "mcp"])

    assert код == 1
    вывод = capsys.readouterr()
    assert вывод.out == ""
    assert "Server returned an error response" in вывод.err


def test_mcp_ошибка_транспорта_httpx_печатается_в_stderr(tmp_path, monkeypatch, capsys):
    async def падающий(home, *, bases, default, url):
        raise httpx2.UnsupportedProtocol(
            "Request URL is missing an 'http://' or 'https://' protocol."
        )

    monkeypatch.setattr(cli, "run_launcher", падающий)

    код = main(["--home", str(tmp_path / "home"), "mcp", "--url", "127.0.0.1:7171/mcp"])

    assert код == 1
    вывод = capsys.readouterr()
    assert вывод.out == ""
    assert вывод.err != ""


def test_mcp_systemexit_из_run_launcher_возвращает_тот_же_код(tmp_path, monkeypatch, capsys):
    """`_дождаться_демона` лаунчера сам печатает причину в stderr и поднимает `SystemExit(1)` —
    `cmd_mcp` не должен печатать ничего сверху, только вернуть тот же код возврата."""

    async def падающий(home, *, bases, default, url):
        raise SystemExit(1)

    monkeypatch.setattr(cli, "run_launcher", падающий)

    код = main(["--home", str(tmp_path / "home"), "mcp"])

    assert код == 1
    вывод = capsys.readouterr()
    assert вывод.out == ""
    assert вывод.err == ""
