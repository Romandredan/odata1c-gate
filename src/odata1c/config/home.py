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
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            return False, f"не удалось закрыть права на {path}: {exc}"
        return True, None
    path.chmod(stat.S_IRWXU)
    return True, None


def check_file_permissions(path: pathlib.Path) -> str | None:
    """Предупреждение, если файл виден другим учётным записям (SPEC §2.3)."""
    if not path.exists():
        return None
    if sys.platform == "win32":
        try:
            output = subprocess.run(
                ["icacls", str(path)], check=True, capture_output=True, text=True
            ).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            return f"не удалось проверить права на {path}: {exc}"
        try:
            username = getpass.getuser().lower()
        except Exception as exc:
            return f"не удалось определить имя пользователя: {exc}"
        # Парсим вывод icacls: первая строка может содержать путь и первую запись
        # Формат: "C:\path ГРУППА:(F)" или "C:\path ГРУППА1:(F)\n                 ГРУППА2:(R)"
        lines = output.splitlines()
        acl_entries = []
        path_str = str(path)
        for i, line in enumerate(lines):
            line = line.strip()
            if not line or "Successfully processed" in line or "Failed processing" in line:
                continue
            # Первая непустая строка может содержать путь и запись
            if i == 0 and line.startswith(path_str):
                remainder = line[len(path_str) :].strip()
                if remainder and ":" in remainder:
                    acl_entries.append(remainder)
            elif ":" in line:
                # Остальные строки — это ACL записи
                acl_entries.append(line)
        # Проверяем каждую запись на наличие доступа для других учётных записей
        others = [
            entry
            for entry in acl_entries
            if username not in entry.lower()
            and "NT AUTHORITY\\SYSTEM" not in entry
            and "BUILTIN\\Администраторы" not in entry
            and "BUILTIN\\Administrators" not in entry
        ]
        if others:
            return f"{path} доступен другим учётным записям: {'; '.join(others)}"
        return None
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"{path} доступен другим учётным записям (режим {oct(mode & 0o777)})"
    return None
