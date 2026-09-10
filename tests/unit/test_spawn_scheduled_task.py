"""`daemon._spawn_via_scheduled_task` (план M1d, задача 6, раунд правок 2):

- находка Б.1 (Critical): код возврата `schtasks` не доказывает, что процесс поднялся — успех
  подтверждается фактом (порт слушается И `daemon.pid` появился ИМЕННО в нашем домашнем
  каталоге), иначе — откат на `CreateProcess`;
- находка Б.2 (Important): имя `.cmd`-файла было общим для всех сессий на одном домашнем
  каталоге — две сессии, стартующие одновременно, дрались за файл.

Реальный `schtasks`/сеть здесь не нужны — `subprocess.run` и `is_listening` подменены, тест
детерминированный и быстрый (не полагается на удачное совпадение времени двух настоящих
процессов, как интеграционные пробы ревью)."""

from __future__ import annotations

import subprocess
import sys
import threading

import pytest

from odata1c import daemon as daemon_module

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Планировщик заданий — Windows")


def _fake_run_ok(*args, **kwargs):
    return subprocess.CompletedProcess(args, 0)


def test_успех_подтверждается_портом_и_pid_а_не_кодом_возврата_schtasks(tmp_path, monkeypatch):
    """Раунд правок 2, находка Б.1: `schtasks` отчитывается успехом (код 0), но демон фактически
    не поднялся (порт не слушается) — функция обязана вернуть `False` (откат на `CreateProcess`),
    а не поверить коду возврата. Мутация «доверять только коду возврата» ловится отдельно ниже."""
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    monkeypatch.setattr(daemon_module.subprocess, "run", _fake_run_ok)
    monkeypatch.setattr(daemon_module, "is_listening", lambda port, timeout=0.5: False)
    monkeypatch.setattr(daemon_module, "ОЖИДАНИЕ_ПОДТВЕРЖДЕНИЯ_SCHTASKS_S", 0.3)

    итог = daemon_module._spawn_via_scheduled_task(
        home, [sys.executable, "-m", "odata1c", "daemon"], home / "logs" / "daemon.log", 12345
    )
    assert итог is False


def test_успех_подтверждается_портом_и_pid_когда_оба_налицо(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "daemon.pid").write_text("1", encoding="utf-8")
    monkeypatch.setattr(daemon_module.subprocess, "run", _fake_run_ok)
    monkeypatch.setattr(daemon_module, "is_listening", lambda port, timeout=0.5: True)

    итог = daemon_module._spawn_via_scheduled_task(
        home, [sys.executable, "-m", "odata1c", "daemon"], home / "logs" / "daemon.log", 12345
    )
    assert итог is True


def test_порт_слушается_но_pid_чужой_не_считается_успехом(tmp_path, monkeypatch):
    """Половина находки Б.1: порт может слушать КТО УГОДНО (в реальном сценарии ревьюера — «левый»
    демон с искажённым `--home` на порту по умолчанию 7171). Без `daemon.pid` ИМЕННО в нашем
    домашнем каталоге факт «порт слушается» один ничего не доказывает."""
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    # daemon.pid НЕ создан — порт слушается, но не факт, что это наш процесс.
    monkeypatch.setattr(daemon_module.subprocess, "run", _fake_run_ok)
    monkeypatch.setattr(daemon_module, "is_listening", lambda port, timeout=0.5: True)
    monkeypatch.setattr(daemon_module, "ОЖИДАНИЕ_ПОДТВЕРЖДЕНИЯ_SCHTASKS_S", 0.3)

    итог = daemon_module._spawn_via_scheduled_task(
        home, [sys.executable, "-m", "odata1c", "daemon"], home / "logs" / "daemon.log", 12345
    )
    assert итог is False


def test_две_одновременные_сессии_не_делят_один_cmd_файл(tmp_path, monkeypatch):
    """Раунд правок 2, находка Б.2: имя `.cmd`-файла было общим (`daemon-launch.cmd`) для всех
    сессий на одном домашнем каталоге — вторая сессия, стартующая одновременно с первой, иногда
    получала `WinError 32` (файл занят другим процессом) или молча теряла своё содержимое под
    чужой перезаписью. Барьер держит оба потока внутри `subprocess.run` (то есть ПОСЛЕ того, как
    оба уже записали свой `.cmd`) ровно в момент, когда коллизия была бы видна — если бы оба
    потока писали в один и тот же путь, здесь оказался бы один файл, а не два."""
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "daemon.pid").write_text("1", encoding="utf-8")

    барьер = threading.Barrier(2)
    файлы_на_барьере: list[frozenset[str]] = []
    блокировка = threading.Lock()

    def fake_run(*args, **kwargs):
        if "/tr" in args[0]:
            # Только на /create. Два раунда одного и того же (циклического) барьера: первый —
            # «оба потока уже записали свой .cmd» (файлы точно на месте), второй — «снимок точно
            # взят» — без него после первого раунда поток-победитель гонки мог успеть добежать до
            # собственного `finally: cmd_путь.unlink()` (тем более быстро, если следующий шаг —
            # тривиальный `is_listening=True` без цикла ожидания) и удалить СВОЙ файл раньше, чем
            # другой поток вообще возьмёт снимок — тест был бы хрупким к постороннему таймингу, а
            # не к самой находке Б.2.
            барьер.wait(timeout=5)
            with блокировка:
                if not файлы_на_барьере:
                    файлы_на_барьере.append(
                        frozenset(p.name for p in (home / "logs").glob("daemon-launch-*.cmd"))
                    )
            барьер.wait(timeout=5)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(daemon_module.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon_module, "is_listening", lambda port, timeout=0.5: True)

    исходы: list[bool] = []

    def сессия(n: int) -> None:
        исход = daemon_module._spawn_via_scheduled_task(
            home,
            [sys.executable, "-m", "odata1c", "daemon", f"--marker-{n}"],
            home / "logs" / "daemon.log",
            12345,
        )
        исходы.append(исход)

    потоки = [threading.Thread(target=сессия, args=(n,)) for n in (1, 2)]
    for поток in потоки:
        поток.start()
    for поток in потоки:
        поток.join(timeout=10)

    assert all(исходы), f"обе сессии должны завершиться успехом: {исходы}"
    assert len(файлы_на_барьере) == 1
    # Ключевая проверка: в момент, когда оба потока уже записали свой `.cmd` и остановились на
    # барьере, на диске лежат ДВА разных файла — не один общий (иначе множество было бы {1 файл}).
    assert len(файлы_на_барьере[0]) == 2, (
        f"ожидалось два независимых .cmd-файла на барьере, найдено: {файлы_на_барьере[0]}"
    )
