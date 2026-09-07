"""Проба P2 (SPEC §15.1): доходит ли _meta тула до клиента.

В SDK mcp 2.x класс FastMCP переименован в MCPServer (mcp.server.mcpserver).
Печатает три вещи: что SDK принимает при объявлении тула, какие поля есть у типа Tool,
что клиент видит в ответе tools/list.
"""

import asyncio
import inspect
import json

import mcp.types as types
from mcp.server.mcpserver import MCPServer

META = {"anthropic/requiresUserInteraction": True, "anthropic/maxResultSizeChars": 120000}


def show_api() -> None:
    print("--- сигнатура MCPServer.tool ---")
    print(inspect.signature(MCPServer.tool))
    print("--- поля types.Tool ---")
    print(sorted(types.Tool.model_fields))
    print("--- псевдоним поля meta ---")
    print(types.Tool.model_fields["meta"].alias)


def build_server() -> tuple[MCPServer, str]:
    """Собрать сервер с одним тулом; вернуть сервер и описание использованного способа."""
    server = MCPServer("odata1c-probe")
    try:
        decorator = server.tool(name="odata1c_commit", meta=META)
        way = "аргумент meta= у декоратора tool()"
    except TypeError as exc:
        print(f"meta= у декоратора не принят: {exc}")
        decorator = server.tool(name="odata1c_commit")
        way = "meta= недоступен"

    @decorator
    def commit(pending_id: str) -> str:
        """Выполнить подготовленную операцию записи."""
        return pending_id

    print(f"--- способ объявления: {way} ---")
    return server, way


async def list_via_server(server: MCPServer) -> list[types.Tool]:
    """Что сервер отдаёт в ответ на tools/list."""
    print("--- что отдаёт сервер ---")
    tools = await server.list_tools()
    for tool in tools:
        print(json.dumps(tool.model_dump(by_alias=True, exclude_none=True, mode="json"),
                         ensure_ascii=False, indent=2))
    return tools


async def list_via_http_client(server: MCPServer) -> None:
    """Сквозная проверка настоящим транспортом: Streamable HTTP, как у демона."""
    print("--- что видит клиент по Streamable HTTP ---")
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    config = uvicorn.Config(server.streamable_http_app(), host="127.0.0.1", port=7179,
                            log_level="warning")
    http = uvicorn.Server(config)
    task = asyncio.create_task(http.serve())
    while not http.started:
        await asyncio.sleep(0.05)
    try:
        # В mcp 2.x контекст отдаёт два потока, без функции получения идентификатора сеанса.
        async with streamable_http_client("http://127.0.0.1:7179/mcp") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                print(json.dumps(result.model_dump(by_alias=True, exclude_none=True, mode="json"),
                                 ensure_ascii=False, indent=2))
                for tool in result.tools:
                    if tool.name == "odata1c_commit":
                        доехало = (tool.meta or {}).get("anthropic/requiresUserInteraction")
                        print(f"ИТОГ: requiresUserInteraction у клиента = {доехало!r}")
    finally:
        http.should_exit = True
        await task


async def main() -> None:
    show_api()
    server, _ = build_server()
    await list_via_server(server)
    await list_via_http_client(server)


if __name__ == "__main__":
    asyncio.run(main())
