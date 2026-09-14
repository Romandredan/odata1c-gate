import json
import pathlib
import shutil

import pytest

from tools.bump_version import bump
from tools.plugin_dev_copy import dev_copy

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]

ОТНОСИТЕЛЬНЫЕ_ПУТИ_ВЕРСИИ = (
    pathlib.Path("src/odata1c/__about__.py"),
    pathlib.Path("plugin/.claude-plugin/plugin.json"),
    pathlib.Path("plugin/.mcp.json"),
    pathlib.Path(".claude-plugin/marketplace.json"),
)


def _скопировать_четыре_файла(root: pathlib.Path) -> pathlib.Path:
    for относительный in ОТНОСИТЕЛЬНЫЕ_ПУТИ_ВЕРСИИ:
        назначение = root / относительный
        назначение.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(КОРЕНЬ / относительный, назначение)
    return root


def test_bump_переписывает_версию_в_четырёх_местах(tmp_path):
    root = _скопировать_четыре_файла(tmp_path)

    результат = bump(root, "0.1.0")

    assert len(результат) == 4

    about_текст = (root / "src/odata1c/__about__.py").read_text("utf-8")
    assert about_текст == '__version__ = "0.1.0"\n'

    plugin_json = json.loads((root / "plugin/.claude-plugin/plugin.json").read_text("utf-8"))
    assert plugin_json["version"] == "0.1.0"

    mcp_json = json.loads((root / "plugin/.mcp.json").read_text("utf-8"))
    assert mcp_json["gate"]["args"] == ["--from", "odata1c-gate==0.1.0", "odata1c", "mcp"]

    marketplace = json.loads((root / ".claude-plugin/marketplace.json").read_text("utf-8"))
    assert marketplace["plugins"][0]["version"] == "0.1.0"


def test_bump_отклоняет_версию_неверного_формата(tmp_path):
    root = _скопировать_четыре_файла(tmp_path)

    with pytest.raises(SystemExit) as ошибка:
        bump(root, "1.0")

    assert ошибка.value.code == 2
    # файл не тронут — версия осталась ровно той, что была скопирована из репозитория
    исходный = (КОРЕНЬ / "src/odata1c/__about__.py").read_text("utf-8")
    about_текст = (root / "src/odata1c/__about__.py").read_text("utf-8")
    assert about_текст == исходный


def test_dev_copy_локальный_mcp_json_и_побайтная_копия_plugin_json(tmp_path):
    out = tmp_path / "plugin-dev"

    dev_copy(КОРЕНЬ / "plugin", out)

    mcp_json = json.loads((out / ".mcp.json").read_text("utf-8"))
    assert mcp_json["gate"]["command"] == "uv"

    исходный = (КОРЕНЬ / "plugin/.claude-plugin/plugin.json").read_bytes()
    копия = (out / ".claude-plugin/plugin.json").read_bytes()
    assert копия == исходный
