"""Проба P3: лаунчер — stdio-сервер снаружи, Streamable HTTP-клиент внутрь, без своей логики.

Задача пробы: убедиться, что запрос сервера к клиенту (elicitation/create) доходит до
stdio-клиента и ответ возвращается обратно. Схема повторяет SPEC §2.1: тонкий процесс без
собственной бизнес-логики, только пересылка вызовов тулов и обратная пересылка запросов демона.

API mcp 2.2.0 отличается от версии 1.x, под которую был написан исходный черновик пробы
(см. docs/probes/P2-tool-meta.md):
  - `streamablehttp_client` (3 значения) -> `streamable_http_client` (2 значения: read, write);
  - низкоуровневый `Server` из mcp.server.lowlevel в 2.x — не декораторный: обработчики
    `on_list_tools` / `on_call_tool` передаются в конструктор, а `ctx` (ServerRequestContext)
    приходит первым аргументом в каждый обработчик, а не через context-var `server.request_context`
    (такого атрибута у Server в 2.x больше нет);
  - запрос сервера к клиенту (elicitation) пересылается вызовом `ServerSession.elicit_form(...)`
    у downstream-сессии, а не вручную собранным `ServerRequest(ElicitRequest(...))`.

В пробе один stdio-клиент на процесс лаунчера, поэтому для передачи downstream-сессии из
`on_call_tool` в callback `on_elicit` достаточно двух module-level переменных, выставляемых перед
вызовом апстрима — конкурентных вызовов тут нет.
"""

import anyio
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

URL = "http://127.0.0.1:7179/mcp"

# Текущая downstream-сессия (сторона stdio-клиента) и id её запроса tools/call, к которому
# относится ожидаемая elicitation — выставляются в on_call_tool перед обращением к апстриму.
_downstream_session: object | None = None
_downstream_request_id: object | None = None


async def on_elicit(
    context: object, params: types.ElicitRequestParams
) -> types.ElicitResult | types.ErrorData:
    """Запрос демона (апстрим) к пользователю: переадресовать своему stdio-клиенту (downstream)."""
    print(f"[лаунчер] получен elicitation/create от демона: {params.message!r}", flush=True)
    if _downstream_session is None:
        return types.ErrorData(code=types.INTERNAL_ERROR, message="нет активной downstream-сессии")
    if params.mode != "form":
        return types.ErrorData(code=types.INVALID_REQUEST, message="проба поддерживает только form-elicitation")
    result = await _downstream_session.elicit_form(
        params.message, params.requested_schema, related_request_id=_downstream_request_id
    )
    print(f"[лаунчер] ответ клиента переслан демону: action={result.action}", flush=True)
    return result


async def main() -> None:
    global _downstream_session, _downstream_request_id

    async with streamable_http_client(URL) as (up_read, up_write):
        async with ClientSession(up_read, up_write, elicitation_callback=on_elicit) as upstream:
            await upstream.initialize()

            async def on_list_tools(ctx, params):
                return await upstream.list_tools()

            async def on_call_tool(ctx, params: types.CallToolRequestParams):
                global _downstream_session, _downstream_request_id
                _downstream_session = ctx.session
                _downstream_request_id = ctx.request_id
                return await upstream.call_tool(params.name, params.arguments or {})

            proxy = Server(
                "odata1c-probe-launcher",
                on_list_tools=on_list_tools,
                on_call_tool=on_call_tool,
            )

            async with stdio_server() as (read, write):
                await proxy.run(read, write, proxy.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
