"""Лаунчер `odata1c mcp` (SPEC §2.2, план M1d задача 6): тонкий stdio-процесс без собственной
логики. Снаружи (к клиенту — Claude Code) — stdio, внутрь (к демону) — Streamable HTTP; лаунчер
сам поднимает демон, если его порт не слушается, и пересылает семь обработчиков lowlevel `Server`
вызовами апстрим-сессии как есть, не трогая содержимое.

Область видимости сессии — не аргумент MCP-протокола, а заголовки HTTP на каждый запрос к демону
(`SCOPE_BASES_HEADER`/`SCOPE_DEFAULT_HEADER`, `daemon.py`, поправка SPEC §2.1 задачи 5): `--bases`
и `--default` этой команды превращаются в них один раз при построении HTTP-клиента лаунчера,
а не при каждом вызове тула.

Elicitation (запрос демона к пользователю — протокол записи, SPEC §7) идёт в обратную сторону:
демон спрашивает апстрим-сессию лаунчера, а переслать его нужно вниз, downstream-клиенту
(настоящему пользователю). Единственная downstream-сессия лаунчера сохраняется в `ProxyHolder`
каждым из семи обработчиков при входе (`ctx.session`) — какой из них отработает первым, не важно:
за один stdio-процесс лаунчера downstream-сессия всегда одна и та же.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

import httpx2
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from odata1c.config.home import ensure_home
from odata1c.config.loader import load_config
from odata1c.daemon import (
    SCOPE_BASES_HEADER,
    SCOPE_DEFAULT_HEADER,
    daemon_url,
    is_listening,
    spawn_detached,
)

# Тот же срок и тот же смысл, что у ОЖИДАНИЕ_ГОТОВНОСТИ_S в cli.py (cmd_daemon): обе стороны ждут
# один и тот же холодный старт демона (импорт lxml/ahocorasick, разбор daemon.yaml). Не импортирован
# оттуда — импорт в обратную сторону (cli.py уже импортирует run_launcher отсюда для команды mcp)
# сделал бы модули взаимозависимыми.
ОЖИДАНИЕ_ГОТОВНОСТИ_S = 15


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
    чем клиент лаунчера сделал хоть один запрос, — вежливый отказ, а не голое исключение."""

    async def on_elicit(
        context: object, params: types.ElicitRequestParams
    ) -> types.ElicitResult | types.ErrorData:
        if holder.session is None:
            return types.ElicitResult(action="decline")
        return await holder.session.elicit_form(params.message, params.requested_schema)

    return on_elicit


def build_proxy(upstream: ClientSession, holder: ProxyHolder) -> Server:
    """Прокси лаунчера: семь обработчиков lowlevel `Server`, каждый вызывает соответствующий
    метод апстрим-сессии (демона) и возвращает его результат как есть — своей логики здесь нет
    (SPEC §2.2). Уведомления `tools/list_changed` не пересылаются: набор тулов демона не
    меняется на лету, пересылка недостающего уведомления вреда клиенту не приносит."""

    async def on_list_tools(ctx, params):
        holder.session = ctx.session
        return await upstream.list_tools(params=params)

    async def on_call_tool(ctx, params: types.CallToolRequestParams):
        holder.session = ctx.session
        return await upstream.call_tool(params.name, params.arguments or {})

    async def on_list_resources(ctx, params):
        holder.session = ctx.session
        return await upstream.list_resources(params=params)

    async def on_list_resource_templates(ctx, params):
        holder.session = ctx.session
        return await upstream.list_resource_templates(params=params)

    async def on_read_resource(ctx, params: types.ReadResourceRequestParams):
        holder.session = ctx.session
        return await upstream.read_resource(params.uri)

    async def on_list_prompts(ctx, params):
        holder.session = ctx.session
        return await upstream.list_prompts(params=params)

    async def on_get_prompt(ctx, params: types.GetPromptRequestParams):
        holder.session = ctx.session
        return await upstream.get_prompt(params.name, params.arguments)

    return Server(
        "odata1c-mcp-launcher",
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
    была бы сужением или расширением видимости, которого никто не просил."""
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
    шаге) и выход с кодом 1: `SystemExit` — обычное `BaseException`, `cli.main` его не перехватывает
    (перехватывает только `Exception`-наследников), поэтому код возврата доходит до процесса как
    есть, тем же способом, что и обычный `sys.exit(main())` в `__main__.py`.
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
    """
    ensure_home(home)

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
        await upstream.initialize()
        proxy = build_proxy(upstream, holder)
        async with stdio_server() as (read, write):
            await proxy.run(read, write, proxy.create_initialization_options())
