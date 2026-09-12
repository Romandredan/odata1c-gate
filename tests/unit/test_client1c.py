"""Клиент 1С: формирование запроса, сеанс, семафор, перевод ошибок."""

import asyncio
import json
import urllib.parse
from unittest import mock

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


# --- Б-3 (M-6 ревью 7): ранний проход у пишущих запросов ------------------------------------

_ЗНАЧЕНИЕ = "7707083893"
_ТОКЕН = "[[inn:ABCDEFGHJK]]"


class _Проход:
    """Ранний проход гейта по протоколу `client1c.client.Scrub`: вызов — по тексту ошибки,
    `load` — по телу успешного ответа вместе с разбором. Записывает, что ему дали."""

    def __init__(self) -> None:
        self.тексты: list[str] = []
        self.тела: list[str] = []

    def __call__(self, text: str) -> str:
        self.тексты.append(text)
        return text.replace(_ЗНАЧЕНИЕ, _ТОКЕН)

    def load(self, text: str):
        self.тела.append(text)
        return json.loads(text.replace(_ЗНАЧЕНИЕ, _ТОКЕН))


def _ошибка_с_эхом() -> dict:
    return {"odata.error": {"code": "-1", "message": {"lang": "ru", "value": f"ИНН {_ЗНАЧЕНИЕ}"}}}


@respx.mock
@pytest.mark.parametrize("метод", ["post", "patch"])
async def test_ответ_на_запись_проходит_ранний_проход(метод):
    """Тело успешного ответа на POST/PATCH — объект 1С целиком, с реальными значениями, и среди
    них то, что шлюз только что раскрыл. Разбирает его ранний проход (`scrub.load`), как у `get`:
    иначе ответ на запись шёл бы в модель мимо него (Б-3)."""
    respx.route(method=метод.upper(), url=f"{URL}Catalog_Контрагенты").mock(
        return_value=httpx.Response(200, json={"ИНН": _ЗНАЧЕНИЕ})
    )
    respx.get(URL).mock(return_value=httpx.Response(200, json={"value": []}))
    проход = _Проход()
    client = Client1C(база())

    ответ = await getattr(client, метод)("Catalog_Контрагенты", {"ИНН": _ЗНАЧЕНИЕ}, scrub=проход)
    await client.close()

    assert ответ == {"ИНН": _ТОКЕН}
    assert len(проход.тела) == 1


@respx.mock
@pytest.mark.parametrize("метод", ["post", "patch", "delete"])
async def test_ошибка_записи_проходит_ранний_проход(метод):
    """1С повторяет переданное значение в тексте ошибки (проба P7), а на записи переданное — это
    раскрытое шлюзом. Текст ошибки проходит ранний проход до `map_error`, как у `get`."""
    respx.route(method=метод.upper(), url=f"{URL}Catalog_Контрагенты").mock(
        return_value=httpx.Response(400, json=_ошибка_с_эхом())
    )
    проход = _Проход()
    client = Client1C(база())
    аргументы = () if метод == "delete" else ({"ИНН": _ЗНАЧЕНИЕ},)

    with pytest.raises(OdataError) as ошибка:
        await getattr(client, метод)("Catalog_Контрагенты", *аргументы, scrub=проход)
    await client.close()

    assert _ЗНАЧЕНИЕ not in ошибка.value.message
    assert _ТОКЕН in ошибка.value.message
    assert проход.тексты


@respx.mock
async def test_пустой_ответ_на_запись_не_разбирается():
    """1С отвечает на PATCH без тела (204) — разбирать нечего, и ранний проход не вызывается."""
    respx.patch(f"{URL}Catalog_Контрагенты").mock(return_value=httpx.Response(204))
    respx.get(URL).mock(return_value=httpx.Response(200, json={"value": []}))
    проход = _Проход()
    client = Client1C(база())

    ответ = await client.patch("Catalog_Контрагенты", {"ИНН": _ЗНАЧЕНИЕ}, scrub=проход)
    await client.close()

    assert ответ == {}
    assert проход.тела == []


@respx.mock
async def test_таймаут_одного_запроса_передаётся_в_httpx():
    """Таймаут одного запроса (план M1d, задача 2): `get(..., timeout=…)` доходит до httpx как
    таймаут конкретного запроса (`request.extensions["timeout"]`), а не только меняет сообщение —
    без этого база продолжала бы ждать `timeout_s` независимо от переданного значения."""
    route = respx.get(f"{URL}Catalog_Валюты").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    respx.get(URL).mock(return_value=httpx.Response(200, json={"value": []}))
    client = Client1C(база())
    await client.get("Catalog_Валюты", timeout=0.01)
    await client.close()

    assert route.calls.last.request.extensions["timeout"] == {
        "connect": 0.01,
        "read": 0.01,
        "write": 0.01,
        "pool": 0.01,
    }


@respx.mock
async def test_таймаут_одного_запроса_называет_фактическое_значение_в_ошибке():
    """Сообщение об ошибке `timeout` называет переданный таймаут запроса, а не `timeout_s` базы
    (60 с по умолчанию) — иначе подсказка «увеличьте timeout_s» была бы верна, а число в тексте
    ошибки нет: `odata1c_query` вызывает виртуальную таблицу с `virtual_timeout_s=180` при
    `timeout_s` базы 60."""
    respx.get(f"{URL}Catalog_Валюты").mock(side_effect=httpx.ReadTimeout("таймаут"))
    client = Client1C(база())
    with pytest.raises(OdataError) as ошибка:
        await client.get("Catalog_Валюты", timeout=0.01)
    await client.close()

    assert ошибка.value.code == "timeout"
    assert "0.01" in ошибка.value.message


# Раунд правок 1, задача 2 плана M1d (ревью Critical): QuerySpec.path — читаемый OData-текст,
# не экранированный под URL (odata_query.py собирает его дословно так, как проверяют тесты
# брифа). Экранирование — обязанность Client1C перед отправкой (_экранировать_путь в client.py).
# Символы `?`/`#`/`&`, попавшие в путь из значения литерала (Condition, строковый ключ), без
# экранирования httpx счёл бы началом query/query-разделителем и обрезал бы путь молча.
#
# Известный предел этой проверки: `_экранировать_путь` кодирует весь путь одним проходом и не
# отличает `(`/`)`/`,`/`=`/`'`, которые сам построитель вставил как разметку (вызов виртуальной
# таблицы, разделители аргументов, OData-кавычка), от тех же символов внутри значения литерала —
# отличить их можно только на этапе сборки (odata_query.py), а туда экранирование сознательно не
# перенесено: тесты брифа сверяют QuerySpec.path дословно, включая кириллицу и удвоенные
# кавычки, без URL-экранирования. Поэтому `(`/`)`/`,`/`=`/`'` внутри значения долетают до 1С
# буквально, и это вопрос к разбору строкового литерала парсером 1С, а не к обрезке URL — тесты
# ниже проверяют то, для чего экранирование действительно нужно (`?`/`#`/`&`/`%`/перевод строки
# не режут путь и не всплывают как отдельный параметр query), а не полную изоляцию значения.


@respx.mock
async def test_опасные_символы_в_литерале_пути_не_режут_путь():
    """`Condition` с `?`, `#`, `&`, `%` и переводом строки — путь доходит целиком: ни один из
    этих символов не стал query-разделителем (`params.keys() == {"$format"}`), хвост с
    `Period=…` цел после декодирования пути обратно. `)`/`,` в значении здесь тоже есть (для
    реалистичности содержимого), но сравнение через `unquote` не отличает их от таких же
    символов, которые сам построитель вставляет как разметку, — оно не доказывает, что они
    были закодированы (см. комментарий выше)."""
    условие = "a?b#c&d%e\ng),h"
    хвост_пути = f"Condition='{условие}',Period=datetime'2026-09-01T00:00:00')"
    путь = f"AccumulationRegister_X_Balance({хвост_пути}"
    route = respx.route(url__regex=r".*").mock(return_value=httpx.Response(200, json={"value": []}))
    client = Client1C(база())
    await client.get(путь)
    await client.close()

    запрос = route.calls[0].request
    # Ничего из значения литерала не осело в query — там только $format (add_format=True).
    assert set(запрос.url.params.keys()) == {"$format"}
    декодированный_путь = urllib.parse.unquote(str(запрос.url.path))
    assert декодированный_путь.endswith(хвост_пути)


@respx.mock
async def test_ключ_строка_со_слэшем_и_вопросом_не_режет_путь():
    """Составной ключ со строковым полем (`Recorder` регистра — `Edm.String`, не `Edm.Guid`,
    формат не проверяется `odata_literal`) может содержать `/` и `?` — путь всё равно доходит
    целиком, символы не превращаются в лишние сегменты или query."""
    путь = "AccumulationRegister_X(Recorder='a/b?c',Recorder_Type='StandardODATA.Document_Y')"
    route = respx.route(url__regex=r".*").mock(return_value=httpx.Response(200, json={"value": []}))
    client = Client1C(база())
    await client.get(путь)
    await client.close()

    запрос = route.calls[0].request
    assert set(запрос.url.params.keys()) == {"$format"}
    декодированный_путь = urllib.parse.unquote(str(запрос.url.path))
    assert декодированный_путь.endswith(
        "Recorder='a/b?c',Recorder_Type='StandardODATA.Document_Y')"
    )


@respx.mock
async def test_metadata_доходит_буквальной_строкой_не_процентами():
    """`$` — легальный символ пути (RFC 3986 sub-delim), а не только начало `$select`/`$filter`
    построителя запросов: `get_raw("$metadata", ...)` (index/reindex.py, cli.py) — единственный
    путь этого клиента без литералов виртуальной таблицы, и он обязан дойти как буквальная
    строка `$metadata`, а не `%24metadata`. Проверка через `raw_path` байт-в-байт, а не через
    `respx`-сопоставление маршрута: respx сверяет URL по нормализованной форме и не отличил бы
    `$metadata` от `%24metadata` как цель — раунд правок 1 подтвердил это отдельным прогоном
    (существующие тесты test_index_reindex.py остаются зелёными в обоих случаях)."""
    route = respx.get(f"{URL}$metadata").mock(return_value=httpx.Response(200, content=b"<edmx/>"))
    respx.get(URL).mock(return_value=httpx.Response(200, json={"value": []}))
    client = Client1C(база())
    await client.get_raw("$metadata", accept="application/xml", add_format=False)
    await client.close()

    assert route.calls[0].request.url.raw_path == b"/ut/odata/standard.odata/$metadata"


@respx.mock
async def test_тело_читается_как_utf8_вопреки_заголовку_кодировки():
    """Тело JSON по спецификации OData — UTF-8; объявленная в заголовке кодировка может ей
    противоречить (публикация 1С за IIS с настроенным `charset=windows-1251`). Если читать по
    заголовку, значение приходит кракозябрами: его не узнаёт ни набор раскрытого, ни маскировщик,
    ни страж — все три слоя ищут точные вхождения, — а получатель восстанавливает исходный текст
    одним `encode('cp1251').decode('utf-8')`. Найдено ревью N1 (M1d) на пути гейта, но свойство
    общее и старше его: решается чтением байтов, а не доверием заголовку."""
    тело = json.dumps({"value": [{"Description": "ООО «Ромашка»"}]}, ensure_ascii=False)
    respx.route(url__regex=r".*").mock(
        return_value=httpx.Response(
            200,
            content=тело.encode("utf-8"),
            headers={"Content-Type": "application/json; charset=windows-1251"},
        )
    )
    client = Client1C(база())
    ответ = await client.get("Catalog_Контрагенты")
    await client.close()

    assert ответ["value"][0]["Description"] == "ООО «Ромашка»"


@respx.mock
async def test_ошибка_читается_как_utf8_вопреки_заголовку_кодировки():
    """Тот же случай на пути ошибки: текст платформы доходит до `map_error` (и до обратной замены
    раскрытого) читаемым, а не в чужой кодировке."""
    сообщение = "Поле Контрагент не найдено"
    тело = json.dumps(
        {"odata.error": {"code": "6", "message": {"lang": "ru", "value": сообщение}}},
        ensure_ascii=False,
    )
    respx.route(url__regex=r".*").mock(
        return_value=httpx.Response(
            400,
            content=тело.encode("utf-8"),
            headers={"Content-Type": "application/json; charset=windows-1251"},
        )
    )
    client = Client1C(база())
    with pytest.raises(OdataError) as ошибка:
        await client.get("Catalog_Контрагенты")
    await client.close()

    assert сообщение in str(ошибка.value)


@respx.mock
async def test_пробел_в_отборе_уходит_как_percent20_а_не_плюсом():
    """1С не знает HTML-правила «`+` в query равен пробелу»: плюс доходит до платформы буквально
    и разбирается как операция. Проверено на живой базе (УТ 11, 8.3):
    `$filter=ИНН+eq+'5024093941'` → 500 «Операция не разрешена в предложении "ГДЕ"», тот же отбор
    с `%20` → 200 и данные. Отсюда `_собрать_запрос` вместо `params=` у httpx (httpx кодирует
    пробел плюсом). Сверка байт-в-байт по `raw_path`: respx нормализует URL и `+` от `%20` как
    цель маршрута не отличает — без этой проверки тест остался бы зелёным и с дефектом."""
    route = respx.route(url__regex=r".*").mock(return_value=httpx.Response(200, json={"value": []}))
    client = Client1C(база())
    await client.get("Catalog_Контрагенты", {"$filter": "ИНН eq '5024093941'", "$top": 3})
    await client.close()

    сырой = route.calls[0].request.url.raw_path
    assert b"%20eq%20" in сырой
    assert b"+" not in сырой


async def test_невалидный_url_даёт_odata_error_а_не_голое_исключение():
    """Если бы экранирование где-то обошли (или ослабили), httpx поднял бы `httpx.InvalidURL` —
    исключение, которое НЕ является подклассом `httpx.HTTPError` и без отдельного перехвата
    пролетело бы мимо `_request` голым исключением (глобальное ограничение плана: тул не должен
    ронять исключение без кода/подсказки). Проверяется перехват напрямую — подменой
    `self._client.request`, а не реальным `\\n` в пути: `_экранировать_путь` кодирует перевод
    строки в `%0A` раньше, чем путь доходит до httpx, так что естественным путём это исключение
    больше не воспроизвести."""
    client = Client1C(база())
    client._client.request = mock.AsyncMock(side_effect=httpx.InvalidURL("плохой путь"))
    with pytest.raises(OdataError) as ошибка:
        await client.get("Catalog_Валюты")
    # _release_session_claim снял признак начатого сеанса — close() не пойдёт в IBSession:finish
    # тем же (замоканным) .request(): вызов ограничится aclose(), который не затронут подменой.
    await client.close()

    assert ошибка.value.code == "odata_error"
    assert "плохой путь" in ошибка.value.message


def test_тело_ошибки_список_не_роняет_разбор():
    ошибка = map_error(400, "[1, 2, 3]")
    assert ошибка.code == "odata_error"
    assert "[1, 2, 3]" in ошибка.message


def test_тело_ошибки_число_не_роняет_разбор():
    ошибка = map_error(400, "42")
    assert ошибка.code == "odata_error"
    assert "42" in ошибка.message


def _ошибка_платформы(код: str, текст: str) -> str:
    import json

    return json.dumps(
        {"odata.error": {"code": код, "message": {"lang": "ru", "value": текст}}},
        ensure_ascii=False,
    )


def test_нет_объекта_по_ключу_отдельный_код():
    """Находка П6: 404 на объект по ключу, которого нет, приходил кодом `entity_unknown` с
    советом «вызовите odata1c_reindex» — модель запускала бы реиндекс 16,8 МБ метаданных впустую.
    Платформа различает случаи кодом `odata.error.code` (живая база: «9» — «Экземпляр сущности не
    найден», «8» — «Сущность '…' не найдена»), и это не зависит от языка текста."""
    ошибка = map_error(404, _ошибка_платформы("9", "Экземпляр сущности не найден"))

    assert ошибка.code == "object_not_found"
    assert "ключ" in ошибка.hint
    assert "reindex" not in ошибка.hint


@pytest.mark.parametrize(
    "тело",
    [
        pytest.param(_ошибка_платформы("8", "Сущность 'Catalog_Нет' не найдена"), id="код-8"),
        pytest.param(_ошибка_платформы("1", "не найдено"), id="прочий-код"),
        pytest.param(_ошибка_платформы("", "не найдено"), id="пустой-код"),
        pytest.param("Not found", id="не-json"),
    ],
)
def test_прочие_404_остаются_неизвестной_сущностью(тело):
    ошибка = map_error(404, тело)

    assert ошибка.code == "entity_unknown"
    assert "reindex" in ошибка.hint


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
