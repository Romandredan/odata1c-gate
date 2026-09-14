# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Хук `PreToolUse` плагина odata1c — защита в глубину, не замена гейта.

Читает с stdin JSON-событие Claude Code (`tool_name`, `tool_input`, ...), печатает в stdout
`{"hookSpecificOutput": {...}}` с решением `ask`/`deny` или ничего (решения нет — тул разрешён
как обычно) и всегда завершается кодом 0 (никогда 2 — ошибка хука не должна блокировать сессию
сильнее, чем осознанный `deny`).

Правила, в этом порядке (design-doc M3 §5,
`docs/superpowers/specs/2026-09-14-m3-package-design.md`):

1. `odata1c_commit` (обе формы имени тула — плагин и ручная регистрация MCP) → `ask`. Это
   страховка диалога разрешения Claude Code по `_meta`, а не замена: если Claude Code уже
   показывает свой вопрос на `odata1c_commit`, пользователь увидит два запроса подряд — решение,
   снимать ли это правило, за владельцем (приёмка вживую, см. отчёт задачи).
2. `Read`/`Edit`/`Write`/`MultiEdit`/`NotebookEdit`/`Grep`/`Glob` по пути внутри домашнего
   каталога шлюза (`ODATA1C_HOME` или `~/.claude/odata1c`), оканчивающемуся на один из файлов с
   паролями или реальными значениями (`bases.yaml`, `daemon.yaml`, `launcher.key`,
   `gate.sqlite[-wal]`, `journal.sqlite[-wal]`) → `deny`. Политика (`policy.yaml`) и рецепты
   (`recipes/`) в этот список не входят — их модели видеть можно.
3. `Bash` с командой, упоминающей один из тех же файлов вместе со словом `odata1c`, либо
   `odata1c reveal`, либо `sqlite3` рядом со словарём/журналом (`gate.sqlite`/`journal.sqlite`)
   → `deny` с той же причиной, что и правило 2.
4. Иначе — молчание (тул выполняется как обычно).

Причины решений — фиксированный русский текст без значений из события: только `pending_id`
(правило 1) и не более. Полные пути, содержимое команд и прочие значения из `tool_input` в текст
причины не попадают.

**Обходы, которые этот хук НЕ закрывает** (см. `AGENTS.md`, раздел «Безопасность» — это
задокументированная, осознанная граница, а не упущение): `python -c "..."` и любой другой способ
прочитать файл в обход matcher'а хука; копирование защищённого файла под другим именем перед
чтением; чтение через инструмент, которого нет в списке `matcher` `hooks.json`; прямое обращение
к демону в обход лаунчера (у хука нет доступа к сетевым запросам модели). Хук — защита в глубину
поверх инварианта 1 (реальные значения не выходят через MCP) и разрешений Claude Code на Bash;
последняя линия — они, не этот скрипт.
"""

import contextlib
import json
import os
import pathlib
import re
import sys

# Правило 1: обе формы имени тула odata1c_commit.
_COMMIT_RE = re.compile(r"^mcp__(plugin_odata1c_gate|odata1c)__odata1c_commit$")

# Правило 2: файлы домашнего каталога шлюза с паролями и реальными значениями.
_ФАЙЛЫ_ДОМА = {
    "bases.yaml",
    "daemon.yaml",
    "launcher.key",
    "gate.sqlite",
    "gate.sqlite-wal",
    "journal.sqlite",
    "journal.sqlite-wal",
}

# Тулы правила 2 и поле их входа, где лежит путь.
_ПУТЬ_ПОЛЕ = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
    "Grep": "path",
    "Glob": "path",
}

# Правило 3: те же имена файлов внутри произвольной команды Bash (дословно из брифа задачи).
_BASH_ФАЙЛЫ_RE = re.compile(
    r"(?<![\w.-])(bases\.yaml|daemon\.yaml|launcher\.key|"
    r"gate\.sqlite(-wal|-shm)?|journal\.sqlite(-wal|-shm)?)\b"
)
_BASH_REVEAL_RE = re.compile(r"\bodata1c\s+reveal\b")
_BASH_SQLITE_RE = re.compile(r"\bsqlite3\b.*(gate|journal)\.sqlite")

_ПРИЧИНА_ФАЙЛ = (
    "файл шлюза odata1c: пароли или реальные значения; читать его модели нельзя, спросите владельца"
)


def _ask(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _нормализовать_путь(значение: str) -> pathlib.Path:
    """`os.path.expanduser` + `normpath`/`abspath`/`normcase`, учитывая оба написания `~`.

    Входные пути из событий хука бывают и POSIX (`/`), и Windows (`\\`) вне зависимости от
    текущей ОС (тест на Windows проверяет также вид пути, характерный для POSIX-команд в примерах
    Bash). Backslash заменяется на `/` до `expanduser`/`normpath`, чтобы `~\\...` раскрывался так
    же, как `~/...`, и чтобы разбор по частям пути (`.parts`) ниже был устойчив к написанию."""
    единообразный = значение.replace("\\", "/")
    раскрытый = os.path.expanduser(единообразный)
    return pathlib.Path(os.path.normcase(os.path.normpath(os.path.abspath(раскрытый))))


def _домашний_каталог() -> pathlib.Path:
    задан = os.environ.get("ODATA1C_HOME")
    if задан:
        return pathlib.Path(задан)
    return pathlib.Path("~/.claude/odata1c")


def _внутри_дома_шлюза(норм_путь: pathlib.Path, норм_дом: pathlib.Path) -> bool:
    if норм_путь == норм_дом or норм_дом in норм_путь.parents:
        return True
    # Запасной случай (`home` не задан явно и не совпал буквально, например из-за разных
    # раскрытий `~`): по частям пути видно, что файл лежит в домашнем каталоге шлюза.
    части = set(норм_путь.parts)
    return "odata1c" in части and ".claude" in части


def decide(event: dict, *, home: pathlib.Path) -> dict | None:
    """Решение хука по одному событию `PreToolUse`. Используется и `main`, и тестами напрямую."""
    tool_name = event.get("tool_name")
    if not isinstance(tool_name, str):
        return None

    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    if _COMMIT_RE.match(tool_name):
        pending_id = tool_input.get("pending_id", "?")
        return _ask(
            f"запись в 1С: pending {pending_id}; подтвердите, что превью показано "
            "пользователю и он согласился"
        )

    поле = _ПУТЬ_ПОЛЕ.get(tool_name)
    if поле is not None:
        значение = tool_input.get(поле)
        if isinstance(значение, str) and значение:
            имя = pathlib.Path(значение.replace("\\", "/")).name
            if имя in _ФАЙЛЫ_ДОМА:
                норм_путь = _нормализовать_путь(значение)
                норм_дом = _нормализовать_путь(str(home))
                if _внутри_дома_шлюза(норм_путь, норм_дом):
                    return _deny(_ПРИЧИНА_ФАЙЛ)
        return None

    if tool_name == "Bash":
        команда = tool_input.get("command")
        if isinstance(команда, str) and команда:
            файлы_с_odata1c = bool(_BASH_ФАЙЛЫ_RE.search(команда)) and "odata1c" in команда.lower()
            reveal = _BASH_REVEAL_RE.search(команда)
            sqlite = _BASH_SQLITE_RE.search(команда)
            if файлы_с_odata1c or reveal or sqlite:
                return _deny(_ПРИЧИНА_ФАЙЛ)
        return None

    return None


def main() -> int:
    # Домашний кодовый режим Windows-консоли не всегда UTF-8 (PEP 686 — с 3.15, наш минимум 3.12);
    # Claude Code передаёт stdin в UTF-8, а причины решений — кириллица. Без явного reconfigure
    # чтение или печать в другой кодировке может упасть исключением ещё до JSON-разбора — тогда
    # решение хука терялось бы молча вместо «код 0, решения нет». Оба потока — пайпы, не консоль,
    # reconfigure на них всегда доступен; suppress — на случай нестандартного окружения запуска.
    for поток in (sys.stdin, sys.stdout):
        with contextlib.suppress(Exception):
            поток.reconfigure(encoding="utf-8")

    try:
        событие = json.loads(sys.stdin.read())
    except Exception:
        return 0
    if not isinstance(событие, dict):
        return 0

    try:
        решение = decide(событие, home=_домашний_каталог())
    except Exception:
        # «Код возврата всегда 0» (интерфейс задачи) — сбой самого хука на неожиданном входе не
        # должен блокировать тул сильнее, чем осознанный deny; тул выполняется как обычно.
        return 0

    if решение is not None:
        # ensure_ascii=True: причина — кириллица, а печать идёт как ASCII-эскейпы (\uXXXX) —
        # валидный JSON, который декодируется одинаково в любой кодовой странице родителя.
        print(json.dumps(решение, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
