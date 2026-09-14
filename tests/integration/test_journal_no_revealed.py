"""Журнал НАСТОЯЩЕГО демона не хранит раскрытых гейтом значений (Ruling 31, раунд правок 2 по
`stop()` и журналу, пункт 2).

httpx пишет на INFO строку `HTTP Request: GET <полный адрес>` про каждый запрос к 1С, а в адресе
стоит УЖЕ раскрытое гейтом настоящее значение — ИНН, по которому модель искала токеном. Журнал
владельца отдают при разборе ошибок, поэтому инвариант 1 («никогда») распространяется и на него.

Почему нужен настоящий процесс, а не `serve()` в процессе pytest. Прошлый раунд закрыл этот канал
случайно: `MCPServer.__init__` (SDK `mcp`) вызывает `logging.basicConfig(level=INFO, …)`, который
срабатывает только на ПУСТОМ корневом логгере, — а журнал демона стал вешать обработчик на корень
раньше `build_server`. Под pytest корень никогда не пуст (обработчики плагина журналирования), и
`basicConfig` там — пустая операция при любом порядке вызовов: перестановку `build_server` раньше
журнала такой тест не увидел бы. В свежем процессе, поднятом тем же `spawn_detached`, что и в
работе (оконный интерпретатор; stdout/stderr — в `logs/daemon-launch.log` при подъёме через
Планировщик заданий или в `logs/daemon.log` при запасном пути), механизм воспроизводится целиком.
Поэтому проверяются ВСЕ файлы `logs/`, а не один `daemon.log`: при перестановке строка httpx
уходит и в файловый обработчик журнала, и в обработчик SDK на stderr.

Что проверено мутациями (см. отчёт раунда): на коде ДО правки перестановка `build_server` раньше
журнала делает этот тест красным — строка httpx с ИНН появляется в `daemon.log`; на коде ПОСЛЕ
правки та же перестановка остаётся зелёной — закрытие больше не зависит от порядка; без явного
уровня `httpx` и с перестановкой — снова красный.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import socket
import sys
import time

import pytest
from fake_1c import EDMX_ФИКСТУРА, ИНН, запущенная
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from odata1c.cli import main
from odata1c.config.loader import load_config
from odata1c.daemon import daemon_url, is_listening, spawn_detached
from odata1c.gate.service import refresh_policy
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository

# Обе целевые ОС (M3 задача 9): нужен не Windows, а отдельный процесс демона — на Linux его
# поднимает тот же `spawn_detached`, только без Планировщика заданий.
pytestmark = pytest.mark.skipif(
    sys.platform not in ("win32", "linux"), reason="подъём демона — Windows и Linux"
)

ПРЕДЕЛ_ГОТОВНОСТИ_С = 25
ПРЕДЕЛ_ВЫЗОВОВ_С = 30
ПРЕДЕЛ_ОСТАНОВКИ_С = 10
ТОКЕН_ИНН = re.compile(r"\[\[inn:[^\]]+\]\]")


def _свободный_порт() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as соединение:
        соединение.bind(("127.0.0.1", 0))
        return соединение.getsockname()[1]


def _дом(tmp_path, порт_демона: int, порт_1с: int):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    with (home / "daemon.yaml").open("a", encoding="utf-8") as поток:
        поток.write(f"port: {порт_демона}\n")
    (home / "bases.yaml").write_text(
        "default: ut\n"
        "bases:\n"
        "  ut:\n"
        "    label: УТ, поддельная 1С (журнал без раскрытых значений)\n"
        f"    url: http://127.0.0.1:{порт_1с}/odata/standard.odata/\n"
        "    user: u\n"
        "    password: p\n"
        "    role: prod\n",
        encoding="utf-8",
    )
    config = load_config(home)
    хранилище = IndexRepository(index_path(home, "ut"))
    хранилище.write(parse_edmx(EDMX_ФИКСТУРА.read_bytes()))
    хранилище.close()
    refresh_policy(home, config.bases["ut"])
    return home


async def _текст(клиент: ClientSession, тул: str, аргументы: dict) -> str:
    ответ = await клиент.call_tool(тул, аргументы)
    assert ответ.is_error is False, ответ
    return ответ.content[0].text


async def _искать_по_токену(порт_демона: int) -> str:
    """Модель получает ИНН токеном и ищет по нему — гейт раскрывает токен в настоящее значение
    внутри демона, и в 1С уходит адрес с настоящим ИНН. Возвращает токен."""
    async with (
        streamable_http_client(daemon_url(порт_демона)) as (чтение, запись),
        ClientSession(чтение, запись) as клиент,
    ):
        await клиент.initialize()
        выборка = await _текст(
            клиент,
            "odata1c_query",
            {"entity": "Catalog_Контрагенты", "select": ["Ref_Key", "ИНН"], "base": "ut"},
        )
        assert ИНН not in выборка
        найдено = ТОКЕН_ИНН.search(выборка)
        assert найдено, f"ИНН не пришёл токеном: {выборка}"
        токен = найдено.group(0)

        отбор = await _текст(
            клиент,
            "odata1c_query",
            {"entity": "Catalog_Контрагенты", "filter": f"ИНН eq '{токен}'", "base": "ut"},
        )
        assert ИНН not in отбор
        assert json.loads(отбор).get("error") is None, отбор
        return токен


async def test_журнал_живого_демона_не_хранит_раскрытого_значения(tmp_path):
    порт_демона = _свободный_порт()
    запросы_к_1с: list[str] = []
    номер_демона: str | None = None

    async with запущенная(журнал_запросов=запросы_к_1с) as порт_1с:
        home = _дом(tmp_path, порт_демона, порт_1с)
        журнал = home / "logs" / "daemon.log"
        try:
            spawn_detached(home, порт_демона)
            # Готовность — порт И pid-файл: демон пишет файл после того, как uvicorn занял порт
            # (`serve`), и между этими двумя событиями есть промежуток. На загруженной машине он
            # достаточен, чтобы порт уже слушался, а файла, по которому этот тест снимает демон в
            # `finally`, ещё не было.
            pid_файл = home / "daemon.pid"
            предел = time.monotonic() + ПРЕДЕЛ_ГОТОВНОСТИ_С
            while not (is_listening(порт_демона) and pid_файл.exists()):
                if time.monotonic() >= предел:
                    break
                await asyncio.sleep(0.2)
            assert is_listening(порт_демона), "демон не поднялся — проверять нечего"
            assert pid_файл.exists(), "демон занял порт, но pid-файл так и не появился"
            номер_демона = pid_файл.read_text(encoding="utf-8").strip()

            await asyncio.wait_for(_искать_по_токену(порт_демона), timeout=ПРЕДЕЛ_ВЫЗОВОВ_С)
        finally:
            # Уборка по номеру процесса, а не через `stop()`: код, который тест не проверяет,
            # не должен решать, остался ли после мутационного прогона живой демон.
            if номер_демона is not None:
                with contextlib.suppress(OSError, ValueError):
                    os.kill(int(номер_демона), signal.SIGTERM)
            предел = time.monotonic() + ПРЕДЕЛ_ОСТАНОВКИ_С
            while is_listening(порт_демона) and time.monotonic() < предел:
                await asyncio.sleep(0.2)

    # Тест не пуст: настоящий ИНН действительно ушёл в 1С адресом запроса — ровно та строка,
    # которую httpx на INFO положил бы в журнал.
    assert any(f"ИНН eq '{ИНН}'" in запрос for запрос in запросы_к_1с), запросы_к_1с
    # И журнал тот самый: демон в него писал (без этого «значения нет» ничего не доказывает).
    assert "демон слушает" in журнал.read_text(encoding="utf-8", errors="replace")
    файлы = [файл for файл in sorted((home / "logs").iterdir()) if файл.is_file()]
    assert журнал in файлы
    строки_с_значением = [
        f"{файл.name}: {строка}"
        for файл in файлы
        for строка in файл.read_text(encoding="utf-8", errors="replace").splitlines()
        if ИНН in строка
    ]
    assert not строки_с_значением, "раскрытое гейтом значение попало в журнал демона:\n" + (
        "\n".join(строки_с_значением)
    )
