"""Подготовка `update` и `mark_for_deletion` (план M2, задача 5): первая точка, где запись
встречается с гейтом. Реальные значения объекта 1С и превью для модели проходят через один вызов
`ToolService._run`, и ошибка здесь сразу нарушает инвариант 1.

Поддельная 1С — `respx`, гейт — `identifiers+names` (роль `prod` с явным `write: true`: у `dev`
гейт выключен, и токенов не было бы вовсе). Проверки «ни одного обращения к 1С» и «только GET»
идут по конкретным маршрутам, а не по общему счётчику роутера: фикстура держит перехват базового
адреса под завершение сеанса 1С.
"""

import json
import re

import httpx
import pytest
import respx
import yaml
from conftest import без_навигаций, ничего_не_скрыто, строение_неизвестно, эхо_отбора

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
    журнал = Journal(tmp_path / "journal.sqlite")
    запись = WriteService(tools, стор, журнал, CommitLimiter(), clock=Часы(1_757_000_000.0))
    yield запись, стор, tools, часы_стора
    журнал.close()
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


async def test_открытое_значение_пользователя_в_превью_как_прислано(среда, одинс):
    """Открытое значение, продиктованное пользователем, идёт по правилам `inbound_value`: с
    контрольной суммой класса поля. «Станет» — как прислала модель (Ruling 45): маска значения
    модели записала бы его в общий словарь до подтверждения. «Было» — по-прежнему токен."""
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
    assert строка["after"] == НОВЫЙ_ИНН and ТОКЕН_ИНН.match(строка["before"])
    нет_реальных_значений(json.dumps(ответ, ensure_ascii=False), кроме=(НОВЫЙ_ИНН,))


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


async def test_литерал_модели_известный_словарю_страж_заменяет_токеном(среда, одинс):
    """Ruling 45, пункт 4 — принятое поведение, закреплённое тестом: литерал модели в «станет»,
    совпавший со значением словаря, страж в `gate.finish` заменяет токеном (`guard_replaced`).
    Это тот же оракул, что `eq` с открытым литералом на чтении. Изменённость — по реальным
    значениям: новое написание того же ИНН уходит в PATCH, хотя токены равны."""
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
    assert строка["before"] == строка["after"] == токен(tools, ИНН)
    assert any(п.startswith("guard_replaced") for п in ответ["warnings"])
    assert операция.request["json"] == {"ИНН": ИНН_С_ПРОБЕЛОМ}
    assert ИНН not in json.dumps(ответ, ensure_ascii=False)
    assert ИНН_С_ПРОБЕЛОМ not in json.dumps(ответ, ensure_ascii=False)


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


async def test_независимый_регистр_путь_по_составному_ключу_и_без_отпечатка(среда, одинс):
    """Запись независимого регистра сведений `check_write` пропускает при одном `write: true`.
    Для `commit` (задача 7) здесь две особенности, которые лучше видеть явно: PATCH пойдёт по
    пути составного ключа, а отпечатка `DataVersion` у записи регистра нет вовсе — защиты от
    чужой записи между превью и `commit` на нём не построить."""
    запись, стор, _, _ = среда
    курсы = "InformationRegister_КурсыВалют"
    ключ = {"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА}
    одинс.объект(
        {"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА, "Кратность": 1, "Курс": 90.5}
    )

    ответ = json.loads(
        await запись.update(
            SessionScope(), "sess-1", base="ut", entity=курсы, key=ключ, data={"Курс": 91.25}
        )
    )
    операция = await стор.take(ответ["pending_id"], "sess-1")

    assert операция.request == {
        "method": "PATCH",
        "path": f"{курсы}(Period=datetime'2026-01-01T00:00:00',Валюта_Key=guid'{ССЫЛКА}')",
        "json": {"Курс": 91.25},
    }
    assert операция.data_version is None
    assert операция.key == ключ
    assert ответ["preview"] == [{"field": "Курс", "before": 90.5, "after": 91.25}]
    # Ruling 44: запись регистра называется полями ключа — маской того же вызова, что «было».
    assert ответ["key"] == ключ
    assert ответ["object"] == ключ
    выбор = одинс.get.calls.last.request.url.params["$select"].split(",")
    assert sorted(выбор) == sorted(["Курс", "Period", "Валюта_Key"])


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
        "InformationRegister_КурсыВалют",
        {"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА},
        {"Кратность": 1.5},
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
    with pytest.raises(GateError) as на_чтении:
        гейт.inbound_filter(
            "ДатаРождения eq datetime'1985-03-14T00:00:00'",
            entity=лица,
            revealed=RevealedValues(),
            shape=строение_неизвестно,
        )

    образец = open_literal_refusal("dob", "ДатаРождения", "1985-03-14T00:00:00")
    assert отказы[0] == {"code": "filter_syntax", "message": str(образец), "hint": образец.hint}
    assert отказы[1]["message"] == отказы[0]["message"].replace("14T", "15T")
    assert (на_чтении.value.code, на_чтении.value.hint) == ("filter_syntax", образец.hint)
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
    with pytest.raises(GateError) as на_чтении:
        гейт.inbound_filter(
            f"ДатаРождения eq '{значение}'",
            entity=лица,
            revealed=RevealedValues(),
            shape=строение_неизвестно,
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
    запись, стор, tools, _ = среда
    токен(tools, НАЗВАНИЕ, поле="Description")

    async def комментарий() -> str:
        одинс.объект(документ(Комментарий="звонили из Ромашка по упд 711"))
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

    assert "ромашка" not in tools._dictionary.ambiguous_name_variants()
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
    """Новое название — «станет» как прислано, без токена: номер токена больше не говорит, было
    ли название в словаре. Остаётся принятый оракул Ruling 45, пункт 4: литерал, УЖЕ известный
    словарю (в том числе из другой базы), страж заменит токеном — как эхо литерала в ошибке 1С
    на чтении."""
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

    assert ответ["preview"][0]["after"] == "ООО Заведомо Новое Имя 1"
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
    журнал = Journal(tmp_path / "journal.sqlite")
    yield WriteService(tools, стор, журнал, CommitLimiter(), clock=Часы(1.0)), стор, tools
    журнал.close()
    await tools.aclose()


async def test_строка_раннего_прохода_в_current_разворачивается(среда_синт, одинс):
    """Изменённость сравнивается с ИСХОДНЫМ значением переписанной строки (`_текущее_реальное`):
    литерал модели, равный тому, что лежит в 1С, — «изменений нет», хотя в разобранном ответе на
    месте ИНН стоит токен. «Было» — токен того же значения (не `lit`, не `internal`), объект
    называется ключом маской того же вызова."""
    запись, стор, tools = среда_синт
    гейт = tools._gate_for(tools._config.bases["ut"])
    assert гейт.field_class(РЕГИСТР_ИНН, "ИНН", shape=строение_неизвестно) == "inn"
    ток = токен(tools, ИНН, entity=РЕГИСТР_ИНН)
    ключ = {"ИНН": ток}
    исходный = f"ИНН {ИНН} проверен"
    одинс.объект({"ИНН": ИНН, "Комментарий": исходный})

    тот_же = ошибка(
        await запись.update(
            SessionScope(),
            "s",
            base="ut",
            entity=РЕГИСТР_ИНН,
            key=ключ,
            data={"Комментарий": исходный},
        )
    )
    текст = await запись.update(
        SessionScope(),
        "s",
        base="ut",
        entity=РЕГИСТР_ИНН,
        key=ключ,
        data={"Комментарий": "проверка снята"},
    )

    assert "изменений нет" in тот_же["message"]
    ответ = json.loads(текст)
    assert ответ["preview"] == [
        {"field": "Комментарий", "before": f"ИНН {ток} проверен", "after": "проверка снята"}
    ]
    assert ответ["object"] == {"ИНН": ток} and ответ["key"] == ключ
    assert "[[lit:" not in текст and ИНН not in текст
    операция = await стор.take(ответ["pending_id"], "s")
    assert операция.request["path"] == f"{РЕГИСТР_ИНН}(ИНН='{ИНН}')"
    assert операция.request["json"] == {"Комментарий": "проверка снята"}
    assert операция.data_version is None


# Ruling 48 (Н-1 повторного ревью): словарь пополняется только из тела ответа 1С с данными.
# Текст отказа — гейта и 1С — маскируется токеном без записи. Литералы — такие, что их находит
# детектор (телефон, ИНН с верной контрольной суммой): чистая дата прошлого раунда детектору не
# видна, и тест словаря дефекта не замечал.

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
    """Отказ Ruling 45 по открытой дате повторяет литерал модели; детектор находит в нём реквизит,
    и тот получает токен — но в словарь не попадает (B1)."""
    запись, стор, tools, _ = среда
    до = строки_словаря(tools)

    отказ = ошибка(
        await запись.update(
            SessionScope(), "s", base="ut", entity=ЛИЦА, key=ССЫЛКА, data={"ДатаРождения": литерал}
        )
    )

    assert отказ["code"] == "filter_syntax"
    assert any(т.startswith(f"[[{класс}:") for т in _токены(отказ["message"]))
    assert строки_словаря(tools) == до
    assert not одинс.обращались and стор._ops == {}


async def test_отказ_по_контрольной_сумме_после_GET_словарь_не_пополняет(среда, одинс):
    """B1b: эхо значения в отказе `inbound_write` (контрольная сумма) — после GET, тот же путь."""
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

    assert "[[phone:" in отказ["message"]
    assert строки_словаря(tools) == до
    assert стор._ops == {}


async def test_отказ_отбора_на_чтении_с_эхом_словарь_не_пополняет(среда, одинс):
    """Тот же канал на чтении (старше задачи 5): отказ отбора повторяет литерал."""
    _, _, tools, _ = среда
    до = строки_словаря(tools)

    отказ = ошибка(
        await tools.query(
            SessionScope(), base="ut", entity=ЛИЦА, filter=f"ДатаРождения eq '{ЭХО_ИНН}'"
        )
    )

    assert "[[inn:" in отказ["message"] and ЦИФРЫ_ИНН not in отказ["message"]
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


async def test_токен_эха_известного_значения_прежний(среда, одинс):
    """Известное значение в другом написании получает в эхе свой прежний токен, а словарь — ни
    второго написания, ни строки."""
    запись, _, tools, _ = среда
    т = токен(tools, "+7 495 700-00-00", entity=БАНК, поле="ТелефоныБанка")
    до = строки_словаря(tools)

    отказ = ошибка(
        await запись.update(
            SessionScope(),
            "s",
            base="ut",
            entity=ЛИЦА,
            key=ССЫЛКА,
            data={"ДатаРождения": "звоните +7 (495) 700-00-00"},
        )
    )

    assert т in _токены(отказ["message"])
    assert строки_словаря(tools) == до


async def test_токен_эха_нового_значения_тот_же_что_у_данных_1С(среда, одинс):
    """Сухой токен эха — тот, что выдаст обычная маска, когда значение придёт данными 1С; но
    пока оно не пришло, токен не раскрывается: отбор по нему — `token_unknown`."""
    запись, _, tools, _ = среда
    отказ = ошибка(
        await запись.update(
            SessionScope(),
            "s",
            base="ut",
            entity=ЛИЦА,
            key=ССЫЛКА,
            data={"ДатаРождения": ЭХО_ТЕЛЕФОН},
        )
    )
    [эхо] = _токены(отказ["message"])

    по_эху = ошибка(
        await tools.query(
            SessionScope(), base="ut", entity=БАНК, filter=f"ТелефоныБанка eq '{эхо}'"
        )
    )
    assert по_эху["code"] == "token_unknown"
    assert not одинс.обращались

    assert эхо == токен(tools, "+7 916 000-07-11", entity=БАНК, поле="ТелефоныБанка")


async def test_B2_обратно_отказ_не_портит_номер_документа(среда, одинс):
    """B2 ревью в обратной форме: отказанная подготовка с ИНН-подобными цифрами не делает номер
    документа токеном `inn` в следующем чтении (инвариант 6)."""
    запись, _, tools, _ = среда
    await запись.update(
        SessionScope(), "s", base="ut", entity=ЛИЦА, key=ССЫЛКА, data={"ДатаРождения": ЭХО_ИНН}
    )

    одинс.объект(документ(Number=ЦИФРЫ_ИНН))
    ответ = json.loads(
        await tools.get(
            SessionScope(), base="ut", entity=РЕАЛИЗАЦИЯ, key=ССЫЛКА_ДОК, select=["Number"]
        )
    )

    assert ответ["item"]["Number"] == ЦИФРЫ_ИНН
    assert not any("guard_replaced" in п for п in ответ.get("warnings", []))


async def test_B4_обратно_отказ_не_делает_токен_неоднозначным(среда, одинс):
    """B4 ревью в обратной форме: отказанная подготовка с эхом того же номера в другом написании
    не кладёт второе написание, и запись токеном в поле без своих написаний проходит."""
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
    одинс.get.reset()
    отказ = ошибка(
        await запись.update(
            SessionScope(),
            "s",
            base="ut",
            entity=ЛИЦА,
            key=ССЫЛКА,
            data={"ДатаРождения": "звоните +7 (495) 700-00-00"},
        )
    )
    assert отказ["code"] == "filter_syntax" and not одинс.get.called
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
