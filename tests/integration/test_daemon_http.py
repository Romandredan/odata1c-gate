"""Сквозная проверка демона по-настоящему: `serve()` на свободном TCP-порту, реальный Streamable
HTTP (не `InMemoryTransport`), два HTTP-клиента с разными заголовками области видимости видят
разные наборы баз (план M1d, задача 5).

Живая 1С не нужна: `odata1c_bases` не обращается к 1С вообще (`ToolService.bases` читает только
`bases.yaml` и статус локального индекса) — этого достаточно, чтобы проверить именно то, что
проверяет этот тест: видимость по заголовкам, реальный сетевой транспорт, жизненный цикл
`serve()`/pid-файла. Адрес базы в `bases.yaml` намеренно недостижим (`http://127.0.0.1:1/...` —
порт 1 зарезервирован ОС, соединение отклоняется мгновенно) — тест не должен и не пытается послать
запрос, который туда попадёт.
"""

import asyncio
import contextlib
import json
import socket

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from odata1c.config.home import ensure_home
from odata1c.config.writer import ensure_gate_secret
from odata1c.daemon import SCOPE_BASES_HEADER, SCOPE_DEFAULT_HEADER, daemon_url, serve

BASES_YAML = """
default: ut
bases:
  ut:
    label: УТ, недостижимая (тест видимости)
    url: http://127.0.0.1:1/odata/standard.odata/
    user: u
    password: p
    role: dev
  dev:
    label: Песочница, недостижимая (тест видимости)
    url: http://127.0.0.1:1/odata/standard.odata/
    user: u
    password: p
    role: dev
"""


def _свободный_порт() -> int:
    # bind(("127.0.0.1", 0)) — ОС сама выбирает свободный порт; сокет закрывается сразу же, порт
    # передаётся serve() как обычное число. Короткая гонка (порт мог быть занят между close() и
    # bind() внутри uvicorn) в тестовом окружении на локальной машине не встречалась ни разу за
    # прогон этого файла — на неё не рассчитываем как на невозможную, но и не защищаемся отдельно.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as соединение:
        соединение.bind(("127.0.0.1", 0))
        return соединение.getsockname()[1]


@pytest.fixture
def дом(tmp_path):
    home = tmp_path / "home"
    ensure_home(home)
    ensure_gate_secret(home / "daemon.yaml")
    (home / "bases.yaml").write_text(BASES_YAML, encoding="utf-8")
    return home


@pytest.fixture
async def запущенный_демон(дом):
    """`serve()` в фоновой задаче на свободном порту; остановка — отмена задачи (у `serve()` нет
    отдельного параметра «стоп»: она держит корутину, пока её не отменят — тот же способ, каким
    `--foreground` останавливают по Ctrl+C). Отдаёт (home, port)."""
    порт = _свободный_порт()
    готовность = asyncio.Event()
    задача = asyncio.create_task(serve(дом, port=порт, ready=готовность))
    await asyncio.wait_for(готовность.wait(), timeout=10)
    try:
        yield дом, порт
    finally:
        задача.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await задача


async def _bases_ответ(url: str, заголовки: dict[str, str]) -> dict:
    таймаут = httpx2.Timeout(10, read=None)
    async with (
        httpx2.AsyncClient(headers=заголовки, timeout=таймаут) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as сессия,
    ):
        await сессия.initialize()
        результат = await сессия.call_tool("odata1c_bases", {})
    return json.loads(результат.content[0].text)


async def _видимые_базы(url: str, заголовки: dict[str, str]) -> list[str]:
    данные = await _bases_ответ(url, заголовки)
    return [б["name"] for б in данные["bases"]]


async def test_serve_pid_файл_создан_и_удалён(запущенный_демон):
    дом, _ = запущенный_демон
    assert (дом / "daemon.pid").exists()
    # Само значение — pid ЭТОГО тестового процесса: серве() работает в текущем event loop задачей,
    # не отдельным процессом (в отличие от spawn_detached, который проверяется юнит-тестами cli).
    import os

    assert (дом / "daemon.pid").read_text(encoding="utf-8").strip() == str(os.getpid())


async def test_serve_pid_файл_удалён_после_остановки(дом):
    порт = _свободный_порт()
    готовность = asyncio.Event()
    задача = asyncio.create_task(serve(дом, port=порт, ready=готовность))
    await asyncio.wait_for(готовность.wait(), timeout=10)
    assert (дом / "daemon.pid").exists()

    задача.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await задача

    assert not (дом / "daemon.pid").exists()


async def test_два_клиента_с_разными_заголовками_видят_разные_базы(запущенный_демон):
    _, порт = запущенный_демон
    url = daemon_url(порт)

    базы_ut = await _видимые_базы(url, {SCOPE_BASES_HEADER: "ut"})
    базы_dev = await _видимые_базы(url, {SCOPE_BASES_HEADER: "dev"})

    assert базы_ut == ["ut"]
    assert базы_dev == ["dev"]


async def test_без_заголовков_видны_все_базы(запущенный_демон):
    _, порт = запущенный_демон
    базы = await _видимые_базы(daemon_url(порт), {})
    assert базы == ["dev", "ut"]


async def test_заголовок_default_переопределяет_базу_по_умолчанию(запущенный_демон):
    # bases.yaml задаёт default: ut — заголовок сессии должен его переопределять для ответа этой
    # сессии, не трогая default для клиента без заголовка (тот же демон, тот же bases.yaml).
    _, порт = запущенный_демон
    url = daemon_url(порт)

    без_заголовка = await _bases_ответ(url, {})
    assert без_заголовка["default"] == "ut"

    с_заголовком = await _bases_ответ(url, {SCOPE_DEFAULT_HEADER: "dev"})
    assert с_заголовком["default"] == "dev"
