"""Проба P3: stdio-клиент, отвечающий на elicitation вместо пользователя."""

import asyncio
import sys

import mcp.types as types
from mcp import StdioServerParameters, stdio_client
from mcp.client.session import ClientSession


async def on_elicit(context, params: types.ElicitRequestParams) -> types.ElicitResult:
    print(f"[клиент] пришёл запрос подтверждения: {params.message}", flush=True)
    return types.ElicitResult(action="accept", content={"approve": True})


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["tools/probes/p3_launcher.py"],
        env={"PYTHONIOENCODING": "utf-8"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, elicitation_callback=on_elicit) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("[клиент] тулы:", [t.name for t in tools.tools], flush=True)
            result = await session.call_tool("odata1c_probe_commit", {"pending_id": "p-1"})
            print("[клиент] результат тула:", result.content, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
