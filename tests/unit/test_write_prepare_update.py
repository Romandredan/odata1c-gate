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
from conftest import без_навигаций, ничего_не_скрыто, строение_неизвестно

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

    def объект(self, тело: dict, *, status: int = 200) -> None:
        self.get.mock(return_value=httpx.Response(status, json=тело))

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


def нет_реальных_значений(текст: str) -> None:
    for значение in РЕАЛЬНЫЕ_ЗНАЧЕНИЯ:
        assert значение not in текст, f"реальное значение фикстуры в ответе: {значение[:3]}…"
    # Маскировщик обязан справиться сам: страж — последний рубеж, а не основной. Без этой
    # проверки превью с открытым ИНН прошло бы тест — страж заменил бы его на выходе.
    assert "guard_replaced" not in текст


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
    # Текущее состояние читается только теми полями, что меняются, и отпечатком версии: объект
    # целиком с табличными частями демону для подготовки не нужен.
    выбор = одинс.get.calls.last.request.url.params["$select"].split(",")
    assert sorted(выбор) == ["DataVersion", "ИНН"]


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


async def test_открытое_значение_пользователя_проходит_и_в_превью_становится_токеном(среда, одинс):
    """Открытое значение, продиктованное пользователем, идёт по правилам `inbound_value`: с
    контрольной суммой класса поля. В превью оно — токен, как и значение из 1С."""
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
    assert ТОКЕН_ИНН.match(ответ["preview"][0]["after"])
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


async def test_смена_только_написания_видна_в_превью(среда, одинс):
    """Одно значение в другом написании получает тот же токен: превью «X → X» без пометки
    выглядело бы как отсутствие изменения, а 1С получит другую строку. Изменённость решается на
    реальных значениях (шаг 6), а не на токенах, и превью об этом говорит."""
    запись, стор, _, _ = среда
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
    assert строка["before"] == строка["after"]
    assert "написани" in строка["note"]
    assert операция.request["json"] == {"ИНН": ИНН_С_ПРОБЕЛОМ}
    нет_реальных_значений(json.dumps(ответ, ensure_ascii=False))


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
    assert одинс.get.calls.last.request.url.params["$select"] == "Курс"


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
    одинс.объект(документ(Posted=True))

    отказ = ошибка(
        await запись.mark_for_deletion(
            SessionScope(), "sess-1", base="ut", entity=РЕАЛИЗАЦИЯ, key=ССЫЛКА_ДОК
        )
    )

    assert отказ["code"] == "params_invalid"
    assert "Unpost" in отказ["hint"]
    assert стор._ops == {}
    assert not одинс.писали


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


def test_diff_preview_не_выбрасывает_поле_с_равными_токенами():
    """Изменённость решает вызывающий на реальных значениях: равные токены — это одно значение
    в разных написаниях, а не отсутствие изменения. Строка остаётся, с пометкой."""
    [строка] = diff_preview({"ИНН": "[[inn:A]]"}, {"ИНН": "[[inn:A]]"})

    assert строка["field"] == "ИНН" and строка["before"] == строка["after"] == "[[inn:A]]"
    assert "написани" in строка["note"]
