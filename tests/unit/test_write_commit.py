"""`commit` (план M2, задача 7): единственное место шлюза, где меняются данные 1С.

Поддельная 1С — `respx` с состоянием (`Одинс`): GET отдаёт объект с соблюдением `$select`, PATCH
меняет его и растит `DataVersion`, POST создаёт объект или выполняет действие. Без состояния тест
не отличил бы «было» от «стало»: словарь после записи, отпечаток и журнал сверяются именно по этой
разнице. Гейт — `identifiers+names` (роль `prod` с явным `write: true`); база `idn` — уровень
`identifiers` (Ruling 52: там текст ошибки 1С идёт через `gate.error`, а не прячется целиком); база
`lim` — лимит в один коммит, чтобы проверить, что отказ пользователя квоту не тратит.

Фикстуры свои, по образцу задач 5–6: файлы тех задач параллельно правят их ревью.
"""

import asyncio
import base64
import dataclasses
import json
import re
import urllib.parse
import uuid

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
from odata1c.gate.revealed import RevealedValues
from odata1c.gate.service import policy_path, refresh_policy
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import index_path
from odata1c.index.repository import EntityDescription, IndexRepository
from odata1c.registry.registry import SessionScope
from odata1c.tools.service import ToolService
from odata1c.write import service
from odata1c.write.confirm import choose_mechanism
from odata1c.write.errors import WriteError
from odata1c.write.journal import Journal
from odata1c.write.pending import CommitLimiter, PendingStore
from odata1c.write.service import WriteService

URL_UT = "http://localhost/ut/odata/standard.odata/"
URL_IDN = "http://localhost/idn/odata/standard.odata/"
URL_LIM = "http://localhost/lim/odata/standard.odata/"

BASES_YAML = f"""
default: ut
bases:
  ut:
    label: УТ, запись разрешена
    url: {URL_UT}
    user: u
    password: p
    role: prod
    write: true
  idn:
    label: УТ, гейт без названий
    url: {URL_IDN}
    user: u
    password: p
    role: test
    write: true
    gate:
      mode: identifiers
  lim:
    label: УТ, один коммит на окно
    url: {URL_LIM}
    user: u
    password: p
    role: prod
    write: true
    permissions:
      commit_limit: 1
"""

КОНТРАГЕНТЫ = "Catalog_Контрагенты"
РЕАЛИЗАЦИЯ = "Document_РеализацияТоваровУслуг"
КУРСЫ = "InformationRegister_КурсыВалют"
ССЫЛКА = "a103cb54-42ee-11ec-a7a0-f10ab59a067e"
ССЫЛКА_ДОК = "0c4320aa-624f-11f0-a7a0-fa78dd2b3d42"
ССЫЛКА_ВАЛЮТЫ = "5a1d6a2e-42ee-11ec-a7a0-f10ab59a067e"

ИНН = "7707083893"
НОВЫЙ_ИНН = "7736050003"
ЧУЖОЙ_ИНН = "7728168971"
# ИНН с верной контрольной суммой, которого словарь фикстуры не знает: его находит только детектор.
ЕЩЁ_ИНН = "7702070139"
НАЗВАНИЕ = "ООО Ромашка"
ПОЛНОЕ_НАЗВАНИЕ = "Общество с ограниченной ответственностью «Ромашка»"
КПП = "770701001"
ТЕЛЕФОН = "+7 916 123-45-67"
ЦИФРЫ_ТЕЛЕФОНА = "79161234567"
ПОЧТА = "ivan@example.com"
АДРЕС_ДОСТАВКИ = "г. Москва, ул. Тверская, д. 7, кв. 43"
ЧУЖОЙ_АДРЕС = "г. Казань, ул. Баумана, д. 1"
ФИО = "Иванов И.И."

# Все реальные значения защищаемых полей фикстуры. Проверка «от класса данных»: каждое ищется во
# всём тексте ответа `commit`, а не только в поле, которое меняли, — «после» читается повторным GET,
# и любое его поле могло бы утечь мимо маски.
РЕАЛЬНЫЕ_ЗНАЧЕНИЯ = (
    ИНН,
    НОВЫЙ_ИНН,
    ЧУЖОЙ_ИНН,
    ЕЩЁ_ИНН,
    НАЗВАНИЕ,
    "Ромашка",
    ПОЛНОЕ_НАЗВАНИЕ,
    КПП,
    ТЕЛЕФОН,
    ЦИФРЫ_ТЕЛЕФОНА,
    ПОЧТА,
    АДРЕС_ДОСТАВКИ,
    ЧУЖОЙ_АДРЕС,
)


def _дом(tmp_path, edmx: bytes):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES_YAML, encoding="utf-8")
    config = load_config(home)
    for имя in config.bases:
        хранилище = IndexRepository(index_path(home, имя))
        хранилище.write(parse_edmx(edmx))
        хранилище.close()
        refresh_policy(home, config.bases[имя])
        обеспечить_policy_yaml(home, имя)
    return home


class Часы:
    def __init__(self, начало: float) -> None:
        self.сейчас = начало

    def __call__(self) -> float:
        return self.сейчас


class Среда:
    """`WriteService` поверх настоящего `ToolService`. Журнал — фабрикой: `commit` открывает
    `journal.sqlite` на один вызов (Windows), тест читает его отдельным соединением."""

    def __init__(
        self,
        дом,
        путь_журнала,
        *,
        лимитёр: CommitLimiter | None = None,
        запасной: str | None = None,
    ) -> None:
        self.tools = ToolService(load_config(дом))
        self.стор = PendingStore(600, clock=Часы(1000.0))
        self.путь_журнала = путь_журнала
        self.лимитёр = лимитёр or CommitLimiter(clock=Часы(1000.0))
        self.запись = WriteService(
            self.tools,
            self.стор,
            lambda: Journal(путь_журнала),
            self.лимитёр,
            clock=Часы(1_757_000_000.0),
            confirm_fallback=запасной,
        )

    def журнал(self, commit_id: str):
        if not self.путь_журнала.exists():
            return None
        журнал = Journal(self.путь_журнала)
        try:
            return журнал.get(commit_id)
        finally:
            журнал.close()


@pytest.fixture
def дом(tmp_path, edmx_ut_real):
    return _дом(tmp_path, edmx_ut_real)


@pytest.fixture
async def среда(дом, tmp_path):
    с = Среда(дом, tmp_path / "journal.sqlite")
    yield с
    await с.tools.aclose()


def _версия(номер: int) -> str:
    return base64.b64encode(номер.to_bytes(8, "big")).decode()


def _путь(request: httpx.Request) -> str:
    """Путь запроса после `standard.odata/`, раскодированный: так его строит шлюз."""
    сырой = urllib.parse.unquote(request.url.raw_path.decode("ascii"))
    return сырой.split("standard.odata/", 1)[1].split("?", 1)[0]


class Одинс:
    """Поддельная 1С с состоянием. Объекты — по пути ключа, как его строит `build_get`.

    - GET соблюдает `$select` (как настоящая 1С: невыбранного поля в ответе нет);
    - PATCH сливает тело с объектом и растит `DataVersion` (P8: растёт на любой записи) и отдаёт
      объект целиком;
    - POST на набор — создание (201, объект целиком с новым `Ref_Key`, P8), POST на `…/Post` и
      `…/Unpost` — действие (200, пустое тело, P8);
    - `отказ` — ответ 1С на запись вместо выполнения; `перед_записью` — вызов в момент
      пишущего запроса (журнал «до» и падающий посреди запроса клиент)."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.объекты: dict[str, dict] = {}
        self.отказ: httpx.Response | None = None
        self.перед_записью = None
        self.искажение: dict | None = None
        self.тела_записи: list[bytes] = []
        self.заголовки_записи: list[httpx.Headers] = []
        self._счётчик = 100
        self.get = router.get(url__regex=r".*standard\.odata/[^?]+").mock(side_effect=self._get)
        self.patch = router.patch(url__regex=r".*").mock(side_effect=self._patch)
        self.post = router.post(url__regex=r".*").mock(side_effect=self._post)
        отказ = httpx.Response(500, json={})
        self.put = router.put(url__regex=r".*").mock(return_value=отказ)
        self.delete = router.delete(url__regex=r".*").mock(return_value=отказ)

    # -- состояние ------------------------------------------------------------------------

    def положить(self, путь: str, тело: dict) -> None:
        self.объекты[путь] = dict(тело)

    def изменить_извне(self, путь: str, **поля) -> None:
        """Чужая запись между превью и `commit` (другой пользователь 1С)."""
        self.объекты[путь].update(поля)
        if "DataVersion" in self.объекты[путь]:
            self._растить(путь)

    def _растить(self, путь: str) -> None:
        self._счётчик += 1
        self.объекты[путь]["DataVersion"] = _версия(self._счётчик)

    # -- маршруты -------------------------------------------------------------------------

    def _get(self, request: httpx.Request) -> httpx.Response:
        путь = _путь(request)
        объект = self.объекты.get(путь)
        if объект is None:
            return httpx.Response(
                404,
                json={"odata.error": {"code": "9", "message": {"value": "Экземпляр не найден"}}},
            )
        выбор = request.url.params.get("$select")
        поля = set(выбор.split(",")) if выбор else set(объект)
        return httpx.Response(200, json={к: з for к, з in объект.items() if к in поля})

    def _запись(self, request: httpx.Request) -> httpx.Response | None:
        self.тела_записи.append(request.content)
        self.заголовки_записи.append(request.headers)
        if self.перед_записью is not None:
            self.перед_записью(request)
        return self.отказ

    def _patch(self, request: httpx.Request) -> httpx.Response:
        отказ = self._запись(request)
        if отказ is not None:
            return отказ
        путь = _путь(request)
        тело = json.loads(request.content)
        if self.искажение:
            тело.update(self.искажение)
        self.объекты[путь].update(тело)
        if "DataVersion" in self.объекты[путь]:
            self._растить(путь)
        return httpx.Response(200, json=self.объекты[путь])

    def _post(self, request: httpx.Request) -> httpx.Response:
        отказ = self._запись(request)
        if отказ is not None:
            return отказ
        путь = _путь(request)
        for действие, проведён in (("/Post", True), ("/Unpost", False)):
            if путь.endswith(действие):
                объект = путь[: -len(действие)]
                self.объекты[объект]["Posted"] = проведён
                self._растить(объект)
                return httpx.Response(200, content=b"")
        ссылка = str(uuid.uuid4())
        тело = {
            "Ref_Key": ссылка,
            "DataVersion": _версия(1),
            "DeletionMark": False,
            "Code": "000000042",
            **json.loads(request.content),
        }
        self.объекты[f"{путь}(guid'{ссылка}')"] = тело
        return httpx.Response(201, json=тело)

    @property
    def записей(self) -> int:
        return self.patch.call_count + self.post.call_count + self.put.call_count


@pytest.fixture
def одинс():
    with respx.mock(assert_all_called=False) as router:
        for url in (URL_UT, URL_IDN, URL_LIM):
            router.get(url).mock(return_value=httpx.Response(200, json={"value": []}))
        yield Одинс(router)


ПУТЬ_КОНТРАГЕНТА = f"{КОНТРАГЕНТЫ}(guid'{ССЫЛКА}')"
ПУТЬ_ДОКУМЕНТА = f"{РЕАЛИЗАЦИЯ}(guid'{ССЫЛКА_ДОК}')"
КЛЮЧ_КУРСА = {"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА_ВАЛЮТЫ}
ПУТЬ_КУРСА = f"{КУРСЫ}(Period=datetime'2026-01-01T00:00:00',Валюта_Key=guid'{ССЫЛКА_ВАЛЮТЫ}')"


def контрагент(**поля) -> dict:
    тело = {
        "Ref_Key": ССЫЛКА,
        "DataVersion": _версия(1),
        "DeletionMark": False,
        "Predefined": False,
        "Code": "000000711",
        "Description": НАЗВАНИЕ,
        "НаименованиеПолное": ПОЛНОЕ_НАЗВАНИЕ,
        "ИНН": ИНН,
        "КПП": КПП,
        "ДополнительнаяИнформация": "постоянный клиент",
        "КонтактнаяИнформация": [
            {"LineNumber": "1", "Тип": "Телефон", "Представление": ТЕЛЕФОН},
            {"LineNumber": "2", "Тип": "АдресЭлектроннойПочты", "Представление": ПОЧТА},
        ],
    }
    тело.update(поля)
    return тело


def документ(**поля) -> dict:
    тело = {
        "Ref_Key": ССЫЛКА_ДОК,
        "DataVersion": _версия(1),
        "DeletionMark": False,
        "Posted": False,
        "Number": "УТ-000711",
        "Date": "2026-08-26T12:00:00",
        "Комментарий": "по вх упд 711",
        "АдресДоставки": АДРЕС_ДОСТАВКИ,
    }
    тело.update(поля)
    return тело


def токен(tools: ToolService, значение: str, *, entity=КОНТРАГЕНТЫ, поле="ИНН") -> str:
    """Токен, который модель увидела бы в ответе чтения: маска гейта базы `ut`, значение ложится
    в словарь с написанием этого поля, как после настоящего `query`/`get`."""
    гейт = tools._gate_for(tools._config.bases["ut"])
    return гейт.mask(
        {поле: значение},
        entity=entity,
        resolve=без_навигаций,
        hidden=ничего_не_скрыто,
        revealed=None,
        shape=строение_неизвестно,
    ).data[поле]


def нет_реальных_значений(текст: str, *, эхо_раскрытого: bool = False) -> None:
    """`эхо_раскрытого` — в тексте ошибки 1С ранний проход заменил раскрытое значение, и
    `guard_replaced` там по замыслу (Ruling 25: в тексте ошибки ранний проход — единственная
    защита, получатель обязан видеть, что защищённое значение было). В данных маскировщик
    обязан справиться сам, и `guard_replaced` — дефект."""
    for значение in РЕАЛЬНЫЕ_ЗНАЧЕНИЯ:
        assert значение not in текст, f"реальное значение фикстуры в ответе: {значение[:3]}…"
    if not эхо_раскрытого:
        assert "guard_replaced" not in текст


def строки_словаря(tools: ToolService) -> dict[str, int]:
    соединение = tools._dictionary._connection
    таблицы = [
        имя
        for (имя,) in соединение.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        имя: соединение.execute(f'SELECT COUNT(*) FROM "{имя}"').fetchone()[0] for имя in таблицы
    }


def словарь_знает(tools: ToolService, значение: str) -> bool:
    """Есть ли написание в словаре гейта (таблица `variants` — написания значений из данных)."""
    return (
        tools._dictionary._connection.execute(
            "SELECT 1 FROM variants WHERE raw_value = ?", (значение,)
        ).fetchone()
        is not None
    )


def ошибка(текст: str) -> dict:
    данные = json.loads(текст)
    assert "error" in данные, текст
    return данные["error"]


async def изменить(среда: Среда, data: dict, *, base="ut", entity=КОНТРАГЕНТЫ, key=ССЫЛКА, s="s1"):
    текст = await среда.запись.update(
        SessionScope(), s, base=base, entity=entity, key=key, data=data
    )
    ответ = json.loads(текст)
    assert "pending_id" in ответ, текст
    return ответ


async def выполнить(
    среда: Среда, pending_id: str, *, s="s1", mechanism="claude_code", confirm=None
) -> str:
    return await среда.запись.commit(
        SessionScope(), s, pending_id, mechanism=mechanism, confirm=confirm
    )


class Подтверждение:
    """Поддельный механизм elicitation: запоминает текст и отвечает заданным."""

    def __init__(self, ответ: bool) -> None:
        self.ответ = ответ
        self.тексты: list[str] = []

    async def __call__(self, message: str) -> bool:
        self.тексты.append(message)
        return self.ответ


# ---------------------------------------------------------------------------------------------
# Сквозной сценарий: update → commit → журнал → повторный commit
# ---------------------------------------------------------------------------------------------


async def test_сквозной_update_commit_журнал_и_повтор(среда, одинс):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    новый = токен(среда.tools, НОВЫЙ_ИНН)
    подготовка = await изменить(среда, {"ИНН": новый})

    текст = await выполнить(среда, подготовка["pending_id"])

    # Ответ — от класса данных: ни одного реального значения фикстуры во всём тексте.
    нет_реальных_значений(текст)
    ответ = json.loads(текст)
    assert set(ответ) >= {"commit_id", "base", "role", "entity", "key", "op", "result", "undo"}
    assert (ответ["base"], ответ["role"], ответ["entity"]) == ("ut", "prod", КОНТРАГЕНТЫ)
    assert ответ["op"] == "update" and ответ["key"] == ССЫЛКА
    assert ответ["result"]["ИНН"] == новый
    assert ответ["result"]["Description"].startswith("[[org:")
    assert ответ["commit_id"] in ответ["undo"] and "odata1c_undo" in ответ["undo"]
    # В 1С ровно один PATCH — изменённое поле реальным значением, по пути ключа.
    assert одинс.patch.call_count == 1 and одинс.post.call_count == 0
    assert json.loads(одинс.тела_записи[0]) == {"ИНН": НОВЫЙ_ИНН}
    assert _путь(одинс.patch.calls.last.request) == ПУТЬ_КОНТРАГЕНТА
    # Журнал: «до» и «после» — реальные значения (файл владельца), с отпечатком до и после.
    строка = среда.журнал(ответ["commit_id"])
    assert строка.status == "committed" and строка.error is None
    assert строка.before["ИНН"] == ИНН and строка.after["ИНН"] == НОВЫЙ_ИНН
    assert строка.before["DataVersion"] == _версия(1)
    assert строка.after["DataVersion"] != строка.before["DataVersion"]
    assert строка.request["json"] == {"ИНН": НОВЫЙ_ИНН}
    assert строка.key == {"Ref_Key": ССЫЛКА} and строка.client == "claude_code"
    assert строка.committed_at is not None

    # Повторный commit — тот же ответ, без второго PATCH (идемпотентность, инвариант 2).
    повтор = await выполнить(среда, подготовка["pending_id"])
    assert повтор == текст
    assert одинс.patch.call_count == 1


async def test_журнал_started_записан_до_запроса(среда, одинс):
    """Запись «до» — строго до запроса: клиент падает посреди запроса, а строка журнала к этому
    моменту уже есть со статусом `started`. Исход неизвестен (запрос мог дойти до 1С), и повторный
    `commit` запрос не повторяет."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    увидели = []

    def посреди(_request):
        увидели.append(среда.журнал(операция.commit_id))
        raise httpx.ConnectError("обрыв посреди запроса")

    одинс.перед_записью = посреди

    текст = await выполнить(среда, подготовка["pending_id"])

    [строка_в_момент_запроса] = увидели
    assert строка_в_момент_запроса is not None
    assert строка_в_момент_запроса.status == "started"
    assert строка_в_момент_запроса.before["ИНН"] == ИНН
    отказ = ошибка(текст)
    assert отказ["code"] == "commit_outcome_unknown" and "неизвест" in отказ["message"]
    assert среда.журнал(операция.commit_id).status == "unknown"
    assert await выполнить(среда, подготовка["pending_id"]) == текст
    assert одинс.patch.call_count == 1


async def test_параллельные_commit_одного_pending_id_один_PATCH(среда, одинс):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    # Поддельный транспорт `respx` не отдаёт управление циклу событий, и без паузы первый commit
    # прошёл бы целиком раньше, чем второй начался: тест не видел бы гонки вовсе. Пауза в PATCH —
    # точка, где настоящая сеть отдаёт управление.
    клиент = среда.tools._client_for(среда.tools._config.bases["ut"])
    настоящий_patch = клиент.patch

    async def с_паузой(path, json, *, scrub=None):
        await asyncio.sleep(0.05)
        return await настоящий_patch(path, json, scrub=scrub)

    клиент.patch = с_паузой

    первый, второй = await asyncio.gather(
        выполнить(среда, подготовка["pending_id"]), выполнить(среда, подготовка["pending_id"])
    )

    assert первый == второй
    assert "commit_id" in json.loads(первый)
    assert одинс.patch.call_count == 1


def _следить_за_стражем(среда: Среда, monkeypatch, base: str = "ut") -> list[str]:
    """Подменяет `finish` гейта базы обёрткой, которая запоминает всё, что прошло стража: так
    видно, что страховочный ответ отмены — выход стража, а не текст мимо него (инвариант 1)."""
    гейт = среда.tools._gate_for(среда.tools._config.bases[base])
    настоящий = гейт.finish
    прошли: list[str] = []

    def finish(конверт, revealed=None):
        текст = настоящий(конверт, revealed)
        прошли.append(текст)
        return текст

    monkeypatch.setattr(гейт, "finish", finish)
    return прошли


async def test_отмена_посреди_запроса_операция_больше_не_pending(среда, одинс, monkeypatch):
    """Клиент отключился, задача `commit` отменена, пока запрос в 1С в пути: `_run` отмену не
    ловит, и без `finally` операция осталась бы `pending` при отправленном запросе — повторный
    `commit` записал бы второй раз. Повтор отвечает «прерван, исход в журнале», журнал — `started`
    (честно: исход неизвестен)."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    клиент = среда.tools._client_for(среда.tools._config.bases["ut"])
    в_пути = asyncio.Event()
    вызовов = []

    async def зависает(path, json, *, scrub=None):
        вызовов.append(path)
        в_пути.set()
        await asyncio.Event().wait()

    клиент.patch = зависает
    прошли_стража = _следить_за_стражем(среда, monkeypatch)
    # Ruling 40: commit пришёл под конец срока подготовки (599 из 600 с) — окно идемпотентности
    # обязано отсчитываться от выполнения, и на пути отмены тоже.
    среда.стор._clock.сейчас += 599
    задача = asyncio.create_task(выполнить(среда, подготовка["pending_id"]))
    await в_пути.wait()
    задача.cancel()
    with pytest.raises(asyncio.CancelledError):
        await задача

    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    assert операция.status == "failed"
    среда.стор._clock.сейчас += 300  # за исходным сроком, внутри продлённого
    assert операция.result in прошли_стража
    повтор = ошибка(await выполнить(среда, подготовка["pending_id"]))
    assert повтор["code"] == "commit_outcome_unknown"
    assert операция.commit_id in повтор["message"] and "неизвест" in повтор["message"]
    assert len(вызовов) == 1
    assert среда.журнал(операция.commit_id).status == "started"


async def test_И4_отмена_без_страховки_тот_же_код_неизвестного_исхода(среда, одинс, monkeypatch):
    """И-4 итогового ревью M2: запасной ответ `finally` в `commit` (страховка не собралась —
    упал страж) — тот же код неизвестного исхода, что у таймаута и отмены, а не `internal` с
    нефиксированным текстом (SPEC §5.2: у `internal` сообщение фиксированное)."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    клиент = среда.tools._client_for(среда.tools._config.bases["ut"])
    в_пути = asyncio.Event()

    async def зависает(path, json, *, scrub=None):
        в_пути.set()
        await asyncio.Event().wait()

    клиент.patch = зависает
    monkeypatch.setattr("odata1c.write.service._через_стража", lambda *_a, **_k: None)
    задача = asyncio.create_task(выполнить(среда, подготовка["pending_id"]))
    await в_пути.wait()
    задача.cancel()
    with pytest.raises(asyncio.CancelledError):
        await задача

    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    повтор = ошибка(await выполнить(среда, подготовка["pending_id"]))
    assert повтор["code"] == "commit_outcome_unknown"
    assert операция.commit_id in повтор["message"] and "не повтор" in повтор["hint"]


async def test_замок_не_держит_commit_другой_операции_во_время_подтверждения(среда, одинс):
    """Замок — на `pending_id`: пока пользователь думает над операцией A, операция B другой
    записи выполняется, а не ждёт диалога A."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    одинс.положить(ПУТЬ_ДОКУМЕНТА, документ())
    a = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    b = await изменить(среда, {"Комментарий": "odata1c-приёмка"}, entity=РЕАЛИЗАЦИЯ, key=ССЫЛКА_ДОК)
    отпустить = asyncio.Event()
    спросили = asyncio.Event()

    async def думает(message: str) -> bool:
        спросили.set()
        await отпустить.wait()
        return True

    задача_a = asyncio.create_task(
        выполнить(среда, a["pending_id"], mechanism="elicitation", confirm=думает)
    )
    await спросили.wait()
    текст_b = await asyncio.wait_for(выполнить(среда, b["pending_id"]), timeout=5)
    assert "commit_id" in json.loads(текст_b)
    assert not задача_a.done()
    отпустить.set()
    assert "commit_id" in json.loads(await задача_a)
    assert одинс.patch.call_count == 2


# ---------------------------------------------------------------------------------------------
# Отпечаток: DataVersion и Ruling 55 (запись регистра)
# ---------------------------------------------------------------------------------------------


async def test_изменённый_DataVersion_pending_stale_без_записи(среда, одинс):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    одинс.изменить_извне(ПУТЬ_КОНТРАГЕНТА, ДополнительнаяИнформация="правка другого пользователя")

    текст = await выполнить(среда, подготовка["pending_id"])

    отказ = ошибка(текст)
    assert отказ["code"] == "pending_stale"
    assert "заново" in отказ["hint"]
    assert одинс.записей == 0
    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    assert среда.журнал(операция.commit_id) is None
    # Устаревание окончательно (переподготовка дешева): повтор — тот же ответ без обращений.
    чтений = одинс.get.call_count
    assert await выполнить(среда, подготовка["pending_id"]) == текст
    assert одинс.get.call_count == чтений


async def test_нет_отпечатка_у_операции_отказ_а_не_запись_вслепую(среда, одинс):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    операция.data_version = None

    отказ = ошибка(await выполнить(среда, подготовка["pending_id"]))

    assert отказ["code"] == "pending_stale" and одинс.записей == 0


def _описание_записи(*имена: str, версия: bool = False) -> EntityDescription:
    поля = [{"name": имя, "edm_type": "Edm.String"} for имя in имена]
    if версия:
        поля.append({"name": "DataVersion", "edm_type": "Edm.String"})
    return EntityDescription(
        name="InformationRegister_Проба",
        kind="InformationRegister",
        russian_kind="РегистрСведений",
        parent_entity=None,
        is_tabular_part=False,
        is_records=False,
        is_virtual=False,
        virtual_kind=None,
        key_fields=["Period", "Валюта_Key"],
        description_field=None,
        fields=поля,
        children=[],
        actions=[],
        members=[],
        navigations={},
        is_independent_register=True,
    )


def test_Ruling_55_отпечаток_записи_регистра_функцией():
    """Механика Ruling 55 на уровне функции (Ruling 60 закрыл запись регистров, и сквозной тест
    `commit` записи регистра больше не собрать подготовкой). У записи регистра нет `DataVersion`:
    отпечаток — SHA-256 канонического JSON полей записи без служебных. Та же запись — тот же
    отпечаток при другом порядке ключей; изменилось поле, которого тело не касается, — другой."""
    описание = _описание_записи("Period", "Валюта_Key", "Курс", "Кратность")
    запись_ = {"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА_ВАЛЮТЫ, "Кратность": 1}
    набор = RevealedValues()

    отпечаток = service._отпечаток(описание, {**запись_, "Курс": 90.5}, набор)
    переставлено = {"Курс": 90.5, "odata.metadata": "…", **запись_}

    assert отпечаток.startswith("sha256:") and len(отпечаток) == 71
    assert service._отпечаток(описание, переставлено, набор) == отпечаток
    assert service._отпечаток(описание, {**запись_, "Курс": 90.5, "Кратность": 10}, набор) != (
        отпечаток
    )
    # Сущность с `DataVersion` — отпечаток и есть `DataVersion`.
    с_версией = _описание_записи("Description", версия=True)
    assert service._отпечаток(с_версией, {"DataVersion": "AAAA"}, набор) == "AAAA"


def test_Ruling_55_отпечаток_по_развёрнутым_значениям_раннего_прохода():
    """Строка, переписанная ранним проходом (`ScrubbedText`), в отпечаток идёт исходным
    значением: иначе строки раннего прохода подготовки и `commit` дали бы разный хэш при
    неизменной записи."""
    описание = _описание_записи("ИНН", "Комментарий")
    набор = RevealedValues()
    исходная = {"ИНН": ИНН, "Комментарий": f"ИНН {ИНН} проверен"}
    переписанная = {
        "ИНН": набор.scrubbed("[[inn:ABCDEFGHJK]]", original=ИНН, hits=1),
        "Комментарий": набор.scrubbed(
            "ИНН [[inn:ABCDEFGHJK]] проверен", original=исходная["Комментарий"], hits=1
        ),
    }

    assert service._отпечаток(описание, переписанная, набор) == service._отпечаток(
        описание, исходная, набор
    )


def test_ключ_созданного_из_ответа_POST():
    """Ключ созданного объекта из ответа POST: у записи регистра — поля ключа с исходными
    значениями строк раннего прохода; поля ключа нет — `None`; `Ref_Key` не формы GUID — `None`
    (путь перечитывания не строится из чего попало)."""
    набор = RevealedValues()
    регистр = _описание_записи("Period", "Валюта_Key", "Курс")
    ответ = {
        "Period": "2026-01-01T00:00:00",
        "Валюта_Key": набор.scrubbed("[[inn:ABCDEFGHJK]]", original=ССЫЛКА_ВАЛЮТЫ, hits=1),
        "Курс": 91.25,
    }
    справочник = dataclasses.replace(регистр, key_fields=["Ref_Key"])

    assert service._ключ_созданного(регистр, ответ, набор) == {
        "Period": "2026-01-01T00:00:00",
        "Валюта_Key": ССЫЛКА_ВАЛЮТЫ,
    }
    assert service._ключ_созданного(регистр, {"Period": "2026-01-01T00:00:00"}, набор) is None
    assert service._ключ_созданного(справочник, {"Ref_Key": "не-guid"}, набор) is None
    assert service._ключ_созданного(справочник, {"Ref_Key": ССЫЛКА}, набор) == {"Ref_Key": ССЫЛКА}


# Синтетический независимый регистр с ключом класса `inn` (как в задаче 5, M-3): ключ приходит
# токеном, и ранний проход переписывает его во всех строках ответа GET. Отпечаток обязан
# считаться по развёрнутым значениям — иначе строки раннего прохода подготовки и commit дали бы
# разные хэши при неизменной записи.
РЕГИСТР_ИНН = "InformationRegister_ПроверкиИНН"
_ТИП_РЕГИСТРА_ИНН = f"""<EntityType Name="{РЕГИСТР_ИНН}">
        <Key>
          <PropertyRef Name="ИНН"/>
        </Key>
        <Property Name="ИНН" Type="Edm.String" Nullable="false"/>
        <Property Name="Комментарий" Type="Edm.String" Nullable="true"/>
      </EntityType>
      """
_НАБОР_РЕГИСТРА_ИНН = (
    f'<EntitySet Name="{РЕГИСТР_ИНН}" EntityType="StandardODATA.{РЕГИСТР_ИНН}"/>\n        '
)


@pytest.fixture
async def среда_синт(tmp_path, edmx_ut_real):
    текст = edmx_ut_real.decode("utf-8")
    якорь_типа = f'<EntityType Name="{КУРСЫ}">'
    якорь_набора = f'<EntitySet Name="{КУРСЫ}"'
    assert якорь_типа in текст and якорь_набора in текст
    текст = текст.replace(якорь_типа, _ТИП_РЕГИСТРА_ИНН + якорь_типа, 1)
    текст = текст.replace(якорь_набора, _НАБОР_РЕГИСТРА_ИНН + якорь_набора, 1)
    с = Среда(_дом(tmp_path, текст.encode("utf-8")), tmp_path / "journal.sqlite")
    yield с
    await с.tools.aclose()


# ---------------------------------------------------------------------------------------------
# Подтверждение по механизму клиента
# ---------------------------------------------------------------------------------------------


async def test_deny_write_unsupported_client_без_записи_и_без_квоты(дом, tmp_path, одинс):
    среда = Среда(дом, tmp_path / "journal.sqlite")
    try:
        одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
        одинс.положить(ПУТЬ_ДОКУМЕНТА, документ())
        a = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)}, base="lim")
        b = await изменить(
            среда, {"Комментарий": "x"}, base="lim", entity=РЕАЛИЗАЦИЯ, key=ССЫЛКА_ДОК
        )

        отказ = ошибка(await выполнить(среда, a["pending_id"], mechanism="deny"))

        assert отказ["code"] == "write_unsupported_client"
        assert "write_confirm_fallback" in отказ["hint"]
        assert одинс.записей == 0
        assert (await среда.стор.take(a["pending_id"], "s1")).status == "pending"
        # Квота `lim` — один коммит: отказ её не тронул, коммит b проходит.
        assert "commit_id" in json.loads(await выполнить(среда, b["pending_id"]))
    finally:
        await среда.tools.aclose()


async def test_elicitation_no_отказ_операция_жива_квота_цела_затем_yes(дом, tmp_path, одинс):
    среда = Среда(дом, tmp_path / "journal.sqlite")
    try:
        одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
        подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)}, base="lim")
        нет = Подтверждение(False)

        отказ = ошибка(
            await выполнить(среда, подготовка["pending_id"], mechanism="elicitation", confirm=нет)
        )

        assert отказ["code"] == "permission_denied" and "отклонил" in отказ["message"]
        assert len(нет.тексты) == 1 and одинс.записей == 0
        assert (await среда.стор.take(подготовка["pending_id"], "s1")).status == "pending"
        # Лимит `lim` — один коммит в окне: будь отказ списан, «yes» получил бы commit_limit.
        да = Подтверждение(True)
        текст = await выполнить(
            среда, подготовка["pending_id"], mechanism="elicitation", confirm=да
        )
        assert "commit_id" in json.loads(текст), текст
        assert одинс.patch.call_count == 1
    finally:
        await среда.tools.aclose()


async def test_текст_подтверждения_в_токенах_через_стража_словарь_не_тронут(среда, одинс):
    """Текст elicitation строится из `PendingOp.preview` до стража, поэтому он проходит
    `gate.finish_text`. Литерал модели в поле защищаемого класса там уже пометкой класса
    (Ruling 62): известный словарю чужой ИНН не повторяется ни литералом, ни токеном — по тексту
    не узнать, знал ли его шлюз. Реальных значений фикстуры нет; словарь не меняется
    (подтверждение — не данные 1С)."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    чужой = токен(среда.tools, ЧУЖОЙ_ИНН)
    подготовка = await изменить(среда, {"ИНН": ЧУЖОЙ_ИНН})
    до = строки_словаря(среда.tools)
    нет = Подтверждение(False)

    await выполнить(среда, подготовка["pending_id"], mechanism="elicitation", confirm=нет)

    [текст] = нет.тексты
    нет_реальных_значений(текст)
    было = подготовка["preview"][0]["before"]
    assert было.startswith("[[inn:") and было in текст
    assert чужой not in текст and ЧУЖОЙ_ИНН not in текст
    assert "значение из запроса (класс inn)" in текст
    assert КОНТРАГЕНТЫ in текст and "ut" in текст
    assert подготовка["object"]["Description"] in текст
    assert строки_словаря(среда.tools) == до


async def test_срок_истёк_пока_пользователь_думал_отказ_без_записи(среда, одинс):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})

    async def долго_думает(message: str) -> bool:
        среда.стор._clock.сейчас += 601
        return True

    отказ = ошибка(
        await выполнить(
            среда, подготовка["pending_id"], mechanism="elicitation", confirm=долго_думает
        )
    )

    assert отказ["code"] == "pending_expired" and одинс.записей == 0
    assert подготовка["pending_id"] not in json.dumps(отказ, ensure_ascii=False)


async def test_elicitation_без_confirm_внутренняя_ошибка(среда, одинс):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})

    отказ = ошибка(await выполнить(среда, подготовка["pending_id"], mechanism="elicitation"))

    assert отказ["code"] == "internal" and одинс.записей == 0


async def test_claude_code_выполняет_без_вопроса_сервера(среда, одинс):
    """У Claude Code подтверждает клиент диалогом разрешения по `_meta` тула (задача 9); сам
    `commit` ничего не спрашивает."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    спросить = Подтверждение(False)

    текст = await выполнить(
        среда, подготовка["pending_id"], mechanism="claude_code", confirm=спросить
    )

    assert "commit_id" in json.loads(текст) and спросить.тексты == []
    assert среда.журнал(json.loads(текст)["commit_id"]).client == "claude_code"


@pytest.mark.parametrize(
    ("имя", "версия", "elicitation", "запасной", "механизм"),
    [
        ("claude-code", "2.1.267", True, "deny", "claude_code"),
        ("claude-code", "2.1.246", False, "trust_client", "claude_code"),
        ("claude-code", "3.0.0", False, "deny", "claude_code"),
        # Задача 9: имя сравнивается точно — похожее имя другого клиента не получает механизм
        # «подтверждает клиент» (иначе запись без единого подтверждения).
        ("Claude Code", "2.1.267", False, "deny", "deny"),
        ("claude_code", "2.1.267", False, "deny", "deny"),
        ("CLAUDE-CODE", "2.1.267", True, "deny", "elicitation"),
        (" claude-code", "2.1.267", False, "deny", "deny"),
        ("claude-code ", "2.1.267", False, "deny", "deny"),
        ("claude-code-fork", "2.1.267", False, "deny", "deny"),
        # Версия ниже 2.1.246 (там «не спрашивать больше» ещё было) и нераспознанная — обычный
        # клиент: elicitation или запасной механизм, но не `claude_code`.
        ("claude-code", "2.1.245", True, "deny", "elicitation"),
        ("claude-code", "2.1.245", False, "deny", "deny"),
        ("claude-code", None, False, "deny", "deny"),
        ("claude-code", "2.1.300-rc1", False, "deny", "deny"),
        ("claude-code", "latest", True, "deny", "elicitation"),
        ("other-agent", "1.0.0", True, "deny", "elicitation"),
        ("other-agent", "1.0.0", False, "deny", "deny"),
        (None, None, False, "trust_client", "trust"),
        ("", None, True, "deny", "elicitation"),
        ("other-agent", "1.0.0", False, "что-то", "deny"),
    ],
)
def test_choose_mechanism(имя, версия, elicitation, запасной, механизм):
    assert choose_mechanism(имя, версия, elicitation, запасной) == механизм


# ---------------------------------------------------------------------------------------------
# Лимит коммитов
# ---------------------------------------------------------------------------------------------


async def test_лимит_prod_21й_commit_в_окне_commit_limit(среда, одинс):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    операции = []
    for номер in range(21):
        # Каждая следующая операция готовится после предыдущего коммита: отпечаток — текущий.
        операции.append(await изменить(среда, {"ДополнительнаяИнформация": f"правка {номер}"}))
        if номер < 20:
            ответ = json.loads(await выполнить(среда, операции[-1]["pending_id"]))
            assert "commit_id" in ответ

    отказ = ошибка(await выполнить(среда, операции[-1]["pending_id"]))

    assert отказ["code"] == "commit_limit"
    assert одинс.patch.call_count == 20
    assert (await среда.стор.take(операции[-1]["pending_id"], "s1")).status == "pending"


# ---------------------------------------------------------------------------------------------
# Ошибка 1С на commit (Ruling 52, 48)
# ---------------------------------------------------------------------------------------------


def _ошибка_1с(status: int, код: str, текст: str) -> httpx.Response:
    return httpx.Response(
        status, json={"odata.error": {"code": код, "message": {"lang": "ru", "value": текст}}}
    )


async def test_Ruling_52_на_уровне_названий_текст_ошибки_1С_не_отдаётся(среда, одинс):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    # Название уже в словаре (подготовка прочитала представление объекта) — даже его токена в
    # ответе быть не должно, не только открытого текста; ФИО словарь не знает вовсе.
    текст_1с = f"Объект заблокирован пользователем {ФИО}: контрагент {НАЗВАНИЕ}"
    одинс.отказ = _ошибка_1с(500, "-1", текст_1с)

    текст = await выполнить(среда, подготовка["pending_id"])

    отказ = ошибка(текст)
    assert отказ["code"] == "odata_error"
    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    assert "HTTP 500" in отказ["message"] and "-1" in отказ["message"]
    assert операция.commit_id in отказ["message"] and "журнал" in отказ["message"]
    for запрещено in (ФИО, "Иванов", НАЗВАНИЕ, "Ромашка", "заблокирован", "[[org:", "[[person:"):
        assert запрещено not in текст, запрещено
    нет_реальных_значений(текст)
    строка = среда.журнал(операция.commit_id)
    assert строка.status == "failed" and строка.after is None
    assert ФИО in строка.error and НАЗВАНИЕ in строка.error and "500" in строка.error
    # Повтор — прежний ответ, без второго запроса.
    assert await выполнить(среда, подготовка["pending_id"]) == текст
    assert одинс.patch.call_count == 1


async def test_ошибка_1С_на_уровне_identifiers_эхо_токеном_в_журнале_реальный_текст(среда, одинс):
    """P7/P8: 1С повторяет в ошибке переданное значение. На уровне без названий текст идёт через
    `gate.error`: раскрытый токеном ИНН возвращается тем же токеном (ранний проход по набору
    подготовки), ИНН, которого словарь не знает, — токеном детектора без записи в словарь
    (Ruling 48). Журнал — реальный текст целиком."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    новый = токен(среда.tools, НОВЫЙ_ИНН)
    подготовка = await изменить(среда, {"ИНН": новый}, base="idn")
    текст_1с = (
        f"Не удалось разобрать строку '{НОВЫЙ_ИНН}' как значение типа Edm.Guid! "
        f"Контрагент с ИНН {ЕЩЁ_ИНН} уже есть"
    )
    одинс.отказ = _ошибка_1с(400, "1", текст_1с)
    до = строки_словаря(среда.tools)

    текст = await выполнить(среда, подготовка["pending_id"])

    отказ = ошибка(текст)
    assert отказ["code"] == "odata_error" and "HTTP 400" in отказ["message"]
    assert "Не удалось разобрать строку" in отказ["message"]
    assert новый in отказ["message"]
    assert отказ["message"].count("[[inn:") == 2
    нет_реальных_значений(текст, эхо_раскрытого=True)
    assert строки_словаря(среда.tools) == до
    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    строка = среда.журнал(операция.commit_id)
    assert строка.status == "failed"
    assert НОВЫЙ_ИНН in строка.error and ЕЩЁ_ИНН in строка.error and "HTTP 400" in строка.error


async def test_эхо_раскрытого_адреса_в_ошибке_1С_закрыто_набором_подготовки(среда, одинс):
    """Раскрытое при подготовке значение уходит в 1С на `commit` — другом вызове тула. Ранний
    проход `commit` обязан знать это раскрытое: у адреса нет детектора, и эхо «…'<адрес>'…» в
    ошибке 1С на уровне `identifiers` иначе ушло бы модели открытым."""
    гейт = среда.tools._gate_for(среда.tools._config.bases["idn"])
    repo = среда.tools._open_index(среда.tools._config.bases["idn"])
    try:
        класс = гейт.field_class(РЕАЛИЗАЦИЯ, "АдресДоставки", shape=среда.tools._строение(repo))
    finally:
        repo.close()
    assert класс == "addr"
    одинс.положить(ПУТЬ_ДОКУМЕНТА, документ())
    адрес = токен(среда.tools, ЧУЖОЙ_АДРЕС, entity=РЕАЛИЗАЦИЯ, поле="АдресДоставки")
    подготовка = await изменить(
        среда, {"АдресДоставки": адрес}, base="idn", entity=РЕАЛИЗАЦИЯ, key=ССЫЛКА_ДОК
    )
    одинс.отказ = _ошибка_1с(500, "-1", f"Адрес '{ЧУЖОЙ_АДРЕС}' не прошёл проверку")

    текст = await выполнить(среда, подготовка["pending_id"])

    assert ошибка(текст)["code"] == "odata_error"
    assert ЧУЖОЙ_АДРЕС not in текст and "Баумана" not in текст
    assert json.loads(одинс.тела_записи[0]) == {"АдресДоставки": ЧУЖОЙ_АДРЕС}


async def test_И9_сущность_скрыта_после_подготовки_commit_отказ_без_записи(дом, среда, одинс):
    """И-9 итогового ревью M2: `_выполнить` проверяет скрытость повторно (`_resolve_entity`):
    политика, скрывшая сущность после подготовки, отказывает `entity_hidden` до диалога, чтения
    и записи. Мутант ревьюера (`_resolve_entity` → `describe`) выполнял PATCH и отдавал в ответе
    `commit` данные скрытой сущности; у отката аналог — Н8-2."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    чтений = одинс.get.call_count
    путь = policy_path(дом, "ut")
    путь.write_text(
        путь.read_text(encoding="utf-8") + f"entities:\n  {КОНТРАГЕНТЫ}: {{hide: true}}\n",
        encoding="utf-8",
    )
    нет = Подтверждение(False)

    текст = await выполнить(среда, подготовка["pending_id"], mechanism="elicitation", confirm=нет)

    отказ = ошибка(текст)
    assert отказ["code"] == "entity_hidden", отказ
    assert одинс.записей == 0 and одинс.get.call_count == чтений
    assert not нет.тексты
    assert ИНН not in текст and НОВЫЙ_ИНН not in текст
    assert (await среда.стор.take(подготовка["pending_id"], "s1")).status == "pending"


async def test_таймаут_записи_исход_неизвестен_повтор_не_пишет(среда, одинс):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})

    def таймаут(_request):
        raise httpx.ReadTimeout("1С думает")

    одинс.перед_записью = таймаут

    текст = await выполнить(среда, подготовка["pending_id"])

    отказ = ошибка(текст)
    assert отказ["code"] == "commit_outcome_unknown" and "неизвест" in отказ["message"]
    assert "таймаут" in отказ["message"]
    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    assert среда.журнал(операция.commit_id).status == "unknown"
    assert await выполнить(среда, подготовка["pending_id"]) == текст
    assert одинс.patch.call_count == 1


# ---------------------------------------------------------------------------------------------
# Словарь пополняется только после выполненной записи (решение 13)
# ---------------------------------------------------------------------------------------------


async def test_словарь_узнаёт_новое_значение_после_успешного_commit(среда, одинс):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": НОВЫЙ_ИНН})
    assert not словарь_знает(среда.tools, НОВЫЙ_ИНН)

    текст = await выполнить(среда, подготовка["pending_id"])

    assert словарь_знает(среда.tools, НОВЫЙ_ИНН)
    assert json.loads(текст)["result"]["ИНН"].startswith("[[inn:")
    нет_реальных_значений(текст)


@pytest.mark.parametrize("исход", ["отклонил", "устарело", "ошибка_1С"])
async def test_словарь_не_узнаёт_значение_после_невыполненного_commit(среда, одинс, исход):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": НОВЫЙ_ИНН})
    механизм, подтверждение = "claude_code", None
    if исход == "отклонил":
        механизм, подтверждение = "elicitation", Подтверждение(False)
    elif исход == "устарело":
        одинс.изменить_извне(ПУТЬ_КОНТРАГЕНТА, ДополнительнаяИнформация="чужая правка")
    else:
        одинс.отказ = _ошибка_1с(500, "-1", f"ИНН {НОВЫЙ_ИНН} уже есть у другого контрагента")

    текст = await выполнить(
        среда, подготовка["pending_id"], mechanism=механизм, confirm=подтверждение
    )

    assert "error" in json.loads(текст)
    assert not словарь_знает(среда.tools, НОВЫЙ_ИНН)
    assert одинс.объекты[ПУТЬ_КОНТРАГЕНТА]["ИНН"] == ИНН


# ---------------------------------------------------------------------------------------------
# create, action, mark_for_deletion
# ---------------------------------------------------------------------------------------------


async def test_create_ключ_из_ответа_POST_в_журнале_и_ответе(среда, одинс):
    новый = токен(среда.tools, НОВЫЙ_ИНН)
    подготовка = json.loads(
        await среда.запись.create(
            SessionScope(),
            "s1",
            base="ut",
            entity=КОНТРАГЕНТЫ,
            data={"Description": "ООО Северный Ветер odata1c-приёмка", "ИНН": новый},
        )
    )

    текст = await выполнить(среда, подготовка["pending_id"])

    ответ = json.loads(текст)
    assert одинс.post.call_count == 1 and одинс.patch.call_count == 0
    assert json.loads(одинс.тела_записи[0]) == {
        "Description": "ООО Северный Ветер odata1c-приёмка",
        "ИНН": НОВЫЙ_ИНН,
    }
    [путь] = [п for п in одинс.объекты if п.startswith(f"{КОНТРАГЕНТЫ}(guid'")]
    ссылка = путь[len(КОНТРАГЕНТЫ) + len("(guid'") : -2]
    assert ответ["op"] == "create" and ответ["key"] == {"Ref_Key": ссылка}
    assert ответ["result"]["Ref_Key"] == ссылка and ответ["result"]["ИНН"] == новый
    assert ответ["result"]["Description"].startswith("[[org:")
    # Повторный GET «после» — по ключу из ответа POST.
    assert _путь(одинс.get.calls.last.request) == путь
    нет_реальных_значений(текст)
    строка = среда.журнал(ответ["commit_id"])
    assert строка.key == {"Ref_Key": ссылка} and строка.before is None
    assert строка.after["ИНН"] == НОВЫЙ_ИНН and строка.status == "committed"


async def test_action_Post_без_тела_и_Content_Type_итог_перечитыванием(среда, одинс):
    одинс.положить(ПУТЬ_ДОКУМЕНТА, документ())
    подготовка = json.loads(
        await среда.запись.action(
            SessionScope(), "s1", base="ut", entity=РЕАЛИЗАЦИЯ, key=ССЫЛКА_ДОК, name="Post"
        )
    )

    текст = await выполнить(среда, подготовка["pending_id"])

    ответ = json.loads(текст)
    assert одинс.post.call_count == 1
    assert _путь(одинс.post.calls.last.request) == f"{ПУТЬ_ДОКУМЕНТА}/Post"
    # Форма P8: ни тела, ни `Content-Type` — `{}` ушёл бы телом, которого проба не видела.
    assert одинс.тела_записи == [b""]
    assert "content-type" not in одинс.заголовки_записи[0]
    assert ответ["result"]["Posted"] is True and ответ["warnings"] == []
    строка = среда.журнал(ответ["commit_id"])
    assert строка.before["Posted"] is False and строка.after["Posted"] is True


async def test_mark_for_deletion_commit(среда, одинс):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = json.loads(
        await среда.запись.mark_for_deletion(
            SessionScope(), "s1", base="ut", entity=КОНТРАГЕНТЫ, key=ССЫЛКА
        )
    )

    ответ = json.loads(await выполнить(среда, подготовка["pending_id"]))

    assert json.loads(одинс.тела_записи[0]) == {"DeletionMark": True}
    assert ответ["result"]["DeletionMark"] is True
    assert среда.журнал(ответ["commit_id"]).before["DeletionMark"] is False


async def test_расхождение_записанного_с_отправленным_предупреждение_без_значения(среда, одинс):
    """P8: 1С молча обрезает длину и не принимает значение — видно только перечитыванием."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ДополнительнаяИнформация": "очень длинный текст"})
    одинс.искажение = {"ДополнительнаяИнформация": "очень дл"}

    ответ = json.loads(await выполнить(среда, подготовка["pending_id"]))

    [предупреждение] = [п for п in ответ["warnings"] if "ДополнительнаяИнформация" in п]
    assert "отличается" in предупреждение and "очень" not in предупреждение


async def test_расхождение_табличной_части_после_записи_предупреждение_без_значений(среда, одинс):
    """Проект M3b §5.2: 1С молча меняет строки табличной части при записи (P8) — видно только
    перечитыванием, тем же построчным способом, что решает «изменилась ли часть» при подготовке
    (`rows_diff`). Предупреждение называет только табличную часть, без содержимого строк."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(
        среда,
        {
            "КонтактнаяИнформация": [
                {"LineNumber": 1, "Тип": "Телефон", "Представление": "+7 916 555-55-55"},
                {"LineNumber": 2, "Тип": "АдресЭлектроннойПочты", "Представление": ПОЧТА},
            ]
        },
    )
    # 1С молча не приняла одну из строк — после записи вернула их только одну.
    одинс.искажение = {
        "КонтактнаяИнформация": [
            {"LineNumber": 1, "Тип": "Телефон", "Представление": "+7 916 555-55-55"},
        ]
    }

    ответ = json.loads(await выполнить(среда, подготовка["pending_id"]))

    [предупреждение] = [п for п in ответ["warnings"] if "КонтактнаяИнформация" in п]
    assert "отличаются" in предупреждение
    assert ПОЧТА not in предупреждение and ТЕЛЕФОН not in предупреждение


async def test_табличная_часть_без_расхождений_после_записи_без_предупреждения(среда, одинс):
    """Отрицательный контроль на форме настоящей 1С (проба P9-6/P9-7), а не эхе поддельной: 1С
    отдаёт `LineNumber` СТРОКОЙ (не числом, каким его отправил шлюз) и перечитанная строка несёт
    служебные поля сверх отправленных (`Ref_Key`, `НомерТелефона`) — их у поддельной 1С не было бы
    без явного `искажение`. Сравниваются только поля, которые отправил шлюз (`_строки_расходятся`):
    лишнее поле перечитанной строки — не расхождение, иначе предупреждение срабатывало бы на
    КАЖДОЙ успешной записи табличной части."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(
        среда,
        {
            "КонтактнаяИнформация": [
                {"LineNumber": 1, "Тип": "Телефон", "Представление": "+7 916 555-55-55"},
                {"LineNumber": 2, "Тип": "АдресЭлектроннойПочты", "Представление": ПОЧТА},
            ]
        },
    )
    одинс.искажение = {
        "КонтактнаяИнформация": [
            {
                "Ref_Key": ССЫЛКА,
                "LineNumber": "1",
                "Тип": "Телефон",
                "Представление": "+7 916 555-55-55",
                "НомерТелефона": "79165555555",
            },
            {
                "Ref_Key": ССЫЛКА,
                "LineNumber": "2",
                "Тип": "АдресЭлектроннойПочты",
                "Представление": ПОЧТА,
            },
        ]
    }

    ответ = json.loads(await выполнить(среда, подготовка["pending_id"]))

    assert not [п for п in ответ["warnings"] if "КонтактнаяИнформация" in п]


# ---------------------------------------------------------------------------------------------
# Журнал: отказ «до» останавливает запрос, отказ «после» — нет
# ---------------------------------------------------------------------------------------------


async def test_отказ_журнала_до_запроса_останавливает_запрос(среда, одинс, monkeypatch):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})

    def не_пишется(self, op, *, client, before):
        raise WriteError("internal", "не удалось записать журнал перед выполнением записи")

    monkeypatch.setattr(Journal, "open_commit", не_пишется)

    отказ = ошибка(await выполнить(среда, подготовка["pending_id"]))

    assert отказ["code"] == "internal" and одинс.записей == 0
    assert (await среда.стор.take(подготовка["pending_id"], "s1")).status == "pending"


async def test_отказ_журнала_после_запроса_не_отменяет_выполненного(
    среда, одинс, monkeypatch, caplog
):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})

    def не_дописывается(self, commit_id, **_):
        raise WriteError("internal", "не удалось дописать журнал после выполненной записи")

    monkeypatch.setattr(Journal, "close_commit", не_дописывается)

    текст = await выполнить(среда, подготовка["pending_id"])

    ответ = json.loads(текст)
    assert "commit_id" in ответ and одинс.patch.call_count == 1
    assert any("журнал" in п for п in ответ["warnings"])
    assert (await среда.стор.take(подготовка["pending_id"], "s1")).status == "committed"
    assert all(НОВЫЙ_ИНН not in запись.getMessage() for запись in caplog.records)
    assert any(ответ["commit_id"] in запись.getMessage() for запись in caplog.records)


# ---------------------------------------------------------------------------------------------
# Отказы до выполнения
# ---------------------------------------------------------------------------------------------


async def test_pending_unknown_один_текст_без_повтора_ввода(среда, одинс):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    выдуманный = f"{ЦИФРЫ_ТЕЛЕФОНА}abc"

    чужая = await выполнить(среда, подготовка["pending_id"], s="s2")
    нет_такой = await выполнить(среда, выдуманный)

    assert ошибка(чужая)["code"] == "pending_unknown"
    assert чужая == нет_такой
    assert подготовка["pending_id"] not in чужая and ЦИФРЫ_ТЕЛЕФОНА not in нет_такой
    assert одинс.записей == 0


async def test_замки_не_копятся(среда, одинс):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    await выполнить(среда, подготовка["pending_id"])
    await выполнить(среда, "нет-такой")
    assert среда.запись._замки == {}


# ---------------------------------------------------------------------------------------------
# Раунд 2 ревью задачи 7
# ---------------------------------------------------------------------------------------------


async def создать(среда: Среда, data: dict, *, entity=КОНТРАГЕНТЫ) -> dict:
    ответ = json.loads(
        await среда.запись.create(SessionScope(), "s1", base="ut", entity=entity, data=data)
    )
    assert "pending_id" in ответ, ответ
    return ответ


def _созданный(одинс: Одинс) -> tuple[str, str]:
    [путь] = [п for п in одинс.объекты if п.startswith(f"{КОНТРАГЕНТЫ}(guid'")]
    return путь, путь[len(КОНТРАГЕНТЫ) + len("(guid'") : -2]


async def test_Т7_1_ответ_записи_не_JSON_исход_неизвестен(среда, одинс):
    """1С выполнила PATCH, но тело ответа не JSON (страница посредника с телом запроса): ответ не
    разобран — исход неизвестен, как при таймауте, а не `internal`. Журнал `unknown`, повтор — тот
    же ответ без записи. Тело ответа модели не выдаётся, и замены раннего прохода в нём стражу не
    засчитываются."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    записать = одинс._patch

    def страница(request):
        записать(request)
        return httpx.Response(200, content=f"<html>OK {request.content.decode()}</html>".encode())

    одинс.patch.side_effect = страница

    текст = await выполнить(среда, подготовка["pending_id"])

    отказ = ошибка(текст)
    assert отказ["code"] == "commit_outcome_unknown"
    assert "неизвест" in отказ["message"] and "не повтор" in отказ["hint"]
    assert "odata1c_get" in отказ["hint"]
    нет_реальных_значений(текст)
    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    строка = среда.журнал(операция.commit_id)
    assert строка.status == "unknown" and "JSON" in строка.error
    assert await выполнить(среда, подготовка["pending_id"]) == текст
    assert одинс.patch.call_count == 1


async def test_Т7_1_create_GET_после_сломан_запись_выполнена_ключ_в_журнале(среда, одинс):
    """`create` выполнен, повторный GET «после» вернул не JSON: запись от этого не отменяется.
    Ответ — «выполнено» с ключом из ответа POST, без результата и с предупреждением; журнал —
    `committed` с ключом и пустым `after`; повтор — тот же ответ, POST один."""
    подготовка = await создать(среда, {"Description": "ООО Проба odata1c-приёмка"})
    читать = одинс._get

    def после_сломано(request):
        if "(guid'" in _путь(request):
            return httpx.Response(200, content=b"not json")
        return читать(request)

    одинс.get.side_effect = после_сломано

    текст = await выполнить(среда, подготовка["pending_id"])

    ответ = json.loads(текст)
    _, ссылка = _созданный(одинс)
    assert ответ["key"] == {"Ref_Key": ссылка} and ответ["result"] is None
    assert any("перечитать" in п for п in ответ["warnings"])
    строка = среда.журнал(ответ["commit_id"])
    assert строка.status == "committed" and строка.key == {"Ref_Key": ссылка}
    assert строка.after is None
    assert await выполнить(среда, подготовка["pending_id"]) == текст
    assert одинс.post.call_count == 1


async def test_Т7_1_отмена_во_время_GET_после_create_журнал_committed_с_ключом(среда, одинс):
    """Клиент отключился после выполненного POST, пока идёт GET «после»: журнал уже `committed` с
    ключом (записан сразу по ответу 1С, до GET), повтор отвечает «запись выполнена» — ответ в форме
    выполненного commit с номером записи и ключом, без результата — и запроса не повторяет."""
    подготовка = await создать(среда, {"Description": "ООО Проба odata1c-приёмка"})
    клиент = среда.tools._client_for(среда.tools._config.bases["ut"])
    настоящий_get = клиент.get
    в_пути = asyncio.Event()

    async def get_зависает(path, params=None, **kw):
        if "(guid'" in path:
            в_пути.set()
            await asyncio.Event().wait()
        return await настоящий_get(path, params, **kw)

    клиент.get = get_зависает
    задача = asyncio.create_task(выполнить(среда, подготовка["pending_id"]))
    await в_пути.wait()
    задача.cancel()
    with pytest.raises(asyncio.CancelledError):
        await задача

    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    _, ссылка = _созданный(одинс)
    строка = среда.журнал(операция.commit_id)
    assert операция.status == "committed"
    assert строка.status == "committed" and строка.key == {"Ref_Key": ссылка}
    повтор = json.loads(await выполнить(среда, подготовка["pending_id"]))
    assert повтор["commit_id"] == операция.commit_id and повтор["key"] == {"Ref_Key": ссылка}
    assert повтор["result"] is None and any("выполнена" in п for п in повтор["warnings"])
    assert одинс.post.call_count == 1


async def test_Т7_1_сбой_маски_после_запись_выполнена(среда, одинс, monkeypatch):
    """Любой сбой показа «после» (здесь — маска) не превращает выполненную запись в `internal`."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    гейт = среда.tools._gate_for(среда.tools._config.bases["ut"])

    def сломана(*_a, **_k):
        raise RuntimeError("дефект маски")

    monkeypatch.setattr(гейт, "mask", сломана)

    ответ = json.loads(await выполнить(среда, подготовка["pending_id"]))

    assert "commit_id" in ответ and ответ["result"] is None
    assert any("результат не показан" in п for п in ответ["warnings"])
    строка = среда.журнал(ответ["commit_id"])
    assert строка.status == "committed" and строка.after["ИНН"] == НОВЫЙ_ИНН
    assert одинс.patch.call_count == 1


@pytest.mark.parametrize("статус", [408, 502, 504])
async def test_Т7_4_ответ_посредника_по_таймауту_исход_неизвестен(среда, одинс, статус):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    одинс.отказ = httpx.Response(статус, content=b"<html>upstream</html>")

    отказ = ошибка(await выполнить(среда, подготовка["pending_id"]))

    assert отказ["code"] == "commit_outcome_unknown" and "неизвест" in отказ["message"]
    assert f"HTTP {статус}" in отказ["message"]
    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    assert среда.журнал(операция.commit_id).status == "unknown"


async def test_Т7_4_код_платформы_не_число_не_показывается(среда, одинс):
    """Нечисловой код платформы не показывается — и фразы о коде нет вовсе (М-4 ревью итоговых
    правок M2: прежде стояло «код ошибки платформы —»)."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    одинс.отказ = _ошибка_1с(500, f"Заблокировал {ФИО}", "текст")

    текст = await выполнить(среда, подготовка["pending_id"])

    отказ = ошибка(текст)
    assert отказ["code"] == "odata_error" and "HTTP 500, категория:" in отказ["message"]
    assert "код ошибки платформы" not in отказ["message"]
    assert "Иванов" not in текст and "Заблокировал" not in текст


# ФП-2 ревью итоговых правок M2: 5xx, тело которого не ошибка 1С, — ответила не 1С (страница
# веб-сервера или посредника); запрос мог выполниться, у `create` повтор — дубль.
ОТВЕТЫ_НЕ_ОТ_1С = [
    pytest.param(500, b"<html><body>Internal Server Error</body></html>", id="500-html"),
    pytest.param(503, b"<html><body>Service Unavailable</body></html>", id="503-html"),
    pytest.param(500, b"{}", id="500-json-без-ошибки"),
]


@pytest.mark.parametrize(("статус", "тело"), ОТВЕТЫ_НЕ_ОТ_1С)
@pytest.mark.parametrize("op", ["update", "create"])
async def test_ФП2_5xx_без_ошибки_1С_исход_неизвестен(среда, одинс, op, статус, тело):
    if op == "update":
        одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
        подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    else:
        подготовка = await создать(среда, {"Description": "ООО Проба odata1c-приёмка"})
    одинс.отказ = httpx.Response(статус, content=тело)

    текст = await выполнить(среда, подготовка["pending_id"])

    отказ = ошибка(текст)
    assert отказ["code"] == "commit_outcome_unknown", отказ
    assert f"HTTP {статус} без ошибки 1С" in отказ["message"]
    assert "запись не выполнена" not in отказ["hint"] and "не готовьте" in отказ["hint"]
    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    # Операция закрыта (повтор её запрос не повторит), в журнале исход помечен неизвестным.
    assert операция.status == "failed"
    assert среда.журнал(операция.commit_id).status == "unknown"
    # Повтор — тот же ответ, без второго запроса.
    assert await выполнить(среда, подготовка["pending_id"]) == текст
    assert одинс.записей == 1


@pytest.mark.parametrize(
    "тело_ошибки",
    [
        pytest.param({"odata.error": {"code": "-1", "message": {"value": "x"}}}, id="с-кодом"),
        pytest.param({"odata.error": {"message": {"value": "x"}}}, id="только-сообщение"),
    ],
)
async def test_ФП2_500_с_ошибкой_1С_отказ_запись_не_выполнена(среда, одинс, тело_ошибки):
    """Настоящая ошибка 1С на 500 — отказ, как прежде, в том числе `odata.error` без кода: тело
    разобрано как ошибка платформы. Без числового кода фразы о коде нет (М-4)."""
    подготовка = await создать(среда, {"Description": "ООО Проба odata1c-приёмка"})
    одинс.отказ = httpx.Response(500, json=тело_ошибки)

    отказ = ошибка(await выполнить(среда, подготовка["pending_id"]))

    assert отказ["code"] == "odata_error" and "запись не выполнена" in отказ["hint"]
    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    assert среда.журнал(операция.commit_id).status == "failed"
    assert ("код ошибки платформы -1" in отказ["message"]) == ("code" in тело_ошибки["odata.error"])
    assert "код ошибки платформы —" not in отказ["message"]


async def test_Т7_5_неизвестный_исход_create_ведёт_к_поиску_по_представлению(среда, одинс):
    подготовка = await создать(среда, {"Description": "ООО Проба odata1c-приёмка"})

    def таймаут(_request):
        raise httpx.ReadTimeout("1С думает")

    одинс.перед_записью = таймаут

    отказ = ошибка(await выполнить(среда, подготовка["pending_id"]))

    assert отказ["code"] == "commit_outcome_unknown" and "неизвест" in отказ["message"]
    assert "odata1c_query" in отказ["hint"] and "Description" in отказ["hint"]
    assert "odata1c_get" not in отказ["hint"]
    # Хвост Ruling 63: значения для отбора — из аргументов своего вызова, в превью их больше нет.
    assert "из превью" not in отказ["hint"] and "вашего вызова" in отказ["hint"]


async def test_Т7_6_trust_при_запасном_deny_отказ_без_записи(среда, одинс):
    """`daemon.yaml` по умолчанию — `write_confirm_fallback: deny`: механизм `trust` недопустим,
    сколько бы вызывающий его ни передавал. Операция, отклонённая в диалоге, не выполняется
    следующим `commit` с `trust`."""
    assert среда.tools._config.daemon.write_confirm_fallback == "deny"
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    await выполнить(
        среда, подготовка["pending_id"], mechanism="elicitation", confirm=Подтверждение(False)
    )

    отказ = ошибка(await выполнить(среда, подготовка["pending_id"], mechanism="trust"))

    assert отказ["code"] == "write_unsupported_client"
    assert одинс.записей == 0


async def test_Т7_6_механизм_закрепляется_за_операцией(дом, tmp_path, одинс):
    """Даже при `trust_client` операцию, которую уже подтверждали диалогом, подтверждают только
    диалогом: смена механизма между вызовами `commit` одной операции — отказ. Тот же механизм —
    можно, операция жива до TTL."""
    среда = Среда(дом, tmp_path / "journal.sqlite", запасной="trust_client")
    try:
        одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
        подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
        await выполнить(
            среда, подготовка["pending_id"], mechanism="elicitation", confirm=Подтверждение(False)
        )

        отказ = ошибка(await выполнить(среда, подготовка["pending_id"], mechanism="trust"))

        # Свой код: «сменился механизм» — не «пользователь отклонил» (SPEC §5.2).
        assert отказ["code"] == "confirm_mechanism_mismatch" and одинс.записей == 0
        текст = await выполнить(
            среда, подготовка["pending_id"], mechanism="elicitation", confirm=Подтверждение(True)
        )
        assert "commit_id" in json.loads(текст) and одинс.patch.call_count == 1
    finally:
        await среда.tools.aclose()


async def test_Т7_6_trust_при_запасном_trust_client_выполняет(дом, tmp_path, одинс):
    среда = Среда(дом, tmp_path / "journal.sqlite", запасной="trust_client")
    try:
        одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
        подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
        ответ = json.loads(await выполнить(среда, подготовка["pending_id"], mechanism="trust"))
        assert "commit_id" in ответ and среда.журнал(ответ["commit_id"]).client == "trust"
    finally:
        await среда.tools.aclose()


async def test_Т7_3_чужая_сессия_не_ждёт_диалога_владельца(среда, одинс):
    """Замок — на пару (сессия, операция): чужая сессия не встаёт в очередь за чужой операцией и
    отвечает `pending_unknown` сразу, как на выдуманный номер."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    отпустить, спросили = asyncio.Event(), asyncio.Event()

    async def думает(message):
        спросили.set()
        await отпустить.wait()
        return False

    владелец = asyncio.create_task(
        выполнить(среда, подготовка["pending_id"], mechanism="elicitation", confirm=думает)
    )
    await спросили.wait()
    try:
        чужой = await asyncio.wait_for(выполнить(среда, подготовка["pending_id"], s="s2"), 1)
        выдуманный = await выполнить(среда, "0" * 32, s="s2")
        assert ошибка(чужой)["code"] == "pending_unknown"
        assert чужой == выдуманный
    finally:
        отпустить.set()
        await владелец


# ---------------------------------------------------------------------------------------------
# Раунд 3 ревью задачи 7
# ---------------------------------------------------------------------------------------------

_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


async def test_Н2_1_сбой_стража_итогового_ответа_после_записи_отдаёт_страховку(
    среда, одинс, monkeypatch
):
    """Запись выполнена, а итоговый `gate.finish` упал (синтетика): модель получает не
    `internal`, а готовую страховку «запись выполнена» — она уже прошла стража. Повтор — тот же
    ответ, PATCH один."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    гейт = среда.tools._gate_for(среда.tools._config.bases["ut"])
    настоящий = гейт.finish

    def finish(конверт, раскрытое=None):
        if конверт.get("result") is not None:
            raise RuntimeError("страж упал")
        return настоящий(конверт, раскрытое)

    monkeypatch.setattr(гейт, "finish", finish)

    текст = await выполнить(среда, подготовка["pending_id"])

    ответ = json.loads(текст)
    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    assert ответ["commit_id"] == операция.commit_id and ответ["result"] is None
    assert ответ["key"] == ССЫЛКА
    assert any("выполнена" in п for п in ответ["warnings"])
    assert операция.status == "committed" and операция.result == текст
    assert await выполнить(среда, подготовка["pending_id"]) == текст
    assert одинс.patch.call_count == 1


async def test_Н2_1_сбой_сборки_ответа_после_записи_отдаёт_страховку(среда, одинс, monkeypatch):
    """Любой сбой сборки ответа после выполненной записи (здесь — сравнение «после» с
    отправленным) — та же страховка «запись выполнена», а не `internal`."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})

    def сломано(*_a, **_k):
        raise RuntimeError("дефект")

    monkeypatch.setattr("odata1c.write.service._расхождения", сломано)

    ответ = json.loads(await выполнить(среда, подготовка["pending_id"]))

    assert "commit_id" in ответ and ответ["result"] is None
    assert any("выполнена" in п for п in ответ["warnings"])
    assert среда.журнал(ответ["commit_id"]).status == "committed"
    assert одинс.patch.call_count == 1


async def test_Н2_1_сбой_до_сборки_страховки_не_выдаёт_исход_неизвестен(среда, одинс, monkeypatch):
    """Сбой после ответа 1С успехом, но раньше, чем собрана страховка «запись выполнена» (здесь —
    неожиданное исключение журнала): страховка «исход неизвестен», собранная до запроса, уже
    неверна и не отдаётся. Ответ — успешный конверт без данных (И-4 итогового ревью M2): запись
    выполнена, и код ошибки толкал бы модель готовить её заново — у `create` это дубль."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    операция = await среда.стор.take(подготовка["pending_id"], "s1")

    def сломан(self, commit_id, **_):
        raise TypeError("дефект журнала")

    monkeypatch.setattr(Journal, "close_commit", сломан)

    текст = await выполнить(среда, подготовка["pending_id"])

    ответ = json.loads(текст)
    assert "error" not in ответ, текст
    assert ответ["commit_id"] == операция.commit_id and ответ["result"] is None
    assert any("выполнена" in п and "не собран" in п for п in ответ["warnings"])
    assert "неизвест" not in текст
    assert операция.status == "committed" and одинс.patch.call_count == 1
    assert await выполнить(среда, подготовка["pending_id"]) == текст


@pytest.mark.parametrize(
    ("операция", "data"),
    [
        pytest.param("create", {"ИНН": ИНН, "Комментарий": "odata1c-приёмка"}, id="create"),
        pytest.param("update", {"Комментарий": "стал"}, id="update"),
    ],
)
async def test_Ruling_60_запись_регистра_не_готовится_commit_не_до_чего(
    среда_синт, одинс, операция, data
):
    """Ruling 60: запись регистра сведений в первой поставке не готовится — ни `create` с ключом
    открытым литералом, ни `update` по ключу-токену. Прежние тесты этого места (Н2-2, Н2-3,
    страховочный ответ отмены с ключом регистра) сторожили механику `commit` записи регистра:
    ключ из ответа POST маской без записи в словарь, словарь учится из GET «после», ключ-токен
    без ложного `guard_replaced`. Код этой механики остаётся для поставки, которая запись
    регистров откроет; её юнит-часть — `_ключ_созданного` и `_отпечаток` выше."""
    среда = среда_синт
    if операция == "create":
        ответ = await среда.запись.create(
            SessionScope(), "s1", base="ut", entity=РЕГИСТР_ИНН, data=data
        )
    else:
        ключ = {"ИНН": токен(среда.tools, ИНН, entity=РЕГИСТР_ИНН)}
        ответ = await среда.запись.update(
            SessionScope(), "s1", base="ut", entity=РЕГИСТР_ИНН, key=ключ, data=data
        )

    отказ = ошибка(ответ)

    assert отказ["code"] == "permission_denied" and РЕГИСТР_ИНН in отказ["message"]
    assert одинс.записей == 0 and одинс.get.call_count == 0


async def test_Н2_4_отказ_deny_и_trust_не_закрепляет_механизм(среда, одинс):
    """Перенесено из воспроизведений ревьюера (Р5): механизм закрепляется только допустимым
    вызовом — `deny` и `trust` при запасном `deny` отказывают раньше закрепления, и операцию
    потом можно подтвердить диалогом."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    for механизм in ("deny", "trust"):
        отказ = ошибка(await выполнить(среда, подготовка["pending_id"], mechanism=механизм))
        assert отказ["code"] == "write_unsupported_client"
    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    assert операция.mechanism is None
    отказ = ошибка(
        await выполнить(
            среда, подготовка["pending_id"], mechanism="elicitation", confirm=Подтверждение(False)
        )
    )
    assert отказ["code"] == "permission_denied" and операция.mechanism == "elicitation"
    отказ = ошибка(await выполнить(среда, подготовка["pending_id"], mechanism="claude_code"))
    assert отказ["code"] == "confirm_mechanism_mismatch" and одинс.записей == 0


async def test_номера_операций_в_форме_GUID(среда, одинс):
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    assert _GUID.fullmatch(подготовка["pending_id"]) and _GUID.fullmatch(операция.commit_id)


@pytest.mark.parametrize(
    "номер",
    [
        # Сегменты-цифры, которые детекторы узнали бы в шестнадцатеричной строке без дефисов:
        # телефон, известный ИНН, счёт, карта, СНИЛС, ОГРН; буквы — вариант названия «deca».
        "89161234-5678-4891-8123-891612345678",
        "77070838-9377-4707-8389-377070838930",
        "40817810-0999-4000-8312-408178100999",
        "41111111-1111-4111-9111-111111111111",
        "11223344-5951-4122-8334-112233445950",
        "10277001-3219-4102-8770-102770013219",
        "deca5bd0-deca-4eca-8eca-decadecadeca",
    ],
)
async def test_номер_операции_в_тексте_ошибки_не_режется_гейтом(среда, одинс, номер):
    """Причина нестабильного теста Ruling 52: номер операции (`uuid4().hex`) шёл в текст ошибки,
    а тот — через `mask_text` и стража; серия цифр в нём иногда совпадала с детектором, и
    `commit_id` приходил с токеном внутри. В форме GUID его не трогает ни один слой."""
    одинс.положить(ПУТЬ_КОНТРАГЕНТА, контрагент())
    подготовка = await изменить(среда, {"ИНН": токен(среда.tools, НОВЫЙ_ИНН)})
    операция = await среда.стор.take(подготовка["pending_id"], "s1")
    операция.commit_id = номер
    одинс.отказ = _ошибка_1с(500, "-1", "текст")

    текст = await выполнить(среда, подготовка["pending_id"])

    assert номер in ошибка(текст)["message"] and "guard_replaced" not in текст
