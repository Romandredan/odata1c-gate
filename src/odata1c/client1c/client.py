"""Единственная точка обращения к OData-интерфейсу 1С (SPEC §9).

Семафор на базу общий для всех сессий, сеанс 1С (IBSession) переиспользуется, повторы — только
для сетевых ошибок и HTTP 503 и только для чтения.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx

from odata1c.client1c.errors import OdataError, map_error
from odata1c.config.models import BaseConfig

ПОВТОРЫ = 2
ПАУЗА_ПЕРЕД_ПОВТОРОМ_С = 0.5


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

    async def get(self, path: str, params: dict | None = None) -> dict:
        response = await self._request("GET", path, params=params, retry=True)
        return response.json()

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
        return response.json() if response.content else {}

    async def patch(self, path: str, json: dict) -> dict:
        response = await self._request("PATCH", path, json=json, retry=False)
        return response.json() if response.content else {}

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
    ) -> httpx.Response:
        params = dict(params or {})
        if add_format:
            params.setdefault("$format", "json")
        headers = dict(headers or {})
        if json is not None:
            headers["Content-Type"] = "application/json"

        async with self._semaphore:
            открывает_сеанс = await self._claim_session_start()
            if открывает_сеанс:
                headers["IBSession"] = "start"
            попытки = ПОВТОРЫ if retry else 1
            последняя: Exception | None = None
            for попытка in range(попытки):
                try:
                    response = await self._client.request(
                        method, path, params=params, json=json, headers=headers
                    )
                except httpx.TimeoutException as exc:
                    последняя = OdataError(
                        "timeout",
                        f"1С не ответила за {self._base.timeout_s} с",
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
                        raise map_error(response.status_code, response.text)
                    return response
                await asyncio.sleep(ПАУЗА_ПЕРЕД_ПОВТОРОМ_С)
            # Недостижимо: на последней попытке каждая ветка выше либо возвращает ответ
            # (успех), либо поднимает исключение (таймаут, сетевая ошибка, статус >= 400,
            # включая 503 — на последней попытке условие повтора уже ложно) — цикл не может
            # завершиться без return/raise. Оставлено ради статического анализа возвращаемого
            # типа и как страховка на случай будущей правки, которая эту гарантию нарушит.
            raise последняя or OdataError("odata_error", "запрос к 1С не удался")
