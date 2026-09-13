"""Заверение имени клиента по родителю лаунчера (Ruling 61): функция решения на подменённом
дереве процессов.

Живые цепочки владельца (отчёт задачи 9, раздел Ruling 61) — лаунчер поднимается вверх, пропуская
шимы запуска (`uv.exe`, интерпретаторы Python, собственный `odata1c.exe`), и первый значимый предок
должен быть исполняемым файлом Claude Code. Скрипт, запустивший лаунчер из Bash/PowerShell, даёт
первым не-шимом `bash.exe`/`cmd.exe`/`powershell.exe` и заверения не получает.
"""

import odata1c.launch_parent as lp
from odata1c.launch_parent import ИМЕНА_CLAUDE_CODE, значимый_предок, родитель_заверён

# Хвост цепочки CLI владельца: `uv run … odata1c mcp` → python(лаунчер) ← python.exe(шим venv)
# ← odata1c.exe ← uv.exe ← claude.exe. `предки` — снизу вверх, без самого лаунчера.
CLI = ["python.exe", "odata1c.exe", "uv.exe", "claude.exe", "Code.exe", "explorer.exe"]
# Расширение VS Code: тот же хвост, claude.exe из каталога расширения (имя образа то же).
VSCODE = ["python.exe", "python.exe", "odata1c.exe", "uv.exe", "claude.exe", "Code.exe"]


def test_значимый_предок_пропускает_только_шимы():
    # Шимы: uv/uvx, интерпретаторы python (в т. ч. версионные), собственный odata1c.exe.
    assert значимый_предок(["python.exe", "odata1c.exe", "uv.exe", "claude.exe"]) == "claude.exe"
    assert значимый_предок(["python3.13.exe", "uvx.exe", "cmd.exe"]) == "cmd.exe"
    assert значимый_предок(["python", "uv", "odata1c", "bash"]) == "bash"  # POSIX-имена
    # Всё — шимы: значимого предка нет.
    assert значимый_предок(["python.exe", "uv.exe", "odata1c.exe"]) is None
    assert значимый_предок([]) is None
    # Регистр не важен.
    assert значимый_предок(["Python.exe", "CLAUDE.EXE"]) == "claude.exe"


def test_легитимные_цепочки_заверяются():
    for цепочка in (CLI, VSCODE):
        заверено, значимый = родитель_заверён(ИМЕНА_CLAUDE_CODE, предки=цепочка)
        assert заверено is True and значимый == "claude.exe", цепочка


def test_скрипт_из_оболочки_не_заверяется():
    случаи = {
        "bash→uv→python": ["uv.exe", "bash.exe", "claude.exe"],
        "python(скрипт)→uv→python": ["uv.exe", "python.exe", "powershell.exe", "claude.exe"],
        "powershell→uv→python": ["uv.exe", "powershell.exe", "claude.exe"],
        "cmd напрямую": ["cmd.exe", "claude.exe"],
        "неизвестный родитель": ["weird-runner.exe"],
    }
    for что, цепочка in случаи.items():
        заверено, _ = родитель_заверён(ИМЕНА_CLAUDE_CODE, предки=цепочка)
        assert заверено is False, что


def test_все_предки_шимы_не_заверяется():
    # Не дойдя до значимого предка, заверения не выдаём (иначе цепочка из одних шимов прошла бы).
    заверено, значимый = родитель_заверён(ИМЕНА_CLAUDE_CODE, предки=["python.exe", "uv.exe"])
    assert заверено is False and значимый is None


def test_дерево_не_прочиталось_не_заверяется(monkeypatch):
    monkeypatch.setattr(lp, "_предки_текущего", lambda: None)
    assert родитель_заверён(ИМЕНА_CLAUDE_CODE) == (False, None)


def test_настраиваемое_имя_родителя():
    # daemon.yaml → claude_code_parents добавляет имена к встроенным.
    разрешённые = ИМЕНА_CLAUDE_CODE | {"claude-code-host.exe"}
    заверено, значимый = родитель_заверён(разрешённые, предки=["uv.exe", "claude-code-host.exe"])
    assert заверено is True and значимый == "claude-code-host.exe"
    # Без настройки то же имя не заверяется.
    assert родитель_заверён(ИМЕНА_CLAUDE_CODE, предки=["claude-code-host.exe"])[0] is False


def test_предки_текущего_на_этой_ос_читаются():
    """Санитарная проверка живого опроса ОС: дерево текущего процесса читается. Что именно в нём —
    зависит от того, чем запущен pytest, но на поддерживаемой ОС оно должно быть непустым списком
    строк (иначе `ctypes`/`_predки_*` сломаны, и заверение всегда падало бы в безопасную сторону
    молча)."""
    import sys

    предки = lp._предки_текущего()
    if sys.platform in ("win32", "linux", "darwin"):
        assert предки, "дерево процессов на поддерживаемой ОС должно читаться"
        assert all(isinstance(и, str) and и for и in предки)
        # У pytest есть значимый предок (не все процессы вверх — шимы запуска).
        assert значимый_предок(предки) is not None
