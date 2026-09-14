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
import urllib.parse

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
from odata1c.gate.service import policy_path, refresh_policy
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
НОВАЯ_ССЫЛКА = "0c4320aa-624f-11f0-a7a0-fa78dd2b3d42"
ПУТЬ_КОНТРАГЕНТА = f"{КОНТРАГЕНТЫ}(guid'{ССЫЛКА}')"
КИ = f"{КОНТРАГЕНТЫ}_КонтактнаяИнформация"
ИНН = "7707083893"
НОВЫЙ_ИНН = "7736050003"
НАЗВАНИЕ = "ООО Ромашка"
ТЕЛЕФОН = "+7 495 000-00-00"


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


def _путь(request: httpx.Request) -> str:
    """Путь запроса после `standard.odata/`, раскодированный: так его строит шлюз."""
    сырой = urllib.parse.unquote(request.url.raw_path.decode("ascii"))
    return сырой.split("standard.odata/", 1)[1].split("?", 1)[0]


class Одинс:
    """Поддельная 1С с состоянием: GET соблюдает `$select`, PATCH сливает тело с объектом и
    растит `DataVersion`, POST на набор создаёт объект (формы пробы P8)."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.объекты = {
            ПУТЬ_КОНТРАГЕНТА: {
                "Ref_Key": ССЫЛКА,
                "DataVersion": "AAAAAAAAAAE=",
                "DeletionMark": False,
                "Code": "000000711",
                "Description": НАЗВАНИЕ,
                "ИНН": ИНН,
            }
        }
        self.get = router.get(url__regex=r".*standard\.odata/[^?]+").mock(side_effect=self._get)
        self.patch = router.patch(url__regex=r".*").mock(side_effect=self._patch)
        self.post = router.post(url__regex=r".*").mock(side_effect=self._post)

    def _get(self, request: httpx.Request) -> httpx.Response:
        объект = self.объекты.get(_путь(request))
        if объект is None:
            return httpx.Response(
                404,
                json={"odata.error": {"code": "9", "message": {"value": "Экземпляр не найден"}}},
            )
        выбор = request.url.params.get("$select")
        поля = set(выбор.split(",")) if выбор else set(объект)
        return httpx.Response(200, json={к: з for к, з in объект.items() if к in поля})

    def _patch(self, request: httpx.Request) -> httpx.Response:
        путь = _путь(request)
        self.объекты[путь].update(json.loads(request.content))
        self.объекты[путь]["DataVersion"] = "AAAAAAAAAAI="
        return httpx.Response(200, json=self.объекты[путь])

    def _post(self, request: httpx.Request) -> httpx.Response:
        тело = {
            "Ref_Key": НОВАЯ_ССЫЛКА,
            "DataVersion": "AAAAAAAAAAE=",
            "DeletionMark": False,
            "Code": "000000042",
            **json.loads(request.content),
        }
        self.объекты[f"{_путь(request)}(guid'{НОВАЯ_ССЫЛКА}')"] = тело
        return httpx.Response(201, json=тело)

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


async def подготовить_create(среда: Среда) -> dict:
    """`create` контрагента со строкой табличной части — у неё своя сущность и свои разрешения."""
    текст = await среда.запись.create(
        SessionScope(),
        "s1",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        data={
            "Description": "ООО Новый",
            "КонтактнаяИнформация": [{"Тип": "Телефон", "Представление": ТЕЛЕФОН}],
        },
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


async def test_pending_несёт_аргументы_проверки_разрешений_подготовки(среда, одинс):
    """Перепроверка при `commit` обязана проверять ТО ЖЕ САМОЕ, что проверила подготовка, а к
    тому времени ни тела от модели, ни описаний из индекса под рукой нет — аргументы едут в
    операции. Пустые `write_rows` сделали бы построчную перепроверку тихим «всё разрешено»."""
    подготовка = await подготовить_create(среда)

    операция = await среда.стор.take(подготовка["pending_id"], "s1")

    assert операция.write_fields == ("Description", "КонтактнаяИнформация")
    assert операция.write_rows == ((КИ, ("Тип", "Представление")),)
    assert операция.write_action is None


async def test_поле_табличной_части_запрещено_после_подготовки_create_отказывает_без_POST(
    среда, одинс, дом
):
    """Решение контроллера к задаче 6: SPEC §7.1 требует перепроверить раздел `permissions`
    ЦЕЛИКОМ, а запрет поля СТРОКИ табличной части — то же правило `deny_fields`, только у
    сущности строки. Без этой перепроверки подготовленный `create` записал бы строку, которую
    свежему `create` база уже не разрешает."""
    подготовка = await подготовить_create(среда)

    переписать(
        дом / "bases.yaml",
        настройки(ещё=f"    permissions:\n      deny_fields: ['{КИ}.Представление']\n"),
    )

    ответ = await выполнить(среда, подготовка["pending_id"])
    assert ответ["error"]["code"] == "field_write_denied"
    assert одинс.записей == 0
    assert ТЕЛЕФОН not in json.dumps(ответ, ensure_ascii=False)
    assert (await среда.стор.take(подготовка["pending_id"], "s1")).status == "pending"


async def test_табличная_часть_скрыта_после_подготовки_create_отказывает_без_POST(
    среда, одинс, дом
):
    """Та же перепроверка, шаг 1: политика скрыла сущность строки после подготовки. Шапку
    `_resolve_entity` пропускает — скрыта не она."""
    подготовка = await подготовить_create(среда)
    путь = policy_path(дом, "ut")
    переписать(путь, путь.read_text(encoding="utf-8") + f"entities:\n  {КИ}: {{hide: true}}\n")

    ответ = await выполнить(среда, подготовка["pending_id"])
    assert ответ["error"]["code"] == "entity_hidden"
    assert одинс.записей == 0


async def test_create_с_табличной_частью_проходит_когда_ничего_не_запрещали(среда, одинс, дом):
    """Сторож обратного для строк: перепроверка не должна отказывать сама по себе."""
    подготовка = await подготовить_create(среда)

    ответ = await выполнить(среда, подготовка["pending_id"])
    assert "error" not in ответ, ответ
    assert одинс.post.call_count == 1


async def test_правка_не_про_разрешения_записи_не_мешает(среда, одинс, дом):
    """Сторож обратного: перепроверка не должна отказывать там, где владелец ничего не запрещал."""
    подготовка = await подготовить(среда)

    переписать(дом / "bases.yaml", настройки().replace("УТ, запись разрешена", "УТ, рабочая"))

    ответ = await выполнить(среда, подготовка["pending_id"])
    assert "error" not in ответ, ответ
    assert одинс.patch.call_count == 1
