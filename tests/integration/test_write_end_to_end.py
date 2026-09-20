"""Запись сквозь настоящий лаунчер (план M2, задача 9): stdio-клиент → `odata1c mcp` → демон
(`serve()` в этом процессе, Streamable HTTP) → поддельная 1С с состоянием (`fake_1c.py`).

Что здесь проверяется и чего не видно в памяти (`tests/unit/test_daemon_write.py`):

- клиент доходит до демона через лаунчер: `initialize` демону шлёт сам лаунчер (имя `mcp`,
  elicitation всегда), и без пересылки клиента заголовками Claude Code получал бы второй диалог, а
  клиент без elicitation — вопрос, которого не увидит;
- elicitation демона доходит до клиента лаунчера и ответ возвращается (проба P3 — теперь на
  настоящем `commit`);
- `_meta["anthropic/requiresUserInteraction"]` у `odata1c_commit` виден клиенту за лаунчером;
- `mcp-session-id` — ключ сессии: у двух лаунчеров разные сессии, операция одной не видна другой;
- подпись лаунчера (Ruling 59): механизм `claude_code` получает только клиент, чьи заголовки
  подписал настоящий лаунчер ключом дома. Прямой HTTP-клиент — как `curl` из Bash модели — с
  именем `claude-code` в `initialize` или в заголовках, с подписью чужой сессии или другим ключом
  механизм не получает; демон, поднятый без ключа, не выдаёт его никому.

Живая 1С не нужна; демон владельца на 7171 не трогается — свой порт и свой домашний каталог.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys

import httpx2
import mcp.types as types
import pytest
from fake_1c import EDMX_ФИКСТУРА, REF_KEY, ИНН, НАЗВАНИЕ, ОбъектыЗаписи, запущенная, свободный_порт
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from sse_starlette import sse

from odata1c.config.home import ensure_home
from odata1c.config.loader import load_config
from odata1c.config.writer import (
    LAUNCHER_KEY_FILE,
    ensure_gate_secret,
    ensure_launcher_key,
    read_launcher_key,
)
from odata1c.daemon import (
    CLIENT_ELICITATION_HEADER,
    CLIENT_NAME_HEADER,
    CLIENT_PARENT_HEADER,
    CLIENT_SIG_HEADER,
    CLIENT_VERSION_HEADER,
    SESSION_ID_HEADER,
    client_signature,
    daemon_url,
    serve,
)
from odata1c.gate.service import refresh_policy
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository
from odata1c.launch_parent import _предки_текущего, значимый_предок
from odata1c.write.journal import Journal

ПРЕДЕЛ_СЦЕНАРИЯ_С = 90
КОНТРАГЕНТЫ = "Catalog_Контрагенты"
ПУТЬ = f"{КОНТРАГЕНТЫ}(guid'{REF_KEY}')"
НОВЫЙ_ИНН = "7736050003"

ДА = types.ElicitResult(action="accept", content={"confirm": "yes"})
НЕТ = types.ElicitResult(action="accept", content={"confirm": "no"})


def _объекты() -> ОбъектыЗаписи:
    return ОбъектыЗаписи(
        {
            ПУТЬ: {
                "Ref_Key": REF_KEY,
                "DataVersion": ОбъектыЗаписи.версия(1),
                "DeletionMark": False,
                "Predefined": False,
                "Code": "000000711",
                "Description": НАЗВАНИЕ,
                "ИНН": ИНН,
            }
        }
    )


def _значимый_предок_теста() -> str | None:
    """Первый не-шим предок процесса pytest — он же значимый предок лаунчера, которого pytest
    запустит: между ними только процессы-питоны (шимы). Тест кладёт его в `claude_code_parents`,
    чтобы лаунчер, поднятый pytest, заверял имя `claude-code` по родителю (Ruling 61) — так
    проверяется путь `claude_code`, не завися от того, чем именно запущен pytest."""
    return значимый_предок(_предки_текущего() or [])


def _дом(
    tmp_path,
    порт_1с: int,
    *,
    ключ: bool = True,
    заверить_родителя: bool = True,
    permissions: str = "",
):
    """Дом, как после `odata1c init`: с ключом лаунчера (`ключ=False` — дом, где init был до
    Ruling 59: демон, поднятый на нём, ключа не находит). `заверить_родителя=True` добавляет
    значимого предка pytest в `claude_code_parents`, чтобы лаунчер заверил `claude-code` по
    родителю (Ruling 61); `False` — родитель лаунчера (pytest) не Claude Code, имя не заверено.
    `permissions` — необязательный блок `permissions:` базы `ut` (M3b задача 7 —
    `independent_register_delete`), уже с нужным отступом, включая перевод строки в конце."""
    home = tmp_path / "home"
    ensure_home(home)
    ensure_gate_secret(home / "daemon.yaml")
    if ключ:
        ensure_launcher_key(home)
    предок = _значимый_предок_теста() if заверить_родителя else None
    if заверить_родителя:
        assert предок, (
            "значимый предок pytest не определён — путь claude_code сквозь лаунчер не проверить; "
            "запустите тесты обычным образом (например, uv run pytest)"
        )
        with (home / "daemon.yaml").open("a", encoding="utf-8") as ф:
            ф.write(f'claude_code_parents: ["{предок}"]\n')
    (home / "bases.yaml").write_text(
        "default: ut\n"
        "bases:\n"
        "  ut:\n"
        "    label: УТ, поддельная 1С (сквозная запись)\n"
        f"    url: http://127.0.0.1:{порт_1с}/odata/standard.odata/\n"
        "    user: u\n"
        "    password: p\n"
        "    role: prod\n"
        "    write: true\n" + permissions,
        encoding="utf-8",
    )
    config = load_config(home)
    хранилище = IndexRepository(index_path(home, "ut"))
    хранилище.write(parse_edmx(EDMX_ФИКСТУРА.read_bytes()))
    хранилище.close()
    refresh_policy(home, config.bases["ut"])
    return home


@contextlib.asynccontextmanager
async def _демон(home):
    """`serve()` в фоновой задаче на свободном порту; остановка — отмена задачи.

    `AppStatus.should_exit` библиотеки `sse_starlette` (ответы Streamable HTTP идут через неё) —
    флаг на весь процесс: её сторож видит `should_exit` остановленного uvicorn предыдущего теста
    и поднимает флаг, после чего КАЖДЫЙ новый SSE-ответ в процессе завершается сразу, не отправив
    ответа («SSE stream ended without a response» у лаунчера). В демоне владельца uvicorn один и
    останавливается вместе с процессом; в тестах их несколько подряд в одном процессе — флаг
    сбрасывается перед каждым демоном (проверено исполнением: без сброса второй тест файла падал
    на `initialize` примерно в каждом третьем прогоне, и флаг перед ним был поднят)."""
    sse.AppStatus.should_exit = False
    порт = свободный_порт()
    готовность = asyncio.Event()
    задача = asyncio.create_task(serve(home, port=порт, ready=готовность))
    await asyncio.wait_for(готовность.wait(), timeout=15)
    try:
        yield порт
    finally:
        задача.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await задача


class Клиент:
    def __init__(self, сессия: ClientSession, вопросы: list[str]) -> None:
        self.сессия = сессия
        self.вопросы = вопросы

    async def вызвать(self, тул: str, аргументы: dict) -> str:
        результат = await self.сессия.call_tool(тул, аргументы)
        return результат.content[0].text

    async def json(self, тул: str, аргументы: dict) -> dict:
        return json.loads(await self.вызвать(тул, аргументы))


@contextlib.asynccontextmanager
async def через_лаунчер(home, порт_демона: int, *, имя: str, версия="1.0.0", ответ=None):
    """Настоящий лаунчер отдельным процессом (`python -m odata1c mcp --url …`): клиент называет
    себя `имя`/`версия`; `ответ` — ответ на elicitation, `None` — клиент её не объявляет."""
    параметры = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odata1c", "--home", str(home), "mcp", "--url", daemon_url(порт_демона)],
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    аргументы: dict = {"client_info": types.Implementation(name=имя, version=версия)}
    вопросы: list[str] = []
    if ответ is not None:

        async def on_elicit(context, params):
            вопросы.append(params.message)
            return ответ

        аргументы["elicitation_callback"] = on_elicit
    async with (
        stdio_client(параметры) as (r, w),
        ClientSession(r, w, **аргументы) as сессия,
    ):
        await сессия.initialize()
        yield Клиент(сессия, вопросы)


async def _подготовить(кл: Клиент) -> dict:
    # Новый ИНН — литерал, как если бы его продиктовал пользователь (Ruling 47: у `update` поле
    # класса `inn` литерал принимает).
    ответ = await кл.json(
        "odata1c_update", {"entity": КОНТРАГЕНТЫ, "key": REF_KEY, "data": {"ИНН": НОВЫЙ_ИНН}}
    )
    assert "pending_id" in ответ, ответ
    assert ИНН not in json.dumps(ответ, ensure_ascii=False)
    assert НАЗВАНИЕ not in json.dumps(ответ, ensure_ascii=False)
    return ответ


def _ошибка(текст: str) -> dict:
    данные = json.loads(текст)
    assert "error" in данные, текст
    return данные["error"]


def _нет_реальных(текст: str, *значения: str) -> None:
    for значение in (ИНН, НАЗВАНИЕ, *значения):
        assert значение not in текст, f"реальное значение в ответе: {значение[:3]}…"


# ---------------------------------------------------------------------------------------------


async def test_elicitation_yes_update_commit_journal_undo_commit_сквозь_лаунчер(tmp_path):
    объекты = _объекты()
    async with запущенная(объекты=объекты) as порт_1с:
        home = _дом(tmp_path, порт_1с)
        async with _демон(home) as порт:
            await asyncio.wait_for(_сценарий_yes(home, порт, объекты), ПРЕДЕЛ_СЦЕНАРИЯ_С)


async def _сценарий_yes(home, порт: int, объекты: ОбъектыЗаписи) -> None:
    async with через_лаунчер(home, порт, имя="t9-клиент", ответ=ДА) as кл:
        тулы = {т.name: т for т in (await кл.сессия.list_tools()).tools}
        commit = тулы["odata1c_commit"]
        # `_meta` тула — сквозь лаунчер как есть (P2): по нему Claude Code спрашивает сам.
        assert commit.meta["anthropic/requiresUserInteraction"] is True
        assert commit.annotations.destructive_hint is True
        assert тулы["odata1c_journal"].annotations.read_only_hint is True

        подготовка = await _подготовить(кл)
        assert объекты.записи == []
        текст = await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
        выполнено = json.loads(текст)
        assert "commit_id" in выполнено, текст
        # Ответ «после» — маской чтения: и прежний, и новый ИНН — токенами.
        _нет_реальных(текст, НОВЫЙ_ИНН)
        assert объекты.записи == [("PATCH", ПУТЬ, {"ИНН": НОВЫЙ_ИНН})]
        # Вопрос elicitation дошёл сквозь лаунчер, и в нём нет значений из 1С.
        [вопрос] = кл.вопросы
        _нет_реальных(вопрос)

        журнал = await кл.вызвать("odata1c_journal", {"limit": 5})
        _нет_реальных(журнал, НОВЫЙ_ИНН)
        [строка] = json.loads(журнал)["entries"]
        assert (строка["commit_id"], строка["status"]) == (выполнено["commit_id"], "committed")

        откат = await кл.json("odata1c_undo", {"commit_id": выполнено["commit_id"]})
        assert "pending_id" in откат, откат
        _нет_реальных(json.dumps(откат, ensure_ascii=False), НОВЫЙ_ИНН)
        откачено = await кл.вызвать("odata1c_commit", {"pending_id": откат["pending_id"]})
        assert "commit_id" in json.loads(откачено), откачено
        _нет_реальных(откачено, НОВЫЙ_ИНН)
        assert len(кл.вопросы) == 2
        assert объекты.записи[1] == ("PATCH", ПУТЬ, {"ИНН": ИНН})


async def test_отказы_механизмы_и_сессии_сквозь_лаунчер(tmp_path):
    объекты = _объекты()
    async with запущенная(объекты=объекты) as порт_1с:
        home = _дом(tmp_path, порт_1с)
        async with _демон(home) as порт:
            await asyncio.wait_for(_сценарий_отказов(home, порт, объекты), ПРЕДЕЛ_СЦЕНАРИЯ_С)


async def _сценарий_отказов(home, порт: int, объекты: ОбъектыЗаписи) -> None:
    # Пользователь ответил «no»: отказ, операция жива (второй commit — снова вопрос, не
    # `pending_unknown`), в 1С ни одной записи.
    async with через_лаунчер(home, порт, имя="t9-клиент", ответ=НЕТ) as кл:
        подготовка = await _подготовить(кл)
        for _ in range(2):
            отказ = _ошибка(
                await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
            )
            assert отказ["code"] == "permission_denied"
        assert len(кл.вопросы) == 2 and объекты.записи == []

    # Клиент без elicitation при `write_confirm_fallback: deny` — запись недоступна. Лаунчер сам
    # elicitation умеет, но демон видит клиента, а не лаунчер.
    async with через_лаунчер(home, порт, имя="other-agent") as кл:
        подготовка = await _подготовить(кл)
        отказ = _ошибка(
            await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
        )
        assert отказ["code"] == "write_unsupported_client"
        assert объекты.записи == []

    # Две сессии (два лаунчера): операция одной не видна другой.
    async with (
        через_лаунчер(home, порт, имя="t9-клиент", ответ=ДА) as первая,
        через_лаунчер(home, порт, имя="t9-клиент", ответ=ДА) as вторая,
    ):
        подготовка = await _подготовить(первая)
        чужой = _ошибка(
            await вторая.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
        )
        assert чужой["code"] == "pending_unknown"
        assert вторая.вопросы == [] and объекты.записи == []
        свой = await первая.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
        assert "commit_id" in json.loads(свой), свой
        assert len(объекты.записи) == 1

    # Claude Code: имя доходит до демона сквозь лаунчер, подписанное его ключом (Ruling 59) и
    # заверенное по родителю (Ruling 61 — здесь дом добавил значимого предка pytest в
    # claude_code_parents), и демон не спрашивает сам: подтверждает диалог разрешения клиента по
    # `_meta` тула (ADR-0012: без второго диалога), хотя elicitation клиент объявил. Возвращаем ИНН
    # обратно, чтобы запись была видна в 1С.
    async with через_лаунчер(home, порт, имя="claude-code", версия="2.1.267", ответ=ДА) as кл:
        ответ = await кл.json(
            "odata1c_update", {"entity": КОНТРАГЕНТЫ, "key": REF_KEY, "data": {"ИНН": ИНН}}
        )
        assert "pending_id" in ответ, ответ
        текст = await кл.вызвать("odata1c_commit", {"pending_id": ответ["pending_id"]})
        assert "commit_id" in json.loads(текст), текст
        assert кл.вопросы == []
        assert объекты.записи[-1] == ("PATCH", ПУТЬ, {"ИНН": ИНН})
        assert _механизм_в_журнале(home, json.loads(текст)["commit_id"]) == "claude_code"


async def test_лаунчер_с_чужим_родителем_не_заверяет_claude_code(tmp_path):
    """И-2 / Ruling 61 сквозь настоящий лаунчер: имя `claude-code` заверяется по родителю
    лаунчера, а не по имени в `initialize`. Дом без `claude_code_parents` — родитель лаунчера
    (pytest/python) не Claude Code, — и клиент, назвавшийся `claude-code` без объявленной
    elicitation (как `curl` ревьюера в И-2), получает отказ, а не запись без диалога. Подпись
    лаунчера при этом верна: она доказывает «через лаунчер», но не «Claude Code»."""
    объекты = _объекты()
    async with запущенная(объекты=объекты) as порт_1с:
        home = _дом(tmp_path, порт_1с, заверить_родителя=False)
        async with _демон(home) as порт:

            async def сценарий() -> None:
                async with через_лаунчер(home, порт, имя="claude-code", версия="2.1.267") as кл:
                    подготовка = await _подготовить(кл)
                    отказ = _ошибка(
                        await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
                    )
                assert отказ["code"] == "write_unsupported_client"
                assert кл.вопросы == [] and объекты.записи == []

            await asyncio.wait_for(сценарий(), ПРЕДЕЛ_СЦЕНАРИЯ_С)


def _механизм_в_журнале(home, commit_id: str) -> str:
    журнал = Journal(home / "journal.sqlite")
    try:
        return журнал.get(commit_id).client
    finally:
        журнал.close()


@pytest.mark.parametrize("имя", ["Claude Code", "claude_code"])
async def test_похожее_имя_сквозь_лаунчер_не_claude_code(tmp_path, имя):
    """Похожее на Claude Code имя без elicitation при `deny` — отказ, а не запись без вопроса."""
    объекты = _объекты()
    async with запущенная(объекты=объекты) as порт_1с:
        home = _дом(tmp_path, порт_1с)
        async with _демон(home) as порт:

            async def сценарий() -> None:
                async with через_лаунчер(home, порт, имя=имя, версия="2.1.267") as кл:
                    подготовка = await _подготовить(кл)
                    отказ = _ошибка(
                        await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
                    )
                assert отказ["code"] == "write_unsupported_client"
                assert объекты.записи == []

            await asyncio.wait_for(сценарий(), ПРЕДЕЛ_СЦЕНАРИЯ_С)


# ---------------------------------------------------------------------------------------------
# Подпись лаунчера (Ruling 59)
# ---------------------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def напрямую(порт: int, *, имя: str, версия="2.1.267", заголовки=None, подпись=None):
    """Прямой HTTP-клиент демона без лаунчера — так демон видит `curl` или скрипт из Bash модели:
    имя в `initialize`, по желанию — заголовки клиента, как у лаунчера, и `подпись(sid)` —
    значение заголовка подписи для своей сессии. elicitation клиент не объявляет."""

    async def хук(request: httpx2.Request) -> None:
        if заголовки:
            request.headers.update(заголовки)
        sid = request.headers.get(SESSION_ID_HEADER)
        if sid and подпись is not None:
            request.headers[CLIENT_SIG_HEADER] = подпись(sid)

    async with (
        httpx2.AsyncClient(
            event_hooks={"request": [хук]}, timeout=httpx2.Timeout(10, read=None)
        ) as http,
        streamable_http_client(daemon_url(порт), http_client=http) as (r, w),
        ClientSession(r, w, client_info=types.Implementation(name=имя, version=версия)) as сессия,
    ):
        await сессия.initialize()
        yield Клиент(сессия, [])


CLAUDE_CODE = {
    CLIENT_NAME_HEADER: "claude-code",
    CLIENT_VERSION_HEADER: "2.1.267",
    CLIENT_ELICITATION_HEADER: "0",
    # Прямой клиент дерзко заявляет и «родитель — Claude Code» (Ruling 61): без верной подписи это
    # значение недоверенно и claude_code не даёт.
    CLIENT_PARENT_HEADER: "1",
}


async def test_прямой_клиент_без_подписи_лаунчера_не_получает_claude_code(tmp_path):
    объекты = _объекты()
    async with запущенная(объекты=объекты) as порт_1с:
        home = _дом(tmp_path, порт_1с)
        async with _демон(home) as порт:
            await asyncio.wait_for(_сценарий_подделок(home, порт, объекты), ПРЕДЕЛ_СЦЕНАРИЯ_С)


async def _сценарий_подделок(home, порт: int, объекты: ОбъектыЗаписи) -> None:
    ключ = read_launcher_key(home)
    assert ключ is not None

    def верная(sid: str) -> str:
        return client_signature(ключ, sid, "claude-code", "2.1.267", "0", "1")

    def чужим_ключом(sid: str) -> str:
        return client_signature(os.urandom(32), sid, "claude-code", "2.1.267", "0", "1")

    # Подпись, подслушанная у настоящей сессии: клиент с верной подписью своей сессии делает один
    # запрос (подготовки не нужно) — его подпись и перехватывается.
    перехвачено: list[str] = []

    def перехватить(sid: str) -> str:
        перехвачено.append(верная(sid))
        return перехвачено[-1]

    async with напрямую(порт, имя="x", заголовки=CLAUDE_CODE, подпись=перехватить) as кл:
        await кл.сессия.list_tools()
    assert перехвачено

    подделки = {
        "имя claude-code в initialize": {},
        "заголовки claude-code без подписи": {"заголовки": CLAUDE_CODE},
        "подпись чужой сессии": {"заголовки": CLAUDE_CODE, "подпись": lambda sid: перехвачено[-1]},
        "подпись другим ключом": {"заголовки": CLAUDE_CODE, "подпись": чужим_ключом},
    }
    for что, параметры in подделки.items():
        async with напрямую(порт, имя="claude-code", **параметры) as кл:
            подготовка = await _подготовить(кл)
            текст = await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
        assert _ошибка(текст)["code"] == "write_unsupported_client", что
        assert объекты.записи == [], что
        for секрет in (ключ.hex(), перехвачено[-1]):
            assert секрет not in текст, что

    # Положительный контроль: верная подпись своей сессии — механизм `claude_code`, запись без
    # вопроса демона. Без него «отказ во всех случаях» не отличался бы от сломанной проверки.
    async with напрямую(порт, имя="x", заголовки=CLAUDE_CODE, подпись=верная) as кл:
        подготовка = await _подготовить(кл)
        текст = await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
    выполнено = json.loads(текст)
    assert "commit_id" in выполнено, текст
    assert _механизм_в_журнале(home, выполнено["commit_id"]) == "claude_code"
    assert объекты.записи == [("PATCH", ПУТЬ, {"ИНН": НОВЫЙ_ИНН})]


async def _сырой_вызов(порт: int, sid: str, номер: int, тул: str, аргументы: dict) -> str:
    """`tools/call` одним POST в чужую живую сессию — без SDK-клиента и без подписи, как `curl`."""
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(30)) as http:
        ответ = await http.post(
            daemon_url(порт),
            headers={
                SESSION_ID_HEADER: sid,
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "mcp-protocol-version": "2025-11-25",
            },
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": номер,
                    "method": "tools/call",
                    "params": {"name": тул, "arguments": аргументы},
                }
            ),
        )
    assert ответ.status_code == 200, ответ.status_code
    for строка in ответ.text.splitlines():
        if строка.startswith("data:"):
            сообщение = json.loads(строка[len("data:") :])
            if сообщение.get("id") == номер:
                return сообщение["result"]["content"][0]["text"]
    raise AssertionError("ответа на вызов нет")


@contextlib.contextmanager
def _идентификаторы_сессий():
    """Идентификаторы сессий, которые объявляет SDK демона («Created new transport with session
    ID: …», логгер `mcp.server.streamable_http_manager`). До правки координатора (Р59-5) эта
    строка шла в `daemon.log` на INFO — так идентификатор и читался бы процессом модели. Тест
    берёт его своим обработчиком, подключённым уже после старта демона, чтобы не зависеть от
    уровня этого логгера в журнале демона."""
    найдено: list[str] = []

    class Перехват(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            совпадение = re.search(r"session ID: ([0-9a-f]{32})", record.getMessage())
            if совпадение:
                найдено.append(совпадение.group(1))

    логгер = logging.getLogger("mcp.server.streamable_http_manager")
    прежний_уровень = логгер.level
    обработчик = Перехват()
    логгер.addHandler(обработчик)
    логгер.setLevel(logging.INFO)
    try:
        yield найдено
    finally:
        логгер.removeHandler(обработчик)
        логгер.setLevel(прежний_уровень)


НЕПОДПИСАННЫЕ_ПОДГОТОВКИ = {
    "odata1c_create": {"entity": КОНТРАГЕНТЫ, "data": {"Description": "x"}},
    "odata1c_update": {"entity": КОНТРАГЕНТЫ, "key": REF_KEY, "data": {"ИНН": ИНН}},
    "odata1c_mark_for_deletion": {"entity": КОНТРАГЕНТЫ, "key": REF_KEY},
    "odata1c_action": {"entity": КОНТРАГЕНТЫ, "key": REF_KEY, "name": "Post"},
}


async def test_идентификатор_чужой_сессии_не_даёт_ни_записи_ни_подготовки(tmp_path):
    """Обход подписи через идентификатор сессии: SDK принимает живую сессию от любого, кто
    предъявит её `mcp-session-id` (проверено исполнением), а механизм запоминается по сессии.
    Процесс модели знает идентификатор сессии Claude Code, которая уже писала (механизм
    `claude_code`), и шлёт в неё запросы без подписи. Ruling 59 и Р59-А: в сессии Claude Code
    неподписанный запрос к любому пишущему тулу — `write_unsupported_client`, до сервиса; своей
    операции там не подготовить, операцию лаунчера не выполнить. Чтение (`journal`) отвечает."""
    объекты = _объекты()
    async with запущенная(объекты=объекты) as порт_1с:
        home = _дом(tmp_path, порт_1с)
        async with _демон(home) as порт:

            async def сценарий() -> None:
                with _идентификаторы_сессий() as сессии:
                    async with через_лаунчер(
                        home, порт, имя="claude-code", версия="2.1.267", ответ=ДА
                    ) as кл:
                        await _сценарий_чужой_сессии(кл, порт, сессии, объекты)

            await asyncio.wait_for(сценарий(), ПРЕДЕЛ_СЦЕНАРИЯ_С)


async def _сценарий_чужой_сессии(кл: Клиент, порт: int, сессии: list[str], объекты) -> None:
    подготовка = await _подготовить(кл)
    текст = await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
    выполнено = json.loads(текст)
    assert "commit_id" in выполнено, текст
    assert len(объекты.записи) == 1 and кл.вопросы == []
    [sid] = сессии

    номер = 100
    вызовы = {
        **НЕПОДПИСАННЫЕ_ПОДГОТОВКИ,
        "odata1c_undo": {"commit_id": выполнено["commit_id"]},
        "odata1c_commit": {"pending_id": "p0"},
    }
    for тул, аргументы in вызовы.items():
        номер += 1
        отказ = _ошибка(await _сырой_вызов(порт, sid, номер, тул, аргументы))
        assert отказ["code"] == "write_unsupported_client", тул
    # Операция, подготовленная лаунчером, чужим неподписанным `commit` не выполняется.
    своя = await кл.json(
        "odata1c_update", {"entity": КОНТРАГЕНТЫ, "key": REF_KEY, "data": {"ИНН": ИНН}}
    )
    отказ = _ошибка(
        await _сырой_вызов(порт, sid, 200, "odata1c_commit", {"pending_id": своя["pending_id"]})
    )
    assert отказ["code"] == "write_unsupported_client"
    assert len(объекты.записи) == 1
    # Чтение в той же сессии — как у любого клиента.
    журнал = json.loads(await _сырой_вызов(порт, sid, 201, "odata1c_journal", {"limit": 5}))
    assert "error" not in журнал, журнал
    # Сессия осталась сессией Claude Code: подписанный запрос лаунчера пишет без вопроса.
    итог = await кл.вызвать("odata1c_commit", {"pending_id": своя["pending_id"]})
    assert "commit_id" in json.loads(итог), итог
    assert len(объекты.записи) == 2 and кл.вопросы == []


async def test_демон_без_ключа_не_выдаёт_claude_code_и_предупреждает(tmp_path):
    """Демон поднят на доме без ключа (init был до Ruling 59). Лаунчер ключ создаст сам, но демон
    читает его только при старте: до перезапуска подписи проверить нечем, и настоящий Claude Code
    получает вопрос демона (elicitation), а не запись без вопроса. В журнал демона — предупреждение
    без имени файла и без ключа."""
    объекты = _объекты()
    async with запущенная(объекты=объекты) as порт_1с:
        home = _дом(tmp_path, порт_1с, ключ=False)
        async with _демон(home) as порт:

            async def сценарий() -> None:
                async with через_лаунчер(
                    home, порт, имя="claude-code", версия="2.1.267", ответ=ДА
                ) as кл:
                    подготовка = await _подготовить(кл)
                    текст = await кл.вызвать(
                        "odata1c_commit", {"pending_id": подготовка["pending_id"]}
                    )
                выполнено = json.loads(текст)
                assert "commit_id" in выполнено, текст
                assert len(кл.вопросы) == 1
                assert _механизм_в_журнале(home, выполнено["commit_id"]) == "elicitation"

            await asyncio.wait_for(сценарий(), ПРЕДЕЛ_СЦЕНАРИЯ_С)
    ключ = read_launcher_key(home)
    assert ключ is not None  # лаунчер создал ключ сам
    журнал = (home / "logs" / "daemon.log").read_text(encoding="utf-8")
    assert "ключ лаунчера" in журнал
    assert LAUNCHER_KEY_FILE not in журнал and ключ.hex() not in журнал


# ---------------------------------------------------------------------------------------------
# Независимый регистр сведений сквозь лаунчер (M3b задача 7, проект §5.4)
# ---------------------------------------------------------------------------------------------

КУРСЫ = "InformationRegister_КурсыВалют"
ВАЛЮТА_KEY = "22222222-2222-2222-2222-222222222222"
ПЕРИОД = "2026-01-01T00:00:00"
ПУТЬ_КУРСА = f"{КУРСЫ}(Period=datetime'{ПЕРИОД}',Валюта_Key=guid'{ВАЛЮТА_KEY}')"


async def test_независимый_регистр_create_update_delete_record_undo_сквозь_лаунчер(tmp_path):
    """M3b задача 7 (проект §5.4): цепочка на независимом регистре сведений (периодический —
    `Period` в ключе, живая проверка на нём не нужна — это записанная граница приёмки) сквозь
    настоящий лаунчер и демон: `create` → `commit` → повторный `create` тем же ключом
    (`record_exists`, ни одного POST в 1С) → `update` → `commit` → `undo` → `commit` →
    `delete_record` → `commit` → `undo` (воссоздание) → `commit` → `undo` исходного `create`
    (`delete_record`) → `commit`."""
    объекты = ОбъектыЗаписи({}, независимые_регистры={КУРСЫ: ["Period", "Валюта_Key"]})
    async with запущенная(объекты=объекты) as порт_1с:
        home = _дом(
            tmp_path,
            порт_1с,
            permissions="    permissions:\n      independent_register_delete: true\n",
        )
        async with _демон(home) as порт:
            await asyncio.wait_for(_сценарий_регистра(home, порт, объекты), ПРЕДЕЛ_СЦЕНАРИЯ_С)


async def _сценарий_регистра(home, порт: int, объекты: ОбъектыЗаписи) -> None:
    async with через_лаунчер(home, порт, имя="t9-клиент", ответ=ДА) as кл:
        данные = {"Period": ПЕРИОД, "Валюта_Key": ВАЛЮТА_KEY, "Курс": 91.25, "Кратность": 1}

        создание = await кл.json("odata1c_create", {"entity": КУРСЫ, "data": данные})
        assert "pending_id" in создание, создание
        коммит1 = await кл.json("odata1c_commit", {"pending_id": создание["pending_id"]})
        assert "commit_id" in коммит1, коммит1
        assert объекты.записи == [("POST", КУРСЫ, данные)]
        assert объекты.объекты[ПУТЬ_КУРСА]["Курс"] == 91.25

        # Тем же ключом второй раз — record_exists, POST в 1С не уходит.
        повтор = await кл.json("odata1c_create", {"entity": КУРСЫ, "data": данные})
        assert "error" in повтор and повтор["error"]["code"] == "record_exists"
        assert len([з for з in объекты.записи if з[0] == "POST"]) == 1

        изменение = await кл.json(
            "odata1c_update",
            {
                "entity": КУРСЫ,
                "key": {"Period": ПЕРИОД, "Валюта_Key": ВАЛЮТА_KEY},
                "data": {"Курс": 92.5},
            },
        )
        assert "pending_id" in изменение, изменение
        коммит2 = await кл.json("odata1c_commit", {"pending_id": изменение["pending_id"]})
        assert "commit_id" in коммит2, коммит2
        assert объекты.объекты[ПУТЬ_КУРСА]["Курс"] == 92.5

        # Откат update: курс возвращается к прежнему значению.
        откат_update = await кл.json("odata1c_undo", {"commit_id": коммит2["commit_id"]})
        assert "pending_id" in откат_update, откат_update
        коммит3 = await кл.json("odata1c_commit", {"pending_id": откат_update["pending_id"]})
        assert "commit_id" in коммит3, коммит3
        assert объекты.объекты[ПУТЬ_КУРСА]["Курс"] == 91.25

        # Физическое удаление записи.
        удаление = await кл.json(
            "odata1c_delete_record",
            {"entity": КУРСЫ, "key": {"Period": ПЕРИОД, "Валюта_Key": ВАЛЮТА_KEY}},
        )
        assert "pending_id" in удаление, удаление
        assert удаление["preview"]["after"] == "записи не будет"
        коммит4 = await кл.json("odata1c_commit", {"pending_id": удаление["pending_id"]})
        assert "commit_id" in коммит4, коммит4
        assert ПУТЬ_КУРСА not in объекты.объекты

        # Откат delete_record: запись создаётся заново тем же телом.
        откат_delete = await кл.json("odata1c_undo", {"commit_id": коммит4["commit_id"]})
        assert откат_delete["undo_op"] == "create"
        коммит5 = await кл.json("odata1c_commit", {"pending_id": откат_delete["pending_id"]})
        assert "commit_id" in коммит5, коммит5
        assert ПУТЬ_КУРСА in объекты.объекты and объекты.объекты[ПУТЬ_КУРСА]["Курс"] == 91.25

        # Откат исходного create — физическое удаление (запись сейчас снова есть).
        откат_create = await кл.json("odata1c_undo", {"commit_id": коммит1["commit_id"]})
        assert откат_create["undo_op"] == "delete_record"
        коммит6 = await кл.json("odata1c_commit", {"pending_id": откат_create["pending_id"]})
        assert "commit_id" in коммит6, коммит6
        assert ПУТЬ_КУРСА not in объекты.объекты

        for текст in (
            json.dumps(создание, ensure_ascii=False),
            json.dumps(коммит1, ensure_ascii=False),
            json.dumps(изменение, ensure_ascii=False),
            json.dumps(коммит2, ensure_ascii=False),
            json.dumps(удаление, ensure_ascii=False),
        ):
            assert "guard_replaced" not in текст
