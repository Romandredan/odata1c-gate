"""Сервис тулов чтения (план M1d, задача 4): `ToolService` собирает гейт, построение запроса,
клиент 1С и формирование ответа в готовые ответы MCP-тулов `bases`, `find_entity`,
`describe_entity`, `query`, `get`. Тестируется без сети (`respx`) и без MCP-транспорта.
"""

import json

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
