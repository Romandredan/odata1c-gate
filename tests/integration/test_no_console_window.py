"""Подъём демона не показывает владельцу ни одного окна консоли (план M1d, задача «окно консоли»).

Находка владельца: при подъёме демона через Планировщик заданий на экране всплывало окно
`C:\\Windows\\SYSTEM32\\cmd.exe` — промежуточный `.cmd`-файл, через который задача перенаправляла
вывод и выставляла окружение. Владелец работает в Claude Code целый день, и всплывающее окно
у него ровно то, чего быть не должно.

Проверка — исполнением, а не сверкой текста команды: окна перечисляются через `EnumWindows`
(классы `ConsoleWindowClass` — классическое окно консоли, и `CASCADIA_HOSTING_WINDOW_CLASS` —
окно Windows Terminal, когда он назначен хостом консоли по умолчанию), снимок берётся до подъёма
и затем каждые 50 мс в течение всего ожидания готовности. Частый опрос обязателен: окно, которое
мелькнуло и закрылось, снимок «до и после» не поймал бы вовсе, а владельцу мелькнувшее окно
мешает ровно так же.

`win32gui` (pywin32) в зависимостях проекта нет и заводить его ради одного теста незачем —
`EnumWindows`/`GetClassNameW`/`IsWindowVisible` берутся из `user32` через `ctypes`: те же самые
функции, тот же результат.
"""

from __future__ import annotations

import ctypes
import os
import socket
import sys
import time

import pytest

from odata1c import daemon as daemon_module
from odata1c.config.home import ensure_home
from odata1c.config.writer import ensure_gate_secret
from odata1c.daemon import is_listening, spawn_detached
from odata1c.daemon import stop as daemon_stop

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="окна консоли — Windows")

ПРЕДЕЛ_ГОТОВНОСТИ_С = 25
ПРЕДЕЛ_ОСТАНОВКИ_С = 10
ШАГ_ОПРОСА_ОКОН_С = 0.05

# Классы окон, которые ОС создаёт для консольного процесса: классический conhost и окно
# Windows Terminal, если он назначен хостом консоли по умолчанию (Windows 11 — по умолчанию он).
КЛАССЫ_ОКОН_КОНСОЛИ = ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS")


def _консольные_окна() -> dict[int, str]:
    """Видимые окна консоли: дескриптор → заголовок. Невидимые окна не считаются: у каждого
    процесса с консолью окно есть всегда, вопрос только в том, показано ли оно владельцу."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    найденные: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def обход(hwnd, _параметр):
        if not user32.IsWindowVisible(hwnd):
            return True
        класс = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, класс, 256)
        if класс.value in КЛАССЫ_ОКОН_КОНСОЛИ:
            заголовок = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, заголовок, 512)
            найденные[int(hwnd)] = f"{класс.value}: {заголовок.value}"
        return True

    user32.EnumWindows(обход, 0)
    return найденные


def _свободный_порт() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as соединение:
        соединение.bind(("127.0.0.1", 0))
        return соединение.getsockname()[1]


def _подготовить_дом(tmp_path):
    home = tmp_path / "home"
    ensure_home(home)
    порт = _свободный_порт()
    (home / "daemon.yaml").write_text(f"port: {порт}\n", encoding="utf-8")
    ensure_gate_secret(home / "daemon.yaml")
    (home / "bases.yaml").write_text("bases: {}\n", encoding="utf-8")
    return home, порт


def _поднять_следя_за_окнами(home, порт: int) -> dict[int, str]:
    """Поднять демон и вернуть окна консоли, появившиеся за время подъёма (дескриптор → описание).

    Останавливает демон сама: оставленный процесс занял бы порт и задачу в Планировщике до конца
    прогона.
    """
    было = _консольные_окна()
    новые: dict[int, str] = {}
    try:
        spawn_detached(home, порт)
        предел = time.monotonic() + ПРЕДЕЛ_ГОТОВНОСТИ_С
        поднялся = False
        while time.monotonic() < предел:
            for дескриптор, описание in _консольные_окна().items():
                if дескриптор not in было:
                    новые[дескриптор] = описание
            if is_listening(порт):
                поднялся = True
                break
            time.sleep(ШАГ_ОПРОСА_ОКОН_С)
        assert поднялся, f"демон не поднялся за {ПРЕДЕЛ_ГОТОВНОСТИ_С} с — проверять нечего"
        # Ещё один проход после готовности: окно консоли могло появиться в тот же момент, что и
        # слушающий порт, и цикл выше вышел бы раньше, чем успел его увидеть.
        for дескриптор, описание in _консольные_окна().items():
            if дескриптор not in было:
                новые[дескриптор] = описание
        return новые
    finally:
        if is_listening(порт):
            daemon_stop(home)
            предел = time.monotonic() + ПРЕДЕЛ_ОСТАНОВКИ_С
            while is_listening(порт) and time.monotonic() < предел:
                time.sleep(0.2)


def test_подъём_демона_не_показывает_окна_консоли(tmp_path):
    """Основной путь — Планировщик заданий. До правки здесь появлялось ровно одно окно
    `ConsoleWindowClass` с заголовком `C:\\Windows\\SYSTEM32\\cmd.exe`."""
    home, порт = _подготовить_дом(tmp_path)
    новые = _поднять_следя_за_окнами(home, порт)
    assert not новые, f"при подъёме демона появились окна консоли: {новые}"


@pytest.mark.skipif(
    bool(os.environ.get("GITHUB_ACTIONS")),
    reason="рабочий стол исполнителя CI не изолирован: чужие окна консоли появляются сами",
)
def test_запасной_путь_тоже_не_показывает_окна_консоли(tmp_path, monkeypatch):
    """Запасной путь (`CreateProcess`), которым код пользуется при отказе Планировщика заданий,
    обязан быть таким же бесшумным: окно от него владелец отличить от штатного не смог бы.

    Пропуск на исполнителе GitHub Actions — вынужденный и только здесь. Проверка сравнивает
    ВСЕ видимые окна консоли до и после подъёма, а на исполнителе за эти секунды появляются
    посторонние окна (прогон ci на windows-latest поймал три `CASCADIA_HOSTING_WINDOW_CLASS:
    Terminal` — они не наши и от нашего кода не зависят). Приписать окно консоли своему процессу
    нечем: его рисует не сам процесс, а хост консоли (`conhost`/Windows Terminal), который в
    дереве процессов стоит отдельно. Основной путь (через Планировщик заданий) на CI не
    пропускается — если правка вернёт окно там, прогон это увидит.

    Прежде тест нёс ещё `filterwarnings` на `PytestUnraisableExceptionWarning`: запасной путь
    отвязывает долгоживущий процесс (`DETACHED_PROCESS`), и его `Popen` при сборке мусора
    предупреждал «process still running». Подавление снято — ссылку держит сам `spawn_detached`
    (`daemon._не_терять_ссылку`)."""
    home, порт = _подготовить_дом(tmp_path)
    monkeypatch.setattr(
        daemon_module, "_spawn_via_scheduled_task", lambda home, аргументы, журнал, port: False
    )
    новые = _поднять_следя_за_окнами(home, порт)
    assert not новые, f"при подъёме демона запасным путём появились окна консоли: {новые}"
