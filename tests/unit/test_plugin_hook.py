"""Хук `PreToolUse` плагина (`plugin/hooks/pretooluse.py`, M3 задача 4, design-doc §5).

Скрипт — один файл на стандартной библиотеке Python без зависимостей проекта: тесты запускают
его настоящим подпроцессом (`sys.executable`, не импорт) — это то же, чем его запустит Claude
Code, и заодно доказывает отсутствие импортов пакета `odata1c`. Ввод — JSON на stdin, как отдаёт
Claude Code событию `PreToolUse`; вывод — JSON `hookSpecificOutput` или пустая строка, код
возврата всегда 0 (хук никогда не должен падать кодом 2 — это остановило бы тул сильнее, чем
осознанный `deny`).

Каждый тест собирает окружение подпроцесса сам (`_окружение`): по умолчанию `ODATA1C_HOME`
указывает на заведомо посторонний временный каталог, чтобы «домашний» путь по умолчанию
(`~/.claude/odata1c` настоящего пользователя) никогда не участвовал в сравнении — единственное
исключение оговорено явно в тесте на `~`."""

import json
import os
import pathlib
import subprocess
import sys

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
СКРИПТ = КОРЕНЬ / "plugin" / "hooks" / "pretooluse.py"


# Скрипт сам переводит свои stdin/stdout в UTF-8 (`main`, `.reconfigure`) — не должен зависеть от
# того, как была запущена сама тестовая сессия. Эти переменные убираются из окружения подпроцесса
# намеренно: если бы `.reconfigure` не сработал, тесты с кириллицей в путях (ниже, домашний
# каталог "чужой_дом") ловили бы молчание вместо ожидаемого решения даже на Windows с не-UTF-8
# кодовой страницей консоли — по умолчанию, без специальной настройки хоста.
_ПЕРЕМЕННЫЕ_КОДИРОВКИ = ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO")


def _окружение(tmp_path: pathlib.Path, **переопределения: str) -> dict[str, str]:
    """Окружение подпроцесса: копия текущего (нужны `PATH`/`SystemRoot` для запуска интерпретатора
    на Windows) без переменных, которые вручную форсируют UTF-8 в дочернем процессе, с
    `ODATA1C_HOME`, указанным на посторонний временный каталог по умолчанию, и любыми точечными
    переопределениями теста."""
    окружение = dict(os.environ)
    for имя in _ПЕРЕМЕННЫЕ_КОДИРОВКИ:
        окружение.pop(имя, None)
    окружение["ODATA1C_HOME"] = str(tmp_path / "чужой_дом" / "odata1c")
    окружение.update(переопределения)
    return окружение


def _запустить(событие: dict, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(СКРИПТ)],
        input=json.dumps(событие, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=10,
    )


def _запустить_текст(текст: str, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(СКРИПТ)],
        input=текст,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=10,
    )


def _решение(результат: subprocess.CompletedProcess[str]) -> dict | None:
    assert результат.returncode == 0, результат.stderr
    вывод = результат.stdout.strip()
    if not вывод:
        return None
    return json.loads(вывод)["hookSpecificOutput"]


# --- Правило 1: odata1c_commit → ask -----------------------------------------------------


@pytest.mark.parametrize(
    "тул",
    ["mcp__plugin_odata1c_gate__odata1c_commit", "mcp__odata1c__odata1c_commit"],
)
def test_commit_ask_с_pending_id(тул, tmp_path):
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"pending_id": "abc-1"}},
        env=_окружение(tmp_path),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "ask"
    assert "pending abc-1" in решение["permissionDecisionReason"]


def test_commit_другого_сервера_молчит(tmp_path):
    результат = _запустить(
        {"tool_name": "mcp__other__odata1c_commit", "tool_input": {"pending_id": "abc-1"}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


# --- Правило 2: файлы домашнего каталога шлюза → deny -------------------------------------

_ИМЕНА_ФАЙЛОВ_ДОМА = [
    "bases.yaml",
    "daemon.yaml",
    "launcher.key",
    "gate.sqlite",
    "gate.sqlite-wal",
    "journal.sqlite",
    "journal.sqlite-wal",
]

_ТУЛЫ_И_ПОЛЯ = [
    ("Read", "file_path"),
    ("Edit", "file_path"),
    ("Write", "file_path"),
    ("NotebookEdit", "notebook_path"),
    ("Grep", "path"),
    ("Glob", "path"),
]


@pytest.mark.parametrize("имя", _ИМЕНА_ФАЙЛОВ_ДОМА)
@pytest.mark.parametrize("тул,поле", _ТУЛЫ_И_ПОЛЯ)
def test_файлы_дома_deny(тул, поле, имя, tmp_path):
    дом = tmp_path / "odata1c"
    путь = дом / имя

    результат = _запустить(
        {"tool_name": тул, "tool_input": {поле: str(путь)}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


def test_multiedit_дома_deny(tmp_path):
    """`MultiEdit` не входит в параметризацию выше (там один универсальный список полей на все
    случаи), но матчер `hooks.json` его перечисляет отдельно от `Edit` — правило 2 обязано
    сработать и на нём, с тем же полем `file_path`, что и у `Edit`."""
    дом = tmp_path / "odata1c"
    путь = дом / "bases.yaml"

    результат = _запустить(
        {"tool_name": "MultiEdit", "tool_input": {"file_path": str(путь)}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


def test_путь_с_тильдой_и_обратными_косыми_deny(tmp_path):
    """`ODATA1C_HOME` не задан вовсе — дом берётся по умолчанию (`~/.claude/odata1c`), а путь из
    события написан в стиле Windows-команды (`~\\...`), а не как отдаёт его сам Claude Code
    (обычно POSIX-стиль `/`): normalisation должна понимать оба написания `~`."""
    окружение = dict(os.environ)
    for имя in _ПЕРЕМЕННЫЕ_КОДИРОВКИ:
        окружение.pop(имя, None)
    окружение.pop("ODATA1C_HOME", None)
    окружение["HOME"] = str(tmp_path)
    окружение["USERPROFILE"] = str(tmp_path)

    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": "~\\.claude\\odata1c\\bases.yaml"}},
        env=окружение,
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


def test_bases_yaml_вне_дома_разрешён(tmp_path):
    путь = tmp_path / "проект" / "bases.yaml"

    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": str(путь)}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_policy_и_recipes_разрешены(tmp_path):
    дом = tmp_path / "odata1c"
    env = _окружение(tmp_path, ODATA1C_HOME=str(дом))

    for путь in (дом / "bases" / "x" / "policy.yaml", дом / "recipes" / "ut" / "a.yaml"):
        результат = _запустить(
            {"tool_name": "Read", "tool_input": {"file_path": str(путь)}},
            env=env,
        )
        assert результат.returncode == 0
        assert результат.stdout.strip() == "", путь


def test_путь_posix_без_буквы_диска_deny_через_части_пути(tmp_path):
    """Путь в стиле Git Bash (`/c/Users/...`, без буквы диска) — на Windows `abspath` разворачивает
    его от текущего диска буквально (`/c` становится обычным каталогом `c`, а не буквой диска), так
    что сравнение «путь начинается с дома» не совпадёт ни при каком `ODATA1C_HOME`. `ODATA1C_HOME`
    здесь указывает на заведомо другой каталог (`_окружение`) — deny обязан держаться на запасном
    случае `_внутри_дома_шлюза` (совпадение `odata1c` и `.claude` среди частей пути), а не на
    первом. Без этого теста эта ветка кода ничем не подтверждена."""
    результат = _запустить(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "/c/Users/u/.claude/odata1c/bases.yaml"},
        },
        env=_окружение(tmp_path),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


# --- Правило 3: Bash --------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "cat ~/.claude/odata1c/bases.yaml",
        "odata1c reveal '[[inn:1]]'",
        "uv run odata1c reveal x",
        "sqlite3 /home/u/.claude/odata1c/gate.sqlite .dump",
        "type C:\\Users\\u\\.claude\\odata1c\\daemon.yaml",
        # Имя переменной окружения в непроинтерполированной команде пишут заглавными буквами
        # (`$ODATA1C_HOME`) — проверка «odata1c в команде» не должна требовать точного регистра.
        "cat $ODATA1C_HOME/bases.yaml",
    ],
)
def test_bash_deny(cmd, tmp_path):
    результат = _запустить(
        {"tool_name": "Bash", "tool_input": {"command": cmd}},
        env=_окружение(tmp_path),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "cmd",
    [
        "odata1c policy show trade_dev",
        "odata1c recipe check ut",
        "cat README.md",
        "grep gate_secret docs/",
    ],
)
def test_bash_разрешён(cmd, tmp_path):
    результат = _запустить(
        {"tool_name": "Bash", "tool_input": {"command": cmd}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


# --- Правило 4 и устойчивость ко входу -----------------------------------------------------


def test_битый_json_молчит_код_0(tmp_path):
    результат = _запустить_текст("это не json {{{", env=_окружение(tmp_path))

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_пустой_stdin_молчит_код_0(tmp_path):
    результат = _запустить_текст("", env=_окружение(tmp_path))

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_json_не_объект_молчит_код_0(tmp_path):
    результат = _запустить_текст("[1, 2, 3]", env=_окружение(tmp_path))

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_нет_полей_молчит(tmp_path):
    результат = _запустить({}, env=_окружение(tmp_path))

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_путь_с_null_байтом_не_роняет_хук(tmp_path):
    """`os.path.abspath` на строке со встроенным нулевым байтом бросает `ValueError` (это валидный
    JSON-вход: `\\u0000` — обычный код в строке JSON). Базовое имя ("bases.yaml") до нулевого байта
    не задето — проверка членства в списке защищённых файлов проходит, и нормализация пути
    действительно вызывается. Интерфейс задачи требует код 0 всегда: сбой внутри `decide` не
    должен превращаться в трассировку и код 1, только в «решения нет».

    Путь собирается через `chr(0)`, а не литеральным экранированием в исходнике: escape-код `\\0`
    в самом тексте файла — слишком лёгкая мишень для случайной порчи файла настоящим нулевым
    байтом при последующем редактировании (ровно так один раз и случилось при подготовке этого
    теста — `ast.parse` теста уже не проходил бы, файл содержал бы бинарный мусор)."""
    путь_с_null = "some" + chr(0) + "dir/bases.yaml"
    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": путь_с_null}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_неизвестный_тул_молчит(tmp_path):
    результат = _запустить(
        {"tool_name": "odata1c_query", "tool_input": {"base": "trade_dev"}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


# --- Скрипт — только стандартная библиотека --------------------------------------------------


def test_скрипт_только_стандартная_библиотека():
    import ast

    дерево = ast.parse(СКРИПТ.read_text(encoding="utf-8"), filename=str(СКРИПТ))
    имена_модулей: set[str] = set()
    for узел in ast.walk(дерево):
        if isinstance(узел, ast.Import):
            имена_модулей.update(псевдоним.name.split(".")[0] for псевдоним in узел.names)
        elif isinstance(узел, ast.ImportFrom) and узел.module:
            имена_модулей.add(узел.module.split(".")[0])

    assert имена_модулей, "скрипт должен хоть что-то импортировать"
    непозволенные = имена_модулей - set(sys.stdlib_module_names)
    assert непозволенные == set(), непозволенные
