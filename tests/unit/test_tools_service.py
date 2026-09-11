"""Сервис тулов чтения (план M1d, задача 4): `ToolService` собирает гейт, построение запроса,
клиент 1С и формирование ответа в готовые ответы MCP-тулов `bases`, `find_entity`,
`describe_entity`, `query`, `get`. Тестируется без сети (`respx`) и без MCP-транспорта.
"""

import json
import sys
import urllib.parse

import httpx
import pytest
import respx
import yaml
from conftest import без_навигаций, эхо_отбора

from odata1c.cli import main
from odata1c.gate.service import policy_path, refresh_policy
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import PARSER_VERSION, index_path
from odata1c.index.repository import IndexRepository
from odata1c.registry.registry import SessionScope
from odata1c.tools.service import ToolService

URL_UT = "http://localhost/ut/odata/standard.odata/"
URL_DEV = "http://localhost/dev/odata/standard.odata/"

BASES_YAML = f"""
default: ut
bases:
  ut:
    label: УТ 11, тестовая
    url: {URL_UT}
    user: u
    password: p
    role: prod
  dev:
    label: Песочница
    url: {URL_DEV}
    user: u
    password: p
    role: dev
"""

ИНН = "7707083893"
ОКПО = "09226071"
ССЫЛКА = "a103cb54-42ee-11ec-a7a0-f10ab59a067e"


def _дом(tmp_path, edmx_ut_real):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES_YAML, encoding="utf-8")

    from odata1c.config.loader import load_config

    config = load_config(home)
    хранилище = IndexRepository(index_path(home, "ut"))
    хранилище.write(parse_edmx(edmx_ut_real))
    хранилище.close()
    refresh_policy(home, config.bases["ut"])
    return home


@pytest.fixture
def дом(tmp_path, edmx_ut_real):
    return _дом(tmp_path, edmx_ut_real)


@pytest.fixture
async def сервис(дом):
    from odata1c.config.loader import load_config

    config = load_config(дом)
    служба = ToolService(config)
    yield служба
    await служба.aclose()


@pytest.fixture
def respx_ut():
    with respx.mock(base_url=URL_UT, assert_all_called=False) as router:
        # Завершение сеанса 1С (Client1C.close, ib_session=True по умолчанию) идёт на базовый
        # адрес без хвоста пути — тот же приём, что в test_cli_reindex.py/test_client1c.py.
        # Полный абсолютный URL (не пустая строка) — respx строит из "" маршрут «startswith
        # базового адреса», который перехватывает вообще все запросы этого теста, если добавлен
        # раньше остальных маршрутов (проверено пробой respx, раунд правок 1).
        router.get(URL_UT).mock(return_value=httpx.Response(200, json={"value": []}))
        yield router


async def токен_инн(сервис: ToolService, инн: str, *, entity: str = "Catalog_Контрагенты") -> str:
    """Токен, который модель увидела бы в ответе, — через маскировку ответа тем же гейтом, что
    использует `query`/`get` (`BaseGate.mask`), без обращения к 1С: побочный эффект тот же, что и
    у реального ответа — значение регистрируется в словаре и его понимает `inbound_*`/страж."""
    гейт = сервис._gate_for(сервис._config.bases["ut"])
    результат = гейт.mask({"ИНН": инн}, entity=entity, resolve=без_навигаций)
    return результат.data["ИНН"]


# ---------------------------------------------------------------------------------------------
# query: конверт, маскировка, обратная подмена в filter, ошибка 1С, запрет сортировки
# ---------------------------------------------------------------------------------------------


async def test_query_маскирует_и_отдаёт_конверт(сервис, respx_ut):
    respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(
            200,
            json={
                "odata.metadata": "…",
                "odata.count": "2",
                "value": [
                    {
                        "Ref_Key": "a103cb54-42ee-11ec-a7a0-f10ab59a067e",
                        "Description": "ООО Ромашка",
                        "ИНН": ИНН,
                        "Контрагент@navigationLinkUrl": "x",
                    }
                ],
            },
        )
    )
    текст = await сервис.query(
        SessionScope(),
        base="ut",
        entity="Catalog_Контрагенты",
        select=["Ref_Key", "Description", "ИНН"],
        inlinecount=True,
    )
    данные = json.loads(текст)

    assert ИНН not in текст and "Ромашка" not in текст
    assert данные["total"] == 2 and данные["base"] == "ut" and данные["role"] == "prod"
    assert данные["items"][0]["Ref_Key"] == "a103cb54-42ee-11ec-a7a0-f10ab59a067e"
    assert "ИНН" in данные["masked_fields"]
    assert "@navigationLinkUrl" not in текст


async def test_фильтр_с_токеном_уходит_в_1С_реальным_значением(сервис, respx_ut):
    токен = await токен_инн(сервис, ИНН)
    маршрут = respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(200, json={"value": []})
    )

    await сервис.query(
        SessionScope(),
        base="ut",
        entity="Catalog_Контрагенты",
        filter=f"ИНН eq '{токен}'",
    )

    assert ИНН in маршрут.calls.last.request.url.params["$filter"]


async def test_ошибка_1С_проходит_гейт(сервис, respx_ut):
    await токен_инн(сервис, ИНН)
    respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(
            400,
            json={
                "odata.error": {
                    "code": "6",
                    "message": {"lang": "ru", "value": f"Сегмент пути {ИНН} не найден!"},
                }
            },
        )
    )

    текст = await сервис.query(SessionScope(), base="ut", entity="Catalog_Контрагенты")

    assert ИНН not in текст
    assert json.loads(текст)["error"]["code"] == "odata_error"


# ---------------------------------------------------------------------------------------------
# Страж видит то, что гейт раскрыл в этом же вызове (задача N1 M1d, инвариант 1)
# ---------------------------------------------------------------------------------------------

# Класс гейта → поле, в котором он законно раскрывается, и реальное значение. Перебор по всем
# классам, а не по одному ИНН: у `addr`, `dob` и свободнотекстового `doc` нет ни детектора, ни
# цифровой серии, ни автомата названий — собрать значение обратно может только набор раскрытого.
ЭХО_КЛАССОВ: dict[str, tuple[str, str]] = {
    "addr": ("АдресРегистрации", "г. Москва, ул. Тверская, д. 7, кв. 43"),
    "dob": ("ДатаРождения", "1980-05-01"),
    "doc": ("КемВыдан", "ОУФМС России по гор. Москве"),
    "acc": ("НомерСчета", "40702810900000012345"),
    "phone": ("Телефон", "+7 916 123-45-67"),
    "inn": ("ИНН", ИНН),
}
ЛИЦА = "Catalog_ФизическиеЛица"


def _токен_поля(служба: ToolService, класс: str, поле: str, значение: str) -> str:
    """Токен, который модель получила бы в ответе от поля этого класса: выдаёт словарь того же
    гейта, поэтому обратная подмена и страж знают о нём ровно то же, что и о настоящем."""
    гейт = служба._gate_for(служба._config.bases["ut"])
    return гейт._dictionary.token_for(класс, значение, base="ut", entity=ЛИЦА, field=поле)


@pytest.mark.parametrize("класс", sorted(ЭХО_КЛАССОВ))
async def test_эхо_отбора_в_ошибке_1С_не_выносит_раскрытое(сервис, respx_ut, класс):
    поле, значение = ЭХО_КЛАССОВ[класс]
    токен = _токен_поля(сервис, класс, поле, значение)
    маршрут = respx_ut.get(ЛИЦА).mock(side_effect=эхо_отбора)

    текст = await сервис.query(
        SessionScope(), base="ut", entity=ЛИЦА, filter=f"{поле} eq '{токен}'"
    )

    # Сначала — что проверка вообще имеет предмет: раскрытое значение действительно ушло в 1С.
    # Без этого утверждения параметризация декоративна (токен неизвестного словарю класса дал бы
    # отказ, запрос бы не состоялся, и ответ оказался бы «чистым» сам собой).
    assert значение in маршрут.calls.last.request.url.params["$filter"]
    assert значение not in текст
    assert токен in текст
    assert json.loads(текст)["error"]["code"] == "odata_error"


async def test_раскрытое_ловится_в_данных_и_не_живёт_до_следующего_вызова(сервис, respx_ut):
    """Требования 1 и 2: значение ловится где угодно в ответе, но набор живёт ровно один вызов.

    Второй вызов — тот же адрес в поле без класса (`Комментарий`, детекторы адреса не знают) и
    без всякого раскрытия: он обязан вернуться как есть. Иначе «набор» оказался бы состоянием,
    переживающим вызов, — то есть пересечением сессий у общего на базу `BaseGate`.
    """
    поле, значение = ЭХО_КЛАССОВ["addr"]
    токен = _токен_поля(сервис, "addr", поле, значение)
    маршрут = respx_ut.get(ЛИЦА).mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"Ref_Key": ССЫЛКА, "Комментарий": f"проживает: {значение}"}]},
        )
    )

    первый = await сервис.query(
        SessionScope(), base="ut", entity=ЛИЦА, filter=f"{поле} eq '{токен}'"
    )
    assert значение in маршрут.calls.last.request.url.params["$filter"]
    assert значение not in первый and токен in первый

    второй = await сервис.query(SessionScope(), base="ut", entity=ЛИЦА)
    assert значение in второй


# ---------------------------------------------------------------------------------------------
# Формы возврата раскрытого (раунд правок 1, Ruling 25)
# ---------------------------------------------------------------------------------------------
#
# Урок ревью 2026-09-11: покрытие по классам было, покрытия по ФОРМАМ не было. Класс определяет,
# есть ли у значения второй рубеж; форма определяет, доживёт ли точное вхождение до стража.
# Все пять находок ревью — это формы, а не классы, и бьют они по одному и тому же `addr`.

АДРЕС = "г. Москва, ул. Тверская, д. 7, кв. 43"
# Хвост адреса — то, чего не должно быть в ответе ни в одной форме. Берётся хвост, а не адрес
# целиком: у форм с усечением целого адреса в ответе не будет и без всякой защиты.
ХВОСТ_АДРЕСА = "ул. Тверская, д. 7, кв. 43"
ДЛИННЫЙ_АДРЕС = АДРЕС + "; " + ("а также корпус 2, строение 3; " * 80)


def _эхо_в_odata_error(значение: str):
    """Штатная ошибка платформы, повторяющая выражение отбора вместе с литералом."""

    def ответить(request: httpx.Request) -> httpx.Response:
        тело = {
            "odata.error": {
                "code": "6",
                "message": {
                    "lang": "ru",
                    "value": f"Ошибка при разборе выражения отбора: Поле eq '{значение}'",
                },
            }
        }
        return httpx.Response(400, json=тело)

    return ответить


def _страница_веб_сервера(значение: str):
    """Страница ошибки IIS/Apache/nginx, повторяющая адрес запроса. Значение в нём закодировано
    процентами — закодировал его сам шлюз (`client1c._собрать_запрос`)."""

    def ответить(request: httpx.Request) -> httpx.Response:
        страница = (
            "<html><head><title>404 Not Found</title></head><body><h1>Not Found</h1>"
            f"<p>The requested URL {request.url.path}?{request.url.query.decode()} "
            "was not found on this server.</p></body></html>"
        )
        return httpx.Response(404, text=страница)

    return ответить


def _неразбираемое_тело_с_границей(значение: str):
    """Тело, которое не разбирается как `odata.error`: `map_error` берёт `body.strip()[:500]`.
    Подложка подобрана так, чтобы граница прошла посередине значения."""

    подложка = "E" * (500 - len(значение) // 2)

    def ответить(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=подложка + значение + " конец")

    return ответить


def _данными_в_поле_без_класса(значение: str):
    """Значение вернулось данными — в поле, которое шлюз не защищает (детекторы адреса не
    знают). Длинное значение на этом пути режет `truncate_strings` до стража."""

    def ответить(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [{"Ref_Key": ССЫЛКА, "Комментарий": значение}]})

    return ответить


# (значение, чего не должно быть в ответе, ответчик поддельной 1С)
ФОРМЫ_ВОЗВРАТА = {
    "прямое-эхо": (АДРЕС, ХВОСТ_АДРЕСА, _эхо_в_odata_error),
    "процентное-кодирование": (АДРЕС, ХВОСТ_АДРЕСА, _страница_веб_сервера),
    "вложенный-телефон": (
        f"{АДРЕС}, тел. +7 916 123-45-67",
        ХВОСТ_АДРЕСА,
        _эхо_в_odata_error,
    ),
    "вложенный-guid": (
        f'<АдресРФ><Регион ИД="{ССЫЛКА}"/>{АДРЕС}</АдресРФ>',
        ХВОСТ_АДРЕСА,
        _эхо_в_odata_error,
    ),
    "длиннее-лимита-усечения": (ДЛИННЫЙ_АДРЕС, ДЛИННЫЙ_АДРЕС[:80], _данными_в_поле_без_класса),
    "граница-усечения-в-неразбираемом-теле": (АДРЕС, АДРЕС[:18], _неразбираемое_тело_с_границей),
}


@pytest.mark.parametrize("форма", sorted(ФОРМЫ_ВОЗВРАТА))
async def test_раскрытое_не_возвращается_ни_в_одной_форме(сервис, respx_ut, форма):
    """Ruling 25: слой раскрытого работает по сырому тексту, каким тот пришёл от 1С, — до
    `mask_text`, `truncate_strings`, `map_error` и заглушки GUID. Иначе точное вхождение, на
    котором держится весь слой, рушится, а с ним и защита класса `addr`."""
    значение, запрещено, ответчик = ФОРМЫ_ВОЗВРАТА[форма]
    поле = "АдресРегистрации"
    токен = _токен_поля(сервис, "addr", поле, значение)
    маршрут = respx_ut.get(ЛИЦА).mock(side_effect=ответчик(значение))

    текст = await сервис.query(
        SessionScope(),
        base="ut",
        entity=ЛИЦА,
        filter=f"{поле} eq '{токен}'",
        select=["Ref_Key", "Комментарий"],
    )

    # Проверка имеет предмет: раскрытое действительно ушло в 1С.
    ушло = urllib.parse.unquote(str(маршрут.calls.last.request.url))
    assert значение in ушло, "раскрытия не было — форма ничего не проверяет"
    # Ответ читается так, как его прочитает получатель: процентную запись достаточно раскодировать.
    assert запрещено not in urllib.parse.unquote(текст)
    # Замену сделал именно набор: `guard_replaced` ставит страж, маскировщик такого
    # предупреждения не выдаёт. Сам токен в ответе проверять нельзя — на формах с
    # усечением его обрезает тот же лимит, что раньше обрезал значение.
    assert "guard_replaced" in текст


async def test_формы_возврата_покрывают_кодирование_клиента(сервис):
    """Анти-дрейф: процентная форма в наборе построена по тем же правилам, что использует сам
    клиент 1С. Если `client1c` сменит `safe`-набор, форма в наборе разойдётся с отправленной —
    проверяется исполнением, а не сверкой констант."""
    from odata1c.client1c.client import _собрать_запрос, _экранировать_путь
    from odata1c.gate.revealed import RevealedValues

    набор = RevealedValues()
    набор.add(АДРЕС, token="[[addr:A60KNQH867]]")
    в_запросе = _собрать_запрос({"$filter": f"АдресРегистрации eq '{АДРЕС}'"})
    в_пути = _экранировать_путь(f"Catalog(РазделУчета='{АДРЕС}')")

    for текст in (в_запросе, в_пути):
        закрыто = сервис._gate_for(сервис._config.bases["ut"]).scrub_revealed(текст, набор)
        assert АДРЕС not in urllib.parse.unquote(закрыто), текст[:80]


async def test_get_не_выносит_раскрытое_в_эхе_ключа(сервис, дом, respx_ut):
    """Сторож для `get` — единственного входа обратной подмены, у которого его не было.

    Поле составного ключа объявлено защищаемым разделом `fields` политики — штатный сценарий
    продукта (владелец сам решает, что защищать), а не искусственный приём. Значение класса `doc`
    без единой цифры: ни детектора, ни цифровой серии, ни автомата названий — заменить его может
    только набор вызова. Тест написан ревьюером (раунд правок 1) и приведён к стилю проекта.
    """
    сущность = "InformationRegister_СтоимостьТоваров_RecordType"
    поле, значение = "РазделУчета", "Раздел учёта: Тверская, Арбат, Хамовники"
    путь = дом / "bases" / "ut" / "policy.yaml"
    политика = yaml.safe_load(путь.read_text(encoding="utf-8"))
    политика["fields"] = {f"{сущность}.{поле}": "doc"}
    путь.write_text(yaml.safe_dump(политика, allow_unicode=True), encoding="utf-8")

    гейт = сервис._gate_for(сервис._config.bases["ut"])
    assert гейт.field_class(сущность, поле) == "doc", "поле не получило объявленный класс"
    токен = гейт.mask({поле: значение}, entity=сущность, resolve=без_навигаций).data[поле]
    assert токен.startswith("[["), "значение не замаскировано — сторож ничего не докажет"

    # Путь `get` — сущность со скобками и ключом внутри, да ещё в процентной записи: маршрут
    # по имени сущности его не ловит, поэтому перехватывается любой GET.
    respx_ut.route(method="GET").mock(
        side_effect=lambda request: httpx.Response(
            400,
            json={
                "odata.error": {
                    "code": "",
                    "message": {"lang": "ru", "value": f"Ошибка ключа: {поле} = '{значение}'"},
                }
            },
        )
    )

    текст = await сервис.get(
        SessionScope(),
        base="ut",
        entity=сущность,
        key={
            "Period": "2026-01-01T00:00:00",
            "АналитикаУчетаНоменклатуры_Key": ССЫЛКА,
            "ВидЗапасов_Key": ССЫЛКА,
            "Организация_Key": ССЫЛКА,
            поле: токен,
        },
    )

    ушло = urllib.parse.unquote(str(respx_ut.calls.last.request.url))
    assert значение in ушло, "раскрытия не было — сторож ничего не проверяет"
    assert значение not in urllib.parse.unquote(текст)
    assert токен in текст


async def test_raw_get_не_выносит_раскрытое_в_эхе_отбора(сервис, respx_ut):
    """`raw_get` разворачивает токен в `$filter` своим путём (`_подготовить_параметры`) — и
    отдаёт раскрытое тому же стражу."""
    поле, значение = ЭХО_КЛАССОВ["addr"]
    токен = _токен_поля(сервис, "addr", поле, значение)
    маршрут = respx_ut.get(ЛИЦА).mock(side_effect=эхо_отбора)

    текст = await сервис.raw_get(
        SessionScope(), base="ut", path=ЛИЦА, query={"$filter": f"{поле} eq '{токен}'"}
    )

    assert значение in маршрут.calls.last.request.url.params["$filter"]
    assert значение not in текст and токен in текст


async def test_404_на_известную_сущность_подсказывает_reindex(сервис, respx_ut):
    respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(
            404, json={"odata.error": {"code": "1", "message": {"value": "не найдено"}}}
        )
    )

    текст = await сервис.query(SessionScope(), base="ut", entity="Catalog_Контрагенты")
    ошибка = json.loads(текст)["error"]

    assert ошибка["code"] == "entity_unknown"
    assert "odata1c_reindex" in ошибка["hint"]


async def test_сортировка_по_защищаемому_полю_запрещена(сервис, respx_ut):
    # Маршрут зарегистрирован, но не должен быть вызван вообще: запрет обязан сработать ДО
    # обращения к 1С (иначе тест не отличит «проверка есть» от случайного кода ошибки сети —
    # именно так и произошло на мутации, где проверку вырезали, раунд правок 2).
    маршрут = respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    текст = await сервис.query(
        SessionScope(), base="ut", entity="Catalog_Контрагенты", orderby="ИНН"
    )
    assert json.loads(текст)["error"]["code"] == "params_invalid"
    assert not маршрут.called


async def test_сортировка_по_защищаемому_полю_через_навигацию_запрещена(сервис, respx_ut):
    # Document_РеализацияТоваровУслуг → навигация Контрагент → Catalog_Контрагенты.ИНН (auto:
    # inn) — путь через навигацию должен закрываться тем же запретом, что и прямое поле
    # (ревью, раунд 1, Minor): is_protected(entity, "Контрагент/ИНН") с сущностью верхнего
    # уровня сам по себе такой путь не резолвит и пропустил бы сортировку.
    маршрут = respx_ut.get("Document_РеализацияТоваровУслуг").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    текст = await сервис.query(
        SessionScope(),
        base="ut",
        entity="Document_РеализацияТоваровУслуг",
        orderby="Контрагент/ИНН",
    )
    assert json.loads(текст)["error"]["code"] == "params_invalid"
    assert not маршрут.called


async def test_сортировка_по_обычному_полю_через_навигацию_разрешена(сервис, respx_ut):
    # Контрагент/Description — org, но не inn/фис. класс, требующий строгой защиты на уровне
    # identifiers (роль prod здесь identifiers+names — Description защищён), поэтому берём
    # действительно незащищённое поле цели навигации: Ref_Key (идентификатор, инвариант 6).
    маршрут = respx_ut.get("Document_РеализацияТоваровУслуг").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    текст = await сервис.query(
        SessionScope(),
        base="ut",
        entity="Document_РеализацияТоваровУслуг",
        orderby="Контрагент/Ref_Key",
    )
    assert "error" not in json.loads(текст)
    assert маршрут.called


async def test_сортировка_по_неизвестной_навигации_запрещена_консервативно(сервис, respx_ut):
    маршрут = respx_ut.get("Document_РеализацияТоваровУслуг").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    текст = await сервис.query(
        SessionScope(),
        base="ut",
        entity="Document_РеализацияТоваровУслуг",
        orderby="НетТакойНавигации/Description",
    )
    assert json.loads(текст)["error"]["code"] == "params_invalid"
    assert not маршрут.called


async def test_сортировка_по_обычному_полю_разрешена(сервис, respx_ut):
    маршрут = respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    текст = await сервис.query(
        SessionScope(), base="ut", entity="Catalog_Контрагенты", orderby="DeletionMark"
    )
    assert "error" not in json.loads(текст)
    assert маршрут.called


async def test_виртуальная_таблица_с_таймаутом(сервис, respx_ut):
    маршрут = respx_ut.get(url__regex=r".*/Balance\(.*").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    await сервис.query(
        SessionScope(),
        base="ut",
        entity="AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance",
        params={"Period": "2026-09-01"},
    )
    assert маршрут.called


# ---------------------------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------------------------


async def test_get_маскирует_и_отдаёт_item(сервис, respx_ut):
    # Имя сущности в пути запроса кодируется процентами (кириллица) — регэксп сверяется по
    # неизменному ASCII-хвосту пути (guid-литерал ключа), а не по кириллическому имени.
    respx_ut.get(url__regex=r".*\(guid'a103cb54-42ee-11ec-a7a0-f10ab59a067e'\).*").mock(
        return_value=httpx.Response(
            200,
            json={
                "Ref_Key": "a103cb54-42ee-11ec-a7a0-f10ab59a067e",
                "ИНН": ИНН,
            },
        )
    )
    текст = await сервис.get(
        SessionScope(),
        base="ut",
        entity="Catalog_Контрагенты",
        key="a103cb54-42ee-11ec-a7a0-f10ab59a067e",
    )
    данные = json.loads(текст)

    assert ИНН not in текст
    assert "items" not in данные
    assert данные["item"]["Ref_Key"] == "a103cb54-42ee-11ec-a7a0-f10ab59a067e"


# ---------------------------------------------------------------------------------------------
# bases, find_entity, describe_entity — без сети
# ---------------------------------------------------------------------------------------------


async def test_неизвестная_база_и_видимость(сервис):
    текст = await сервис.query(SessionScope(bases=("dev",)), base="ut", entity="Catalog_Валюты")
    assert json.loads(текст)["error"]["code"] == "base_unknown"

    видимые = json.loads(await сервис.bases(SessionScope(bases=("dev",))))["bases"]
    assert [б["name"] for б in видимые] == ["dev"]


async def test_bases_не_пропускает_локальные_данные_через_страж(tmp_path, edmx_ut_real):
    """`bases()` не должен идти через `guard_only`: тот работает на строжайшем уровне
    (identifiers+names) независимо от режима гейта конкретной базы — «Песочница», чей label
    случайно содержит цифры уже известного словарю ИНН, была бы искажена под чужую политику,
    хотя `label` — не данные 1С, а то, что пользователь сам вписал в `bases.yaml` (ревью плана,
    раунд 2)."""
    home = _дом(tmp_path, edmx_ut_real)
    (home / "bases.yaml").write_text(
        BASES_YAML.replace("Песочница", f"Песочница {ИНН}"), encoding="utf-8"
    )
    from odata1c.config.loader import load_config

    служба = ToolService(load_config(home))
    try:
        await токен_инн(служба, ИНН)  # тот же словарь общий на все базы сервиса
        данные = json.loads(await служба.bases(SessionScope()))
        по_имени = {б["name"]: б for б in данные["bases"]}
        assert по_имени["dev"]["label"] == f"Песочница {ИНН}"
    finally:
        await служба.aclose()


async def test_bases_показывает_статус_индекса(сервис):
    данные = json.loads(await сервис.bases(SessionScope()))
    по_имени = {б["name"]: б for б in данные["bases"]}

    assert по_имени["ut"]["indexed"] is True
    assert по_имени["ut"]["entity_count"] and по_имени["ut"]["entity_count"] > 0
    assert по_имени["dev"]["indexed"] is False


async def test_bases_на_пустом_доме_даёт_подсказку(tmp_path):
    home = tmp_path / "пустой_дом"
    main(["init", "--home", str(home)])
    from odata1c.config.loader import load_config

    служба = ToolService(load_config(home))
    текст = await служба.bases(SessionScope())
    await служба.aclose()

    данные = json.loads(текст)
    assert данные["bases"] == []
    assert "bases.yaml" in данные["hint"]


async def test_неизвестная_сущность_с_кандидатами(сервис):
    текст = await сервис.query(SessionScope(), base="ut", entity="Catalog_Контрагент")
    ошибка = json.loads(текст)["error"]
    assert ошибка["code"] == "entity_unknown"
    assert "Catalog_Контрагенты" in ошибка["hint"]


async def test_виртуальная_таблица_без_такого_действия_подсказывает_братьев(сервис):
    # У AccumulationRegister_ДвиженияДенежныеСредстваКонтрагент в ut-real.edmx есть только
    # действие Turnovers — запрос несуществующего _Balance должен подсказать именно его, а не
    # общий нечёткий поиск по имени.
    текст = await сервис.query(
        SessionScope(),
        base="ut",
        entity="AccumulationRegister_ДвиженияДенежныеСредстваКонтрагент_Balance",
    )
    ошибка = json.loads(текст)["error"]
    assert ошибка["code"] == "entity_unknown"
    assert "Turnovers" in ошибка["hint"]


async def test_скрытая_сущность_не_видна_ни_напрямую_ни_в_кандидатах(дом, edmx_ut_real):
    """Раунд ревью плана (Important): скрытая сущность не должна ни отвечать своим кодом
    (entity_hidden выдаёт признак существования по-другому, чем entity_unknown), ни всплывать
    именем в подсказке-кандидате при опечатке в похожем запросе."""
    from odata1c.config.loader import load_config
    from odata1c.gate.service import policy_path

    путь_политики = policy_path(дом, "ut")
    путь_политики.write_text(
        путь_политики.read_text(encoding="utf-8") + "entities:\n  Catalog_Валюты: {hide: true}\n",
        encoding="utf-8",
    )

    служба = ToolService(load_config(дом))
    try:
        текст_скрытой = await служба.query(SessionScope(), base="ut", entity="Catalog_Валюты")
        ошибка_скрытой = json.loads(текст_скрытой)["error"]
        assert ошибка_скрытой["code"] == "entity_hidden"

        текст_опечатки = await служба.query(SessionScope(), base="ut", entity="Catalog_Валют")
        ошибка_опечатки = json.loads(текст_опечатки)["error"]
        assert ошибка_опечатки["code"] == "entity_unknown"
        assert "Catalog_Валюты" not in ошибка_опечатки["hint"]
    finally:
        await служба.aclose()


async def test_find_entity_исключает_скрытые(дом):
    from odata1c.config.loader import load_config
    from odata1c.gate.service import policy_path

    путь_политики = policy_path(дом, "ut")
    путь_политики.write_text(
        путь_политики.read_text(encoding="utf-8") + "entities:\n  Catalog_Валюты: {hide: true}\n",
        encoding="utf-8",
    )

    служба = ToolService(load_config(дом))
    try:
        текст = await служба.find_entity(SessionScope(), base="ut", query="Валюты")
        имена = [с["name"] for с in json.loads(текст)["entities"]]
        assert "Catalog_Валюты" not in имена
    finally:
        await служба.aclose()


async def test_find_entity_находит_контрагентов(сервис):
    текст = await сервис.find_entity(SessionScope(), base="ut", query="контрагенты")
    имена = [с["name"] for с in json.loads(текст)["entities"]]
    assert "Catalog_Контрагенты" in имена


async def test_describe_показывает_навигации_и_классы(сервис):
    текст = await сервис.describe_entity(SessionScope(), base="ut", entity="Catalog_Контрагенты")
    assert "ИНН" in текст and "inn" in текст and "ГоловнойКонтрагент" in текст


async def test_describe_класс_гейта_из_политики_а_не_из_индекса(дом):
    """Отличает `effective_field_class(policy, ...)` от прочитанного `fields.sensitivity`
    индекса: ручная политика понижает ИНН до keep — describe обязан показать именно keep, а не
    inn из авто-разметки и не пустое значение из непроставленной колонки индекса."""
    from odata1c.config.loader import load_config
    from odata1c.gate.service import policy_path

    путь_политики = policy_path(дом, "ut")
    путь_политики.write_text(
        путь_политики.read_text(encoding="utf-8") + "fields:\n  Catalog_Контрагенты.ИНН: keep\n",
        encoding="utf-8",
    )

    служба = ToolService(load_config(дом))
    try:
        текст = await служба.describe_entity(
            SessionScope(), base="ut", entity="Catalog_Контрагенты"
        )
        строка_инн = next(строка for строка in текст.splitlines() if "| ИНН " in строка)
        assert "keep" in строка_инн
        assert "inn" not in строка_инн
    finally:
        await служба.aclose()


async def test_describe_json_через_finish(сервис):
    текст = await сервис.describe_entity(
        SessionScope(), base="ut", entity="Catalog_Контрагенты", response_format="json"
    )
    данные = json.loads(текст)
    assert данные["name"] == "Catalog_Контрагенты"
    поле_инн = next(п for п in данные["fields"] if п["name"] == "ИНН")
    assert поле_инн["gate_class"] == "inn"


async def test_describe_сущность_не_проиндексирована(tmp_path):
    home = tmp_path / "без_индекса"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES_YAML, encoding="utf-8")
    from odata1c.config.loader import load_config

    служба = ToolService(load_config(home))
    try:
        текст = await служба.describe_entity(SessionScope(), base="ut", entity="Catalog_Валюты")
        ошибка = json.loads(текст)["error"]
        assert ошибка["code"] == "entity_unknown"
        assert "reindex" in ошибка["hint"]
    finally:
        await служба.aclose()


# ---------------------------------------------------------------------------------------------
# internal — не отдаёт текст исключения
# ---------------------------------------------------------------------------------------------


async def test_внутренняя_ошибка_не_отдаёт_текст_исключения(сервис, monkeypatch):
    def взрыв(*a, **k):
        raise RuntimeError(f"секрет {ИНН}")

    monkeypatch.setattr("odata1c.tools.service.build_query", взрыв)

    текст = await сервис.query(SessionScope(), base="ut", entity="Catalog_Валюты")

    assert ИНН not in текст
    assert json.loads(текст)["error"]["code"] == "internal"


# ---------------------------------------------------------------------------------------------
# Дополнение оркестратора (M1b-fix задача 3): индекс прежней версии разбора — index_corrupt
# ---------------------------------------------------------------------------------------------


async def test_индекс_прежней_версии_разбора_даёт_index_corrupt(дом):
    from odata1c.config.loader import load_config
    from odata1c.index.schema import connect

    соединение = connect(index_path(дом, "ut"))
    with соединение:
        соединение.execute(
            "UPDATE meta SET value = ? WHERE key = 'parser_version'", (PARSER_VERSION + "-старая",)
        )
    соединение.close()

    служба = ToolService(load_config(дом))
    try:
        текст = await служба.query(SessionScope(), base="ut", entity="Catalog_Валюты")
        ошибка = json.loads(текст)["error"]
        assert ошибка["code"] == "index_corrupt"
    finally:
        await служба.aclose()


# ---------------------------------------------------------------------------------------------
# Раунд правок 1: битый policy.yaml не должен ронять тул голым исключением (ревью, Critical).
# `_gate_for` конструирует BaseGate лениво при первом обращении, а конструктор сам вызывает
# refresh() → load_policy() — на СУЩЕСТВУЮЩЕМ, но синтаксически битом policy.yaml это PolicyError
# ДО входа в try/except внутри _run (тот раньше перехватывал PolicyError только у refresh() ПОСЛЕ
# успешного построения гейта). Гейт при неудаче конструктора не кэшируется — повторный вызов
# обязан упасть так же штатно, а не по-другому.
# ---------------------------------------------------------------------------------------------


def _сломать_политику(дом):
    from odata1c.gate.service import policy_path

    путь = policy_path(дом, "ut")
    путь.write_text("fields: {broken: [unclosed\n", encoding="utf-8")


async def test_битая_политика_не_роняет_query(дом):
    from odata1c.config.loader import load_config

    _сломать_политику(дом)
    служба = ToolService(load_config(дом))
    try:
        текст = await служба.query(SessionScope(), base="ut", entity="Catalog_Валюты")
        assert json.loads(текст)["error"]["code"] == "policy_invalid"

        # Повтор — гейт не закэширован (конструктор упал), но ошибка та же, не голое исключение.
        текст_повтор = await служба.query(SessionScope(), base="ut", entity="Catalog_Валюты")
        assert json.loads(текст_повтор)["error"]["code"] == "policy_invalid"
    finally:
        await служба.aclose()


async def test_битая_политика_не_роняет_get(дом):
    from odata1c.config.loader import load_config

    _сломать_политику(дом)
    служба = ToolService(load_config(дом))
    try:
        текст = await служба.get(
            SessionScope(),
            base="ut",
            entity="Catalog_Контрагенты",
            key="a103cb54-42ee-11ec-a7a0-f10ab59a067e",
        )
        assert json.loads(текст)["error"]["code"] == "policy_invalid"
    finally:
        await служба.aclose()


async def test_битая_политика_не_роняет_describe_entity(дом):
    from odata1c.config.loader import load_config

    _сломать_политику(дом)
    служба = ToolService(load_config(дом))
    try:
        текст = await служба.describe_entity(
            SessionScope(), base="ut", entity="Catalog_Контрагенты"
        )
        assert json.loads(текст)["error"]["code"] == "policy_invalid"
    finally:
        await служба.aclose()


async def test_битая_политика_не_роняет_find_entity(дом):
    from odata1c.config.loader import load_config

    _сломать_политику(дом)
    служба = ToolService(load_config(дом))
    try:
        текст = await служба.find_entity(SessionScope(), base="ut", query="контрагенты")
        assert json.loads(текст)["error"]["code"] == "policy_invalid"
    finally:
        await служба.aclose()


async def test_битая_политика_не_роняет_bases(дом):
    """bases() не строит гейт вообще (не читает policy.yaml) — битая политика её не касается;
    тест фиксирует это явно, а не полагается на отсутствие исключения как на случайность."""
    from odata1c.config.loader import load_config

    _сломать_политику(дом)
    служба = ToolService(load_config(дом))
    try:
        текст = await служба.bases(SessionScope())
        данные = json.loads(текст)
        assert [б["name"] for б in данные["bases"]] == ["dev", "ut"]
    finally:
        await служба.aclose()


# =============================================================================================
# Задача 7 плана M1d: reindex, info, raw_get, ресурсы
# =============================================================================================


async def токен_названия(сервис: ToolService, название: str) -> str:
    """Токен названия организации — через ту же маскировку ответа, что и `токен_инн` выше:
    значение попадает в словарь, и с этого момента его знает и обратная подмена, и страж."""
    гейт = сервис._gate_for(сервис._config.bases["ut"])
    результат = гейт.mask(
        {"Description": название}, entity="Catalog_Контрагенты", resolve=без_навигаций
    )
    return результат.data["Description"]


def _скрыть_сущность(дом, имя: str) -> None:
    """Дописать `entities.<имя>.hide: true` в политику базы ut — ручной раздел, реиндекс его
    не трогает."""
    import yaml

    from odata1c.gate.service import policy_path

    путь = policy_path(дом, "ut")
    политика = yaml.safe_load(путь.read_text(encoding="utf-8")) or {}
    политика.setdefault("entities", {})[имя] = {"hide": True}
    путь.write_text(yaml.safe_dump(политика, allow_unicode=True), encoding="utf-8")


# ---------------------------------------------------------------------------------------------
# reindex
# ---------------------------------------------------------------------------------------------


async def test_reindex_перестраивает_индекс_и_возвращает_разницу(
    сервис, respx_ut, edmx_synthetic, дом
):
    # В доме лежит индекс по урезанному реальному УТ; 1С отдаёт ДРУГОЕ описание метаданных —
    # значит и контрольная сумма другая, и индекс обязан перестроиться.
    from odata1c.index.schema import connect

    respx_ut.get(f"{URL_UT}$metadata").mock(
        return_value=httpx.Response(200, content=edmx_synthetic)
    )
    # Отметка индексации пишется с точностью до секунды, а тест укладывается в одну: состаряем
    # прежнюю отметку явно, иначе сравнение «изменилась» ничего не проверяло бы.
    прежний_момент = "2000-01-01T00:00:00+00:00"
    соединение = connect(index_path(дом, "ut"))
    with соединение:
        соединение.execute("UPDATE meta SET value = ? WHERE key = 'indexed_at'", (прежний_момент,))
    соединение.close()

    данные = json.loads(await сервис.reindex(SessionScope(), base="ut"))

    assert данные["changed"] is True
    assert данные["indexed_at"] != прежний_момент
    assert "Catalog_Валюты" in данные["removed_entities"]
    assert "Catalog_БанковскиеСчета" in данные["added_entities"]
    assert данные["added_entities_total"] == len(данные["added_entities"]) == 3
    assert данные["removed_entities_total"] == len(данные["removed_entities"]) > 0
    assert данные["entity_count"] == 8

    стало = IndexRepository(index_path(дом, "ut"))
    try:
        assert стало.meta("indexed_at") == данные["indexed_at"]
        assert "Catalog_Валюты" not in стало.entity_names()
    finally:
        стало.close()


async def test_reindex_разница_урезана_до_предела(сервис, respx_ut, edmx_synthetic, monkeypatch):
    """Полный список разницы на боевой базе — тысячи имён: он вытеснил бы из ответа всё
    остальное. Показываются первые `_ПРЕДЕЛ_РАЗНИЦЫ`, полное число — рядом."""
    monkeypatch.setattr("odata1c.tools.service._ПРЕДЕЛ_РАЗНИЦЫ", 2)
    respx_ut.get(f"{URL_UT}$metadata").mock(
        return_value=httpx.Response(200, content=edmx_synthetic)
    )

    данные = json.loads(await сервис.reindex(SessionScope(), base="ut"))

    assert len(данные["removed_entities"]) == 2
    assert данные["removed_entities_total"] > 2


async def test_reindex_без_изменений_не_трогает_индекс(сервис, respx_ut, edmx_ut_real):
    respx_ut.get(f"{URL_UT}$metadata").mock(return_value=httpx.Response(200, content=edmx_ut_real))

    данные = json.loads(await сервис.reindex(SessionScope(), base="ut"))

    assert данные["changed"] is False
    assert "без изменений" in данные["message"]
    assert данные["added_entities"] == [] and данные["added_entities_total"] == 0


async def test_reindex_пересобирает_политику_и_гейт_её_видит(сервис, respx_ut, edmx_ut_real, дом):
    """После перестройки индекса политика пересобрана И гейт её перечитал.

    Пока гейт держит прежнюю политику, маскировщик знает прежние классы полей — то есть поле,
    ставшее защищаемым при этом реиндексе, ушло бы модели открытым. Проверяется с пустого места:
    политика удалена, гейт это увидел (поле не защищено), и только реиндекс возвращает защиту.
    """
    from odata1c.gate.service import policy_path

    # Поле выбрано так, чтобы защита зависела ИМЕННО от политики: `ИНН` узнаёт запасной
    # классификатор по имени поля даже с пустой политикой, а `Description` контрагента относит
    # к классу `org` только раздел `auto`, собранный по индексу.
    policy_path(дом, "ut").unlink()
    гейт = сервис._gate_for(сервис._config.bases["ut"])
    гейт.refresh()
    assert not гейт.is_protected("Catalog_Контрагенты", "Description")

    respx_ut.get(f"{URL_UT}$metadata").mock(return_value=httpx.Response(200, content=edmx_ut_real))
    данные = json.loads(await сервис.reindex(SessionScope(), base="ut", force=True))

    assert данные["changed"] is True
    assert policy_path(дом, "ut").exists()
    assert гейт.is_protected("Catalog_Контрагенты", "Description")


async def test_reindex_сохраняет_ручные_разделы_политики(сервис, respx_ut, edmx_ut_real, дом):
    """Реиндекс перезаписывает только раздел `auto`; `entities.hide` — ручная настройка
    владельца, и потерять её значит молча открыть модели то, что он закрыл."""
    гейт = сервис._gate_for(сервис._config.bases["ut"])
    _скрыть_сущность(дом, "Catalog_Валюты")
    гейт.refresh()
    assert гейт.is_hidden("Catalog_Валюты")

    respx_ut.get(f"{URL_UT}$metadata").mock(return_value=httpx.Response(200, content=edmx_ut_real))
    await сервис.reindex(SessionScope(), base="ut", force=True)

    assert гейт.is_hidden("Catalog_Валюты")


async def test_reindex_ошибка_1С_приходит_кодом_а_не_исключением(сервис, respx_ut):
    respx_ut.get(f"{URL_UT}$metadata").mock(
        return_value=httpx.Response(
            500, json={"odata.error": {"code": "1", "message": {"value": "нет доступа"}}}
        )
    )

    ошибка = json.loads(await сервис.reindex(SessionScope(), base="ut"))["error"]

    assert ошибка["code"] == "odata_error"


async def test_reindex_негодное_описание_метаданных(сервис, respx_ut):
    respx_ut.get(f"{URL_UT}$metadata").mock(
        return_value=httpx.Response(200, content=b"<edmx:Edmx><ne-zakryt>")
    )

    ошибка = json.loads(await сервис.reindex(SessionScope(), base="ut"))["error"]

    assert ошибка["code"] == "odata_error"
    assert "traceback" not in ошибка["message"].lower()


async def test_reindex_на_непроиндексированной_базе(tmp_path, edmx_ut_real):
    """Реиндекс обязан работать ровно там, где индекса ещё нет: `_run(with_index=False)`.

    Общий `_run` открывает индекс до вызова тела и на базе без индекса отдаёт `entity_unknown` —
    без отдельной ветки единственная команда, которая индекс создаёт, была бы недоступна.
    """
    from odata1c.config.loader import load_config

    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES_YAML, encoding="utf-8")
    служба = ToolService(load_config(home))
    try:
        with respx.mock(base_url=URL_UT, assert_all_called=False) as router:
            router.get(URL_UT).mock(return_value=httpx.Response(200, json={"value": []}))
            router.get(f"{URL_UT}$metadata").mock(
                return_value=httpx.Response(200, content=edmx_ut_real)
            )
            данные = json.loads(await служба.reindex(SessionScope(), base="ut"))
    finally:
        await служба.aclose()

    assert данные["changed"] is True
    assert index_path(home, "ut").exists()


@pytest.mark.skipif(
    sys.platform != "win32", reason="заменить открытый файл не даёт только Windows (WinError 32)"
)
async def test_reindex_на_открытом_индексе_отвечает_index_busy(сервис, respx_ut, edmx_synthetic):
    """Другая сессия читает индекс — `os.replace` на Windows не проходит.

    Несколько сессий на одной машине — условие продукта, и без отдельного кода вызывающий
    получил бы `internal` с текстом «подробности в журнале демона», по которому не понять, что
    достаточно повторить вызов.
    """
    respx_ut.get(f"{URL_UT}$metadata").mock(
        return_value=httpx.Response(200, content=edmx_synthetic)
    )
    читатель = IndexRepository(index_path(сервис._config.home, "ut"))
    читатель.entity_names()  # соединение открывает файл лениво — заставляем открыть
    try:
        текст = await сервис.reindex(SessionScope(), base="ut")
    finally:
        читатель.close()

    assert json.loads(текст)["error"]["code"] == "index_busy"


# ---------------------------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------------------------


async def test_info_все_темы_объяснимого_объёма(сервис):
    текст = await сервис.info("all")

    assert "[[" in текст  # про токены сказано
    assert len(текст) < 20_000  # справочник, а не пересказ спецификации
    for тема in ("naming", "standard_fields", "registers", "keys", "filter", "tokens"):
        assert await сервис.info(тема) in текст


async def test_info_тема_записи_одной_строкой(сервис):
    assert "M2" in await сервис.info("write_protocol")


async def test_info_неизвестная_тема(сервис):
    ошибка = json.loads(await сервис.info("что_нибудь"))["error"]

    assert ошибка["code"] == "params_invalid"
    assert "naming" in ошибка["hint"] and "all" in ошибка["hint"]


async def test_info_не_проходит_страж(сервис):
    """Справочник — единственный ответ тула мимо стража, и это должно быть ВИДНО.

    Страж здесь заряжен на слово, которое буквально стоит в тексте справочника (пример
    `substringof('Ромашка', Description)`): в любом другом ответе тула оно стало бы токеном.
    Справочник обязан прийти байт в байт таким, как он записан константой, — иначе объяснение
    превращается в мусор ради защиты того, что защищать не от чего.
    """
    from odata1c.tools import info as info_topics

    assert "Ромашка" in info_topics.ALL
    гейт = сервис._gate_for(сервис._config.bases["ut"])
    токен = await токен_названия(сервис, "Ромашка")
    assert токен.startswith("[["), "название не попало в словарь — тест проверял бы пустоту"
    assert "Ромашка" not in гейт.finish({"проба": "Ромашка"}), "страж не заряжен на это слово"

    текст = await сервис.info("all")

    assert текст == info_topics.ALL
    assert "Ромашка" in текст


# ---------------------------------------------------------------------------------------------
# raw_get
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "путь",
    [
        "../../../etc/passwd",
        "%2e%2e/Catalog_Контрагенты",
        "/Catalog_Контрагенты",
        "http://чужой-сервер/odata/standard.odata/Catalog_Контрагенты",
        "$metadata",
        "Catalog_Контрагенты?$top=1",
        "Catalog_Контрагенты\\..\\x",
        "",
    ],
)
async def test_raw_get_отклоняет_негодный_путь(сервис, respx_ut, путь):
    # Маршрут на корень базы зарегистрирован фикстурой: не сработай проверка, запрос ушёл бы
    # в 1С и тест увидел бы успешный ответ, а не params_invalid.
    текст = await сервис.raw_get(SessionScope(), base="ut", path=путь)

    assert json.loads(текст)["error"]["code"] == "params_invalid"


async def test_raw_get_маскирует_ответ(сервис, respx_ut):
    respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(
            200,
            json={
                "odata.metadata": "…",
                "value": [
                    {
                        "Ref_Key": "a103cb54-42ee-11ec-a7a0-f10ab59a067e",
                        "Description": "ООО Ромашка",
                        "ИНН": ИНН,
                        "Контрагент@navigationLinkUrl": "x",
                    }
                ],
            },
        )
    )

    текст = await сервис.raw_get(
        SessionScope(), base="ut", path="Catalog_Контрагенты", query={"$top": 1}
    )
    данные = json.loads(текст)

    assert ИНН not in текст and "Ромашка" not in текст
    # Именно маскировщик, а не страж: `masked_fields` заполняет только `gate.mask`, а страж,
    # поймай он значение сам, оставил бы в ответе `guard_replaced`. Без этих двух проверок тест
    # остаётся зелёным даже при полностью выключенной маскировке (страж поймает ИНН последним
    # проходом) — задокументированный в этом проекте способ проходить тест по чужой причине.
    assert "ИНН" in данные["masked_fields"] and "Description" in данные["masked_fields"]
    assert not any("guard_replaced" in п for п in данные["warnings"])
    assert данные["entity"] == "Catalog_Контрагенты"
    assert данные["count"] == 1
    assert "@navigationLinkUrl" not in текст


async def test_raw_get_ошибка_1С_проходит_гейт(сервис, respx_ut):
    await токен_инн(сервис, ИНН)
    respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(
            400,
            json={
                "odata.error": {
                    "code": "6",
                    "message": {"lang": "ru", "value": f"Сегмент пути {ИНН} не найден!"},
                }
            },
        )
    )

    текст = await сервис.raw_get(SessionScope(), base="ut", path="Catalog_Контрагенты")

    assert ИНН not in текст
    assert json.loads(текст)["error"]["code"] == "odata_error"


async def test_raw_get_токен_в_фильтре_уходит_реальным_значением(сервис, respx_ut):
    токен = await токен_инн(сервис, ИНН)
    маршрут = respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(200, json={"value": []})
    )

    await сервис.raw_get(
        SessionScope(),
        base="ut",
        path="Catalog_Контрагенты",
        query={"$filter": f"ИНН eq '{токен}'"},
    )

    assert ИНН in маршрут.calls.last.request.url.params["$filter"]


async def test_raw_get_токен_вне_фильтра_отклонён(сервис, respx_ut):
    токен = await токен_инн(сервис, ИНН)
    маршрут = respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(200, json={"value": []})
    )

    текст = await сервис.raw_get(
        SessionScope(),
        base="ut",
        path="Catalog_Контрагенты",
        query={"$select": f"Ref_Key,{токен}"},
    )

    assert json.loads(текст)["error"]["code"] == "params_invalid"
    assert not маршрут.called


async def test_raw_get_сортировка_по_защищаемому_полю_запрещена(сервис, respx_ut):
    маршрут = respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(200, json={"value": []})
    )

    текст = await сервис.raw_get(
        SessionScope(), base="ut", path="Catalog_Контрагенты", query={"$orderby": "ИНН desc"}
    )

    assert json.loads(текст)["error"]["code"] == "params_invalid"
    assert not маршрут.called


async def test_raw_get_скрытая_сущность_не_читается(сервис, respx_ut, дом):
    маршрут = respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(200, json={"value": [{"ИНН": ИНН}]})
    )
    _скрыть_сущность(дом, "Catalog_Контрагенты")

    текст = await сервис.raw_get(SessionScope(), base="ut", path="Catalog_Контрагенты")

    assert json.loads(текст)["error"]["code"] == "entity_hidden"
    assert not маршрут.called


async def test_raw_get_скрытая_сущность_не_читается_через_навигацию(сервис, respx_ut, дом):
    """Запрет `entities.hide` обходится навигацией, если проверять только первый сегмент пути."""
    маршрут = respx_ut.get(url__regex=r".*Document_.*").mock(
        return_value=httpx.Response(200, json={"ИНН": ИНН})
    )
    _скрыть_сущность(дом, "Catalog_Контрагенты")

    текст = await сервис.raw_get(
        SessionScope(),
        base="ut",
        path=f"Document_РеализацияТоваровУслуг(guid'{ССЫЛКА}')/Контрагент",
    )

    assert json.loads(текст)["error"]["code"] == "entity_hidden"
    assert not маршрут.called


async def test_raw_get_маскирует_по_сущности_навигации(сервис, respx_ut):
    """Путь через навигацию отдаёт запись ЦЕЛИ, а не первого сегмента.

    `Description` — то самое поле, на котором это видно: классом `org` его помечает раздел
    `auto` политики по паре «сущность и поле», и для `Catalog_Контрагенты` такая пара есть, а
    для `Document_РеализацияТоваровУслуг` — нет. Маскируй ответ классами первого сегмента —
    название организации ушло бы открытым. `ИНН` для этой проверки не годится: его узнаёт
    запасной классификатор по имени поля независимо от сущности.
    """
    respx_ut.get(url__regex=r".*Document_.*").mock(
        return_value=httpx.Response(200, json={"Ref_Key": ССЫЛКА, "Description": "Ромашка"})
    )

    текст = await сервис.raw_get(
        SessionScope(),
        base="ut",
        path=f"Document_РеализацияТоваровУслуг(guid'{ССЫЛКА}')/Контрагент",
    )
    данные = json.loads(текст)

    assert данные["entity"] == "Catalog_Контрагенты"
    assert "Ромашка" not in текст
    assert "Description" in данные["masked_fields"]
    assert not any("guard_replaced" in п for п in данные["warnings"])
    assert данные["item"]["Ref_Key"] == ССЫЛКА


async def test_raw_get_неизвестная_база(сервис):
    текст = await сервис.raw_get(SessionScope(), base="нет_такой", path="Catalog_Валюты")

    assert json.loads(текст)["error"]["code"] == "base_unknown"


# ---------------------------------------------------------------------------------------------
# ресурсы
# ---------------------------------------------------------------------------------------------


async def test_resource_policy_отдаёт_текст_политики(сервис):
    текст = await сервис.resource_policy(SessionScope(), "ut")

    assert "Catalog_Контрагенты.ИНН" in текст
    assert "auto" in текст


async def test_resource_policy_чужой_базы_не_видна_суженной_сессии(сервис):
    текст = await сервис.resource_policy(SessionScope(bases=("dev",)), "ut")

    assert json.loads(текст)["error"]["code"] == "base_unknown"


async def test_resource_policy_без_политики(сервис, дом):
    from odata1c.gate.service import policy_path

    policy_path(дом, "ut").unlink()

    ошибка = json.loads(await сервис.resource_policy(SessionScope(), "ut"))["error"]

    assert "odata1c_reindex" in ошибка["hint"]


async def test_resource_index_сводка(сервис):
    данные = json.loads(await сервис.resource_index(SessionScope(), "ut"))

    assert данные["base"] == "ut"
    assert данные["indexed_at"]
    assert данные["kinds"]["Справочник"] == 7
    assert данные["entity_count"] == sum(данные["kinds"].values())


async def test_resource_index_непроиндексированной_базы(сервис):
    ошибка = json.loads(await сервис.resource_index(SessionScope(), "dev"))["error"]

    assert ошибка["code"] == "entity_unknown"


# =============================================================================================
# Раунд правок 1: утечка названий через raw_get на пути, не разрешимом по индексу
#
# Четыре формы атаки, воспроизведённые ревьюером на поддельной 1С. Общая причина одна:
# `_цель_пути` при неразрешимом пути открывался вместо того, чтобы закрыться, а запасной
# классификатор (`classify_field(..., names_for=set())`) выключает ровно тот слой, который
# защищает Description, НаименованиеПолное и ФИО. Ответ при этом выглядел успешным:
# masked_fields ["ИНН"], guard_replaced нет, «ООО Ромашка» открытым текстом.
#
# `Catalog_Партнеры` для этих тестов выбран не случайно: этой сущности НЕТ в фикстуре
# `ut-real.edmx`, но она стоит в `field_rules.DEFAULT_NAMES_FOR` — то есть при живом индексе её
# Description был бы классом org, и разница «есть сущность в индексе / нет» видна начисто.
# =============================================================================================

ЗАПИСЬ_ПАРТНЁРА = {
    "Ref_Key": ССЫЛКА,
    "Description": "ООО Ромашка",
    "НаименованиеПолное": "Общество с ограниченной ответственностью «Ромашка»",
    "ИНН": ИНН,
}


async def test_raw_get_вне_индекса_маскирует_названия(сервис, respx_ut):
    """Форма (а): набор, которого нет в индексе, — заявленный сценарий самого `raw_get`."""
    respx_ut.get("Catalog_Партнеры").mock(
        return_value=httpx.Response(200, json={"value": [ЗАПИСЬ_ПАРТНЁРА]})
    )

    текст = await сервис.raw_get(SessionScope(), base="ut", path="Catalog_Партнеры")
    данные = json.loads(текст)

    assert "Ромашка" not in текст
    assert {"Description", "НаименованиеПолное", "ИНН"} <= set(данные["masked_fields"])
    assert not any("guard_replaced" in п for п in данные["warnings"])
    assert any("вне индекса" in п for п in данные["warnings"])
    assert данные["items"][0]["Ref_Key"] == ССЫЛКА  # инвариант 6: ссылка не тронута


async def test_raw_get_неразрешимая_навигация_маскирует_названия(сервис, respx_ut):
    """Форма (б): сегмент в середине пути индексу неизвестен — раньше разбор молча
    останавливался на документе, и запись контрагента маскировалась классами документа."""
    respx_ut.get(url__regex=r".*Document_.*").mock(
        return_value=httpx.Response(200, json=ЗАПИСЬ_ПАРТНЁРА)
    )

    текст = await сервис.raw_get(
        SessionScope(),
        base="ut",
        path=f"Document_РеализацияТоваровУслуг(guid'{ССЫЛКА}')/Партнер",
    )
    данные = json.loads(текст)

    assert "Ромашка" not in текст
    assert {"Description", "НаименованиеПолное"} <= set(данные["masked_fields"])
    assert any("вне индекса" in п for п in данные["warnings"])


async def test_raw_get_примитивное_свойство_маскирует_реквизит(сервис, respx_ut):
    """Форма (в): ответ на примитивное свойство — `{"value": <скаляр>}`, имя поля в ответе не
    приходит вовсе, оно стоит в пути. Без восстановления имени из пути 10-значный ИНН не
    распознаётся детектором (ему нужно слово «ИНН» рядом) и уходит открытым."""
    respx_ut.get(url__regex=r".*Catalog_.*").mock(
        return_value=httpx.Response(200, json={"odata.metadata": "…", "value": ИНН})
    )

    текст = await сервис.raw_get(
        SessionScope(), base="ut", path=f"Catalog_Контрагенты(guid'{ССЫЛКА}')/ИНН"
    )
    данные = json.loads(текст)

    assert ИНН not in текст
    assert "ИНН" in данные["masked_fields"]


async def test_raw_get_сортировка_по_названию_вне_индекса_запрещена(сервис, respx_ut):
    """Форма (г): оракул порядка. Запрет на сортировку по защищаемому полю снимался ровно там,
    где снималась маска, — на сущности вне индекса."""
    маршрут = respx_ut.get("Catalog_Партнеры").mock(
        return_value=httpx.Response(200, json={"value": [ЗАПИСЬ_ПАРТНЁРА]})
    )

    текст = await сервис.raw_get(
        SessionScope(), base="ut", path="Catalog_Партнеры", query={"$orderby": "Description"}
    )

    assert json.loads(текст)["error"]["code"] == "params_invalid"
    assert not маршрут.called


async def test_raw_get_вне_индекса_не_маскирует_лишнего(сервис, respx_ut):
    """Инвариант 6: суммы, количества, даты, GUID, коды и номера документов не защищаются ни на
    каком уровне — избыточное срабатывание строгой политики такой же дефект, как пропуск."""
    запись = {
        "Ref_Key": ССЫЛКА,
        "Date": "2026-01-15T00:00:00",
        "Number": "ТД-004512",
        "Code": "00-000123",
        "СуммаДокумента": "125000.50",
        "Количество": "17",
    }
    respx_ut.get("Document_НетТакого").mock(
        return_value=httpx.Response(200, json={"value": [запись]})
    )

    текст = await сервис.raw_get(SessionScope(), base="ut", path="Document_НетТакого")
    данные = json.loads(текст)

    assert данные["items"][0] == запись
    assert данные["masked_fields"] == []


@pytest.mark.parametrize("путь", ["Catalog_Контрагенты", "Catalog_Партнеры"])
async def test_raw_get_анти_оракул_фильтра_одинаков_вне_индекса(сервис, respx_ut, путь):
    """Проверки `$filter` на неразрешённом пути не должны быть слабее, чем на разрешённом.

    Прогон обеих веток (`Catalog_Контрагенты` есть в индексе, `Catalog_Партнеры` — нет)
    показывает, что они симметричны: ИНН с битой контрольной суммой отклоняется и там и там
    (первый слой классификатора по имени поля от сущности не зависит), название открытым
    текстом и `substringof` по названию разрешены и там и там (SPEC §6.7, `ИМЕНОВАННЫЕ_КЛАССЫ`:
    модель знает название только если его продиктовал пользователь). Тест сторожит ту половину,
    где отказ обязателен."""
    маршрут = respx_ut.get(url__regex=r".*atalog_.*").mock(
        return_value=httpx.Response(200, json={"value": []})
    )

    текст = await сервис.raw_get(
        SessionScope(), base="ut", path=путь, query={"$filter": "ИНН eq '1234567890'"}
    )

    assert json.loads(текст)["error"]["code"] == "filter_syntax"
    assert not маршрут.called


async def test_raw_get_имя_в_чужом_регистре_не_обходит_запрет(сервис, respx_ut, дом):
    """Форма (д): имя набора в другом регистре. Индекс и `entities.hide` сверяют имя точным
    сравнением — если публикация 1С к регистру нечувствительна, запрет обходится одной буквой."""
    маршрут = respx_ut.get(url__regex=r".*atalog_.*").mock(
        return_value=httpx.Response(200, json={"value": [ЗАПИСЬ_ПАРТНЁРА]})
    )
    _скрыть_сущность(дом, "Catalog_Контрагенты")

    текст = await сервис.raw_get(SessionScope(), base="ut", path="catalog_Контрагенты")

    assert json.loads(текст)["error"]["code"] == "entity_hidden"
    assert not маршрут.called


# =============================================================================================
# Раунд правок 1 задачи 8: токен раскрывается только при известном поле; путь `$value`
# =============================================================================================

# Класс гейта → реальное значение (тот же перебор, что в test_gate_token_context.py: сторож на
# одном ИНН дыры в `addr`/`dob` не ловит).
ЗНАЧЕНИЯ_КЛАССОВ: dict[str, str] = {
    "inn": ИНН,
    "kpp": "770701001",
    "ogrn": "1027700132195",
    "acc": "40702810900000012345",
    "corr": "30101810400000000225",
    "bic": "044525225",
    "iban": "DE89370400440532013000",
    "card": "4111111111111111",
    "snils": "112-233-445 95",
    "doc": "45 03 123456",
    "phone": "+7 916 123-45-67",
    "email": "ivan@example.com",
    "dob": "1980-05-01",
    "addr": "г. Москва, ул. Тверская, д. 7, кв. 43",
    "org": "ООО Ромашка",
    "person": "Иванов Иван Иванович",
}


def _токен_класса(служба: ToolService, класс: str) -> tuple[str, str]:
    гейт = служба._gate_for(служба._config.bases["ut"])
    значение = ЗНАЧЕНИЯ_КЛАССОВ[класс]
    токен = гейт._dictionary.token_for(
        класс, значение, base="ut", entity="Catalog_ФизическиеЛица", field=f"Поле{класс}"
    )
    return токен, значение


@pytest.mark.parametrize("класс", sorted(ЗНАЧЕНИЯ_КЛАССОВ))
async def test_get_токен_в_ключе_не_раскрывается(сервис, respx_ut, класс):
    """C2 ревью 2026-09-11: `odata1c_get(key="[[addr:…]]")` раскрывал токен любого класса и
    возвращал реальное значение в тексте отказа построителя ключа. Простой ключ 1С — всегда
    `Ref_Key`, то есть GUID: токен там не бывает правильным ни при каком классе."""
    маршрут = respx_ut.get(url__regex=r".*Catalog_.*").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    токен, значение = _токен_класса(сервис, класс)

    текст = await сервис.get(SessionScope(), base="ut", entity="Catalog_Контрагенты", key=токен)

    assert json.loads(текст)["error"]["code"] == "token_type_mismatch"
    assert значение not in текст
    assert not маршрут.called


async def test_get_настоящий_ключ_по_прежнему_работает(сервис, respx_ut):
    """Парный разрешающий случай: запрет на токен в ключе не должен ломать обычный `get`."""
    respx_ut.get(url__regex=r".*Catalog_.*").mock(
        return_value=httpx.Response(200, json={"Ref_Key": ССЫЛКА, "Description": "ООО Ромашка"})
    )
    данные = json.loads(
        await сервис.get(SessionScope(), base="ut", entity="Catalog_Контрагенты", key=ССЫЛКА)
    )
    assert данные["item"]["Ref_Key"] == ССЫЛКА


@pytest.mark.parametrize("хвост", ["$value", "$count", "$ref"], ids=["value", "count", "ref"])
async def test_raw_get_системный_сегмент_после_реквизита_отклонён(сервис, respx_ut, хвост):
    """Пункт 5 дополнения: `…/ИНН/$value` отдавал реальный ИНН числом при пустом `masked_fields`.
    Системный сегмент вытеснял имя реквизита из хвоста пути, а обёртка скаляра снимала прежний
    отказ. Системные сегменты именем поля не становятся и после реквизита не допускаются."""
    маршрут = respx_ut.get(url__regex=r".*Catalog_.*").mock(
        return_value=httpx.Response(200, text=f'"{ИНН}"')
    )

    текст = await сервис.raw_get(
        SessionScope(), base="ut", path=f"Catalog_Контрагенты(guid'{ССЫЛКА}')/ИНН/{хвост}"
    )

    assert json.loads(текст)["error"]["code"] in ("params_invalid", "entity_unknown")
    assert ИНН not in текст
    assert not маршрут.called


async def test_raw_get_лишний_сегмент_после_реквизита_отклонён(сервис, respx_ut):
    """Тот же корень с произвольным хвостом: `…/ИНН/что_угодно` переименовывал запись в
    `что_угодно` и терял имя `ИНН`, ради которого восстановление имени и написано. Неразрешённых
    сегментов больше одного — отказ, а не угадывание имени поля."""
    маршрут = respx_ut.get(url__regex=r".*Catalog_.*").mock(
        return_value=httpx.Response(200, json={"odata.metadata": "…", "value": ИНН})
    )

    текст = await сервис.raw_get(
        SessionScope(), base="ut", path=f"Catalog_Контрагенты(guid'{ССЫЛКА}')/ИНН/что_угодно"
    )

    assert json.loads(текст)["error"]["code"] == "entity_unknown"
    assert ИНН not in текст
    assert not маршрут.called


@pytest.mark.parametrize("тело", [{"value": int(ИНН)}, {"value": ИНН}], ids=["число", "строка"])
async def test_raw_get_скаляр_реквизита_маскируется(сервис, respx_ut, тело):
    """Второе звено пункта 5: число маскировщик не трогает (и правильно — инвариант 6), а страж
    числовые литералы внутри конверта пропускает сознательно. Значит закрыть это можно только
    до маскировки — приведением скаляра к строке."""
    respx_ut.get(url__regex=r".*Catalog_.*").mock(
        return_value=httpx.Response(200, json={"odata.metadata": "…", **тело})
    )

    текст = await сервис.raw_get(
        SessionScope(), base="ut", path=f"Catalog_Контрагенты(guid'{ССЫЛКА}')/ИНН"
    )
    данные = json.loads(текст)

    assert ИНН not in текст
    assert "ИНН" in данные["masked_fields"]


@pytest.mark.parametrize(
    ("тело", "ожидается"),
    [(42, "42"), (int("40702810900000012345"), None)],
    ids=["чужое-число", "само-раскрытое-число"],
)
async def test_raw_get_скаляр_при_непустом_наборе_раскрытого(сервис, respx_ut, тело, ожидается):
    """Скалярное тело (`…/$count`) на вызове, который ЧТО-ТО раскрыл (Ruling 25): ранний проход
    идёт по сырому телу, а у скаляра у стража особая ветка — замена оборачивается в строку JSON.

    Заменять нечего — форма ответа обязана остаться прежней (число в конверте строкой). Есть
    что — значение закрывается токеном, и конверт остаётся валидным; это та же ветка, что и у
    позднего прохода, только раньше по конвейеру.
    """
    поле, значение = "НомерСчета", "40702810900000012345"
    токен = _токен_поля(сервис, "acc", поле, значение)
    # Кириллица в пути уходит в процентной записи — маршрут по имени сущности её не ловит.
    respx_ut.route(method="GET").mock(return_value=httpx.Response(200, json=тело))

    текст = await сервис.raw_get(
        SessionScope(),
        base="ut",
        path=f"{ЛИЦА}/$count",
        query={"$filter": f"{поле} eq '{токен}'"},
    )

    элемент = json.loads(текст)["item"]
    if ожидается is not None:
        assert элемент == {"value": ожидается}
    else:
        assert значение not in текст and токен in текст


async def test_raw_get_скаляр_вне_json_маскируется(сервис, respx_ut):
    """Ответ `$value`-подобной публикации приходит не словарём вовсе: обёртка скаляра обязана
    класть его в конверт строкой, иначе он проходит и мимо маскировщика, и мимо стража."""
    respx_ut.get(url__regex=r".*Catalog_.*").mock(return_value=httpx.Response(200, json=int(ИНН)))

    текст = await сервис.raw_get(
        SessionScope(), base="ut", path=f"Catalog_Контрагенты(guid'{ССЫЛКА}')/ИНН"
    )

    assert ИНН not in текст


@pytest.mark.parametrize(
    "значение",
    [
        1234567.89,  # сумма
        17,  # количество
        4601234567893,  # EAN-13, проходит Luhn
        "0104607093950746215",  # 19-значный код маркировки
        "2026-01-15T00:00:00",  # дата
        "УТ-00012345",  # номер документа
    ],
    ids=["сумма", "количество", "штрихкод", "кодмаркировки", "дата", "номер"],
)
async def test_raw_get_скаляр_не_маскирует_лишнего(сервис, respx_ut, значение):
    """Инвариант 6 через НОВЫЙ путь: приведение скаляра к строке отдаёт его детекторам, поэтому
    отсутствие ложных срабатываний надо доказывать заново именно здесь, а не на обычном `query`.
    Суммы, количества, коды и номера документов не защищаются ни на одном уровне."""
    respx_ut.get(url__regex=r".*Document_.*").mock(
        return_value=httpx.Response(200, json={"odata.metadata": "…", "value": значение})
    )

    текст = await сервис.raw_get(
        SessionScope(), base="ut", path=f"Document_РеализацияТоваровУслуг(guid'{ССЫЛКА}')/СуммаДок"
    )
    данные = json.loads(текст)

    assert данные["masked_fields"] == []
    assert str(значение) in текст


# ---------------------------------------------------------------------------------------------
# Пункт 6 дополнения: `entities.hide` обходится неразрешённым звеном и украшением имени
# ---------------------------------------------------------------------------------------------

УКРАШЕНИЯ_ИМЕНИ = [
    "Catalog_Контрагенты.",  # завершающая точка — IIS её срезает, до 1С дойдёт каноническое имя
    "Catalog_Контрагенты​",  # невидимый пробел
    "Catalog_Контраге́нты",  # комбинирующее ударение (NFD)
]


@pytest.mark.parametrize("имя", УКРАШЕНИЯ_ИМЕНИ)
@pytest.mark.parametrize(
    "скрытая",
    ["Catalog_Контрагенты", "Catalog_Валюты"],
    ids=["скрыт_он_же", "скрыта_другая"],
)
async def test_raw_get_украшенное_имя_не_обходит_запрет(сервис, respx_ut, дом, имя, скрытая):
    """Ruling 21 дополнения: не гнаться за нормализациями (их всегда окажется на одну больше), а
    отказывать, если имя после канонизации не совпадает с индексом, — при условии, что владельцу
    вообще есть что скрывать.

    Второй набор параметров сторожит формулировку правила: отказ привязан к НАЛИЧИЮ правил `hide`
    у базы, а не к скрытости именно того набора, чьё имя украшено. Иначе шлюзу пришлось бы знать,
    во что канонизирует украшенное имя 1С за IIS, — то есть ровно то знание, которого у него нет
    и ради отсутствия которого правило и написано."""
    маршрут = respx_ut.get(url__regex=r".*").mock(
        return_value=httpx.Response(200, json={"value": [ЗАПИСЬ_ПАРТНЁРА]})
    )
    _скрыть_сущность(дом, скрытая)

    текст = await сервис.raw_get(SessionScope(), base="ut", path=имя)

    assert json.loads(текст)["error"]["code"] == "entity_unknown"
    assert not маршрут.called


async def test_raw_get_неразрешённая_навигация_не_обходит_запрет(сервис, respx_ut, дом):
    """Неразрешённое звено пути пропускало проверку скрытости вовсе: значения приходили
    замаскированными, но запрет владельца был снят — видны состав записи, коды, даты и сам факт
    её существования."""
    маршрут = respx_ut.get(url__regex=r".*Document_.*").mock(
        return_value=httpx.Response(200, json=ЗАПИСЬ_ПАРТНЁРА)
    )
    _скрыть_сущность(дом, "Catalog_Партнеры")

    текст = await сервис.raw_get(
        SessionScope(),
        base="ut",
        path=f"Document_РеализацияТоваровУслуг(guid'{ССЫЛКА}')/Партнер",
    )

    assert json.loads(текст)["error"]["code"] == "entity_unknown"
    assert not маршрут.called


async def test_raw_get_вне_индекса_работает_без_правил_скрытия(сервис, respx_ut):
    """Контрольный прогон к двум предыдущим: заявленный сценарий `raw_get` — набор, которого ещё
    нет в индексе, — обязан продолжать работать, когда владелец ничего не скрывал. Отказ
    привязан к наличию правил `hide`, а не к неизвестности имени самой по себе."""
    respx_ut.get("Catalog_Партнеры").mock(
        return_value=httpx.Response(200, json={"value": [ЗАПИСЬ_ПАРТНЁРА]})
    )

    данные = json.loads(await сервис.raw_get(SessionScope(), base="ut", path="Catalog_Партнеры"))

    assert "error" not in данные
    assert {"Description", "НаименованиеПолное", "ИНН"} <= set(данные["masked_fields"])


# ---------------------------------------------------------------------------------------------
# Пункт 7 дополнения: `$filter` строг и вне индекса (Ruling 18 — «тем более»)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "выражение,отказ",
    [
        ("Description eq 'ООО Ромашка'", False),
        ("Description ne 'ООО Ромашка'", False),
        ("Description gt 'А'", True),
        ("Description ge 'М'", True),
        ("Description lt 'Я'", True),
        ("Description le 'Б'", True),
        ("substringof('Ромаш', Description)", False),
        ("startswith(Description, 'ООО')", False),
        ("endswith(Description, 'шка')", False),
    ],
)
@pytest.mark.parametrize("путь", ["Catalog_Контрагенты", "Catalog_Партнеры"])
async def test_raw_get_фильтр_одинаков_внутри_и_вне_индекса(
    сервис, respx_ut, путь, выражение, отказ
):
    """Вердикт зависит от формы сравнения, но не от того, есть сущность в индексе или нет.
    Прошлый тест проверял одну форму (ИНН с битой контрольной суммой) и не задевал упорядоченное
    сравнение — а именно там асимметрия и была: `Description ge 'М'` вне индекса уходил в 1С,
    то есть двоичный поиск по названию работал."""
    маршрут = respx_ut.get(url__regex=r".*atalog_.*").mock(
        return_value=httpx.Response(200, json={"value": []})
    )

    текст = await сервис.raw_get(SessionScope(), base="ut", path=путь, query={"$filter": выражение})
    данные = json.loads(текст)

    if отказ:
        assert данные["error"]["code"] == "filter_syntax"
        assert not маршрут.called
    else:
        assert "error" not in данные


async def test_raw_get_известная_сущность_без_предупреждения(сервис, respx_ut):
    """Контрольный прогон: на разрешённом пути строгой политики нет и предупреждения тоже —
    иначе «вне индекса» в предупреждениях означало бы просто «всегда»."""
    respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(200, json={"value": [{"Description": "ООО Ромашка"}]})
    )

    данные = json.loads(await сервис.raw_get(SessionScope(), base="ut", path="Catalog_Контрагенты"))

    assert not any("вне индекса" in п for п in данные["warnings"])


# ---------------------------------------------------------------------------------------------
# Ruling 19: токен, выданный базой, работает и после смены класса поля
# ---------------------------------------------------------------------------------------------


async def test_токен_переживает_смену_класса_поля(сервис, respx_ut, edmx_ut_real, дом):
    """Класс поля входит в HMAC токена, а реиндекс переписывает раздел `auto` политики — то же
    значение после реиндекса отдаётся другим токеном. Токен, который модель получила от шлюза
    десять минут назад, обязан продолжать работать: иначе `odata1c_reindex` и фоновая проверка
    ломают сессию молча и посреди работы.
    """
    import yaml

    from odata1c.gate.service import policy_path

    токен = await токен_названия(сервис, "Ромашка")
    assert токен.startswith("[[org:")

    # Класс того же поля меняется: org → person (ручной раздел; реиндекс его сохранит).
    путь = policy_path(дом, "ut")
    политика = yaml.safe_load(путь.read_text(encoding="utf-8")) or {}
    политика.setdefault("fields", {})["Catalog_Контрагенты.Description"] = "person"
    путь.write_text(yaml.safe_dump(политика, allow_unicode=True), encoding="utf-8")

    respx_ut.get(f"{URL_UT}$metadata").mock(return_value=httpx.Response(200, content=edmx_ut_real))
    await сервис.reindex(SessionScope(), base="ut", force=True)

    маршрут = respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    текст = await сервис.query(
        SessionScope(),
        base="ut",
        entity="Catalog_Контрагенты",
        filter=f"Description eq '{токен}'",
    )

    assert "error" not in json.loads(текст)
    assert "Ромашка" in маршрут.calls.last.request.url.params["$filter"]


async def test_чужой_токен_в_поле_другого_класса_по_прежнему_отклонён(сервис, respx_ut):
    """Послабление Ruling 19 именное: оно касается токена, выданного ЭТОЙ базой для ЭТОГО поля.
    Токен из другого поля в чужом поле — это оракул сравнения, и он остаётся отказом."""
    токен = await токен_инн(сервис, ИНН)
    маршрут = respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(200, json={"value": []})
    )

    текст = await сервис.query(
        SessionScope(),
        base="ut",
        entity="Catalog_Контрагенты",
        filter=f"Description eq '{токен}'",
    )

    assert json.loads(текст)["error"]["code"] == "token_type_mismatch"
    assert not маршрут.called


async def test_reindex_предупреждает_о_смене_классов(сервис, respx_ut, edmx_synthetic):
    """Смена классов меняет токены новых ответов: одно и то же значение до и после реиндекса
    приходит разными токенами. Утечки нет, но модель должна знать, почему токен «поменялся»."""
    respx_ut.get(f"{URL_UT}$metadata").mock(
        return_value=httpx.Response(200, content=edmx_synthetic)
    )

    данные = json.loads(await сервис.reindex(SessionScope(), base="ut"))

    assert данные["new_sensitive_fields_total"] > 0
    assert any("токен" in п for п in данные["warnings"])


# ---------------------------------------------------------------------------------------------
# Замечания ревью: форма ответа-списка, $select списком, сужение index_busy
# ---------------------------------------------------------------------------------------------


async def test_raw_get_список_отдаёт_поля_страницы(сервис, respx_ut):
    """Бриф: «ответ — как у `query`/`get` по форме». Без `has_more`/`next_skip` модель не
    отличает «это всё» от «есть ещё» и ставит `$skip` наугад."""
    respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(
            200, json={"odata.count": "150", "value": [{"Ref_Key": ССЫЛКА}] * 2}
        )
    )

    данные = json.loads(
        await сервис.raw_get(
            SessionScope(),
            base="ut",
            path="Catalog_Контрагенты",
            query={"$top": 2, "$skip": 10, "$inlinecount": "allpages"},
        )
    )

    assert данные["count"] == 2 and данные["total"] == 150
    assert данные["has_more"] is True and данные["next_skip"] == 12


async def test_raw_get_select_списком(сервис, respx_ut):
    """`query` принимает `select` и списком, и строкой; у `raw_get` список отклонялся как
    «значение должно быть строкой или числом»."""
    маршрут = respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(200, json={"value": []})
    )

    текст = await сервис.raw_get(
        SessionScope(),
        base="ut",
        path="Catalog_Контрагенты",
        query={"$select": ["Ref_Key", "Description"]},
    )

    assert "error" not in json.loads(текст)
    assert маршрут.calls.last.request.url.params["$select"] == "Ref_Key,Description"


async def test_index_busy_не_подменяет_прочие_отказы_в_правах(сервис, respx_ut, monkeypatch):
    """`PermissionError` бывает не только от занятого файла: нет прав на домашний каталог,
    каталог только для чтения. Ответ «повторите через несколько секунд» на такое заставит
    владельца повторять вызов бесконечно."""

    async def отказ_в_правах(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("odata1c.tools.service.rebuild_index", отказ_в_правах)

    ошибка = json.loads(await сервис.reindex(SessionScope(), base="ut"))["error"]

    assert ошибка["code"] == "internal"


# ---------------------------------------------------------------------------------------------
# Итоговое ревью M1d: $expand сквозь ToolService — своя политика у раскрытого объекта (C1)
# и entities.hide через раскрытие связи (C2). Сквозных тестов `$expand` не было ни одного.
# ---------------------------------------------------------------------------------------------

ДОКУМЕНТ = "Document_РеализацияТоваровУслуг"
НАЗВАНИЕ_КЛИЕНТА = "ООО Незабудка-Трейд"
ФИО_КУРЬЕРА = "Петров Пётр Петрович"
# Живые формы инварианта 6 из отчёта приёмки на базе владельца (задача 9 M1d).
НОМЕР_ДОКУМЕНТА = "00УТ-180814"
ДАТА_ДОКУМЕНТА = "2026-08-26T19:30:11"


def _документ_с_раскрытием(**добавки) -> dict:
    """Ответ 1С на документ с раскрытыми `КлиентКонтрагент` и `Курьер`.

    Имена навигаций настоящие и выбраны намеренно: ни одно из них не входит ни в какой список
    имён-контейнеров, поэтому наследование класса от контейнера здесь не срабатывает и зелёный
    результат может дать только переключение политики на сущность-цель навигации."""
    запись = {
        "Ref_Key": ССЫЛКА,
        "Number": НОМЕР_ДОКУМЕНТА,
        "Date": ДАТА_ДОКУМЕНТА,
        "СуммаДокумента": 145200.5,
        "КлиентКонтрагент": {"Ref_Key": ССЫЛКА, "Description": НАЗВАНИЕ_КЛИЕНТА, "КодПоОКПО": ОКПО},
        "Курьер": {"Ref_Key": ССЫЛКА, "Description": ФИО_КУРЬЕРА},
    }
    запись.update(добавки)
    return запись


def _запись_ответа(данные: dict) -> dict:
    return данные["items"][0] if "items" in данные else данные["item"]


async def _вызвать_с_expand(сервис, тул: str, expand: list[str], *, base: str = "ut") -> str:
    """Один и тот же `$expand` тремя дверями к одной структуре (урок этапа о покрытии по формам:
    `raw_get` — вторая дверь, и её уже ловили на том же)."""
    if тул == "query":
        return await сервис.query(SessionScope(), base=base, entity=ДОКУМЕНТ, expand=expand)
    if тул == "get":
        return await сервис.get(
            SessionScope(), base=base, entity=ДОКУМЕНТ, key=ССЫЛКА, expand=expand
        )
    return await сервис.raw_get(
        SessionScope(),
        base=base,
        path=f"{ДОКУМЕНТ}(guid'{ССЫЛКА}')",
        query={"$expand": ",".join(expand)} if expand else None,
    )


@pytest.fixture
def любой_get(respx_ut):
    """Перехват любого GET: пути `get`/`raw_get` — сущность со скобками и ключом в процентной
    записи, маршрут по имени сущности их не ловит."""

    def ответить(тело: dict):
        return respx_ut.route(method="GET").mock(return_value=httpx.Response(200, json=тело))

    return ответить


@pytest.mark.parametrize("тул", ["query", "get", "raw_get"])
async def test_expand_маскирует_раскрытый_объект_по_его_политике(сервис, любой_get, тул):
    """C1: SPEC §6.5 (строка 772) — «раскрытый объект обрабатывается по политике своей
    сущности». До правки классы искались по паре «сущность верхнего уровня и поле», то есть для
    раскрытого контрагента не работали ни `auto`, ни ручной раздел `fields` его настоящей
    сущности: название организации и ФИО уходили открытым текстом при `masked_fields`, где
    `Description` числился замаскированным, — то есть список ещё и врал (Ruling 20)."""
    любой_get({"value": [_документ_с_раскрытием()]})

    текст = await _вызвать_с_expand(сервис, тул, ["КлиентКонтрагент", "Курьер"])
    данные = json.loads(текст)
    запись = _запись_ответа(данные)

    assert НАЗВАНИЕ_КЛИЕНТА not in текст and ФИО_КУРЬЕРА not in текст
    assert запись["КлиентКонтрагент"]["Description"].startswith("[[org:")
    assert запись["Курьер"]["Description"].startswith("[[person:")
    assert "Description" in данные["masked_fields"]


@pytest.mark.parametrize("тул", ["query", "get", "raw_get"])
async def test_инвариант_6_в_раскрытом_объекте_сквозь_тулы(сервис, любой_get, тул):
    """Суммы, даты, номера и коды в ответе с `$expand` целы — на живых формах из отчёта приёмки
    (номер `00УТ-180814`, дата `2026-08-26T19:30:11`, ОКПО `09226071`)."""
    любой_get({"value": [_документ_с_раскрытием()]})

    данные = json.loads(await _вызвать_с_expand(сервис, тул, ["КлиентКонтрагент"]))
    запись = _запись_ответа(данные)

    assert запись["Number"] == НОМЕР_ДОКУМЕНТА
    assert запись["Date"] == ДАТА_ДОКУМЕНТА
    assert запись["СуммаДокумента"] == 145200.5
    assert запись["КлиентКонтрагент"]["КодПоОКПО"] == ОКПО


async def test_expand_глубже_одного_уровня_маскируется_на_каждом_шаге(сервис, любой_get):
    """`$expand=Контрагент/ГоловнойКонтрагент`: переключение политики рекурсивное, а не на один
    уровень вниз."""
    любой_get(
        {
            "value": [
                _документ_с_раскрытием(
                    Контрагент={
                        "Description": "ООО Первый Уровень",
                        "ГоловнойКонтрагент": {"Description": "ООО Тайна-Инвест", "ИНН": ИНН},
                    }
                )
            ]
        }
    )

    текст = await сервис.query(
        SessionScope(), base="ut", entity=ДОКУМЕНТ, expand=["Контрагент/ГоловнойКонтрагент"]
    )
    вложенный = json.loads(текст)["items"][0]["Контрагент"]["ГоловнойКонтрагент"]

    assert "Тайна-Инвест" not in текст and "Первый Уровень" not in текст
    assert вложенный["Description"].startswith("[[org:")
    assert вложенный["ИНН"].startswith("[[inn:")


async def test_табличная_часть_маскируется_по_своей_сущности_сквозь_тулы(сервис, любой_get, дом):
    """Табличная часть приходит списком словарей и без `$expand` (проба P4) — индекс знает её
    дочерней сущностью, а не навигацией. Правило владельца написано на сущность табличной части,
    класс `doc` без единой цифры: поймать значение может только политика своей сущности."""
    сущность, поле = f"{ДОКУМЕНТ}_Товары", "СодержаниеУслуги"
    значение = "Услуга: доставка до Тверской"
    путь = policy_path(дом, "ut")
    политика = yaml.safe_load(путь.read_text(encoding="utf-8"))
    политика.setdefault("fields", {})[f"{сущность}.{поле}"] = "doc"
    путь.write_text(yaml.safe_dump(политика, allow_unicode=True), encoding="utf-8")
    любой_get({"value": [_документ_с_раскрытием(Товары=[{поле: значение, "Количество": 3}])]})

    текст = await сервис.query(SessionScope(), base="ut", entity=ДОКУМЕНТ)
    строка = json.loads(текст)["items"][0]["Товары"][0]

    assert значение not in текст
    assert строка[поле].startswith("[[doc:")
    assert строка["Количество"] == 3


async def test_представление_контактной_информации_остаётся_телефоном(сервис, любой_get):
    """Сторож на побочный эффект переключения политики (замечание ревью в ходе работы).

    `КонтактнаяИнформация` — дочерняя сущность, и резолвер теперь спускается в неё на КАЖДОМ
    запросе к контрагентам: табличные части приходят без `$expand`. Поле `Представление` носят
    сразу два разных смысла — название у контрагента верхнего уровня и телефон у строки
    контактной информации (об этом прямо предупреждает комментарий в `masking.py`), и если бы
    авто-разметка отнесла его к `org`, телефон получил бы токен организации, сжёг порядковый
    номер и лёг в словарь названий как имя юрлица. Проверено исполнением на настоящей политике,
    собранной реиндексом: `Представление` у табличной части класса не получает, значение остаётся
    телефоном. Тест закрепляет это на будущее."""
    телефон = "+7 916 123-45-67"
    любой_get(
        {
            "value": [
                {
                    "Ref_Key": ССЫЛКА,
                    "Description": "ООО Ромашка",
                    "КонтактнаяИнформация": [
                        {"Тип": "Телефон", "Представление": телефон, "НомерТелефона": телефон}
                    ],
                }
            ]
        }
    )

    текст = await сервис.query(SessionScope(), base="ut", entity="Catalog_Контрагенты")
    строка = json.loads(текст)["items"][0]["КонтактнаяИнформация"][0]

    assert телефон not in текст
    assert строка["Представление"].startswith("[[phone:")
    assert строка["Представление"] == строка["НомерТелефона"]


async def test_коллекция_внутри_коллекции_маскируется_по_дальней_сущности(сервис, любой_get, дом):
    """Два перехода подряд через списки: запись → табличная часть → навигация → справочник.
    Правило владельца стоит на САМОЙ ДАЛЬНЕЙ сущности, класс свободнотекстовый (`doc`): ни
    детектора, ни автомата названий — значение может поймать только политика цели последнего
    перехода.

    Форма взята из прогона ревьюера, где она дважды дала ложную тревогу: без правила владельца
    `Catalog_СерииНоменклатуры.Description` политикой не размечен вовсе (серия номенклатуры — не
    организация), и открытое название там законно. Правило владельца эти два случая разводит."""
    поле, значение = "Description", "Серия: партия из Твери"
    путь = policy_path(дом, "ut")
    политика = yaml.safe_load(путь.read_text(encoding="utf-8"))
    политика.setdefault("fields", {})[f"Catalog_СерииНоменклатуры.{поле}"] = "doc"
    путь.write_text(yaml.safe_dump(политика, allow_unicode=True), encoding="utf-8")
    любой_get(
        {"value": [_документ_с_раскрытием(Товары=[{"Серия": {поле: значение, "Код": "000123"}}])]}
    )

    текст = await сервис.query(SessionScope(), base="ut", entity=ДОКУМЕНТ)
    серия = json.loads(текст)["items"][0]["Товары"][0]["Серия"]

    assert значение not in текст
    assert серия[поле].startswith("[[doc:")
    assert серия["Код"] == "000123"  # инвариант 6 на дальнем конце цепочки


@pytest.fixture
async def сервис_identifiers(дом):
    """Служба на базе `ut` с уровнем гейта `identifiers` — названия и ФИО на этом уровне не
    защищаются, а ручное правило владельца обязано действовать."""
    from odata1c.config.loader import load_config

    путь = дом / "bases.yaml"
    данные = yaml.safe_load(путь.read_text(encoding="utf-8"))
    данные["bases"]["ut"]["gate"] = {"mode": "identifiers"}
    путь.write_text(yaml.safe_dump(данные, allow_unicode=True), encoding="utf-8")
    служба = ToolService(load_config(дом))
    yield служба
    await служба.aclose()


async def test_ручное_правило_владельца_в_раскрытом_объекте_работает_на_identifiers(
    сервис_identifiers, любой_get, дом
):
    """Проверка отдельным прогоном ревьюера: правило `fields: Catalog_ФизическиеЛица.<поле>: addr`
    игнорировалось и на уровне `identifiers`, где названия и ФИО не защищаются вовсе.

    Контейнер здесь намеренно `Контрагент` — имя ИЗ списка контейнеров организации, то есть
    случай, где наследование класса от контейнера срабатывает: на `identifiers` унаследованный
    `org` понижается до сканирования, и значение выходит открытым. Правило владельца объявляет
    полю класс `doc` — уровнем он не понижается, детектора у него нет, автомат названий на этом
    уровне выключен, — поэтому зелёный результат доказывает сразу две вещи: политика ищется по
    настоящей сущности раскрытого объекта И она главнее наследования от контейнера."""
    поле, значение = "Description", НАЗВАНИЕ_КЛИЕНТА
    путь = policy_path(дом, "ut")
    политика = yaml.safe_load(путь.read_text(encoding="utf-8"))
    политика.setdefault("fields", {})[f"Catalog_Контрагенты.{поле}"] = "doc"
    путь.write_text(yaml.safe_dump(политика, allow_unicode=True), encoding="utf-8")
    любой_get({"value": [_документ_с_раскрытием(Контрагент={поле: значение})]})

    текст = await _вызвать_с_expand(сервис_identifiers, "query", ["Контрагент"])
    запись = json.loads(текст)["items"][0]

    assert значение not in текст
    assert запись["Контрагент"][поле].startswith("[[doc:")
    # Уровень свою работу делает: ФИО на identifiers не защищается, и правка этого не меняет.
    assert запись["Курьер"]["Description"] == ФИО_КУРЬЕРА


@pytest.mark.parametrize("тул", ["query", "get", "raw_get"])
async def test_expand_на_скрытую_сущность_обрезается(сервис, любой_get, дом, тул):
    """C2: SPEC §6.9 (строка 1068) — «`hide: true` … `$expand` на неё обрезается».

    Отказ именно обрезанием, а не кодом `entity_hidden`: последний сообщил бы модели о
    существовании скрытой сущности, чего `_resolve_entity` сознательно избегает. Предупреждение
    обязано быть, но без имени сущности и без имени навигации."""
    маршрут = любой_get({"value": [_документ_с_раскрытием()]})
    _скрыть_сущность(дом, "Catalog_Контрагенты")

    текст = await _вызвать_с_expand(сервис, тул, ["Контрагент"])
    данные = json.loads(текст)

    assert "error" not in данные
    assert "$expand" not in маршрут.calls.last.request.url.params
    # Предупреждение ровно одно, хотя сработали оба рубежа — обрезка запроса и изъятие из ответа.
    assert [п for п in данные["warnings"] if п.startswith("expand_trimmed")] == [
        "expand_trimmed: часть связанных объектов недоступна по политике гейта"
    ]
    assert "Контрагент" not in " ".join(данные["warnings"])


@pytest.mark.parametrize("тул", ["query", "get", "raw_get"])
async def test_expand_обрезается_и_когда_скрыта_цель_второго_звена(сервис, любой_get, дом, тул):
    """Путь обрезается целиком, а не до последнего разрешённого звена: `А/Б` со скрытой `Б`
    нельзя спасти, оставив `А`, — 1С раскрыла бы `Б` следующим запросом модели, а предупреждение
    об обрезке уже прозвучало бы."""
    маршрут = любой_get({"value": [_документ_с_раскрытием()]})
    _скрыть_сущность(дом, "Catalog_Валюты")

    текст = await _вызвать_с_expand(
        сервис, тул, ["БанковскийСчетКонтрагента/ВалютаДенежныхСредств"]
    )
    данные = json.loads(текст)

    assert "error" not in данные
    assert "$expand" not in маршрут.calls.last.request.url.params
    assert any(п.startswith("expand_trimmed") for п in данные["warnings"])


@pytest.mark.parametrize("тул", ["query", "get", "raw_get"])
async def test_разрешённые_пути_раскрытия_остаются(сервис, любой_get, дом, тул):
    """Обратный сторож обрезке: скрыта одна сущность — прочие пути раскрытия уходят в 1С как
    были, иначе правка лечила бы обход запрета отключением `$expand` вообще."""
    маршрут = любой_get({"value": [_документ_с_раскрытием()]})
    _скрыть_сущность(дом, "Catalog_Валюты")

    текст = await _вызвать_с_expand(сервис, тул, ["Валюта", "КлиентКонтрагент"])

    assert "error" not in json.loads(текст)
    assert маршрут.calls.last.request.url.params["$expand"] == "КлиентКонтрагент"


@pytest.mark.parametrize("тул", ["query", "get", "raw_get"])
async def test_скрытая_табличная_часть_изымается_из_ответа(сервис, любой_get, дом, тул):
    """Второй рубеж к обрезке `$expand`: табличные части 1С отдаёт списками словарей САМА, без
    всякого `$expand` (проба P4), — запрет владельца на такую сущность обрезкой запроса не
    закрывается в принципе. Объект изымается целиком, а не маскируется: иначе модель увидела бы
    и состав записи, и сам факт её существования."""
    любой_get({"value": [_документ_с_раскрытием(Товары=[{"СодержаниеУслуги": "доставка"}])]})
    _скрыть_сущность(дом, f"{ДОКУМЕНТ}_Товары")

    текст = await _вызвать_с_expand(сервис, тул, [])
    данные = json.loads(текст)

    assert "error" not in данные
    assert "Товары" not in _запись_ответа(данные)
    assert any(п.startswith("expand_trimmed") for п in данные["warnings"])
    assert "Товары" not in " ".join(данные["warnings"])


async def test_raw_get_expand_глубже_лимита_отклоняется(сервис, любой_get):
    """`raw_get` копировал `$expand` дословно — лимит `limits.expand_depth`, который `query`/`get`
    соблюдают через построитель запроса, на нём не работал вовсе."""
    маршрут = любой_get({"value": []})

    текст = await сервис.raw_get(
        SessionScope(),
        base="ut",
        path=f"{ДОКУМЕНТ}(guid'{ССЫЛКА}')",
        query={"$expand": "Контрагент/ГоловнойКонтрагент/ГоловнойКонтрагент"},
    )

    assert json.loads(текст)["error"]["code"] == "params_invalid"
    assert not маршрут.called


async def test_raw_get_неизвестная_навигация_в_expand_обрезается_при_скрытых(
    сервис, любой_get, дом
):
    """Звено, которого индекс не знает, проверить на скрытость нечем — то же правило, что у
    `_цель_пути`: там, где владельцу есть что скрывать, неразрешённое звено не обслуживается."""
    маршрут = любой_get({"value": [_документ_с_раскрытием()]})
    _скрыть_сущность(дом, "Catalog_Валюты")

    данные = json.loads(
        await сервис.raw_get(
            SessionScope(),
            base="ut",
            path=f"{ДОКУМЕНТ}(guid'{ССЫЛКА}')",
            query={"$expand": "НеизвестнаяНавигация"},
        )
    )

    assert "error" not in данные
    assert "$expand" not in маршрут.calls.last.request.url.params
    assert any(п.startswith("expand_trimmed") for п in данные["warnings"])


async def test_raw_get_неизвестная_навигация_в_expand_остаётся_без_скрытых(сервис, любой_get):
    """Контрольный прогон к предыдущему: без правил `hide` `raw_get` остаётся аварийным входом —
    неразрешённое звено уходит в 1С как есть (устаревший индекс — его заявленный сценарий)."""
    маршрут = любой_get({"value": [_документ_с_раскрытием()]})

    данные = json.loads(
        await сервис.raw_get(
            SessionScope(),
            base="ut",
            path=f"{ДОКУМЕНТ}(guid'{ССЫЛКА}')",
            query={"$expand": "НеизвестнаяНавигация"},
        )
    )

    assert "error" not in данные
    assert маршрут.calls.last.request.url.params["$expand"] == "НеизвестнаяНавигация"


# ---------------------------------------------------------------------------------------------
# C3 раунда 2: неразрешённая цель раскрытия — строгий режим и предупреждение (Ruling 18)
# ---------------------------------------------------------------------------------------------

НАЗВАНИЕ_ВНЕ_ИНДЕКСА = "ООО Астра-Неизвестная"


@pytest.mark.parametrize(
    "связь",
    ["ВыдуманнаяСвязь", "ОсновнаяВалютаОбъект"],
    ids=["навигации-нет-в-индексе", "ключ-ответа-не-совпал-с-именем-навигации"],
)
async def test_raw_get_неразрешённая_связь_маскируется_строго(сервис, любой_get, связь):
    """C3: `raw_get` пропускает `$expand` в 1С, если у базы нет ни одного правила `hide` (а это
    состояние базы по умолчанию — `generate_policy` пишет `entities: {}`), и 1С отдаёт раскрытый
    объект под ключом, которого индекс не знает. Устаревший индекс — заявленный сценарий самого
    `raw_get`, так что оба условия обыденные.

    До правки такой объект обрабатывался по политике сущности верхнего уровня, строгий режим не
    включался, и ответ выглядел совершенно нормальным: название открытым текстом, `masked_fields`
    пуст, предупреждений ноль."""
    маршрут = любой_get({"Ref_Key": ССЫЛКА, связь: {"Description": НАЗВАНИЕ_ВНЕ_ИНДЕКСА}})

    текст = await сервис.raw_get(
        SessionScope(), base="ut", path="Catalog_Валюты", query={"$expand": связь}
    )
    данные = json.loads(текст)

    assert маршрут.calls.last.request.url.params["$expand"] == связь
    assert НАЗВАНИЕ_ВНЕ_ИНДЕКСА not in текст
    assert данные["item"][связь]["Description"].startswith("[[org:")
    assert "Description" in данные["masked_fields"]
    assert any(п.startswith("путь или связь не разрешены") for п in данные["warnings"])


async def test_предупреждение_вне_индекса_не_двоится(сервис, любой_get):
    """Путь вне индекса и вложенный объект под неразрешённым ключом — два разных источника одного
    и того же предупреждения. Текст у них общий намеренно (получателю незачем гадать, чем эти
    случаи отличаются), и повторы обязаны сниматься: иначе ответ на обычный `raw_get` по
    непроиндексированному набору получал бы его дважды."""
    любой_get(
        {"value": [{"Description": "ООО Партнёр", "Связь": {"Description": "ООО Вложенная"}}]}
    )

    данные = json.loads(await сервис.raw_get(SessionScope(), base="ut", path="Catalog_Партнеры"))
    вне_индекса = [п for п in данные["warnings"] if п.startswith("путь или связь")]

    assert "error" not in данные
    assert len(вне_индекса) == 1


def test_резолвер_молчит_о_цели_вне_индекса(сервис, дом):
    """Форма Б той же причины: навигация есть, но её цели в индексе нет (битая ссылка на тип —
    `unresolved_entity_sets`, о которых предупреждает сам реиндекс). Резолвер обязан отвечать
    «не знаю» и здесь: иначе обход ушёл бы в сущность, о которой политика молчит, без строгого
    режима — ровно то же, что в форме А."""
    from odata1c.index.reindex import index_path
    from odata1c.index.repository import IndexRepository

    class БезЦели:
        """Индекс, который знает документ, но не знает сущность-цель его навигации."""

        def __init__(self, настоящий):
            self._настоящий = настоящий

        def describe(self, имя):
            return None if имя == "Catalog_Контрагенты" else self._настоящий.describe(имя)

    репозиторий = IndexRepository(index_path(дом, "ut"))
    try:
        целый = сервис._навигации(репозиторий)
        битый = сервис._навигации(БезЦели(репозиторий))
        assert целый(ДОКУМЕНТ, "Контрагент") == "Catalog_Контрагенты"
        assert битый(ДОКУМЕНТ, "Контрагент") is None
    finally:
        репозиторий.close()


# ---------------------------------------------------------------------------------------------
# Ruling 28: describe_entity не называет скрытую сущность, но имя навигационного поля оставляет
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("формат", ["markdown", "json"])
async def test_describe_не_называет_скрытую_цель_навигации(сервис, дом, формат):
    """Предупреждение об обрезке нейтрально, но его нейтральность обесценивалась соседним тулом:
    `describe_entity` печатал `Контрагент → Catalog_Контрагенты` при `hide: true`, то есть
    сообщал модели, что такая сущность существует, — четырежды в одном ответе.

    Ruling 28: имя навигационного ПОЛЯ остаётся (это часть формы сущности, без него модель ходит
    вслепую и придумывает несуществующие связи), имя ЦЕЛИ убирается."""
    _скрыть_сущность(дом, "Catalog_Контрагенты")

    текст = await сервис.describe_entity(
        SessionScope(), base="ut", entity=ДОКУМЕНТ, response_format=формат
    )

    assert "Catalog_Контрагенты" not in текст
    assert "КлиентКонтрагент" in текст  # имя поля на месте
    assert "скрыта настройкой базы" in текст


async def test_describe_называет_видимые_цели(сервис):
    """Обратный сторож: без правил `hide` описание по-прежнему называет цели навигаций — иначе
    правка лечила бы разглашение отключением полезного."""
    текст = await сервис.describe_entity(SessionScope(), base="ut", entity=ДОКУМЕНТ)

    assert "КлиентКонтрагент → Catalog_Контрагенты" in текст
    assert "скрыта настройкой базы" not in текст


async def test_describe_не_называет_скрытую_дочернюю_сущность(сервис, дом):
    """Табличная часть — тоже имя сущности в ответе, и правило то же: скрытую не называем.
    Вычёркиваем целиком, а не заменяем заглушкой: в отличие от навигации, `$expand` по ней не
    строят, и знать о её существовании модели незачем.

    Имя поля `Товары` при этом остаётся — как и имя навигационного поля, оно часть формы самой
    сущности. А вот имя типа строки (`Collection(…Document_X_Товары_RowType)`) содержит скрытое
    имя целиком: тип строки модель не адресует, функционального смысла в нём нет, и оставить его
    значило бы отдать в колонке типа то, что вычеркнуто из списка дочерних объектов."""
    _скрыть_сущность(дом, f"{ДОКУМЕНТ}_Товары")

    текст = await сервис.describe_entity(SessionScope(), base="ut", entity=ДОКУМЕНТ)

    assert f"{ДОКУМЕНТ}_Товары" not in текст
    assert "| Товары |" in текст  # само поле на месте
    assert "скрыта настройкой базы" in текст


# ---------------------------------------------------------------------------------------------
# Итоговое ревью M1d, Important: bases() не объявляет умолчанием базу вне области видимости
# ---------------------------------------------------------------------------------------------


async def test_bases_не_объявляет_умолчанием_невидимую_базу(сервис):
    """Суженной сессии сообщалось `default: "ut"` — имя базы вне её области видимости, при этом
    любой вызов без явного `base` отвечал `base_unknown`. Умолчание, о котором объявлено, должно
    быть либо видимым сессии, либо не объявляться вовсе."""
    данные = json.loads(await сервис.bases(SessionScope(bases=("dev",))))

    assert [б["name"] for б in данные["bases"]] == ["dev"]
    assert данные["default"] is None
    assert "ut" not in json.dumps(данные, ensure_ascii=False)


async def test_bases_объявляет_умолчание_сессии(сервис):
    """Обратный сторож: видимое умолчание по-прежнему объявляется — и своё у сессии, и общее."""
    своё = json.loads(await сервис.bases(SessionScope(bases=("dev",), default="dev")))
    общее = json.loads(await сервис.bases(SessionScope()))

    assert своё["default"] == "dev"
    assert общее["default"] == "ut"
