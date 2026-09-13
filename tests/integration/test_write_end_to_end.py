"""Запись сквозь настоящий лаунчер (план M2, задача 9): stdio-клиент → `odata1c mcp` → демон
(`serve()` в этом процессе, Streamable HTTP) → поддельная 1С с состоянием (`fake_1c.py`).

Что здесь проверяется и чего не видно в памяти (`tests/unit/test_daemon_write.py`):

- клиент доходит до демона через лаунчер: `initialize` демону шлёт сам лаунчер (имя `mcp`,
  elicitation всегда), и без пересылки клиента заголовками Claude Code получал бы второй диалог, а
  клиент без elicitation — вопрос, которого не увидит;
- elicitation демона доходит до клиента лаунчера и ответ возвращается (проба P3 — теперь на
  настоящем `commit`);
- `_meta["anthropic/requiresUserInteraction"]` у `odata1c_commit` виден клиенту за лаунчером;
- `mcp-session-id` — ключ сессии: у двух лаунчеров разные сессии, операция одной не видна другой.

Живая 1С не нужна; демон владельца на 7171 не трогается — свой порт и свой домашний каталог.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys

import mcp.types as types
import pytest
from fake_1c import EDMX_ФИКСТУРА, REF_KEY, ИНН, НАЗВАНИЕ, ОбъектыЗаписи, запущенная, свободный_порт
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sse_starlette import sse

from odata1c.config.home import ensure_home
from odata1c.config.loader import load_config
from odata1c.config.writer import ensure_gate_secret
from odata1c.daemon import daemon_url, serve
from odata1c.gate.service import refresh_policy
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository

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


def _дом(tmp_path, порт_1с: int):
    home = tmp_path / "home"
    ensure_home(home)
    ensure_gate_secret(home / "daemon.yaml")
    (home / "bases.yaml").write_text(
        "default: ut\n"
        "bases:\n"
        "  ut:\n"
        "    label: УТ, поддельная 1С (сквозная запись)\n"
        f"    url: http://127.0.0.1:{порт_1с}/odata/standard.odata/\n"
        "    user: u\n"
        "    password: p\n"
        "    role: prod\n"
        "    write: true\n",
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

    # Claude Code: имя доходит до демона сквозь лаунчер, и демон не спрашивает сам — подтверждает
    # диалог разрешения клиента по `_meta` тула (ADR-0012: без второго диалога), хотя elicitation
    # клиент объявил. Возвращаем ИНН обратно, чтобы запись была видна в 1С.
    async with через_лаунчер(home, порт, имя="claude-code", версия="2.1.267", ответ=ДА) as кл:
        ответ = await кл.json(
            "odata1c_update", {"entity": КОНТРАГЕНТЫ, "key": REF_KEY, "data": {"ИНН": ИНН}}
        )
        assert "pending_id" in ответ, ответ
        текст = await кл.вызвать("odata1c_commit", {"pending_id": ответ["pending_id"]})
        assert "commit_id" in json.loads(текст), текст
        assert кл.вопросы == []
        assert объекты.записи[-1] == ("PATCH", ПУТЬ, {"ИНН": ИНН})


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
