"""`commit` перепроверяет разрешения записи по `bases.yaml` на момент коммита (SPEC §7.1,
поправка 2026-09-14, ADR-0015).

`bases.yaml` действует без перезапуска демона (§3.1), значит между подготовкой pending-операции и
`commit` владелец успевает закрыть запись — базу целиком или отдельное поле. Проверка при
подготовке отвечала на вопрос о прежнем файле; отвечать на него записью в 1С нельзя, поэтому
разрешения проверяются ещё раз, теми же аргументами, что и при подготовке, до диалога
подтверждения и до всякого запроса на запись.
"""

import json
import os
import pathlib

import httpx
import pytest
import respx
from conftest import (
    без_навигаций,
    ничего_не_скрыто,
    обеспечить_policy_yaml,
    строение_неизвестно,
)

from odata1c.cli import main
from odata1c.config.loader import load_config
from odata1c.gate.service import refresh_policy
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository
from odata1c.registry.registry import SessionScope
from odata1c.tools.service import ToolService
from odata1c.write.journal import Journal
from odata1c.write.pending import CommitLimiter, PendingStore
from odata1c.write.service import WriteService

URL_UT = "http://localhost/ut/odata/standard.odata/"
КОНТРАГЕНТЫ = "Catalog_Контрагенты"
ССЫЛКА = "a103cb54-42ee-11ec-a7a0-f10ab59a067e"
ИНН = "7707083893"
НОВЫЙ_ИНН = "7736050003"
НАЗВАНИЕ = "ООО Ромашка"


def настройки(*, write: bool = True, ещё: str = "") -> str:
    return (
        "default: ut\n"
        "bases:\n"
        "  ut:\n"
        "    label: УТ, запись разрешена\n"
        f"    url: {URL_UT}\n"
        "    user: u\n"
        "    password: p\n"
        "    role: prod\n"
        f"    write: {str(write).lower()}\n"
        f"{ещё}"
    )


def переписать(путь: pathlib.Path, текст: str) -> None:
    """Правка файла со сдвигом mtime: отметка (mtime, размер) обязана отличаться от прежней, а
    mtime на Windows грубее, чем две правки подряд в одном тесте."""
    путь.write_text(текст, encoding="utf-8")
    отметка = путь.stat().st_mtime + 10
    os.utime(путь, (отметка, отметка))


@pytest.fixture
def дом(tmp_path, edmx_ut_real):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(настройки(), encoding="utf-8")
    config = load_config(home)
    хранилище = IndexRepository(index_path(home, "ut"))
    хранилище.write(parse_edmx(edmx_ut_real))
    хранилище.close()
    refresh_policy(home, config.bases["ut"])
    обеспечить_policy_yaml(home, "ut")
    return home


class Среда:
    def __init__(self, дом: pathlib.Path, путь_журнала: pathlib.Path) -> None:
        self.tools = ToolService(load_config(дом))
        self.стор = PendingStore(600)
        self.запись = WriteService(
            self.tools, self.стор, lambda: Journal(путь_журнала), CommitLimiter()
        )


@pytest.fixture
async def среда(дом, tmp_path):
    с = Среда(дом, tmp_path / "journal.sqlite")
    yield с
    await с.tools.aclose()


class Одинс:
    """Поддельная 1С: GET отдаёт одного контрагента с соблюдением `$select`, PATCH сливает тело
    с объектом и растит `DataVersion` (так ведёт себя настоящая — проба P8)."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.объект = {
            "Ref_Key": ССЫЛКА,
            "DataVersion": "AAAAAAAAAAE=",
            "DeletionMark": False,
            "Code": "000000711",
            "Description": НАЗВАНИЕ,
            "ИНН": ИНН,
        }
        self.get = router.get(url__regex=r".*standard\.odata/[^?]+").mock(side_effect=self._get)
        self.patch = router.patch(url__regex=r".*").mock(side_effect=self._patch)
        self.post = router.post(url__regex=r".*").mock(return_value=httpx.Response(500, json={}))

    def _get(self, request: httpx.Request) -> httpx.Response:
        выбор = request.url.params.get("$select")
        поля = set(выбор.split(",")) if выбор else set(self.объект)
        return httpx.Response(200, json={к: з for к, з in self.объект.items() if к in поля})

    def _patch(self, request: httpx.Request) -> httpx.Response:
        self.объект.update(json.loads(request.content))
        self.объект["DataVersion"] = "AAAAAAAAAAI="
        return httpx.Response(200, json=self.объект)

    @property
    def записей(self) -> int:
        return self.patch.call_count + self.post.call_count


@pytest.fixture
def одинс():
    with respx.mock(assert_all_called=False) as router:
        router.get(URL_UT).mock(return_value=httpx.Response(200, json={"value": []}))
        yield Одинс(router)


def токен(tools: ToolService, значение: str, *, поле: str = "ИНН") -> str:
    """Токен, который модель увидела бы в ответе чтения, — маской гейта базы, без обращения к 1С."""
    гейт = tools._gate_for(tools._registry.get("ut", SessionScope()))
    return гейт.mask(
        {поле: значение},
        entity=КОНТРАГЕНТЫ,
        resolve=без_навигаций,
        hidden=ничего_не_скрыто,
        revealed=None,
        shape=строение_неизвестно,
    ).data[поле]


async def подготовить(среда: Среда) -> dict:
    текст = await среда.запись.update(
        SessionScope(),
        "s1",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        key=ССЫЛКА,
        data={"ИНН": токен(среда.tools, НОВЫЙ_ИНН)},
    )
    ответ = json.loads(текст)
    assert "pending_id" in ответ, текст
    return ответ


async def выполнить(среда: Среда, pending_id: str) -> dict:
    return json.loads(
        await среда.запись.commit(
            SessionScope(), "s1", pending_id, mechanism="claude_code", confirm=None
        )
    )


async def test_запись_выключена_после_подготовки_commit_отказывает_без_PATCH(среда, одинс, дом):
    подготовка = await подготовить(среда)
    чтений = одинс.get.call_count

    переписать(дом / "bases.yaml", настройки(write=False))

    ответ = await выполнить(среда, подготовка["pending_id"])
    assert ответ["error"]["code"] == "base_read_only"
    assert одинс.записей == 0 and одинс.get.call_count == чтений
    assert ИНН not in json.dumps(ответ, ensure_ascii=False)
    # Операция не выполнена и не испорчена: тот же путь, что у `entity_hidden` (И-9 задачи 7).
    assert (await среда.стор.take(подготовка["pending_id"], "s1")).status == "pending"


async def test_поле_запрещено_после_подготовки_commit_отказывает_без_PATCH(среда, одинс, дом):
    """Перепроверка идёт теми же аргументами, что и подготовка: `deny_fields` смотрит на имена
    полей тела, и перепроверка с пустым набором полей эту правку владельца пропустила бы."""
    подготовка = await подготовить(среда)

    переписать(
        дом / "bases.yaml",
        настройки(ещё=f"    permissions:\n      deny_fields: ['{КОНТРАГЕНТЫ}.ИНН']\n"),
    )

    ответ = await выполнить(среда, подготовка["pending_id"])
    assert ответ["error"]["code"] == "field_write_denied"
    assert одинс.записей == 0


async def test_правка_не_про_разрешения_записи_не_мешает(среда, одинс, дом):
    """Сторож обратного: перепроверка не должна отказывать там, где владелец ничего не запрещал."""
    подготовка = await подготовить(среда)

    переписать(дом / "bases.yaml", настройки().replace("УТ, запись разрешена", "УТ, рабочая"))

    ответ = await выполнить(среда, подготовка["pending_id"])
    assert "error" not in ответ, ответ
    assert одинс.patch.call_count == 1
