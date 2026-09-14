"""Заверение имени клиента `claude-code` по родительскому процессу лаунчера (Ruling 61).

Подпись лаунчера (Ruling 59) доказывает демону только «запрос пришёл через лаунчер этой машины»,
но не «клиент — Claude Code»: любой скрипт под тем же пользователем может сам запустить
`odata1c mcp` и назваться `claude-code`. Поэтому лаунчер при старте смотрит своё дерево процессов
вверх и передаёт имя `claude-code` заверенным, только если его запустил исполняемый файл Claude
Code.

Правило (проверено на живом дереве процессов владельца, отчёт задачи 9, раздел Ruling 61):
- вверх по предкам пропускаются только шимы запуска — `uv.exe`/`uvx.exe`, интерпретаторы
  Python (`python.exe`, `pythonw.exe`, `python3*.exe`) и собственный консольный скрипт пакета
  (`odata1c.exe`); первый предок не из этого набора и решает;
- он заверен, только если это исполняемый файл Claude Code (`claude.exe`; список расширяется
  настройкой `daemon.yaml → claude_code_parents`). Живые цепочки владельца:
  CLI — `uv run … odata1c mcp`: `python`(лаунчер) ← `python.exe`(шим venv) ← `odata1c.exe`
  ← `uv.exe` ← `claude.exe` (`~/.local/bin`); расширение VS Code — тот же хвост, но
  `claude.exe` из `…/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/`.

Почему пропускаются только шимы, а не все предки: Bash самой модели — тоже потомок `claude.exe`.
Скрипт, запущенный из Bash (`python evil.py`, `powershell …`), даёт первым не-шимом
`bash.exe`/`cmd.exe`/`powershell.exe` — не Claude Code, и заверения не получает. Не удалось
определить дерево — тоже не заверено (отказ в безопасную сторону).

Граница честная: переименовать интерпретатор в `claude.exe` и запустить лаунчер от него — обход.
Он виден в дереве процессов; полноценная защита — аутентификация человека (M4).

Linux (M3 задача 9, проверяется только в GitHub Actions — Linux у владельца не запускается):
правило то же, меняются лишь имена. Дерево читается из `/proc`, шимы — `uv`, `uvx`, `odata1c`,
`python3*`, заверенный предок — `claude` нативного установщика. Установка через npm даёт образ
`node`, а сам Claude Code стоит в его аргументах (`…/@anthropic-ai/claude-code/cli.js`) — такой
предок называется `claude` (`claude_code_в_командной_строке`). Сам по себе `node` не заверяется и
прозрачным не становится.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Sequence

_log = logging.getLogger(__name__)

# Имена образов Claude Code (без регистра). Расширяется `daemon.yaml → claude_code_parents`.
ИМЕНА_CLAUDE_CODE: frozenset[str] = frozenset({"claude.exe", "claude"})

# Шимы запуска, прозрачные при подъёме по предкам. Только они — иначе Bash модели, тоже потомок
# claude.exe, прошёл бы заверение.
_ШИМЫ: frozenset[str] = frozenset(
    {"uv.exe", "uvx.exe", "uv", "uvx", "odata1c.exe", "odata1c", "pythonw.exe"}
)
_ПИТОН = re.compile(r"python3?[0-9.]*(\.exe)?$")

_ПРЕДЕЛ_ГЛУБИНЫ = 24

# Имена образа Node.js. Claude Code, поставленный через npm, работает именно так: образ — `node`,
# а сам Claude Code лежит в аргументе командной строки (`…/@anthropic-ai/claude-code/cli.js`).
# Ни в `ИМЕНА_CLAUDE_CODE`, ни в `_ШИМЫ` `node` не входит и входить не должен: в первом любая
# программа на Node заверялась бы как Claude Code, во втором — становилась бы прозрачной, и за
# ней заверение получал бы тот, кто запустил её саму.
_ИМЕНА_NODE: frozenset[str] = frozenset({"node", "node.exe"})


def claude_code_в_командной_строке(cmdline: str) -> bool:
    """Запущен ли этим `node` именно Claude Code: среди аргументов есть путь к `cli.js` внутри
    каталога `claude-code` (`…/node_modules/@anthropic-ai/claude-code/cli.js`).

    `cmdline` — содержимое `/proc/<pid>/cmdline`, аргументы разделены нулевым байтом.

    Условия два, и оба обязательны. Только `cli.js` — слишком широко: файл с таким именем есть у
    доброй половины пакетов npm. Только `claude-code` в пути — тоже: под этим каталогом лежит не
    один файл, и запуск любого другого из них Claude Code не делает."""
    return any(
        аргумент.endswith("cli.js") and "claude-code" in аргумент
        for аргумент in cmdline.split("\0")
    )


def _шим(имя: str) -> bool:
    н = имя.lower()
    return н in _ШИМЫ or _ПИТОН.fullmatch(н) is not None


def значимый_предок(предки: Sequence[str]) -> str | None:
    """Первое имя образа среди предков (снизу вверх), не являющееся шимом запуска, в нижнем
    регистре. `None` — если все предки оказались шимами (или список пуст)."""
    for имя in предки:
        if имя and not _шим(имя):
            return имя.lower()
    return None


def родитель_заверён(
    разрешённые: frozenset[str] | set[str],
    *,
    предки: Sequence[str] | None = None,
) -> tuple[bool, str | None]:
    """(заверено, имя значимого предка). `предки` — снизу вверх, для тестов; по умолчанию —
    предки текущего процесса. Дерево не прочиталось (`None`) — не заверено (безопасная сторона)."""
    цепочка = предки if предки is not None else _предки_текущего()
    if цепочка is None:
        return False, None
    значимый = значимый_предок(цепочка)
    if значимый is None:
        return False, None
    return значимый in {и.lower() for и in разрешённые}, значимый


def _предки_текущего() -> list[str] | None:
    """Имена образов предков текущего процесса, снизу вверх (без самого процесса). `None` — если
    дерево прочитать не удалось."""
    try:
        if sys.platform == "win32":
            return _предки_windows()
        if sys.platform == "linux":
            return _предки_linux()
        if sys.platform == "darwin":
            return _предки_macos()
    except Exception as exc:  # noqa: BLE001 — любой сбой опроса ОС: отказ в безопасную сторону
        _log.debug("не удалось прочитать дерево процессов (%s)", type(exc).__name__)
        return None
    return None


def _предки_windows() -> list[str] | None:
    """PID → (PPID, имя образа) одним снимком `CreateToolhelp32Snapshot`; подъём от родителя
    текущего процесса. Только чтение снимка, без `OpenProcess` — прав не требует."""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = wintypes.HANDLE(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    снимок = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if снимок == INVALID:
        return None
    родитель: dict[int, int] = {}
    имя: dict[int, str] = {}
    try:
        запись = PROCESSENTRY32W()
        запись.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        есть = kernel32.Process32FirstW(снимок, ctypes.byref(запись))
        while есть:
            родитель[запись.th32ProcessID] = запись.th32ParentProcessID
            имя[запись.th32ProcessID] = запись.szExeFile
            есть = kernel32.Process32NextW(снимок, ctypes.byref(запись))
    finally:
        kernel32.CloseHandle(снимок)
    return _подъём(os.getpid(), родитель, имя)


def _подъём(pid: int, родитель: dict[int, int], имя: dict[int, str]) -> list[str]:
    """Имена предков `pid` снизу вверх по картам PID→PPID и PID→имя, с защитой от циклов и
    предела глубины (реюз PID)."""
    цепочка: list[str] = []
    видели = {pid}
    текущий = родитель.get(pid, 0)
    while текущий and текущий not in видели and len(цепочка) < _ПРЕДЕЛ_ГЛУБИНЫ:
        видели.add(текущий)
        имя_образа = имя.get(текущий)
        if not имя_образа:
            break
        цепочка.append(имя_образа)
        текущий = родитель.get(текущий, 0)
    return цепочка


def _предки_linux() -> list[str] | None:
    цепочка: list[str] = []
    видели: set[int] = set()
    pid = os.getppid()
    while pid and pid not in видели and len(цепочка) < _ПРЕДЕЛ_ГЛУБИНЫ:
        видели.add(pid)
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as ф:
                stat = ф.read()
        except OSError:
            break
        закр = stat.rfind(")")
        поля = stat[закр + 2 :].split() if закр != -1 else []
        имя = None
        try:
            имя = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
        except OSError:
            начало = stat.find("(")
            if начало != -1 and закр != -1:
                имя = stat[начало + 1 : закр]
        if not имя:
            break
        if имя.lower() in _ИМЕНА_NODE and claude_code_в_командной_строке(_командная_строка(pid)):
            # Разрешаем `node` до `claude` прямо здесь, чтобы наружу — в `значимый_предок` и в
            # заголовок демона — уходило одно имя на оба способа установки Claude Code:
            # нативный установщик даёт образ `claude`, установка через npm — `node` с `cli.js`.
            имя = "claude"
        цепочка.append(имя)
        pid = int(поля[1]) if len(поля) > 1 and поля[1].isdigit() else 0
    return цепочка or None


def _командная_строка(pid: int) -> str:
    """`/proc/<pid>/cmdline` как есть (аргументы разделены нулевым байтом); пусто — прочитать не
    удалось (процесс уже завершился, нет прав). Пустая строка ничего не заверяет."""
    try:
        with open(f"/proc/{pid}/cmdline", encoding="utf-8", errors="replace") as ф:
            return ф.read()
    except OSError:
        return ""


def _предки_macos() -> list[str] | None:
    цепочка: list[str] = []
    видели: set[int] = set()
    pid = os.getppid()
    while pid and pid not in видели and len(цепочка) < _ПРЕДЕЛ_ГЛУБИНЫ:
        видели.add(pid)
        итог = subprocess.run(
            ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        части = итог.stdout.strip().split(None, 1)
        if len(части) < 2:
            break
        цепочка.append(os.path.basename(части[1]))
        pid = int(части[0]) if части[0].isdigit() else 0
    return цепочка or None
