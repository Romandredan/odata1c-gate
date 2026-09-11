"""Сервис тулов чтения (план M1d, задача 4): `ToolService` собирает гейт, построение запроса,
клиент 1С и формирование ответа в готовые ответы MCP-тулов `bases`, `find_entity`,
`describe_entity`, `query`, `get`. Тестируется без сети (`respx`) и без MCP-транспорта.
"""

import json
import sys

import httpx
import pytest
import respx

from odata1c.cli import main
from odata1c.gate.service import refresh_policy
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
    результат = гейт.mask({"ИНН": инн}, entity=entity)
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
    результат = гейт.mask({"Description": название}, entity="Catalog_Контрагенты")
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
