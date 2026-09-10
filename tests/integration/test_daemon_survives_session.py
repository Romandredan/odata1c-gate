"""Демон переживает закрытие сессии клиента (план M1d, задача 6, раунд правок 1, находка 9,
оркестратор — живая база `trade_dev`).

На Windows лаунчер, запущенный настоящим `mcp.client.stdio.stdio_client`, находится под Job
Object с `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`: обычный `CreateProcess` демона, которого лаунчер
порождает, наследует членство в этом job и гибнет вместе с сессией клиента, даже с
`CREATE_BREAKAWAY_FROM_JOB` (job не разрешает отрыв — флаг молча игнорируется ОС). Демон должен
пережить закрытие сессии: `daemon.spawn_detached` на Windows поднимает его через Планировщик
заданий (`daemon.py::_spawn_via_scheduled_task`) — процесс, запущенный службой Планировщика, вне
дерева процессов лаунчера и его job вообще.

Проверка не делает полный MCP-handshake (`ClientSession.initialize`) — она про то, что демон
переживает закрытие именно stdio-транспорта лаунчера (на Windows это и есть момент закрытия job),
не про протокол.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import time

import pytest
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from odata1c.config.home import ensure_home
from odata1c.config.writer import ensure_gate_secret
from odata1c.daemon import is_listening
from odata1c.daemon import stop as daemon_stop

ПРЕДЕЛ_ГОТОВНОСТИ_С = 20
ПРЕДЕЛ_ОСТАНОВКИ_С = 10


def _свободный_порт() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as соединение:
        соединение.bind(("127.0.0.1", 0))
        return соединение.getsockname()[1]


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object stdio_client — особенность Windows")
async def test_демон_переживает_закрытие_stdio_сессии_лаунчера(tmp_path):
    home = tmp_path / "home"
    ensure_home(home)
    ensure_gate_secret(home / "daemon.yaml")
    порт = _свободный_порт()
    with (home / "daemon.yaml").open("a", encoding="utf-8") as поток:
        поток.write(f"port: {порт}\n")
    (home / "bases.yaml").write_text("bases: {}\n", encoding="utf-8")

    параметры = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odata1c", "--home", str(home), "mcp"],
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    try:
        async with stdio_client(параметры) as (read, write):
            del read, write  # достаточно самого факта, что лаунчер запущен под Job Object
            предел = time.monotonic() + ПРЕДЕЛ_ГОТОВНОСТИ_С
            while not is_listening(порт) and time.monotonic() < предел:
                await asyncio.sleep(0.2)
            assert is_listening(порт), "демон не поднялся за отведённое время"

        # Транспорт stdio_client закрыт — на Windows это и есть момент закрытия Job Object,
        # в который SDK поместил лаунчер (mcp/os/win32/utilities.py::_create_job_object).
        await asyncio.sleep(1.0)
        assert is_listening(порт), (
            "демон погиб вместе с сессией лаунчера — Job Object stdio_client убил его "
            "(находка 9: демон обязан переживать закрытие сессии клиента)"
        )
    finally:
        if is_listening(порт):
            daemon_stop(home)
            предел = time.monotonic() + ПРЕДЕЛ_ОСТАНОВКИ_С
            while is_listening(порт) and time.monotonic() < предел:
                await asyncio.sleep(0.2)
