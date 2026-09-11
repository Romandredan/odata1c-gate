"""Межпроцессный замок вокруг `gate_secret` (план M1d, задача 6, раунд правок 3, пункт 2 —
находка Б.1 ревьюера).

Ветка снятия брошенного замка была мёртвым кодом: `st_mtime` (шкала epoch, ≈1,79·10⁹) сравнивался
с `time.monotonic()` (время с загрузки системы, ≈4,2·10⁴), то есть условие снятия ложно всегда.
Первый же держатель, упавший внутри критической секции (владелец прервал первый запуск, клиент MCP
снял лаунчер, отключение питания), выводил домашний каталог из строя навсегда: все три команды,
вызывающие `ensure_gate_secret` (`init`, `daemon`, `mcp`), падали через 10 с, а команды `doctor`,
которая могла бы про замок сказать, в шлюзе ещё нет.

Чинить одной шкалой времени было нельзя: снятие по возрасту само становится гонкой — A снимает
замок, который B уже перезахватил, и писателей `gate_secret` снова двое, а расхождение секрета
рвёт совпадение старых токенов гейта с новыми (инвариант 5). Поэтому снятие опирается на
ВЛАДЕЛЬЦА: замок брошен, только если процесса с записанным в нём pid в системе нет.

Живые процессы здесь настоящие: «мёртвый pid» — номер завершённого и дождавшегося дочернего
процесса, «живой pid» — номер работающего. Подделывать проверку живости нечем — она и есть предмет
проверки.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

import pytest

from odata1c import cli
from odata1c.config import writer
from odata1c.config.loader import ConfigError
from odata1c.config.writer import ensure_gate_secret


@pytest.fixture
def живой_процесс():
    """Настоящий работающий процесс, чей pid можно положить в файл замка."""
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


@pytest.fixture
def мёртвый_pid() -> int:
    """Номер процесса, который точно завершился: запускаем и дожидаемся выхода. Взять произвольное
    большое число было бы догадкой — система вправе его использовать."""
    процесс = subprocess.Popen([sys.executable, "-c", "pass"])
    процесс.wait(timeout=30)
    return процесс.pid


def _состарить(path: pathlib.Path, секунд: float) -> None:
    метка = time.time() - секунд
    os.utime(path, (метка, метка))


def test_живой_процесс_виден_живым_а_завершившийся_нет(живой_процесс, мёртвый_pid):
    """Опора всей ветки снятия — проверка живости. Если она врёт, врут и остальные тесты файла."""
    assert writer.процесс_жив(живой_процесс.pid) is True
    assert writer.процесс_жив(мёртвый_pid) is False


def test_замок_упавшего_держателя_снимается(tmp_path, мёртвый_pid):
    """Держатель упал, не убрав файл замка. Секрет обязан быть записан, замок — снят."""
    path = tmp_path / "daemon.yaml"
    path.write_text("port: 7171\n", encoding="utf-8")
    замок = tmp_path / "daemon.yaml.lock"
    замок.write_text(str(мёртвый_pid), encoding="ascii")

    начало = time.monotonic()
    секрет = ensure_gate_secret(path)

    assert секрет
    assert "gate_secret:" in path.read_text(encoding="utf-8")
    assert not замок.exists(), "снятый замок не должен оставаться на диске"
    assert time.monotonic() - начало < writer.ТАЙМАУТ_ЗАМКА_С / 2, (
        "брошенный замок снимается сразу, а не после полного ожидания"
    )


def test_замок_живого_держателя_не_снимается_даже_если_состарен(
    tmp_path, живой_процесс, monkeypatch
):
    """Ключевая проверка против починки «одной шкалой времени»: файл замка стар (час), но его
    держатель ЖИВ. Снятие по возрасту сочло бы такой замок протухшим и впустило бы второго
    писателя `gate_secret`; снятие по владельцу обязано оставить его в покое и честно отказать."""
    monkeypatch.setattr(writer, "ТАЙМАУТ_ЗАМКА_С", 0.5)
    path = tmp_path / "daemon.yaml"
    path.write_text("port: 7171\n", encoding="utf-8")
    замок = tmp_path / "daemon.yaml.lock"
    замок.write_text(str(живой_процесс.pid), encoding="ascii")
    _состарить(замок, 3600)

    with pytest.raises(ConfigError) as отказ:
        ensure_gate_secret(path)

    assert замок.exists(), "замок живого держателя снимать нельзя"
    assert "gate_secret" not in path.read_text(encoding="utf-8")
    assert str(замок) in отказ.value.hint, "подсказка обязана назвать файл: doctor в шлюзе ещё нет"


def test_замок_без_номера_держателя_снимается_только_по_возрасту(tmp_path, monkeypatch):
    """Замок прежней версии шлюза (pid внутрь не писался) и авария в микросекундном промежутке
    между созданием файла и записью номера выглядят одинаково — судим по возрасту. Свежий файл без
    номера — это, скорее всего, держатель, который прямо сейчас записывает свой pid."""
    # Порог протухания задаётся тестом заведомо маленьким, а файл старится заведомо сильно: тест
    # проверяет правило, а не текущее значение константы — иначе он перестал бы различать случаи,
    # стоило бы кому-нибудь поднять `ПРОТУХАНИЕ_ЗАМКА_С` до часа.
    monkeypatch.setattr(writer, "ТАЙМАУТ_ЗАМКА_С", 0.5)
    monkeypatch.setattr(writer, "ПРОТУХАНИЕ_ЗАМКА_С", 5.0)
    path = tmp_path / "daemon.yaml"
    path.write_text("port: 7171\n", encoding="utf-8")
    замок = tmp_path / "daemon.yaml.lock"
    замок.write_bytes(b"")

    with pytest.raises(ConfigError):
        ensure_gate_secret(path)
    assert замок.exists()

    _состарить(замок, 3600)
    assert ensure_gate_secret(path)
    assert not замок.exists()


def _дом_с_неснимаемым_замком(tmp_path: pathlib.Path, pid: int) -> pathlib.Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "daemon.yaml.lock").write_text(str(pid), encoding="ascii")
    return home


def test_init_на_неснимаемом_замке_отказывает_понятно(tmp_path, живой_процесс, monkeypatch, capsys):
    """Отказ по замку — обычная ошибка команды с кодом и подсказкой. До этого раунда `init` и
    `daemon` роняли голый `TimeoutError` трассировкой стека: он не попадал в общий перехват
    `cli.main`."""
    monkeypatch.setattr(writer, "ТАЙМАУТ_ЗАМКА_С", 0.5)
    home = _дом_с_неснимаемым_замком(tmp_path, живой_процесс.pid)

    код = cli.main(["--home", str(home), "init"])

    вывод = capsys.readouterr().out
    assert код == 1
    assert "[config_invalid]" in вывод
    assert "daemon.yaml.lock" in вывод
    assert "подсказка:" in вывод


def test_mcp_на_неснимаемом_замке_молчит_в_stdout(tmp_path, живой_процесс, monkeypatch, capsys):
    """Инвариант лаунчера: stdout команды `mcp` — канал JSON-RPC клиента, человеческий текст там
    протокольный шум. Отказ по замку обязан уйти в stderr целиком, вместе с подсказкой."""
    monkeypatch.setattr(writer, "ТАЙМАУТ_ЗАМКА_С", 0.5)
    home = _дом_с_неснимаемым_замком(tmp_path, живой_процесс.pid)

    код = cli.main(["--home", str(home), "mcp"])

    захват = capsys.readouterr()
    assert код == 1
    assert захват.out == ""
    assert "daemon.yaml.lock" in захват.err
    assert "подсказка:" in захват.err


@pytest.mark.skipif(sys.platform != "win32", reason="запрет удаления открытого файла — Windows")
def test_живой_замок_не_удаляется_средствами_ос(tmp_path):
    """Вторая линия защиты, независимая от проверки владельца: держатель держит файл замка
    открытым, а открытый файл Windows удалить не даёт. Значит живой замок не снимет никто, даже
    если проверка живости ошибётся."""
    замок = tmp_path / "daemon.yaml.lock"
    with writer._межпроцессный_замок(замок):
        assert замок.read_text(encoding="ascii") == str(os.getpid())
        assert writer._снять_замок(замок) is False
        assert замок.exists()
    assert not замок.exists(), "свой замок держатель убирает за собой"
