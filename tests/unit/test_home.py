"""Домашний каталог: порядок разрешения пути, создание, права."""

import pathlib
import subprocess
import sys

import pytest

from odata1c.config.home import check_file_permissions, ensure_home, resolve_home


def test_явный_путь_главнее_переменной_окружения(tmp_path, monkeypatch):
    monkeypatch.setenv("ODATA1C_HOME", str(tmp_path / "из-окружения"))
    assert resolve_home(str(tmp_path / "явный")) == tmp_path / "явный"


def test_переменная_окружения_главнее_умолчания(tmp_path, monkeypatch):
    monkeypatch.setenv("ODATA1C_HOME", str(tmp_path / "из-окружения"))
    assert resolve_home(None) == tmp_path / "из-окружения"


def test_умолчание_внутри_домашнего_каталога_пользователя(monkeypatch):
    monkeypatch.delenv("ODATA1C_HOME", raising=False)
    assert resolve_home(None) == pathlib.Path.home() / ".claude" / "odata1c"


def test_создание_каталога_и_подкаталогов(tmp_path):
    status = ensure_home(tmp_path / "home")
    assert status.created is True
    assert (tmp_path / "home" / "bases").is_dir()
    assert (tmp_path / "home" / "logs").is_dir()


def test_повторный_вызов_не_считается_созданием(tmp_path):
    ensure_home(tmp_path / "home")
    assert ensure_home(tmp_path / "home").created is False


@pytest.mark.skipif(sys.platform == "win32", reason="проверка POSIX-прав, пропускается на Windows")
def test_права_каталога_закрыты(tmp_path):
    status = ensure_home(tmp_path / "home")
    assert status.permissions_narrowed is True


@pytest.mark.skipif(sys.platform != "win32", reason="icacls работает только на Windows")
def test_check_file_permissions_ensure_home_без_предупреждений(tmp_path):
    status = ensure_home(tmp_path / "home")
    assert check_file_permissions(status.path) is None


@pytest.mark.skipif(sys.platform != "win32", reason="icacls работает только на Windows")
def test_check_file_permissions_широкий_доступ_первой_записью_даёт_предупреждение(tmp_path):
    test_dir = tmp_path / "open"
    test_dir.mkdir()
    # Даём широкий доступ группе "Все" (Everyone) — GUID *S-1-1-0
    subprocess.run(
        ["icacls", str(test_dir), "/grant", "*S-1-1-0:(F)"],
        check=True,
        capture_output=True,
    )
    warning = check_file_permissions(test_dir)
    assert warning is not None
    assert "доступен" in warning


@pytest.mark.skipif(sys.platform != "win32", reason="icacls работает только на Windows")
def test_check_file_permissions_несуществующий_путь_возвращает_none(tmp_path):
    nonexistent = tmp_path / "nonexistent"
    assert check_file_permissions(nonexistent) is None


@pytest.mark.skipif(sys.platform != "win32", reason="icacls работает только на Windows")
def test_check_file_permissions_кодировка_читаема_и_системные_группы_исключены(tmp_path):
    """Регресс на смешение кодировок: icacls пишет в кодировке консоли (OEM, обычно cp866
    на ru-RU), а не в кодировке процесса (ANSI, cp1251), которую subprocess.run(text=True)
    берёт по умолчанию.

    Каталоги под tmp_path уже наследуют явные ACE от NT AUTHORITY\\СИСТЕМА и
    BUILTIN\\Администраторы (проверено вручную: icacls на новом подкаталоге показывает
    оба флагами (I)(OI)(CI)(F)) — тест не вакуумный. Ключевая проверка ниже —
    `"Все" in warning`: экспериментально подтверждено (decode тем же сырым выводом
    кодировкой процесса, cp1251, вместо кодировки консоли, cp866), что при возврате
    дефекта группа "Все" превращается в "‚бҐ" и эта проверка падает первой — однобайтовые
    кодовые страницы не бросают UnicodeDecodeError на "чужих" байтах, поэтому символа
    замены U+FFFD при такой порче не возникает и его отдельно не проверяем.
    """
    test_dir = tmp_path / "wide"
    test_dir.mkdir()
    subprocess.run(
        ["icacls", str(test_dir), "/grant", "*S-1-1-0:(R)"],
        check=True,
        capture_output=True,
    )

    warning = check_file_permissions(test_dir)

    assert warning is not None
    assert "Все" in warning  # реальный широкий доступ по-прежнему виден и читаем
    assert "СИСТЕМА" not in warning
    assert "SYSTEM" not in warning
    assert "Администраторы" not in warning
    assert "Administrators" not in warning
