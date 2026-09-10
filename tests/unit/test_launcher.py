"""Прокси лаунчера в памяти (план M1d, задача 6): два перехода `InMemoryTransport`
(клиент → прокси → апстрим → демон-заглушка), без сети и без реального демона.

Демон-заглушка — обычный `MCPServer` с одним тулом, одним статическим ресурсом, одним шаблоном
ресурса, одним промптом и одним тулом, который спрашивает подтверждение через `ctx.elicit`
(elicitation) — тот же приём, что в пробе P3 (`tools/probes/p3_elicit_server.py`), но целиком
в памяти: сквозная проверка по-настоящему (stdio, реальный демон) — `tests/integration/
test_end_to_end.py`.
"""

import contextlib

import mcp.types as types
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, Field

from odata1c.daemon import SCOPE_BASES_HEADER, SCOPE_DEFAULT_HEADER
from odata1c.launcher import ProxyHolder, build_proxy, forward_elicit, scope_headers


class Подтверждение(BaseModel):
    approve: bool = Field(description="подтвердить действие")


def _демон_заглушка() -> MCPServer:
    сервер = MCPServer("fake-demon")

    @сервер.tool(name="echo")
    async def echo(text: str) -> str:
        return f"эхо: {text}"

    @сервер.resource("odata1c://cheatsheet")
    def шпаргалка() -> str:
        return "статический ресурс демона"

    @сервер.resource("odata1c://policy/{base}")
    def политика(base: str) -> str:
        return f"политика базы {base}"

    @сервер.prompt(name="подсказка")
    def подсказка(topic: str) -> str:
        return f"промпт про {topic}"

    @сервер.tool(name="confirm")
    async def confirm(ctx: Context) -> str:
        результат = await ctx.elicit(message="подтвердите операцию", schema=Подтверждение)
        return f"action={результат.action}"

    return сервер


async def _клиентский_elicit(
    context: object, params: types.ElicitRequestParams
) -> types.ElicitResult:
    return types.ElicitResult(action="accept", content={"approve": True})


@contextlib.asynccontextmanager
async def _подключение():
    """Собирает обе стороны прокси: апстрим-сессия к демону-заглушке (транспорт 1), прокси
    поверх неё, downstream-клиент к прокси (транспорт 2). Отдаёт downstream-клиент.

    Не фикстура `pytest`, а обычный асинхронный контекст-менеджер, используемый внутри одного
    теста (`async with _подключение() as client: ...`) — фикстура-генератор здесь ломается:
    `InMemoryTransport` держит внутри `anyio.create_task_group()`, у которого cancel scope
    привязан к конкретной задаче (`Task`) asyncio; `pytest-asyncio` выполняет шаг ДО `yield`
    и шаг ПОСЛЕ `yield` асинхронной фикстуры в разных задачах, и выход из такого cancel scope
    в другой задаче — `RuntimeError` у anyio, а не у прокси. Один тест — одна задача, поэтому
    вход и выход остаются в одной и той же."""
    демон = _демон_заглушка()
    holder = ProxyHolder()

    async with (
        InMemoryTransport(демон) as (up_read, up_write),
        ClientSession(up_read, up_write, elicitation_callback=forward_elicit(holder)) as upstream,
    ):
        await upstream.initialize()
        proxy = build_proxy(upstream, holder)

        async with (
            InMemoryTransport(proxy) as (down_read, down_write),
            ClientSession(down_read, down_write, elicitation_callback=_клиентский_elicit) as client,
        ):
            await client.initialize()
            yield client


async def test_прокси_пересылает_все_семь_обработчиков_и_elicitation():
    async with _подключение() as client:
        тулы = await client.list_tools()
        assert {т.name for т in тулы.tools} == {"echo", "confirm"}

        результат = await client.call_tool("echo", {"text": "привет"})
        assert результат.is_error is False
        assert результат.content[0].text == "эхо: привет"

        ресурсы = await client.list_resources()
        assert {р.uri for р in ресурсы.resources} == {"odata1c://cheatsheet"}

        шаблоны = await client.list_resource_templates()
        assert {ш.uri_template for ш in шаблоны.resource_templates} == {"odata1c://policy/{base}"}

        статический = await client.read_resource("odata1c://cheatsheet")
        assert статический.contents[0].text == "статический ресурс демона"

        по_шаблону = await client.read_resource("odata1c://policy/ut")
        assert по_шаблону.contents[0].text == "политика базы ut"

        промпты = await client.list_prompts()
        assert {п.name for п in промпты.prompts} == {"подсказка"}

        промпт = await client.get_prompt("подсказка", {"topic": "гейт"})
        assert "промпт про гейт" in промпт.messages[0].content.text

        # Elicitation: тул демона (апстрим) спрашивает подтверждение — запрос идёт вверх по
        # апстрим-сессии лаунчера, оттуда `forward_elicit` пересылает его downstream-клиенту
        # (`_клиентский_elicit` отвечает accept), ответ возвращается тулу тем же путём обратно.
        подтверждение = await client.call_tool("confirm", {})
        assert подтверждение.is_error is False
        assert подтверждение.content[0].text == "action=accept"


def test_scope_headers_с_базами_и_умолчанием():
    assert scope_headers(["ut", "buh"], "ut") == {
        SCOPE_BASES_HEADER: "ut,buh",
        SCOPE_DEFAULT_HEADER: "ut",
    }


def test_scope_headers_без_аргументов_заголовки_не_выставляются():
    # Не {SCOPE_BASES_HEADER: ""} — пустая строка для daemon.scope_from_headers означает «явно
    # ни одной базы», а отсутствие bases здесь значит «не сужено», это разные вещи.
    assert scope_headers(None, None) == {}


async def test_elicitation_без_downstream_сессии_отклоняется():
    """`forward_elicit` вызванный до первого запроса downstream-клиента (`holder.session is
    None`) — отказ, а не падение: проверяется напрямую на колбэке, без второго транспорта."""
    holder = ProxyHolder()
    колбэк = forward_elicit(holder)
    результат = await колбэк(
        None,
        types.ElicitRequestFormParams(
            mode="form",
            message="подтвердите",
            requested_schema={"type": "object", "properties": {}},
        ),
    )
    assert isinstance(результат, types.ElicitResult)
    assert результат.action == "decline"
