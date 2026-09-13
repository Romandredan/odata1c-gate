"""Поддельная 1С для сквозной проверки лаунчера (план M1d, задача 6): Starlette-приложение на
свободном порту с ровно двумя маршрутами, которых достаточно для одного реального прогона через
демон — `$metadata` для `odata1c reindex` и один справочник для `odata1c_query`. Всё остальное
отвечает 404 в формате `odata.error`, как настоящая 1С на неизвестном пути (SPEC §9,
`client1c/errors.py::map_error`). Basic-аутентификация не проверяется — `Client1C` шлёт её всегда,
но эта заглушка запросы не отклоняет.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import pathlib
import socket
import urllib.parse
from collections.abc import AsyncIterator

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

_ОБРАЗЦЫ = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "edmx"
EDMX_ФИКСТУРА = _ОБРАЗЦЫ / "ut-real.edmx"

# Контрагент, на котором end-to-end тест проверяет, что гейт действительно маскирует ответ:
# ИНН попадает под класс inn (regex `^инн$`), Description — под org (Catalog_Контрагенты в
# DEFAULT_NAMES_FOR, gate/field_rules.py) — оба класса включены уровнем identifiers+names,
# который стоит по умолчанию (config/models.py::GateSettings.mode).
REF_KEY = "11111111-1111-1111-1111-111111111111"
ИНН = "7707083893"
НАЗВАНИЕ = "ООО Ромашка"


async def _metadata(request: Request) -> Response:
    return Response(EDMX_ФИКСТУРА.read_bytes(), media_type="application/xml")


async def _контрагенты(request: Request) -> Response:
    тело = {
        "odata.metadata": "http://127.0.0.1/odata/standard.odata/$metadata#Catalog_Контрагенты",
        "value": [
            {
                "Ref_Key": REF_KEY,
                "DataVersion": "AAAAAAAAAAA=",
                "DeletionMark": False,
                "Description": НАЗВАНИЕ,
                "ИНН": ИНН,
            }
        ],
    }
    return JSONResponse(тело)


async def _физлица_эхо_отбора(request: Request) -> Response:
    """Ошибка разбора отбора, повторяющая выражение ЦЕЛИКОМ, вместе с литералом (задача N1 M1d).

    Отдельный маршрут на отдельной сущности: `Catalog_Контрагенты` остаётся прежним — на нём
    держатся остальные потребители заглушки. Форма сообщения взята из находки ревью; спорить о
    том, повторяет ли конкретная публикация литералы, эта проверка не должна — инвариант 1
    сформулирован как «никогда», и защита строится так, чтобы не зависеть от того,
    проговорится платформа или нет.
    """
    отбор = request.query_params.get("$filter", "")
    return JSONResponse(
        {
            "odata.error": {
                "code": "6",
                "message": {
                    "lang": "ru-RU",
                    "value": f"Ошибка при разборе выражения отбора: {отбор}",
                },
            }
        },
        status_code=400,
    )


async def _не_найдено(request: Request) -> Response:
    # Тот же формат odata.error, что разбирает client1c/errors.py::_ошибка_платформы —
    # неизвестный путь настоящая 1С тоже отдаёт так, а не голым текстом.
    return JSONResponse(
        {"odata.error": {"code": "0", "message": {"lang": "ru-RU", "value": "не найдено"}}},
        status_code=404,
    )


class ОбъектыЗаписи:
    """Объекты «1С» с состоянием для сквозной проверки записи (план M2, задача 9): GET по пути
    ключа (`Catalog_Контрагенты(guid'…')`) с соблюдением `$select`, PATCH сливает тело с объектом
    и растит `DataVersion` (P8: растёт на любой записи). `записи` — (метод, путь, тело) каждого
    пишущего запроса: тест сверяет, что дошло до 1С и сколько раз."""

    def __init__(self, объекты: dict[str, dict]) -> None:
        self.объекты = {путь: dict(тело) for путь, тело in объекты.items()}
        self.записи: list[tuple[str, str, dict]] = []
        self._счётчик = 1

    @staticmethod
    def версия(номер: int) -> str:
        return base64.b64encode(номер.to_bytes(8, "big")).decode()

    async def обработать(self, request: Request) -> Response:
        # Метод, а не `__call__`: экземпляр с `__call__` Starlette считает ASGI-приложением.
        путь = request.path_params["rest"]
        объект = self.объекты.get(путь)
        if объект is None:
            return await _не_найдено(request)
        if request.method == "PATCH":
            тело = json.loads(await request.body())
            self.записи.append(("PATCH", путь, тело))
            объект.update(тело)
            self._счётчик += 1
            объект["DataVersion"] = self.версия(self._счётчик)
            return JSONResponse(объект)
        if request.method != "GET":
            self.записи.append((request.method, путь, {}))
            return await _не_найдено(request)
        выбор = request.query_params.get("$select")
        поля = set(выбор.split(",")) if выбор else set(объект)
        return JSONResponse({поле: значение for поле, значение in объект.items() if поле in поля})


def build_app(
    журнал_запросов: list[str] | None = None, *, объекты: ОбъектыЗаписи | None = None
) -> ASGIApp:
    """`журнал_запросов` — список, в который дописывается путь и строка запроса КАЖДОГО
    обращения, уже раскодированные из процентной записи. Нужен тем, кто проверяет, что именно
    дошло до «1С» (раунд правок 2 по `stop()` и журналу): раскрыл ли гейт токен в настоящее
    значение, было ли обращение вообще.

    `объекты` — объекты с состоянием для записи (задача 9): их маршрут стоит перед общим «не
    найдено» и прежних маршрутов не касается."""
    маршруты = [
        Route("/odata/standard.odata/$metadata", _metadata),
        Route("/odata/standard.odata/Catalog_Контрагенты", _контрагенты),
        Route("/odata/standard.odata/Catalog_ФизическиеЛица", _физлица_эхо_отбора),
    ]
    if объекты is not None:
        маршруты.append(
            Route(
                "/odata/standard.odata/{rest:path}",
                объекты.обработать,
                methods=["GET", "PATCH", "POST", "PUT", "DELETE"],
            )
        )
    маршруты.append(Route("/{rest:path}", _не_найдено))
    приложение = Starlette(routes=маршруты)
    if журнал_запросов is None:
        return приложение

    async def записывающее(scope, receive, send) -> None:
        if scope["type"] == "http":
            строка = scope.get("query_string", b"").decode("latin-1")
            журнал_запросов.append(urllib.parse.unquote(f"{scope['path']}?{строка}"))
        await приложение(scope, receive, send)

    return записывающее


def свободный_порт() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as соединение:
        соединение.bind(("127.0.0.1", 0))
        return соединение.getsockname()[1]


@contextlib.asynccontextmanager
async def запущенная(
    port: int | None = None,
    *,
    журнал_запросов: list[str] | None = None,
    объекты: ОбъектыЗаписи | None = None,
) -> AsyncIterator[int]:
    """Поднять поддельную 1С в фоновой задаче на свободном (или заданном) порту, отдать номер
    порта, остановить при выходе — тот же приём (`uvicorn.Server` + флаг `should_exit`), что
    `odata1c.daemon.serve()` использует для настоящего демона."""
    порт = port if port is not None else свободный_порт()
    настройки = uvicorn.Config(
        build_app(журнал_запросов, объекты=объекты),
        host="127.0.0.1",
        port=порт,
        log_level="warning",
    )
    сервер = uvicorn.Server(настройки)
    задача = asyncio.create_task(сервер.serve())
    try:
        while not сервер.started:
            if задача.done():
                await задача
            await asyncio.sleep(0.05)
        yield порт
    finally:
        сервер.should_exit = True
        await задача
