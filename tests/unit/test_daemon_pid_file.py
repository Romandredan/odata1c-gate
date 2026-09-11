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

Раунд правок 4 добавил сюда пункт 1 (нечитаемый `daemon.pid` ронял `finally` у `serve()` —
регрессия раунда 3) и пункт 2 (тот же класс дефекта в `stop()`: файл удалялся по пути, а не
опознанный, плюс неатомарная запись pid-файла).
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


# ---------------------------------------------------------------------------------------------
# Раунд правок 4: нечитаемый pid-файл (пункт 1, регрессия раунда 3) и та же дисциплина удаления
# в `stop()` (пункт 2, находка Б.7)
# ---------------------------------------------------------------------------------------------


async def test_нечитаемый_pid_файл_не_обрывает_уборку_в_finally(tmp_path, monkeypatch):
    """Раунд правок 4, пункт 1 (находка Б.1 ревьюера, регрессия раунда 3):
    `read_text(encoding="utf-8")` на невалидном UTF-8 бросает `UnicodeDecodeError`, а это
    `ValueError`, не `OSError`, — прежний перехват его пропускал. Вылетев из `finally` у `serve()`,
    он подменял исходную ошибку (ту, из-за которой демон и завершался) и обрывал уборку на первой
    же строке: `служба.aclose()` не вызывался, соединения с 1С оставались открытыми."""
    home = tmp_path / "home"
    ensure_home(home)
    ensure_gate_secret(home / "daemon.yaml")
    (home / "daemon.pid").write_bytes(b"\xff\xfe\x00")

    закрытия: list[int] = []

    class _СлужбаСоСчётчиком(daemon_module.ToolService):
        async def aclose(self):
            закрытия.append(1)
            await super().aclose()

    monkeypatch.setattr(daemon_module, "ToolService", _СлужбаСоСчётчиком)
    monkeypatch.setattr(daemon_module, "is_listening", lambda port, timeout=0.5: False)
    monkeypatch.setattr(daemon_module.uvicorn, "Server", _UvicornНеЗанявшийПорт)

    with pytest.raises(OSError) as отказ:
        await serve(home, port=58232)

    # Наверх должна уйти ИСХОДНАЯ ошибка, а не UnicodeDecodeError из уборки.
    assert not isinstance(отказ.value, UnicodeDecodeError)
    assert закрытия == [1], "finally оборвался до aclose() — соединения с 1С остались открытыми"
    assert (home / "daemon.pid").exists(), "чужой (нечитаемый) pid-файл трогать незачем"


def test_stop_не_уносит_pid_файл_нового_демона(tmp_path, monkeypatch):
    """Раунд правок 4, пункт 2 (находка Б.7): между чтением файла и его удалением умещается целый
    перезапуск демона — прежний завершается и убирает свой pid-файл, новый поднимается и пишет
    свой. `unlink` по пути уносил pid-файл ЖИВОГО нового демона, и получался ровно тот исход,
    который чинил раунд 3: демон работает, остановить его нечем.

    Перезапуск вставляется в настоящее окно — подменённый `os.kill` играет роль паузы между
    чтением и удалением."""
    home = tmp_path / "home"
    home.mkdir()
    pid_файл = home / "daemon.pid"
    pid_файл.write_text("4242", encoding="utf-8")

    def kill_и_перезапуск(pid, sig):
        assert pid == 4242
        pid_файл.write_text("777777", encoding="utf-8")  # поднялся новый демон

    monkeypatch.setattr(daemon_module.os, "kill", kill_и_перезапуск)
    # Прежний демон снят — это условие теста, а не то, что он проверяет. Без подмены исход зависел
    # бы от того, занят ли на машине номер 4242 посторонним процессом (ревью раунда 4, пункт 4:
    # `stop()` теперь судит по факту смерти).
    monkeypatch.setattr(daemon_module, "процесс_жив", lambda pid: False)

    assert daemon_stop(home) is True
    assert pid_файл.exists(), "stop() унёс pid-файл нового демона"
    assert pid_файл.read_text(encoding="utf-8").strip() == "777777"


def test_stop_не_удаляет_файл_с_недописанным_содержимым(tmp_path):
    """Второй вход в тот же исход: пустая строка в `daemon.pid` — это не мусор, а живой демон,
    чей pid-файл пишется прямо сейчас. Прежний `stop()` отчитывался «демон не запущен» и заодно
    удалял файл."""
    home = tmp_path / "home"
    home.mkdir()
    pid_файл = home / "daemon.pid"
    pid_файл.write_text("", encoding="utf-8")

    assert daemon_stop(home) is False
    assert pid_файл.exists(), "stop() удалил файл, который не смог разобрать"


# ---------------------------------------------------------------------------------------------
# Ревью раунда 4, пункт 4 (находка вне раунда): `stop()` отчитывался успехом, не сняв процесс.
#
# Прежний код подавлял отказ `os.kill` целиком, после чего удалял pid-файл и возвращал `True`
# независимо от исхода. Владелец видел ровно то, что и описано в находке: первый `stop()` —
# «успех» и файл унесён, демон при этом жив; второй — `False`, и штатно управлять демоном больше
# нечем. Решение — судить по ФАКТУ смерти процесса, а не по тому, подавилось ли исключение:
# разбор `OSError` по подклассам на Windows не работает, потому что `os.kill` идёт через
# `TerminateProcess` и `ProcessLookupError` может не прийти ни разу.
# ---------------------------------------------------------------------------------------------


def test_stop_не_уносит_файл_когда_процесс_остался_жив(tmp_path, monkeypatch, caplog):
    """Сердце находки: снять не смогли — значит отказ, а не успех. Файл остаётся (иначе демоном
    нельзя управлять штатно вообще), ответ `False`, причина — в журнале.

    Отказ `os.kill` задан принудительно: чем он был вызван у владельца, неизвестно, а механизм
    существует безотносительно триггера."""
    home = tmp_path / "home"
    home.mkdir()
    pid_файл = home / "daemon.pid"
    pid_файл.write_text("4242", encoding="utf-8")

    def отказ_kill(pid, sig):
        raise PermissionError(5, "отказано в доступе")

    monkeypatch.setattr(daemon_module.os, "kill", отказ_kill)
    monkeypatch.setattr(daemon_module, "процесс_жив", lambda pid: True)
    monkeypatch.setattr(daemon_module, "ОЖИДАНИЕ_СМЕРТИ_ДЕМОНА_С", 0.2)

    with caplog.at_level("WARNING", logger="odata1c.daemon"):
        итог = daemon_stop(home)

    assert итог is False, "не снятый процесс не может считаться остановленным"
    assert pid_файл.exists(), "файл живого демона унесён — управлять им больше нечем"
    assert pid_файл.read_text(encoding="utf-8").strip() == "4242"
    assert "4242" in caplog.text, f"владелец не узнает, что демон жив: {caplog.text!r}"


def test_stop_не_уносит_файл_даже_когда_kill_прошёл_а_процесс_жив(tmp_path, monkeypatch):
    """Вторая половина того же: `os.kill` может не бросить ничего и при этом не снять процесс
    (на Windows это `TerminateProcess`, завершение асинхронное). Решает не отсутствие исключения,
    а проверка живости."""
    home = tmp_path / "home"
    home.mkdir()
    pid_файл = home / "daemon.pid"
    pid_файл.write_text("4242", encoding="utf-8")

    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(daemon_module, "процесс_жив", lambda pid: True)
    monkeypatch.setattr(daemon_module, "ОЖИДАНИЕ_СМЕРТИ_ДЕМОНА_С", 0.2)

    assert daemon_stop(home) is False
    assert pid_файл.exists()


def test_stop_дожидается_смерти_а_не_отчитывается_сразу(tmp_path, monkeypatch):
    """Завершение на Windows асинхронное: сразу после `os.kill` процесс ещё жив. Ответ даётся по
    факту смерти, поэтому «жив, жив, мёртв» — это успех, а не отказ."""
    home = tmp_path / "home"
    home.mkdir()
    pid_файл = home / "daemon.pid"
    pid_файл.write_text("4242", encoding="utf-8")
    осталось_живым = [2]

    def жив(pid):
        if осталось_живым[0]:
            осталось_живым[0] -= 1
            return True
        return False

    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(daemon_module, "процесс_жив", жив)

    assert daemon_stop(home) is True
    assert not pid_файл.exists()
    assert осталось_живым[0] == 0, "проверка живости не повторялась — ответ дан до смерти"


def test_stop_убирает_осиротевший_файл_если_процесса_уже_нет(tmp_path):
    """Обратная сторона правила: отказ `os.kill` сам по себе провалом не является. Демон, упавший
    или снятый диспетчером задач, оставляет pid-файл без процесса за ним; `os.kill` на такой номер
    на Windows бросает `OSError` — это единственный способ ОС сказать «процесса нет». Работа при
    этом сделана: файл надо убрать, иначе он останется навсегда, а вместе с ним и риск снять
    чужой процесс, когда ОС переиспользует номер.

    Всё настоящее: номер принадлежал процессу, который действительно завершился, исключение
    бросает сама ОС, живость проверяется настоящим `OpenProcess`."""
    процесс = subprocess.Popen([sys.executable, "-c", "pass"])
    процесс.wait(timeout=30)
    home = tmp_path / "home"
    home.mkdir()
    pid_файл = home / "daemon.pid"
    pid_файл.write_text(str(процесс.pid), encoding="utf-8")

    assert daemon_stop(home) is True
    assert not pid_файл.exists(), "осиротевший pid-файл остался бы навсегда"


def test_stop_на_настоящем_процессе_отличает_снятый_от_живого(tmp_path, monkeypatch, победитель):
    """То же на настоящем процессе и настоящей проверке живости (`OpenProcess` +
    `GetExitCodeProcess`), без подмены чего-либо, кроме самого отказа `os.kill`.

    Сначала отказ: процесс жив, файл на месте, ответ `False`. Затем обычный вызов: процесс снят,
    файл убран, ответ `True`. Одна и та же функция на одном и том же процессе отвечает по-разному —
    ровно то, чего прежний `stop()` не мог по построению."""
    home = tmp_path / "home"
    home.mkdir()
    pid_файл = home / "daemon.pid"
    pid_файл.write_text(str(победитель.pid), encoding="utf-8")

    with monkeypatch.context() as отказ:
        отказ.setattr(
            daemon_module.os,
            "kill",
            lambda pid, sig: (_ for _ in ()).throw(PermissionError(5, "отказано в доступе")),
        )
        отказ.setattr(daemon_module, "ОЖИДАНИЕ_СМЕРТИ_ДЕМОНА_С", 0.3)
        assert daemon_stop(home) is False

    assert победитель.poll() is None, "процесс не должен был пострадать"
    assert pid_файл.exists()

    assert daemon_stop(home) is True
    assert победитель.wait(timeout=10) is not None
    assert not pid_файл.exists()


def test_pid_файл_пишется_атомарно(tmp_path, monkeypatch):
    """Та же находка с другой стороны: `write_text` — это «усечь, потом записать», и читатель,
    попавший в промежуток, видит пустой файл. Атомарность наблюдаема только по механизму записи,
    поэтому тест белого ящика: замена происходит через `os.replace` временного файла, и временных
    файлов после записи не остаётся."""
    pid_файл = tmp_path / "daemon.pid"
    pid_файл.write_text("1", encoding="utf-8")
    замены: list[tuple[str, str]] = []
    настоящий_replace = daemon_module.os.replace

    def следящий_replace(откуда, куда):
        замены.append((str(откуда), str(куда)))
        настоящий_replace(откуда, куда)

    monkeypatch.setattr(daemon_module.os, "replace", следящий_replace)

    daemon_module._записать_pid_файл(pid_файл, 4242)

    assert pid_файл.read_text(encoding="utf-8") == "4242"
    assert [куда for _, куда in замены] == [str(pid_файл)], (
        "pid-файл должен появляться целиком одной заменой, а не перезаписью на месте"
    )
    assert not list(tmp_path.glob("daemon.pid.tmp-*")), "временный файл не убран"


def test_запись_pid_файла_переживает_занятость_файла_читателем(tmp_path, monkeypatch):
    """На Windows файл, открытый читателем обычными средствами, нельзя ни удалить, ни заменить —
    `os.replace` падает `PermissionError`. Хендл читателя живёт микросекунды, поэтому запись
    повторяется: без повторов редкое совпадение с `odata1c daemon stop` роняло бы старт демона —
    новый отказ вместо того, который чинится."""
    pid_файл = tmp_path / "daemon.pid"
    настоящий_replace = daemon_module.os.replace
    осталось_отказов = [2]

    def капризный_replace(откуда, куда):
        if осталось_отказов[0]:
            осталось_отказов[0] -= 1
            raise PermissionError(32, "файл занят другим процессом")
        настоящий_replace(откуда, куда)

    monkeypatch.setattr(daemon_module.os, "replace", капризный_replace)

    daemon_module._записать_pid_файл(pid_файл, 4242, пауза=0.001)

    assert pid_файл.read_text(encoding="utf-8") == "4242"
    assert осталось_отказов[0] == 0
