"""Подготовка `update` и `mark_for_deletion` (план M2, задача 5): первая точка, где запись
встречается с гейтом. Реальные значения объекта 1С и превью для модели проходят через один вызов
`ToolService._run`, и ошибка здесь сразу нарушает инвариант 1.

Поддельная 1С — `respx`, гейт — `identifiers+names` (роль `prod` с явным `write: true`: у `dev`
гейт выключен, и токенов не было бы вовсе). Проверки «ни одного обращения к 1С» и «только GET»
идут по конкретным маршрутам, а не по общему счётчику роутера: фикстура держит перехват базового
адреса под завершение сеанса 1С.
"""

import contextlib
import dataclasses
import functools
import json
import re
import urllib.parse

import httpx
import pytest
import respx
import yaml
from conftest import (
    без_навигаций,
    ничего_не_скрыто,
    обеспечить_policy_yaml,
    строение_неизвестно,
    эхо_отбора,
)

from odata1c.cli import main
from odata1c.config.loader import load_config
from odata1c.gate.revealed import RevealedValues
from odata1c.gate.service import policy_path, refresh_policy
from odata1c.gate.unmasking import GateError, open_literal_refusal
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository
from odata1c.registry.registry import SessionScope
from odata1c.tools.service import ToolService
from odata1c.write import service
from odata1c.write.journal import Journal
from odata1c.write.pending import CommitLimiter, PendingStore
from odata1c.write.preview import diff_preview
from odata1c.write.service import WriteService

URL_UT = "http://localhost/ut/odata/standard.odata/"
URL_RO = "http://localhost/ro/odata/standard.odata/"
URL_NOMARK = "http://localhost/nomark/odata/standard.odata/"

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
    permissions:
      deny_fields: [Catalog_Контрагенты.КодПоОКПО]
  ro:
    label: УТ, только чтение
    url: {URL_RO}
    user: u
    password: p
    role: prod
  nomark:
    label: УТ, без пометки удаления
    url: {URL_NOMARK}
    user: u
    password: p
    role: prod
    write: true
    permissions:
      mark_deletion: false
"""

КОНТРАГЕНТЫ = "Catalog_Контрагенты"
РЕАЛИЗАЦИЯ = "Document_РеализацияТоваровУслуг"
ССЫЛКА = "a103cb54-42ee-11ec-a7a0-f10ab59a067e"
ССЫЛКА_ДОК = "0c4320aa-624f-11f0-a7a0-fa78dd2b3d42"
ВЕРСИЯ = "AAAAAQAAAAA="

ИНН = "7707083893"
ИНН_С_ПРОБЕЛОМ = "7707 083893"
НОВЫЙ_ИНН = "7736050003"
ЧУЖОЙ_ИНН = "7728168971"
НАЗВАНИЕ = "ООО Ромашка"
ПОЛНОЕ_НАЗВАНИЕ = "Общество с ограниченной ответственностью «Ромашка»"
КПП = "770701001"
ТЕЛЕФОН = "+7 916 123-45-67"
ЦИФРЫ_ТЕЛЕФОНА = "79161234567"
ПОЧТА = "ivan@example.com"
АДРЕС_ДОСТАВКИ = "г. Москва, ул. Тверская, д. 7, кв. 43"
ЧУЖОЙ_АДРЕС = "г. Казань, ул. Баумана, д. 1"

# Все реальные значения защищаемых полей фикстуры — проверка «от класса данных» ищет каждое во
# всём тексте ответа, а не только в поле, которое меняли (бриф задачи 5, решение 5 контролёра).
# Сюда входят и поля, которых запись не касается: GET текущего состояния отдаёт объект, и любое
# его поле могло бы утечь мимо превью.
РЕАЛЬНЫЕ_ЗНАЧЕНИЯ = (
    ИНН,
    ИНН_С_ПРОБЕЛОМ,
    НОВЫЙ_ИНН,
    ЧУЖОЙ_ИНН,
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

ТОКЕН_ИНН = re.compile(r"^\[\[inn:[^\]]+\]\]$")


def _дом(tmp_path, edmx: bytes, bases_yaml: str = BASES_YAML):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(bases_yaml, encoding="utf-8")
    config = load_config(home)
    for имя in config.bases:
        хранилище = IndexRepository(index_path(home, имя))
        хранилище.write(parse_edmx(edmx))
        хранилище.close()
        refresh_policy(home, config.bases[имя])
        обеспечить_policy_yaml(home, имя)
    return home


class Часы:
    """Подменные часы: монотонные — у стора, настенные — у сервиса. Разные значения намеренно:
    смешай код одни с другими, `expires_in_s` вышел бы мусором и тест бы это показал."""

    def __init__(self, начало: float) -> None:
        self.сейчас = начало

    def __call__(self) -> float:
        return self.сейчас


@pytest.fixture
def дом(tmp_path, edmx_ut_real):
    return _дом(tmp_path, edmx_ut_real)


@pytest.fixture
async def среда(дом, tmp_path):
    """`WriteService` поверх настоящего `ToolService` — тот же гейт, индекс и набор раскрытого,
    что у тулов чтения."""
    tools = ToolService(load_config(дом))
    часы_стора = Часы(1000.0)
    стор = PendingStore(600, clock=часы_стора)
    # Журнал — фабрикой: `commit` открывает его на вызов (задача 7); подготовка его не зовёт.
    журнал = functools.partial(Journal, tmp_path / "journal.sqlite")
    запись = WriteService(tools, стор, журнал, CommitLimiter(), clock=Часы(1_757_000_000.0))
    yield запись, стор, tools, часы_стора
    await tools.aclose()


class Маршруты:
    """Маршруты поддельной 1С одной базы: GET по сущностям и все пишущие методы. Пишущие
    отвечают 500 — если код их всё же вызовет, ответ заметен, а не тих."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.router = router
        self.get = router.get(url__regex=r".*standard\.odata/[^?]+").mock(
            return_value=httpx.Response(500, json={})
        )
        отказ = httpx.Response(500, json={})
        self.patch = router.patch(url__regex=r".*").mock(return_value=отказ)
        self.post = router.post(url__regex=r".*").mock(return_value=отказ)
        self.put = router.put(url__regex=r".*").mock(return_value=отказ)
        self.delete = router.delete(url__regex=r".*").mock(return_value=отказ)

    def объект(self, тело: dict, *, status: int = 200, по_выбору: bool = False) -> None:
        """`по_выбору=True` — поддельная 1С соблюдает `$select`, как настоящая: поле, которое
        код не запросил, не приходит вовсе (находка M-2: без этого тест не видит, что `Posted`
        выпал из выбора и решение 12 молча отключилось)."""
        if not по_выбору:
            self.get.mock(return_value=httpx.Response(status, json=тело))
            return

        def ответ(request: httpx.Request) -> httpx.Response:
            выбор = request.url.params.get("$select")
            поля = set(выбор.split(",")) if выбор else set(тело)
            return httpx.Response(status, json={к: з for к, з in тело.items() if к in поля})

        self.get.mock(side_effect=ответ)

    @property
    def писали(self) -> bool:
        return any(м.called for м in (self.patch, self.post, self.put, self.delete))

    @property
    def обращались(self) -> bool:
        return self.get.called or self.писали


@pytest.fixture
def одинс():
    with respx.mock(assert_all_called=False) as router:
        # Завершение сеанса 1С идёт на базовый адрес без хвоста — отдельный маршрут, чтобы он
        # не попадал в счётчик GET по сущностям (тот же приём, что в test_tools_service.py).
        for url in (URL_UT, URL_RO, URL_NOMARK):
            router.get(url).mock(return_value=httpx.Response(200, json={"value": []}))
        yield Маршруты(router)


def контрагент(**поля) -> dict:
    """Объект справочника так, как его отдала бы 1С целиком: с табличной частью контактной
    информации и всеми защищаемыми полями. Поддельная 1С отдаёт его, не глядя на `$select`, —
    так проверяется, что превью строится только из изменённых полей, а остальное не утекает."""
    тело = {
        "odata.metadata": "…",
        "Ref_Key": ССЫЛКА,
        "DataVersion": ВЕРСИЯ,
        "DeletionMark": False,
        "Predefined": False,
        "Description": НАЗВАНИЕ,
        "НаименованиеПолное": ПОЛНОЕ_НАЗВАНИЕ,
        "ИНН": ИНН,
        "КПП": КПП,
        "ДополнительнаяИнформация": "постоянный клиент",
        "НДСПоСтавкам4и2": False,
        "ЮрФизЛицо": "ЮрЛицо",
        "КонтактнаяИнформация": [
            {
                "LineNumber": "1",
                "Тип": "Телефон",
                "Представление": ТЕЛЕФОН,
                "НомерТелефона": ЦИФРЫ_ТЕЛЕФОНА,
                "Значение": json.dumps({"value": ТЕЛЕФОН, "type": "Телефон"}, ensure_ascii=False),
            },
            {"LineNumber": "2", "Тип": "АдресЭлектроннойПочты", "Представление": ПОЧТА},
        ],
    }
    тело.update(поля)
    return тело


def документ(**поля) -> dict:
    тело = {
        "Ref_Key": ССЫЛКА_ДОК,
        "DataVersion": ВЕРСИЯ,
        "DeletionMark": False,
        "Posted": False,
        "Number": "УТ-000711",
        "Date": "2026-08-26T12:00:00",
        "Комментарий": "по вх упд 711",
        "СуммаДокумента": 1500.0,
        "АдресДоставки": АДРЕС_ДОСТАВКИ,
        "АдресДоставкиЗначение": json.dumps(
            {"value": ЧУЖОЙ_АДРЕС, "type": "Адрес"}, ensure_ascii=False
        ),
    }
    тело.update(поля)
    return тело


def токен(tools: ToolService, значение: str, *, entity=КОНТРАГЕНТЫ, поле="ИНН") -> str:
    """Токен, который модель увидела бы в ответе чтения, — маской того же гейта базы ut, без
    обращения к 1С (тот же приём, что `токен_инн` в test_tools_service.py): значение ложится в
    словарь с написанием этого поля, как после настоящего `query`/`get`."""
    гейт = tools._gate_for(tools._config.bases["ut"])
    return гейт.mask(
        {поле: значение},
        entity=entity,
        resolve=без_навигаций,
        hidden=ничего_не_скрыто,
        revealed=None,
        shape=строение_неизвестно,
    ).data[поле]


def нет_реальных_значений(текст: str, *, кроме: tuple[str, ...] = ()) -> None:
    """`кроме` — литералы, которые прислала сама модель: «станет» показывается как прислано
    (Ruling 45), и её собственное значение в ответе — не утечка."""
    for значение in РЕАЛЬНЫЕ_ЗНАЧЕНИЯ:
        if значение in кроме:
            continue
        assert значение not in текст, f"реальное значение фикстуры в ответе: {значение[:3]}…"
    # Маскировщик обязан справиться сам: страж — последний рубеж, а не основной. Без этой
    # проверки превью с открытым ИНН прошло бы тест — страж заменил бы его на выходе.
    assert "guard_replaced" not in текст


def строки_словаря(tools: ToolService) -> dict[str, int]:
    """Число строк в каждой таблице `gate.sqlite` — состояние словаря целиком (Ruling 45:
    подготовка не пишет в него ничего). Все таблицы, а не только `tokens`: написание ложится в
    `variants`, вариант названия — в свою таблицу, и проверка одной таблицы пропустила бы их."""
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


@contextlib.contextmanager
def _строение_индекса(tools: ToolService):
    """Строение сущностей по индексу базы `ut` — то, что тул передаёт гейту. Нужно, когда тест
    сверяет текст отказа прямого вызова гейта с текстом тула: имя поля отказ называет, только
    если индекс его знает (Ruling 53)."""
    репозиторий = tools._open_index(tools._config.bases["ut"])
    try:
        yield tools._строение(репозиторий)
    finally:
        репозиторий.close()


def ошибка(текст: str) -> dict:
    данные = json.loads(текст)
    assert "error" in данные, текст
    return данные["error"]


# ---------------------------------------------------------------------------------------------
# update: превью в токенах, тело PATCH, pending-операция
# ---------------------------------------------------------------------------------------------


async def test_превью_ИНН_в_токенах_и_ни_одного_реального_значения(среда, одинс):
    запись, _, tools, _ = среда
    новый = токен(tools, НОВЫЙ_ИНН)
    одинс.объект(контрагент())

    текст = await запись.update(
        SessionScope(), "sess-1", base="ut", entity=КОНТРАГЕНТЫ, key=ССЫЛКА, data={"ИНН": новый}
    )

    нет_реальных_значений(текст)
    ответ = json.loads(текст)
    [строка] = ответ["preview"]
    assert строка["field"] == "ИНН"
    assert ТОКЕН_ИНН.match(строка["before"]) and строка["after"] == новый
    assert строка["before"] != строка["after"]
    # Ruling 44: превью называет объект — ключ как прислала модель и представление маской.
    assert ответ["key"] == ССЫЛКА
    assert list(ответ["object"]) == ["Description"]
    assert ответ["object"]["Description"].startswith("[[org:")
    assert ответ["base"] == "ut" and ответ["role"] == "prod"
    assert ответ["entity"] == КОНТРАГЕНТЫ and ответ["op"] == "update"
    assert ответ["pending_id"]
    assert "odata1c_commit" in ответ["next"]


async def test_в_1С_ушёл_только_GET(среда, одинс):
    """Инвариант 2: подготовка ничего не пишет — ни PATCH, ни POST, ни DELETE."""
    запись, _, tools, _ = среда
    одинс.объект(контрагент())

    await запись.update(
        SessionScope(),
        "sess-1",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        key=ССЫЛКА,
        data={"ИНН": токен(tools, НОВЫЙ_ИНН)},
    )

    assert одинс.get.call_count == 1
    assert not одинс.писали


async def test_pending_несёт_реальное_значение_из_словаря_и_читает_только_нужные_поля(среда, одинс):
    запись, стор, tools, часы_стора = среда
    одинс.объект(контрагент())

    ответ = json.loads(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=КОНТРАГЕНТЫ,
            key=ССЫЛКА,
            data={"ИНН": токен(tools, НОВЫЙ_ИНН)},
        )
    )
    операция = await стор.take(ответ["pending_id"], "sess-1")

    assert операция.request == {
        "method": "PATCH",
        "path": f"{КОНТРАГЕНТЫ}(guid'{ССЫЛКА}')",
        "json": {"ИНН": НОВЫЙ_ИНН},
    }
    assert операция.data_version == ВЕРСИЯ
    assert (операция.session_id, операция.base, операция.role) == ("sess-1", "ut", "prod")
    assert (операция.op, операция.entity) == ("update", КОНТРАГЕНТЫ)
    assert операция.key == {"Ref_Key": ССЫЛКА}
    assert операция.status == "pending"
    assert операция.pending_id != операция.commit_id and операция.commit_id
    # expires_at — от часов стора (монотонных), created_at — от часов сервиса (настенных).
    assert операция.expires_at == часы_стора() + 600
    assert операция.created_at == 1_757_000_000.0
    assert ответ["expires_in_s"] == 600
    нет_реальных_значений(json.dumps(операция.preview, ensure_ascii=False))
    assert операция.preview["key"] == ССЫЛКА
    assert операция.preview["object"] == ответ["object"]
    # Текущее состояние читается только теми полями, что меняются, отпечатком версии и
    # представлением объекта: объект целиком с табличными частями подготовке не нужен.
    выбор = одинс.get.calls.last.request.url.params["$select"].split(",")
    assert sorted(выбор) == ["DataVersion", "Description", "ИНН"]


async def test_неизменённые_поля_не_уходят_в_тело_PATCH(среда, одинс):
    """Решение 8 плана: неизменённое поле 1С не получает — ни токеном того же значения, ни
    открытым значением, совпадающим с текущим."""
    запись, стор, tools, _ = среда
    одинс.объект(контрагент())

    ответ = json.loads(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=КОНТРАГЕНТЫ,
            key=ССЫЛКА,
            data={
                "ИНН": токен(tools, ИНН),
                "КПП": КПП,
                "ДополнительнаяИнформация": "звонить после обеда",
            },
        )
    )
    операция = await стор.take(ответ["pending_id"], "sess-1")

    assert операция.request["json"] == {"ДополнительнаяИнформация": "звонить после обеда"}
    assert [строка["field"] for строка in ответ["preview"]] == ["ДополнительнаяИнформация"]
    assert ответ["preview"][0]["before"] == "постоянный клиент"


async def test_токен_двух_написаний_берёт_написание_из_current(среда, одинс):
    """Шаг 2 лестницы (раздел 1.2 отчёта блокеров): значение уже лежит в поле — записывается
    оно дословно, а не другое известное словарю написание. Иначе запись молча переписала бы
    «7707 083893» в «7707083893», и превью этого не показало бы."""
    запись, стор, tools, _ = среда
    ток = токен(tools, ИНН)
    assert токен(tools, ИНН_С_ПРОБЕЛОМ) == ток
    одинс.объект(контрагент(ИНН=ИНН_С_ПРОБЕЛОМ))

    ответ = json.loads(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=КОНТРАГЕНТЫ,
            key=ССЫЛКА,
            data={"ИНН": ток, "ДополнительнаяИнформация": "новое"},
        )
    )
    операция = await стор.take(ответ["pending_id"], "sess-1")

    assert "ИНН" not in операция.request["json"]


async def test_токен_двух_написаний_без_current_отказ_token_ambiguous(среда, одинс):
    запись, стор, tools, _ = среда
    ток = токен(tools, ИНН)
    assert токен(tools, ИНН_С_ПРОБЕЛОМ) == ток
    одинс.объект(контрагент(ИНН=ЧУЖОЙ_ИНН))

    текст = await запись.update(
        SessionScope(), "sess-1", base="ut", entity=КОНТРАГЕНТЫ, key=ССЫЛКА, data={"ИНН": ток}
    )

    assert ошибка(текст)["code"] == "token_ambiguous"
    нет_реальных_значений(текст)
    assert not одинс.писали
    assert стор._ops == {}


async def test_открытое_значение_пользователя_в_превью_пометкой_класса(среда, одинс):
    """Открытое значение, продиктованное пользователем, идёт по правилам `inbound_value`: с
    контрольной суммой класса поля. «Станет» не маскируется (Ruling 45: маска значения модели
    записала бы его в общий словарь до подтверждения) и не повторяется — пометка класса поля
    (Ruling 62). «Было» — по-прежнему токен."""
    запись, стор, _, _ = среда
    одинс.объект(контрагент())

    ответ = json.loads(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=КОНТРАГЕНТЫ,
            key=ССЫЛКА,
            data={"ИНН": НОВЫЙ_ИНН},
        )
    )
    операция = await стор.take(ответ["pending_id"], "sess-1")

    assert операция.request["json"] == {"ИНН": НОВЫЙ_ИНН}
    [строка] = ответ["preview"]
    assert строка["after"] == "значение из запроса (класс inn)"
    assert ТОКЕН_ИНН.match(строка["before"])
    нет_реальных_значений(json.dumps(ответ, ensure_ascii=False))


async def test_открытое_значение_без_контрольной_суммы_отклоняется(среда, одинс):
    запись, стор, _, _ = среда
    одинс.объект(контрагент())

    текст = await запись.update(
        SessionScope(),
        "sess-1",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        key=ССЫЛКА,
        data={"ИНН": "7707083894"},
    )

    assert ошибка(текст)["code"] == "filter_syntax"
    assert стор._ops == {}


async def test_литерал_модели_в_станет_пометкой_класса(среда, одинс):
    """Ruling 62: литерал модели в «станет» поля защищаемого класса не повторяется — пометка с
    классом поля; стражу заменять нечего, `guard_replaced` нет (прежде страж заменял известное
    значение токеном, и по разнице читалось членство). Изменённость — по реальным значениям:
    новое написание того же ИНН уходит в PATCH, хотя «было» — токен того же значения."""
    запись, стор, tools, _ = среда
    одинс.объект(контрагент())

    ответ = json.loads(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=КОНТРАГЕНТЫ,
            key=ССЫЛКА,
            data={"ИНН": ИНН_С_ПРОБЕЛОМ},
        )
    )
    операция = await стор.take(ответ["pending_id"], "sess-1")

    [строка] = ответ["preview"]
    assert строка["before"] == токен(tools, ИНН)
    assert строка["after"] == "значение из запроса (класс inn)"
    assert not any(п.startswith("guard_replaced") for п in ответ["warnings"])
    assert операция.request["json"] == {"ИНН": ИНН_С_ПРОБЕЛОМ}
    assert ИНН not in json.dumps(ответ, ensure_ascii=False)
    assert ИНН_С_ПРОБЕЛОМ not in json.dumps(ответ, ensure_ascii=False)


def _без_номера(текст: str) -> str:
    return текст.replace(json.loads(текст)["pending_id"], "<pending_id>")


@pytest.mark.parametrize(
    ("entity", "поле", "известное", "неизвестное", "класс", "объект"),
    [
        pytest.param(
            КОНТРАГЕНТЫ, "Description", "ООО Василёк", "ООО Незнакомка", "org", None, id="название"
        ),
        pytest.param(
            "Catalog_БанковскиеСчетаКонтрагентов",
            "ТелефоныБанка",
            ТЕЛЕФОН,
            "+7 495 700-11-22",
            "phone",
            {
                "Ref_Key": ССЫЛКА,
                "DataVersion": ВЕРСИЯ,
                "Description": "Расчётный",
                "ТелефоныБанка": "+7 800 000-00-01",
            },
            id="телефон",
        ),
    ],
)
async def test_Ruling_62_update_известное_и_неизвестное_значение_байт_в_байт(
    среда, одинс, entity, поле, известное, неизвестное, класс, объект
):
    """Ruling 62 для `update`: «станет» известного словарю значения и неизвестного — ответы
    подготовки и тексты подтверждения совпадают байт в байт, `guard_replaced` нет."""
    запись, стор, tools, _ = среда
    одинс.объект(объект or контрагент())
    токен(tools, известное, entity=entity, поле=поле)

    ответы = [
        await запись.update(
            SessionScope(), "sess-1", base="ut", entity=entity, key=ССЫЛКА, data={поле: значение}
        )
        for значение in (известное, неизвестное)
    ]

    assert _без_номера(ответы[0]) == _без_номера(ответы[1]), ответы
    assert "guard_replaced" not in ответы[0]
    assert f"значение из запроса (класс {класс})" in ответы[0]
    assert известное not in ответы[0]
    операции = [await стор.take(json.loads(о)["pending_id"], "sess-1") for о in ответы]
    assert service._текст_подтверждения(операции[0]) == service._текст_подтверждения(операции[1])
    assert [о.request["json"][поле] for о in операции] == [известное, неизвестное]


@pytest.mark.parametrize(
    ("известное", "неизвестное", "entity", "поле", "шаблон"),
    [
        pytest.param(ИНН, НОВЫЙ_ИНН, КОНТРАГЕНТЫ, "ИНН", "ИНН покупателя {}", id="ИНН"),
        pytest.param(
            ТЕЛЕФОН,
            "+7 495 700-11-22",
            "Catalog_БанковскиеСчетаКонтрагентов",
            "ТелефоныБанка",
            "звонить {}",
            id="телефон",
        ),
        pytest.param(
            НАЗВАНИЕ,
            "ООО Незнакомка",
            КОНТРАГЕНТЫ,
            "Description",
            "отгрузить {} по договору",
            id="название",
        ),
    ],
)
async def test_Ruling_63_update_догадка_в_поле_без_класса_байт_в_байт(
    среда, одинс, известное, неизвестное, entity, поле, шаблон
):
    """Ruling 63 (ФП-1 ревью итоговых правок M2) для `update`: «станет» `Комментарий` документа
    (класса нет) с известным словарю значением внутри и с неизвестным — ответы подготовки и тексты
    подтверждения после стража совпадают байт в байт; ни токена чтения, ни `guard_replaced`."""
    запись, стор, tools, _ = среда
    токен_чтения = токен(tools, известное, entity=entity, поле=поле)
    одинс.объект(документ())

    ответы = [
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=РЕАЛИЗАЦИЯ,
            key=ССЫЛКА_ДОК,
            data={"Комментарий": шаблон.format(з)},
        )
        for з in (известное, неизвестное)
    ]

    assert _без_номера(ответы[0]) == _без_номера(ответы[1]), ответы
    assert "guard_replaced" not in ответы[0] and токен_чтения not in ответы[0]
    assert известное not in ответы[0]
    [строка] = json.loads(ответы[0])["preview"]
    assert строка["after"] == "значение из запроса"
    операции = [await стор.take(json.loads(о)["pending_id"], "sess-1") for о in ответы]
    гейт = tools._gate_for(tools._config.bases["ut"])
    тексты = {гейт.finish_text(service._текст_подтверждения(о)) for о in операции}
    assert len(тексты) == 1, тексты
    assert [о.request["json"]["Комментарий"] for о in операции] == [
        шаблон.format(известное),
        шаблон.format(неизвестное),
    ]


async def test_изменений_нет_отказ_params_invalid(среда, одинс):
    запись, стор, tools, _ = среда
    одинс.объект(контрагент())

    текст = await запись.update(
        SessionScope(),
        "sess-1",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        key=ССЫЛКА,
        data={"ИНН": токен(tools, ИНН), "ДополнительнаяИнформация": "постоянный клиент"},
    )

    отказ = ошибка(текст)
    assert отказ["code"] == "params_invalid" and "изменений нет" in отказ["message"]
    assert стор._ops == {}
    assert not одинс.писали


async def test_токен_в_структурное_поле_контактной_информации_отказ(среда, одинс):
    """Ruling 38, решение 2 контролёра: в `…Значение` токен записывается, только если это
    значение там уже лежит. Иначе — отказ гейта `token_ambiguous` с его подсказкой, как есть:
    собирать JSON БСП по токену шлюз не берётся, а изменение адреса структурой — вне поставки."""
    запись, стор, tools, _ = среда
    адрес = токен(tools, АДРЕС_ДОСТАВКИ, entity=РЕАЛИЗАЦИЯ, поле="АдресДоставки")
    одинс.объект(документ())

    текст = await запись.update(
        SessionScope(),
        "sess-1",
        base="ut",
        entity=РЕАЛИЗАЦИЯ,
        key=ССЫЛКА_ДОК,
        data={"АдресДоставкиЗначение": адрес},
    )

    отказ = ошибка(текст)
    assert отказ["code"] == "token_ambiguous"
    assert "открытым значением" in отказ["hint"]
    нет_реальных_значений(текст)
    assert стор._ops == {}


async def test_дата_токеном_проверяется_после_раскрытия(среда, одинс):
    """`Edm.DateTime` бывает защищаемым значением (`dob`): токен даты до 1С по формату не
    проверить, его формат известен только после `inbound_write`. Годное написание — в тело PATCH,
    негодное (дата без времени, так 1С не примет) — отказ до создания операции, без значения."""
    запись, стор, tools, _ = среда
    лица, ссылка = "Catalog_ФизическиеЛица", ССЫЛКА
    годная = токен(tools, "1980-05-01T00:00:00", entity=лица, поле="ДатаРождения")
    негодная = токен(tools, "1975-03-02", entity=лица, поле="ДатаРождения")
    assert годная.startswith("[[dob:") and негодная.startswith("[[dob:")
    одинс.объект({"Ref_Key": ссылка, "DataVersion": ВЕРСИЯ, "ДатаРождения": "1990-01-01T00:00:00"})

    ответ = json.loads(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=лица,
            key=ссылка,
            data={"ДатаРождения": годная},
        )
    )
    отказ = ошибка(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=лица,
            key=ссылка,
            data={"ДатаРождения": негодная},
        )
    )

    операция = await стор.take(ответ["pending_id"], "sess-1")
    assert операция.request["json"] == {"ДатаРождения": "1980-05-01T00:00:00"}
    assert ответ["preview"][0]["after"] == годная
    assert "1980-05-01" not in json.dumps(ответ, ensure_ascii=False)
    assert "1990-01-01" not in json.dumps(ответ, ensure_ascii=False)
    assert отказ["code"] == "params_invalid" and "ДатаРождения" in отказ["message"]
    assert "1975-03-02" not in json.dumps(отказ, ensure_ascii=False)
    assert len(стор._ops) == 1


async def test_update_независимого_регистра_сведений(среда, одинс):
    """M3b задача 7 (проба P9-3, Ruling 60 закрыт этой поставкой): `update` записи независимого
    регистра сведений — тем же приёмом, что у справочника/документа: GET по составному ключу →
    PATCH изменённых полей-ресурсов. Путь составного ключа сторожит `odata1c_get`
    (test_tools_query), отпечаток без `DataVersion` — юнит-тесты `_отпечаток`
    (test_write_commit)."""
    запись, стор, _, _ = среда
    ключ = {"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА}
    одинс.объект(
        {"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА, "Курс": 90.5, "Кратность": 1}
    )

    ответ = json.loads(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity="InformationRegister_КурсыВалют",
            key=ключ,
            data={"Курс": 91.25},
        )
    )

    операция = await стор.take(ответ["pending_id"], "sess-1")
    assert операция.op == "update" and операция.request["json"] == {"Курс": 91.25}
    assert операция.key == ключ
    assert одинс.get.call_count == 1 and not одинс.писали


async def test_update_независимого_регистра_не_даёт_менять_поля_ключа(среда, одинс):
    """Поле ключа записи регистра в теле `update` — та же проверка, что у `Ref_Key` объекта
    (`_поле_тела`, `ключ=True`): значение не меняется через `update`, ключ передаётся аргументом
    `key`, а изменить его можно только `odata1c_delete_record` + `odata1c_create`."""
    запись, стор, _, _ = среда
    ключ = {"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА}
    одинс.объект(
        {"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА, "Курс": 90.5, "Кратность": 1}
    )

    отказ = ошибка(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity="InformationRegister_КурсыВалют",
            key=ключ,
            data={"Валюта_Key": "b103cb54-42ee-11ec-a7a0-f10ab59a067e"},
        )
    )

    assert отказ["code"] == "params_invalid"
    assert "ключ" in отказ["message"]
    assert стор._ops == {}


async def test_проведённый_и_уже_помеченный_документ_изменений_нет(среда, одинс):
    """Нужное состояние уже достигнуто — «изменений нет», а не «сначала Unpost»."""
    запись, _, _, _ = среда
    одинс.объект(документ(Posted=True, DeletionMark=True))

    отказ = ошибка(
        await запись.mark_for_deletion(
            SessionScope(), "sess-1", base="ut", entity=РЕАЛИЗАЦИЯ, key=ССЫЛКА_ДОК
        )
    )

    assert "изменений нет" in отказ["message"] and "Unpost" not in отказ["hint"]


async def test_объект_не_найден_при_чтении_текущего_состояния(среда, одинс):
    запись, _, _, _ = среда
    одинс.get.mock(
        return_value=httpx.Response(
            404,
            json={
                "odata.error": {
                    "code": "9",
                    "message": {"lang": "ru", "value": "Экземпляр сущности не найден"},
                }
            },
        )
    )

    текст = await запись.update(
        SessionScope(),
        "sess-1",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        key=ССЫЛКА,
        data={"ДополнительнаяИнформация": "x"},
    )

    assert ошибка(текст)["code"] == "object_not_found"
    assert not одинс.писали


# ---------------------------------------------------------------------------------------------
# Табличные части в update — замещение целиком, разница по строкам (M3b задача 6, проект §5.2)
# ---------------------------------------------------------------------------------------------

КИ = "Catalog_Контрагенты_КонтактнаяИнформация"
ДРУГОЙ_ТЕЛЕФОН = "+7 916 555-55-55"


async def test_line_number_нумеруется_шлюзом_в_update(среда, одинс):
    """Правило §5.1 (Ruling 104) действует у `update` тем же кодом, что у `create`: строки без
    LineNumber шлюз нумерует 1..n в порядке тела."""
    запись, стор, tools, _ = среда
    одинс.объект(контрагент())
    новый = токен(tools, ДРУГОЙ_ТЕЛЕФОН, entity=КИ, поле="Представление")
    # C-1 ревью хвостов M3b: поле строки класса `contact` («Представление») открытым литералом
    # `update` теперь отклоняет — тем же значением, но токеном (сама нумерация строк это не
    # затрагивает).
    почта = токен(tools, "new@example.com", entity=КИ, поле="Представление")

    ответ = json.loads(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=КОНТРАГЕНТЫ,
            key=ССЫЛКА,
            data={
                "КонтактнаяИнформация": [
                    {"Тип": "Телефон", "Представление": новый},
                    {"Тип": "АдресЭлектроннойПочты", "Представление": почта},
                ]
            },
        )
    )
    операция = await стор.take(ответ["pending_id"], "sess-1")

    строки = операция.request["json"]["КонтактнаяИнформация"]
    assert [строка["LineNumber"] for строка in строки] == [1, 2]


async def test_line_number_свои_номера_проверяются_в_update(среда, одинс):
    """Номера присутствуют у всех строк — обязаны быть ровно 1..n; отказ до GET (тело проверяется
    раньше запроса за текущим состоянием, как поля и типы)."""
    запись, стор, _, _ = среда

    текст = await запись.update(
        SessionScope(),
        "sess-1",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        key=ССЫЛКА,
        data={
            "КонтактнаяИнформация": [
                {"LineNumber": 1, "Тип": "Телефон", "Представление": ТЕЛЕФОН},
                {"LineNumber": 1, "Тип": "АдресЭлектроннойПочты", "Представление": ПОЧТА},
            ]
        },
    )

    отказ = ошибка(текст)
    assert отказ["code"] == "params_invalid"
    assert "подряд с 1" in отказ["message"]
    assert стор._ops == {} and not одинс.обращались


async def test_line_number_смешанные_в_update_отказ_до_1С(среда, одинс):
    """Часть строк с номером, часть без — шлюз не угадывает место ненумерованной строки."""
    запись, стор, _, _ = среда

    текст = await запись.update(
        SessionScope(),
        "sess-1",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        key=ССЫЛКА,
        data={
            "КонтактнаяИнформация": [
                {"LineNumber": 1, "Тип": "Телефон", "Представление": ТЕЛЕФОН},
                {"Тип": "АдресЭлектроннойПочты", "Представление": ПОЧТА},
            ]
        },
    )

    отказ = ошибка(текст)
    assert отказ["code"] == "params_invalid"
    assert "либо у всех строк" in отказ["message"]
    assert стор._ops == {} and not одинс.обращались


async def test_табличная_часть_замещается_целиком_разница_по_строкам_без_утечки(среда, одинс):
    """Проект §5.2, P9-6: тело `update` несёт табличную часть — модель передаёт ВСЕ строки,
    которые должны остаться, часть заменяется целиком. Превью — разница построчно
    (`rows_diff`/`rows_preview`): «было» — маска старой строки по сущности строки (Ruling 56),
    «станет» — показ значений модели (Ruling 62, 63). Ни одно реальное значение защищаемого поля
    (класс `contact` у «Представление») не показывается — только токен или пометка."""
    запись, стор, tools, _ = среда
    одинс.объект(контрагент())
    изменённый = токен(tools, ДРУГОЙ_ТЕЛЕФОН, entity=КИ, поле="Представление")
    неизменённый = токен(tools, ПОЧТА, entity=КИ, поле="Представление")
    # C-1 ревью хвостов M3b: поле строки класса `contact` открытым литералом `update` отклоняет —
    # даже у добавленной строки без пары в текущем состоянии; новое значение идёт токеном.
    новый_телефон = токен(tools, "+7 000 000-00-00", entity=КИ, поле="Представление")

    ответ = json.loads(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=КОНТРАГЕНТЫ,
            key=ССЫЛКА,
            data={
                "КонтактнаяИнформация": [
                    {"LineNumber": 1, "Тип": "Телефон", "Представление": изменённый},
                    {
                        "LineNumber": 2,
                        "Тип": "АдресЭлектроннойПочты",
                        "Представление": неизменённый,
                    },
                    {"LineNumber": 3, "Тип": "Телефон", "Представление": новый_телефон},
                ]
            },
        )
    )

    нет_реальных_значений(json.dumps(ответ, ensure_ascii=False))
    [часть] = [п for п in ответ["preview"] if п["field"] == "КонтактнаяИнформация"]
    assert часть["rows"]["before_count"] == 2
    assert часть["rows"]["after_count"] == 3
    # Строка 1 (класс `contact`, значение реально меняется) — в изменённых. Строка 2 (значение
    # прислано тем же токеном) тоже может попасть в изменённые: поле «Тип» — обычная строка без
    # класса, и «показ» отмечает ЛЮБОЙ присланный литерал пометкой независимо от того, совпадает
    # ли он с текущим значением (Ruling 62, 63 — анти-оракул: иначе по «есть/нет строки в
    # изменённых» модель узнавала бы, угадала ли она содержимое поля без единого обращения к 1С).
    # Проверяем то, что обязано быть верно всегда: строка 1 — среди изменённых, а поле
    # «Представление» строки 2 (класс `contact`, токен совпал буквально) в изменённых полях
    # строки 2 не значится — только «Тип», у которого класса нет.
    строки_изменённые = {с["line"]: с["fields"] for с in часть["rows"]["changed"]}
    assert 1 in строки_изменённые
    поля_строки_1 = {п["field"] for п in строки_изменённые[1]}
    assert "Представление" in поля_строки_1
    if 2 in строки_изменённые:
        поля_строки_2 = {п["field"] for п in строки_изменённые[2]}
        assert "Представление" not in поля_строки_2
    assert [с["line"] for с in часть["rows"]["added"]] == [3]
    assert часть["rows"]["removed"] == []
    assert часть["summary"] == "строк было 2, станет 3"

    операция = await стор.take(ответ["pending_id"], "sess-1")
    # PATCH несёт ПОЛНЫЙ новый список, а не только изменившиеся строки (проект §5.2), с реальными
    # значениями — включая строку 2, чьё «Представление» осталось прежним.
    строки_patch = операция.request["json"]["КонтактнаяИнформация"]
    assert [строка["LineNumber"] for строка in строки_patch] == [1, 2, 3]
    assert строки_patch[0]["Представление"] == ДРУГОЙ_ТЕЛЕФОН
    assert строки_patch[1]["Представление"] == ПОЧТА
    нет_реальных_значений(json.dumps(операция.preview, ensure_ascii=False))
    # Живая приёмка 2026-09-20: текст подтверждения (elicitation) читал у каждой строки превью
    # ключи `before`/`after`, а у табличной части их нет — `commit` падал `internal` ещё до 1С.
    from odata1c.write import service as слой_записи

    текст = слой_записи._текст_подтверждения(операция)
    assert "КонтактнаяИнформация: строк было 2, станет 3" in текст
    assert "строка 1" in текст and "добавлена строка 3" in текст
    нет_реальных_значений(текст)


async def test_табличная_часть_без_изменений_отказ_изменений_нет(среда, одинс):
    """Та же табличная часть целиком — не изменение, а отказ, как у полей шапки: часть строк
    может исчезать из результата, только если сама модель их не прислала."""
    запись, стор, tools, _ = среда
    одинс.объект(контрагент())
    ток_1 = токен(tools, ТЕЛЕФОН, entity=КИ, поле="Представление")
    ток_2 = токен(tools, ПОЧТА, entity=КИ, поле="Представление")

    текст = await запись.update(
        SessionScope(),
        "sess-1",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        key=ССЫЛКА,
        data={
            "КонтактнаяИнформация": [
                {"LineNumber": 1, "Тип": "Телефон", "Представление": ток_1},
                {"LineNumber": 2, "Тип": "АдресЭлектроннойПочты", "Представление": ток_2},
            ]
        },
    )

    отказ = ошибка(текст)
    assert отказ["code"] == "params_invalid" and "изменений нет" in отказ["message"]
    assert стор._ops == {}
    assert not одинс.писали


async def test_C1_литерал_в_строке_отклоняется_попадание_и_промах_одинаково(среда, одинс):
    """C-1 (Critical), ревью хвостов M3b: поле строки класса `contact` — открытым литералом,
    догадкой о значении — до правки поля СТРОК не проходили `check_open_literal` (только поля
    шапки), и «изменилась ли часть» решалось на реальных значениях: один бит на попытку — верная
    догадка отвечала «изменений нет», неверная готовила pending-операцию. Ровно тот оракул,
    который Ruling 45 закрыл для полей шапки и Ruling 35/37 — для отбора. Отказ обязан быть
    ОДИНАКОВЫМ для попадания и промаха и случиться ДО GET текущего состояния — заглушка 1С не
    видела ни одного обращения ни при попадании, ни при промахе."""
    запись, стор, tools, _ = среда
    одинс.объект(контрагент())
    ток_2 = токен(tools, ПОЧТА, entity=КИ, поле="Представление")

    def тело(литерал: str) -> dict:
        return {
            "КонтактнаяИнформация": [
                {"LineNumber": 1, "Тип": "Телефон", "Представление": литерал},
                {"LineNumber": 2, "Тип": "АдресЭлектроннойПочты", "Представление": ток_2},
            ]
        }

    попадание = await _отказ_записи(запись, КОНТРАГЕНТЫ, ССЫЛКА, тело(ТЕЛЕФОН))
    промах = await _отказ_записи(запись, КОНТРАГЕНТЫ, ССЫЛКА, тело("+7 000 111-22-33"))

    assert попадание["code"] == "filter_syntax" and попадание == промах
    assert "Представление" in попадание["message"]
    текст_попадания = json.dumps(попадание, ensure_ascii=False)
    assert ТЕЛЕФОН not in текст_попадания and "+7 000 111-22-33" not in текст_попадания
    assert стор._ops == {}
    assert not одинс.обращались


async def test_C1_усиленный_вариант_с_меняющимся_полем_шапки_тот_же_отказ(среда, одинс):
    """Усиленный вариант ревью: рядом с догадкой в строке — заведомо меняющееся поле шапки. До
    правки отказа не было вовсе: ответ отличал попадание от промаха присутствием табличной части
    среди `preview` (шапка меняется в обоих случаях, часть — только когда часть отвечает
    «изменений нет»). После правки — тот же отказ по открытому литералу, до GET, независимо от
    того, что ещё несёт тело."""
    запись, стор, tools, _ = среда
    одинс.объект(контрагент())
    ток_2 = токен(tools, ПОЧТА, entity=КИ, поле="Представление")

    отказ = await _отказ_записи(
        запись,
        КОНТРАГЕНТЫ,
        ССЫЛКА,
        {
            "НаименованиеПолное": "Совсем другое название",
            "КонтактнаяИнформация": [
                {"LineNumber": 1, "Тип": "Телефон", "Представление": ТЕЛЕФОН},
                {"LineNumber": 2, "Тип": "АдресЭлектроннойПочты", "Представление": ток_2},
            ],
        },
    )

    assert отказ["code"] == "filter_syntax"
    assert "Представление" in отказ["message"]
    assert стор._ops == {}
    assert not одинс.обращались


async def test_C1_токен_в_строке_по_прежнему_проходит(среда, одинс):
    """Регресс: строка табличной части ЦЕЛЫМ токеном (не открытым литералом) по-прежнему готовит
    pending-операцию — правка C-1 закрывает только открытый литерал класса `dob`/`contact`, не
    токен из ответа."""
    запись, стор, tools, _ = среда
    одинс.объект(контрагент())
    изменённый = токен(tools, ДРУГОЙ_ТЕЛЕФОН, entity=КИ, поле="Представление")
    неизменённый = токен(tools, ПОЧТА, entity=КИ, поле="Представление")

    ответ = json.loads(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=КОНТРАГЕНТЫ,
            key=ССЫЛКА,
            data={
                "КонтактнаяИнформация": [
                    {"LineNumber": 1, "Тип": "Телефон", "Представление": изменённый},
                    {
                        "LineNumber": 2,
                        "Тип": "АдресЭлектроннойПочты",
                        "Представление": неизменённый,
                    },
                ]
            },
        )
    )

    assert "pending_id" in ответ, ответ
    операция = await стор.take(ответ["pending_id"], "sess-1")
    assert операция.request["json"]["КонтактнаяИнформация"][0]["Представление"] == ДРУГОЙ_ТЕЛЕФОН


BASES_DENY_КИ = BASES_YAML.replace(
    "deny_fields: [", f"deny_entities: [{КИ}]\n      deny_fields: [", 1
)


@pytest.fixture
async def среда_запрет_ки(tmp_path, edmx_ut_real):
    assert "deny_entities" in BASES_DENY_КИ
    дом_ = _дом(tmp_path, edmx_ut_real, BASES_DENY_КИ)
    tools = ToolService(load_config(дом_))
    стор = PendingStore(600, clock=Часы(1000.0))
    журнал = functools.partial(Journal, tmp_path / "journal.sqlite")
    запись = WriteService(tools, стор, журнал, CommitLimiter(), clock=Часы(1_757_000_000.0))
    yield запись, стор, tools
    await tools.aclose()


async def test_deny_entities_табличной_части_не_обходится_через_update_владельца(
    среда_запрет_ки, одинс
):
    """Запрет записи в табличную часть (`permissions.deny_entities`) действует и на её строки в
    теле `update` владельца — тем же кодом, что у прямой записи и у `create` (Ruling 30): права
    строк проверяются до GET, отказ не обходится записью через владельца."""
    запись, стор, _ = среда_запрет_ки

    текст = await запись.update(
        SessionScope(),
        "sess-1",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        key=ССЫЛКА,
        data={"КонтактнаяИнформация": [{"Тип": "Телефон", "Представление": ТЕЛЕФОН}]},
    )

    отказ = ошибка(текст)
    assert отказ["code"] == "permission_denied"
    assert стор._ops == {} and not одинс.обращались


BASES_DENY_ПОЛЕ_СТРОКИ = BASES_YAML.replace(
    "deny_fields: [Catalog_Контрагенты.КодПоОКПО]",
    f"deny_fields: [Catalog_Контрагенты.КодПоОКПО, {КИ}.Регион]",
    1,
)


@pytest.fixture
async def среда_запрет_поля_строки(tmp_path, edmx_ut_real):
    assert BASES_DENY_ПОЛЕ_СТРОКИ != BASES_YAML
    дом_ = _дом(tmp_path, edmx_ut_real, BASES_DENY_ПОЛЕ_СТРОКИ)
    tools = ToolService(load_config(дом_))
    стор = PendingStore(600, clock=Часы(1000.0))
    журнал = functools.partial(Journal, tmp_path / "journal.sqlite")
    запись = WriteService(tools, стор, журнал, CommitLimiter(), clock=Часы(1_757_000_000.0))
    yield запись, стор, tools
    await tools.aclose()


async def test_deny_fields_строки_табличной_части_в_update(среда_запрет_поля_строки, одинс):
    """`permissions.deny_fields` строки табличной части (своё поле, не поле владельца) отказывает
    в теле `update` так же, как у `create` (шаг 6 SPEC §7.1)."""
    запись, стор, _ = среда_запрет_поля_строки

    текст = await запись.update(
        SessionScope(),
        "sess-1",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        key=ССЫЛКА,
        data={"КонтактнаяИнформация": [{"Тип": "Телефон", "Регион": "Москва"}]},
    )

    отказ = ошибка(текст)
    assert отказ["code"] == "field_write_denied"
    assert стор._ops == {} and not одинс.обращались


async def test_прямая_строка_табличной_части_в_update_отклоняется(среда, одинс):
    """У строки табличной части нет своего ключа в OData 1С — прямое изменение остаётся отказом
    (item 4 M3b задачи 6), независимо от того, что владелец теперь пишет часть целиком."""
    запись, стор, _, _ = среда

    текст = await запись.update(
        SessionScope(),
        "sess-1",
        base="ut",
        entity=КИ,
        key={"Ref_Key": ССЫЛКА, "LineNumber": "1"},
        data={"Представление": "x"},
    )

    отказ = ошибка(текст)
    assert отказ["code"] == "params_invalid"
    assert "владельца" in отказ["hint"]
    assert стор._ops == {} and not одинс.обращались


async def test_коллекция_без_сущности_строки_в_update_отклоняется_до_1С(среда, одинс):
    """`_табличные_части_тела` — общая функция `create` и `update` (задача 6): коллекция-поле,
    которой в индексе не находится собственной сущности строки (на живой УТ — `RecordSet`
    зависимых регистров), отказывает и через `update` тем же кодом, что и через `create` —
    сообщение не называет `create`, раз путь теперь общий."""
    запись, стор, _, _ = среда

    текст = await запись.update(
        SessionScope(),
        "sess-1",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        key=ССЫЛКА,
        data={"ИсторияКПП": []},
    )

    отказ = ошибка(текст)
    assert отказ["code"] == "params_invalid"
    assert "create" not in отказ["message"]
    assert стор._ops == {} and not одинс.обращались
    # Minor 3 ревью хвостов M3b: «уберите из тела» у update звучало бы советом потерять
    # существующую часть — подсказка нейтральна для обеих операций.
    assert "уберите" not in отказ["hint"]
    assert "ИсторияКПП" in отказ["hint"] and "не задаётся" in отказ["hint"]


# ---------------------------------------------------------------------------------------------
# Отказы до обращения к 1С: разрешения (SPEC §7.1), поля и типы
# ---------------------------------------------------------------------------------------------

ОТКАЗЫ_UPDATE = [
    pytest.param("ro", КОНТРАГЕНТЫ, ССЫЛКА, {"ИНН": НОВЫЙ_ИНН}, "base_read_only", id="write-false"),
    pytest.param(
        "ut", КОНТРАГЕНТЫ, ССЫЛКА, {"КодПоОКПО": "09226071"}, "field_write_denied", id="deny"
    ),
    pytest.param(
        "ut",
        "AccumulationRegister_ДвиженияДенежныеСредстваКонтрагент_RecordType",
        {"Recorder": ССЫЛКА_ДОК, "Recorder_Type": "StandardODATA.Document_X"},
        {"Контрагент": "x"},
        "permission_denied",
        id="регистр-без-register_direct_write",
    ),
    pytest.param(
        "ut",
        "InformationRegister_КурсыВалют_SliceLast",
        ССЫЛКА,
        {"Курс": 1.0},
        "permission_denied",
        id="виртуальная-таблица",
    ),
    pytest.param("ut", КОНТРАГЕНТЫ, ССЫЛКА, {}, "params_invalid", id="пустое-тело"),
    pytest.param("ut", РЕАЛИЗАЦИЯ, ССЫЛКА_ДОК, {"Posted": True}, "params_invalid", id="Posted"),
    pytest.param(
        "ut", КОНТРАГЕНТЫ, ССЫЛКА, {"DeletionMark": True}, "params_invalid", id="DeletionMark"
    ),
    pytest.param("ut", КОНТРАГЕНТЫ, ССЫЛКА, {"Ref_Key": ССЫЛКА}, "params_invalid", id="Ref_Key"),
    pytest.param(
        "ut", КОНТРАГЕНТЫ, ССЫЛКА, {"DataVersion": ВЕРСИЯ}, "params_invalid", id="DataVersion"
    ),
    pytest.param(
        "ut", КОНТРАГЕНТЫ, ССЫЛКА, {"Predefined": True}, "params_invalid", id="Predefined"
    ),
    pytest.param(
        "ut",
        КОНТРАГЕНТЫ,
        ССЫЛКА,
        {"PredefinedDataName": "x"},
        "params_invalid",
        id="PredefinedDataName",
    ),
    pytest.param(
        "ut", КОНТРАГЕНТЫ, ССЫЛКА, {"НетТакогоПоля": "x"}, "params_invalid", id="нет-поля"
    ),
    pytest.param(
        "ut",
        КОНТРАГЕНТЫ,
        ССЫЛКА,
        {"ИсторияКПП": []},
        "params_invalid",
        id="табличная-часть-полем",
    ),
    pytest.param(
        "ut",
        "Catalog_Контрагенты_КонтактнаяИнформация",
        {"Ref_Key": ССЫЛКА, "LineNumber": "1"},
        {"Представление": "x"},
        "params_invalid",
        id="строка-табличной-части",
    ),
    pytest.param(
        "ut",
        КОНТРАГЕНТЫ,
        ССЫЛКА,
        {"ГоловнойКонтрагент_Key": "не-гуид"},
        "params_invalid",
        id="не-GUID-в-_Key",
    ),
    pytest.param(
        "ut",
        КОНТРАГЕНТЫ,
        ССЫЛКА,
        {"НДСПоСтавкам4и2": "true"},
        "params_invalid",
        id="строка-в-Boolean",
    ),
    pytest.param(
        "ut",
        РЕАЛИЗАЦИЯ,
        ССЫЛКА_ДОК,
        {"СуммаДокумента": "1500"},
        "params_invalid",
        id="строка-в-Double",
    ),
    pytest.param(
        "ut",
        РЕАЛИЗАЦИЯ,
        ССЫЛКА_ДОК,
        {"СуммаДокумента": True},
        "params_invalid",
        id="bool-в-Double",
    ),
    pytest.param(
        "ut",
        "ChartOfCharacteristicTypes_СтатьиДоходов",
        ССЫЛКА,
        {"РеквизитДопУпорядочивания": 1.5},
        "params_invalid",
        id="дробное-в-Int64",
    ),
    pytest.param(
        "ut",
        РЕАЛИЗАЦИЯ,
        ССЫЛКА_ДОК,
        {"Date": "26.08.2026"},
        "params_invalid",
        id="дата-не-по-формату",
    ),
    pytest.param(
        "ut", РЕАЛИЗАЦИЯ, ССЫЛКА_ДОК, {"Комментарий": 5}, "params_invalid", id="число-в-String"
    ),
    pytest.param("ut", РЕАЛИЗАЦИЯ, ССЫЛКА_ДОК, {"Комментарий": None}, "params_invalid", id="null"),
    # M-1 ревью: `\d` совпадает с любой десятичной цифрой Unicode, а 1С такую дату не примет.
    pytest.param(
        "ut",
        РЕАЛИЗАЦИЯ,
        ССЫЛКА_ДОК,
        {"Date": "٢٠٢٦-٠١-٠١T٠٠:٠٠:٠٠"},
        "params_invalid",
        id="дата-арабскими-цифрами",
    ),
    # M-1 ревью: pydantic разбирает NaN и Infinity из JSON вызова, `json.dumps` пишет их в тело
    # PATCH как невалидный JSON — отказ пришёл бы от 1С уже после подтверждения.
    pytest.param(
        "ut", РЕАЛИЗАЦИЯ, ССЫЛКА_ДОК, {"СуммаДокумента": float("nan")}, "params_invalid", id="NaN"
    ),
    pytest.param(
        "ut", РЕАЛИЗАЦИЯ, ССЫЛКА_ДОК, {"СуммаДокумента": float("inf")}, "params_invalid", id="inf"
    ),
    pytest.param(
        "ut",
        РЕАЛИЗАЦИЯ,
        ССЫЛКА_ДОК,
        {"СуммаДокумента": float("-inf")},
        "params_invalid",
        id="-inf",
    ),
    # Ruling 45, находка I-1: открытый литерал даты рождения — отказ, как у отбора на чтении.
    pytest.param(
        "ut",
        "Catalog_ФизическиеЛица",
        ССЫЛКА,
        {"ДатаРождения": "1985-03-14T00:00:00"},
        "filter_syntax",
        id="dob-литерал",
    ),
    pytest.param(
        "ut", КОНТРАГЕНТЫ, "не-ключ", {"ИНН": НОВЫЙ_ИНН}, "params_invalid", id="ключ-не-GUID"
    ),
]


@pytest.mark.parametrize(("база", "сущность", "ключ", "данные", "код"), ОТКАЗЫ_UPDATE)
async def test_update_отказывает_до_обращения_к_1С(среда, одинс, база, сущность, ключ, данные, код):
    """Порядок SPEC §7.1 и проверки полей брифа (шаги 1–3) — ни одного обращения к 1С:
    неверный запрос не должен трогать базу даже чтением."""
    запись, стор, _, _ = среда

    текст = await запись.update(
        SessionScope(), "sess-1", base=база, entity=сущность, key=ключ, data=данные
    )

    assert ошибка(текст)["code"] == код
    assert not одинс.обращались
    assert стор._ops == {}


async def test_отказ_называет_поле_и_подсказывает_нужный_тул(среда, одинс):
    запись, _, _, _ = среда

    проведение = ошибка(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=РЕАЛИЗАЦИЯ,
            key=ССЫЛКА_ДОК,
            data={"Posted": True},
        )
    )
    пометка = ошибка(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=КОНТРАГЕНТЫ,
            key=ССЫЛКА,
            data={"DeletionMark": True},
        )
    )
    ссылка = ошибка(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=КОНТРАГЕНТЫ,
            key=ССЫЛКА,
            data={"ГоловнойКонтрагент": ССЫЛКА},
        )
    )

    assert "Posted" in проведение["message"] and "odata1c_action" in проведение["hint"]
    assert "DeletionMark" in пометка["message"]
    assert "odata1c_mark_for_deletion" in пометка["hint"]
    assert "ГоловнойКонтрагент_Key" in ссылка["hint"]


async def test_отказ_по_типу_не_повторяет_значение(среда, одинс):
    """Ruling 20, пункт 2: текст ошибки уходит модели тем же путём, что данные. Отказ называет
    поле и ожидаемый тип, но не само значение."""
    запись, _, _, _ = среда

    отказ = ошибка(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=РЕАЛИЗАЦИЯ,
            key=ССЫЛКА_ДОК,
            data={"СуммаДокумента": "секретная-строка-42"},
        )
    )

    assert "секретная-строка-42" not in json.dumps(отказ, ensure_ascii=False)
    assert "СуммаДокумента" in отказ["message"] and "Edm.Double" in отказ["message"]


def _скрыть(дом, имя: str) -> None:
    путь = policy_path(дом, "ut")
    политика = yaml.safe_load(путь.read_text(encoding="utf-8")) or {}
    политика.setdefault("entities", {})[имя] = {"hide": True}
    путь.write_text(yaml.safe_dump(политика, allow_unicode=True), encoding="utf-8")


async def test_скрытая_сущность_отказ_до_обращения_к_1С(среда, одинс, дом):
    запись, стор, _, _ = среда
    _скрыть(дом, КОНТРАГЕНТЫ)

    обновление = ошибка(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=КОНТРАГЕНТЫ,
            key=ССЫЛКА,
            data={"КодПоОКПО": "1"},
        )
    )
    пометка = ошибка(
        await запись.mark_for_deletion(
            SessionScope(), "sess-1", base="ut", entity=КОНТРАГЕНТЫ, key=ССЫЛКА
        )
    )

    # entity_hidden раньше field_write_denied: скрытая сущность отвечает так, будто её нет.
    assert обновление["code"] == "entity_hidden" and not обновление["hint"]
    assert пометка["code"] == "entity_hidden"
    assert not одинс.обращались
    assert стор._ops == {}


# ---------------------------------------------------------------------------------------------
# mark_for_deletion
# ---------------------------------------------------------------------------------------------


async def test_пометка_удаления_превью_нет_да_и_тело_PATCH(среда, одинс):
    запись, стор, _, _ = среда
    одинс.объект(контрагент())

    текст = await запись.mark_for_deletion(
        SessionScope(), "sess-1", base="ut", entity=КОНТРАГЕНТЫ, key=ССЫЛКА
    )

    нет_реальных_значений(текст)
    ответ = json.loads(текст)
    assert ответ["op"] == "mark_for_deletion"
    assert ответ["preview"] == [{"field": "DeletionMark", "before": False, "after": True}]
    assert ответ["summary"] == "пометка удаления: нет → да"
    assert ответ["base"] == "ut" and ответ["role"] == "prod"
    assert ответ["key"] == ССЫЛКА and ответ["object"]["Description"].startswith("[[org:")
    операция = await стор.take(ответ["pending_id"], "sess-1")
    assert операция.request == {
        "method": "PATCH",
        "path": f"{КОНТРАГЕНТЫ}(guid'{ССЫЛКА}')",
        "json": {"DeletionMark": True},
    }
    assert операция.op == "mark_for_deletion" and операция.data_version == ВЕРСИЯ
    assert одинс.get.call_count == 1 and not одинс.писали


async def test_снятие_пометки_удаления(среда, одинс):
    запись, _, _, _ = среда
    одинс.объект(контрагент(DeletionMark=True))

    ответ = json.loads(
        await запись.mark_for_deletion(
            SessionScope(), "sess-1", base="ut", entity=КОНТРАГЕНТЫ, key=ССЫЛКА, mark=False
        )
    )

    assert ответ["preview"] == [{"field": "DeletionMark", "before": True, "after": False}]
    assert ответ["summary"] == "пометка удаления: да → нет"


async def test_пометка_уже_стоит_изменений_нет(среда, одинс):
    запись, стор, _, _ = среда
    одинс.объект(контрагент(DeletionMark=True))

    отказ = ошибка(
        await запись.mark_for_deletion(
            SessionScope(), "sess-1", base="ut", entity=КОНТРАГЕНТЫ, key=ССЫЛКА
        )
    )

    assert отказ["code"] == "params_invalid" and "изменений нет" in отказ["message"]
    assert стор._ops == {}


async def test_пометка_проведённого_документа_отказ_при_подготовке(среда, одинс):
    """Решение 12 плана, факт P8: 1С отвечает 500 на пометку проведённого документа. Отказ —
    при подготовке, с подсказкой «сначала Unpost», а не после подтверждения пользователя."""
    запись, стор, _, _ = среда
    # Поддельная 1С соблюдает `$select` (M-2): без `Posted` в выборе поле не пришло бы, и отказ
    # молча не случился бы — 1С ответила бы 500 уже на `commit`.
    одинс.объект(документ(Posted=True), по_выбору=True)

    отказ = ошибка(
        await запись.mark_for_deletion(
            SessionScope(), "sess-1", base="ut", entity=РЕАЛИЗАЦИЯ, key=ССЫЛКА_ДОК
        )
    )

    assert отказ["code"] == "params_invalid"
    assert "Unpost" in отказ["hint"]
    assert стор._ops == {}
    assert not одинс.писали
    выбор = одинс.get.calls.last.request.url.params["$select"].split(",")
    assert sorted(выбор) == sorted(["DeletionMark", "DataVersion", "Posted", "Number", "Date"])


async def test_пометка_документа_называет_его_номером_и_датой(среда, одинс):
    """Ruling 44: документ называется `Number` и `Date` — они инвариантом 6 не защищаются и в
    превью идут как есть, маской того же вызова, что «было»."""
    запись, _, _, _ = среда
    одинс.объект(документ(), по_выбору=True)

    ответ = json.loads(
        await запись.mark_for_deletion(
            SessionScope(), "sess-1", base="ut", entity=РЕАЛИЗАЦИЯ, key=ССЫЛКА_ДОК
        )
    )

    assert ответ["object"] == {"Number": "УТ-000711", "Date": "2026-08-26T12:00:00"}
    assert ответ["key"] == ССЫЛКА_ДОК


ОТКАЗЫ_ПОМЕТКИ = [
    pytest.param("nomark", КОНТРАГЕНТЫ, ССЫЛКА, True, "permission_denied", id="mark_deletion-off"),
    pytest.param("ro", КОНТРАГЕНТЫ, ССЫЛКА, True, "base_read_only", id="write-false"),
    pytest.param(
        "ut",
        "InformationRegister_КурсыВалют",
        {"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА},
        True,
        # M3b задача 7: у записи независимого регистра сведений нет `DeletionMark` вовсе — шаг 3
        # разрешений отвечает `params_invalid` (не запрет, а неверный запрос), а не
        # `permission_denied`, и делает это раньше любого обращения к 1С.
        "params_invalid",
        id="регистр",
    ),
    pytest.param(
        "ut",
        "InformationRegister_КурсыВалют_SliceLast",
        ССЫЛКА,
        True,
        "permission_denied",
        id="виртуальная-таблица",
    ),
    pytest.param(
        "ut",
        "Catalog_Контрагенты_КонтактнаяИнформация",
        {"Ref_Key": ССЫЛКА, "LineNumber": "1"},
        True,
        "params_invalid",
        id="строка-табличной-части",
    ),
    pytest.param("ut", КОНТРАГЕНТЫ, ССЫЛКА, "да", "params_invalid", id="mark-не-bool"),
]


@pytest.mark.parametrize(("база", "сущность", "ключ", "пометка", "код"), ОТКАЗЫ_ПОМЕТКИ)
async def test_пометка_отказывает_до_обращения_к_1С(
    среда, одинс, база, сущность, ключ, пометка, код
):
    запись, стор, _, _ = среда

    текст = await запись.mark_for_deletion(
        SessionScope(), "sess-1", base=база, entity=сущность, key=ключ, mark=пометка
    )

    assert ошибка(текст)["code"] == код
    assert not одинс.обращались
    assert стор._ops == {}


# ---------------------------------------------------------------------------------------------
# diff_preview
# ---------------------------------------------------------------------------------------------


def test_diff_preview_строка_на_каждое_поле_в_порядке_тела():
    до = {"ИНН": "[[inn:A]]", "Комментарий": "было"}
    после = {"ИНН": "[[inn:B]]", "Комментарий": "стало"}

    assert diff_preview(до, после) == [
        {"field": "ИНН", "before": "[[inn:A]]", "after": "[[inn:B]]"},
        {"field": "Комментарий", "before": "было", "after": "стало"},
    ]


def test_diff_preview_не_выбрасывает_поле_с_равными_значениями():
    """Изменённость решает вызывающий на реальных значениях; превью показывает всё, что ему
    передали, — и строку с совпадающими «было» и «станет» тоже (после стража литерал модели,
    известный словарю, выглядит тем же токеном, что «было»)."""
    [строка] = diff_preview({"ИНН": "[[inn:A]]"}, {"ИНН": "[[inn:A]]"})

    assert строка == {"field": "ИНН", "before": "[[inn:A]]", "after": "[[inn:A]]"}


# ---------------------------------------------------------------------------------------------
# Ruling 45: подготовка не пишет в общий словарь гейта (C-1, I-1 ревью задачи 5)
# ---------------------------------------------------------------------------------------------

БАНК = "Catalog_БанковскиеСчетаКонтрагентов"

СЛУЧАИ_СЛОВАРЯ = [
    pytest.param(
        КОНТРАГЕНТЫ,
        ССЫЛКА,
        "контрагент",
        {
            "ИНН": НОВЫЙ_ИНН,
            "Description": "ООО Тверская Плаза",
            "НаименованиеПолное": "Общество с ограниченной ответственностью «Тверская Плаза»",
            "КПП": "773601001",
        },
        id="новые-литералы-защищённых-полей",
    ),
    pytest.param(
        БАНК,
        ССЫЛКА,
        "банк",
        {"ТелефоныБанка": "0000711"},
        id="телефон-без-контрольной-суммы",
    ),
    pytest.param(
        РЕАЛИЗАЦИЯ,
        ССЫЛКА_ДОК,
        "документ",
        {"Комментарий": "позвонить 0000711, ИНН 7728168971, +7 495 123-45-67"},
        id="поле-без-класса-с-реквизитами-в-тексте",
    ),
]


def _объект_случая(вид: str) -> dict:
    if вид == "контрагент":
        return контрагент()
    if вид == "банк":
        return {
            "Ref_Key": ССЫЛКА,
            "DataVersion": ВЕРСИЯ,
            "Description": "Расчётный счёт",
            "ТелефоныБанка": "+7 495 700-00-00",
        }
    return документ()


@pytest.mark.parametrize(("сущность", "ключ", "вид", "данные"), СЛУЧАИ_СЛОВАРЯ)
async def test_подготовка_не_меняет_словарь(среда, одинс, сущность, ключ, вид, данные):
    """Главная проверка Ruling 45 — от состояния словаря, а не от ответа: число строк во всех
    таблицах `gate.sqlite` до и после подготовки одинаково. Объект перед подготовкой прочитан
    через шлюз (так модель и узнаёт его токены), поэтому «было» — маска данных 1С — ничего
    нового не добавляет; «станет» — литералы модели — не маскируется вовсе. Операция при этом
    подготовлена: проверяется путь, который дошёл до конца, а не отказ."""
    запись, _, tools, _ = среда
    одинс.объект(_объект_случая(вид))
    прочитано = json.loads(await tools.get(SessionScope(), base="ut", entity=сущность, key=ключ))
    assert "item" in прочитано
    до = строки_словаря(tools)

    ответ = json.loads(
        await запись.update(
            SessionScope(), "sess-1", base="ut", entity=сущность, key=ключ, data=данные
        )
    )

    assert "pending_id" in ответ, ответ
    assert строки_словаря(tools) == до
    assert not одинс.писали


async def test_отказ_по_открытой_дате_рождения_как_у_чтения_и_без_следа(среда, одинс):
    """I-1: открытый литерал даты рождения — отказ тем же кодом и текстом, что у отбора на
    чтении (один предикат `open_literal_refusal`), до GET. Промах и попадание неразличимы — ответ
    не зависит от текущего значения, его даже не читали; токена догадки нет (A1, A2 ревью)."""
    запись, стор, tools, _ = среда
    лица = "Catalog_ФизическиеЛица"
    гейт = tools._gate_for(tools._config.bases["ut"])
    до = строки_словаря(tools)

    отказы = [
        ошибка(
            await запись.update(
                SessionScope(),
                "sess-1",
                base="ut",
                entity=лица,
                key=ССЫЛКА,
                data={"ДатаРождения": догадка},
            )
        )
        for догадка in ("1985-03-14T00:00:00", "1985-03-15T00:00:00")
    ]
    with _строение_индекса(tools) as строение, pytest.raises(GateError) as на_чтении:
        гейт.inbound_filter(
            "ДатаРождения eq datetime'1985-03-14T00:00:00'",
            entity=лица,
            revealed=RevealedValues(),
            shape=строение,
        )

    образец = open_literal_refusal("dob", "ДатаРождения")
    assert отказы[0] == {"code": "filter_syntax", "message": str(образец), "hint": образец.hint}
    # Ruling 51: литерал в тексте не повторяется — промах и попадание дают один ответ.
    assert отказы[1] == отказы[0]
    assert (на_чтении.value.code, str(на_чтении.value), на_чтении.value.hint) == (
        "filter_syntax",
        str(образец),
        образец.hint,
    )
    assert all("[[dob:" not in json.dumps(отказ, ensure_ascii=False) for отказ in отказы)
    assert not одинс.обращались
    assert стор._ops == {}
    assert строки_словаря(tools) == до


@pytest.mark.parametrize("вид", ["смешан", "обрезан"])
async def test_токен_внутри_литерала_даты_рождения_как_у_чтения(среда, одинс, вид):
    """Строка с токеном внутри или его обрезком в поле `dob` — `token_partial` тем же текстом,
    что у отбора на чтении, а не общий отказ по открытой дате: на чтении точный код стоит раньше
    (`_проверить_литерал_текстом`), и запись не должна отвечать на тот же вопрос иначе. До GET."""
    запись, стор, tools, _ = среда
    лица = "Catalog_ФизическиеЛица"
    гейт = tools._gate_for(tools._config.bases["ut"])
    целый = токен(tools, "1980-05-01T00:00:00", entity=лица, поле="ДатаРождения")
    значение = f"{целый} тут" if вид == "смешан" else целый[:-2]
    до = строки_словаря(tools)

    отказ = ошибка(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=лица,
            key=ССЫЛКА,
            data={"ДатаРождения": значение},
        )
    )
    with _строение_индекса(tools) as строение, pytest.raises(GateError) as на_чтении:
        гейт.inbound_filter(
            f"ДатаРождения eq '{значение}'",
            entity=лица,
            revealed=RevealedValues(),
            shape=строение,
        )

    assert на_чтении.value.code == "token_partial"
    assert отказ == {
        "code": "token_partial",
        "message": str(на_чтении.value),
        "hint": на_чтении.value.hint,
    }
    assert not одинс.обращались
    assert стор._ops == {}
    assert строки_словаря(tools) == до


# Воспроизведения ревью (A3a–A3d, A5) — в обратной форме: после Ruling 45 дефекта нет.


async def test_A3a_неподтверждённое_название_не_становится_вариантом(среда, одинс):
    запись, стор, tools, _ = среда
    одинс.объект(контрагент())
    await запись.update(
        SessionScope(),
        "s",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        key=ССЫЛКА,
        data={"Description": "Тверская Плаза"},
    )
    assert len(стор._ops) == 1 and not одинс.писали

    assert "тверская плаза" not in tools._dictionary.name_variants()
    одинс.объект(документ(Комментарий="доставка в Тверская Плаза, 2 этаж"))
    текст = await tools.get(
        SessionScope(), base="ut", entity=РЕАЛИЗАЦИЯ, key=ССЫЛКА_ДОК, select=["Комментарий"]
    )
    assert "Тверская Плаза" in текст


async def test_A3b_подготовка_не_делает_токен_неоднозначным(среда, одинс):
    запись, _, tools, _ = среда
    ток = токен(tools, ИНН)

    одинс.объект(контрагент(ИНН=ЧУЖОЙ_ИНН))
    await запись.update(
        SessionScope(),
        "s",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        key=ССЫЛКА,
        data={"ИНН": ИНН_С_ПРОБЕЛОМ},
    )

    одинс.объект(контрагент(ИНН=ЧУЖОЙ_ИНН))
    после = json.loads(
        await запись.update(
            SessionScope(), "s", base="ut", entity=КОНТРАГЕНТЫ, key=ССЫЛКА, data={"ИНН": ток}
        )
    )
    assert "pending_id" in после, после


async def test_A3c_подготовка_не_снимает_защиту_ядра_названия(среда, одинс):
    """Ruling 115: голое «Ромашка» без кавычек в свободном тексте больше не ищется вовсе, и
    комментарий с ним не был бы защищён уже на первой проверке. Узнаваемое написание — в
    кавычках («звонили из «Ромашка» по упд 711») — ключ `"ромашка"` (с формой из
    `name_variants_of`), который правило не снимает. По той же причине проверка неоднозначности
    смотрит на ключ `"ромашка"`, а не на голый — голого ключа для этого названия в словаре
    больше не бывает."""
    запись, стор, tools, _ = среда
    токен(tools, НАЗВАНИЕ, поле="Description")

    async def комментарий() -> str:
        одинс.объект(документ(Комментарий="звонили из «Ромашка» по упд 711"))
        return await tools.get(
            SessionScope(), base="ut", entity=РЕАЛИЗАЦИЯ, key=ССЫЛКА_ДОК, select=["Комментарий"]
        )

    assert "Ромашка" not in await комментарий()
    одинс.объект(контрагент(Description="Лютик"))
    await запись.update(
        SessionScope(),
        "s",
        base="ut",
        entity=КОНТРАГЕНТЫ,
        key=ССЫЛКА,
        data={"Description": "Ромашка"},
    )
    assert len(стор._ops) == 1 and not одинс.писали

    assert '"ромашка"' not in tools._dictionary.ambiguous_name_variants()
    assert "Ромашка" not in await комментарий()


async def test_A3d_подготовка_не_портит_номер_документа(среда, одинс):
    запись, стор, tools, _ = среда
    одинс.объект({"Ref_Key": ССЫЛКА, "DataVersion": ВЕРСИЯ, "ТелефоныБанка": ""})
    await запись.update(
        SessionScope(),
        "s",
        base="ut",
        entity=БАНК,
        key=ССЫЛКА,
        data={"ТелефоныБанка": "0000711"},
    )
    assert len(стор._ops) == 1 and not одинс.писали

    одинс.объект(документ(Number="УТ-0000711", Комментарий="по вх упд 0000711"))
    текст = await tools.get(
        SessionScope(),
        base="ut",
        entity=РЕАЛИЗАЦИЯ,
        key=ССЫЛКА_ДОК,
        select=["Number", "Комментарий"],
    )
    assert "УТ-0000711" in текст and "[[phone:" not in текст


async def test_A5_новое_название_в_превью_литералом_без_нового_токена(среда, одинс):
    """Новое название — в «станет» без токена: номер токена не говорит, было ли название в
    словаре, и словарь не пополняется. Литерал не повторяется — пометка класса (Ruling 62): прежний
    оракул Ruling 45 п. 4 (известный словарю литерал страж заменял токеном) закрыт."""
    запись, _, tools, _ = среда
    только_ut = SessionScope(bases=("ut",), default="ut")
    одинс.объект(контрагент())
    await tools.get(только_ut, base="ut", entity=КОНТРАГЕНТЫ, key=ССЫЛКА)
    до = строки_словаря(tools)

    ответ = json.loads(
        await запись.update(
            только_ut,
            "s",
            base="ut",
            entity=КОНТРАГЕНТЫ,
            key=ССЫЛКА,
            data={"Description": "ООО Заведомо Новое Имя 1"},
        )
    )

    assert ответ["preview"][0]["after"] == "значение из запроса (класс org)"
    assert строки_словаря(tools) == до
    assert "заведомо новое имя 1" not in tools._dictionary.name_variants()


# ---------------------------------------------------------------------------------------------
# M-3: строка текущего состояния, переписанная ранним проходом (`ScrubbedText`)
# ---------------------------------------------------------------------------------------------

# В `ut-real.edmx` ветка недостижима: у update по GUID набор раскрытого на момент GET пуст. Нужен
# ключ с защищаемым значением — синтетический независимый регистр со строковым измерением `ИНН`.
# Модель передаёт ключ токеном, гейт раскрывает его для пути запроса, и ранний проход клиента
# переписывает это значение во ВСЕХ строках ответа GET — в том числе в поле тела `Комментарий`.
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
    якорь_типа = '<EntityType Name="InformationRegister_КурсыВалют">'
    якорь_набора = '<EntitySet Name="InformationRegister_КурсыВалют"'
    assert якорь_типа in текст and якорь_набора in текст
    текст = текст.replace(якорь_типа, _ТИП_РЕГИСТРА_ИНН + якорь_типа, 1)
    текст = текст.replace(якорь_набора, _НАБОР_РЕГИСТРА_ИНН + якорь_набора, 1)
    home = _дом(tmp_path, текст.encode("utf-8"))
    tools = ToolService(load_config(home))
    стор = PendingStore(600, clock=Часы(1000.0))
    # Журнал — фабрикой: `commit` открывает его на вызов (задача 7); подготовка его не зовёт.
    журнал = functools.partial(Journal, tmp_path / "journal.sqlite")
    yield WriteService(tools, стор, журнал, CommitLimiter(), clock=Часы(1.0)), стор, tools
    await tools.aclose()


async def test_строка_раннего_прохода_в_current_разворачивается(среда_синт, одинс):
    """`update` записи синтетического регистра с ключом-ИНН — единственный путь, где набор
    раскрытого непуст уже к моменту GET (ключ пришёл токеном, гейт раскрыл его для пути запроса).
    Ранний проход клиента переписывает совпадения этого значения во ВСЕХ строках ответа, включая
    поле тела `Комментарий»: без разворота `_текущее_реальное` строка с тем же текстом, что и
    прислала модель, выглядела бы «изменённой» (в ней токен вместо ИНН, а не сам ИНН). M3b
    (задача 7) открыла запись независимого регистра сведений — раньше (Ruling 60) этот путь
    `update` был недостижим и тест проверял только отказ до GET; юнит на саму функцию —
    `test_текущее_реальное_разворачивает_строку_раннего_прохода` ниже, гейтовый —
    `test_текущее_после_раннего_прохода_разворачивается_через_набор` (test_gate_spellings)."""
    запись, стор, tools = среда_синт
    ток = токен(tools, ИНН, entity=РЕГИСТР_ИНН)
    одинс.объект({"ИНН": ИНН, "Комментарий": f"ИНН {ИНН} проверен"})

    отказ = ошибка(
        await запись.update(
            SessionScope(),
            "s",
            base="ut",
            entity=РЕГИСТР_ИНН,
            key={"ИНН": ток},
            data={"Комментарий": f"ИНН {ИНН} проверен"},
        )
    )

    # Тот же текст, что уже лежит в записи (реально, под токеном в ответе GET), — «изменений
    # нет»: без разворота ScrubbedText сравнение считало бы поле изменённым всегда.
    assert отказ["code"] == "params_invalid" and "изменений нет" in отказ["message"]
    assert стор._ops == {}


def test_текущее_реальное_разворачивает_строку_раннего_прохода():
    """Изменённость сравнивается с ИСХОДНЫМ значением переписанной строки: строка раннего прохода
    несёт на месте раскрытого значения токен, исходное знает только набор этого вызова; чужой
    набор его не знает — строка сравнивается как есть (безопасная сторона)."""
    набор = RevealedValues()
    исходный = f"ИНН {ИНН} проверен"
    строка = набор.scrubbed("ИНН [[inn:ABCDEFGHJK]] проверен", original=исходный, hits=1)

    assert service._текущее_реальное(строка, набор) == исходный
    assert service._текущее_реальное(строка, RevealedValues()) == строка
    assert service._текущее_реальное("как есть", набор) == "как есть"


# Ruling 48 (Н-1 повторного ревью): словарь пополняется только из тела ответа 1С с данными.
# Текст отказа — гейта и 1С — маскируется токеном без записи. Литералы — такие, что их находит
# детектор (телефон, ИНН с верной контрольной суммой): чистая дата прошлого раунда детектору не
# видна, и тест словаря дефекта не замечал. После Ruling 51 собственные отказы шлюза значение не
# повторяют вовсе, и эхо остаётся только у 1С (P7): токен без записи проверяется на нём.

ЛИЦА = "Catalog_ФизическиеЛица"
ЭХО_ТЕЛЕФОН = "звоните +7 916 000-07-11"
# Десять цифр с верной контрольной суммой ИНН — и правдоподобный номер документа 1С (B2).
ЦИФРЫ_ИНН = "0000000716"
ЭХО_ИНН = f"ИНН {ЦИФРЫ_ИНН}"


def _токены(текст: str) -> list[str]:
    return re.findall(r"\[\[[a-z]+:[^\]]+\]\]", текст)


@pytest.mark.parametrize(
    ("литерал", "класс"), [(ЭХО_ТЕЛЕФОН, "phone"), (ЭХО_ИНН, "inn")], ids=["телефон", "ИНН"]
)
async def test_отказ_записи_с_эхом_реквизита_словарь_не_пополняет(среда, одинс, литерал, класс):
    """B1: отказ Ruling 45 по открытой дате с реквизитом в литерале словарь не пополняет. После
    Ruling 51 литерала в тексте нет вовсе — ни открыто, ни токеном."""
    запись, стор, tools, _ = среда
    до = строки_словаря(tools)

    отказ = ошибка(
        await запись.update(
            SessionScope(), "s", base="ut", entity=ЛИЦА, key=ССЫЛКА, data={"ДатаРождения": литерал}
        )
    )

    assert отказ["code"] == "filter_syntax"
    assert not any(т.startswith(f"[[{класс}:") for т in _токены(json.dumps(отказ)))
    assert строки_словаря(tools) == до
    assert not одинс.обращались and стор._ops == {}


async def test_отказ_по_контрольной_сумме_после_GET_словарь_не_пополняет(среда, одинс):
    """B1b: отказ `inbound_write` по контрольной сумме — после GET, тот же путь."""
    запись, стор, tools, _ = среда
    одинс.объект(контрагент())
    до = строки_словаря(tools)

    отказ = ошибка(
        await запись.update(
            SessionScope(),
            "s",
            base="ut",
            entity=КОНТРАГЕНТЫ,
            key=ССЫЛКА,
            data={"ИНН": "1 +7 916 000-07-12"},
        )
    )

    assert "[[phone:" not in отказ["message"] and "000-07-12" not in отказ["message"]
    assert строки_словаря(tools) == до
    assert стор._ops == {}


async def test_отказ_отбора_на_чтении_с_эхом_словарь_не_пополняет(среда, одинс):
    """Тот же канал на чтении (старше задачи 5): отказ отбора повторял литерал."""
    _, _, tools, _ = среда
    до = строки_словаря(tools)

    отказ = ошибка(
        await tools.query(
            SessionScope(), base="ut", entity=ЛИЦА, filter=f"ДатаРождения eq '{ЭХО_ИНН}'"
        )
    )

    assert "[[inn:" not in отказ["message"] and ЦИФРЫ_ИНН not in отказ["message"]
    assert строки_словаря(tools) == до
    assert not одинс.обращались


async def test_ошибка_1С_с_эхом_литерала_на_чтении_словарь_не_пополняет(среда, одинс):
    """Эхо P7: 1С повторяет выражение отбора в тексте ошибки. Поле без класса — литерал уходит в
    1С и возвращается в ошибке; телефон в нём получает токен без записи."""
    _, _, tools, _ = среда
    одинс.get.mock(side_effect=эхо_отбора)
    до = строки_словаря(tools)

    отказ = ошибка(
        await tools.query(
            SessionScope(), base="ut", entity=РЕАЛИЗАЦИЯ, filter=f"Комментарий eq '{ЭХО_ТЕЛЕФОН}'"
        )
    )

    assert одинс.get.called
    assert "[[phone:" in отказ["message"] and "000-07-11" not in отказ["message"]
    assert строки_словаря(tools) == до


async def test_ошибка_1С_с_эхом_на_подготовке_записи_словарь_не_пополняет(среда, одинс):
    """Ошибка 1С на GET текущего состояния с реквизитом в тексте — тот же путь `gate.error`."""
    запись, стор, tools, _ = среда
    одинс.объект(
        {"odata.error": {"code": "6", "message": {"lang": "ru", "value": f"сбой: {ЭХО_ИНН}"}}},
        status=400,
    )
    до = строки_словаря(tools)

    отказ = ошибка(
        await запись.update(
            SessionScope(),
            "s",
            base="ut",
            entity=РЕАЛИЗАЦИЯ,
            key=ССЫЛКА_ДОК,
            data={"Комментарий": "новый"},
        )
    )

    assert "[[inn:" in отказ["message"] and ЦИФРЫ_ИНН not in отказ["message"]
    assert строки_словаря(tools) == до
    assert стор._ops == {}


async def _эхо_1С(tools: ToolService, одинс, литерал: str) -> dict:
    """Ошибка 1С, повторяющая литерал отбора по полю без класса (эхо P7): литерал уходит в 1С и
    возвращается в тексте ошибки через `gate.error`."""
    одинс.get.mock(side_effect=эхо_отбора)
    return ошибка(
        await tools.query(
            SessionScope(), base="ut", entity=РЕАЛИЗАЦИЯ, filter=f"Комментарий eq '{литерал}'"
        )
    )


async def test_токен_эха_известного_значения_прежний(среда, одинс):
    """Известное значение в другом написании получает в эхе свой прежний токен, а словарь — ни
    второго написания, ни строки."""
    _, _, tools, _ = среда
    т = токен(tools, "+7 495 700-00-00", entity=БАНК, поле="ТелефоныБанка")
    до = строки_словаря(tools)

    отказ = await _эхо_1С(tools, одинс, "звоните +7 (495) 700-00-00")

    assert т in _токены(отказ["message"])
    assert строки_словаря(tools) == до


async def test_токен_эха_нового_значения_тот_же_что_у_данных_1С(среда, одинс):
    """Сухой токен эха — тот, что выдаст обычная маска, когда значение придёт данными 1С; но
    пока оно не пришло, токен не раскрывается: отбор по нему — `token_unknown`."""
    _, _, tools, _ = среда
    [эхо] = _токены((await _эхо_1С(tools, одинс, ЭХО_ТЕЛЕФОН))["message"])
    одинс.get.reset()

    по_эху = ошибка(
        await tools.query(
            SessionScope(), base="ut", entity=БАНК, filter=f"ТелефоныБанка eq '{эхо}'"
        )
    )
    assert по_эху["code"] == "token_unknown"
    assert not одинс.get.called

    assert эхо == токен(tools, "+7 916 000-07-11", entity=БАНК, поле="ТелефоныБанка")


async def test_B2_обратно_эхо_не_портит_номер_документа(среда, одинс):
    """B2 ревью в обратной форме: эхо ИНН-подобных цифр в ошибке 1С не делает номер документа
    токеном `inn` в следующем чтении (инвариант 6)."""
    _, _, tools, _ = среда
    assert "[[inn:" in (await _эхо_1С(tools, одинс, ЭХО_ИНН))["message"]

    одинс.объект(документ(Number=ЦИФРЫ_ИНН))
    ответ = json.loads(
        await tools.get(
            SessionScope(), base="ut", entity=РЕАЛИЗАЦИЯ, key=ССЫЛКА_ДОК, select=["Number"]
        )
    )

    assert ответ["item"]["Number"] == ЦИФРЫ_ИНН
    assert not any("guard_replaced" in п for п in ответ.get("warnings", []))


async def test_B4_обратно_эхо_не_делает_токен_неоднозначным(среда, одинс):
    """B4 ревью в обратной форме: эхо того же номера в другом написании в ошибке 1С не кладёт
    второе написание, и запись токеном в поле без своих написаний проходит."""
    запись, _, tools, _ = среда
    т = токен(tools, "+7 495 700-00-00", entity=БАНК, поле="ТелефоныБанка")

    async def запись_токеном() -> dict:
        одинс.объект({"Ref_Key": ССЫЛКА, "DataVersion": ВЕРСИЯ, "ТелефоныБанкаДляРасчетов": ""})
        return json.loads(
            await запись.update(
                SessionScope(),
                "s",
                base="ut",
                entity=БАНК,
                key=ССЫЛКА,
                data={"ТелефоныБанкаДляРасчетов": т},
            )
        )

    assert "pending_id" in await запись_токеном()
    assert т in (await _эхо_1С(tools, одинс, "звоните +7 (495) 700-00-00"))["message"]
    assert "pending_id" in await запись_токеном()


# М-3 повторного ревью: очистка даты рождения.


async def test_пустая_дата_1С_в_поле_dob_не_открытый_литерал(среда, одинс):
    """Пустая дата 1С `0001-01-01T00:00:00` — очистка поля, а не догадка о дате: без исключения
    очистить дату рождения через `update` было бы нельзя. «Было» — токеном, «станет» — как
    прислано."""
    запись, стор, tools, _ = среда
    одинс.объект({"Ref_Key": ССЫЛКА, "DataVersion": ВЕРСИЯ, "ДатаРождения": "1990-01-01T00:00:00"})

    ответ = json.loads(
        await запись.update(
            SessionScope(),
            "s",
            base="ut",
            entity=ЛИЦА,
            key=ССЫЛКА,
            data={"ДатаРождения": "0001-01-01T00:00:00"},
        )
    )

    assert "pending_id" in ответ, ответ
    [строка] = ответ["preview"]
    assert строка["before"].startswith("[[dob:") and строка["after"] == "0001-01-01T00:00:00"
    нет_реальных_значений(json.dumps(ответ, ensure_ascii=False))
    assert "1990-01-01" not in json.dumps(ответ, ensure_ascii=False)
    [операция] = стор._ops.values()
    assert операция.request["json"] == {"ДатаРождения": "0001-01-01T00:00:00"}


async def test_null_в_поле_dob_отклоняет_проверка_типа_а_не_правило_dob(среда, одинс):
    """`null` правило открытого литерала `dob` не задевает (это не строка); его отклоняет общая
    проверка типа с подсказкой на пустую дату — ей очистка и делается (см. тест выше)."""
    запись, _, _, _ = среда

    отказ = ошибка(
        await запись.update(
            SessionScope(), "s", base="ut", entity=ЛИЦА, key=ССЫЛКА, data={"ДатаРождения": None}
        )
    )

    assert отказ["code"] == "params_invalid"
    assert "0001-01-01T00:00:00" in отказ["hint"]
    assert not одинс.обращались


# Ruling 49: написания, которые путь ошибки успел записать до Ruling 48 (`field='error'`), шлюз не
# учитывает нигде. «Старый» словарь — тот же вызов, которым писал прежний `gate.error`.


def _след_ошибки(tools: ToolService, класс: str, значение: str) -> str:
    return tools._dictionary.token_for(класс, значение, base="ut", entity="", field="error")


async def test_B4_на_старом_словаре_написание_из_ошибки_не_делает_токен_неоднозначным(среда, одинс):
    запись, стор, tools, _ = среда
    т = токен(tools, "+7 495 700-00-00", entity=БАНК, поле="ТелефоныБанка")
    assert _след_ошибки(tools, "phone", "+7 (495) 700-00-00") == т
    одинс.объект({"Ref_Key": ССЫЛКА, "DataVersion": ВЕРСИЯ, "ТелефоныБанкаДляРасчетов": ""})

    ответ = json.loads(
        await запись.update(
            SessionScope(),
            "s",
            base="ut",
            entity=БАНК,
            key=ССЫЛКА,
            data={"ТелефоныБанкаДляРасчетов": т},
        )
    )

    assert "pending_id" in ответ, ответ
    [операция] = стор._ops.values()
    assert операция.request["json"] == {"ТелефоныБанкаДляРасчетов": "+7 495 700-00-00"}


async def test_отбор_по_токену_не_берёт_написание_из_ошибки(среда, одинс):
    """Группа написаний отбора (поле без своих написаний — все написания словаря) — без
    написания пути ошибки: в `or` 1С не уходит написание, которого в 1С нет."""
    _, _, tools, _ = среда
    т = токен(tools, "+7 495 700-00-00", entity=БАНК, поле="ТелефоныБанка")
    _след_ошибки(tools, "phone", "+7 (495) 700-00-00")
    одинс.объект({"value": []})

    ответ = json.loads(
        await tools.query(
            SessionScope(), base="ut", entity=БАНК, filter=f"ТелефоныБанкаДляРасчетов eq '{т}'"
        )
    )

    assert "error" not in ответ, ответ
    отбор = одинс.get.calls.last.request.url.params["$filter"]
    assert "+7 495 700-00-00" in отбор and "(495)" not in отбор


async def test_токен_только_из_ошибок_token_unknown_на_чтении_и_записи(среда, одинс):
    запись, стор, tools, _ = среда
    т = _след_ошибки(tools, "inn", ЦИФРЫ_ИНН)

    по_отбору = ошибка(
        await tools.query(SessionScope(), base="ut", entity=КОНТРАГЕНТЫ, filter=f"ИНН eq '{т}'")
    )
    assert по_отбору["code"] == "token_unknown"
    assert not одинс.обращались
    одинс.объект(контрагент())
    по_телу = ошибка(
        await запись.update(
            SessionScope(), "s", base="ut", entity=КОНТРАГЕНТЫ, key=ССЫЛКА, data={"ИНН": т}
        )
    )

    assert по_телу["code"] == "token_unknown"
    assert not одинс.писали
    assert стор._ops == {}


async def test_B2_на_старом_словаре_номер_документа_цел(среда, одинс):
    """Токен только из ошибок не входит и в множество стража: номер документа с теми же цифрами
    в ответе остаётся номером (инвариант 6)."""
    _, _, tools, _ = среда
    _след_ошибки(tools, "inn", ЦИФРЫ_ИНН)
    одинс.объект(документ(Number=ЦИФРЫ_ИНН))

    ответ = json.loads(
        await tools.get(
            SessionScope(), base="ut", entity=РЕАЛИЗАЦИЯ, key=ССЫЛКА_ДОК, select=["Number"]
        )
    )

    assert ответ["item"]["Number"] == ЦИФРЫ_ИНН
    assert not any("guard_replaced" in п for п in ответ.get("warnings", []))


# Ruling 51 (Н-2 ревью раунда 3): собственные отказы шлюза по входному значению значение не
# повторяют. Иначе эхо проходит маску текста и страж, и верная догадка модели о телефоне или почте
# возвращается ровно тем токеном, что модель видела при чтении, — подтверждение без 1С, в обход
# Ruling 35. Метка — телефон, известный словарю фикстуры: в ответе не должно быть ни её, ни её
# цифр, ни её токена, ни токена телефона вообще.

МЕТКА = f"звоните {ТЕЛЕФОН}"
КУРСЫ = "InformationRegister_КурсыВалют"


def _без_метки(ответ: dict, т: str) -> None:
    текст = json.dumps(ответ, ensure_ascii=False)
    assert ТЕЛЕФОН not in текст and ЦИФРЫ_ТЕЛЕФОНА not in текст, текст
    assert т not in текст and "[[phone:" not in текст, текст


def _метка_в_словаре(tools: ToolService) -> str:
    return токен(tools, ТЕЛЕФОН, entity=БАНК, поле="ТелефоныБанка")


async def _отказ_записи(запись, entity: str, key, data: dict) -> dict:
    return ошибка(
        await запись.update(SessionScope(), "s", base="ut", entity=entity, key=key, data=data)
    )


async def _отказ_чтения(tools, entity: str, **аргументы) -> dict:
    return ошибка(await tools.query(SessionScope(), base="ut", entity=entity, **аргументы))


async def test_Н2_на_записи_отказ_по_дате_рождения_не_подтверждает_телефон(среда, одинс):
    """Н-2 ревьюера: догадка о телефоне в поле `dob` — промах и попадание дают один и тот же
    ответ, без токена телефона."""
    запись, _, tools, _ = среда
    т = _метка_в_словаре(tools)

    попадание = await _отказ_записи(запись, ЛИЦА, ССЫЛКА, {"ДатаРождения": МЕТКА})
    промах = await _отказ_записи(запись, ЛИЦА, ССЫЛКА, {"ДатаРождения": "звоните +7 916 123-45-68"})

    assert попадание["code"] == "filter_syntax" and попадание == промах
    assert "ДатаРождения" in попадание["message"] and "токеном" in попадание["hint"]
    _без_метки(попадание, т)
    assert not одинс.обращались


async def test_Н2_на_чтении_отказ_отбора_по_дате_рождения_не_подтверждает_телефон(среда, одинс):
    _, _, tools, _ = среда
    т = _метка_в_словаре(tools)

    попадание = await _отказ_чтения(tools, ЛИЦА, filter=f"ДатаРождения eq '{МЕТКА}'")
    промах = await _отказ_чтения(tools, ЛИЦА, filter="ДатаРождения eq 'звоните +7 916 123-45-68'")

    assert попадание["code"] == "filter_syntax" and попадание == промах
    _без_метки(попадание, т)
    assert not одинс.обращались


ОТКАЗЫ_ЧТЕНИЯ_С_МЕТКОЙ = [
    pytest.param(
        ЛИЦА, {"filter": f"ДатаРождения eq '[[dob:ABCD {МЕТКА}'"}, "token_partial", id="обрезок"
    ),
    pytest.param(
        КОНТРАГЕНТЫ,
        {"filter": f"ИНН eq '{МЕТКА} [[inn:ABCDEFGHJK]]'"},
        "token_partial",
        id="токен-в-тексте",
    ),
    pytest.param(
        КОНТРАГЕНТЫ, {"filter": f"ИНН eq '1 {ТЕЛЕФОН}'"}, "filter_syntax", id="контрольная-сумма"
    ),
    pytest.param(
        КОНТРАГЕНТЫ,
        {"filter": f"Description eq 'x' and '{МЕТКА}'"},
        "filter_syntax",
        id="литерал-без-поля",
    ),
    pytest.param(
        ЛИЦА,
        {"filter": f"year(ДатаРождения) eq {ЦИФРЫ_ТЕЛЕФОНА}"},
        "filter_syntax",
        id="функция-над-dob",
    ),
    pytest.param(
        f"{КОНТРАГЕНТЫ}_КонтактнаяИнформация",
        {"filter": f"Представление eq '{ТЕЛЕФОН}'"},
        "filter_syntax",
        id="контактная-информация",
    ),
    pytest.param(КОНТРАГЕНТЫ, {"top": МЕТКА}, "params_invalid", id="top-не-число"),
    pytest.param(КОНТРАГЕНТЫ, {"top": -int(ЦИФРЫ_ТЕЛЕФОНА)}, "params_invalid", id="top-меньше-0"),
    pytest.param(КОНТРАГЕНТЫ, {"skip": -int(ЦИФРЫ_ТЕЛЕФОНА)}, "params_invalid", id="skip-меньше-0"),
]


@pytest.mark.parametrize(("сущность", "аргументы", "код"), ОТКАЗЫ_ЧТЕНИЯ_С_МЕТКОЙ)
async def test_входной_отказ_чтения_не_повторяет_значение(среда, одинс, сущность, аргументы, код):
    _, _, tools, _ = среда
    т = _метка_в_словаре(tools)

    отказ = await _отказ_чтения(tools, сущность, **аргументы)

    assert отказ["code"] == код, отказ
    _без_метки(отказ, т)
    assert not одинс.обращались


async def test_входные_отказы_записи_не_повторяют_значение(среда, одинс):
    """Тело записи: открытая дата рождения, токен внутри литерала, контрольная сумма (после GET)."""
    запись, _, tools, _ = среда
    т = _метка_в_словаре(tools)

    отказы = [
        await _отказ_записи(запись, ЛИЦА, ССЫЛКА, {"ДатаРождения": f"[[dob:ABCD {МЕТКА}"}),
        await _отказ_записи(запись, ЛИЦА, ССЫЛКА, {"ДатаРождения": f"[[dob:ABCDEFGHJK]] {МЕТКА}"}),
    ]
    одинс.объект(контрагент())
    отказы.append(await _отказ_записи(запись, КОНТРАГЕНТЫ, ССЫЛКА, {"ИНН": f"1 {ТЕЛЕФОН}"}))

    assert [о["code"] for о in отказы] == ["token_partial", "token_partial", "filter_syntax"]
    for отказ in отказы:
        _без_метки(отказ, т)
    assert "ДатаРождения" in отказы[0]["message"] and "ИНН" in отказы[2]["message"]
    assert not одинс.писали


async def test_висячий_литерал_при_защищаемом_поле_не_повторяется(среда, одинс):
    """Вторая линия Ruling 16: число без поля в условии с защищаемым полем. Число с цифрами
    известного телефона страж заменил бы токеном — тот же оракул через слой цифр."""
    _, _, tools, _ = среда
    т = _метка_в_словаре(tools)
    инн = токен(tools, ИНН)

    отказ = await _отказ_чтения(tools, КОНТРАГЕНТЫ, filter=f"(ИНН eq '{инн}') eq {ЦИФРЫ_ТЕЛЕФОНА}")

    assert отказ["code"] == "filter_syntax" and "ИНН" in отказ["message"]
    _без_метки(отказ, т)
    assert not одинс.обращались


async def test_обрезок_токена_в_имени_поля_ключа_не_повторяется(среда, одинс):
    _, _, tools, _ = среда
    т = _метка_в_словаре(tools)
    ключ = {"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА, f"[[inn:AB {МЕТКА}": "x"}

    отказ = ошибка(await tools.get(SessionScope(), base="ut", entity=КУРСЫ, key=ключ))

    _без_метки(отказ, т)
    assert not одинс.обращались


# Ruling 53: отказы по несуществующему имени имя не повторяют — оно вход модели, и эхо прошло бы
# маску текста или страж (у отказов без гейта — `guard_only`). Метка — цифры телефона из словаря
# фикстуры, вписанные в имя: слой цифр стража заменил бы их токеном. Имена из индекса (найденная
# сущность, подсказки похожих) повторяются.

ИМЯ_С_МЕТКОЙ = f"Поле{ЦИФРЫ_ТЕЛЕФОНА}"


async def _отказ(вызов) -> dict:
    return ошибка(await вызов)


ОТКАЗЫ_ПО_ИМЕНИ = [
    pytest.param(
        lambda tools: tools.query(SessionScope(), base="ut", entity=f"Catalog_{ЦИФРЫ_ТЕЛЕФОНА}"),
        "entity_unknown",
        id="сущность",
    ),
    pytest.param(
        lambda tools: tools.describe_entity(
            SessionScope(), base="ut", entity=f"Catalog_{ЦИФРЫ_ТЕЛЕФОНА}"
        ),
        "entity_unknown",
        id="сущность-describe",
    ),
    pytest.param(
        lambda tools: tools.query(
            SessionScope(), base="ut", entity=КОНТРАГЕНТЫ, filter=f"ИНН{ЦИФРЫ_ТЕЛЕФОНА} gt 'x'"
        ),
        "filter_syntax",
        id="поле-отбора",
    ),
    pytest.param(
        lambda tools: tools.query(
            SessionScope(),
            base="ut",
            entity=КОНТРАГЕНТЫ,
            filter=f"Контрагент/ИНН{ЦИФРЫ_ТЕЛЕФОНА} gt 'x'",
        ),
        "filter_syntax",
        id="путь-отбора",
    ),
    pytest.param(
        lambda tools: tools.raw_get(
            SessionScope(),
            base="ut",
            path=f"{КОНТРАГЕНТЫ}(guid'{ССЫЛКА}')/{ИМЯ_С_МЕТКОЙ}/{ИМЯ_С_МЕТКОЙ}",
        ),
        "entity_unknown",
        id="сегменты-raw_get",
    ),
    pytest.param(
        lambda tools: tools.raw_get(
            SessionScope(), base="ut", path=КОНТРАГЕНТЫ, query={ИМЯ_С_МЕТКОЙ: {"a": 1}}
        ),
        "params_invalid",
        id="параметр-raw_get",
    ),
    pytest.param(
        lambda tools: tools.raw_get(
            SessionScope(), base="ut", path=КОНТРАГЕНТЫ, query={ИМЯ_С_МЕТКОЙ: "[[inn:X]]"}
        ),
        "params_invalid",
        id="токен-в-параметре-raw_get",
    ),
    pytest.param(
        lambda tools: tools.get(
            SessionScope(),
            base="ut",
            entity=КУРСЫ,
            key={"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА, ИМЯ_С_МЕТКОЙ: "x"},
        ),
        "params_invalid",
        id="лишнее-поле-ключа",
    ),
    pytest.param(
        lambda tools: tools.query(
            SessionScope(), base="ut", entity=КОНТРАГЕНТЫ, expand=ИМЯ_С_МЕТКОЙ
        ),
        "params_invalid",
        id="навигация-expand",
    ),
    pytest.param(
        lambda tools: tools.query(
            SessionScope(),
            base="ut",
            entity=f"{КУРСЫ}_SliceLast",
            params={ИМЯ_С_МЕТКОЙ: 1},
        ),
        "params_invalid",
        id="параметр-виртуальной-таблицы",
    ),
    pytest.param(
        lambda tools: tools.recipe(SessionScope(), base="ut", name=ИМЯ_С_МЕТКОЙ),
        "recipe_unknown",
        id="рецепт",
    ),
    pytest.param(lambda tools: tools.info(topic=ИМЯ_С_МЕТКОЙ), "params_invalid", id="тема-info"),
    pytest.param(
        lambda tools: tools.query(SessionScope(), base=ИМЯ_С_МЕТКОЙ, entity=КОНТРАГЕНТЫ),
        "base_unknown",
        id="база",
    ),
]


@pytest.mark.parametrize(("вызов", "код"), ОТКАЗЫ_ПО_ИМЕНИ)
async def test_отказ_по_имени_не_повторяет_имя(среда, одинс, вызов, код):
    _, _, tools, _ = среда
    т = _метка_в_словаре(tools)

    отказ = await _отказ(вызов(tools))

    assert отказ["code"] == код, отказ
    _без_метки(отказ, т)
    assert not одинс.обращались


async def test_отказ_по_полю_тела_не_повторяет_имя(среда, одинс):
    запись, _, tools, _ = среда
    т = _метка_в_словаре(tools)

    отказ = await _отказ_записи(запись, КОНТРАГЕНТЫ, ССЫЛКА, {ИМЯ_С_МЕТКОЙ: "x"})

    assert отказ["code"] == "params_invalid" and КОНТРАГЕНТЫ in отказ["message"]
    _без_метки(отказ, т)
    assert not одинс.обращались


async def test_подсказки_имён_из_индекса_остаются(среда, одинс):
    """Опечатка в настоящем имени по-прежнему получает подсказку — именами из индекса."""
    запись, _, tools, _ = среда

    сущность = await _отказ(tools.query(SessionScope(), base="ut", entity="Catalog_Контрагент"))
    поле = await _отказ_записи(запись, КОНТРАГЕНТЫ, ССЫЛКА, {"ИННН": "x"})
    навигация = await _отказ(
        tools.query(SessionScope(), base="ut", entity=РЕАЛИЗАЦИЯ, expand="Контрагнет")
    )

    assert сущность["code"] == "entity_unknown"
    assert "Catalog_Контрагенты" in сущность["hint"]
    assert "odata1c_find_entity" in сущность["message"]
    assert "Catalog_Контрагент»" not in json.dumps(сущность, ensure_ascii=False)
    assert "ИНН" in поле["hint"] and "ИННН" not in json.dumps(поле, ensure_ascii=False)
    assert "Контрагент" in навигация["hint"] and "Контрагнет" not in json.dumps(
        навигация, ensure_ascii=False
    )


async def test_имя_поля_из_индекса_в_отказе_отбора_остаётся(среда, одинс):
    """Поле, которое индекс знает, отказ называет — это метаданные базы, а не ввод модели."""
    _, _, tools, _ = среда

    отказ = await _отказ_чтения(tools, КОНТРАГЕНТЫ, filter="ИНН gt 'x'")

    assert отказ["code"] == "filter_syntax" and "«ИНН»" in отказ["message"]


# Ruling 54 (Н-4 ревью раунда 4): ни один отказ шлюза не повторяет ввод модели, в том числе токены.
# Токен в тексте отказа проходил слой цифр стража: хвост из цифр, известных словарю, становился
# токеном телефона, а неизвестные цифры возвращались как есть — разница подтверждала членство.
# Отказ называет порядковый номер токена и поле, если индекс его знает.

ХВОСТ_ИЗВЕСТНЫЙ = ЦИФРЫ_ТЕЛЕФОНА
ХВОСТ_НЕИЗВЕСТНЫЙ = "99999999999"


def _без_хвоста(текст: str, т: str) -> None:
    """Ни хвоста, ни токена метки, ни токена телефона, ни `guard_replaced` — ответ не зависит от
    того, известны ли словарю цифры хвоста."""
    данные = json.loads(текст)
    assert ХВОСТ_ИЗВЕСТНЫЙ not in текст and ХВОСТ_НЕИЗВЕСТНЫЙ not in текст, текст
    assert т not in текст and "[[phone:" not in текст, текст
    assert not any("guard_replaced" in п for п in данные.get("warnings", [])), текст


async def _пара(вызов) -> tuple[str, str]:
    """Один и тот же вызов с хвостом из известных словарю цифр и из неизвестных."""
    return await вызов(ХВОСТ_ИЗВЕСТНЫЙ), await вызов(ХВОСТ_НЕИЗВЕСТНЫЙ)


СЛУЧАИ_ТОКЕНА = [
    pytest.param(
        lambda tools, _: (
            lambda хвост: tools.query(
                SessionScope(), base="ut", entity=КОНТРАГЕНТЫ, filter=f"ИНН eq '[[inn:{хвост}]]'"
            )
        ),
        "token_unknown",
        id="неизвестный-в-отборе",
    ),
    pytest.param(
        lambda tools, _: (
            lambda хвост: tools.query(
                SessionScope(), base="ut", entity=КОНТРАГЕНТЫ, filter=f"ИНН eq '[[phone:{хвост}]]'"
            )
        ),
        "token_type_mismatch",
        id="чужой-класс-в-отборе",
    ),
    pytest.param(
        lambda tools, _: (
            lambda хвост: tools.query(
                SessionScope(), base="ut", entity=КОНТРАГЕНТЫ, filter=f"ИНН eq '[[a{хвост}:AB]]'"
            )
        ),
        "token_type_mismatch",
        id="цифры-в-классе",
    ),
    pytest.param(
        lambda tools, _: (
            lambda хвост: tools.get(
                SessionScope(),
                base="ut",
                entity=КУРСЫ,
                key={"Period": "2026-01-01T00:00:00", "Валюта_Key": f"[[inn:{хвост}]]"},
            )
        ),
        "token_type_mismatch",
        id="ключ",
    ),
    pytest.param(
        lambda _, запись: (
            lambda хвост: запись.update(
                SessionScope(),
                "s",
                base="ut",
                entity=КОНТРАГЕНТЫ,
                key=ССЫЛКА,
                data={"ИНН": f"[[inn:{хвост}]]"},
            )
        ),
        "token_unknown",
        id="тело-записи",
    ),
]


@pytest.mark.parametrize(("вызов", "код"), СЛУЧАИ_ТОКЕНА)
async def test_отказ_по_токену_не_зависит_от_цифр_хвоста(среда, одинс, вызов, код):
    запись, _, tools, _ = среда
    т = _метка_в_словаре(tools)
    одинс.объект(контрагент())

    известный, неизвестный = await _пара(вызов(tools, запись))

    assert ошибка(известный)["code"] == код, известный
    assert известный.replace(ХВОСТ_ИЗВЕСТНЫЙ, "") == неизвестный.replace(ХВОСТ_НЕИЗВЕСТНЫЙ, "")
    assert известный == неизвестный
    _без_хвоста(известный, т)
    assert not одинс.писали


async def test_неизвестный_токен_назван_порядковым_номером_и_полем(среда, одинс):
    """Несколько токенов в отборе: отказ называет, какой по счёту неизвестен, и поле из индекса."""
    _, _, tools, _ = среда
    инн = токен(tools, ИНН)

    отказ = ошибка(
        await tools.query(
            SessionScope(),
            base="ut",
            entity=КОНТРАГЕНТЫ,
            filter=f"ИНН eq '{инн}' or ИНН eq '[[inn:{ХВОСТ_НЕИЗВЕСТНЫЙ}]]'",
        )
    )

    assert отказ["code"] == "token_unknown"
    assert "второй токен в отборе" in отказ["message"] and "«ИНН»" in отказ["message"]
    assert инн not in json.dumps(отказ, ensure_ascii=False)
    assert not одинс.обращались


async def test_token_ambiguous_не_повторяет_токен(среда, одинс):
    запись, _, tools, _ = среда
    ток = токен(tools, ИНН)
    assert токен(tools, ИНН_С_ПРОБЕЛОМ) == ток
    одинс.объект(контрагент(ИНН=ЧУЖОЙ_ИНН))

    отказ = ошибка(
        await запись.update(
            SessionScope(), "s", base="ut", entity=КОНТРАГЕНТЫ, key=ССЫЛКА, data={"ИНН": ток}
        )
    )

    assert отказ["code"] == "token_ambiguous"
    assert ток not in отказ["message"] and "[[" not in отказ["message"]
    assert "первый токен в теле записи" in отказ["message"] and "«ИНН»" in отказ["message"]


# ---------------------------------------------------------------------------------------------
# Н-5, Н-6: путь `raw_get` — те же правила отбора, что у `query`, и ни байта пути в ответе
# ---------------------------------------------------------------------------------------------

ДОКУМЕНТЫ_ЛИЦ = "InformationRegister_ДокументыФизическихЛиц"
СРЕЗ_ДОКУМЕНТОВ = f"{ДОКУМЕНТЫ_ЛИЦ}_SliceLast"
ПОДСТРОКА_СЕРИИ = "substringof('45', Серия)"

# Три формы ревью: каждую `query` с `params` отклоняет правилами отбора (SPEC §6.7), и ровно их
# путь `raw_get` отправлял в 1С как есть.
УСЛОВИЯ_ОРАКУЛА = [
    pytest.param(ПОДСТРОКА_СЕРИИ, id="подстрока-серии"),
    pytest.param(
        "Физлицо/ДатаРождения eq datetime'1990-01-01T00:00:00'", id="открытая-дата-рождения"
    ),
    pytest.param("length(Серия) eq 4", id="функция-над-серией"),
]


def _в_путь(выражение: str) -> str:
    """Строковый литерал OData в пути: апостроф внутри удваивается."""
    return выражение.replace("'", "''")


def _отказ_пути_raw_get(отказ: dict) -> None:
    assert отказ["code"] == "params_invalid", отказ
    assert "odata1c_query" in отказ["hint"] and "params" in отказ["hint"], отказ


async def _отказ_raw_get(tools, path: str, query: dict | None = None) -> dict:
    return ошибка(await tools.raw_get(SessionScope(), base="ut", path=path, query=query))


@pytest.mark.parametrize("условие", УСЛОВИЯ_ОРАКУЛА)
async def test_Н6_условие_оракула_отклоняется_и_в_query_и_в_пути_raw_get(среда, одинс, условие):
    _, _, tools, _ = среда
    одинс.get.mock(return_value=httpx.Response(200, json={"value": []}))

    через_query = await _отказ_чтения(tools, СРЕЗ_ДОКУМЕНТОВ, params={"Condition": условие})
    через_путь = await _отказ_raw_get(
        tools, f"{ДОКУМЕНТЫ_ЛИЦ}/SliceLast(Condition='{_в_путь(условие)}')"
    )

    assert через_query["code"] == "filter_syntax"
    _отказ_пути_raw_get(через_путь)
    assert not одинс.get.called


# Написания той же виртуальной таблицы с аргументами в пути. Шлюз их не разбирает и не угадывает,
# какое из них 1С примет: любой аргумент в скобках после первого сегмента — отказ.
ПУТИ_С_АРГУМЕНТАМИ = [
    pytest.param("{т}/slicelast(Condition='{у}')", id="регистр-имени"),
    pytest.param("{т}/SliceLast (Condition='{у}')", id="пробел-перед-скобкой"),
    pytest.param("{т}/SliceLast( Condition = '{у}' )", id="пробелы-внутри"),
    pytest.param("{т}/SliceFirst(Condition='{у}')", id="другая-таблица"),
    pytest.param("{т}/SliceLast(Period=datetime'2026-01-01T00:00:00',Condition='{у}')", id="два"),
    pytest.param("{т}/SliceLast(Condition='{у} or Номер eq ''a/b''')", id="косая-в-кавычках"),
    pytest.param("{т}/SliceLast(condition='{у}')", id="регистр-параметра"),
    pytest.param("{т}/НеизвестноеДействие(Условие='{у}')", id="неизвестное-действие"),
    pytest.param("{т}_SliceLast(Condition='{у}')", id="имя-из-индекса-первым-сегментом"),
]


@pytest.mark.parametrize("шаблон", ПУТИ_С_АРГУМЕНТАМИ)
async def test_Н6_аргументы_в_пути_raw_get_отклоняются_до_1С(среда, одинс, шаблон):
    _, _, tools, _ = среда
    одинс.get.mock(return_value=httpx.Response(200, json={"value": []}))
    путь = шаблон.format(т=ДОКУМЕНТЫ_ЛИЦ, у=_в_путь(ПОДСТРОКА_СЕРИИ))

    отказ = await _отказ_raw_get(tools, путь)

    assert отказ["code"] == "params_invalid", отказ
    assert "45" not in json.dumps(отказ, ensure_ascii=False)
    assert not одинс.get.called


async def test_Н6_процентная_запись_пути_отклоняется_до_1С(среда, одинс):
    _, _, tools, _ = среда
    одинс.get.mock(return_value=httpx.Response(200, json={"value": []}))
    условие = urllib.parse.quote(f"Condition='{_в_путь(ПОДСТРОКА_СЕРИИ)}'", safe="")

    for путь in (
        f"{ДОКУМЕНТЫ_ЛИЦ}/SliceLast({условие})",
        f"{ДОКУМЕНТЫ_ЛИЦ}/SliceLast%28{условие}%29",
    ):
        отказ = await _отказ_raw_get(tools, путь)
        assert отказ["code"] == "params_invalid", отказ

    assert not одинс.get.called


async def test_Н6_токен_в_условии_раскрывается_в_query_а_путь_raw_get_отклоняется(среда, одинс):
    """Правила одинаковы для обоих путей: токен в `Condition` через `query` раскрывается гейтом, а
    тот же `Condition` в пути `raw_get` не уходит в 1С вовсе — ни токеном, ни значением."""
    _, _, tools, _ = среда
    серия = токен(tools, "4512", entity=ДОКУМЕНТЫ_ЛИЦ, поле="Серия")
    assert серия.startswith("[[doc:"), серия
    условие = f"Серия eq '{серия}'"
    одинс.get.mock(return_value=httpx.Response(200, json={"value": []}))

    ответ = json.loads(
        await tools.query(
            SessionScope(), base="ut", entity=СРЕЗ_ДОКУМЕНТОВ, params={"Condition": условие}
        )
    )
    assert "error" not in ответ, ответ
    отправлено = urllib.parse.unquote(str(одинс.get.calls[-1].request.url))
    assert "4512" in отправлено and серия not in отправлено
    вызовов = одинс.get.call_count

    отказ = await _отказ_raw_get(tools, f"{ДОКУМЕНТЫ_ЛИЦ}/SliceLast(Condition='{условие}')")

    _отказ_пути_raw_get(отказ)
    assert серия not in json.dumps(отказ, ensure_ascii=False)
    assert одинс.get.call_count == вызовов


@pytest.mark.parametrize(
    "имя", ["Condition", "condition", " Condition ", "AccountCondition", "BalancedAccountCondition"]
)
async def test_Н6_условие_в_query_raw_get_отклоняется_до_1С(среда, одинс, имя):
    """Параметр действия OData v3 может прийти и строкой запроса — `SliceLast?Condition=…`."""
    _, _, tools, _ = среда
    одинс.get.mock(return_value=httpx.Response(200, json={"value": []}))

    отказ = await _отказ_raw_get(tools, f"{ДОКУМЕНТЫ_ЛИЦ}/SliceLast", {имя: ПОДСТРОКА_СЕРИИ})

    _отказ_пути_raw_get(отказ)
    assert not одинс.get.called


@pytest.mark.parametrize("имя", ["$Filter", "$FILTER", " $filter", "$filter ", "filter"])
async def test_Н6_написание_имени_filter_не_обходит_гейт(среда, одинс, имя):
    """Имена параметров `raw_get` сравнивались дословно: `$Filter` уходил в 1С мимо разбора."""
    _, _, tools, _ = среда
    одинс.get.mock(return_value=httpx.Response(200, json={"value": []}))

    отказ = await _отказ_raw_get(tools, ДОКУМЕНТЫ_ЛИЦ, {имя: ПОДСТРОКА_СЕРИИ})

    assert отказ["code"] == "filter_syntax", отказ
    assert not одинс.get.called


async def test_Н6_написание_имени_filter_приводится_к_каноническому(среда, одинс):
    _, _, tools, _ = среда
    одинс.get.mock(return_value=httpx.Response(200, json={"value": []}))

    ответ = json.loads(
        await tools.raw_get(
            SessionScope(), base="ut", path=ДОКУМЕНТЫ_ЛИЦ, query={"$Filter": "Номер eq '1'"}
        )
    )

    assert "error" not in ответ, ответ
    параметры = одинс.get.calls[-1].request.url.params
    assert параметры.get("$filter") == "Номер eq '1'" and "$Filter" not in параметры


@pytest.mark.parametrize(
    "запрос",
    [
        pytest.param({"$OrderBy": "Серия"}, id="сортировка"),
        pytest.param({"$Format": "xml"}, id="формат"),
        pytest.param({"$filter": "Номер eq '1'", "$Filter": "Номер eq '2'"}, id="дубль"),
    ],
)
async def test_Н6_написание_остальных_параметров_не_обходит_проверки(среда, одинс, запрос):
    _, _, tools, _ = среда
    одинс.get.mock(return_value=httpx.Response(200, json={"value": []}))

    отказ = await _отказ_raw_get(tools, ДОКУМЕНТЫ_ЛИЦ, запрос)

    assert отказ["code"] == "params_invalid", отказ
    assert not одинс.get.called


async def test_Н6_составной_ключ_в_пути_raw_get_отклоняется_до_1С(среда, одинс):
    """Значение ключа в пути гейт не видит: токен не раскрыт, открытый литерал не проверен. У
    `get` с `key` те же значения проходят правила гейта — туда и подсказка."""
    _, _, tools, _ = среда
    одинс.get.mock(return_value=httpx.Response(200, json={"value": []}))

    отказ = await _отказ_raw_get(
        tools, f"{КУРСЫ}(Period=datetime'2026-01-01T00:00:00',Валюта_Key=guid'{ССЫЛКА}')"
    )

    assert отказ["code"] == "params_invalid", отказ
    assert "odata1c_get" in отказ["hint"], отказ
    assert not одинс.get.called


async def test_Н6_параметр_условия_виртуальной_таблицы_с_любым_именем_проходит_гейт(
    среда, одинс, monkeypatch
):
    """У регистров бухгалтерии БП выражение отбора стоит и в `AccountCondition`,
    `BalancedAccountCondition`: `query` прогонял через гейт только `Condition`."""
    _, _, tools, _ = среда
    одинс.get.mock(return_value=httpx.Response(200, json={"value": []}))
    исходный = IndexRepository.describe

    def describe(self, name):
        описание = исходный(self, name)
        if описание is not None and описание.name == СРЕЗ_ДОКУМЕНТОВ:
            действие = dict(описание.actions[0])
            действие["params"] = {**действие["params"], "AccountCondition": "Edm.String"}
            описание = dataclasses.replace(описание, actions=[действие])
        return описание

    monkeypatch.setattr(IndexRepository, "describe", describe)

    отказ = await _отказ_чтения(
        tools, СРЕЗ_ДОКУМЕНТОВ, params={"AccountCondition": ПОДСТРОКА_СЕРИИ}
    )

    assert отказ["code"] == "filter_syntax", отказ
    assert not одинс.get.called


@pytest.mark.parametrize(
    "тело",
    [{"value": []}, {"value": "любой текст"}, {"value": 5}, {"value": [{"Номер": "1"}]}],
    ids=["пусто", "строка", "число", "записи"],
)
async def test_Н5_успешный_raw_get_не_повторяет_путь(среда, одинс, тело):
    """Путь в ответе — эхо ввода модели: цифры, известные словарю, страж превращал в токен. Хвост
    пути приходил ещё и ИМЕНЕМ поля скалярного ответа (`{"Поле<цифры>": …}`)."""
    _, _, tools, _ = среда
    т = _метка_в_словаре(tools)
    одинс.get.mock(return_value=httpx.Response(200, json=тело))

    for путь in (
        f"Catalog_Новый{ЦИФРЫ_ТЕЛЕФОНА}",
        f"Catalog_Новый{ЦИФРЫ_ТЕЛЕФОНА}(guid'{ССЫЛКА}')",
        f"{КОНТРАГЕНТЫ}(guid'{ССЫЛКА}')/{ИМЯ_С_МЕТКОЙ}",
    ):
        текст = await tools.raw_get(SessionScope(), base="ut", path=путь)
        ответ = json.loads(текст)
        assert "error" not in ответ, ответ
        assert "path" not in ответ and "entity" not in ответ, ответ
        _без_метки(ответ, т)
        assert "guard_replaced" not in текст


async def test_Н5_хвост_вне_индекса_не_становится_именем_поля(среда, одинс):
    """Имя из хвоста пути нужно маскировщику — без него ИНН скалярного ответа на поле вне индекса
    уходил числом (форма (в) ревью 2026-09-11). В ответ оно не выходит: ни ключом, ни в
    `masked_fields`."""
    _, _, tools, _ = среда
    одинс.get.mock(return_value=httpx.Response(200, json={"value": ИНН}))

    ответ = json.loads(
        await tools.raw_get(
            SessionScope(), base="ut", path=f"{КОНТРАГЕНТЫ}(guid'{ССЫЛКА}')/ИННКонтрагента"
        )
    )

    assert set(ответ["item"]) == {"value"}, ответ
    assert ТОКЕН_ИНН.match(ответ["item"]["value"]), ответ
    assert ответ["masked_fields"] == ["value"], ответ
    нет_реальных_значений(json.dumps(ответ, ensure_ascii=False))


async def test_Н5_хвост_из_индекса_остаётся_именем_поля(среда, одинс):
    _, _, tools, _ = среда
    одинс.get.mock(return_value=httpx.Response(200, json={"value": ИНН}))

    ответ = json.loads(
        await tools.raw_get(SessionScope(), base="ut", path=f"{КОНТРАГЕНТЫ}(guid'{ССЫЛКА}')/ИНН")
    )

    assert set(ответ["item"]) == {"ИНН"} and ТОКЕН_ИНН.match(ответ["item"]["ИНН"]), ответ
    assert ответ["masked_fields"] == ["ИНН"], ответ


async def test_Н5_entity_остаётся_на_пути_из_индекса(среда, одинс):
    _, _, tools, _ = среда
    одинс.get.mock(return_value=httpx.Response(200, json={"value": []}))

    ответ = json.loads(
        await tools.raw_get(
            SessionScope(), base="ut", path=f"{КОНТРАГЕНТЫ}(guid'{ССЫЛКА}')/КонтактнаяИнформация"
        )
    )

    assert ответ["entity"] == f"{КОНТРАГЕНТЫ}_КонтактнаяИнформация"
    assert "path" not in ответ
