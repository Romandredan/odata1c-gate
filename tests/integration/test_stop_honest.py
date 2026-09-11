"""`stop()` на НАСТОЯЩЕМ демоне: отказ снять его — это отказ, а не успех (ревью M1d, раунд 4,
пункт 4).

Дефект, который здесь закрыт, владелец наблюдал живьём: демон работает, `daemon.pid` исчез,
`odata1c daemon stop` отвечает «демон не запущен», снимать приходится вручную по номеру процесса.
Причина — прежний `stop()` подавлял отказ `os.kill` целиком, удалял pid-файл в любом случае и
возвращал `True`; наблюдаемый владельцем `False` был уже ВТОРЫМ вызовом, а первый унёс файл.

Здесь всё настоящее: демон поднят как в работе (через Планировщик заданий или откат), порт
слушается, проверка живости — та же самая (`OpenProcess`+`GetExitCodeProcess`). Подменён ровно
один элемент — отказ `os.kill`. Чем такой отказ был вызван у владельца, неизвестно (ревьюер
установил механизм, но не триггер), и воспроизводить нужно именно механизм: «не сняли — отчитались
успехом и унесли файл».
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import sys
import time

import pytest

from odata1c import daemon as daemon_module
from odata1c.config.home import ensure_home
from odata1c.config.writer import ensure_gate_secret
from odata1c.daemon import is_listening, spawn_detached
from odata1c.daemon import stop as daemon_stop

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="подъём демона — Windows")

ПРЕДЕЛ_ГОТОВНОСТИ_С = 25
ПРЕДЕЛ_ОСТАНОВКИ_С = 10


def _свободный_порт() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as соединение:
        соединение.bind(("127.0.0.1", 0))
        return соединение.getsockname()[1]


def test_stop_на_живом_демоне_не_врёт_про_успех(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ensure_home(home)
    порт = _свободный_порт()
    (home / "daemon.yaml").write_text(f"port: {порт}\n", encoding="utf-8")
    ensure_gate_secret(home / "daemon.yaml")
    (home / "bases.yaml").write_text("bases: {}\n", encoding="utf-8")
    pid_файл = home / "daemon.pid"
    номер_демона: list[str | None] = [None]

    try:
        spawn_detached(home, порт)
        предел = time.monotonic() + ПРЕДЕЛ_ГОТОВНОСТИ_С
        while not is_listening(порт) and time.monotonic() < предел:
            time.sleep(0.2)
        assert is_listening(порт), "демон не поднялся — проверять нечего"
        assert pid_файл.exists()
        номер = pid_файл.read_text(encoding="utf-8").strip()
        номер_демона[0] = номер

        # 1. Снять не смогли: отказ обязан остаться отказом.
        with monkeypatch.context() as отказ:
            отказ.setattr(
                daemon_module.os,
                "kill",
                lambda pid, sig: (_ for _ in ()).throw(PermissionError(5, "отказано в доступе")),
            )
            отказ.setattr(daemon_module, "ОЖИДАНИЕ_СМЕРТИ_ДЕМОНА_С", 1.0)
            итог = daemon_stop(home)

        assert итог is False, "stop() отчитался успехом, не сняв демон"
        assert pid_файл.exists(), "pid-файл живого демона унесён — управлять им больше нечем"
        assert pid_файл.read_text(encoding="utf-8").strip() == номер
        assert is_listening(порт), "демон должен был остаться жив: os.kill до него не дошёл"

        # 2. Тот же демон, тот же вызов без подмены: снят по-настоящему.
        assert daemon_stop(home) is True
        предел = time.monotonic() + ПРЕДЕЛ_ОСТАНОВКИ_С
        while is_listening(порт) and time.monotonic() < предел:
            time.sleep(0.2)
        assert not is_listening(порт), "демон не снят, хотя stop() отчитался успехом"
        assert not pid_файл.exists(), "pid-файл снятого демона остался"
    finally:
        # Уборка НЕ через `stop()`: именно его этот тест и проверяет, а проверяемым нельзя
        # убирать за собой — сломанный `stop()` (например, под мутацией) оставит демон работать
        # вечно. Проверено на себе: мутационный прогон оставил в системе два демона на временных
        # домашних каталогах, которые пришлось снимать руками по номеру процесса — ровно то, что
        # владелец делал из-за самого дефекта. Номер запоминается сразу после подъёма и
        # снимается безусловно.
        if номер_демона[0] is not None:
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(номер_демона[0]), signal.SIGTERM)
            предел = time.monotonic() + ПРЕДЕЛ_ОСТАНОВКИ_С
            while is_listening(порт) and time.monotonic() < предел:
                time.sleep(0.2)
