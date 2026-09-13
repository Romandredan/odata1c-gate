"""Подготовка `create` и `action` (план M2, задача 6) рядом с `update`/`mark_for_deletion`
задачи 5.

`create` не обращается к 1С вовсе: текущего состояния у нового объекта нет, превью — тело как его
прислала модель (Ruling 45), открытый литерал не отклоняется (Ruling 47). `action` читает текущее
состояние одним GET и предвидит отказы 1С (решение 12 плана); форма запроса — факт пробы P8.

Поддельная 1С — `respx`, гейт — `identifiers+names` (роль `prod` с явным `write: true`: у `dev`
гейт выключен, и токенов не было бы вовсе). «Ни одного обращения к 1С» проверяется по конкретным
маршрутам, а не общим счётчиком роутера: перехват базового адреса держит завершение сеанса 1С.
"""

import json

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
    return home


@pytest.fixture
async def среда(дом, tmp_path):
    tools = ToolService(load_config(дом))
    стор = PendingStore(600, clock=Часы(1000.0))
    журнал = Journal(tmp_path / "journal.sqlite")
    запись = WriteService(tools, стор, журнал, CommitLimiter(), clock=Часы(1_757_000_000.0))
    yield запись, стор, tools
    журнал.close()
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
    assert ответ["preview"] == [
        {"field": "Description", "value": "ООО Северный Ветер"},
        {"field": "ИНН", "value": новый},
    ]
    # Ruling 44: объект называется представлением из данных модели как есть — ключа ещё нет.
    assert ответ["object"] == {"Description": "ООО Северный Ветер"}
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
    реальным значением, в превью — как прислала модель (Ruling 45: маски своей сущности у
    строки превью нет, она нужна только проверке полей строки по индексу)."""
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
    assert ответ["preview"][1] == {"field": "КонтактнаяИнформация", "value": строки}
    операция = await стор.take(ответ["pending_id"], "sess-1")
    [строка] = операция.request["json"]["КонтактнаяИнформация"]
    assert строка == {"LineNumber": 1, "Тип": "Телефон", "Представление": ТЕЛЕФОН}
    assert not одинс.обращались


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
    assert операция.request["json"]["КонтактнаяИнформация"] == строки
    assert операция.request["json"]["ИНН"] == ЕЩЁ_ИНН
    assert контрагент_["preview"][1] == {"field": "ИНН", "value": ЕЩЁ_ИНН}
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


async def test_создание_записи_независимого_регистра_полями_ключа(среда, одинс):
    """У записи регистра нет аргумента `key`: измерения и период — её данные. Ключевые поля,
    которые `update` не пишет, в `create` разрешены; `Ref_Key` запрещён везде — его выдаёт 1С.
    Объект называется полями ключа, как у `update` (Ruling 44), — из данных модели."""
    запись, стор, _ = среда
    данные = {"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА, "Курс": 91.25, "Кратность": 1}

    ответ = json.loads(await создать(запись, данные, entity=КУРСЫ))

    assert ответ["object"] == {"Period": "2026-01-01T00:00:00", "Валюта_Key": ССЫЛКА}
    операция = await стор.take(ответ["pending_id"], "sess-1")
    assert операция.request == {"method": "POST", "path": КУРСЫ, "json": данные}
    assert not одинс.обращались


async def test_литерал_создания_известный_словарю_страж_заменяет_токеном(среда, одинс):
    """Ruling 45, пункт 4 — принятое поведение: литерал модели, совпавший со значением общего
    словаря, страж в `gate.finish` заменяет токеном (`guard_replaced`), как `eq` с открытым
    литералом на чтении. В 1С уходит то, что прислала модель."""
    запись, стор, tools = среда
    ток = токен(tools, ИНН)

    ответ = json.loads(await создать(запись, {"Description": "ООО Северный Ветер", "ИНН": ИНН}))

    assert ответ["preview"][1] == {"field": "ИНН", "value": ток}
    assert any(п.startswith("guard_replaced") for п in ответ["warnings"])
    операция = await стор.take(ответ["pending_id"], "sess-1")
    assert операция.request["json"]["ИНН"] == ИНН


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
    assert "reindex" in вне_индекса["hint"]
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

    assert шапка["code"] == строка["code"] == "params_invalid"
    assert КОНТРАГЕНТЫ in шапка["message"] and КИ in строка["message"]
    _без_метки(шапка, т)
    _без_метки(строка, т)
    assert not одинс.обращались


def test_body_preview_строка_на_каждое_поле_в_порядке_тела():
    строки = [{"Тип": "Телефон", "Представление": "[[phone:A]]"}]

    assert body_preview({"ИНН": "[[inn:B]]", "Комментарий": "x", "КИ": строки}) == [
        {"field": "ИНН", "value": "[[inn:B]]"},
        {"field": "Комментарий", "value": "x"},
        {"field": "КИ", "value": строки},
    ]


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
