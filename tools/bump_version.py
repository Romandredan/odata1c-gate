"""Переписать версию проекта одновременно в четырёх местах поставки.

Единственный источник версии — `src/odata1c/__about__.py`; в манифестах плагина и
маркетплейса она только повторяется (`tests/unit/test_versions_agree.py` проверяет равенство).
Запуск: `uv run python tools/bump_version.py <версия>`.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re

ШАБЛОН_ВЕРСИИ = re.compile(r"^\d+\.\d+\.\d+(\.dev\d+|rc\d+)?$")


def _записать_json(путь: pathlib.Path, данные: dict) -> None:
    путь.write_text(json.dumps(данные, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def bump(root: pathlib.Path, версия: str) -> list[pathlib.Path]:
    """Переписать версию в четырёх файлах поставки под `root` и вернуть их пути.

    Некорректный формат версии — `SystemExit(2)` без изменения файлов.
    """
    if not ШАБЛОН_ВЕРСИИ.match(версия):
        raise SystemExit(2)

    about_путь = root / "src/odata1c/__about__.py"
    about_путь.write_text(f'__version__ = "{версия}"\n', encoding="utf-8")

    plugin_json_путь = root / "plugin/.claude-plugin/plugin.json"
    plugin_json = json.loads(plugin_json_путь.read_text("utf-8"))
    plugin_json["version"] = версия
    _записать_json(plugin_json_путь, plugin_json)

    mcp_json_путь = root / "plugin/.mcp.json"
    mcp_json = json.loads(mcp_json_путь.read_text("utf-8"))
    mcp_json["gate"]["args"][1] = f"odata1c-gate=={версия}"
    _записать_json(mcp_json_путь, mcp_json)

    marketplace_путь = root / ".claude-plugin/marketplace.json"
    marketplace = json.loads(marketplace_путь.read_text("utf-8"))
    marketplace["plugins"][0]["version"] = версия
    _записать_json(marketplace_путь, marketplace)

    return [about_путь, plugin_json_путь, mcp_json_путь, marketplace_путь]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("версия", help="например 0.1.0, 0.1.0.dev0, 0.1.0rc1")
    args = parser.parse_args()

    root = pathlib.Path(__file__).resolve().parents[1]
    for путь in bump(root, args.версия):
        print(путь)


if __name__ == "__main__":
    main()
