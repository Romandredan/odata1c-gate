"""Карта кода: файл → класс/функция → строки «от–до» → первая строка докстринга.

Зачем: `write/service.py` и `gate/masking.py` — тысячи строк, и агент, не зная, где нужная
функция, читает файл кусками подряд. Карта отвечает на вопрос «где» за одно чтение, после чего
открывается ровно нужный диапазон строк (`Read` с `offset`/`limit`).

Запуск: `uv run python tools/code_map.py [каталог ...] [--out build/code-map]`.
Без аргументов — `src/odata1c`, `plugin/hooks`, `tools`; пакет делится на карты по подпакетам
(`build/code-map/src-odata1c-write.md` и так далее). Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
ПО_УМОЛЧАНИЮ = ("src/odata1c", "plugin/hooks", "tools")
ПРЕДЕЛ_ДОКСТРИНГА = 110


def _первая_строка(узел: ast.AST) -> str:
    текст = ast.get_docstring(узел, clean=True) or ""
    строка = текст.strip().splitlines()[0] if текст.strip() else ""
    return строка[:ПРЕДЕЛ_ДОКСТРИНГА]


def _строки_узла(узел: ast.AST, отступ: int) -> list[str]:
    итог: list[str] = []
    for дочерний in getattr(узел, "body", []):
        if isinstance(дочерний, ast.ClassDef):
            вид = "class"
        elif isinstance(дочерний, ast.FunctionDef | ast.AsyncFunctionDef):
            вид = "async def" if isinstance(дочерний, ast.AsyncFunctionDef) else "def"
        else:
            continue
        описание = _первая_строка(дочерний)
        хвост = f" — {описание}" if описание else ""
        итог.append(
            f"{'  ' * отступ}- `{вид} {дочерний.name}` "
            f"{дочерний.lineno}–{дочерний.end_lineno}{хвост}"
        )
        if isinstance(дочерний, ast.ClassDef):
            итог.extend(_строки_узла(дочерний, отступ + 1))
    return итог


def карта_файла(путь: pathlib.Path) -> list[str]:
    исходник = путь.read_text(encoding="utf-8")
    дерево = ast.parse(исходник)
    относительный = путь.relative_to(КОРЕНЬ).as_posix()
    число_строк = исходник.count("\n") + 1
    описание = _первая_строка(дерево)
    заголовок = f"## `{относительный}` ({число_строк} строк)"
    строки = [заголовок]
    if описание:
        строки.append(описание)
    строки.extend(_строки_узла(дерево, 0))
    строки.append("")
    return строки


def построить(файлы: list[pathlib.Path]) -> str:
    строки = [
        "# Карта кода odata1c-gate",
        "",
        "Сгенерировано `tools/code_map.py`. Формат: вид и имя, строки «от–до», первая строка "
        "докстринга. Нужную функцию открывайте по диапазону, а не чтением файла подряд.",
        "",
    ]
    for путь in файлы:
        if "__pycache__" not in путь.parts:
            строки.extend(карта_файла(путь))
    return "\n".join(строки)


def _части(каталог: str) -> list[tuple[str, list[pathlib.Path]]]:
    """Каталог пакета делится на подпакеты и модули верхнего уровня: одна карта — один слой,
    чтобы агент брал только свой (карта всего пакета — 170 КБ, слоя записи — 22 КБ)."""
    корень = КОРЕНЬ / каталог
    подпакеты = sorted(п for п in корень.iterdir() if п.is_dir() and any(п.glob("*.py")))
    if not подпакеты:
        return [(каталог, sorted(корень.rglob("*.py")))]
    части = [(п.relative_to(КОРЕНЬ).as_posix(), sorted(п.rglob("*.py"))) for п in подпакеты]
    части.append((каталог + "-верхний-уровень", sorted(корень.glob("*.py"))))
    return части


def main(argv: list[str] | None = None) -> int:
    разбор = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    разбор.add_argument("каталоги", nargs="*", default=list(ПО_УМОЛЧАНИЮ))
    разбор.add_argument("--out", default="build/code-map", help="каталог для карт")
    аргументы = разбор.parse_args(argv)
    выход = КОРЕНЬ / аргументы.out
    выход.mkdir(parents=True, exist_ok=True)
    for каталог in аргументы.каталоги:
        for имя, файлы in _части(каталог):
            текст = построить(файлы)
            файл = выход / (имя.replace("/", "-") + ".md")
            файл.write_text(текст, encoding="utf-8")
            размер = len(текст.encode("utf-8")) // 1024
            print(f"{файл.relative_to(КОРЕНЬ).as_posix()}: {размер} КБ", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
