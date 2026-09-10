"""`daemon.pid` в гонке двух демонов за один порт (план M1d, задача 6, раунд правок 3, пункт 1 —
находка Б.2 ревьюера).

Откат лаунчера на `CreateProcess` случается теперь не только при отказе Планировщика заданий, но и
при любом превышении `ОЖИДАНИЕ_ПОДТВЕРЖДЕНИЯ_SCHTASKS_S` — то есть на исправной, но занятой машине.
Тогда на один порт претендуют два демона. Проигравший проходит предстартовую проверку `is_listening`
(порт в тот момент ещё свободен), валится уже на биндинге uvicorn — внутри `try` — и своего
pid-файла записать не успевает, а безусловный `unlink` в его `finally` уносил pid-файл ПОБЕДИТЕЛЯ.
Ревьюер воспроизвёл исход на настоящих процессах (`rv2_pid_race2.py`): «порт слушается: True,
daemon.pid существует: False, stop() смог остановить: False» — живой демон, который нечем
остановить штатно, и следующая сессия снова уходит на откат.

Здесь та же развилка пришпилена в точке, где происходит удаление: победитель — настоящий чужой
процесс, чей pid лежит в `daemon.pid`; проигравший — `serve()` этого процесса, чей uvicorn не смог
занять порт. Настоящий второй демон для этого не нужен и сделал бы тест зависимым от совпадения
времени двух холодных стартов.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from odata1c import daemon as daemon_module
from odata1c.config.home import ensure_home
from odata1c.config.writer import ensure_gate_secret
from odata1c.daemon import serve
from odata1c.daemon import stop as daemon_stop


class _UvicornНеЗанявшийПорт:
    """Замена `uvicorn.Server`, повторяющая поведение проигравшего гонку: `serve()` падает на
    биндинге (`WinError 10048`), `started` так и остаётся `False`. Настоящий uvicorn в этом месте
    вызывает `sys.exit(STARTUP_FAILURE)`; `serve()` шлюза одинаково пропускает наверх и то, и
    другое, а `OSError` не рвёт цикл событий теста."""

    def __init__(self, config):
        self.config = config
        self.started = False
        self.should_exit = False

    async def serve(self, sockets=None):
        raise OSError(10048, "порт уже занят другим процессом")


@pytest.fixture
def победитель():
    """Настоящий чужой процесс — «демон, выигравший гонку за порт»: его pid лежит в `daemon.pid`,
    и его же в конце останавливает `stop()`."""
    процесс = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield процесс
    finally:
        процесс.kill()
        процесс.wait(timeout=10)


async def test_проигравший_гонку_за_порт_не_уносит_pid_файл_победителя(
    tmp_path, monkeypatch, победитель
):
    home = tmp_path / "home"
    ensure_home(home)
    ensure_gate_secret(home / "daemon.yaml")
    pid_файл = home / "daemon.pid"
    pid_файл.write_text(str(победитель.pid), encoding="utf-8")

    # Предстартовая проверка порта проигравшего проходит: порт он видит свободным (именно так
    # выглядит окно, в котором победитель ещё не забиндил порт), а натыкается на победителя уже
    # внутри uvicorn.
    monkeypatch.setattr(daemon_module, "is_listening", lambda port, timeout=0.5: False)
    monkeypatch.setattr(daemon_module.uvicorn, "Server", _UvicornНеЗанявшийПорт)

    with pytest.raises(OSError):
        await serve(home, port=58231)

    assert pid_файл.exists(), "проигравший удалил чужой pid-файл"
    assert pid_файл.read_text(encoding="utf-8").strip() == str(победитель.pid)

    # Штатная остановка победителя после этого работает — ровно то, что теряется без сверки pid.
    assert daemon_stop(home) is True
    assert победитель.wait(timeout=10) is not None
    assert not pid_файл.exists()
