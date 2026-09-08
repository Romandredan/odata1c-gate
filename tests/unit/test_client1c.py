"""Клиент 1С: формирование запроса, сеанс, семафор, перевод ошибок."""

import asyncio

import httpx
import pytest
import respx

from odata1c.client1c.client import Client1C
from odata1c.client1c.errors import OdataError
from odata1c.config.models import BaseConfig

URL = "http://localhost/ut/odata/standard.odata/"


def база(**kwargs) -> BaseConfig:
    return BaseConfig(name="ut", label="УТ", url=URL, user="u", password="p", **kwargs)


@respx.mock
async def test_запрос_идёт_с_форматом_json_и_basic_аутентификацией():
    route = respx.get(f"{URL}Catalog_Валюты").mock(
        return_value=httpx.Response(200, json={"value": [{"Code": "643"}]})
    )
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
    client = Client1C(база(ib_session=True))
    await client.get("Catalog_Валюты")
    await client.get("Catalog_Валюты")
    await client.close()

    заголовки = [call.request.headers.get("IBSession") for call in respx.calls]
    assert заголовки[0] == "start"
    assert заголовки[1] is None


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
