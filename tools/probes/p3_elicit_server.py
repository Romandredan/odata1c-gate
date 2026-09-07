"""Проба P3 (SPEC §15.2): демон-заглушка со Streamable HTTP и тулом, который спрашивает
пользователя через elicitation.

API mcp 2.2.0 (см. docs/probes/P2-tool-meta.md): FastMCP переименован в MCPServer
(mcp.server.mcpserver). Сигнатура MCPServer.tool и класс Context — оттуда же.
"""

from pydantic import BaseModel, Field

from mcp.server.mcpserver import Context, MCPServer

server = MCPServer("odata1c-probe-daemon")


class Confirm(BaseModel):
    approve: bool = Field(description="Подтвердить запись в 1С")


@server.tool(name="odata1c_probe_commit")
async def probe_commit(pending_id: str, ctx: Context) -> str:
    """Спросить подтверждение и вернуть ответ пользователя."""
    result = await ctx.elicit(message=f"Выполнить операцию {pending_id}?", schema=Confirm)
    return f"action={result.action} data={getattr(result, 'data', None)}"


if __name__ == "__main__":
    server.run(transport="streamable-http", host="127.0.0.1", port=7179)
