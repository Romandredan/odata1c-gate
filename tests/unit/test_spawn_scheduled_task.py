"""`daemon._spawn_via_scheduled_task` — подъём демона через Планировщик заданий.

Раунд правок 2 (план M1d, задача 6):
- находка Б.1 (Critical): код возврата `schtasks` не доказывает, что процесс поднялся — успех
  подтверждается фактом (порт слушается И `daemon.pid` появился ИМЕННО в нашем домашнем
  каталоге), иначе — откат на `CreateProcess`;
- находка Б.2 (Important): имя пускового файла было общим для всех сессий на одном домашнем
  каталоге — две сессии, стартующие одновременно, дрались за файл.

Задача «окно консоли»: промежуточный `.cmd` убран из цепочки — он был консольным процессом и
рисовал владельцу окно. Вместо него задача запускает оконный интерпретатор `pythonw.exe` с
пусковым файлом на Python. Тесты прежних раундов про экранирование `cmd.exe` (кавычка и перевод
строки рвали строку `set`, `%` требовал удвоения, длина за 8191 роняла `cmd.exe`, `!` подменялся
при включённом отложенном раскрытии) переписаны под новый механизм: проверяется не то, что
опасное значение отброшено, а то, что оно доходит до процесса ЦЕЛЫМ — литерал Python выдерживает
всё, что рвало `.cmd`.

Реальный `schtasks`/сеть здесь не нужны — `subprocess.run` и `is_listening` подменены, тесты
детерминированные и быстрые; там, где важна не форма файла, а его поведение, пусковой файл
исполняется настоящим интерпретатором (Планировщик при этом всё равно подменён, задач в системе
не остаётся)."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

from odata1c import daemon as daemon_module

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Планировщик заданий — Windows")

# Полезная нагрузка вместо демона: выгружает в JSON всё, что нужно проверить о процессе, который
# поднял пусковой файл, — окружение, sys.path, sys.argv.
ДАМП_ПРОЦЕССА = (
    "import json, os, sys, pathlib\n"
    "pathlib.Path(sys.argv[1]).write_text(\n"
    "    json.dumps({'окружение': dict(os.environ), 'путь': sys.path, 'argv': sys.argv}),\n"
    "    encoding='utf-8',\n"
    ")\n"
)


def _fake_run_ok(*args, **kwargs):
    return subprocess.CompletedProcess(args, 0)


def _путь_пускового(аргументы_schtasks: list[str]) -> pathlib.Path:
    """Путь к пусковому файлу из строки задачи `"<интерпретатор>" -X utf8 "<пусковой файл>"`."""
    строка = аргументы_schtasks[аргументы_schtasks.index("/tr") + 1]
    return pathlib.Path(строка.rsplit('"', 2)[-2])


def _дом(tmp_path, *, с_pid: bool = True) -> pathlib.Path:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    if с_pid:
        (home / "daemon.pid").write_text("1", encoding="utf-8")
    return home


def _строка_задачи(home, monkeypatch, аргументы: list[str]) -> str:
    """Прогнать `_spawn_via_scheduled_task` и вернуть строку, ушедшую в `/tr`."""
    снимок: list[str] = []

    def fake_run(*args, **kwargs):
        if "/tr" in args[0]:
            снимок.append(args[0][args[0].index("/tr") + 1])
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(daemon_module.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon_module, "is_listening", lambda port, timeout=0.5: True)
    daemon_module._spawn_via_scheduled_task(home, аргументы, home / "logs" / "daemon.log", 12345)
    return снимок[0]


def _исполнить_пусковой(home, monkeypatch, аргументы: list[str]) -> dict:
    """Прогнать `_spawn_via_scheduled_task` и ВЫПОЛНИТЬ созданный пусковой файл настоящим
    интерпретатором — вместо Планировщика заданий, который подменён. Возвращает снимок текста
    файла и результат его выполнения: сверка текста доказала бы только форму, а нас интересует,
    что дошло до процесса.

    Из окружения выполняемого процесса вычищается ровно то, что пусковой файл обязан доставить
    сам (`ПЕРЕДАВАЕМЫЕ_ПЕРЕМЕННЫЕ_ОКРУЖЕНИЯ`). Без этого тест ничего бы не проверял: обычный
    дочерний процесс наследует окружение родителя и увидел бы те же значения, даже если пусковой
    файл не доставил ни одного. Настоящий Планировщик заданий как раз ничего не наследует — он
    берёт окружение из профиля пользователя (находка Б.5), и очистка воспроизводит это условие.
    """
    итог: dict = {}
    окружение_задачи = {
        имя: значение
        for имя, значение in os.environ.items()
        if имя not in daemon_module.ПЕРЕДАВАЕМЫЕ_ПЕРЕМЕННЫЕ_ОКРУЖЕНИЯ
    }
    # `monkeypatch` подменяет атрибут самого модуля `subprocess`, а не копию в `daemon`: без
    # ссылки на настоящую функцию, взятой ДО подмены, пусковой файл «запускался» бы подменой —
    # то есть не запускался бы вовсе, а тест радостно зеленел.
    настоящий_run = subprocess.run

    def fake_run(*args, **kwargs):
        if "/tr" in args[0]:
            путь = _путь_пускового(args[0])
            итог["текст"] = путь.read_text(encoding="utf-8")
            итог["путь"] = путь
            итог["выполнение"] = настоящий_run(
                [sys.executable, "-X", "utf8", str(путь)],
                capture_output=True,
                text=True,
                timeout=120,
                env=окружение_задачи,
            )
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(daemon_module.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon_module, "is_listening", lambda port, timeout=0.5: True)
    daemon_module._spawn_via_scheduled_task(home, аргументы, home / "logs" / "daemon.log", 12345)
    return итог


# -------------------------------------------------------------------------------------------
# Находка Б.1: успех подтверждается фактом, а не кодом возврата schtasks.
# -------------------------------------------------------------------------------------------


def test_успех_подтверждается_портом_и_pid_а_не_кодом_возврата_schtasks(tmp_path, monkeypatch):
    """Раунд правок 2, находка Б.1: `schtasks` отчитывается успехом (код 0), но демон фактически
    не поднялся (порт не слушается) — функция обязана вернуть `False` (откат на `CreateProcess`),
    а не поверить коду возврата."""
    home = _дом(tmp_path, с_pid=False)
    monkeypatch.setattr(daemon_module.subprocess, "run", _fake_run_ok)
    monkeypatch.setattr(daemon_module, "is_listening", lambda port, timeout=0.5: False)
    monkeypatch.setattr(daemon_module, "ОЖИДАНИЕ_ПОДТВЕРЖДЕНИЯ_SCHTASKS_S", 0.3)

    итог = daemon_module._spawn_via_scheduled_task(
        home, [sys.executable, "-m", "odata1c", "daemon"], home / "logs" / "daemon.log", 12345
    )
    assert итог is False


def test_успех_подтверждается_портом_и_pid_когда_оба_налицо(tmp_path, monkeypatch):
    home = _дом(tmp_path)
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
    home = _дом(tmp_path, с_pid=False)  # daemon.pid НЕ создан
    monkeypatch.setattr(daemon_module.subprocess, "run", _fake_run_ok)
    monkeypatch.setattr(daemon_module, "is_listening", lambda port, timeout=0.5: True)
    monkeypatch.setattr(daemon_module, "ОЖИДАНИЕ_ПОДТВЕРЖДЕНИЯ_SCHTASKS_S", 0.3)

    итог = daemon_module._spawn_via_scheduled_task(
        home, [sys.executable, "-m", "odata1c", "daemon"], home / "logs" / "daemon.log", 12345
    )
    assert итог is False


# -------------------------------------------------------------------------------------------
# Задача «окно консоли»: в цепочке запуска не должно остаться консольного процесса.
# -------------------------------------------------------------------------------------------


def test_задача_запускает_интерпретатор_с_пусковым_файлом_а_не_cmd(tmp_path, monkeypatch):
    """Окно владельцу рисовал `cmd.exe` — консольный процесс, стоявший в цепочке между
    Планировщиком заданий и демоном. Задача теперь запускает сам интерпретатор с пусковым
    файлом на Python; `.cmd` в строке задачи не остаётся вовсе. Факт отсутствия окна проверяет
    `tests/integration/test_no_console_window.py` — здесь проверяется причина, а не следствие."""
    home = _дом(tmp_path)
    интерпретатор = daemon_module._интерпретатор_без_консоли()

    строка = _строка_задачи(
        home, monkeypatch, [интерпретатор, "-X", "utf8", "-m", "odata1c", "daemon"]
    )

    assert ".cmd" not in строка.lower(), f"в цепочке запуска остался cmd-файл: {строка}"
    assert "cmd.exe" not in строка.lower()
    assert строка.startswith(f'"{интерпретатор}"'), строка
    assert строка.endswith('.py"'), строка
    # `-X utf8` заменил переменную окружения PYTHONUTF8=1, которую Планировщик заданий не
    # принимает: без общего для процесса режима utf-8 половина строк журнала ложилась в файл в
    # кодовой странице ANSI (см. докстринг spawn_detached).
    assert "-X utf8" in строка, строка


def test_пусковой_файл_удаляет_себя_при_запуске(tmp_path, monkeypatch):
    """В пусковом файле лежат значения переменных окружения владельца, а в `HTTP_PROXY` бывает
    пароль — файл обязан исчезнуть с диска сразу, как только его прочитал интерпретатор.

    Удаляет себя он сам: только он знает момент, когда текст уже прочитан. Прежний `.cmd`
    удалялся вызывающим кодом, и тот состязался за файл с ОС, которая его в этот момент
    открывала."""
    home = _дом(tmp_path)
    дамп = tmp_path / "дамп.json"
    скрипт = tmp_path / "нагрузка.py"
    скрипт.write_text(ДАМП_ПРОЦЕССА, encoding="utf-8")

    итог = _исполнить_пусковой(
        home, monkeypatch, [sys.executable, "-X", "utf8", str(скрипт), str(дамп)]
    )

    assert дамп.exists(), f"нагрузка не выполнилась: {итог['выполнение']}"
    assert not итог["путь"].exists(), "пусковой файл остался на диске после запуска"
    assert not list((home / "logs").glob("launch-*.py"))


def test_брошенный_пусковой_файл_убирается_по_возрасту(tmp_path, monkeypatch):
    """Если задача так и не выполнилась (Планировщик отчитался успехом, а процесс не поднялся —
    находка Б.1), удалить пусковой файл некому: сам он не запускался, а вызывающий код его не
    трогает намеренно. Такой файл — забытое на диске значение `HTTP_PROXY` с паролем, поэтому
    следующий подъём демона убирает всё старше часа. Свежий чужой файл (соседняя сессия, которая
    как раз стартует) при этом не трогается."""
    home = _дом(tmp_path)
    давно = time.time() - daemon_module.ВОЗРАСТ_БРОШЕННОГО_ПУСКОВОГО_ФАЙЛА_С - 60
    брошенный = home / "logs" / "launch-старый.py"
    брошенный.write_text("# забытый\n", encoding="utf-8")
    os.utime(брошенный, (давно, давно))
    # Файл прежнего механизма: их больше никто не создаёт, но в домашних каталогах, поработавших
    # до задачи «окно консоли», они лежат — с теми же значениями переменных окружения внутри.
    от_прежнего_механизма = home / "logs" / "daemon-launch.cmd"
    от_прежнего_механизма.write_text("@echo off\r\n", encoding="utf-8")
    os.utime(от_прежнего_механизма, (давно, давно))
    чужой_свежий = home / "logs" / "launch-соседний.py"
    чужой_свежий.write_text("# соседняя сессия\n", encoding="utf-8")

    monkeypatch.setattr(daemon_module.subprocess, "run", _fake_run_ok)
    monkeypatch.setattr(daemon_module, "is_listening", lambda port, timeout=0.5: True)
    daemon_module._spawn_via_scheduled_task(
        home, [sys.executable, "-m", "odata1c", "daemon"], home / "logs" / "daemon.log", 12345
    )

    assert not брошенный.exists(), "брошенный пусковой файл остался на диске"
    assert not от_прежнего_механизма.exists(), "файл прежнего механизма остался на диске"
    assert чужой_свежий.exists(), "уборка унесла файл соседней сессии, а не только брошенный"


def test_слишком_длинная_строка_задачи_не_отдаётся_планировщику(tmp_path, monkeypatch):
    """`schtasks.exe` отвергает значение `/tr` длиннее 261 символа (проверено исполнением:
    235 принимается, 335 — «ERROR: Value for '/tr' option cannot be more than 261 character(s)»).
    Строка длиннее предела до Планировщика не доводится вовсе: вместо невнятного отказа утилиты
    в журнал попадает причина (длинный путь), а демон поднимается запасным путём."""
    home = _дом(tmp_path)
    monkeypatch.setattr(daemon_module, "ПРЕДЕЛ_TR", 10)
    вызовы: list[list[str]] = []

    def fake_run(*args, **kwargs):
        вызовы.append(list(args[0]))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(daemon_module.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon_module, "is_listening", lambda port, timeout=0.5: True)

    итог = daemon_module._spawn_via_scheduled_task(
        home, [sys.executable, "-m", "odata1c", "daemon"], home / "logs" / "daemon.log", 12345
    )

    assert итог is False, "слишком длинная строка задачи обязана уводить на запасной путь"
    assert not вызовы, f"задачу всё-таки попытались создать: {вызовы}"
    assert not list((home / "logs").glob("launch-*.py")), "пусковой файл остался на диске"


# -------------------------------------------------------------------------------------------
# Находка Б.5 и правки раундов 3–4: окружение владельца доходит до процесса демона ЦЕЛЫМ.
# -------------------------------------------------------------------------------------------


def test_враждебные_значения_окружения_доходят_до_процесса_целыми(tmp_path, monkeypatch):
    """Переписанные тесты раундов 3 и 4 (находки Б.4, Б.2, Б.3). Через `.cmd` каждое из этих
    значений либо ломало файл, либо молча отбрасывалось:

    - кавычка закрывала кавычку `set "имя=значение"`, и остаток строки `cmd.exe` выполнял как
      отдельную команду (ревьюер воспроизвёл выполнение посторонней команды маркером);
    - перевод строки рвал файл тем же образом и без всякой кавычки;
    - `!` подменялся содержимым посторонней переменной, если у владельца глобально включено
      отложенное раскрытие (`HKCU\\Software\\Microsoft\\Command Processor\\DelayedExpansion`);
    - `%` требовал удвоения;
    - строка `set` длиннее 8191 символа роняла `cmd.exe` целиком, и демон не стартовал вовсе.

    В пусковом файле на Python значение лежит строковым литералом и доходит целым — проверяется
    исполнением, а не сверкой текста: в тексте оно записано escape-последовательностями."""
    home = _дом(tmp_path)
    дамп = tmp_path / "дамп.json"
    скрипт = tmp_path / "нагрузка.py"
    скрипт.write_text(ДАМП_ПРОЦЕССА, encoding="utf-8")
    маркер = tmp_path / "ПОСТОРОННЯЯ_КОМАНДА.txt"
    значения = {
        "SSL_CERT_FILE": f'x" & echo вырвались > "{маркер}" & rem ',
        "NO_PROXY": "localhost\r\nstart calc.exe",
        "SSL_CERT_DIR": "a!PATH!b",
        "HTTP_PROXY": "http://пользователь:пароль@прокси.test:3128/100%значение",
        "CURL_CA_BUNDLE": "д" * 9000,
    }
    for имя, значение in значения.items():
        monkeypatch.setenv(имя, значение)

    итог = _исполнить_пусковой(
        home, monkeypatch, [sys.executable, "-X", "utf8", str(скрипт), str(дамп)]
    )

    assert дамп.exists(), f"нагрузка не выполнилась: {итог['выполнение']}"
    окружение = json.loads(дамп.read_text(encoding="utf-8"))["окружение"]
    for имя, значение in значения.items():
        assert окружение.get(имя) == значение, f"{имя} дошла искажённой"
    assert not маркер.exists(), "значение с кавычкой выполнилось как команда"


def test_переменная_вне_списка_не_переносится(tmp_path, monkeypatch):
    """Переносится не всё окружение, а перечисленный список (прокси, корневые сертификаты,
    PYTHONPATH): Планировщик заданий берёт окружение из профиля пользователя, и подменять ему
    всё подряд — не «починить прокси», а создать процессу условия, в которых его никто не
    отлаживал."""
    home = _дом(tmp_path)
    monkeypatch.setenv("ODATA1C_ПОСТОРОННЯЯ", "значение")
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    текст = _исполнить_пусковой(home, monkeypatch, [sys.executable, "-m", "odata1c", "daemon"])[
        "текст"
    ]

    assert "ODATA1C_ПОСТОРОННЯЯ" not in текст
    assert "REQUESTS_CA_BUNDLE" not in текст


def test_pythonpath_попадает_в_sys_path_в_том_же_порядке(tmp_path, monkeypatch):
    """`PYTHONPATH` читает интерпретатор при старте, а пусковой файл выставляет окружение уже
    после — сам по себе `os.environ` на `sys.path` не влияет. Записи добавляются в начало пути
    поиска в исходном порядке, иначе модуль будет найден не тот.

    Заодно это единственный тест формы `-m <модуль>` — той самой, которой поднимается демон:
    нагрузка лежит модулем в каталоге из `PYTHONPATH` и находится только благодаря ему."""
    home = _дом(tmp_path)
    дамп = tmp_path / "дамп.json"
    первый = tmp_path / "первый"
    второй = tmp_path / "второй"
    первый.mkdir()
    второй.mkdir()
    (первый / "нагрузка.py").write_text(ДАМП_ПРОЦЕССА, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", f"{первый}{os.pathsep}{второй}")

    итог = _исполнить_пусковой(home, monkeypatch, [sys.executable, "-m", "нагрузка", str(дамп)])

    assert дамп.exists(), f"модуль из PYTHONPATH не запустился: {итог['выполнение']}"
    снимок = json.loads(дамп.read_text(encoding="utf-8"))
    assert снимок["путь"][:2] == [str(первый), str(второй)], (
        f"записи PYTHONPATH не в начале пути поиска или не в том порядке: {снимок['путь'][:4]}"
    )
    # sys.argv собран по правилам интерпретатора: для `-m` нулевой элемент — имя модуля.
    assert снимок["argv"] == ["нагрузка", str(дамп)]


def test_ошибка_до_старта_демона_попадает_в_журнал_запуска(tmp_path, monkeypatch):
    """Под `pythonw.exe` у процесса нет ни консоли, ни `sys.stdout`/`sys.stderr` — необработанное
    исключение по умолчанию не оставило бы следа нигде. Пусковой файл открывает журнал запуска
    ДО импорта полезной нагрузки, поэтому ошибка, случившаяся раньше, чем демон завёл свой
    собственный журнал (сорванный импорт, битая установка пакета), всё равно видна."""
    home = _дом(tmp_path)
    скрипт = tmp_path / "падение.py"
    скрипт.write_text("raise RuntimeError('нагрузка не поднялась')\n", encoding="utf-8")

    _исполнить_пусковой(home, monkeypatch, [sys.executable, str(скрипт)])

    журнал = (home / "logs" / "daemon-launch.log").read_text(encoding="utf-8")
    assert "пусковой файл" in журнал, f"в журнал запуска не попала даже строка старта: {журнал}"
    assert "RuntimeError" in журнал and "нагрузка не поднялась" in журнал, журнал


def test_две_одновременные_сессии_не_делят_один_пусковой_файл(tmp_path, monkeypatch):
    """Раунд правок 2, находка Б.2: имя пускового файла было общим (`daemon-launch.cmd`) для всех
    сессий на одном домашнем каталоге — вторая сессия, стартующая одновременно с первой, иногда
    получала `WinError 32` (файл занят другим процессом) или молча теряла своё содержимое под
    чужой перезаписью. Барьер держит оба потока внутри `subprocess.run` (то есть ПОСЛЕ того, как
    оба уже записали свой файл) ровно в момент, когда коллизия была бы видна — если бы оба потока
    писали в один и тот же путь, здесь оказался бы один файл, а не два."""
    home = _дом(tmp_path)

    барьер = threading.Barrier(2)
    файлы_на_барьере: list[frozenset[str]] = []
    блокировка = threading.Lock()

    def fake_run(*args, **kwargs):
        if "/tr" in args[0]:
            # Только на /create. Два раунда одного и того же (циклического) барьера: первый —
            # «оба потока уже записали свой файл» (файлы точно на месте), второй — «снимок точно
            # взят».
            барьер.wait(timeout=5)
            with блокировка:
                if not файлы_на_барьере:
                    файлы_на_барьере.append(
                        frozenset(p.name for p in (home / "logs").glob("launch-*.py"))
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
    # Ключевая проверка: в момент, когда оба потока уже записали свой файл и остановились на
    # барьере, на диске лежат ДВА разных файла — не один общий.
    assert len(файлы_на_барьере[0]) == 2, (
        f"ожидалось два независимых пусковых файла на барьере, найдено: {файлы_на_барьере[0]}"
    )
