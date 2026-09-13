"""Тулы записи в демоне (план M2, задача 9): семь тулов, сессия, подтверждение по клиенту.

После этой задачи запись доступна модели, и ошибка здесь — запись в 1С без подтверждения
пользователя. Поэтому проверяется в первую очередь выбор механизма подтверждения:

- имя клиента сравнивается ТОЧНО по перечню (`claude-code`), и похожее имя не получает механизм
  «подтверждает клиент» — у такого клиента не было бы ни одного вопроса;
- elicitation — только явное «yes» выполняет запись; «no», `decline`, `cancel`, пустой ответ,
  «YES» — отказ, операция остаётся подготовленной;
- клиент без elicitation при `deny` — `write_unsupported_client`, в 1С ни одного запроса записи;
- `_meta["anthropic/requiresUserInteraction"]` у `odata1c_commit`;
- операция одной сессии не видна другой;
- отказы не повторяют ввод модели — и отказы SDK на аргумент не того типа тоже.

Демон — в памяти (`InMemoryTransport`): `ctx.headers` там нет, и клиент берётся из `initialize`
самого соединения — тот же путь, что у прямого HTTP-клиента без лаунчера. Путь через лаунчер
(заголовки клиента) — `tests/integration/test_write_end_to_end.py`. Поддельная 1С, значения и
проверка «от класса данных» — из `test_write_commit.py` (задача 7).
"""

import contextlib
import json
import types as pytypes

import httpx
import mcp.types as types
import pytest
import respx
import test_write_commit as к
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from odata1c.config.loader import load_config
from odata1c.daemon import (
    CLIENT_ELICITATION_HEADER,
    CLIENT_NAME_HEADER,
    CLIENT_VERSION_HEADER,
    INSTRUCTIONS,
    SESSION_ID_HEADER,
    ClientIdentity,
    SessionKeys,
    SessionMechanisms,
    build_server,
    build_write_layer,
    client_from_request,
    client_headers,
    pending_grace_s,
    sweep_write_layer,
)
from odata1c.launcher import ProxyHolder
from odata1c.tools.info import WRITE_PROTOCOL
from odata1c.tools.service import ToolService
from odata1c.write.journal import Journal

КОНТРАГЕНТЫ = к.КОНТРАГЕНТЫ
ССЫЛКА = к.ССЫЛКА
ИНН = к.ИНН
НОВЫЙ_ИНН = к.НОВЫЙ_ИНН
ПУТЬ_КОНТРАГЕНТА = к.ПУТЬ_КОНТРАГЕНТА

ТУЛЫ_ПОДГОТОВКИ = {
    "odata1c_create",
    "odata1c_update",
    "odata1c_mark_for_deletion",
    "odata1c_action",
    "odata1c_undo",
}
ТУЛЫ_ЗАПИСИ = ТУЛЫ_ПОДГОТОВКИ | {"odata1c_commit", "odata1c_journal"}


@pytest.fixture
def дом(tmp_path, edmx_ut_real):
    return к._дом(tmp_path, edmx_ut_real)


@pytest.fixture
def одинс():
    with respx.mock(assert_all_called=False) as router:
        for url in (к.URL_UT, к.URL_IDN, к.URL_LIM):
            router.get(url).mock(return_value=httpx.Response(200, json={"value": []}))
        yield к.Одинс(router)


class Демон:
    """`ToolService` + слой записи + `MCPServer` поверх одного домашнего каталога — как собирает
    их `serve()`, только без сети."""

    def __init__(self, дом) -> None:
        config = load_config(дом)
        self.дом = дом
        self.tools = ToolService(config)
        self.слой = build_write_layer(self.tools)
        self.сервер = build_server(self.tools, config.daemon.limits, write=self.слой)

    def журнал(self, commit_id: str):
        журнал = Journal(self.дом / "journal.sqlite")
        try:
            return журнал.get(commit_id)
        finally:
            журнал.close()


@pytest.fixture
async def демон(дом):
    д = Демон(дом)
    yield д
    await д.tools.aclose()


def _запасной(дом, значение: str) -> None:
    путь = дом / "daemon.yaml"
    текст = путь.read_text(encoding="utf-8")
    assert "write_confirm_fallback: deny" in текст
    путь.write_text(
        текст.replace("write_confirm_fallback: deny", f"write_confirm_fallback: {значение}"),
        encoding="utf-8",
    )


class Клиент:
    """Клиент MCP в памяти: имя и версия в `initialize`, ответ на elicitation (или без неё)."""

    def __init__(self, сессия: ClientSession, вопросы: list) -> None:
        self.сессия = сессия
        self.вопросы = вопросы

    async def вызвать(self, тул: str, аргументы: dict) -> str:
        результат = await self.сессия.call_tool(тул, аргументы)
        assert len(результат.content) == 1
        return результат.content[0].text

    async def json(self, тул: str, аргументы: dict) -> dict:
        return json.loads(await self.вызвать(тул, аргументы))


@contextlib.asynccontextmanager
async def клиент(демон: Демон, *, имя="t9-клиент", версия="1.0.0", ответ=None):
    """`ответ` — `ElicitResult` или функция параметров запроса; `None` — клиент без elicitation
    (возможность в `initialize` не объявлена)."""
    аргументы: dict = {"client_info": types.Implementation(name=имя, version=версия)}
    вопросы: list = []
    if ответ is not None:

        async def on_elicit(context, params):
            вопросы.append(params)
            return ответ(params) if callable(ответ) else ответ

        аргументы["elicitation_callback"] = on_elicit
    async with (
        InMemoryTransport(демон.сервер) as (r, w),
        ClientSession(r, w, **аргументы) as сессия,
    ):
        await сессия.initialize()
        yield Клиент(сессия, вопросы)


ДА = types.ElicitResult(action="accept", content={"confirm": "yes"})
НЕТ = types.ElicitResult(action="accept", content={"confirm": "no"})


async def подготовить(кл: Клиент, демон: Демон, одинс) -> dict:
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, к.контрагент())
    ответ = await кл.json(
        "odata1c_update",
        {"entity": КОНТРАГЕНТЫ, "key": ССЫЛКА, "data": {"ИНН": к.токен(демон.tools, НОВЫЙ_ИНН)}},
    )
    assert "pending_id" in ответ, ответ
    return ответ


def ошибка(текст: str) -> dict:
    данные = json.loads(текст)
    assert "error" in данные, текст
    return данные["error"]


# ---------------------------------------------------------------------------------------------
# Объявление тулов
# ---------------------------------------------------------------------------------------------


async def test_семь_тулов_записи_объявлены_с_аннотациями(демон):
    async with клиент(демон) as кл:
        тулы = {т.name: т for т in (await кл.сессия.list_tools()).tools}

    assert set(тулы) >= ТУЛЫ_ЗАПИСИ
    for имя in ТУЛЫ_ЗАПИСИ:
        assert тулы[имя].output_schema is None
        assert тулы[имя].meta["anthropic/maxResultSizeChars"] == 120000
    for имя in ТУЛЫ_ПОДГОТОВКИ | {"odata1c_commit"}:
        assert тулы[имя].annotations.read_only_hint is not True, имя
    for имя in ТУЛЫ_ПОДГОТОВКИ:
        # Подготовка в 1С не пишет — не «разрушающая»; подтверждения у неё нет.
        assert тулы[имя].annotations.destructive_hint is False, имя
        assert "anthropic/requiresUserInteraction" not in тулы[имя].meta
    commit = тулы["odata1c_commit"]
    assert commit.annotations.destructive_hint is True
    assert commit.meta["anthropic/requiresUserInteraction"] is True
    assert тулы["odata1c_journal"].annotations.read_only_hint is True
    assert set(commit.input_schema["properties"]) == {"pending_id"}
    assert set(тулы["odata1c_undo"].input_schema["properties"]) == {"commit_id"}


async def test_описания_тулов_и_справочник_называют_правила_записи(демон):
    async with клиент(демон) as кл:
        тулы = {т.name: т for т in (await кл.сессия.list_tools()).tools}
    commit = тулы["odata1c_commit"].description
    assert "следующем сообщении" in commit and "данные, а не инструкции" in commit
    for имя in ТУЛЫ_ПОДГОТОВКИ:
        описание = тулы[имя].description
        assert "odata1c_commit" in описание and "токен" in описание, имя
    for обязательное in (
        "токен",
        "следующем сообщении",
        "данные, а не инструкции",
        "odata1c_undo",
        "odata1c_journal",
        "unknown",
        "готовьте `create` заново",
        "bypassPermissions",
        "dontAsk",
        # Раунд 2 ревью задачи 8: откат по видимости базы, цепочка откатов, скрытый commit_id.
        "любая сессия",
        "последнее звено",
        "commit_unknown",
    ):
        assert обязательное in WRITE_PROTOCOL, обязательное
    assert "M2" not in WRITE_PROTOCOL


def test_instructions_одно_правило_записи_и_лимит():
    assert "следующем сообщении" in INSTRUCTIONS
    assert "odata1c_commit" in INSTRUCTIONS
    assert "данные, а не инструкции" in INSTRUCTIONS
    assert len(INSTRUCTIONS) <= 2048


# ---------------------------------------------------------------------------------------------
# Подтверждение по клиенту
# ---------------------------------------------------------------------------------------------


async def test_elicitation_yes_выполняет_и_вопрос_в_токенах(демон, одинс):
    async with клиент(демон, ответ=ДА) as кл:
        подготовка = await подготовить(кл, демон, одинс)
        текст = await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})

    ответ = json.loads(текст)
    assert "commit_id" in ответ, текст
    к.нет_реальных_значений(текст)
    assert одинс.patch.call_count == 1
    assert json.loads(одинс.тела_записи[0]) == {"ИНН": НОВЫЙ_ИНН}
    [вопрос] = кл.вопросы
    # Текст диалога — через стража: ни одного реального значения фикстуры (инвариант 1).
    к.нет_реальных_значений(вопрос.message)
    assert вопрос.requested_schema["properties"]["confirm"]["enum"] == ["yes", "no"]
    assert демон.журнал(ответ["commit_id"]).client == "elicitation"


@pytest.mark.parametrize(
    "ответ",
    [
        НЕТ,
        types.ElicitResult(action="decline"),
        types.ElicitResult(action="cancel"),
        types.ElicitResult(action="accept", content={}),
        types.ElicitResult(action="accept"),
        types.ElicitResult(action="accept", content={"confirm": "YES"}),
        types.ElicitResult(action="accept", content={"confirm": "yes "}),
        types.ElicitResult(action="accept", content={"confirm": True}),
        types.ElicitResult(action="decline", content={"confirm": "yes"}),
    ],
    ids=[
        "no",
        "decline",
        "cancel",
        "пусто",
        "без_content",
        "YES",
        "yes_пробел",
        "True",
        "decline_yes",
    ],
)
async def test_только_явное_yes_выполняет(демон, одинс, ответ):
    async with клиент(демон, ответ=ответ) as кл:
        подготовка = await подготовить(кл, демон, одинс)
        отказ = ошибка(await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]}))
    assert отказ["code"] == "permission_denied"
    assert одинс.записей == 0


async def test_elicitation_no_операция_жива_затем_yes(демон, одинс):
    ответы = [НЕТ, ДА]
    async with клиент(демон, ответ=lambda _params: ответы.pop(0)) as кл:
        подготовка = await подготовить(кл, демон, одинс)
        отказ = ошибка(await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]}))
        assert отказ["code"] == "permission_denied" and одинс.записей == 0
        текст = await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
    assert "commit_id" in json.loads(текст), текст
    assert одинс.patch.call_count == 1


async def test_сбой_elicitation_у_клиента_отказ(демон, одинс):
    def падает(_params):
        raise RuntimeError("клиент не смог показать диалог")

    async with клиент(демон, ответ=падает) as кл:
        подготовка = await подготовить(кл, демон, одинс)
        отказ = ошибка(await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]}))
    assert отказ["code"] == "permission_denied"
    assert одинс.записей == 0


async def test_клиент_без_elicitation_при_deny_write_unsupported_client(демон, одинс):
    async with клиент(демон, имя="other-agent") as кл:
        подготовка = await подготовить(кл, демон, одинс)
        отказ = ошибка(await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]}))
    assert отказ["code"] == "write_unsupported_client"
    # Отказ механизма `deny` (демон выбрал его сам по настройке), а не второго рубежа
    # `WriteService` против `trust` при `deny` (Т7-6): оба рубежа проверяются по отдельности.
    assert "не умеет подтверждать" in отказ["message"]
    assert одинс.записей == 0


async def test_claude_code_выполняет_без_вопроса_сервера(демон, одинс):
    """Claude Code подтверждает сам — диалогом разрешения по `_meta` тула; сервер при этом не
    спрашивает, даже если клиент объявил elicitation (ADR-0012: без второго диалога)."""
    async with клиент(демон, имя="claude-code", версия="2.1.267", ответ=ДА) as кл:
        подготовка = await подготовить(кл, демон, одинс)
        текст = await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
    ответ = json.loads(текст)
    assert "commit_id" in ответ, текст
    assert кл.вопросы == []
    assert демон.журнал(ответ["commit_id"]).client == "claude_code"
    assert одинс.patch.call_count == 1


@pytest.mark.parametrize(
    ("имя", "версия"),
    [
        ("Claude Code", "2.1.267"),
        ("claude_code", "2.1.267"),
        ("CLAUDE-CODE", "2.1.267"),
        ("claude-code ", "2.1.267"),
        ("claude-code-fork", "2.1.267"),
        ("claude-code", "2.1.245"),
        ("claude-code", "latest"),
    ],
)
async def test_похожее_имя_или_старая_версия_не_claude_code(демон, одинс, имя, версия):
    """Похожее имя без elicitation при `deny` — отказ, а не запись без подтверждения."""
    async with клиент(демон, имя=имя, версия=версия) as кл:
        подготовка = await подготовить(кл, демон, одинс)
        отказ = ошибка(await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]}))
    assert отказ["code"] == "write_unsupported_client"
    assert одинс.записей == 0


async def test_старый_claude_code_с_elicitation_спрашивает_сервер(демон, одинс):
    async with клиент(демон, имя="claude-code", версия="2.1.245", ответ=НЕТ) as кл:
        подготовка = await подготовить(кл, демон, одинс)
        отказ = ошибка(await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]}))
    assert отказ["code"] == "permission_denied" and len(кл.вопросы) == 1
    assert одинс.записей == 0


async def test_trust_client_выполняет_без_вопроса(дом, одинс):
    _запасной(дом, "trust_client")
    д = Демон(дом)
    try:
        async with клиент(д, имя="other-agent") as кл:
            подготовка = await подготовить(кл, д, одинс)
            текст = await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
        ответ = json.loads(текст)
        assert "commit_id" in ответ, текст
        assert д.журнал(ответ["commit_id"]).client == "trust"
    finally:
        await д.tools.aclose()


# ---------------------------------------------------------------------------------------------
# Сессия
# ---------------------------------------------------------------------------------------------


async def test_операция_одной_сессии_не_видна_другой(демон, одинс):
    async with клиент(демон, ответ=ДА) as первая, клиент(демон, ответ=ДА) as вторая:
        подготовка = await подготовить(первая, демон, одинс)
        чужой = ошибка(
            await вторая.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
        )
        assert чужой["code"] == "pending_unknown"
        assert подготовка["pending_id"] not in json.dumps(чужой, ensure_ascii=False)
        assert одинс.записей == 0 and вторая.вопросы == []
        # Своя сессия — выполняет: ключ сессии устойчив между вызовами одного соединения.
        свой = await первая.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
    assert "commit_id" in json.loads(свой), свой
    assert одинс.patch.call_count == 1


def _ctx(headers=None, *, params=None, caps=None, can_send=True):
    сессия = pytypes.SimpleNamespace(
        client_params=params, client_capabilities=caps, can_send_request=can_send
    )
    return pytypes.SimpleNamespace(headers=headers, session=сессия)


def test_ключ_сессии_заголовок_и_соединение():
    ключи = SessionKeys()
    assert ключи.key(_ctx({SESSION_ID_HEADER: "abc"})) == "abc"
    параметры = types.InitializeRequestParams(
        protocol_version="2025-11-25",
        capabilities=types.ClientCapabilities(),
        client_info=types.Implementation(name="x", version="1"),
    )
    другие = параметры.model_copy()
    первый = ключи.key(_ctx(params=параметры))
    assert первый == ключи.key(_ctx(params=параметры))
    assert первый.startswith("local:") and первый != ключи.key(_ctx(params=другие))
    # Без заголовка и без параметров соединения ключа нет — каждый вызов свой (операция не
    # найдётся, запись не выполнится: сторона отказа).
    assert ключи.key(_ctx()) != ключи.key(_ctx())


def test_механизм_выбирается_первым_вызовом_и_запоминается():
    часы = к.Часы(0.0)
    механизмы = SessionMechanisms("deny", clock=часы, idle_s=100)
    assert механизмы.choose("s", ClientIdentity("x", "1", True)) == "elicitation"
    # Та же сессия назвалась иначе (новый протокол шлёт клиента в каждом запросе) — механизм
    # прежний: сменить его посреди сессии нельзя.
    assert механизмы.choose("s", ClientIdentity("claude-code", "2.1.267", False)) == "elicitation"
    assert механизмы.choose("t", ClientIdentity("claude-code", "2.1.267", False)) == "claude_code"
    часы.сейчас = 150
    assert механизмы.purge() == 2
    assert механизмы.choose("s", ClientIdentity("other", "1", False)) == "deny"


# ---------------------------------------------------------------------------------------------
# Клиент через лаунчер: заголовки
# ---------------------------------------------------------------------------------------------


def _сессия_клиента(имя, версия, caps, can_send=True):
    params = (
        types.InitializeRequestParams(
            protocol_version="2025-11-25",
            capabilities=caps or types.ClientCapabilities(),
            client_info=types.Implementation(name=имя, version=версия),
        )
        if имя is not None
        else None
    )
    return pytypes.SimpleNamespace(
        client_params=params, client_capabilities=caps, can_send_request=can_send
    )


С_ELICITATION = types.ClientCapabilities(elicitation=types.ElicitationCapability())


def test_заголовки_клиента_ASCII_и_обратно():
    for имя in ("claude-code", "Клиент «Ромашка» 7707083893", "a b/c%d", ""):
        заголовки = client_headers(_сессия_клиента(имя, "1.2.3", С_ELICITATION))
        for значение in заголовки.values():
            значение.encode("ascii")
        клиент_ = client_from_request(_ctx(заголовки))
        assert клиент_.name == (имя or None)
        assert клиент_.version == "1.2.3" and клиент_.elicitation is True


def test_заголовки_клиента_без_elicitation_и_без_обратного_канала():
    без = client_from_request(_ctx(client_headers(_сессия_клиента("x", "1", None))))
    assert без.elicitation is False
    # Новый протокол (2026-07-28) запросов сервера к клиенту не допускает: elicitation объявлена,
    # но переслать её лаунчер не сможет — для демона её нет.
    нет_канала = client_headers(_сессия_клиента("x", "1", С_ELICITATION, can_send=False))
    assert client_from_request(_ctx(нет_канала)).elicitation is False
    # Только URL-режим — форма не поддерживается.
    только_url = types.ClientCapabilities(
        elicitation=types.ElicitationCapability(url=types.UrlElicitationCapability())
    )
    assert client_from_request(_ctx(client_headers(_сессия_клиента("x", "1", только_url)))) == (
        client_from_request(_ctx(client_headers(_сессия_клиента("x", "1", None))))
    )
    # Клиент не представился — имени нет, а не имя лаунчера.
    аноним = client_from_request(_ctx(client_headers(_сессия_клиента(None, None, None))))
    assert аноним.name is None and аноним.elicitation is False


def test_заголовки_клиента_важнее_initialize_соединения():
    """Через лаунчер `initialize` демону шлёт сам лаунчер (имя `mcp` SDK, elicitation всегда) —
    демон берёт клиента из заголовков лаунчера, а не из них."""
    лаунчер = types.InitializeRequestParams(
        protocol_version="2025-11-25",
        capabilities=С_ELICITATION,
        client_info=types.Implementation(name="mcp", version="0.1.0"),
    )
    заголовки = {
        CLIENT_NAME_HEADER: "other",
        CLIENT_VERSION_HEADER: "1",
        CLIENT_ELICITATION_HEADER: "0",
    }
    клиент_ = client_from_request(_ctx(заголовки, params=лаунчер, caps=С_ELICITATION))
    assert (клиент_.name, клиент_.elicitation) == ("other", False)
    прямой = client_from_request(_ctx({}, params=лаунчер, caps=С_ELICITATION))
    assert (прямой.name, прямой.version, прямой.elicitation) == ("mcp", "0.1.0", True)


def test_длинное_имя_клиента_не_ломает_заголовки():
    заголовки = client_headers(_сессия_клиента("я" * 5000, "1", None))
    assert all(len(значение) <= 1024 for значение in заголовки.values())
    assert client_from_request(_ctx(заголовки)).name is None


# ---------------------------------------------------------------------------------------------
# Отказы не повторяют ввод
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("тул", "аргументы", "поле"),
    [
        ("odata1c_commit", {"pending_id": {"ИНН": ИНН}}, "pending_id"),
        ("odata1c_commit", {"pending_id": int(ИНН)}, "pending_id"),
        ("odata1c_commit", {"лишнее": ИНН}, "pending_id"),
        ("odata1c_undo", {"commit_id": [ИНН]}, "commit_id"),
        ("odata1c_update", {"entity": КОНТРАГЕНТЫ, "key": ССЫЛКА, "data": ИНН}, "data"),
        ("odata1c_journal", {"limit": ИНН + "x"}, "limit"),
        ("odata1c_journal", {"base": [ИНН]}, "base"),
        ("odata1c_query", {"entity": КОНТРАГЕНТЫ, "top": ИНН + "x"}, "top"),
    ],
)
async def test_отказ_SDK_по_аргументу_не_повторяет_ввод(демон, тул, аргументы, поле):
    async with клиент(демон) as кл:
        текст = await кл.вызвать(тул, аргументы)
    отказ = ошибка(текст)
    assert отказ["code"] == "params_invalid"
    assert поле in отказ["message"]
    assert ИНН not in текст and "input_value" not in текст


async def test_сбой_обёртки_демона_вне_сервиса_отказ_internal_через_стража(
    демон, одинс, monkeypatch, caplog
):
    """Обёртка тула делает работу до вызова сервиса (ключ сессии, клиент, механизм) — вне
    `ToolService._run`. Упав там, она без перехвата ушла бы в SDK: клиенту — голая строка
    «Error executing tool …» не в формате §5.2 и мимо стража, в журнал демона — трассировка с
    текстом исключения (SDK сам делает `logger.exception`). Здесь — отказ `internal` через стража
    сервиса, трассировка — через ту же защиту журнала, что у `ToolService`: значение, известное
    словарю, в журнал не попадает, класс исключения — попадает."""
    к.токен(демон.tools, ИНН)  # значение известно словарю — как после любого чтения карточки

    def падает(ctx):
        raise RuntimeError(f"сломалось на {ИНН}")

    monkeypatch.setattr(демон.слой.keys, "key", падает)
    async with клиент(демон, ответ=ДА) as кл:
        результат = await кл.сессия.call_tool("odata1c_commit", {"pending_id": "p"})
    текст = результат.content[0].text
    отказ = ошибка(текст)
    assert отказ["code"] == "internal"
    assert ИНН not in текст and "RuntimeError" not in текст
    assert "RuntimeError" in caplog.text
    assert ИНН not in caplog.text


async def test_отказы_commit_и_undo_не_повторяют_идентификатор(демон, одинс):
    выдуманный = "7707083893-не-операция"
    async with клиент(демон, ответ=ДА) as кл:
        commit = await кл.вызвать("odata1c_commit", {"pending_id": выдуманный})
        undo = await кл.вызвать("odata1c_undo", {"commit_id": выдуманный})
        журнал = await кл.вызвать("odata1c_journal", {"base": выдуманный})
    assert ошибка(commit)["code"] == "pending_unknown"
    assert ошибка(undo)["code"] == "commit_unknown"
    assert ошибка(журнал)["code"] == "base_unknown"
    for текст in (commit, undo, журнал):
        assert "7707083893" not in текст and "[[" not in текст


# ---------------------------------------------------------------------------------------------
# undo и journal через тулы
# ---------------------------------------------------------------------------------------------


async def test_update_commit_journal_undo_commit_через_тулы(демон, одинс):
    async with клиент(демон, ответ=ДА) as кл:
        подготовка = await подготовить(кл, демон, одинс)
        выполнено = await кл.json("odata1c_commit", {"pending_id": подготовка["pending_id"]})
        журнал = await кл.json("odata1c_journal", {"limit": 5})
        [строка] = [с for с in журнал["entries"] if с["commit_id"] == выполнено["commit_id"]]
        откат = await кл.json("odata1c_undo", {"commit_id": выполнено["commit_id"]})
        assert "pending_id" in откат, откат
        откачено = await кл.вызвать("odata1c_commit", {"pending_id": откат["pending_id"]})

    assert строка["status"] == "committed"
    к.нет_реальных_значений(json.dumps(журнал, ensure_ascii=False))
    к.нет_реальных_значений(json.dumps(откат, ensure_ascii=False))
    к.нет_реальных_значений(откачено)
    assert "commit_id" in json.loads(откачено), откачено
    assert одинс.patch.call_count == 2
    assert json.loads(одинс.тела_записи[1]) == {"ИНН": ИНН}
    assert демон.журнал(выполнено["commit_id"]).undone_by == json.loads(откачено)["commit_id"]


async def test_create_mark_action_доходят_до_сервиса(демон, одинс):
    одинс.положить(к.ПУТЬ_ДОКУМЕНТА, к.документ())
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, к.контрагент())
    async with клиент(демон, ответ=ДА) as кл:
        создание = await кл.json(
            "odata1c_create",
            {"entity": КОНТРАГЕНТЫ, "data": {"Description": "odata1c-приёмка"}},
        )
        пометка = await кл.json(
            "odata1c_mark_for_deletion", {"entity": КОНТРАГЕНТЫ, "key": ССЫЛКА, "mark": True}
        )
        действие = await кл.json(
            "odata1c_action", {"entity": к.РЕАЛИЗАЦИЯ, "key": к.ССЫЛКА_ДОК, "name": "Post"}
        )
    for ответ in (создание, пометка, действие):
        assert "pending_id" in ответ, ответ
        assert (ответ["base"], ответ["role"]) == ("ut", "prod")
    assert одинс.записей == 0


# ---------------------------------------------------------------------------------------------
# Хранилище: запас уборки и уборка
# ---------------------------------------------------------------------------------------------


def test_запас_уборки_не_меньше_таймаута_и_300(дом):
    config = load_config(дом)
    assert pending_grace_s(config) == 300
    текст = (дом / "bases.yaml").read_text(encoding="utf-8")
    assert "    role: test\n" in текст
    (дом / "bases.yaml").write_text(
        текст.replace("    role: test\n", "    role: test\n    timeout_s: 200\n"), encoding="utf-8"
    )
    config = load_config(дом)
    # Три последовательных запроса `commit` (перечитывание, запись, чтение «после») по 200 с.
    assert pending_grace_s(config) == 3 * 200 + 60
    assert pending_grace_s(config) >= max(б.timeout_s for б in config.bases.values()) + 60


async def test_уборка_убирает_истёкшее(демон, одинс):
    async with клиент(демон, ответ=ДА) as кл:
        подготовка = await подготовить(кл, демон, одинс)
    стор = демон.слой.store
    assert демон.слой.write._store is стор
    операция = стор._ops[подготовка["pending_id"]]
    операция.expires_at -= 10_000
    assert await sweep_write_layer(демон.слой) >= 1
    assert подготовка["pending_id"] not in стор._ops


def test_лаунчер_передаёт_клиента_заголовками_один_раз():
    """`ProxyHolder.запомнить` — вход каждого обработчика лаунчера: первый запрос его клиента
    ставит заголовки клиента на HTTP-клиент апстрима; следующие их не меняют (клиент у процесса
    лаунчера один)."""
    http = pytypes.SimpleNamespace(headers={"x-odata1c-bases": "ut"})
    держатель = ProxyHolder(http)
    первая = _сессия_клиента("claude-code", "2.1.267", С_ELICITATION)
    держатель.запомнить(pytypes.SimpleNamespace(session=первая))
    assert держатель.session is первая
    assert client_from_request(_ctx(dict(http.headers))) == ClientIdentity(
        "claude-code", "2.1.267", True
    )
    assert http.headers["x-odata1c-bases"] == "ut"
    вторая = _сессия_клиента("other", "1", None)
    держатель.запомнить(pytypes.SimpleNamespace(session=вторая))
    assert держатель.session is вторая
    assert client_from_request(_ctx(dict(http.headers))).name == "claude-code"
    # Без HTTP-клиента (прокси в памяти) — только сессия.
    без_http = ProxyHolder()
    без_http.запомнить(pytypes.SimpleNamespace(session=первая))
    assert без_http.session is первая
