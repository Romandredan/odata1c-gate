import json
import pathlib

from odata1c import __version__

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]


def test_plugin_json_version_равна_версии_пакета():
    манифест = json.loads((КОРЕНЬ / "plugin/.claude-plugin/plugin.json").read_text("utf-8"))
    assert манифест["name"] == "odata1c"
    assert манифест["version"] == __version__


def test_mcp_json_закрепляет_ту_же_версию_пакета():
    mcp = json.loads((КОРЕНЬ / "plugin/.mcp.json").read_text("utf-8"))
    gate = mcp["gate"]
    assert gate["command"] == "uvx"
    assert gate["args"] == ["--from", f"odata1c-gate=={__version__}", "odata1c", "mcp"]


def test_marketplace_version_равна_версии_пакета():
    рынок = json.loads((КОРЕНЬ / ".claude-plugin/marketplace.json").read_text("utf-8"))
    (плагин,) = рынок["plugins"]
    assert плагин["name"] == "odata1c"
    assert плагин["source"] == "./plugin"
    assert плагин["version"] == __version__


def test_pyproject_называет_дистрибутив_odata1c_gate():
    текст = (КОРЕНЬ / "pyproject.toml").read_text("utf-8")
    assert 'name = "odata1c-gate"' in текст
    assert 'odata1c = "odata1c.cli:main"' in текст
