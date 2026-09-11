"""`daemon._обеспечить_потоки_вывода` — демон переживает отсутствие `sys.stdout`/`sys.stderr`
(задача «окно консоли»).

Демон поднимается оконным интерпретатором `pythonw.exe`: он не создаёт консоли, а значит окна —
ровно того, что раздражало владельца. Плата за это — процесс без стандартных потоков: CPython
выставляет `sys.stdout` и `sys.stderr` в `None`, и любая запись туда становится ошибкой у
пишущего (`logging.StreamHandler`, который заводит себе uvicorn; вывод необработанного
исключения; всё, кроме встроенного `print`, который единственный проверяет `None` сам).
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

from odata1c.daemon import _обеспечить_потоки_вывода


@pytest.fixture
def закрыть_подменённые():
    """Закрыть то, что функция открыла: без этого файл доживёт до сборки мусора и `pytest`,
    который считает предупреждения ошибками, свалится на `ResourceWarning` в постороннем тесте."""
    открытые: list = []
    yield открытые
    for поток in открытые:
        поток.close()


def test_потоки_на_месте_не_подменяются(tmp_path, monkeypatch, capsys):
    """Функция вмешивается только там, где потоков нет. Демон, запущенный обычным
    интерпретатором (`odata1c daemon --foreground` в консоли владельца), должен и дальше писать
    в консоль, а не в файл."""
    _обеспечить_потоки_вывода(tmp_path)

    print("строка осталась в обычном выводе")
    assert "строка осталась в обычном выводе" in capsys.readouterr().out
    assert not (tmp_path / "logs" / "daemon.log").exists()


def test_без_потоков_вывод_уходит_в_журнал_демона(tmp_path, monkeypatch, закрыть_подменённые):
    """Процесс без консоли: `sys.stdout` и `sys.stderr` равны `None`. После вызова оба — открытый
    журнал демона, и то, что раньше ушло бы в консоль, лежит в файле читаемым utf-8."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    _обеспечить_потоки_вывода(tmp_path)

    закрыть_подменённые.extend({sys.stdout, sys.stderr})
    assert sys.stdout is not None and sys.stderr is not None
    print("обычная строка с кириллицей")
    # Тот же путь, которым пишет uvicorn: StreamHandler без явного потока берёт sys.stderr.
    обработчик = logging.StreamHandler()
    обработчик.emit(
        logging.LogRecord("проба", logging.WARNING, __file__, 1, "строка журнала", None, None)
    )
    обработчик.flush()

    журнал = (tmp_path / "logs" / "daemon.log").read_text(encoding="utf-8")
    assert "обычная строка с кириллицей" in журнал
    assert "строка журнала" in журнал


def test_настоящие_дескрипторы_процесса_не_перехватываются(tmp_path, monkeypatch, capfd):
    """Дескрипторы 1 и 2 подменяются только тогда, когда их нет. Иначе функция увела бы в файл
    весь вывод процесса уровня ОС — в прогоне тестов это вывод самого pytest, и сломалось бы не
    только то место, где её вызвали."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    _обеспечить_потоки_вывода(tmp_path)

    поток = sys.stdout
    monkeypatch.undo()  # вернуть настоящие sys.stdout/sys.stderr до записи в дескриптор
    try:
        # Запись мимо sys.stdout, прямо в дескриптор — именно его и перехватывает dup2.
        os.write(1, "прямо в дескриптор 1\n".encode())
    finally:
        поток.close()

    assert "прямо в дескриптор 1" in capfd.readouterr().out
    assert "прямо в дескриптор 1" not in (tmp_path / "logs" / "daemon.log").read_text(
        encoding="utf-8"
    )
