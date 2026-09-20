"""Подготовка `create` и `action` (план M2, задача 6) рядом с `update`/`mark_for_deletion`
задачи 5.

`create` не обращается к 1С вовсе: текущего состояния у нового объекта нет, превью — тело как его
прислала модель (Ruling 45), открытый литерал не отклоняется (Ruling 47). `action` читает текущее
состояние одним GET и предвидит отказы 1С (решение 12 плана); форма запроса — факт пробы P8.

Поддельная 1С — `respx`, гейт — `identifiers+names` (роль `prod` с явным `write: true`: у `dev`
гейт выключен, и токенов не было бы вовсе). «Ни одного обращения к 1С» проверяется по конкретным
маршрутам, а не общим счётчиком роутера: перехват базового адреса держит завершение сеанса 1С.
"""

import functools
import inspect
import json
import pathlib

import httpx
import pytest
import respx
import yaml
from conftest import (
    без_класса_пути,
    без_навигаций,
    ничего_не_скрыто,
    обеспечить_policy_yaml,
    строение_неизвестно,
)

from odata1c.cli import main
from odata1c.config.loader import load_config
from odata1c.gate.contact_info import EntityShape
from odata1c.gate.dictionary import Dictionary
from odata1c.gate.pipeline import BaseGate
from odata1c.gate.revealed import RevealedValues
from odata1c.gate.service import policy_path, refresh_policy
from odata1c.gate.unmasking import GateError, Unmasker
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository
from odata1c.registry.registry import SessionScope
from odata1c.tools.service import ToolService
from odata1c.write import permissions, service
from odata1c.write.journal import Journal
from odata1c.write.pending import CommitLimiter, PendingStore
from odata1c.write.permissions import ДЕЙСТВИЯ_ПРОВЕДЕНИЯ
from odata1c.write.preview import body_preview
from odata1c.write.service import WriteService

URL_UT = "http://localhost/ut/odata/standard.odata/"
URL_RO = "http://localhost/ro/odata/standard.odata/"
URL_NOPOST = "http://localhost/nopost/odata/standard.odata/"

КОНТРАГЕНТЫ = "Catalog_Контрагенты"
КИ = "Catalog_Контрагенты_КонтактнаяИнформация"
РЕАЛИЗАЦИЯ = "Document_РеализацияТоваровУслуг"
ЛИЦА = "Catalog_ФизическиеЛица"
БАНК = "Catalog_БанковскиеСчетаКонтрагентов"
КУРСЫ = "InformationRegister_КурсыВалют"
ПРОЦЕСС = "BusinessProcess_пр_БизнесПроцессСогласованияОрдеров"
ЗАДАЧА = "Task_пр_ЗадачаСогласования"
ДВИЖЕНИЯ = "AccumulationRegister_ДвиженияДенежныеСредстваКонтрагент_RecordType"

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
      deny_fields: [Catalog_Контрагенты.КодПоОКПО, {КИ}.Регион]
  ro:
    label: УТ, только чтение
    url: {URL_RO}
    user: u
    password: p
    role: prod
  nopost:
    label: УТ, без проведения
    url: {URL_NOPOST}
    user: u
    password: p
    role: prod
    write: true
    permissions:
      post_documents: false
"""

ССЫЛКА = "a103cb54-42ee-11ec-a7a0-f10ab59a067e"
ССЫЛКА_ДОК = "0c4320aa-624f-11f0-a7a0-fa78dd2b3d42"
ВЕРСИЯ = "AAAAAQAAAAA="

ИНН = "7707083893"
ИНН_С_ПРОБЕЛОМ = "7707 083893"
НОВЫЙ_ИНН = "7736050003"
ЕЩЁ_ИНН = "7728168971"
НАЗВАНИЕ = "ООО Ромашка"
ПОЛНОЕ_НАЗВАНИЕ = "Общество с ограниченной ответственностью «Ромашка»"
КПП = "770701001"
ТЕЛЕФОН = "+7 916 123-45-67"
ЦИФРЫ_ТЕЛЕФОНА = "79161234567"
ПОЧТА = "ivan@example.com"
НОВЫЙ_ТЕЛЕФОН = "+7 495 700-11-22"

# Реальные значения фикстуры, которые модель видит только токенами. Литералы, которые модель
# присылает сама в теле `create`, сюда не входят: «станет» показывается как прислано (Ruling 45).
РЕАЛЬНЫЕ_ЗНАЧЕНИЯ = (
    ИНН,
    ИНН_С_ПРОБЕЛОМ,
    НОВЫЙ_ИНН,
    НАЗВАНИЕ,
    "Ромашка",
    ПОЛНОЕ_НАЗВАНИЕ,
    КПП,
    ТЕЛЕФОН,
    ЦИФРЫ_ТЕЛЕФОНА,
    ПОЧТА,
)


class Часы:
    def __init__(self, начало: float) -> None:
        self.сейчас = начало

    def __call__(self) -> float:
        return self.сейчас


@pytest.fixture
def дом(tmp_path, edmx_ut_real):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES_YAML, encoding="utf-8")
    config = load_config(home)
    for имя in config.bases:
        хранилище = IndexRepository(index_path(home, имя))
        хранилище.write(parse_edmx(edmx_ut_real))
        хранилище.close()
        refresh_policy(home, config.bases[имя])
        обеспечить_policy_yaml(home, имя)
    return home


@pytest.fixture
async def среда(дом, tmp_path):
    tools = ToolService(load_config(дом))
    стор = PendingStore(600, clock=Часы(1000.0))
    # Журнал — фабрикой: `commit` открывает его на вызов (задача 7); подготовка его не зовёт.
    журнал = functools.partial(Journal, tmp_path / "journal.sqlite")
    запись = WriteService(tools, стор, журнал, CommitLimiter(), clock=Часы(1_757_000_000.0))
    yield запись, стор, tools
    await tools.aclose()


class Маршруты:
    """GET по сущностям и все пишущие методы поддельной 1С. Пишущие отвечают 500 — если код их
    всё же вызовет, ответ заметен, а не тих."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.get = router.get(url__regex=r".*standard\.odata/[^?]+").mock(
            return_value=httpx.Response(500, json={})
        )
        отказ = httpx.Response(500, json={})
        self.patch = router.patch(url__regex=r".*").mock(return_value=отказ)
        self.post = router.post(url__regex=r".*").mock(return_value=отказ)
        self.put = router.put(url__regex=r".*").mock(return_value=отказ)
        self.delete = router.delete(url__regex=r".*").mock(return_value=отказ)

    def объект(self, тело: dict, *, по_выбору: bool = False) -> None:
        """`по_выбору=True` — поддельная 1С соблюдает `$select`, как настоящая (M-2 ревью задачи
        5): поле, которое код не запросил, не приходит вовсе. Без этого тест не видит, что
        `Posted` или `DeletionMark` выпал из выбора и предвидение отказа молча отключилось."""
        if not по_выбору:
            self.get.mock(return_value=httpx.Response(200, json=тело))
            return

        def ответ(request: httpx.Request) -> httpx.Response:
            выбор = request.url.params.get("$select")
            поля = set(выбор.split(",")) if выбор else set(тело)
            return httpx.Response(200, json={к: з for к, з in тело.items() if к in поля})

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
        for url in (URL_UT, URL_RO, URL_NOPOST):
            router.get(url).mock(return_value=httpx.Response(200, json={"value": []}))
        yield Маршруты(router)


def контрагент(**поля) -> dict:
    тело = {
        "Ref_Key": ССЫЛКА,
        "DataVersion": ВЕРСИЯ,
        "DeletionMark": False,
        "Description": НАЗВАНИЕ,
        "НаименованиеПолное": ПОЛНОЕ_НАЗВАНИЕ,
        "ИНН": ИНН,
        "КПП": КПП,
        "КонтактнаяИнформация": [
            {
                "LineNumber": "1",
                "Тип": "Телефон",
                "Представление": ТЕЛЕФОН,
                "НомерТелефона": ЦИФРЫ_ТЕЛЕФОНА,
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
    }
    тело.update(поля)
    return тело


def токен(tools: ToolService, значение: str, *, entity=КОНТРАГЕНТЫ, поле="ИНН") -> str:
    """Токен, который модель увидела бы в ответе чтения: маска того же гейта базы `ut` кладёт
    значение в словарь с написанием этого поля, как после настоящего `query`/`get`."""
    гейт = tools._gate_for(tools._config.bases["ut"])
    return гейт.mask(
        {поле: значение},
        entity=entity,
        resolve=без_навигаций,
        hidden=ничего_не_скрыто,
        revealed=None,
        shape=строение_неизвестно,
    ).data[поле]


async def прочитать_контрагента(tools: ToolService, одинс: Маршруты) -> dict:
    """Карточка контрагента через настоящий `get` шлюза — так модель узнаёт токены его
    реквизитов и контактной информации (строки табличной части маскируются по своей сущности)."""
    одинс.объект(контрагент())
    ответ = json.loads(await tools.get(SessionScope(), base="ut", entity=КОНТРАГЕНТЫ, key=ССЫЛКА))
    одинс.get.reset()
    return ответ["item"]


def нет_реальных_значений(текст: str) -> None:
    for значение in РЕАЛЬНЫЕ_ЗНАЧЕНИЯ:
        assert значение not in текст, f"реальное значение фикстуры в ответе: {значение[:3]}…"
    # Маскировщик и превью обязаны справиться сами: страж — последний рубеж, а не основной.
    assert "guard_replaced" not in текст


def строки_словаря(tools: ToolService) -> dict[str, int]:
    """Число строк в каждой таблице `gate.sqlite` — состояние словаря целиком (Ruling 45)."""
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


async def создать(запись, data, *, base="ut", entity=КОНТРАГЕНТЫ) -> str:
    return await запись.create(SessionScope(), "sess-1", base=base, entity=entity, data=data)


async def действие(
    запись, name, *, base="ut", entity=РЕАЛИЗАЦИЯ, key=ССЫЛКА_ДОК, params=None
) -> str:
    return await запись.action(
        SessionScope(), "sess-1", base=base, entity=entity, key=key, name=name, params=params
    )


# ---------------------------------------------------------------------------------------------
# create: превью — тело как прислала модель, request — реальные значения, 1С не трогается
# ---------------------------------------------------------------------------------------------


async def test_создание_контрагента_ИНН_токеном_в_превью_токен_в_request_реальный(среда, одинс):
    запись, стор, tools = среда
    новый = токен(tools, НОВЫЙ_ИНН)
    данные = {"Description": "ООО Северный Ветер", "ИНН": новый}

    текст = await создать(запись, данные)

    нет_реальных_значений(текст)
    ответ = json.loads(текст)
    assert ответ["op"] == "create" and ответ["entity"] == КОНТРАГЕНТЫ
    assert ответ["base"] == "ut" and ответ["role"] == "prod"
    # Токен модели — как есть; литерал названия (класс `org`) — пометкой (Ruling 62).
    assert ответ["preview"] == [
        {"field": "Description", "value": "значение из запроса (класс org)"},
        {"field": "ИНН", "value": новый},
    ]
    # Ruling 44: объект называется представлением из данных модели — ключа ещё нет.
    assert ответ["object"] == {"Description": "значение из запроса (класс org)"}
    assert "key" not in ответ
    assert ответ["expires_in_s"] == 600 and "odata1c_commit" in ответ["next"]
    операция = await стор.take(ответ["pending_id"], "sess-1")
    assert операция.request == {
        "method": "POST",
        "path": КОНТРАГЕНТЫ,
        "json": {"Description": "ООО Северный Ветер", "ИНН": НОВЫЙ_ИНН},
    }
    assert операция.data_version is None and операция.key is None
    assert (операция.op, операция.entity, операция.base) == ("create", КОНТРАГЕНТЫ, "ut")
    assert операция.created_at == 1_757_000_000.0
    нет_реальных_значений(json.dumps(операция.preview, ensure_ascii=False))
    assert операция.preview["object"] == ответ["object"]
    # Инвариант 2 и нечего читать: у нового объекта нет текущего состояния.
    assert not одинс.обращались


async def test_создание_с_табличной_частью_токен_в_превью_реальное_в_request(среда, одинс):
    """Табличная часть — список строк. Токен телефона из прочитанной карточки уходит в 1С
    реальным значением, в превью — токеном (Ruling 45: маски своей сущности у строки превью нет,
    она нужна только проверке полей строки по индексу). Литерал `Тип` — пометкой, число
    `LineNumber` — как есть (Ruling 63)."""
    запись, стор, tools = среда
    карточка = await прочитать_контрагента(tools, одинс)
    телефон = карточка["КонтактнаяИнформация"][0]["Представление"]
    assert телефон.startswith("[[") and ТЕЛЕФОН not in телефон
    строки = [{"LineNumber": 1, "Тип": "Телефон", "Представление": телефон}]

    текст = await создать(
        запись, {"Description": "ООО Северный Ветер", "КонтактнаяИнформация": строки}
    )

    нет_реальных_значений(текст)
    ответ = json.loads(текст)
    assert ответ["preview"][1] == {
        "field": "КонтактнаяИнформация",
        "value": [{"LineNumber": 1, "Тип": "значение из запроса", "Представление": телефон}],
    }
    операция = await стор.take(ответ["pending_id"], "sess-1")
    [строка] = операция.request["json"]["КонтактнаяИнформация"]
    assert строка == {"LineNumber": 1, "Тип": "Телефон", "Представление": ТЕЛЕФОН}
    assert not одинс.обращались


async def test_строки_без_line_number_нумеруются_шлюзом(среда, одинс):
    """Правило §5.1 (M3b задача 6): ни одна строка табличной части не несёт LineNumber — шлюз
    нумерует их 1..n в порядке тела. Без этого 1С отвечала бы HTTP 500 уже после подтверждения
    пользователя (известное ограничение записи M2)."""
    запись, стор, _ = среда

    ответ = json.loads(
        await создать(
            запись,
            {
                "Description": "ООО Северный Ветер",
                "КонтактнаяИнформация": [
                    {"Тип": "Телефон", "Представление": НОВЫЙ_ТЕЛЕФОН},
                    {"Тип": "АдресЭлектроннойПочты", "Представление": "new@example.com"},
                ],
            },
        )
    )

    операция = await стор.take(ответ["pending_id"], "sess-1")
    строки = операция.request["json"]["КонтактнаяИнформация"]
    assert [строка["LineNumber"] for строка in строки] == [1, 2]
    assert not одинс.обращались


async def test_свои_line_number_проверяются_на_повторы_и_положительность(среда, одинс):
    """Все строки несут LineNumber — множество номеров обязано быть ровно `{1..n}`; повтор номера
    отклоняется до 1С (create вообще не обращается к 1С при подготовке — проверяется явно)."""
    запись, стор, _ = среда

    отказ = ошибка(
        await создать(
            запись,
            {
                "Description": "ООО Северный Ветер",
                "КонтактнаяИнформация": [
                    {"LineNumber": 1, "Тип": "Телефон", "Представление": НОВЫЙ_ТЕЛЕФОН},
                    {"LineNumber": 1, "Тип": "АдресЭлектроннойПочты", "Представление": "x"},
                ],
            },
        )
    )

    assert отказ["code"] == "params_invalid"
    assert "подряд с 1" in отказ["message"]
    assert стор._ops == {} and not одинс.обращались


async def test_смешанные_line_number_отклоняются_до_1С(среда, одинс):
    """Часть строк с номером, часть без — шлюз не угадывает место ненумерованной строки среди
    номерованных (могло бы быть несколько способов вставить её)."""
    запись, стор, _ = среда

    отказ = ошибка(
        await создать(
            запись,
            {
                "Description": "ООО Северный Ветер",
                "КонтактнаяИнформация": [
                    {"LineNumber": 1, "Тип": "Телефон", "Представление": НОВЫЙ_ТЕЛЕФОН},
                    {"Тип": "АдресЭлектроннойПочты", "Представление": "x"},
                ],
            },
        )
    )

    assert отказ["code"] == "params_invalid"
    assert "либо у всех строк" in отказ["message"]
    assert стор._ops == {} and not одинс.обращались


async def test_открытые_литералы_создания_не_отклоняются(среда, одинс):
    """Ruling 47: у нового объекта сравнивать не с чем — открытое значение, продиктованное
    пользователем, проходит, в том числе контактная информация (Ruling 38: новое значение КИ —
    открытым значением) и дата рождения, которую `update` открытым литералом не принимает."""
    запись, стор, _ = среда
    строки = [{"Тип": "Телефон", "Представление": НОВЫЙ_ТЕЛЕФОН}]

    контрагент_ = json.loads(
        await создать(
            запись, {"Description": "ИП Лесной", "ИНН": ЕЩЁ_ИНН, "КонтактнаяИнформация": строки}
        )
    )
    лицо = json.loads(
        await создать(
            запись,
            {"Description": "Лесной Пётр", "ДатаРождения": "1985-03-14T00:00:00"},
            entity=ЛИЦА,
        )
    )

    assert "pending_id" in контрагент_, контрагент_
    assert "pending_id" in лицо, лицо
    операция = await стор.take(контрагент_["pending_id"], "sess-1")
    # Правило §5.1: строка без LineNumber получает номер по порядку тела (M3b задача 6).
    assert операция.request["json"]["КонтактнаяИнформация"] == [{"LineNumber": 1, **строки[0]}]
    assert операция.request["json"]["ИНН"] == ЕЩЁ_ИНН
    # В 1С — как прислано, в превью — пометкой: у поля с классом — с классом (Ruling 62), у
    # поля без класса — без него (Ruling 63).
    assert контрагент_["preview"][1] == {"field": "ИНН", "value": "значение из запроса (класс inn)"}
    assert контрагент_["preview"][2]["value"] == [
        {
            "LineNumber": 1,
            "Тип": "значение из запроса",
            "Представление": "значение из запроса (класс contact)",
        }
    ]
    assert not одинс.обращались


async def test_создание_с_токеном_двух_написаний_отказ_token_ambiguous(среда, одинс):
    """У нового объекта нет `current`, и выбрать одно из написаний не по чему (лестница 1.2
    отчёта блокеров): молчаливый выбор подменил бы написание невидимо для превью."""
    запись, стор, tools = среда
    ток = токен(tools, ИНН)
    assert токен(tools, ИНН_С_ПРОБЕЛОМ) == ток
    до = строки_словаря(tools)

    текст = await создать(запись, {"Description": "ООО Северный Ветер", "ИНН": ток})

    assert ошибка(текст)["code"] == "token_ambiguous"
    нет_реальных_значений(текст)
    assert стор._ops == {}
    assert строки_словаря(tools) == до
    assert not одинс.обращались


async def test_создание_дата_токеном_проверяется_после_раскрытия(среда, одинс):
    """Та же проверка, что у `update`: формат даты, пришедшей токеном, известен только после
    раскрытия. Негодное написание — отказ без значения, до создания операции."""
    запись, стор, tools = среда
    годная = токен(tools, "1980-05-01T00:00:00", entity=ЛИЦА, поле="ДатаРождения")
    негодная = токен(tools, "1975-03-02", entity=ЛИЦА, поле="ДатаРождения")

    ответ = json.loads(
        await создать(запись, {"Description": "Лесной Пётр", "ДатаРождения": годная}, entity=ЛИЦА)
    )
    отказ = ошибка(
        await создать(запись, {"Description": "Лесной Пётр", "ДатаРождения": негодная}, entity=ЛИЦА)
    )

    операция = await стор.take(ответ["pending_id"], "sess-1")
    assert операция.request["json"]["ДатаРождения"] == "1980-05-01T00:00:00"
    assert "1980-05-01" not in json.dumps(ответ, ensure_ascii=False)
    assert отказ["code"] == "params_invalid" and "ДатаРождения" in отказ["message"]
    assert "1975-03-02" not in json.dumps(отказ, ensure_ascii=False)
    assert len(стор._ops) == 1


async def test_дата_токеном_в_строке_табличной_части_проверяется_после_раскрытия(среда, одинс, дом):
    """То же правило для строки табличной части — по полям её сущности. Поле строки с классом
    `dob` задано ручным правилом владельца (в типовой УТ такого нет, но политика это позволяет)."""
    запись, стор, tools = среда
    путь = policy_path(дом, "ut")
    политика = yaml.safe_load(путь.read_text(encoding="utf-8")) or {}
    политика.setdefault("fields", {})[f"{КИ}.ДействуетС"] = "dob"
    путь.write_text(yaml.safe_dump(политика, allow_unicode=True), encoding="utf-8")
    годная = токен(tools, "1980-05-01T00:00:00", entity=КИ, поле="ДействуетС")
    негодная = токен(tools, "1975-03-02", entity=КИ, поле="ДействуетС")
    assert годная.startswith("[[dob:") and негодная.startswith("[[dob:")

    ответ = json.loads(
        await создать(запись, {"КонтактнаяИнформация": [{"Тип": "Адрес", "ДействуетС": годная}]})
    )
    отказ = ошибка(
        await создать(запись, {"КонтактнаяИнформация": [{"Тип": "Адрес", "ДействуетС": негодная}]})
    )

    операция = await стор.take(ответ["pending_id"], "sess-1")
    [строка] = операция.request["json"]["КонтактнаяИнформация"]
    assert строка["ДействуетС"] == "1980-05-01T00:00:00"
    assert отказ["code"] == "params_invalid" and "ДействуетС" in отказ["message"]
    assert "1975-03-02" not in json.dumps(отказ, ensure_ascii=False)
    assert len(стор._ops) == 1 and not одинс.обращались


async def test_Ruling_60_создание_записи_независимого_регистра_отказ(среда, одинс):
    """Ruling 60 (И-3 итогового ревью M2): запись регистра — и независимого регистра сведений —
    в первой поставке не поддерживается. POST по существующему набору измерений мог бы заместить
    запись, а «до» у `create` в журнале нет. Отказ до 1С, операции нет."""
    запись, стор, _ = среда
    данные = {"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА, "Курс": 91.25, "Кратность": 1}

    отказ = ошибка(await создать(запись, данные, entity=КУРСЫ))

    assert отказ["code"] == "permission_denied" and КУРСЫ in отказ["message"]
    assert "odata1c_action" in отказ["hint"]
    assert стор._ops == {} and not одинс.обращались


async def test_Ruling_62_литерал_защищаемого_класса_в_превью_create_не_повторяется(среда, одинс):
    """Ruling 62 (И-1 итогового ревью M2): открытый литерал в поле защищаемого класса в превью,
    `object` и тексте подтверждения не повторяется — на его месте пометка с классом поля из
    политики. Иначе страж заменял бы известное словарю значение токеном (`guard_replaced`), а
    неизвестное оставлял литералом, и по разнице модель узнавала бы, известен ли шлюзу телефон
    или название — без 1С, в обход Ruling 35. В 1С уходит то, что прислала модель."""
    запись, стор, tools = среда
    токен(tools, ИНН)

    ответ = json.loads(await создать(запись, {"Description": "ООО Северный Ветер", "ИНН": ИНН}))

    assert ответ["preview"][1] == {"field": "ИНН", "value": "значение из запроса (класс inn)"}
    assert ответ["object"]["Description"] == "значение из запроса (класс org)"
    assert "guard_replaced" not in json.dumps(ответ, ensure_ascii=False)
    операция = await стор.take(ответ["pending_id"], "sess-1")
    assert операция.request["json"]["ИНН"] == ИНН
    assert операция.request["json"]["Description"] == "ООО Северный Ветер"


def _без_номера(текст: str) -> str:
    """Ответ подготовки без `pending_id` — единственного поля, которое различается у двух
    подготовок по построению (uuid4)."""
    return текст.replace(json.loads(текст)["pending_id"], "<pending_id>")


async def test_Ruling_62_create_известное_и_неизвестное_значение_байт_в_байт(среда, одинс):
    """Известный словарю телефон (строка контактной информации, класс `contact` по сущности
    строки) и название (`Description`, класс `org`) против неизвестных: ответы подготовки и тексты
    подтверждения совпадают байт в байт, `guard_replaced` нет ни в одном."""
    запись, стор, tools = среда
    токен(tools, ТЕЛЕФОН, entity=БАНК, поле="ТелефоныБанка")
    токен(tools, НАЗВАНИЕ, поле="Description")

    ответы = []
    for название in (НАЗВАНИЕ, "ООО Незнакомка"):
        for телефон in (ТЕЛЕФОН, НОВЫЙ_ТЕЛЕФОН):
            ответы.append(
                await создать(
                    запись,
                    {
                        "Description": название,
                        "КонтактнаяИнформация": [{"Тип": "Телефон", "Представление": телефон}],
                    },
                )
            )

    образец = _без_номера(ответы[0])
    assert all(_без_номера(ответ) == образец for ответ in ответы), ответы
    assert "guard_replaced" not in образец and "[[" not in образец
    assert "значение из запроса (класс contact)" in образец
    assert "значение из запроса (класс org)" in образец
    тексты = {
        service._текст_подтверждения(await стор.take(json.loads(ответ)["pending_id"], "sess-1"))
        for ответ in ответы
    }
    assert len(тексты) == 1
    [текст] = тексты
    assert НАЗВАНИЕ not in текст and ТЕЛЕФОН not in текст and НОВЫЙ_ТЕЛЕФОН not in текст
    assert not одинс.обращались


# ---------------------------------------------------------------------------------------------
# Ruling 63 (ФП-1 ревью итоговых правок M2): литерал модели не повторяется ни в каком поле
# ---------------------------------------------------------------------------------------------

# Догадка модели внутри свободного текста поля без класса: известное словарю значение (его
# кладёт маска чтения — `entity`/`поле`) против неизвестного. До Ruling 63 страж заменял
# известное ТЕМ ЖЕ токеном, что в чтении, — модель сопоставляла догадку с токеном без 1С.
ДОГАДКИ = [
    pytest.param(ИНН, НОВЫЙ_ИНН, КОНТРАГЕНТЫ, "ИНН", "ИНН покупателя {}", id="ИНН"),
    pytest.param(ТЕЛЕФОН, НОВЫЙ_ТЕЛЕФОН, БАНК, "ТелефоныБанка", "звонить {}", id="телефон"),
    pytest.param(
        НАЗВАНИЕ,
        "ООО Незнакомка",
        КОНТРАГЕНТЫ,
        "Description",
        "отгрузить {} по договору",
        id="название",
    ),
]


def _маска_в_базе(tools, база: str, значение: str, *, entity: str, поле: str) -> str:
    """Токен значения в ответе чтения базы `база` — маской её гейта (кладёт в общий словарь)."""
    гейт = tools._gate_for(tools._config.bases[база])
    return гейт.mask(
        {поле: значение},
        entity=entity,
        resolve=без_навигаций,
        hidden=ничего_не_скрыто,
        revealed=None,
        shape=строение_неизвестно,
    ).data[поле]


async def _тексты_подтверждения(tools, стор, ответы: list[str]) -> set[str]:
    """Тексты диалога так, как их увидит пользователь, — после стража базы (`finish_text`)."""
    гейт = tools._gate_for(tools._config.bases["ut"])
    return {
        гейт.finish_text(
            service._текст_подтверждения(await стор.take(json.loads(о)["pending_id"], "sess-1"))
        )
        for о in ответы
    }


def _без_догадки(ответ: str, известное: str, токен_чтения: str) -> None:
    assert "guard_replaced" not in ответ, ответ
    assert "[[" not in ответ and токен_чтения not in ответ, ответ
    assert известное not in ответ


@pytest.mark.parametrize(("известное", "неизвестное", "entity", "поле", "шаблон"), ДОГАДКИ)
async def test_Ruling_63_create_догадка_в_поле_без_класса_байт_в_байт(
    среда, одинс, известное, неизвестное, entity, поле, шаблон
):
    """`Комментарий` документа (класса нет): ответ подготовки на известное словарю значение и на
    неизвестное совпадают байт в байт, токена чтения и `guard_replaced` нет, тексты подтверждения
    после стража совпадают. В 1С — текст как прислан."""
    запись, стор, tools = среда
    токен_чтения = _маска_в_базе(tools, "ut", известное, entity=entity, поле=поле)

    ответы = [
        await создать(запись, {"Комментарий": шаблон.format(з)}, entity=РЕАЛИЗАЦИЯ)
        for з in (известное, неизвестное)
    ]

    assert _без_номера(ответы[0]) == _без_номера(ответы[1]), ответы
    _без_догадки(ответы[0], известное, токен_чтения)
    assert json.loads(ответы[0])["preview"] == [
        {"field": "Комментарий", "value": "значение из запроса"}
    ]
    операции = [await стор.take(json.loads(о)["pending_id"], "sess-1") for о in ответы]
    assert [о.request["json"]["Комментарий"] for о in операции] == [
        шаблон.format(известное),
        шаблон.format(неизвестное),
    ]
    гейт = tools._gate_for(tools._config.bases["ut"])
    тексты = {гейт.finish_text(service._текст_подтверждения(о)) for о in операции}
    assert len(тексты) == 1, тексты
    assert not одинс.обращались


@pytest.mark.parametrize(("известное", "неизвестное", "entity", "поле", "шаблон"), ДОГАДКИ)
async def test_Ruling_63_create_догадка_в_строке_табличной_части_байт_в_байт(
    среда, одинс, известное, неизвестное, entity, поле, шаблон
):
    """То же для строки табличной части: `Товары.ИдентификаторСтроки` (строка, класса нет) —
    пометкой, `LineNumber` (число) — как есть."""
    запись, стор, tools = среда
    токен_чтения = _маска_в_базе(tools, "ut", известное, entity=entity, поле=поле)

    ответы = [
        await создать(
            запись,
            {"Товары": [{"LineNumber": 1, "ИдентификаторСтроки": шаблон.format(з)}]},
            entity=РЕАЛИЗАЦИЯ,
        )
        for з in (известное, неизвестное)
    ]

    assert _без_номера(ответы[0]) == _без_номера(ответы[1]), ответы
    _без_догадки(ответы[0], известное, токен_чтения)
    assert json.loads(ответы[0])["preview"] == [
        {
            "field": "Товары",
            "value": [{"LineNumber": 1, "ИдентификаторСтроки": "значение из запроса"}],
        }
    ]
    assert len(await _тексты_подтверждения(tools, стор, ответы)) == 1


async def test_Ruling_63_значение_известное_только_другой_базе_не_сопоставляется(среда, одинс):
    """ФП-1: словарь общий для всех баз домашнего каталога. Название, которое словарь знает
    только по базе `ro`, в поле без класса базы `ut` страж заменял бы токеном `ro` — граница
    `--bases` не держала. С пометкой ответ тот же, что на незнакомое название."""
    запись, _, tools = среда
    чужое = "ООО Только В Другой Базе"
    токен_ro = _маска_в_базе(tools, "ro", чужое, entity=КОНТРАГЕНТЫ, поле="Description")
    assert токен_ro.startswith("[[org:")

    ответы = [
        await создать(запись, {"Комментарий": f"звонить {з}"}, entity=РЕАЛИЗАЦИЯ)
        for з in (чужое, "ООО Незнакомка")
    ]

    assert _без_номера(ответы[0]) == _без_номера(ответы[1]), ответы
    _без_догадки(ответы[0], чужое, токен_ro)


async def test_Ruling_63_дата_модели_пометкой_цифровой_слой_в_ней_работает(среда, одинс):
    """Дата — JSON-строка, и цифровой слой стража в ней работает: дефис — разделитель серии,
    «2026-01-15» даёт окно «20260115», а известный словарю телефон из этих цифр страж заменил бы
    (положительный контроль ниже). Формат записи проверяет только вид цифр — модель кладёт в дату
    любую догадку. Поэтому дата модели — пометкой; пустая дата 1С — как есть (очистка поля)."""
    запись, _, tools = среда
    гейт = tools._gate_for(tools._config.bases["ut"])
    _маска_в_базе(tools, "ut", "2026-01-15", entity=БАНК, поле="ТелефоныБанка")
    # Контроль: без пометки та же строка в конверте вернулась бы токеном.
    assert "guard_replaced" in гейт.finish({"v": "2026-01-15T00:00:00"}, None)

    ответы = [
        await создать(запись, {"ДоверенностьДата": дата}, entity=РЕАЛИЗАЦИЯ)
        for дата in ("2026-01-15T00:00:00", "2027-03-16T00:00:00")
    ]
    пустая = json.loads(
        await создать(запись, {"ДоверенностьДата": "0001-01-01T00:00:00"}, entity=РЕАЛИЗАЦИЯ)
    )

    assert _без_номера(ответы[0]) == _без_номера(ответы[1]), ответы
    assert "guard_replaced" not in ответы[0] and "[[" not in ответы[0]
    assert json.loads(ответы[0])["preview"] == [
        {"field": "ДоверенностьДата", "value": "значение из запроса"}
    ]
    assert пустая["preview"] == [{"field": "ДоверенностьДата", "value": "0001-01-01T00:00:00"}]


async def test_Ruling_63_число_булево_GUID_как_есть_страж_их_не_трогает(среда, одинс):
    """Числа, булевы и GUID (инвариант 6) — как есть: в JSON-конверте число и булево — не строки,
    а страж проходит только строковые литералы JSON; GUID он глушит целиком. Число, совпадающее
    с известным словарю ИНН, остаётся числом, `guard_replaced` нет."""
    запись, _, tools = среда
    токен(tools, ИНН)
    тело = {"СуммаДокумента": int(ИНН), "Согласован": True, "Контрагент_Key": ССЫЛКА}

    ответ = json.loads(await создать(запись, тело, entity=РЕАЛИЗАЦИЯ))

    assert ответ["preview"] == [
        {"field": "СуммаДокумента", "value": int(ИНН)},
        {"field": "Согласован", "value": True},
        {"field": "Контрагент_Key", "value": ССЫЛКА},
    ]
    assert "guard_replaced" not in json.dumps(ответ, ensure_ascii=False)


# ---------------------------------------------------------------------------------------------
# Ruling 45: подготовка create не пишет в общий словарь гейта ничего
# ---------------------------------------------------------------------------------------------

СЛУЧАИ_СЛОВАРЯ = [
    pytest.param(
        КОНТРАГЕНТЫ,
        {
            "Description": "ООО Тверская Плаза",
            "НаименованиеПолное": "Общество с ограниченной ответственностью «Тверская Плаза»",
            "ИНН": ЕЩЁ_ИНН,
            "КПП": "773601001",
            "ДополнительнаяИнформация": "звонить +7 495 123-45-67, ИНН 7736050003",
            "КонтактнаяИнформация": [
                {"Тип": "Телефон", "Представление": НОВЫЙ_ТЕЛЕФОН, "НомерТелефона": "74957001122"},
                {"Тип": "АдресЭлектроннойПочты", "Представление": "petr@example.org"},
            ],
        },
        id="новые-литералы-шапки-и-табличной-части",
    ),
    pytest.param(
        БАНК,
        {"Description": "Расчётный счёт", "ТелефоныБанка": "0000711"},
        id="телефон-без-контрольной-суммы",
    ),
    pytest.param(
        РЕАЛИЗАЦИЯ,
        {"Комментарий": "позвонить 0000711, ИНН 7728168971, +7 495 123-45-67"},
        id="поле-без-класса-с-реквизитами-в-тексте",
    ),
]


@pytest.mark.parametrize(("сущность", "данные"), СЛУЧАИ_СЛОВАРЯ)
async def test_подготовка_создания_не_меняет_словарь(среда, одинс, сущность, данные):
    """Главная проверка Ruling 45 для `create` — от состояния словаря: число строк во всех
    таблицах `gate.sqlite` до и после одинаково. Словарь до подготовки не пуст (карточка
    прочитана через шлюз), а литералы модели узнают детекторы (ИНН с верной контрольной суммой,
    телефоны). Операция при этом подготовлена — проверяется путь до конца, а не отказ."""
    запись, _, tools = среда
    await прочитать_контрагента(tools, одинс)
    до = строки_словаря(tools)

    ответ = json.loads(await создать(запись, данные, entity=сущность))

    assert "pending_id" in ответ, ответ
    assert строки_словаря(tools) == до
    assert not одинс.обращались


# ---------------------------------------------------------------------------------------------
# create: отказы до обращения к 1С — разрешения SPEC §7.1, устройство тела, поля и типы
# ---------------------------------------------------------------------------------------------

СТРОКА_КИ = {"Тип": "Телефон", "Представление": НОВЫЙ_ТЕЛЕФОН}

ОТКАЗЫ_CREATE = [
    pytest.param("ro", КОНТРАГЕНТЫ, {"Description": "x"}, "base_read_only", id="write-false"),
    pytest.param(
        "ut", КОНТРАГЕНТЫ, {"КодПоОКПО": "09226071"}, "field_write_denied", id="deny-шапка"
    ),
    pytest.param(
        "ut",
        КОНТРАГЕНТЫ,
        {"КонтактнаяИнформация": [{"Регион": "Москва"}]},
        "field_write_denied",
        id="deny-поле-строки",
    ),
    pytest.param(
        "ut",
        ДВИЖЕНИЯ,
        {"Recorder": ССЫЛКА_ДОК, "Recorder_Type": "StandardODATA.Document_X"},
        "permission_denied",
        id="регистр-без-register_direct_write",
    ),
    pytest.param(
        "ut",
        "InformationRegister_КурсыВалют_SliceLast",
        {"Курс": 1.0},
        "permission_denied",
        id="виртуальная-таблица",
    ),
    pytest.param("ut", КОНТРАГЕНТЫ, {}, "params_invalid", id="пустое-тело"),
    pytest.param("ut", КОНТРАГЕНТЫ, ["Description"], "params_invalid", id="тело-не-словарь"),
    pytest.param("ut", КОНТРАГЕНТЫ, {"Ref_Key": ССЫЛКА}, "params_invalid", id="Ref_Key"),
    pytest.param("ut", КОНТРАГЕНТЫ, {"DataVersion": ВЕРСИЯ}, "params_invalid", id="DataVersion"),
    pytest.param("ut", РЕАЛИЗАЦИЯ, {"Posted": True}, "params_invalid", id="Posted"),
    pytest.param("ut", КОНТРАГЕНТЫ, {"DeletionMark": True}, "params_invalid", id="DeletionMark"),
    pytest.param("ut", КОНТРАГЕНТЫ, {"Predefined": True}, "params_invalid", id="Predefined"),
    pytest.param(
        "ut", КОНТРАГЕНТЫ, {"PredefinedDataName": "x"}, "params_invalid", id="PredefinedDataName"
    ),
    pytest.param("ut", КОНТРАГЕНТЫ, {"НетТакогоПоля": "x"}, "params_invalid", id="нет-поля"),
    pytest.param("ut", КИ, {"Представление": "x"}, "params_invalid", id="строка-ТЧ-сущностью"),
    pytest.param(
        "ut",
        КОНТРАГЕНТЫ,
        {"КонтактнаяИнформация": СТРОКА_КИ},
        "params_invalid",
        id="ТЧ-не-списком",
    ),
    pytest.param(
        "ut",
        КОНТРАГЕНТЫ,
        {"КонтактнаяИнформация": ["Телефон"]},
        "params_invalid",
        id="строка-ТЧ-не-словарь",
    ),
    pytest.param(
        "ut",
        КОНТРАГЕНТЫ,
        {"КонтактнаяИнформация": [{**СТРОКА_КИ, "Ref_Key": ССЫЛКА}]},
        "params_invalid",
        id="Ref_Key-в-строке",
    ),
    pytest.param(
        "ut",
        КОНТРАГЕНТЫ,
        {"КонтактнаяИнформация": [{**СТРОКА_КИ, "НетТакогоПоля": "x"}]},
        "params_invalid",
        id="нет-поля-в-строке",
    ),
    pytest.param(
        "ut",
        КОНТРАГЕНТЫ,
        {"КонтактнаяИнформация": [{**СТРОКА_КИ, "LineNumber": "1"}]},
        "params_invalid",
        id="строка-в-Int64-строки",
    ),
    pytest.param(
        "ut",
        КОНТРАГЕНТЫ,
        {"КонтактнаяИнформация": [{**СТРОКА_КИ, "Вид_Key": "не-гуид"}]},
        "params_invalid",
        id="не-GUID-в-строке",
    ),
    pytest.param(
        "ut",
        КОНТРАГЕНТЫ,
        {"ИсторияКПП": [{"КПП": "770701001"}]},
        "params_invalid",
        id="ТЧ-вне-индекса",
    ),
    pytest.param(
        "ut", КОНТРАГЕНТЫ, {"НДСПоСтавкам4и2": "true"}, "params_invalid", id="строка-в-Boolean"
    ),
    pytest.param("ut", РЕАЛИЗАЦИЯ, {"СуммаДокумента": float("nan")}, "params_invalid", id="NaN"),
    pytest.param(
        "ut", РЕАЛИЗАЦИЯ, {"Date": "26.08.2026"}, "params_invalid", id="дата-не-по-формату"
    ),
    pytest.param("ut", РЕАЛИЗАЦИЯ, {"Комментарий": None}, "params_invalid", id="null"),
    pytest.param(
        "ut", КОНТРАГЕНТЫ, {"ИНН": "7707083894"}, "filter_syntax", id="ИНН-без-контрольной-суммы"
    ),
    # Предвидимый отказ 1С: журнал документов и константа не создаются ни при каком теле.
    pytest.param(
        "ut",
        "DocumentJournal_СогласияНаОбработкуПерсональныхДанных",
        {"Комментарий": "x"},
        "params_invalid",
        id="журнал-документов",
    ),
    pytest.param(
        "ut",
        "Constant_Xx_АвтоматическиСоздаватьАктыРасхождений",
        {"Value": True},
        "params_invalid",
        id="константа",
    ),
]


@pytest.mark.parametrize(("база", "сущность", "данные", "код"), ОТКАЗЫ_CREATE)
async def test_create_отказывает_до_обращения_к_1С(среда, одинс, база, сущность, данные, код):
    """Порядок SPEC §7.1 и проверки тела — ни одного обращения к 1С, операции нет, словарь не
    тронут (отказ с литералом модели маскируется без записи, Ruling 48)."""
    запись, стор, tools = среда
    до = строки_словаря(tools)

    текст = await создать(запись, данные, base=база, entity=сущность)

    assert ошибка(текст)["code"] == код, текст
    assert not одинс.обращались
    assert стор._ops == {}
    assert строки_словаря(tools) == до


def _скрыть(дом, имя: str) -> None:
    путь = policy_path(дом, "ut")
    политика = yaml.safe_load(путь.read_text(encoding="utf-8")) or {}
    политика.setdefault("entities", {})[имя] = {"hide": True}
    путь.write_text(yaml.safe_dump(политика, allow_unicode=True), encoding="utf-8")


async def test_скрытая_сущность_и_скрытая_табличная_часть_отказ_до_обращения_к_1С(
    среда, одинс, дом
):
    """Скрытая сущность отвечает как у чтения — `entity_hidden` раньше полей. Скрытая табличная
    часть не пишется через тело владельца: иначе запрет владельца обходился бы созданием."""
    запись, стор, _ = среда
    _скрыть(дом, КИ)

    строки = await создать(запись, {"Description": "x", "КонтактнаяИнформация": [СТРОКА_КИ]})
    _скрыть(дом, РЕАЛИЗАЦИЯ)
    документ_ = await создать(запись, {"НетТакогоПоля": "x"}, entity=РЕАЛИЗАЦИЯ)

    assert ошибка(строки)["code"] == "entity_hidden"
    assert ошибка(документ_)["code"] == "entity_hidden" and not ошибка(документ_)["hint"]
    assert not одинс.обращались
    assert стор._ops == {}


async def test_отказы_create_называют_поле_и_нужный_тул(среда, одинс):
    запись, _, _ = среда

    ссылка = ошибка(await создать(запись, {"Ref_Key": ССЫЛКА}))
    проведение = ошибка(await создать(запись, {"Posted": True}, entity=РЕАЛИЗАЦИЯ))
    пометка = ошибка(await создать(запись, {"DeletionMark": True}))
    строка = ошибка(await создать(запись, {"Представление": "x"}, entity=КИ))
    вне_индекса = ошибка(await создать(запись, {"ИсторияКПП": []}))
    навигация = ошибка(await создать(запись, {"ГоловнойКонтрагент": ССЫЛКА}))

    assert "create" in ссылка["message"] and "1С" in ссылка["hint"]
    assert "odata1c_action" in проведение["hint"]
    assert "odata1c_mark_for_deletion" in пометка["hint"]
    assert КОНТРАГЕНТЫ in строка["hint"] and "КонтактнаяИнформация" in строка["hint"]
    # `ИсторияКПП` в урезанной фикстуре — коллекция без сущности строки: реиндекс того же
    # `$metadata` её не добавит, подсказки «обновите индекс» нет (Н6r2-2).
    assert "табличные части этой сущности записью не задаются" in вне_индекса["message"]
    assert "reindex" not in вне_индекса["hint"] and "регистр" not in вне_индекса["hint"]
    assert "ГоловнойКонтрагент_Key" in навигация["hint"]


ИМЯ_С_МЕТКОЙ = f"Поле{ЦИФРЫ_ТЕЛЕФОНА}"


def _без_метки(ответ: dict, т: str) -> None:
    текст = json.dumps(ответ, ensure_ascii=False)
    assert ТЕЛЕФОН not in текст and ЦИФРЫ_ТЕЛЕФОНА not in текст, текст
    assert т not in текст and "[[phone:" not in текст, текст


async def test_отказ_create_по_имени_поля_не_повторяет_имя(среда, одинс):
    """Ruling 53: имя, которого нет в индексе, — вход модели и не повторяется ни для шапки, ни
    для строки табличной части; метка — цифры известного словарю телефона в имени (иначе слой
    цифр стража вернул бы их токеном — ответ подтвердил бы телефон)."""
    запись, _, tools = среда
    т = токен(tools, ТЕЛЕФОН, entity=БАНК, поле="ТелефоныБанка")

    шапка = ошибка(await создать(запись, {ИМЯ_С_МЕТКОЙ: "x"}))
    строка = ошибка(await создать(запись, {"КонтактнаяИнформация": [{ИМЯ_С_МЕТКОЙ: "x"}]}))
    # Имя табличной части с меткой: место в отказе строки (Ruling 56) строится только из имён
    # индекса — неизвестное имя со списком строк отклоняется как поле шапки, до раскрытия.
    часть = ошибка(await создать(запись, {ИМЯ_С_МЕТКОЙ: [{"Представление": "x"}]}))

    assert шапка["code"] == строка["code"] == часть["code"] == "params_invalid"
    assert КОНТРАГЕНТЫ in шапка["message"] and КИ in строка["message"]
    assert КОНТРАГЕНТЫ in часть["message"] and "табличная часть" not in часть["message"]
    _без_метки(шапка, т)
    _без_метки(строка, т)
    _без_метки(часть, т)
    assert not одинс.обращались


def test_body_preview_строка_на_каждое_поле_в_порядке_тела():
    строки = [{"Тип": "Телефон", "Представление": "[[phone:A]]"}]

    assert body_preview({"ИНН": "[[inn:B]]", "Комментарий": "x", "КИ": строки}) == [
        {"field": "ИНН", "value": "[[inn:B]]"},
        {"field": "Комментарий", "value": "x"},
        {"field": "КИ", "value": строки},
    ]


async def test_М6_1_токен_названия_в_Description_объект_превью_тот_же_токен(среда, одинс):
    """М6-1 ревью задачи 6: `object` — из данных модели, а не из раскрытого тела. Иначе реальное
    название ушло бы в `object` и его заменил бы только страж — последний рубеж."""
    запись, стор, tools = среда
    название = токен(tools, НАЗВАНИЕ, поле="Description")
    assert название.startswith("[[org:")

    текст = await создать(запись, {"Description": название, "ИНН": ЕЩЁ_ИНН})

    нет_реальных_значений(текст)
    ответ = json.loads(текст)
    assert ответ["object"] == {"Description": название}
    операция = await стор.take(ответ["pending_id"], "sess-1")
    assert операция.preview["object"] == {"Description": название}
    assert операция.request["json"]["Description"] == НАЗВАНИЕ


# ---------------------------------------------------------------------------------------------
# Ruling 56 (Н6-1, Н6-3 ревью задачи 6): строка табличной части раскрывается по своей сущности
# ---------------------------------------------------------------------------------------------

ИСТОРИЯ_КПП = "Catalog_Контрагенты_ИсторияКПП"
ВСД = "Catalog_ВетеринарноСопроводительныйДокументВЕТИС"
МАРШРУТ = f"{ВСД}_Маршрут"
АДРЕС_ДОСТАВКИ = "г. Пробный, ул. Вымышленная, д. 7, кв. 12"

# Табличная часть `ИсторияКПП` в урезанной фикстуре не опубликована (поле `Collection(…)` есть,
# сущности строки нет); на живой УТ табличные части публикуются набором `<владелец>_<ТЧ>` (P4).
_ТИП_ИСТОРИИ = f"""<EntityType Name="{ИСТОРИЯ_КПП}">
        <Key><PropertyRef Name="Ref_Key"/><PropertyRef Name="LineNumber"/></Key>
        <Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
        <Property Name="LineNumber" Type="Edm.Int64" Nullable="false"/>
        <Property Name="Период" Type="Edm.DateTime" Nullable="true"/>
        <Property Name="КПП" Type="Edm.String" Nullable="true"/>
      </EntityType>
      """
# ВСД ВЕТИС — тем же устройством, что на полном `$metadata` УТ (скан ревьюера): поле строки
# `Маршрут.Адрес` по своей сущности — `keep`, а имя «Адрес» на владельце классифицируется `addr`.
_ТИПЫ_ВСД = f"""<EntityType Name="{ВСД}">
        <Key><PropertyRef Name="Ref_Key"/></Key>
        <Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
        <Property Name="DataVersion" Type="Edm.String" Nullable="true"/>
        <Property Name="DeletionMark" Type="Edm.Boolean" Nullable="true"/>
        <Property Name="Description" Type="Edm.String" Nullable="true"/>
        <Property Name="Маршрут" Type="Collection(StandardODATA.{МАРШРУТ}_RowType)"
          Nullable="true"/>
      </EntityType>
      <EntityType Name="{МАРШРУТ}">
        <Key><PropertyRef Name="Ref_Key"/><PropertyRef Name="LineNumber"/></Key>
        <Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
        <Property Name="LineNumber" Type="Edm.Int64" Nullable="false"/>
        <Property Name="Адрес" Type="Edm.String" Nullable="true"/>
        <Property Name="АдресПредставление" Type="Edm.String" Nullable="true"/>
      </EntityType>
      """
_НАБОРЫ = (
    f'<EntitySet Name="{ИСТОРИЯ_КПП}" EntityType="StandardODATA.{ИСТОРИЯ_КПП}"/>\n        '
    f'<EntitySet Name="{ВСД}" EntityType="StandardODATA.{ВСД}"/>\n        '
    f'<EntitySet Name="{МАРШРУТ}" EntityType="StandardODATA.{МАРШРУТ}"/>\n        '
)


def _edmx_с_табличными_частями(edmx: bytes) -> bytes:
    текст = edmx.decode("utf-8")
    тип, набор = f'<EntityType Name="{КИ}">', f'<EntitySet Name="{КИ}"'
    assert тип in текст and набор in текст
    текст = текст.replace(тип, _ТИП_ИСТОРИИ + _ТИПЫ_ВСД + тип, 1)
    return текст.replace(набор, _НАБОРЫ + набор, 1).encode("utf-8")


async def _среда_на(tmp_path, edmx: bytes, bases_yaml: str, поля: dict[str, str] | None = None):
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
    if поля:
        путь = policy_path(home, "ut")
        политика = yaml.safe_load(путь.read_text(encoding="utf-8")) or {}
        политика.setdefault("fields", {}).update(поля)
        путь.write_text(yaml.safe_dump(политика, allow_unicode=True), encoding="utf-8")
    tools = ToolService(load_config(home))
    стор = PendingStore(600, clock=Часы(1000.0))
    журнал = Journal(tmp_path / "journal.sqlite")
    запись = WriteService(tools, стор, журнал, CommitLimiter(), clock=Часы(1_757_000_000.0))
    return запись, стор, tools, журнал


@pytest.fixture
async def среда_тч(tmp_path, edmx_ut_real):
    """Фикстура с опубликованными `ИсторияКПП` и ВСД ВЕТИС; политика — как её строит реиндекс,
    плюс ручной `keep` на `ИсторияКПП.КПП` (владелец счёл историю КПП неконфиденциальной)."""
    запись, стор, tools, журнал = await _среда_на(
        tmp_path,
        _edmx_с_табличными_частями(edmx_ut_real),
        BASES_YAML,
        {f"{ИСТОРИЯ_КПП}.КПП": "keep"},
    )
    yield запись, стор, tools
    журнал.close()
    await tools.aclose()


def _классы(tools: ToolService, *пары: tuple[str, str]) -> list[str | None]:
    гейт = tools._gate_for(tools._config.bases["ut"])
    репозиторий = tools._open_index(tools._config.bases["ut"])
    try:
        строение = tools._строение(репозиторий)
        return [гейт.field_class(сущность, поле, shape=строение) for сущность, поле in пары]
    finally:
        репозиторий.close()


async def test_Н6_1_токен_адреса_в_поле_строки_открытом_по_её_сущности_отказ(среда_тч, одинс):
    """Н6-1 ревью задачи 6 в обратной форме. `Маршрут.Адрес` ВСД по своей сущности — `keep`
    (чтение отдаёт его открытым), а имя «Адрес» на владельце — `addr`. Раньше строка
    раскрывалась от владельца, и токен адреса доставки уходил в 1С реальным адресом — после
    `commit` чтение показало бы его открытым, а у `addr` нет последнего рубежа в страже. Теперь
    строка раскрывается по своей сущности: токен `addr` в поле `keep` — отказ, операции нет."""
    запись, стор, tools = среда_тч
    assert _классы(tools, (МАРШРУТ, "Адрес"), (ВСД, "Адрес")) == ["keep", "addr"]
    адрес = токен(tools, АДРЕС_ДОСТАВКИ, entity=РЕАЛИЗАЦИЯ, поле="АдресДоставки")
    assert адрес.startswith("[[addr:")

    текст = await создать(
        запись, {"Description": "ВСД проба", "Маршрут": [{"Адрес": адрес}]}, entity=ВСД
    )

    отказ = ошибка(текст)
    assert отказ["code"] == "token_type_mismatch", отказ
    assert АДРЕС_ДОСТАВКИ not in текст
    assert стор._ops == {}
    assert not одинс.обращались


async def test_Н6_1б_токен_КПП_шапки_в_строку_с_keep_отказ(среда_тч, одинс):
    """Цифровой класс: владелец открыл КПП в истории (`keep` на поле строки), токен КПП шапки,
    прочитанный через настоящий `get`, в эту строку не раскрывается. Раньше раскрывался по
    классу «КПП» шапки, и на чтении такой КПП ловил только страж."""
    запись, стор, tools = среда_тч
    assert _классы(tools, (ИСТОРИЯ_КПП, "КПП")) == ["keep"]
    кпп = (await прочитать_контрагента(tools, одинс))["КПП"]
    assert кпп.startswith("[[") and КПП not in кпп

    текст = await создать(запись, {"Description": "ООО Проба", "ИсторияКПП": [{"КПП": кпп}]})

    assert ошибка(текст)["code"] == "token_type_mismatch"
    assert КПП not in текст
    assert стор._ops == {}
    assert not одинс.обращались


async def test_Н6_3_строка_табличной_части_с_защищаемым_корнем_имени_создаётся(среда_тч, одинс):
    """Н6-3: имя табличной части `ИсторияКПП` содержит корень защищаемого имени, и раскрытие
    строки от владельца отклоняло любое её строковое поле правилом пути отбора («промежуточный
    сегмент указывает на связанный объект»). По своей сущности поле строки — обычное поле."""
    запись, стор, _ = среда_тч
    строки = [{"LineNumber": 1, "Период": "2026-01-01T00:00:00", "КПП": "770701002"}]

    ответ = json.loads(await создать(запись, {"Description": "ООО Проба", "ИсторияКПП": строки}))

    assert "pending_id" in ответ, ответ
    операция = await стор.take(ответ["pending_id"], "sess-1")
    assert операция.request["json"]["ИсторияКПП"] == строки
    assert not одинс.обращались


async def test_нумерация_токенов_сквозная_по_всему_телу(среда, одинс):
    """Ruling 54 при раскрытии строк отдельными вызовами: номер токена в отказе — по порядку
    всего тела, как его видит модель, а место называет табличную часть, номер строки и поле
    (имена из индекса). Токен шапки стоит в теле ПОСЛЕ табличной части — и нумеруется после её
    токенов, хотя шапка раскрывается первой (без заранее выданных номеров он стал бы «первым»,
    а отклонённый токен строки — «вторым»)."""
    запись, _, tools = среда
    ток_инн = токен(tools, НОВЫЙ_ИНН)
    чужой = "[[inn:ZZZZZZZZZZ]]"

    отказ = ошибка(
        await создать(
            запись,
            {
                "Description": "ООО Проба",
                "КонтактнаяИнформация": [
                    {"Тип": "Телефон", "Представление": НОВЫЙ_ТЕЛЕФОН},
                    {"Тип": "Телефон", "Представление": чужой},
                ],
                "ИНН": ток_инн,
            },
        )
    )

    текст = json.dumps(отказ, ensure_ascii=False)
    assert (
        "первый токен в теле записи, табличная часть «КонтактнаяИнформация», строка 2, "
        "поле «Представление»" in отказ["message"]
    ), отказ
    assert ток_инн not in текст and чужой not in текст


async def test_нумерация_токенов_не_начинается_заново_в_строке(среда, одинс):
    """Второй токен тела — в строке 1 после токена шапки: «второй», а не «первый»."""
    запись, _, tools = среда
    ток_инн = токен(tools, НОВЫЙ_ИНН)

    отказ = ошибка(
        await создать(
            запись,
            {
                "ИНН": ток_инн,
                "КонтактнаяИнформация": [{"Тип": "Телефон", "Представление": "[[inn:ZZZZZZZZZZ]]"}],
            },
        )
    )

    assert (
        "второй токен в теле записи, табличная часть «КонтактнаяИнформация», строка 1, "
        "поле «Представление»" in отказ["message"]
    ), отказ


# --- Н6r2-1: место строки в отказе строит гейт, а не вызывающий ------------------------------

ВЛАДЕЛЕЦ_Т = "Catalog_Проба"
СТРОКА_Т = "Catalog_Проба_Товары"
ЧУЖОЙ_ТОКЕН = "[[inn:ZZZZZZZZZZ]]"


def _раскрыть_строку(tmp_path, *, имя_в_индексе: bool = True, **параметры):
    """`Unmasker.write` строки табличной части с неизвестным словарю токеном в поле `ИНН`:
    отказ `token_unknown` называет место. Строение — синтетика: владелец знает поле «Товары»
    (или нет), строка знает `ИНН` и владельца."""
    строения = {
        ВЛАДЕЛЕЦ_Т: EntityShape(
            fields=frozenset({"Description"} | ({"Товары"} if имя_в_индексе else set()))
        ),
        СТРОКА_Т: EntityShape(fields=frozenset({"ИНН"}), parent=ВЛАДЕЛЕЦ_Т),
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    словарь = Dictionary(tmp_path / "gate.sqlite", "секрет ровно для обратной подмены".encode())
    try:
        обратно = Unmasker(
            словарь,
            base="ut",
            field_class=lambda сущность, поле, *, strict=False: (
                "inn" if (сущность, поле) == (СТРОКА_Т, "ИНН") else None
            ),
            path_class=без_класса_пути,
            shape=строения.get,
        )
        with pytest.raises(GateError) as отказ:
            обратно.write(
                {"ИНН": ЧУЖОЙ_ТОКЕН},
                entity=СТРОКА_Т,
                current=None,
                revealed=RevealedValues(),
                numbering={"[[inn:AAAAAAAAAA]]": 1},
                **параметры,
            )
    finally:
        словарь.close()
    return отказ.value


def test_Н6r2_1_имя_табличной_части_в_отказе_гейт_берёт_из_индекса(tmp_path):
    """Вызывающий передаёт только номер строки; имя табличной части гейт выводит из строения
    (`EntityShape.parent`) и проверяет, что владелец знает такое поле (Ruling 53)."""
    отказ = _раскрыть_строку(tmp_path, row=2)

    assert отказ.code == "token_unknown", отказ
    assert "второй токен в теле записи, табличная часть «Товары», строка 2, поле «ИНН»" in str(
        отказ
    ), отказ


def test_Н6r2_1_имя_табличной_части_вне_индекса_в_отказ_не_попадает(tmp_path):
    """Владелец не знает поля «Товары» — имя из сущности строки не подтверждено индексом и в текст
    не идёт; место — номер строки без имени."""
    отказ = _раскрыть_строку(tmp_path, имя_в_индексе=False, row=2)

    assert "Товары" not in str(отказ) + отказ.hint
    assert "в теле записи, строка 2 табличной части, поле «ИНН»" in str(отказ), отказ


def test_Н6r2_1_сигнатура_раскрытия_не_берёт_свободного_текста(tmp_path):
    """В текст отказа от вызывающего идёт только номер строки: параметра-строки места нет ни у
    гейта, ни у `Unmasker`, а номер — целое не меньше 1 (строка и `bool` отклоняются)."""
    for функция in (Unmasker.write, BaseGate.inbound_write):
        параметры = inspect.signature(функция, eval_str=True).parameters
        assert "place" not in параметры
        assert параметры["row"].annotation == int | None
        assert all(
            параметр.kind is not inspect.Parameter.VAR_KEYWORD for параметр in параметры.values()
        )
    for номер, плохой in enumerate(("в теле записи, табличная часть «x», строка 1", True, 0, 1.0)):
        with pytest.raises(TypeError):
            _раскрыть_строку(tmp_path / str(номер), row=плохой)


# --- Н6r2-2: табличная часть без сущности строки -----------------------------------------------

РЕГИСТР_НАКОПЛЕНИЯ = "AccumulationRegister_ДвиженияДенежныеСредстваКонтрагент"
ТЕЛО_НАБОРА = {
    "Recorder": ССЫЛКА_ДОК,
    "Recorder_Type": "StandardODATA.Document_X",
    "RecordSet": [{"LineNumber": "1"}],
}
BASES_РЕГИСТРЫ = BASES_YAML.replace(
    "deny_fields: [", "register_direct_write: true\n      deny_fields: [", 1
)


@pytest.fixture
async def среда_регистры(tmp_path, edmx_ut_real):
    assert "register_direct_write: true" in BASES_РЕГИСТРЫ
    запись, стор, tools, журнал = await _среда_на(tmp_path, edmx_ut_real, BASES_РЕГИСТРЫ)
    yield запись, стор, tools
    журнал.close()
    await tools.aclose()


async def test_Н6r2_2_табличная_часть_без_сущности_строки_отказ_без_реиндекса(среда, одинс):
    """Поле-коллекция без своей сущности строки среди детей владельца — отказ при разборе тела,
    прямо и без ложной подсказки «обновите индекс»: поля и дети владельца — из одного снимка
    `$metadata`, реиндекс ничего не изменит. На полном УТ такие поля — только `RecordSet`
    зависимых регистров, и их после Ruling 57 раньше отклоняет шаг 3 разрешений; здесь — коллекция
    `ИсторияКПП` урезанной фикстуры, у которой сущность строки не опубликована."""
    запись, стор, _ = среда

    отказ = ошибка(await создать(запись, {"Description": "ООО Проба", "ИсторияКПП": []}))

    assert отказ["code"] == "params_invalid", отказ
    assert "табличные части этой сущности записью не задаются" in отказ["message"]
    assert "ИсторияКПП" in отказ["message"]
    текст = отказ["message"] + отказ["hint"]
    assert "обновите" not in текст and "reindex" not in текст
    assert not одинс.обращались and стор._ops == {}


# --- Ruling 57: зависимый регистр в первой поставке не пишется ---------------------------------

КУРСЫ_ВАЛЮТ = "InformationRegister_КурсыВалют"
ТЕЛО_КУРСА = {
    "Period": "2026-01-01T00:00:00",
    "Валюта_Key": ССЫЛКА,
    "Курс": 91.25,
    "Кратность": 1,
}


@pytest.fixture(params=[False, True], ids=["без-флага", "с-флагом"])
async def среда_флаг(request, tmp_path, edmx_ut_real):
    """Та же база `ut` без `register_direct_write` и с ним."""
    запись, стор, tools, журнал = await _среда_на(
        tmp_path, edmx_ut_real, BASES_РЕГИСТРЫ if request.param else BASES_YAML
    )
    yield запись, стор, tools
    журнал.close()
    await tools.aclose()


def _отказ_регистра(отказ: dict, сущность: str) -> None:
    assert отказ["code"] == "permission_denied", отказ
    assert сущность in отказ["message"]
    assert "подчинённых регистратору" in отказ["hint"] and "odata1c_action" in отказ["hint"]


@pytest.mark.parametrize(
    ("сущность", "тело"),
    [
        pytest.param(
            РЕГИСТР_НАКОПЛЕНИЯ,
            {"Recorder": ССЫЛКА_ДОК, "Recorder_Type": "StandardODATA.Document_X"},
            id="основной-набор-без-строк",
        ),
        pytest.param(РЕГИСТР_НАКОПЛЕНИЯ, ТЕЛО_НАБОРА, id="основной-набор-с-RecordSet"),
        pytest.param(
            ДВИЖЕНИЯ,
            {"Recorder": ССЫЛКА_ДОК, "Recorder_Type": "StandardODATA.Document_X"},
            id="RecordType",
        ),
    ],
)
async def test_Ruling_57_create_зависимого_регистра_отказ_при_любом_флаге(
    среда_флаг, сущность, тело, одинс
):
    """Воспроизведение находки раунда 3: `create` основного набора регистра накопления (ключ
    `Recorder`, `is_records=False`) готовил `POST AccumulationRegister_…` без флага. Теперь —
    отказ при любом `register_direct_write`, `…_RecordType` — так же; 1С не вызывается."""
    запись, стор, _ = среда_флаг

    отказ = ошибка(await создать(запись, тело, entity=сущность))

    _отказ_регистра(отказ, сущность)
    assert not одинс.обращались and стор._ops == {}


async def test_Ruling_57_update_основного_набора_зависимого_регистра_отказ(среда_регистры, одинс):
    """`update` основного набора зависимого регистра — тот же отказ, до чтения из 1С и до
    разбора ключа."""
    запись, стор, _ = среда_регистры

    отказ = ошибка(
        await запись.update(
            SessionScope(),
            "sess-1",
            base="ut",
            entity=РЕГИСТР_НАКОПЛЕНИЯ,
            key={"Recorder": ССЫЛКА_ДОК, "Recorder_Type": "StandardODATA.Document_X"},
            data={"Recorder_Type": "StandardODATA.Document_Y"},
        )
    )

    _отказ_регистра(отказ, РЕГИСТР_НАКОПЛЕНИЯ)
    assert not одинс.обращались and стор._ops == {}


async def test_Ruling_60_независимый_регистр_сведений_create_отказ_при_любом_флаге(
    среда_флаг, одинс
):
    """Ruling 60: независимый регистр сведений отклоняется так же, как зависимый (Ruling 57), —
    и без `register_direct_write`, и с ним."""
    запись, стор, _ = среда_флаг

    отказ = ошибка(await создать(запись, ТЕЛО_КУРСА, entity=КУРСЫ_ВАЛЮТ))

    _отказ_регистра(отказ, КУРСЫ_ВАЛЮТ)
    assert стор._ops == {} and not одинс.обращались


async def test_Н6r2_3_шаги_владельца_раньше_шагов_табличных_частей(среда, одинс):
    """SPEC §7.1: сначала все шаги владельца, потом табличные части. База только для чтения
    отклоняет набор записей своим шагом раньше разбора `RecordSet`; запрет поля шапки
    (`deny_fields`) называется раньше запрета поля строки, хотя шапка в теле стоит второй."""
    запись, стор, _ = среда

    только_чтение = ошибка(await создать(запись, ТЕЛО_НАБОРА, base="ro", entity=РЕГИСТР_НАКОПЛЕНИЯ))
    два_запрета = ошибка(
        await создать(
            запись, {"КонтактнаяИнформация": [{"Регион": "Москва"}], "КодПоОКПО": "09226071"}
        )
    )

    assert только_чтение["code"] == "base_read_only", только_чтение
    assert два_запрета["code"] == "field_write_denied", два_запрета
    assert "КодПоОКПО" in два_запрета["message"] and "Регион" not in два_запрета["message"]
    assert not одинс.обращались and стор._ops == {}


# --- И-7: база с выключенным гейтом ------------------------------------------------------------

BASES_С_DEV = (
    BASES_YAML
    + f"""  dev:
    label: копия для разработки, гейт выключен
    url: {URL_UT}
    user: u
    password: p
    role: dev
"""
)


@pytest.fixture
async def среда_dev(tmp_path, edmx_ut_real):
    запись, стор, tools, журнал = await _среда_на(tmp_path, edmx_ut_real, BASES_С_DEV)
    assert tools._gate_for(tools._config.bases["dev"]).mode == "off"
    yield запись, стор, tools
    журнал.close()
    await tools.aclose()


async def test_И7_create_с_токеном_на_базе_без_гейта_отказ(среда_dev, одинс):
    """И-7 итогового ревью M2: роль `dev` по умолчанию — гейт `off` и запись включена. Токен из
    ответа базы с гейтом (словарь общий) раньше уходил в 1С текстом `[[inn:…]]`, и превью
    показывало то же. Теперь — отказ до 1С; без токена `create` готовится."""
    запись, стор, tools = среда_dev
    ток = токен(tools, ИНН)

    отказ = ошибка(await создать(запись, {"Description": "ООО Проба", "ИНН": ток}, base="dev"))
    годное = json.loads(await создать(запись, {"Description": "ООО Проба", "ИНН": ИНН}, base="dev"))

    assert отказ["code"] == "params_invalid" and "гейт этой базы выключен" in отказ["message"]
    assert "pending_id" in годное
    [операция] = стор._ops.values()
    assert операция.request["json"]["ИНН"] == ИНН
    assert not одинс.обращались


async def test_И7_отбор_с_токеном_на_базе_без_гейта_отказ_без_1С(среда_dev, одинс):
    """Чтение на уровне `off`: токен в отборе уходил в 1С текстом, и ответ был пустым вместо
    записи. Та же правка гейта отклоняет его до 1С."""
    _, _, tools = среда_dev
    ток = токен(tools, ИНН)

    отказ = ошибка(
        await tools.query(
            SessionScope(), base="dev", entity=КОНТРАГЕНТЫ, filter=f"ИНН eq '{ток}'", top=1
        )
    )

    assert отказ["code"] == "params_invalid" and "гейт этой базы выключен" in отказ["message"]
    assert not одинс.обращались


BASES_DENY_КИ = BASES_YAML.replace(
    "deny_fields: [", f"deny_entities: [{КИ}]\n      deny_fields: [", 1
)


@pytest.fixture
async def среда_запрет_ки(tmp_path, edmx_ut_real):
    assert "deny_entities" in BASES_DENY_КИ
    запись, стор, tools, журнал = await _среда_на(tmp_path, edmx_ut_real, BASES_DENY_КИ)
    yield запись, стор, tools
    журнал.close()
    await tools.aclose()


async def test_Н6_2_deny_entities_табличной_части_не_обходится_через_create_владельца(
    среда_запрет_ки, одинс
):
    """Н6-2: запрет записи в табличную часть действует и на её строки в теле `create` владельца —
    тем же кодом и текстом, что прямая запись в неё. Владелец без строк этой части создаётся."""
    запись, стор, _ = среда_запрет_ки
    прямо = ошибка(await создать(запись, {"Тип": "Телефон"}, entity=КИ))
    через_владельца = ошибка(
        await создать(
            запись, {"Description": "ООО Проба", "КонтактнаяИнформация": [{"Тип": "Телефон"}]}
        )
    )
    без_строк = json.loads(await создать(запись, {"Description": "ООО Проба"}))

    assert через_владельца["code"] == прямо["code"] == "permission_denied"
    assert через_владельца["message"] == прямо["message"]
    assert "pending_id" in без_строк
    assert len(стор._ops) == 1
    assert not одинс.обращались


# Полный `$metadata` живой УТ (проба P4; файл вне git) и политика, построенная реиндексом, — без
# единой ручной правки владельца. Нет файла — тесты пропускаются.
ПОЛНЫЙ_ДАМП = pathlib.Path(__file__).parent.parent / "fixtures" / "edmx" / "probe.full.edmx"


@pytest.fixture
async def среда_полная(tmp_path):
    if not ПОЛНЫЙ_ДАМП.exists():
        pytest.skip("нет probe.full.edmx")
    запись, стор, tools, журнал = await _среда_на(tmp_path, ПОЛНЫЙ_ДАМП.read_bytes(), BASES_YAML)
    yield запись, стор, tools
    журнал.close()
    await tools.aclose()


async def test_Н6_1_полный_metadata_адрес_доставки_не_раскрывается_в_маршрут_ВСД(
    среда_полная, одинс
):
    """Воспроизведение ревьюера на полном `$metadata` в обратной форме: модель читает заказ
    настоящим `get`, адрес доставки — токеном; строка маршрута ВСД с этим токеном — отказ."""
    запись, стор, tools = среда_полная
    заказ = "Document_ЗаказКлиента"
    assert _классы(tools, (заказ, "АдресДоставки"), (МАРШРУТ, "Адрес")) == ["addr", "keep"]
    одинс.объект({"Ref_Key": ССЫЛКА_ДОК, "DataVersion": ВЕРСИЯ, "АдресДоставки": АДРЕС_ДОСТАВКИ})
    прочитано = json.loads(await tools.get(SessionScope(), base="ut", entity=заказ, key=ССЫЛКА_ДОК))
    адрес = прочитано["item"]["АдресДоставки"]
    assert адрес.startswith("[[addr:")
    одинс.get.reset()

    текст = await создать(запись, {"Маршрут": [{"Адрес": адрес}]}, entity=ВСД)

    assert ошибка(текст)["code"] == "token_type_mismatch"
    assert АДРЕС_ДОСТАВКИ not in текст
    assert стор._ops == {}
    assert not одинс.обращались


СТРОКИ_Н6_3 = [
    pytest.param(
        "Document_ПодтверждениеЗачисленияЗарплаты",
        "Сотрудники",
        {"БИКБанкаСчета": "044525225", "ИдентификаторСтроки": "строка-1"},
        id="ПодтверждениеЗачисленияЗарплаты.Сотрудники",
    ),
    pytest.param(
        "Catalog_МашиночитаемыеДоверенностиОрганизаций",
        "ФИО",
        {"Владелец": "доверитель"},
        id="МашиночитаемыеДоверенности.ФИО",
    ),
]


@pytest.mark.parametrize(("сущность", "часть", "строка"), СТРОКИ_Н6_3)
async def test_Н6_3_полный_metadata_строки_табличных_частей_создаются(
    среда_полная, одинс, сущность, часть, строка
):
    """Две из 18 табличных частей УТ, строки которых раньше отклонялись ложным `filter_syntax`."""
    запись, стор, _ = среда_полная

    ответ = json.loads(await создать(запись, {часть: [строка]}, entity=сущность))

    assert "pending_id" in ответ, ответ
    операция = await стор.take(ответ["pending_id"], "sess-1")
    assert операция.request["json"][часть] == [строка]
    assert not одинс.обращались


# ---------------------------------------------------------------------------------------------
# action: Post/Unpost — форма P8, превью «проведён: было → станет», предвидимые отказы 1С
# ---------------------------------------------------------------------------------------------

ВЫБОР_ДЕЙСТВИЯ = ["DataVersion", "Date", "DeletionMark", "Number", "Posted"]


def _выбор(одинс: Маршруты) -> list[str]:
    return sorted(одинс.get.calls.last.request.url.params["$select"].split(","))


async def test_Post_непроведённого_превью_нет_да_и_request_формы_P8(среда, одинс):
    запись, стор, _ = среда
    одинс.объект(документ(), по_выбору=True)

    текст = await действие(запись, "Post")

    нет_реальных_значений(текст)
    ответ = json.loads(текст)
    assert ответ["op"] == "action" and ответ["entity"] == РЕАЛИЗАЦИЯ
    assert ответ["base"] == "ut" and ответ["role"] == "prod"
    assert ответ["key"] == ССЫЛКА_ДОК
    объект = {"Number": "УТ-000711", "Date": "2026-08-26T12:00:00"}
    assert ответ["object"] == объект
    assert ответ["preview"] == {
        "object": объект,
        "action": "Post",
        "posted": {"before": False, "after": True},
    }
    assert ответ["summary"] == "проведён: нет → да"
    assert ответ["warnings"] == []
    операция = await стор.take(ответ["pending_id"], "sess-1")
    # P8: `POST Document_X(guid'…')/Post` без скобок, без параметров и без тела; ответ 1С — 200
    # с пустым телом, итог — перечитыванием `Posted` (задача 7).
    assert операция.request == {
        "method": "POST",
        "path": f"{РЕАЛИЗАЦИЯ}(guid'{ССЫЛКА_ДОК}')/Post",
        "json": None,
    }
    assert операция.data_version == ВЕРСИЯ
    assert операция.key == {"Ref_Key": ССЫЛКА_ДОК}
    assert (операция.op, операция.entity) == ("action", РЕАЛИЗАЦИЯ)
    assert операция.preview["action"] == "Post" and операция.preview["key"] == ССЫЛКА_ДОК
    # Ровно один GET, и в выборе явно — поля, по которым предвидятся отказы 1С (M-2).
    assert одинс.get.call_count == 1 and not одинс.писали
    assert _выбор(одинс) == ВЫБОР_ДЕЙСТВИЯ


async def test_Post_проведённого_превью_да_да_и_предупреждение(среда, одинс):
    """P8: повторное проведение — не ошибка, а перепроведение (`DataVersion` растёт). Отказывать
    не за что, но пользователь должен видеть, что документ уже проведён."""
    запись, стор, _ = среда
    одинс.объект(документ(Posted=True), по_выбору=True)

    ответ = json.loads(await действие(запись, "Post"))

    assert ответ["preview"]["posted"] == {"before": True, "after": True}
    assert ответ["summary"] == "проведён: да → да"
    assert any("повторное проведение" in п for п in ответ["warnings"])
    assert len(стор._ops) == 1


async def test_Post_помеченного_на_удаление_отказ_при_подготовке(среда, одинс):
    """Решение 12 плана, P8: проведение помеченного — 500 от 1С. Отказ здесь, с подсказкой, а не
    после подтверждения пользователя."""
    запись, стор, _ = среда
    одинс.объект(документ(DeletionMark=True), по_выбору=True)

    отказ = ошибка(await действие(запись, "Post"))

    assert отказ["code"] == "params_invalid" and "помечен" in отказ["message"]
    assert "odata1c_mark_for_deletion" in отказ["hint"] and "mark=false" in отказ["hint"]
    assert стор._ops == {}
    assert not одинс.писали
    assert _выбор(одинс) == ВЫБОР_ДЕЙСТВИЯ


async def test_Unpost_проведённого_превью_да_нет(среда, одинс):
    запись, стор, _ = среда
    одинс.объект(документ(Posted=True), по_выбору=True)

    ответ = json.loads(await действие(запись, "Unpost"))

    assert ответ["preview"]["posted"] == {"before": True, "after": False}
    assert ответ["summary"] == "проведён: да → нет"
    операция = await стор.take(ответ["pending_id"], "sess-1")
    assert операция.request["path"] == f"{РЕАЛИЗАЦИЯ}(guid'{ССЫЛКА_ДОК}')/Unpost"


async def test_Unpost_непроведённого_отказ_params_invalid(среда, одинс):
    """Отмена проведения непроведённого документа ничего не меняет (1С отвечает 200, P8), но
    `DataVersion` растёт — запись без смысла, которую пользователь подтвердил бы зря."""
    запись, стор, _ = среда
    одинс.объект(документ(), по_выбору=True)

    отказ = ошибка(await действие(запись, "Unpost"))

    assert отказ["code"] == "params_invalid" and "не проведён" in отказ["message"]
    assert стор._ops == {}
    assert _выбор(одинс) == ВЫБОР_ДЕЙСТВИЯ


async def test_Post_на_справочнике_action_unknown(среда, одинс):
    запись, стор, _ = среда

    отказ = ошибка(await действие(запись, "Post", entity=КОНТРАГЕНТЫ, key=ССЫЛКА))

    assert отказ["code"] == "action_unknown"
    assert "действий" in отказ["hint"]
    assert not одинс.обращались and стор._ops == {}


async def test_действие_не_из_индекса_action_unknown_с_перечнем_без_эха(среда, одинс):
    """Отказ перечисляет объявленные действия сущности из индекса (метаданные), но не повторяет
    имя, которое прислала модель (Ruling 51, 53): метка — цифры известного телефона в имени."""
    запись, стор, tools = среда
    т = токен(tools, ТЕЛЕФОН, entity=БАНК, поле="ТелефоныБанка")

    отказ = ошибка(await действие(запись, f"Post{ЦИФРЫ_ТЕЛЕФОНА}"))

    assert отказ["code"] == "action_unknown"
    assert "Post" in отказ["hint"] and "Unpost" in отказ["hint"]
    _без_метки(отказ, т)
    assert not одинс.обращались and стор._ops == {}


@pytest.mark.parametrize(("сущность", "имя"), [(ПРОЦЕСС, "Start"), (ЗАДАЧА, "ExecuteTask")])
async def test_объявленное_действие_вне_первой_поставки_action_unknown(среда, одинс, сущность, имя):
    запись, стор, _ = среда

    отказ = ошибка(await действие(запись, имя, entity=сущность, key=ССЫЛКА))

    assert отказ["code"] == "action_unknown"
    assert "Post" in отказ["hint"] and "Unpost" in отказ["hint"]
    assert имя in отказ["hint"]  # объявлено у сущности — имя из индекса, не эхо
    assert not одинс.обращались and стор._ops == {}


ОТКАЗЫ_ACTION = [
    pytest.param("ro", РЕАЛИЗАЦИЯ, ССЫЛКА_ДОК, "Post", None, "base_read_only", id="write-false"),
    # Отказ базы не зависит от имени действия: и неизвестное имя получает `base_read_only`.
    pytest.param("ro", ПРОЦЕСС, ССЫЛКА, "Start", None, "base_read_only", id="write-false-Start"),
    pytest.param(
        "nopost", РЕАЛИЗАЦИЯ, ССЫЛКА_ДОК, "Post", None, "permission_denied", id="post_documents"
    ),
    pytest.param(
        "nopost", РЕАЛИЗАЦИЯ, ССЫЛКА_ДОК, "Unpost", None, "permission_denied", id="post_documents-U"
    ),
    pytest.param(
        "ut",
        РЕАЛИЗАЦИЯ,
        ССЫЛКА_ДОК,
        "Post",
        {"PostingModeOperational": True},
        "params_invalid",
        id="параметры",
    ),
    pytest.param("ut", РЕАЛИЗАЦИЯ, ССЫЛКА_ДОК, 5, None, "action_unknown", id="имя-не-строка"),
    pytest.param("ut", РЕАЛИЗАЦИЯ, ССЫЛКА_ДОК, "post", None, "action_unknown", id="регистр-имени"),
    pytest.param("ut", РЕАЛИЗАЦИЯ, "не-ключ", "Post", None, "params_invalid", id="ключ-не-GUID"),
    pytest.param(
        "ut",
        "InformationRegister_КурсыВалют_SliceLast",
        ССЫЛКА,
        "SliceLast",
        None,
        "permission_denied",
        id="виртуальная-таблица",
    ),
]


@pytest.mark.parametrize(("база", "сущность", "ключ", "имя", "параметры", "код"), ОТКАЗЫ_ACTION)
async def test_action_отказывает_до_обращения_к_1С(
    среда, одинс, база, сущность, ключ, имя, параметры, код
):
    """Разрешения SPEC §7.1 и проверки действия — до GET текущего состояния: неверный вызов не
    трогает базу даже чтением."""
    запись, стор, _ = среда

    текст = await действие(запись, имя, base=база, entity=сущность, key=ключ, params=параметры)

    assert ошибка(текст)["code"] == код, текст
    assert not одинс.обращались
    assert стор._ops == {}


async def test_параметры_действия_отказ_называет_объявленные_из_индекса(среда, одинс):
    """P8: `Post`/`Unpost` работают без параметров, а форма с параметром сквозь гейт и `commit`
    не проверена — в первой поставке параметры не принимаются. Подсказка называет объявленные
    параметры из индекса; имя, которое прислала модель, не повторяется."""
    запись, _, tools = среда
    т = токен(tools, ТЕЛЕФОН, entity=БАНК, поле="ТелефоныБанка")

    отказ = ошибка(await действие(запись, "Post", params={ИМЯ_С_МЕТКОЙ: True}))

    assert отказ["code"] == "params_invalid"
    assert "PostingModeOperational" in отказ["hint"]
    _без_метки(отказ, т)
    assert not одинс.обращались


async def test_скрытый_документ_action_отказ_до_обращения_к_1С(среда, одинс, дом):
    запись, стор, _ = среда
    _скрыть(дом, РЕАЛИЗАЦИЯ)

    отказ = ошибка(await действие(запись, "Post"))

    assert отказ["code"] == "entity_hidden" and not отказ["hint"]
    assert not одинс.обращались and стор._ops == {}


async def test_action_объект_не_найден(среда, одинс):
    запись, стор, _ = среда
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

    отказ = ошибка(await действие(запись, "Post"))

    assert отказ["code"] == "object_not_found"
    assert стор._ops == {} and not одинс.писали


def test_проведение_и_флаг_post_documents_один_перечень():
    """Перечень действий первой поставки и действия под флагом `post_documents` — одна
    константа: разойдись они, действие прошло бы `action_unknown` мимо флага."""
    assert ДЕЙСТВИЯ_ПРОВЕДЕНИЯ == ("Post", "Unpost")
    # Тождество, а не равенство: перечень, заново вписанный в сервис, остался бы равным.
    assert service.ДЕЙСТВИЯ_ПРОВЕДЕНИЯ is permissions.ДЕЙСТВИЯ_ПРОВЕДЕНИЯ
