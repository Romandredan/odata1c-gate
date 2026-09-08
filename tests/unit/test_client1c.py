"""Клиент 1С: формирование запроса, сеанс, семафор, перевод ошибок."""

import asyncio

import httpx
import pytest
import respx

from odata1c.client1c.client import Client1C
from odata1c.client1c.errors import OdataError, map_error
from odata1c.config.models import BaseConfig

URL = "http://localhost/ut/odata/standard.odata/"


def база(**kwargs) -> BaseConfig:
    return BaseConfig(name="ut", label="УТ", url=URL, user="u", password="p", **kwargs)


@respx.mock
async def test_запрос_идёт_с_форматом_json_и_basic_аутентификацией():
    route = respx.get(f"{URL}Catalog_Валюты").mock(
        return_value=httpx.Response(200, json={"value": [{"Code": "643"}]})
    )
    # завершение сеанса при close() пойдёт на этот же адрес без хвоста пути
    respx.get(URL).mock(return_value=httpx.Response(200, json={"value": []}))
    client = Client1C(база())
    результат = await client.get("Catalog_Валюты", {"$top": 1})
    await client.close()

    assert результат == {"value": [{"Code": "643"}]}
    запрос = route.calls.last.request
    assert запрос.url.params["$format"] == "json"
    assert запрос.url.params["$top"] == "1"
    assert запрос.headers["Authorization"].startswith("Basic ")
    assert запрос.headers["Accept"] == "application/json"


@respx.mock
async def test_сеанс_запрашивается_один_раз():
    respx.get(f"{URL}Catalog_Валюты").mock(return_value=httpx.Response(200, json={"value": []}))
    respx.get(URL).mock(return_value=httpx.Response(200, json={"value": []}))
    client = Client1C(база(ib_session=True))
    await client.get("Catalog_Валюты")
    await client.get("Catalog_Валюты")
    await client.close()

    заголовки = [call.request.headers.get("IBSession") for call in respx.calls]
    assert заголовки[0] == "start"
    assert заголовки[1] is None


@respx.mock
async def test_сеанс_открывается_только_одной_из_параллельных_задач():
    """Гонка: без блокировки два первых параллельных запроса оба видят «сеанс не начат».

    Мгновенный ответ-заглушка гонку не покажет — окно между чтением признака и его
    установкой открывается сетевым запросом. Задержка в обработчике, как и в тесте на
    семафор, обязательна: без неё тест проходит и на сломанном коде.
    """
    respx.get(URL).mock(return_value=httpx.Response(200, json={"value": []}))

    async def медленный(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"value": []})

    respx.get(f"{URL}Catalog_Валюты").mock(side_effect=медленный)
    client = Client1C(база(ib_session=True, concurrency=4))
    await asyncio.gather(*(client.get("Catalog_Валюты") for _ in range(4)))
    await client.close()

    старты = [
        вызов.request.headers.get("IBSession")
        for вызов in respx.calls
        if вызов.request.headers.get("IBSession") == "start"
    ]
    assert len(старты) == 1


@respx.mock
async def test_семафор_ограничивает_одновременные_запросы():
    одновременно, пик = 0, 0

    async def медленный(request):
        nonlocal одновременно, пик
        одновременно += 1
        пик = max(пик, одновременно)
        await asyncio.sleep(0.05)
        одновременно -= 1
        return httpx.Response(200, json={"value": []})

    respx.get(f"{URL}Catalog_Валюты").mock(side_effect=медленный)
    respx.get(URL).mock(return_value=httpx.Response(200, json={"value": []}))
    client = Client1C(база(concurrency=2))
    await asyncio.gather(*(client.get("Catalog_Валюты") for _ in range(6)))
    await client.close()

    assert пик <= 2


@respx.mock
async def test_ошибка_аутентификации():
    respx.get(f"{URL}Catalog_Валюты").mock(return_value=httpx.Response(401, text="Unauthorized"))
    client = Client1C(база())
    with pytest.raises(OdataError) as ошибка:
        await client.get("Catalog_Валюты")
    await client.close()
    assert ошибка.value.code == "auth_failed"


@respx.mock
async def test_ошибка_платформы_передаётся_текстом():
    тело = {"odata.error": {"message": {"value": "Поле объекта не обнаружено (ИНН)"}}}
    respx.get(f"{URL}Catalog_Контрагенты").mock(return_value=httpx.Response(400, json=тело))
    client = Client1C(база())
    with pytest.raises(OdataError) as ошибка:
        await client.get("Catalog_Контрагенты")
    await client.close()
    assert ошибка.value.code == "odata_error"
    assert "Поле объекта не обнаружено" in ошибка.value.message


@respx.mock
async def test_неизвестная_сущность_отдаёт_свой_код():
    respx.get(f"{URL}Catalog_Нет").mock(return_value=httpx.Response(404, text="Not found"))
    client = Client1C(база())
    with pytest.raises(OdataError) as ошибка:
        await client.get("Catalog_Нет")
    await client.close()
    assert ошибка.value.code == "entity_unknown"
    assert "reindex" in ошибка.value.hint


@respx.mock
async def test_повтор_только_для_503():
    route = respx.get(f"{URL}Catalog_Валюты").mock(
        side_effect=[httpx.Response(503, text="busy"), httpx.Response(200, json={"value": []})]
    )
    respx.get(URL).mock(return_value=httpx.Response(200, json={"value": []}))
    client = Client1C(база())
    assert await client.get("Catalog_Валюты") == {"value": []}
    await client.close()
    assert route.call_count == 2


@respx.mock
async def test_запись_не_повторяется():
    route = respx.post(f"{URL}Catalog_Валюты").mock(return_value=httpx.Response(503, text="busy"))
    client = Client1C(база())
    with pytest.raises(OdataError):
        await client.post("Catalog_Валюты", {"Code": "643"})
    await client.close()
    assert route.call_count == 1


def test_тело_ошибки_список_не_роняет_разбор():
    ошибка = map_error(400, "[1, 2, 3]")
    assert ошибка.code == "odata_error"
    assert "[1, 2, 3]" in ошибка.message


def test_тело_ошибки_число_не_роняет_разбор():
    ошибка = map_error(400, "42")
    assert ошибка.code == "odata_error"
    assert "42" in ошибка.message


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_несуществующий_файл_сертификата_даёт_понятную_ошибку(tmp_path):
    """Регресс: verify_tls как путь к несуществующему CA-сертификату роняет конструктор
    Client1C необработанным FileNotFoundError (httpx строит ssl-контекст синхронно, ещё до
    первого запроса) вместо ошибки с кодом и подсказкой, которую понимает командная строка.

    filterwarnings здесь не про сам дефект: httpx отдельно и заранее предупреждает
    (DeprecationWarning), что verify=<строка> устарел как API — это самостоятельный, не
    связанный с этой правкой долг миграции на verify=ssl.SSLContext(...), а в тестах этого
    проекта предупреждения превращены в ошибки (filterwarnings = ["error"]). Глушим здесь
    только его, чтобы дойти до проверяемого поведения — файла нет, ошибка понятная.
    """
    путь = tmp_path / "нет_такого.pem"
    with pytest.raises(OdataError) as ошибка:
        Client1C(база(verify_tls=str(путь)))
    assert ошибка.value.code == "odata_error"
    assert str(путь) in ошибка.value.message
