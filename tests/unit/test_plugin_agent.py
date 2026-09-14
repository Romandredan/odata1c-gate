"""Агент плагина `odata1c-investigator` (M3 задача 6, `plugin/agents/odata1c-investigator.md`):
следователь только чтением, который работает через навык `odata1c:odata1c`.

Тест проверяет frontmatter (`name`, `model: haiku`, точный набор `tools` — только `Skill`,
`ToolSearch` и тулы чтения шлюза `odata1c` в обеих формах имени: `mcp__plugin_odata1c_gate__…` при
установке плагином и `mcp__odata1c__…` при ручном подключении сервера, SPEC §11.2) и то, что тело
первым действием загружает навык и прямо запрещает запись, раскрытие токенов и чтение файлов
шлюза. Файл — не пакет `odata1c`, а часть каталога `plugin/`, поэтому путь ищется от корня
репозитория, а не через `importlib.resources`.
"""

from __future__ import annotations

import pathlib
import re

import yaml

КОРЕНЬ = pathlib.Path(__file__).resolve().parent.parent.parent
ФАЙЛ_АГЕНТА = КОРЕНЬ / "plugin" / "agents" / "odata1c-investigator.md"

# Frontmatter — однострочные YAML-значения (как у глобальных агентов владельца), поэтому парсим
# регулярным выражением, а не построчным split: устойчиво к CRLF и не зависит от количества `---`
# внутри самого текста description.
_FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(?P<frontmatter>.*?)\r?\n---\r?\n(?P<body>.*)\Z", re.DOTALL
)

ТУЛЫ_ЧТЕНИЯ = (
    "bases",
    "find_entity",
    "describe_entity",
    "query",
    "get",
    "info",
    "recipe",
    "raw_get",
)

ОЖИДАЕМЫЕ_ТУЛЫ = frozenset(
    {"Skill", "ToolSearch"}
    | {f"mcp__plugin_odata1c_gate__odata1c_{тул}" for тул in ТУЛЫ_ЧТЕНИЯ}
    | {f"mcp__odata1c__odata1c_{тул}" for тул in ТУЛЫ_ЧТЕНИЯ}
)

# Тулы записи, reindex и файловый/командный доступ — этому агенту нельзя ни одного из них
# (AGENTS.md, инвариант 2; бриф задачи 6).
ЗАПРЕЩЁННЫЕ_ТУЛЫ = (
    "odata1c_create",
    "odata1c_update",
    "odata1c_mark_for_deletion",
    "odata1c_action",
    "odata1c_undo",
    "odata1c_commit",
    "odata1c_journal",
    "odata1c_reindex",
    "Bash",
    "Write",
    "Edit",
    "Read",
    "Grep",
    "Glob",
)


def _текст_агента() -> str:
    assert ФАЙЛ_АГЕНТА.exists(), f"нет файла агента: {ФАЙЛ_АГЕНТА}"
    return ФАЙЛ_АГЕНТА.read_text(encoding="utf-8")


def _разобрать(текст: str) -> tuple[dict, str]:
    совпадение = _FRONTMATTER_RE.match(текст)
    assert совпадение, "не нашли YAML-frontmatter в файле агента"
    frontmatter = yaml.safe_load(совпадение.group("frontmatter"))
    assert isinstance(frontmatter, dict)
    return frontmatter, совпадение.group("body")


def _тулы_агента(frontmatter: dict) -> set[str]:
    строка = frontmatter["tools"]
    assert isinstance(строка, str), (
        "tools должен быть строкой через запятую, как у глобальных агентов"
    )
    return {часть.strip() for часть in строка.split(",")}


def test_frontmatter_имя_и_модель():
    frontmatter, _ = _разобрать(_текст_агента())
    assert frontmatter["name"] == "odata1c-investigator"
    assert frontmatter["model"] == "haiku"


def test_frontmatter_tools_ровно_чтение_шлюза_в_обеих_формах():
    frontmatter, _ = _разобрать(_текст_агента())
    assert _тулы_агента(frontmatter) == ОЖИДАЕМЫЕ_ТУЛЫ


def test_frontmatter_tools_без_записи_reindex_и_файлового_доступа():
    frontmatter, _ = _разобрать(_текст_агента())
    тулы = _тулы_агента(frontmatter)
    for запрещённый in ЗАПРЕЩЁННЫЕ_ТУЛЫ:
        assert запрещённый not in тулы, f"тул {запрещённый} не должен быть у следователя"


def test_frontmatter_description_на_русском_с_тремя_примерами():
    frontmatter, _ = _разобрать(_текст_агента())
    описание = frontmatter["description"]
    assert isinstance(описание, str) and описание.strip()
    assert описание.count("<example>") == 3
    assert описание.count("</example>") == 3
    assert any(ord(символ) > 127 for символ in описание), "описание должно быть на русском"


def test_тело_первым_действием_загружает_навык_odata1c():
    _, тело = _разобрать(_текст_агента())
    assert "Skill" in тело
    assert "odata1c:odata1c" in тело


def test_тело_прямо_запрещает_commit_раскрытие_токенов_и_файлы_шлюза():
    _, тело = _разобрать(_текст_агента())
    нижний_регистр = тело.lower()
    assert "commit" in нижний_регистр
    assert "токен" in нижний_регистр
    assert "bases.yaml" in тело or "policy.yaml" in тело or "файл" in нижний_регистр
