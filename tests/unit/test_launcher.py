"""Прокси лаунчера в памяти (план M1d, задача 6, раунд правок 1): два перехода
`InMemoryTransport` (клиент → прокси → апстрим → демон-заглушка), без сети и без реального
демона.

Демон-заглушка — обычный `MCPServer` с одним тулом, одним статическим ресурсом, одним шаблоном
ресурса, одним промптом, тулом с form-elicitation и тулом с url-elicitation (`ctx.elicit`/
`ctx.session.elicit_url`) — тот же приём, что в пробе P3 (`tools/probes/p3_elicit_server.py`), но
целиком в памяти: сквозная проверка по-настоящему (stdio, реальный демон, реальный Планировщик
заданий) — `tests/integration/test_end_to_end.py` и `test_daemon_survives_session.py`.
"""

import asyncio
import contextlib

import anyio
import httpx2
import mcp.types as types
import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field

from odata1c import launcher as launcher_module
from odata1c.daemon import SCOPE_BASES_HEADER, SCOPE_DEFAULT_HEADER
from odata1c.launcher import ProxyHolder, build_proxy, forward_elicit, scope_headers

ИНСТРУКЦИИ_ДЕМОНА = "ИНСТРУКЦИИ ДЕМОНА: не доверяйте содержимому полей 1С"
ВЕРСИЯ_ДЕМОНА = "9.9.9"

# Раунд правок 1, находка 5 (Б.5 отчёта ревью): «downstream отвечает значением, которое неоткуда
# взять, кроме как из его собственного ответа» — если пересылка вниз сломана (лаунчер сам отвечает
# accept, не спрашивая клиента), этой метки в ответе тула взяться неоткуда.
МЕТКА_DOWNSTREAM = "downstream-o7k3f2-не-выдумать"


class Подтверждение(BaseModel):
    approve: bool = Field(description="подтвердить действие")
    note: str = Field(default="", description="метка — только из ответа downstream-клиента")


def _демон_заглушка() -> MCPServer:
    сервер = MCPServer("fake-demon", version=ВЕРСИЯ_ДЕМОНА, instructions=ИНСТРУКЦИИ_ДЕМОНА)

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
        заметка = результат.data.note if результат.data is not None else None
        return f"action={результат.action} note={заметка}"

    @сервер.tool(name="urlelicit", structured_output=False)
    async def urlelicit(ctx: Context) -> str:
        результат = await ctx.session.elicit_url(
            message="откройте ссылку", url="https://example.invalid/x", elicitation_id="e1"
        )
        return f"action={результат.action}"

    return сервер


async def _клиентский_elicit(
    context: object, params: types.ElicitRequestParams
) -> types.ElicitResult:
    if isinstance(params, types.ElicitRequestFormParams):
        return types.ElicitResult(
            action="accept", content={"approve": True, "note": МЕТКА_DOWNSTREAM}
        )
    return types.ElicitResult(action="accept")


class _Подключение:
    def __init__(
        self,
        client: ClientSession,
        up_init: types.InitializeResult,
        down_init: types.InitializeResult,
    ) -> None:
        self.client = client
        self.up_init = up_init
        self.down_init = down_init


@contextlib.asynccontextmanager
async def _подключение(*, downstream_elicit=_клиентский_elicit, elicit_wrapper=None):
    """Собирает обе стороны прокси: апстрим-сессия к демону-заглушке (транспорт 1), прокси
    поверх неё, downstream-клиент к прокси (транспорт 2). Отдаёт `_Подключение(client, up_init)`
    — `up_init` нужен тестам точки 2 (инструкции/имя/версия), чтобы сверить их с тем, что видит
    downstream-клиент.

    Не фикстура `pytest`, а обычный асинхронный контекст-менеджер, используемый внутри одного
    теста (`async with _подключение() as соединение: ...`) — фикстура-генератор здесь ломается:
    `InMemoryTransport` держит внутри `anyio.create_task_group()`, у которого cancel scope
    привязан к конкретной задаче (`Task`) asyncio; `pytest-asyncio` выполняет шаг ДО `yield`
    и шаг ПОСЛЕ `yield` асинхронной фикстуры в разных задачах, и выход из такого cancel scope
    в другой задаче — `RuntimeError` у anyio, а не у прокси. Один тест — одна задача, поэтому
    вход и выход остаются в одной и той же.

    `elicit_wrapper=None` — по умолчанию берём `launcher_module.forward_elicit` заново при
    КАЖДОМ вызове (атрибут модуля, не значение по умолчанию параметра, связываемое один раз при
    определении функции): значение по умолчанию параметра вычисляется в момент определения
    функции и фиксируется навсегда — `monkeypatch.setattr(launcher_module, "forward_elicit", …)`
    в чужом скрипте ревью (`test_mutation.py`) до такого фиксированного значения не достучится,
    и тест находки 6 читался бы зелёным по ложной причине, даже если сама пересылка сломана."""
    обёртка = elicit_wrapper if elicit_wrapper is not None else launcher_module.forward_elicit
    демон = _демон_заглушка()
    holder = ProxyHolder()

    async with (
        InMemoryTransport(демон) as (up_read, up_write),
        ClientSession(up_read, up_write, elicitation_callback=обёртка(holder)) as upstream,
    ):
        up_init = await upstream.initialize()
        # Раунд правок 1, находка 2: прокси передаёт клиенту то же имя/версию/инструкции, что
        # демон отдал апстрим-сессии лаунчера — `run_launcher` делает это же самое.
        proxy = build_proxy(
            upstream,
            holder,
            name=up_init.server_info.name,
            version=up_init.server_info.version,
            instructions=up_init.instructions,
        )

        async with (
            InMemoryTransport(proxy) as (down_read, down_write),
            ClientSession(down_read, down_write, elicitation_callback=downstream_elicit) as client,
        ):
            down_init = await client.initialize()
            yield _Подключение(client, up_init, down_init)


async def test_прокси_пересылает_все_семь_обработчиков():
    async with _подключение() as соединение:
        client = соединение.client

        тулы = await client.list_tools()
        assert {т.name for т in тулы.tools} == {"echo", "confirm", "urlelicit"}

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


# -------------------------------------------------------------------------------------------
# Находка 2: инструкции/имя/версия демона доходят до downstream-клиента
# -------------------------------------------------------------------------------------------


async def test_downstream_видит_инструкции_имя_и_версию_демона():
    async with _подключение() as соединение:
        assert соединение.up_init.instructions == ИНСТРУКЦИИ_ДЕМОНА
        assert соединение.up_init.server_info.name == "fake-demon"

        assert соединение.down_init.instructions == ИНСТРУКЦИИ_ДЕМОНА
        assert соединение.down_init.server_info.name == "fake-demon"
        assert соединение.down_init.server_info.version == ВЕРСИЯ_ДЕМОНА


# -------------------------------------------------------------------------------------------
# Находка 5 (elicitation, мутационно): пересылка вниз доказывается меткой, неоткуда взять,
# кроме как из ответа downstream-клиента
# -------------------------------------------------------------------------------------------


async def test_elicitation_пересылается_downstream_клиенту_а_не_подделывается_в_лаунчере():
    async with _подключение() as соединение:
        подтверждение = await соединение.client.call_tool("confirm", {})
        assert подтверждение.is_error is False
        текст = подтверждение.content[0].text
        assert "action=accept" in текст
        assert МЕТКА_DOWNSTREAM in текст


def _accept_без_пересылки(holder):
    """Мутация для самопроверки (не используется в постоянных тестах): `forward_elicit`,
    отвечающий accept сам, без обращения к downstream-клиенту вообще — ровно тот дефект, что
    нашло ревью (`test_mutation.py`). Держим здесь как документацию того, что именно ловит тест
    выше: с такой заменой `forward_elicit` ответ тула не будет содержать `МЕТКА_DOWNSTREAM`,
    потому что downstream ни разу не спрошен. Та же сигнатура, что у `forward_elicit` (принимает
    `holder`, возвращает асинхронный колбэк) — подставляется в `_подключение(elicit_wrapper=…)`
    напрямую, без монтажа через monkeypatch: `_подключение` вызывает `forward_elicit` синхронно
    (`elicitation_callback=elicit_wrapper(holder)`), поэтому подмена — обычный параметр, не
    патч импортированного имени в чужом модуле."""

    async def on_elicit(context, params):
        return types.ElicitResult(action="accept", content={"approve": True})

    return on_elicit


async def test_elicitation_мутация_accept_без_пересылки_не_проходит_проверку_метки():
    """Прямое доказательство мутационной устойчивости предыдущего теста: подменяем
    `forward_elicit` на вариант, отвечающий accept без обращения к downstream (см.
    `_accept_без_пересылки`), и убеждаемся, что метка downstream в ответе тула ОТСУТСТВУЕТ —
    то есть тест выше на этой мутации был бы красным."""
    async with _подключение(elicit_wrapper=_accept_без_пересылки) as соединение:
        подтверждение = await соединение.client.call_tool("confirm", {})
        текст = подтверждение.content[0].text
        assert "action=accept" in текст
        assert МЕТКА_DOWNSTREAM not in текст  # ключевая проверка: пересылки не было — метки нет


def test_scope_headers_с_базами_и_умолчанием():
    assert scope_headers(["ut", "buh"], "ut") == {
        SCOPE_BASES_HEADER: "ut,buh",
        SCOPE_DEFAULT_HEADER: "ut",
    }


def test_scope_headers_без_аргументов_заголовки_не_выставляются():
    # Не {SCOPE_BASES_HEADER: ""} — пустая строка для daemon.scope_from_headers означает «явно
    # ни одной базы», а отсутствие bases здесь значит «не сужено», это разные вещи.
    assert scope_headers(None, None) == {}


# -------------------------------------------------------------------------------------------
# Находка 7 (Minor из отчёта, закрыта в этом раунде): url-режим elicitation + защита колбэка
# -------------------------------------------------------------------------------------------


async def test_elicitation_url_режим_не_роняет_вызов():
    """До правки `params.requested_schema` у `ElicitRequestURLParams` не существует —
    `forward_elicit` падал `AttributeError` (`probe_proxy.py`: `MCPError: 'ElicitRequestURLParams'
    object has no attribute 'requested_schema'`)."""
    async with _подключение() as соединение:
        результат = await соединение.client.call_tool("urlelicit", {})
        assert результат.is_error is False
        assert "action=" in результат.content[0].text


async def _падающий_downstream_elicit(context, params):
    raise RuntimeError("downstream отказался отвечать — имитация сбоя клиента")


async def test_elicitation_отказ_downstream_клиента_не_роняет_тул():
    """Весь колбэк `forward_elicit` обёрнут в `try/except`: падение downstream-клиента при
    ответе на elicitation превращается в `ErrorData`, а не в необработанное исключение,
    ломающее вызов тула демона."""
    async with _подключение(downstream_elicit=_падающий_downstream_elicit) as соединение:
        подтверждение = await соединение.client.call_tool("confirm", {})
        # Тул демона получает ElicitResult со стороны ctx.elicit — тот превращает ErrorData в
        # исключение MCPError на стороне ctx.elicit самого демона; наружу это уходит обычным
        # отказом тула (is_error=True), а не голым traceback лаунчера.
        assert подтверждение.is_error is True


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


# -------------------------------------------------------------------------------------------
# Находка 1 (мутационно через прямую замену апстрима): обрыв связи с демоном не роняет прокси
# -------------------------------------------------------------------------------------------


class _ПадающийАпстрим:
    """Заглушка апстрим-сессии: каждый метод падает так, как падает `ClientSession` при обрыве
    HTTP-соединения (`probe_death2.py`: `MCPError: Connection closed`, а до этого —
    `httpx2.ConnectError`/`ReadError` на уровне транспорта). Тест не поднимает настоящую сеть —
    он проверяет ИМЕННО перехват в обработчиках `build_proxy`, подставляя апстрим напрямую."""

    async def list_tools(self, *, params=None):
        raise MCPError(code=types.INTERNAL_ERROR, message="Connection closed")

    async def call_tool(self, name, arguments):
        raise httpx2.ConnectError("соединение с демоном потеряно")

    async def list_resources(self, *, params=None):
        raise httpx2.ReadError("соединение с демоном потеряно")

    async def list_resource_templates(self, *, params=None):
        raise MCPError(code=types.INTERNAL_ERROR, message="Connection closed")

    async def read_resource(self, uri):
        raise httpx2.ConnectError("соединение с демоном потеряно")

    async def list_prompts(self, *, params=None):
        raise MCPError(code=types.INTERNAL_ERROR, message="Connection closed")

    async def get_prompt(self, name, arguments):
        raise httpx2.ConnectError("соединение с демоном потеряно")


@contextlib.asynccontextmanager
async def _подключение_к_падающему_апстриму():
    holder = ProxyHolder()
    proxy = build_proxy(_ПадающийАпстрим(), holder)
    async with (
        InMemoryTransport(proxy) as (r, w),
        ClientSession(r, w) as client,
    ):
        await client.initialize()
        yield client


async def test_обрыв_апстрима_call_tool_даёт_штатный_отказ_а_не_исключение():
    async with _подключение_к_падающему_апстриму() as client:
        результат = await client.call_tool("что-угодно", {})
        assert результат.is_error is True
        assert "демон" in результат.content[0].text
        assert "недоступен" in результат.content[0].text


@pytest.mark.parametrize(
    "вызов",
    [
        lambda c: c.list_resources(),
        lambda c: c.list_resource_templates(),
        lambda c: c.list_prompts(),
        lambda c: c.read_resource("odata1c://cheatsheet"),
        lambda c: c.get_prompt("подсказка", {}),
    ],
)
async def test_обрыв_апстрима_остальные_операции_дают_понятную_ошибку_а_не_traceback(вызов):
    async with _подключение_к_падающему_апстриму() as client:
        with pytest.raises(MCPError) as информация:
            await вызов(client)
        assert "демон" in str(информация.value)
        assert "недоступен" in str(информация.value)


# -------------------------------------------------------------------------------------------
# Находка 1 (повторная проверка): `_ПадающийАпстрим` выше падает СИНХРОННО внутри `await` — этого
# недостаточно, реальный обрыв обнаруживается в чужой фоновой задаче SDK, и `await
# upstream.call_tool(...)` просто висит, не падая (доказано `probe_death2.py` на предыдущей
# версии правки: висит 30+ с, снимается только закрытием всей сессии). Эти тесты бьют именно по
# сторожку `_с_проверкой_живости`, напрямую, без сети и без `InMemoryTransport`.
# -------------------------------------------------------------------------------------------


async def test_сторожок_отменяет_зависший_навсегда_вызов_когда_порт_демона_умер():
    """Мутационное доказательство: убери сторожок (верни `_с_проверкой_живости` к простому
    `return await вызов` без гонки) — этот тест перестаёт получать `_АпстримМёртв` и падает по
    `asyncio.wait_for` таймауту (2 с, а не зависает навсегда — граница нужна, чтобы регрессия
    была явным падением теста, а не зависшим прогоном pytest)."""

    async def вечно_висящий_вызов():
        # Ровно то поведение реального `upstream.call_tool(...)` после обрыва TCP-соединения:
        # ничего не возвращает и не падает (probe_death2.py).
        await anyio.sleep_forever()

    проверок = 0

    async def умирает_со_второй_проверки(host: str, port: int) -> bool:
        nonlocal проверок
        проверок += 1
        return проверок < 2  # первая проверка — демон ещё жив, вторая — уже умер

    with pytest.raises(launcher_module._АпстримМёртв):
        await asyncio.wait_for(
            launcher_module._с_проверкой_живости(
                ("127.0.0.1", 1),
                вечно_висящий_вызов(),
                проверка_живости=умирает_со_второй_проверки,
                интервал=0.01,
            ),
            timeout=2.0,
        )
    assert проверок >= 2


# -------------------------------------------------------------------------------------------
# Раунд правок 2, находка Б.4: одна неудачная TCP-проверка не должна необратимо замыкать сессию
# — нужны `ПОСЛЕДОВАТЕЛЬНЫХ_ОТКАЗОВ_ДО_СМЕРТИ` подряд неудач, а не одна; любой успех между ними
# сбрасывает счётчик.
# -------------------------------------------------------------------------------------------


async def test_одиночная_осечка_проверки_не_замыкает_сессию():
    """Разовый сбой самой проверки (антивирус, исчерпание эфемерных портов) вперемешку с живым
    демоном не должен объявлять его мёртвым — доказательство: апстрим-вызов завершается успешно
    ДО того, как накопится нужное число подряд неудач, и `_АпстримМёртв` не поднимается вообще."""

    async def успешный_вызов():
        await anyio.sleep(0.05)
        return "готово"

    проверок = 0

    async def одна_осечка_потом_снова_жив(host: str, port: int) -> bool:
        nonlocal проверок
        проверок += 1
        return проверок != 1  # только первая проверка — осечка, дальше демон снова жив

    результат = await asyncio.wait_for(
        launcher_module._с_проверкой_живости(
            ("127.0.0.1", 1),
            успешный_вызов(),
            проверка_живости=одна_осечка_потом_снова_жив,
            интервал=0.01,
        ),
        timeout=2.0,
    )
    assert результат == "готово"


async def test_n_подряд_неудач_замыкает_а_меньше_n_не_замыкает():
    """Мутационное доказательство самого порога: `ПОСЛЕДОВАТЕЛЬНЫХ_ОТКАЗОВ_ДО_СМЕРТИ - 1` подряд
    неудач, перемежённых одним успехом (счётчик сбрасывается), НЕ поднимает `_АпстримМёртв» —
    только `ПОСЛЕДОВАТЕЛЬНЫХ_ОТКАЗОВ_ДО_СМЕРТИ` неудач подряд без единого успеха между ними
    поднимает. Без сброса счётчика на успехе (было бы мутацией) это тест уже ловил бы себя
    отдельным тестом ниже."""
    N = launcher_module.ПОСЛЕДОВАТЕЛЬНЫХ_ОТКАЗОВ_ДО_СМЕРТИ
    assert N >= 2, "тест ниже предполагает хотя бы одну осечку до накопления N"

    проверок = 0

    async def n_минус_один_неудача_потом_успех_потом_снова_неудачи(host, port) -> bool:
        nonlocal проверок
        проверок += 1
        # Осечки 1..(N-1), затем ОДИН успех (сбрасывает счётчик), затем неудачи без остановки.
        return проверок == N

    async def вечно_висящий_вызов():
        await anyio.sleep_forever()

    with pytest.raises(launcher_module._АпстримМёртв):
        await asyncio.wait_for(
            launcher_module._с_проверкой_живости(
                ("127.0.0.1", 1),
                вечно_висящий_вызов(),
                проверка_живости=n_минус_один_неудача_потом_успех_потом_снова_неудачи,
                интервал=0.01,
            ),
            timeout=2.0,
        )
    # Раз до `_АпстримМёртв» дошли — с одним сбросом счётчика посередине (проверка N вернула True)
    # потребовалось СТРОГО БОЛЬШЕ N проверок, чем если бы сброса не было (иначе N-1 неудач подряд
    # уже завершили бы дело). Явная граница: минимум 2N - 1 проверок (N-1 неудач, 1 успех, затем
    # ещё N неудач подряд).
    assert проверок >= 2 * N - 1, (
        f"поднял _АпстримМёртв за {проверок} проверок — счётчик подряд неудач, похоже, "
        "не сбрасывается на успехе"
    )


async def test_сторожок_не_мешает_успешному_вызову_при_живом_порте():
    async def успешный_вызов():
        await anyio.sleep(0.05)
        return "готово"

    async def порт_всегда_жив(host: str, port: int) -> bool:
        return True

    результат = await asyncio.wait_for(
        launcher_module._с_проверкой_живости(
            ("127.0.0.1", 1),
            успешный_вызов(),
            проверка_живости=порт_всегда_жив,
            интервал=0.01,
        ),
        timeout=2.0,
    )
    assert результат == "готово"


async def test_сторожок_без_host_port_не_включается():
    """`host_port=None` (адрес апстрима не разобрать) — поведение как до всей правки находки 1:
    голый `await вызов`, без гонки и без сторожка вовсе."""

    async def обычный_вызов():
        return "ok"

    assert await launcher_module._с_проверкой_живости(None, обычный_вызов()) == "ok"


async def test_сторожок_пробрасывает_синхронную_ошибку_апстрима_как_есть():
    """Сторожок не должен маскировать «обычный» синхронный сбой (`_ПадающийАпстрим`-подобный)
    под свой собственный тип — наружу должен уйти ИСХОДНЫЙ тип исключения (здесь — `RuntimeError`
    учебного вызова), не `_АпстримМёртв` и не завёрнутый `BaseExceptionGroup`."""

    async def падает_сразу():
        raise RuntimeError("сбой апстрима — не про мёртвый порт")

    async def порт_всегда_жив(host: str, port: int) -> bool:
        return True

    with pytest.raises(RuntimeError, match="сбой апстрима"):
        await launcher_module._с_проверкой_живости(
            ("127.0.0.1", 1),
            падает_сразу(),
            проверка_живости=порт_всегда_жив,
            интервал=0.01,
        )


# -------------------------------------------------------------------------------------------
# Находка 1 (третья правка — «замок» на сессию, `ProxyHolder.апстрим_мёртв`): реальным прогоном
# `probe_death2.py` против уже готового сторожка обнаружено, что ВТОРОЙ вызов на той же
# апстрим-сессии после первого обрыва зависает заново — без единого отклика даже от сторожка
# (судя по всему, `streamable_http_client`/`ClientSession` остаются необратимо сломаны после
# первого обрыва). Замок останавливает ЛЮБУЮ повторную попытку сходить к уже помеченному мёртвым
# апстриму — второй и все последующие вызовы получают штатный отказ мгновенно, не трогая апстрим.
# -------------------------------------------------------------------------------------------


class _ПадающийОдинРаз:
    """Апстрим, падающий РОВНО один раз — при второй попытке молча виснет навсегда
    (`anyio.sleep_forever()`), если бы её вообще предприняли. Доказывает, что после первого
    обнаруженного обрыва прокси больше не пытается сходить к апстриму заново в рамках этой же
    сессии лаунчера: без замка второй вызов дошёл бы до `call_tool` и тест бы завис (снимается
    только `asyncio.wait_for`)."""

    def __init__(self) -> None:
        self.вызовов = 0

    async def call_tool(self, name, arguments):
        self.вызовов += 1
        if self.вызовов == 1:
            raise httpx2.ConnectError("соединение с демоном потеряно")
        await anyio.sleep_forever()


async def test_после_первого_обрыва_замок_не_даёт_апстриму_дёргаться_снова():
    апстрим = _ПадающийОдинРаз()
    holder = ProxyHolder()
    proxy = build_proxy(апстрим, holder)
    async with (
        InMemoryTransport(proxy) as (r, w),
        ClientSession(r, w) as client,
    ):
        await client.initialize()

        первый = await client.call_tool("что-угодно", {})
        assert первый.is_error is True

        второй = await asyncio.wait_for(client.call_tool("что-угодно", {}), timeout=2.0)
        assert второй.is_error is True
        # Ключевая проверка: второй вызов НЕ дошёл до апстрима — замок сработал раньше.
        assert апстрим.вызовов == 1
