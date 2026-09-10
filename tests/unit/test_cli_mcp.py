"""`odata1c mcp` (план M1d, задача 6): разбор аргументов `--bases`/`--default`/`--url` и их
передача в `run_launcher` — без реального запуска лаунчера (тот проверяется в памяти
`tests/unit/test_launcher.py` и по-настоящему `tests/integration/test_end_to_end.py`).
`run_launcher` подменяется, чтобы этот файл проверял ровно разбор argparse и сборку
`cmd_mcp`, а не сеть/stdio/демон.
"""

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
