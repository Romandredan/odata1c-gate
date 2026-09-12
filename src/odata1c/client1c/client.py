"""Единственная точка обращения к OData-интерфейсу 1С (SPEC §9).

Семафор на базу общий для всех сессий, сеанс 1С (IBSession) переиспользуется, повторы — только
для сетевых ошибок и HTTP 503 и только для чтения.
"""

from __future__ import annotations

import asyncio
import contextlib
import json as json_mod
import urllib.parse
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from odata1c.client1c.errors import OdataError, map_error
from odata1c.config.models import BaseConfig


class Scrub(Protocol):
    """Ранний проход гейта по сырому ответу (Ruling 25): вызов — по тексту ошибки, `load` — по
    телу успешного ответа вместе с разбором JSON (Ruling 32). Реализация — `gate.pipeline.Scrubber`;
    клиент о гейте ничего не знает."""

    def __call__(self, text: str) -> str: ...

    def load(self, text: str) -> Any: ...


ПОВТОРЫ = 2
ПАУЗА_ПЕРЕД_ПОВТОРОМ_С = 0.5

# Раунд правок 1, задача 2 плана M1d (ревью Critical): QuerySpec.path (odata_query.py) собирает
# путь как читаемый OData-текст — литералы из odata_literal() с одинарными кавычками, аргументы
# через «,» и «=», виртуальная таблица через «/» и скобки — и не экранирует его: строка идёт в
# httpx как есть. Символы `?`/`#`/`&`, попавшие туда из значения (`Condition`, строковый ключ),
# httpx поймёт как начало query/fragment и обрежет путь молча; управляющие символы (перевод
# строки) роняют httpx.InvalidURL. Экранирование — здесь, в один проход по всему пути
# (`urllib.parse.quote`), а не в odata_query.py: там путь остаётся читаемым текстом, который
# сверяют тесты дословно из брифа (например, кириллица и удвоенные кавычки в ожидаемой строке
# QuerySpec.path). `safe` оставляет буквальными только то, что сам построитель использует как
# разметку пути: `/` — разделитель сегментов (в т.ч. `<регистр>/<действие>`), `(` `)` — вызов
# виртуальной таблицы/ключа, `,` — разделитель аргументов, `=` — `Имя=литерал`, `'` —
# OData-кавычка строкового литерала. Всё остальное содержимое значения — включая кириллицу в
# именах наборов и полей, `?`, `#`, `&`, `%`, пробелы и переводы строк — кодируется процентами;
# httpx не перекодирует уже закодированные `%XX` повторно (проверено пробой на respx —
# `request.url.raw_path` содержит ровно то, что здесь построено). `$` — тоже в safe: единственный
# путь этого клиента без параметров виртуальной таблицы — литеральная строка `$metadata`
# (reindex.py, cli.py); `$` — легальный sub-delim по RFC 3986, кодировать его незачем, а без
# явного `$` в safe запрос ушёл бы как `%24metadata` — прогон тестов реиндекса это не поймал бы:
# respx сверяет URL по нормализованной форме и не различает `$metadata` и `%24metadata` как цели
# маршрута (проверено отдельно, раунд правок 1: без `$` в safe тесты реиндекса остаются зелёными).
_БЕЗОПАСНЫЕ_СИМВОЛЫ_ПУТИ = "/():,='$"


def _экранировать_путь(path: str) -> str:
    return urllib.parse.quote(path, safe=_БЕЗОПАСНЫЕ_СИМВОЛЫ_ПУТИ)


def _тело_текстом(response: httpx.Response) -> str:
    """Тело ответа как текст, прочитанный из байтов по правилам OData, а не по заголовку.

    JSON в OData — UTF-8 по спецификации, но публикация 1С за IIS может объявить в
    `Content-Type` другую кодировку (`charset=windows-1251`). `response.text` доверяет заголовку,
    и тогда кириллица приходит кракозябрами: их не узнаёт ни один слой защиты — набор раскрытого,
    маскировщик и страж ищут точные вхождения, — а получатель восстанавливает исходный текст
    одним `encode('cp1251').decode('utf-8')`. То есть неверный заголовок кодировки обходит гейт
    целиком. Найдено ревью N1 (M1d); свойство не этого раунда, а общее и давнее.

    `errors="replace"` — на случай тела, которое и правда не UTF-8: получить испорченные символы
    лучше, чем уронить разбор; невалидные байты становятся U+FFFD и точным вхождением уже не
    притворяются.
    """
    return response.content.decode("utf-8", errors="replace")


def _собрать_запрос(params: dict) -> str:
    """Строка параметров запроса, где пробел закодирован `%20`, а не `+`.

    httpx (как и `urlencode` по умолчанию) кодирует пробел плюсом — форма из HTML, где `+`
    в query равен пробелу. 1С этого правила не знает: `+` в `$filter` доходит до платформы
    буквально и разбирается как операция. Проверено на живой базе (УТ 11, платформа 8.3):
    `$filter=ИНН+eq+'5024093941'` → 500 «Операция не разрешена в предложении "ГДЕ"»,
    тот же отбор с `%20` → 200. Поэтому query собирается здесь, а не передаётся в httpx
    аргументом `params`: готовую процентную запись httpx сохраняет как есть.

    Значения по контракту `QuerySpec.params` — строки (`dict[str, str]`); `doseq=True` оставлен
    для совпадения с прежним поведением httpx на случай, если когда-нибудь придёт список:
    без него в query уехал бы `repr` списка, а не повторённый ключ.
    """
    return urllib.parse.urlencode(params, doseq=True, quote_via=urllib.parse.quote, safe="")


class Client1C:
    def __init__(self, base: BaseConfig) -> None:
        self._base = base
        self._semaphore = asyncio.Semaphore(base.concurrency)
        self._session_started = False
        self._session_lock = asyncio.Lock()
        try:
            self._client = httpx.AsyncClient(
                base_url=base.url,
                auth=(base.user, base.password),
                verify=base.verify_tls,
                timeout=base.timeout_s,
                headers={"Accept": "application/json"},
            )
        except OSError as exc:
            # verify_tls как путь к CA-сертификату (PEM): httpx строит ssl-контекст уже здесь,
            # синхронно, и при отсутствующем файле роняет OSError (обычно FileNotFoundError)
            # прямо из конструктора — до первого запроса и до входа в асинхронный код.
            raise OdataError(
                "odata_error",
                f"файл сертификата не найден: {base.verify_tls}",
                f"проверьте путь verify_tls в настройках базы {base.name}",
            ) from exc

    async def get(
        self,
        path: str,
        params: dict | None = None,
        *,
        timeout: float | None = None,
        scrub: Scrub | None = None,
    ) -> dict:
        """Таймаут одного запроса (SPEC §10): по умолчанию — таймаут базы (`timeout_s`,
        конструктор `httpx.AsyncClient`), явный `timeout=` (план M1d: виртуальные таблицы —
        `virtual_timeout_s`) переопределяет его только для этого запроса.

        `scrub` — ранний проход гейта, возвращающий на место раскрытых значений их токены
        (Ruling 25, задача N1 M1d). Он применяется к СЫРОМУ телу ответа — и успешного, и
        ошибочного — до разбора JSON и до `map_error`: инвариант 1 требует, чтобы раскрытое гейтом
        значение не вышло наружу, а точное вхождение, на котором держится обратная замена, рвут все
        преобразования ниже по конвейеру (маскировка, усечение строк, обрезка неразобранного
        тела до 500 знаков в `map_error`). Тело успешного ответа проход сам и разбирает
        (`scrub.load`, Ruling 32): разбор помечает переписанные строки их исходным значением. Клиент
        о гейте ничего не знает — только вызывает переданное; без него поведение прежнее."""
        response = await self._request(
            "GET", path, params=params, retry=True, timeout=timeout, scrub=scrub
        )
        тело = _тело_текстом(response)
        return scrub.load(тело) if scrub is not None else json_mod.loads(тело)

    async def get_raw(
        self,
        path: str,
        params: dict | None = None,
        accept: str = "application/json",
        add_format: bool = True,
    ) -> bytes:
        """Сырой ответ. Для $metadata вызывается с add_format=False: $format=json там неуместен."""
        response = await self._request(
            "GET",
            path,
            params=params,
            retry=True,
            headers={"Accept": accept},
            add_format=add_format,
        )
        return response.content

    async def post(self, path: str, json: dict) -> dict:
        response = await self._request("POST", path, json=json, retry=False)
        return json_mod.loads(_тело_текстом(response)) if response.content else {}

    async def patch(self, path: str, json: dict) -> dict:
        response = await self._request("PATCH", path, json=json, retry=False)
        return json_mod.loads(_тело_текстом(response)) if response.content else {}

    async def delete(self, path: str) -> None:
        await self._request("DELETE", path, retry=False)

    async def close(self) -> None:
        if self._session_started and self._base.ib_session:
            # завершение сеанса — вежливость, а не обязанность; но только сетевой сбой,
            # не любая ошибка — программную опечатку такое подавление прятать не должно
            with contextlib.suppress(httpx.HTTPError):
                await self._client.get("", headers={"IBSession": "finish"})
        await self._client.aclose()

    async def _claim_session_start(self) -> bool:
        """Атомарно решает, эта ли задача пошлёт заголовок начала сеанса 1С.

        Отдельная короткая блокировка, а не семафор одновременности: у семафора другая
        задача — ограничивать число параллельных запросов к базе. Без этой блокировки
        между чтением признака «сеанс не начат» и его установкой лежит сам сетевой
        запрос — точка переключения задач, — и несколько первых параллельных запросов
        успевают увидеть «не начат» раньше, чем кто-то из них его выставит, и все шлют
        заголовок начала сеанса. Если запрос с этим заголовком в итоге не удался, право
        возвращается методом _release_session_claim, чтобы его мог получить следующий.
        """
        if not self._base.ib_session:
            return False
        async with self._session_lock:
            if self._session_started:
                return False
            self._session_started = True
            return True

    async def _release_session_claim(self) -> None:
        """Вернуть право открыть сеанс — запрос с заголовком начала не удался."""
        async with self._session_lock:
            self._session_started = False

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        retry: bool,
        headers: dict | None = None,
        add_format: bool = True,
        timeout: float | None = None,
        scrub: Callable[[str], str] | None = None,
    ) -> httpx.Response:
        путь = _экранировать_путь(path)
        params = dict(params or {})
        if add_format:
            params.setdefault("$format", "json")
        запрос = _собрать_запрос(params)
        цель = f"{путь}?{запрос}" if запрос else путь
        headers = dict(headers or {})
        if json is not None:
            headers["Content-Type"] = "application/json"
        # httpx: timeout=None в вызове request() значит «без таймаута», а не «умолчание клиента» —
        # аргумент передаётся, только когда вызывающий его явно указал (Client1C.get(timeout=…)).
        # Без этого таймаут одного запроса нельзя было бы вообще отключить именованием None,
        # но здесь такого сценария нет: неуказанный timeout должен использовать timeout_s базы.
        параметры_запроса: dict = {}
        if timeout is not None:
            параметры_запроса["timeout"] = timeout
        фактический_таймаут = timeout if timeout is not None else self._base.timeout_s

        async with self._semaphore:
            открывает_сеанс = await self._claim_session_start()
            if открывает_сеанс:
                headers["IBSession"] = "start"
            попытки = ПОВТОРЫ if retry else 1
            последняя: Exception | None = None
            for попытка in range(попытки):
                try:
                    response = await self._client.request(
                        method,
                        цель,
                        json=json,
                        headers=headers,
                        **параметры_запроса,
                    )
                except httpx.InvalidURL as exc:
                    # Экранирование выше (_экранировать_путь) не оставляет для httpx поводов
                    # счесть путь невалидным — но это единственная защита от голого исключения,
                    # если экранирование когда-нибудь ослабят или обойдут: не сетевая ошибка,
                    # повтор не поможет, поэтому без цикла ретраев.
                    if открывает_сеанс:
                        await self._release_session_claim()
                    raise OdataError(
                        "odata_error",
                        f"не удалось сформировать запрос к 1С: {exc}",
                        "путь запроса содержит символы, которые 1С не примет — сузьте значение",
                    ) from exc
                except httpx.TimeoutException as exc:
                    последняя = OdataError(
                        "timeout",
                        f"1С не ответила за {фактический_таймаут} с",
                        "увеличьте timeout_s базы или сузьте выборку",
                    )
                    if попытка + 1 == попытки:
                        if открывает_сеанс:
                            await self._release_session_claim()
                        raise последняя from exc
                except httpx.HTTPError as exc:
                    последняя = OdataError(
                        "odata_error",
                        f"не удалось обратиться к 1С: {exc}",
                        "проверьте адрес базы и доступность сервера",
                    )
                    if попытка + 1 == попытки:
                        if открывает_сеанс:
                            await self._release_session_claim()
                        raise последняя from exc
                else:
                    if response.status_code == 503 and попытка + 1 < попытки:
                        await asyncio.sleep(ПАУЗА_ПЕРЕД_ПОВТОРОМ_С)
                        continue
                    if response.status_code >= 400:
                        if открывает_сеанс:
                            await self._release_session_claim()
                        тело = _тело_текстом(response)
                        # Обратная замена раскрытого — ДО map_error (Ruling 25): он берёт
                        # `body.strip()[:500]`, когда тело не разбирается как odata.error
                        # (страница веб-сервера, XML-ошибка), и обрезает значение посередине.
                        raise map_error(
                            response.status_code, scrub(тело) if scrub is not None else тело
                        )
                    return response
                await asyncio.sleep(ПАУЗА_ПЕРЕД_ПОВТОРОМ_С)
            # Недостижимо: на последней попытке каждая ветка выше либо возвращает ответ
            # (успех), либо поднимает исключение (таймаут, сетевая ошибка, статус >= 400,
            # включая 503 — на последней попытке условие повтора уже ложно) — цикл не может
            # завершиться без return/raise. Оставлено ради статического анализа возвращаемого
            # типа и как страховка на случай будущей правки, которая эту гарантию нарушит.
            raise последняя or OdataError("odata_error", "запрос к 1С не удался")
