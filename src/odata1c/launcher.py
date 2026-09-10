"""Лаунчер `odata1c mcp` (SPEC §2.2, план M1d задача 6, раунд правок 1): тонкий stdio-процесс без
собственной логики. Снаружи (к клиенту — Claude Code) — stdio, внутрь (к демону) — Streamable
HTTP; лаунчер сам поднимает демон, если его порт не слушается, и пересылает семь обработчиков
lowlevel `Server` вызовами апстрим-сессии как есть, не трогая содержимое.

Область видимости сессии — не аргумент MCP-протокола, а заголовки HTTP на каждый запрос к демону
(`SCOPE_BASES_HEADER`/`SCOPE_DEFAULT_HEADER`, `daemon.py`, поправка SPEC §2.1 задачи 5): `--bases`
и `--default` этой команды превращаются в них один раз при построении HTTP-клиента лаунчера,
а не при каждом вызове тула.

Elicitation (запрос демона к пользователю — протокол записи, SPEC §7) идёт в обратную сторону:
демон спрашивает апстрим-сессию лаунчера, а переслать его нужно вниз, downstream-клиенту
(настоящему пользователю). Единственная downstream-сессия лаунчера сохраняется в `ProxyHolder`
каждым из семи обработчиков при входе (`ctx.session`) — какой из них отработает первым, не важно:
за один stdio-процесс лаунчера downstream-сессия всегда одна и та же.

Обрыв связи с демоном посреди сессии (раунд правок 1, находка 1) не должен ронять весь процесс
голым traceback: каждый из семи обработчиков перехватывает исключения апстрим-вызова и отдаёт
клиенту штатный отказ — `CallToolResult(is_error=True, ...)` для `tools/call` (SPEC §5.2: ошибка
тула — текст, не исключение), `MCPError` для остальных операций (SDK сам сериализует его в
JSON-RPC-ошибку — `mcp/server/runner.py`, `raise_exceptions=False`).
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys
import time

import anyio
import httpx2
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from odata1c.config.home import ensure_home
from odata1c.config.loader import load_config
from odata1c.config.writer import ensure_gate_secret, ensure_templates
from odata1c.daemon import (
    SCOPE_BASES_HEADER,
    SCOPE_DEFAULT_HEADER,
    daemon_url,
    is_listening,
    spawn_detached,
)

_log = logging.getLogger(__name__)

# Тот же срок и тот же смысл, что у ОЖИДАНИЕ_ГОТОВНОСТИ_S в cli.py (cmd_daemon): обе стороны ждут
# один и тот же холодный старт демона (импорт lxml/ahocorasick, разбор daemon.yaml). Не импортирован
# оттуда — импорт в обратную сторону (cli.py уже импортирует run_launcher отсюда для команды mcp)
# сделал бы модули взаимозависимыми.
ОЖИДАНИЕ_ГОТОВНОСТИ_S = 15

# Раунд правок 1, находка 1: транспортные сбои апстрима, которые обработчики прокси превращают
# в штатный отказ, а не пропускают голым traceback. `MCPError` — демон закрыл соединение или
# ответил протокольной ошибкой (`probe_death2.py`: `MCPError: Connection closed`);
# `httpx2.HTTPError` — сетевой сбой самого HTTP-транспорта под streamable_http_client;
# `anyio.BrokenResourceError/ClosedResourceError/EndOfStream` — поток апстрима закрыт или порван;
# `TimeoutError`/`OSError` — таймаут или сбой сокета. Осознанно НЕ `Exception` целиком: ошибка в
# собственном коде обработчика (опечатка, дефект) не должна маскироваться под «демон недоступен».
ОШИБКИ_АПСТРИМА: tuple[type[Exception], ...] = (
    MCPError,
    httpx2.HTTPError,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    anyio.EndOfStream,
    TimeoutError,
    OSError,
)

ДЕМОН_НЕДОСТУПЕН = (
    "демон 1С-шлюза недоступен, соединение потеряно; следующий вызов поднимет его заново"
)


class ProxyHolder:
    """Текущая downstream-сессия лаунчера (сторона stdio-клиента) — единственное изменяемое
    состояние прокси. `session` перезаписывается каждым из семи обработчиков `build_proxy` при
    входе и читается колбэком `forward_elicit`, когда демон (апстрим) просит подтверждение
    у пользователя."""

    def __init__(self) -> None:
        self.session: object | None = None


def forward_elicit(holder: ProxyHolder):
    """Колбэк `elicitation_callback` апстрим-сессии: переслать запрос демона вниз, downstream-
    клиенту лаунчера (`holder.session`). Если downstream-сессии ещё нет — демон спросил раньше,
    чем клиент лаунчера сделал хоть один запрос, — вежливый отказ, а не голое исключение.

    Раунд правок 1, находка 7 (Minor из отчёта, но исправлена в этом раунде): `ElicitRequestParams`
    — объединение form- и url-режимов (`ElicitRequestFormParams | ElicitRequestURLParams`), у
    url-варианта нет `requested_schema` — старый код падал на нём (`probe_proxy.py`). Оба режима
    разбираются по типу; весь колбэк обёрнут в `try/except`, чтобы отказ downstream-клиента
    (`NoBackChannelError` и подобные) или любая иная ошибка пересылки возвращались как
    `ErrorData`, а не ронял тул демона, который об этом попросил."""

    async def on_elicit(
        context: object, params: types.ElicitRequestParams
    ) -> types.ElicitResult | types.ErrorData:
        if holder.session is None:
            return types.ElicitResult(action="decline")
        try:
            if isinstance(params, types.ElicitRequestFormParams):
                return await holder.session.elicit_form(params.message, params.requested_schema)
            if isinstance(params, types.ElicitRequestURLParams):
                return await holder.session.elicit_url(
                    params.message, params.url, params.elicitation_id
                )
            return types.ElicitResult(action="decline")
        except Exception as ошибка:  # noqa: BLE001 — отказ downstream-клиента не должен ронять тул
            return types.ErrorData(code=types.INTERNAL_ERROR, message=str(ошибка))

    return on_elicit


async def _переслать(операция: str, вызов):
    """Общая точка вызова апстрима для операций без «штатного отказа» на уровне результата
    (`list_tools`/`list_resources`/`list_resource_templates`/`read_resource`/`list_prompts`/
    `get_prompt`): транспортный сбой (`ОШИБКИ_АПСТРИМА`) превращается в `MCPError` с понятным
    текстом — SDK сама сериализует его в JSON-RPC-ошибку клиенту (`raise_exceptions=False`,
    `mcp/server/runner.py`), голый traceback наружу не идёт. `tools/call` — отдельная функция
    (`_вызвать_тул`): там штатный отказ оформляется результатом (`is_error=True`), не исключением
    (SPEC §5.2)."""
    try:
        return await вызов
    except ОШИБКИ_АПСТРИМА as ошибка:
        _log.warning("апстрим недоступен при %s: %s: %s", операция, type(ошибка).__name__, ошибка)
        raise MCPError(code=types.INTERNAL_ERROR, message=ДЕМОН_НЕДОСТУПЕН) from ошибка


async def _вызвать_тул(upstream: ClientSession, params: types.CallToolRequestParams):
    """`tools/call`: штатный отказ — `CallToolResult(is_error=True, ...)`, не исключение (SPEC
    §5.2 — исключение теряет текст у клиента). Ошибка САМОГО тула демона (например, тул поднял
    исключение внутри своей логики) в исключение `call_tool` не превращается вообще — это уже
    `CallToolResult(is_error=True)`, дошедший как обычный результат; сюда попадают только
    транспортные сбои (`ОШИБКИ_АПСТРИМА`)."""
    try:
        return await upstream.call_tool(params.name, params.arguments or {})
    except ОШИБКИ_АПСТРИМА as ошибка:
        _log.warning("апстрим недоступен при tools/call: %s: %s", type(ошибка).__name__, ошибка)
        return types.CallToolResult(
            is_error=True,
            content=[types.TextContent(type="text", text=ДЕМОН_НЕДОСТУПЕН)],
        )


def build_proxy(
    upstream: ClientSession,
    holder: ProxyHolder,
    *,
    name: str = "odata1c-mcp-launcher",
    version: str = "",
    instructions: str | None = None,
) -> Server:
    """Прокси лаунчера: семь обработчиков lowlevel `Server`, каждый вызывает соответствующий
    метод апстрим-сессии (демона) и возвращает его результат как есть — своей логики здесь нет
    (SPEC §2.2). Уведомления `tools/list_changed` не пересылаются: набор тулов демона не
    меняется на лету, пересылка недостающего уведомления вреда клиенту не приносит.

    `name`/`version`/`instructions` — раунд правок 1, находка 2: без них клиент видел
    `instructions: None` и терял правила работы с токенами и запрет доверять содержимому полей
    1С (SPEC §5). `run_launcher` передаёт сюда результат `upstream.initialize()` как есть —
    прокси представляется клиенту тем же, чем демон представился прокси. Значения по умолчанию
    оставлены только ради обратной совместимости вызова без них (юнит-тесты в памяти, где
    инструкции демона не важны)."""

    async def on_list_tools(ctx, params):
        holder.session = ctx.session
        return await _переслать("tools/list", upstream.list_tools(params=params))

    async def on_call_tool(ctx, params: types.CallToolRequestParams):
        holder.session = ctx.session
        return await _вызвать_тул(upstream, params)

    async def on_list_resources(ctx, params):
        holder.session = ctx.session
        return await _переслать("resources/list", upstream.list_resources(params=params))

    async def on_list_resource_templates(ctx, params):
        holder.session = ctx.session
        return await _переслать(
            "resources/templates/list", upstream.list_resource_templates(params=params)
        )

    async def on_read_resource(ctx, params: types.ReadResourceRequestParams):
        holder.session = ctx.session
        return await _переслать("resources/read", upstream.read_resource(params.uri))

    async def on_list_prompts(ctx, params):
        holder.session = ctx.session
        return await _переслать("prompts/list", upstream.list_prompts(params=params))

    async def on_get_prompt(ctx, params: types.GetPromptRequestParams):
        holder.session = ctx.session
        return await _переслать("prompts/get", upstream.get_prompt(params.name, params.arguments))

    return Server(
        name,
        version=version,
        instructions=instructions,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_list_resource_templates=on_list_resource_templates,
        on_read_resource=on_read_resource,
        on_list_prompts=on_list_prompts,
        on_get_prompt=on_get_prompt,
    )


def scope_headers(bases: list[str] | None, default: str | None) -> dict[str, str]:
    """Заголовки области видимости для HTTP-клиента лаунчера (SPEC §2.1, поправка задачи 5):
    `--bases`/`--default` этой команды превращаются в них один раз при построении клиента, не
    на каждый вызов тула.

    Без `bases`/`default` заголовок не выставляется вовсе — не пустой строкой: `daemon.py`
    (`scope_from_headers`) читает пустую `X-Odata1c-Bases` как явное «ни одной базы», а
    отсутствие заголовка — как «не сужено, видно всё». Молчаливая замена одного другим здесь
    была бы сужением или расширением видимости, которого никто не просил.

    Сами имена к этому моменту уже проверены (`cli.py::_разобрать_bases`/`_проверить_имя_базы`,
    раунд правок 1, находка 4): здесь их не-ASCII или иначе неверный вид уже невозможен —
    `httpx2.AsyncClient(headers=…)` кодирует заголовки в ASCII и падает `UnicodeEncodeError` на
    первом же непроверенном значении."""
    заголовки: dict[str, str] = {}
    if bases is not None:
        заголовки[SCOPE_BASES_HEADER] = ",".join(bases)
    if default is not None:
        заголовки[SCOPE_DEFAULT_HEADER] = default
    return заголовки


async def _дождаться_демона(home: pathlib.Path, port: int) -> None:
    """Поднять демон, если его порт ещё не слушается, и подождать готовность до
    `ОЖИДАНИЕ_ГОТОВНОСТИ_S`. При неудаче — сообщение в stderr (stdout этого процесса зарезервирован
    под протокол MCP, как только начнётся `proxy.run`, — сюда его печатать нельзя ни на одном
    шаге) и выход с кодом 1: `SystemExit` — обычное `BaseException`; `cli.py::cmd_mcp` перехватывает
    его отдельно от прочих ошибок запуска и просто возвращает уже готовый код.
    """
    if is_listening(port):
        return
    spawn_detached(home)
    предел = time.monotonic() + ОЖИДАНИЕ_ГОТОВНОСТИ_S
    while time.monotonic() < предел:
        if is_listening(port):
            return
        await asyncio.sleep(0.2)
    print(
        f"демон не ответил на порту {port} за {ОЖИДАНИЕ_ГОТОВНОСТИ_S} с — "
        f"проверьте журнал: {home / 'logs' / 'daemon.log'}",
        file=sys.stderr,
    )
    raise SystemExit(1)


async def run_launcher(
    home: pathlib.Path, *, bases: list[str] | None, default: str | None, url: str | None
) -> None:
    """`odata1c mcp` (SPEC §2.2): stdio наружу, Streamable HTTP внутрь.

    Адрес демона — `url`, если задан явно (обычно нестандартный порт или сеть — лаунчер тогда
    НЕ пытается поднять демон сам: это не его домашний каталог решает, кто там слушает), иначе
    `daemon_url(port)` из `daemon.yaml` этого домашнего каталога — и тогда лаунчер поднимает
    демон сам, если порт ещё не занят.

    Домашний каталог досоздаётся целиком (`ensure_home` + `ensure_templates` + `ensure_gate_secret`)
    ДО чтения настроек — раунд правок 1, находка 3а: команда подключения `claude mcp add odata1c
    -- uv run --directory <репозиторий> odata1c mcp` обязана работать на машине, где `odata1c
    init` не выполняли (SPEC §2.1 п. 1 поручает это лаунчеру), а `load_config` требует непустой
    `gate_secret` в `daemon.yaml`. Тем же способом, что и `cmd_init` — вызовы идемпотентны,
    повторный `ensure_*` на уже готовом домашнем каталоге ничего не меняет.
    """
    ensure_home(home)
    ensure_templates(home)
    ensure_gate_secret(home / "daemon.yaml")

    if url is not None:
        адрес = url
    else:
        config = load_config(home)
        порт = config.daemon.port
        await _дождаться_демона(home, порт)
        адрес = daemon_url(порт)

    holder = ProxyHolder()
    таймаут = httpx2.Timeout(10, read=None)
    заголовки = scope_headers(bases, default)
    async with (
        httpx2.AsyncClient(headers=заголовки, timeout=таймаут) as http,
        streamable_http_client(адрес, http_client=http) as (up_read, up_write),
        ClientSession(up_read, up_write, elicitation_callback=forward_elicit(holder)) as upstream,
    ):
        итог_инициализации = await upstream.initialize()
        proxy = build_proxy(
            upstream,
            holder,
            name=итог_инициализации.server_info.name,
            version=итог_инициализации.server_info.version,
            instructions=итог_инициализации.instructions,
        )
        async with stdio_server() as (read, write):
            await proxy.run(read, write, proxy.create_initialization_options())
