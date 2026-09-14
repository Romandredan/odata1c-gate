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
`docs/superpowers/specs/2026-09-14-m3-package-design.md` — раунд правок 1 по ревью задачи 4
(Ruling 68/72/73) и раунд 2 (Ruling 77)):

1. `odata1c_commit` (обе формы имени тула — плагин и ручная регистрация MCP) → `ask`. Это
   страховка диалога разрешения Claude Code по `_meta`, а не замена: если Claude Code уже
   показывает свой вопрос на `odata1c_commit`, пользователь увидит два запроса подряд — решение,
   снимать ли это правило, за владельцем (приёмка вживую, см. отчёт задачи).
2. `Read`/`Edit`/`Write`/`MultiEdit`/`NotebookEdit`/`Grep`/`Glob` по пути внутри домашнего
   каталога шлюза → `deny`. Базовое имя пути сравнивается без учёта регистра, без хвостовых точек
   и пробелов и без хвостового потока NTFS (`::$DATA`) с защищёнными именами (`bases.yaml`,
   `daemon.yaml`, `launcher.key`, `gate.sqlite[-wal|-shm]`, `journal.sqlite[-wal|-shm]`). «Внутри
   дома» проверяется по ДВУМ известным домам сразу — дому этой сессии (`ODATA1C_HOME`, если
   задан, иначе `~/.claude/odata1c`) и, независимо от него, документированному дефолту
   `~/.claude/odata1c`: путь к настоящему дому владельца обязан оставаться закрытым, даже если
   сессия сконфигурирована на другой `ODATA1C_HOME`. Для `Grep`/`Glob` дополнительно запрещён путь,
   равный любому из этих домов или его предку (`~`, `~/.claude`, сам дом целиком) — один вызов с
   таким путём рекурсивно вычитывает все защищённые файлы разом; подкаталоги дома (`bases/…`,
   `recipes/…`, `logs/…`) разрешены.
3. `Bash`/`PowerShell` (оба — поле `command`) → `deny`, если команда содержит: имя защищённого
   файла как отдельное слово, без учёта регистра, без требования соседства со словом `odata1c`;
   либо `odata1c`/`odata1c.exe` (без учёта регистра) рядом со словом `reveal` (с пробелами/
   кавычками между); либо `sqlite3` вместе с `gate.sqlite`/`journal.sqlite` в любом месте команды,
   включая разные строки многострочной — во всех трёх случаях причина та же, что у правила 2
   (Ruling 73). Отдельно (Ruling 77, раунд 2): `deny` с причиной «шаблон в каталоге шлюза может
   задеть файлы с паролями», если команда упоминает путь дома шлюза (буквально `.claude/odata1c`,
   любая форма подстановки `ODATA1C_HOME`, или буквальное значение переменной, если она задана) И
   символ шаблона (`*` или `?`) — `bases.y*`, `~/.claude/odata1c/*`, `$env:ODATA1C_HOME\\*`,
   `%ODATA1C_HOME%\\*.yaml`. Команды с путём дома без шаблона (`ls ~/.claude/odata1c/recipes/ut`)
   остаются разрешены.
4. Иначе — молчание (тул выполняется как обычно).

Причины решений — фиксированный русский текст без значений из события: только `pending_id`
(правило 1) и не более. Полные пути, содержимое команд и прочие значения из `tool_input` в текст
причины не попадают.

**Обходы, которые этот хук НЕ закрывает** (см. `AGENTS.md`, раздел «Безопасность» — это
задокументированная, осознанная граница, а не упущение): `python -c "..."` и любой другой способ
прочитать файл в обход matcher'а хука; копирование защищённого файла под другим именем перед
чтением; чтение через инструмент, которого нет в списке `matcher` `hooks.json`; шаблон оболочки,
разнесённый на два отдельных вызова тула (`cd ~/.claude/odata1c` одним вызовом, `cat *` без
упоминания дома — следующим; рабочий каталог тула сохраняется между вызовами, а хук решения не
хранит между ними — правило 3 видит только одну команду за раз); временный дом шлюза вне `.claude`
и вне значения `ODATA1C_HOME` этой сессии, на который сессия хука не настроена (скрипты приёмки
поднимают такие для тестов на живой базе); прямое обращение к демону в обход лаунчера (у хука нет
доступа к сетевым запросам модели). Хук — защита в глубину поверх инварианта 1 (реальные значения
не выходят через MCP) и разрешений Claude Code на Bash; последняя линия — они, не этот скрипт.
"""

import contextlib
import json
import os
import pathlib
import re
import sys

# Правило 1: обе формы имени тула odata1c_commit.
_COMMIT_RE = re.compile(r"^mcp__(plugin_odata1c_gate|odata1c)__odata1c_commit$")

# Правило 2: файлы домашнего каталога шлюза с паролями и реальными значениями (Ruling 68: с -shm).
_ФАЙЛЫ_ДОМА = {
    "bases.yaml",
    "daemon.yaml",
    "launcher.key",
    "gate.sqlite",
    "gate.sqlite-wal",
    "gate.sqlite-shm",
    "journal.sqlite",
    "journal.sqlite-wal",
    "journal.sqlite-shm",
}
_ФАЙЛЫ_ДОМА_CF = {имя.casefold() for имя in _ФАЙЛЫ_ДОМА}

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
# Ruling 72 (находка M-3): для этих двух дополнительно запрещён путь на весь дом или его предка.
_КАТАЛОЖНЫЕ_ТУЛЫ = {"Grep", "Glob"}

# Ruling 68 (находка м-3): PowerShell перехватывается как Bash — то же поле command.
_КОМАНДНЫЕ_ТУЛЫ = {"Bash", "PowerShell"}

# Правило 3 (Ruling 73/находка M-4): без учёта регистра, без обязательной подстроки odata1c рядом
# с именем файла — только рядом со словом reveal, и там же допускаются .exe, пробелы и кавычки.
_BASH_ФАЙЛЫ_RE = re.compile(
    r"(?<![\w.-])(bases\.yaml|daemon\.yaml|launcher\.key|"
    r"gate\.sqlite(-wal|-shm)?|journal\.sqlite(-wal|-shm)?)\b",
    re.IGNORECASE,
)
_BASH_REVEAL_RE = re.compile(r"\bodata1c(\.exe)?\b[\s'\"]*reveal\b", re.IGNORECASE)
_BASH_SQLITE_RE = re.compile(r"\bsqlite3\b.*(gate|journal)\.sqlite", re.IGNORECASE | re.DOTALL)

# Ruling 77 (раунд 2): путь дома шлюза в команде — буквально .claude/odata1c (любой разделитель)
# или любая форма подстановки ODATA1C_HOME ($ODATA1C_HOME, ${ODATA1C_HOME}, %ODATA1C_HOME%,
# $env:ODATA1C_HOME — все они содержат подстроку "odata1c_home" без учёта регистра, поэтому ловятся
# одной проверкой). IGNORECASE — не из текста ruling дословно, но той же логики ради, что и у
# остальных проверок Bash/PowerShell этого правила (Ruling 73) — без него `.CLAUDE/ODATA1C` прошёл
# бы мимо.
_HOME_PATH_RE = re.compile(r"\.claude[/\\]odata1c", re.IGNORECASE)

_ПРИЧИНА_ФАЙЛ = (
    "файл или каталог шлюза odata1c: пароли или реальные значения; читать его модели нельзя, "
    "спросите владельца"
)
_ПРИЧИНА_ШАБЛОН = "шаблон в каталоге шлюза может задеть файлы с паролями"

# Документированный дефолт дома шлюза (SPEC §2.2) — известен независимо от ODATA1C_HOME сессии
# (находка м-2: замена ненадёжного признака "odata1c и .claude среди частей пути").
_ДЕФОЛТНЫЙ_ДОМ = "~/.claude/odata1c"

# Ruling — находка м-2: Git Bash на Windows отдаёт абсолютные пути без буквы диска (/c/Users/...).
# os.path.abspath такой путь не превращает обратно в путь с буквой диска (трактует /c как обычный
# каталог), из-за чего сравнение с домом по префиксу не совпадает ни при каком ODATA1C_HOME. Здесь
# — обратное преобразование Git Bash/MSYS. Только для Windows: на настоящем POSIX /c/... — путь от
# корня файловой системы, переписывать его нельзя.
_POSIX_ДИСК_RE = re.compile(r"^/([A-Za-z])(/.*)?$")


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


def _переписать_posix_диск(путь: str) -> str:
    if os.name != "nt":
        return путь
    совпадение = _POSIX_ДИСК_RE.match(путь)
    if совпадение is None:
        return путь
    буква = совпадение.group(1)
    остаток = совпадение.group(2) or "/"
    return f"{буква}:{остаток}"


def _нормализовать_путь(значение: str) -> pathlib.Path:
    """Приводит путь к канонической форме для сравнения с домом шлюза.

    `replace("\\\\", "/")` — единое написание разделителя (входные пути бывают и POSIX, и
    Windows вне зависимости от текущей ОС). `_переписать_posix_диск` — Git Bash на Windows.
    `expanduser` — оба написания `~` (`~/...` и после замены разделителя `~\\...` тоже). Дальше
    `os.path.realpath` вместо голого `abspath`/`normpath` (находка м-2): резолвит `..`, символьные
    ссылки и точки соединения (junction), не зависит от UNC-формы (`\\\\?\\...`) и коротких имён
    8.3 — все они обязаны сравниться как один и тот же путь. `normcase` — сравнение без учёта
    регистра."""
    единообразный = _переписать_posix_диск(значение.replace("\\", "/"))
    раскрытый = os.path.expanduser(единообразный)
    канонический = os.path.realpath(раскрытый)
    return pathlib.Path(os.path.normcase(канонический))


def _базовое_имя_для_сравнения(значение: str) -> str:
    """Базовое имя пути в виде для сравнения со списком защищённых файлов (находка M-2): без
    хвостового потока NTFS (`::$DATA` и подобные Alternate Data Streams — Windows открывает по
    нему тот же файл), без хвостовых точек и пробелов (`bases.yaml.` и `bases.yaml ` на NTFS —
    тот же файл, что `bases.yaml`), без учёта регистра (`BASES.YAML` — тот же файл)."""
    имя = pathlib.Path(значение.replace("\\", "/")).name
    имя = имя.split(":", 1)[0]
    имя = имя.rstrip(" .")
    return имя.casefold()


def _домашний_каталог() -> pathlib.Path:
    задан = os.environ.get("ODATA1C_HOME")
    if задан:
        return pathlib.Path(задан)
    return pathlib.Path(_ДЕФОЛТНЫЙ_ДОМ)


def _известные_дома(home: pathlib.Path) -> list[pathlib.Path]:
    """Дом этой сессии (`home`) и, независимо от него, документированный дефолт — оба нормализованы
    и без дублей. Замена признака "части пути" (находка м-2): тот и пропускал временный дом вне
    `.claude`, и ложно срабатывал на `src/odata1c/` этого же репозитория под
    `.claude/worktrees/…`."""
    кандидаты = [_нормализовать_путь(str(home)), _нормализовать_путь(_ДЕФОЛТНЫЙ_ДОМ)]
    уникальные: list[pathlib.Path] = []
    for дом in кандидаты:
        if дом not in уникальные:
            уникальные.append(дом)
    return уникальные


def _внутри_какого_то_дома(норм_путь: pathlib.Path, дома: list[pathlib.Path]) -> bool:
    return any(норм_путь == дом or дом in норм_путь.parents for дом in дома)


def _путь_это_дом_или_предок(норм_путь: pathlib.Path, дома: list[pathlib.Path]) -> bool:
    return any(норм_путь == дом or норм_путь in дом.parents for дом in дома)


def _команда_упоминает_дом_шлюза(команда: str) -> bool:
    """Ruling 77: путь дома шлюза в команде — буквально `.claude/odata1c` (любой разделитель),
    любая форма подстановки `ODATA1C_HOME`, или буквальное значение переменной, если она задана
    для этой сессии (клон дома с другим именем каталога — например, временный дом приёмки — не
    содержит ни `.claude/odata1c`, ни слова `ODATA1C_HOME`, только свой собственный путь)."""
    if _HOME_PATH_RE.search(команда):
        return True
    команда_нижним_регистром = команда.lower()
    if "odata1c_home" in команда_нижним_регистром:
        return True
    задано = os.environ.get("ODATA1C_HOME")
    if задано:
        if задано.lower() in команда_нижним_регистром:
            return True
        # То же значение с другим разделителем — путь могли переписать в командной строке.
        перевёрнутое = задано.replace("\\", "/") if "\\" in задано else задано.replace("/", "\\")
        if перевёрнутое != задано and перевёрнутое.lower() in команда_нижним_регистром:
            return True
    return False


def _есть_шаблон_оболочки(команда: str) -> bool:
    """Ruling 77: символ шаблона `*` или `?` — дословно эти два, не полный набор спецсимволов
    оболочки (`[]`, `{}` и т. п. не проверяются — решение контроллера ограничивает правило ими)."""
    return "*" in команда or "?" in команда


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
            норм_путь = _нормализовать_путь(значение)
            дома = _известные_дома(home)

            if tool_name in _КАТАЛОЖНЫЕ_ТУЛЫ and _путь_это_дом_или_предок(норм_путь, дома):
                return _deny(_ПРИЧИНА_ФАЙЛ)

            имя = _базовое_имя_для_сравнения(значение)
            if имя in _ФАЙЛЫ_ДОМА_CF and _внутри_какого_то_дома(норм_путь, дома):
                return _deny(_ПРИЧИНА_ФАЙЛ)
        return None

    if tool_name in _КОМАНДНЫЕ_ТУЛЫ:
        команда = tool_input.get("command")
        if isinstance(команда, str) and команда:
            если_файл = _BASH_ФАЙЛЫ_RE.search(команда)
            если_reveal = _BASH_REVEAL_RE.search(команда)
            если_sqlite = _BASH_SQLITE_RE.search(команда)
            if если_файл or если_reveal or если_sqlite:
                return _deny(_ПРИЧИНА_ФАЙЛ)
            if _есть_шаблон_оболочки(команда) and _команда_упоминает_дом_шлюза(команда):
                return _deny(_ПРИЧИНА_ШАБЛОН)
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
