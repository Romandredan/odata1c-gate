"""Демон MCP: обёртка `ToolService` в `MCPServer` — тулы чтения, область видимости по заголовкам
HTTP, server instructions (план M1d, задача 5). Юнит-часть — в памяти (`InMemoryTransport`), без
сети; сквозная проверка настоящего Streamable HTTP — `tests/integration/test_daemon_http.py`.
"""

import json

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from odata1c.config.home import ensure_home
from odata1c.config.loader import load_config
from odata1c.config.writer import ensure_gate_secret
from odata1c.daemon import (
    INSTRUCTIONS,
    SCOPE_BASES_HEADER,
    SCOPE_DEFAULT_HEADER,
    build_server,
    scope_from_headers,
)
from odata1c.registry.registry import SessionScope
from odata1c.tools.service import ToolService

ОЖИДАЕМЫЕ_ТУЛЫ = {
    "odata1c_bases",
    "odata1c_find_entity",
    "odata1c_describe_entity",
    "odata1c_query",
    "odata1c_get",
}


@pytest.fixture
def дом(tmp_path):
    """Домашний каталог без описанных баз: юнит-тесты этого файла проверяют только форму тулов
    и видимость по заголовкам — обращения к 1С в них нет (`bases()` не ходит в сеть)."""
    home = tmp_path / "home"
    ensure_home(home)
    ensure_gate_secret(home / "daemon.yaml")
    return home


@pytest.fixture
async def сервис(дом):
    служба = ToolService(load_config(дом))
    yield служба
    await служба.aclose()


@pytest.fixture
def сервер(сервис):
    config = load_config(сервис._config.home)
    return build_server(сервис, config.daemon.limits)


# -------------------------------------------------------------------------------------------
# Объявление тулов: аннотации, meta, structured_output=False (SDK не дублирует текст в
# structuredContent)
# -------------------------------------------------------------------------------------------


async def test_тулы_объявлены_с_аннотациями_и_meta(сервер):
    async with InMemoryTransport(сервер) as (r, w), ClientSession(r, w) as сессия:
        await сессия.initialize()
        тулы = {т.name: т for т in (await сессия.list_tools()).tools}
    assert set(тулы) >= ОЖИДАЕМЫЕ_ТУЛЫ
    for имя in ОЖИДАЕМЫЕ_ТУЛЫ:
        т = тулы[имя]
        assert т.annotations.read_only_hint is True
        assert т.meta["anthropic/maxResultSizeChars"] == 120000
        assert т.output_schema is None


async def test_результат_только_текстом(сервер):
    async with InMemoryTransport(сервер) as (r, w), ClientSession(r, w) as сессия:
        await сессия.initialize()
        результат = await сессия.call_tool("odata1c_bases", {})
    assert результат.is_error is False
    assert результат.structured_content is None
    assert len(результат.content) == 1
    assert "bases" in json.loads(результат.content[0].text)


# -------------------------------------------------------------------------------------------
# Область видимости: заголовки X-Odata1c-Bases / X-Odata1c-Default (SPEC §2.1, поправка задачи 5)
# -------------------------------------------------------------------------------------------


def test_scope_from_headers():
    assert scope_from_headers(None) == SessionScope()
    assert scope_from_headers(
        {SCOPE_BASES_HEADER: "ut, buh", SCOPE_DEFAULT_HEADER: "ut"}
    ) == SessionScope(bases=("ut", "buh"), default="ut")


def test_scope_from_headers_без_нужных_заголовков():
    assert scope_from_headers({"content-type": "application/json"}) == SessionScope()


def test_scope_from_headers_пустая_строка_баз():
    # Заголовок прислан, но пуст — не «база не задана» (bases=None, видно всё), а «явно ни одной»:
    # видимость сужена до пустого множества, а не расширена молчаливым откатом на «всё видно».
    assert scope_from_headers({SCOPE_BASES_HEADER: ""}) == SessionScope(bases=())


# -------------------------------------------------------------------------------------------
# Server instructions
# -------------------------------------------------------------------------------------------


def test_instructions_не_длиннее_2_кб():
    assert len(INSTRUCTIONS) <= 2048
