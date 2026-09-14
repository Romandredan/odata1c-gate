"""Копия `plugin/` для проверок и evals до публикации на PyPI.

Основной `.mcp.json` плагина закрепляет `uvx --from odata1c-gate==<версия>`, но до выпуска на
PyPI этого колеса ещё нет. Копия заменяет запуск на `uv run --directory <корень репозитория>
odata1c mcp` — тот же шлюз из рабочей копии, без публикации.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil

ОТНОСИТЕЛЬНЫЙ_ВЫВОД_ПО_УМОЛЧАНИЮ = "build/plugin-dev"


def dev_copy(plugin_dir: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    """Скопировать `plugin_dir` в `out` и заменить в копии `.mcp.json` на локальный запуск."""
    shutil.copytree(plugin_dir, out, dirs_exist_ok=True)

    корень_репозитория = plugin_dir.resolve().parent
    mcp_json = {
        "gate": {
            "type": "stdio",
            "command": "uv",
            "args": ["run", "--directory", str(корень_репозитория), "odata1c", "mcp"],
        }
    }
    (out / ".mcp.json").write_text(
        json.dumps(mcp_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=ОТНОСИТЕЛЬНЫЙ_ВЫВОД_ПО_УМОЛЧАНИЮ)
    args = parser.parse_args()

    корень_репозитория = pathlib.Path(__file__).resolve().parents[1]
    plugin_dir = корень_репозитория / "plugin"

    out = pathlib.Path(args.out)
    if not out.is_absolute():
        out = корень_репозитория / out

    dev_copy(plugin_dir, out)
    print(out)


if __name__ == "__main__":
    main()
