"""Сквозная проверка `odata1c mcp` через настоящий stdio-подпроцесс (план M1d, задача 6):
лаунчер сам поднимает демон, демон обращается к поддельной 1С (`fake_1c.py`) вместо боевой базы.
Живая 1С не нужна — заглушка отвечает ровно на то, что нужно этому пути: `$metadata` для
`odata1c reindex` и один справочник для `odata1c_query`/`odata1c_describe_entity`.

Ограничение времени — по частям (reindex, запуск лаунчера с холодным стартом демона), а не одним
`pytest.mark.timeout`: плагин `pytest-timeout` не входит в зависимости проекта, а
`filterwarnings = ["error"]` в pyproject.toml превращает предупреждение о незарегистрированной
метке в ошибку сбора теста. Суммарно пределы этого теста — 20 + 30 + 10 = 60 с (раунд правок 1,
находка 8: было 20 + 45 + 10 = 75 с, брифу «не дольше 60 с суммарно» не соответствовало); по
факту прогонов на этой машине холодный старт демона (включая переход через Планировщик заданий,
находка 9) укладывается в 4–9 с суммарно на весь тест — 30 с на блок лаунчера оставляют запас
на порядок, не только на бумаге.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time

from fake_1c import ИНН, НАЗВАНИЕ, запущенная
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from odata1c import cli
from odata1c.config.home import ensure_home
from odata1c.config.writer import ensure_gate_secret
from odata1c.daemon import is_listening
from odata1c.daemon import stop as daemon_stop

ПРЕДЕЛ_REINDEX_С = 20
ПРЕДЕЛ_ЛАУНЧЕРА_С = 30
ПРЕДЕЛ_ОСТАНОВКИ_С = 10


def _свободный_порт() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as соединение:
        соединение.bind(("127.0.0.1", 0))
        return соединение.getsockname()[1]


async def test_launcher_сквозь_stdio_видит_базу_и_маскирует_ответ(tmp_path):
    home = tmp_path / "home"
    ensure_home(home)
    ensure_gate_secret(home / "daemon.yaml")

    демон_порт = _свободный_порт()
    # Порт демона нужен и здесь (чтобы `odata1c reindex` и лаунчер сошлись на одном), и внутри
    # самого лаунчера — оба читают его из daemon.yaml, а не передают друг другу иначе (SPEC §2.1).
    with (home / "daemon.yaml").open("a", encoding="utf-8") as поток:
        поток.write(f"port: {демон_порт}\n")

    async with запущенная() as порт_1с:
        (home / "bases.yaml").write_text(
            "default: ut\n"
            "bases:\n"
            "  ut:\n"
            "    label: УТ, поддельная 1С (сквозной тест лаунчера)\n"
            f"    url: http://127.0.0.1:{порт_1с}/odata/standard.odata/\n"
            "    user: u\n"
            "    password: p\n"
            "    role: prod\n",
            encoding="utf-8",
        )

        код_реиндекса = await asyncio.wait_for(
            asyncio.to_thread(cli.main, ["--home", str(home), "reindex", "ut"]),
            timeout=ПРЕДЕЛ_REINDEX_С,
        )
        assert код_реиндекса == 0

        параметры = StdioServerParameters(
            command=sys.executable,
            args=["-m", "odata1c", "--home", str(home), "mcp"],
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        try:
            await asyncio.wait_for(_проверить_через_лаунчер(параметры), timeout=ПРЕДЕЛ_ЛАУНЧЕРА_С)
        finally:
            if is_listening(демон_порт):
                daemon_stop(home)
                предел = time.monotonic() + ПРЕДЕЛ_ОСТАНОВКИ_С
                while is_listening(демон_порт) and time.monotonic() < предел:
                    await asyncio.sleep(0.2)


async def _проверить_через_лаунчер(параметры: StdioServerParameters) -> None:
    async with (
        stdio_client(параметры) as (read, write),
        ClientSession(read, write) as клиент,
    ):
        await клиент.initialize()

        ответ_баз = await клиент.call_tool("odata1c_bases", {})
        assert ответ_баз.is_error is False
        данные_баз = json.loads(ответ_баз.content[0].text)
        assert "ut" in [база["name"] for база in данные_баз["bases"]]

        ответ_запроса = await клиент.call_tool(
            "odata1c_query", {"entity": "Catalog_Контрагенты", "base": "ut"}
        )
        assert ответ_запроса.is_error is False
        текст_запроса = ответ_запроса.content[0].text
        assert ИНН not in текст_запроса
        assert НАЗВАНИЕ not in текст_запроса
        assert "[[" in текст_запроса
        данные_запроса = json.loads(текст_запроса)
        assert set(данные_запроса["masked_fields"]) >= {"ИНН", "Description"}

        ответ_описания = await клиент.call_tool(
            "odata1c_describe_entity", {"entity": "Catalog_Контрагенты", "base": "ut"}
        )
        assert ответ_описания.is_error is False
        assert ответ_описания.content[0].text


async def test_launcher_поднимается_на_чистом_домашнем_каталоге_без_предварительного_init(
    tmp_path,
):
    """Раунд правок 1, находка 3а: команда подключения `claude mcp add odata1c -- uv run
    --directory <репозиторий> odata1c mcp` обязана работать на машине, где `odata1c init` ни
    разу не запускали (SPEC §2.1 п. 1 поручает создание домашнего каталога и файлов-шаблонов
    именно лаунчеру). До правки — `load_config` внутри `run_launcher` требовал непустой
    `gate_secret` в `daemon.yaml`, а секрет создавал только `cmd_init` или сам демон (который
    читает порт РАНЬШЕ, чем успевает подняться) — `[config_invalid] секрет гейта не найден`.

    Домашний каталог здесь ТОЛЬКО с портом в `daemon.yaml` (для изоляции от порта по умолчанию
    и от других тестов) — ни `gate_secret`, ни `bases.yaml` этот тест сознательно не создаёт,
    это и есть «чистая машина»."""
    home = tmp_path / "home"
    home.mkdir()
    порт = _свободный_порт()
    (home / "daemon.yaml").write_text(f"port: {порт}\n", encoding="utf-8")

    параметры = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odata1c", "--home", str(home), "mcp"],
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    try:
        await asyncio.wait_for(_дождаться_handshake(параметры), timeout=ПРЕДЕЛ_ЛАУНЧЕРА_С)
    finally:
        if is_listening(порт):
            daemon_stop(home)
            предел = time.monotonic() + ПРЕДЕЛ_ОСТАНОВКИ_С
            while is_listening(порт) and time.monotonic() < предел:
                await asyncio.sleep(0.2)

    # Лаунчер должен был досоздать то же, что и `odata1c init` (ensure_templates +
    # ensure_gate_secret) — не только подключиться, но и оставить домашний каталог в рабочем
    # состоянии для следующего запуска.
    assert "gate_secret" in (home / "daemon.yaml").read_text(encoding="utf-8")
    assert (home / "bases.yaml").exists()


async def _дождаться_handshake(параметры: StdioServerParameters) -> None:
    async with (
        stdio_client(параметры) as (read, write),
        ClientSession(read, write) as клиент,
    ):
        await клиент.initialize()
