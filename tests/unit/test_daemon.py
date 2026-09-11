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

# Тулы с аннотацией readOnly: данных в 1С не меняют и индекса не перестраивают.
ОЖИДАЕМЫЕ_ТУЛЫ = {
    "odata1c_bases",
    "odata1c_find_entity",
    "odata1c_describe_entity",
    "odata1c_query",
    "odata1c_get",
    "odata1c_info",
    "odata1c_raw_get",
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


# -------------------------------------------------------------------------------------------
# Задача 7: reindex, info, raw_get, ресурсы, промпт, фоновая проверка $metadata
# -------------------------------------------------------------------------------------------


async def test_reindex_объявлен_идемпотентным_а_не_readonly(сервер):
    """`odata1c_reindex` перестраивает индекс базы и раздел `auto` её политики — readOnly он не
    может быть по определению; повторный вызов при этом безопасен, отсюда idempotent."""
    async with InMemoryTransport(сервер) as (r, w), ClientSession(r, w) as сессия:
        await сессия.initialize()
        тулы = {т.name: т for т in (await сессия.list_tools()).tools}

    реиндекс = тулы["odata1c_reindex"]
    assert реиндекс.annotations.idempotent_hint is True
    assert реиндекс.annotations.read_only_hint is not True
    assert реиндекс.output_schema is None


async def test_info_доступен_туло́м_без_базы(сервер):
    """`info` базы не требует вовсе (SPEC §5) — вызывается в доме без единой описанной базы."""
    async with InMemoryTransport(сервер) as (r, w), ClientSession(r, w) as сессия:
        await сессия.initialize()
        результат = await сессия.call_tool("odata1c_info", {"topic": "tokens"})

    assert результат.is_error is False
    assert "[[" in результат.content[0].text


async def test_ресурсы_и_шаблоны_объявлены(сервер):
    async with InMemoryTransport(сервер) as (r, w), ClientSession(r, w) as сессия:
        await сессия.initialize()
        ресурсы = {str(р.uri) for р in (await сессия.list_resources()).resources}
        шаблоны = {
            ш.uri_template for ш in (await сессия.list_resource_templates()).resource_templates
        }
        справочник = await сессия.read_resource("odata1c://cheatsheet")

    assert "odata1c://cheatsheet" in ресурсы
    assert {"odata1c://policy/{base}", "odata1c://index/{base}"} <= шаблоны
    assert "[[" in справочник.contents[0].text


async def test_промпт_explore_объявлен(сервер):
    async with InMemoryTransport(сервер) as (r, w), ClientSession(r, w) as сессия:
        await сессия.initialize()
        промпты = {п.name: п for п in (await сессия.list_prompts()).prompts}
        результат = await сессия.get_prompt("explore", {"base": "ut"})

    assert "explore" in промпты
    текст = результат.messages[0].content.text
    assert "ut" in текст and "odata1c_bases" in текст and "[[" in текст


async def test_ресурс_политики_отказывает_на_неизвестной_базе(сервер):
    async with InMemoryTransport(сервер) as (r, w), ClientSession(r, w) as сессия:
        await сессия.initialize()
        содержимое = await сессия.read_resource("odata1c://policy/нет_такой")

    assert json.loads(содержимое.contents[0].text)["error"]["code"] == "base_unknown"


async def test_фоновая_проверка_пропускает_базы_без_индекса(сервис, дом, monkeypatch):
    """Первый реиндекс — сознательное действие владельца, а не побочный эффект старта демона:
    на базе уровня ERP он стоит десятков мегабайт трафика и минут разбора."""
    from odata1c.config.loader import load_config
    from odata1c.daemon import check_metadata_once

    вызовы = []

    async def перехват(self, scope, *, base=None, force=False):
        вызовы.append(base)
        return "{}"

    monkeypatch.setattr(ToolService, "reindex", перехват)
    await check_metadata_once(сервис, load_config(дом))

    assert вызовы == []


async def test_фоновая_проверка_записывает_отказ_в_реестр(tmp_path, monkeypatch, caplog):
    """Отказ фоновой проверки не виден никому, кроме журнала и реестра: MCP-клиента у неё нет.

    Ответ `reindex` уже прошёл гейт и страж — его можно и записать, и залогировать целиком.
    """
    from odata1c.cli import main
    from odata1c.config.loader import load_config
    from odata1c.daemon import check_metadata_once
    from odata1c.index.reindex import index_path

    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(
        "default: ut\nbases:\n  ut:\n    label: УТ 11, тестовая\n"
        "    url: http://localhost/ut/odata/standard.odata/\n"
        "    user: u\n    password: p\n    role: prod\n",
        encoding="utf-8",
    )
    index_path(home, "ut").parent.mkdir(parents=True, exist_ok=True)
    index_path(home, "ut").write_bytes(b"")  # индекс есть — базу проверять положено

    async def отказ(self, scope, *, base=None, force=False):
        return '{"error": {"code": "odata_error", "message": "1С недоступна", "hint": ""}}'

    monkeypatch.setattr(ToolService, "reindex", отказ)
    config = load_config(home)
    служба = ToolService(config)
    try:
        with caplog.at_level("WARNING"):
            await check_metadata_once(служба, config)
        assert служба._registry.visible(SessionScope())[0].last_error == (
            "[odata_error] 1С недоступна"
        )
    finally:
        await служба.aclose()
    assert "1С недоступна" in caplog.text
