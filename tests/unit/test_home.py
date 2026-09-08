"""Домашний каталог: порядок разрешения пути, создание, права."""

import pathlib

import pytest

from odata1c.config.home import ensure_home, resolve_home


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


@pytest.mark.skipif(pathlib.Path("/etc").exists(), reason="проверка режима только для POSIX-прав")
def test_права_каталога_закрыты(tmp_path):
    status = ensure_home(tmp_path / "home")
    assert status.permissions_narrowed is True
