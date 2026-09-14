"""Домашний каталог шлюза: разрешение пути, создание, ограничение прав.

Порядок разрешения задан SPEC §2.1: аргумент --home, затем ODATA1C_HOME, затем ~/.claude/odata1c/.
В каталоге лежат пароли баз и журнал с реальными значениями, поэтому права закрываются сразу
(SPEC §2.3).
"""

from __future__ import annotations

import dataclasses
import getpass
import os
import pathlib
import stat
import subprocess
import sys

SUBDIRS = ("bases", "logs")

# `icacls` — консольная утилита, и у консольного процесса, запущенного из процесса БЕЗ консоли,
# Windows заводит свою: вместе с ней на экране владельца появляется окно. Демон поднимается
# оконным интерпретатором (`pythonw.exe`, см. `daemon._интерпретатор_без_консоли`) и при старте
# закрывает права домашнего каталога — то есть зовёт `icacls`. Найдено прогоном ci на
# windows-latest: сторож окон консоли (`tests/integration/test_no_console_window.py`) увидел
# окно `CASCADIA_HOSTING_WINDOW_CLASS: …\icacls.exe`. `CREATE_NO_WINDOW` — как раз для
# консольного дочернего процесса, которому консоль не нужна.
_БЕЗ_ОКНА = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclasses.dataclass(slots=True)
class HomeStatus:
    path: pathlib.Path
    created: bool
    permissions_narrowed: bool
    warning: str | None = None


def resolve_home(explicit: str | None = None) -> pathlib.Path:
    if explicit:
        return pathlib.Path(explicit).expanduser()
    from_env = os.environ.get("ODATA1C_HOME")
    if from_env:
        return pathlib.Path(from_env).expanduser()
    return pathlib.Path.home() / ".claude" / "odata1c"


def base_dir(home: pathlib.Path, name: str) -> pathlib.Path:
    """Каталог конкретной базы внутри домашнего: политика, индекс метаданных, сохранённое
    описание $metadata, рецепты (SPEC §2.2) — всё, что появится там на следующих этапах,
    должно собирать этот путь одним и тем же способом, а не по месту в разных модулях."""
    return home / "bases" / name


def ensure_home(path: pathlib.Path) -> HomeStatus:
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    for name in SUBDIRS:
        (path / name).mkdir(exist_ok=True)
    narrowed, warning = _narrow_permissions(path)
    return HomeStatus(path=path, created=created, permissions_narrowed=narrowed, warning=warning)


def _narrow_permissions(path: pathlib.Path) -> tuple[bool, str | None]:
    if sys.platform == "win32":
        try:
            username = getpass.getuser()
        except Exception as exc:
            return False, f"не удалось определить имя пользователя: {exc}"
        user = f"{os.environ.get('USERDOMAIN', '')}\\{username}".lstrip("\\")
        try:
            # text=False (байты) — как в check_file_permissions ниже: icacls пишет в
            # кодировке консоли (OEM), а не в кодировке процесса, которую подставляет
            # text=True. При PYTHONUTF8=1 кодировка процесса — utf-8, и text=True пытается
            # декодировать байты OEM как utf-8 ВНУТРИ subprocess.run — необработанный
            # UnicodeDecodeError вылетает раньше, чем этот except успевает сработать,
            # даже при успешном выполнении icacls. Поэтому декодируем сами и только
            # при неудаче — сообщение об успехе вывод icacls не использует.
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                check=True,
                capture_output=True,
                text=False,
                creationflags=_БЕЗ_ОКНА,
            )
        except subprocess.CalledProcessError as exc:
            сырой_вывод = exc.stderr or exc.stdout or b""
            вывод = сырой_вывод.decode(_консольная_кодировка(), errors="replace")
            return False, f"не удалось закрыть права на {path}: {вывод.strip() or exc}"
        except OSError as exc:
            return False, f"не удалось закрыть права на {path}: {exc}"
        return True, None
    path.chmod(stat.S_IRWXU)
    return True, None


def _консольная_кодировка() -> str:
    """Кодовая страница, в которой icacls (консольная утилита) пишет свой вывод.

    Вывод идёт в кодировке консоли (OEM), а не в кодировке процесса (ANSI, её берёт
    subprocess.run(text=True) по умолчанию из locale.getpreferredencoding()). На русской
    Windows это разные страницы (обычно cp866 против cp1251): при разборе в кодировке
    процесса имена системных групп («Администраторы», «SYSTEM») превращаются в нечитаемые
    символы, и сравнение с ними в этой функции перестаёт срабатывать.
    """
    try:
        import ctypes

        код = ctypes.windll.kernel32.GetOEMCP()
        return f"cp{код}"
    except Exception:
        return "cp866"


def check_file_permissions(path: pathlib.Path) -> str | None:
    """Предупреждение, если файл виден другим учётным записям (SPEC §2.3)."""
    if not path.exists():
        return None
    if sys.platform == "win32":
        try:
            # icacls спрашивается про ИМЯ файла из его каталога, а не про полный путь, — и
            # ровно это имя он эхом печатает перед первой записью списка доступа. Причина —
            # находка прогона ci на windows-latest: в полном пути почти всегда стоит имя
            # пользователя (`C:\Users\<имя>\…`), а имя пользователя ниже по коду означает
            # «запись своя». Пока путь остаётся в строке, первая запись списка — та, что
            # напечатана рядом с ним, — считается своей ВСЕГДА, чей бы доступ она ни описывала:
            # настоящий широкий доступ группе «Все», попавший на эту строку, проверка молча
            # пропускала. Снимать путь текстом ненадёжно: он приходит из чужой утилиты и совпадать
            # символ в символ с `str(path)` не обязан. Не передавать его вовсе — надёжно.
            # У корня диска имени нет — тогда спрашиваем по полному пути, как раньше (свои
            # файлы шлюз в корне диска не держит, но падать на этом незачем).
            цель = path.name or str(path)
            raw = subprocess.run(
                ["icacls", цель],
                cwd=path.parent if path.name else None,
                check=True,
                capture_output=True,
                text=False,
                creationflags=_БЕЗ_ОКНА,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            return f"не удалось проверить права на {path}: {exc}"
        output = raw.decode(_консольная_кодировка(), errors="replace")
        try:
            username = getpass.getuser().lower()
        except Exception as exc:
            return f"не удалось определить имя пользователя: {exc}"
        # Разбираем вывод icacls. Формат: "<имя> ГРУППА:(F)" в первой строке и по одной записи
        # в каждой следующей, с отступом: "                 ГРУППА2:(R)".
        acl_entries = []
        имя_файла = path.name or str(path)
        for line in output.splitlines():
            line = line.strip()
            if not line or "Successfully processed" in line or "Failed processing" in line:
                continue
            # Имя файла снимается с любой строки, где оно стоит в начале, а не со строки с
            # номером ноль: пустая или посторонняя строка в начале вывода сдвинула бы нумерацию,
            # и разбор поехал бы весь.
            if line.lower().startswith(имя_файла.lower()):
                line = line[len(имя_файла) :].strip()
            if ":" in line:
                acl_entries.append(line)
        # Проверяем каждую запись на наличие доступа для других учётных записей
        others = [
            entry
            for entry in acl_entries
            if username not in entry.lower()
            and "NT AUTHORITY\\SYSTEM" not in entry
            and "NT AUTHORITY\\СИСТЕМА" not in entry  # SYSTEM на локализованной (ru-RU) Windows
            and "BUILTIN\\Администраторы" not in entry
            and "BUILTIN\\Administrators" not in entry
            # `OWNER RIGHTS` (S-1-3-4) — не учётная запись, а права ВЛАДЕЛЬЦА объекта, то есть
            # текущего пользователя; отдельным ACE их обычно наоборот ограничивают. Встречается
            # во временных каталогах исполнителей GitHub Actions (найдено прогоном ci на
            # windows-latest: там эта запись есть у каждого каталога под TEMP). Оба написания
            # проверены `icacls` вживую: английское — на исполнителе, русское — на машине
            # владельца (`ПРАВА ВЛАДЕЛЬЦА`).
            and "OWNER RIGHTS" not in entry
            and "ПРАВА ВЛАДЕЛЬЦА" not in entry
        ]
        if others:
            return f"{path} доступен другим учётным записям: {'; '.join(others)}"
        return None
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"{path} доступен другим учётным записям (режим {oct(mode & 0o777)})"
    return None
