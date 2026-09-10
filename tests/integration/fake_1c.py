"""Поддельная 1С для сквозной проверки лаунчера (план M1d, задача 6): Starlette-приложение на
свободном порту с ровно двумя маршрутами, которых достаточно для одного реального прогона через
демон — `$metadata` для `odata1c reindex` и один справочник для `odata1c_query`. Всё остальное
отвечает 404 в формате `odata.error`, как настоящая 1С на неизвестном пути (SPEC §9,
`client1c/errors.py::map_error`). Basic-аутентификация не проверяется — `Client1C` шлёт её всегда,
но эта заглушка запросы не отклоняет.
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import socket
from collections.abc import AsyncIterator

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

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


async def _не_найдено(request: Request) -> Response:
    # Тот же формат odata.error, что разбирает client1c/errors.py::_текст_ошибки_платформы —
    # неизвестный путь настоящая 1С тоже отдаёт так, а не голым текстом.
    return JSONResponse(
        {"odata.error": {"code": "0", "message": {"lang": "ru-RU", "value": "не найдено"}}},
        status_code=404,
    )


def build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/odata/standard.odata/$metadata", _metadata),
            Route("/odata/standard.odata/Catalog_Контрагенты", _контрагенты),
            Route("/{rest:path}", _не_найдено),
        ]
    )


def свободный_порт() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as соединение:
        соединение.bind(("127.0.0.1", 0))
        return соединение.getsockname()[1]


@contextlib.asynccontextmanager
async def запущенная(port: int | None = None) -> AsyncIterator[int]:
    """Поднять поддельную 1С в фоновой задаче на свободном (или заданном) порту, отдать номер
    порта, остановить при выходе — тот же приём (`uvicorn.Server` + флаг `should_exit`), что
    `odata1c.daemon.serve()` использует для настоящего демона."""
    порт = port if port is not None else свободный_порт()
    настройки = uvicorn.Config(build_app(), host="127.0.0.1", port=порт, log_level="warning")
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
