"""Лаунчер `odata1c mcp` (SPEC §2.2, план M1d задача 6, раунд правок 1): тонкий stdio-процесс без
собственной логики. Снаружи (к клиенту — Claude Code) — stdio, внутрь (к демону) — Streamable
HTTP; лаунчер сам поднимает демон, если его порт не слушается, и пересылает семь обработчиков
lowlevel `Server` вызовами апстрим-сессии как есть, не трогая содержимое.

Область видимости сессии — не аргумент MCP-протокола, а заголовки HTTP на каждый запрос к демону
(`SCOPE_BASES_HEADER`/`SCOPE_DEFAULT_HEADER`, `daemon.py`, поправка SPEC §2.1 задачи 5): `--bases`
и `--default` этой команды превращаются в них один раз при построении HTTP-клиента лаунчера,
а не при каждом вызове тула.

Elicitation (запрос демона к пользователю — протокол записи, SPEC §7) идёт в обратную сторону:
демон спрашивает апстрим-сессию лаунчера, а переслать его нужно вниз, downstream-клиенту
(настоящему пользователю). Единственная downstream-сессия лаунчера сохраняется в `ProxyHolder`
каждым из семи обработчиков при входе (`ctx.session`) — какой из них отработает первым, не важно:
за один stdio-процесс лаунчера downstream-сессия всегда одна и та же.

Клиент лаунчера (`clientInfo` и elicitation) демону нужен для выбора механизма подтверждения
записи (ADR-0012, план M2 задача 9), а `initialize` демону шлёт сам лаунчер — раньше, чем
подключится клиент, и со своим именем. Поэтому первый же обработчик передаёт клиента демону
заголовками HTTP (`ProxyHolder.запомнить`, `daemon.client_headers`), как область видимости.
Эти заголовки лаунчер подписывает ключом домашнего каталога (`launcher.key`, Ruling 59) на каждом
запросе, у которого уже есть `mcp-session-id` (`client_signer`): демон выдаёт механизм «подтверждает
сам клиент» только подписанному клиенту, а не любому процессу, назвавшемуся Claude Code.

Обрыв связи с демоном посреди сессии (раунд правок 1, находка 1) не должен ронять весь процесс
голым traceback и не должен вешать вызов клиента навсегда: каждый из семи обработчиков перехватывает
исключения апстрим-вызова и отдаёт клиенту штатный отказ — `CallToolResult(is_error=True, ...)` для
`tools/call` (SPEC §5.2: ошибка тула — текст, не исключение), `MCPError` для остальных операций
(SDK сам сериализует его в JSON-RPC-ошибку — `mcp/server/runner.py`, `raise_exceptions=False`).
Обычного `try/except` вокруг апстрим-вызова для этого недостаточно: SDK обнаруживает реальный
обрыв TCP-соединения в СОБСТВЕННОЙ фоновой задаче (`mcp/client/streamable_http.py`), а не в задаче,
которая ждёт ответа, — поэтому вызов висит бесконечно, а исключение всплывает только при закрытии
всей сессии. Сторожок `_с_проверкой_живости` гоняет апстрим-вызов наперегонки с периодической
TCP-проверкой порта демона и отменяет зависший вызов, если порт перестал отвечать — подробности
в докстринге самой функции.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import sys
import time
import urllib.parse

import anyio
import httpx2
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from odata1c.config.home import ensure_home
from odata1c.config.loader import load_config
from odata1c.config.writer import ensure_gate_secret, ensure_launcher_key, ensure_templates
from odata1c.daemon import (
    CLIENT_ELICITATION_HEADER,
    CLIENT_NAME_HEADER,
    CLIENT_PARENT_HEADER,
    CLIENT_SIG_HEADER,
    CLIENT_VERSION_HEADER,
    SCOPE_BASES_HEADER,
    SCOPE_DEFAULT_HEADER,
    SESSION_ID_HEADER,
    client_headers,
    client_signature,
    daemon_url,
    is_listening,
    spawn_detached,
)
from odata1c.launch_parent import ИМЕНА_CLAUDE_CODE, родитель_заверён

_log = logging.getLogger(__name__)

# Тот же срок и тот же смысл, что у ОЖИДАНИЕ_ГОТОВНОСТИ_S в cli.py (cmd_daemon): обе стороны ждут
# один и тот же холодный старт демона (импорт lxml/ahocorasick, разбор daemon.yaml). Не импортирован
# оттуда — импорт в обратную сторону (cli.py уже импортирует run_launcher отсюда для команды mcp)
# сделал бы модули взаимозависимыми.
ОЖИДАНИЕ_ГОТОВНОСТИ_S = 15


class _АпстримМёртв(Exception):
    """Внутренний сигнал сторожка `_с_проверкой_живости`: TCP-порт демона перестал отвечать,
    пока апстрим-вызов ещё не вернул результат. Это НЕ таймаут вызова — Ruling 12
    (`task-6-fix-1.md`) прямо запрещает вводить общий таймаут ради виртуальных таблиц (легитимно
    идут до 180 с без признаков жизни): пока порт демона слушается, вызов ждёт сколько угодно;
    сторожок обнаруживает именно МЁРТВЫЙ процесс, от которого ответа не дождаться ни за какое
    время (см. `_с_проверкой_живости` — почему обычный `try/except` вокруг `await` этого
    не ловит)."""


# Раунд правок 1, находка 1: транспортные сбои апстрима, которые обработчики прокси превращают
# в штатный отказ, а не пропускают голым traceback. `MCPError` — демон закрыл соединение или
# ответил протокольной ошибкой (`probe_death2.py`: `MCPError: Connection closed`);
# `httpx2.HTTPError` — сетевой сбой самого HTTP-транспорта под streamable_http_client;
# `anyio.BrokenResourceError/ClosedResourceError/EndOfStream` — поток апстрима закрыт или порван;
# `TimeoutError`/`OSError` — таймаут или сбой сокета. `_АпстримМёртв` — сторожок обнаружил мёртвый
# TCP-порт под зависшим вызовом (см. класс выше). Осознанно НЕ `Exception` целиком: ошибка в
# собственном коде обработчика (опечатка, дефект) не должна маскироваться под «демон недоступен».
ОШИБКИ_АПСТРИМА: tuple[type[Exception], ...] = (
    MCPError,
    httpx2.HTTPError,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    anyio.EndOfStream,
    TimeoutError,
    OSError,
    _АпстримМёртв,
)

# Формулировка правится оркестратором против буквы задания раунда правок 1 (опасение 2 отчёта
# исполнителя): «следующий вызов поднимет его заново» было бы неправдой — эта сессия лаунчера
# после обрыва замкнута замком `ProxyHolder.апстрим_мёртв`, и демон поднимает не следующий вызов
# тула, а следующий запуск `odata1c mcp`, то есть переподключение клиента. Обещать модели
# несуществующее восстановление хуже, чем честно назвать нужное действие.
ДЕМОН_НЕДОСТУПЕН = (
    "демон 1С-шлюза недоступен, соединение потеряно; переподключите шлюз "
    "(перезапуск сессии MCP поднимет демон заново)"
)

# Раунд правок 1, находка 1 (повторная проверка после первичной правки): период между TCP-
# проверками сторожка `_с_проверкой_живости`. Не связан с `ОЖИДАНИЕ_ГОТОВНОСТИ_S` (холодный старт)
# — это обнаружение СМЕРТИ уже поднятого демона, не ожидание его запуска.
ИНТЕРВАЛ_СТОРОЖКА_С = 1.0

# Раунд правок 2, находка Б.4 (Minor с тяжёлым последствием): одна неудачная TCP-проверка не
# должна замыкать сессию замком навсегда. `_демон_жив` возвращает `False` на ЛЮБОЙ `OSError` —
# исчерпание эфемерных портов (WinError 10048/10055), икота фильтр-драйвера антивируса, момент
# перезапуска сокета — разовый локальный сбой самой проверки, не обрыв связи с демоном. Ревьюер
# воспроизвёл: единственная неудача из многих всё равно давала `_АпстримМёртв`, хотя следующая же
# проверка показала бы живой демон. N=3 подряд неудачных проверок (при интервале 1 с — 2-3 лишние
# секунды на настоящем обрыве, тот же порядок величины, что и раньше) отсекает единичную осечку,
# сохраняя быстрое обнаружение НАСТОЯЩЕЙ смерти: три проверки подряд без единого успеха — уже не
# совпадение. Любая ОДНА успешная проверка сбрасывает счётчик в ноль — «наполовину дохлый» демон
# так не засчитывается, только устойчивая недоступность.
ПОСЛЕДОВАТЕЛЬНЫХ_ОТКАЗОВ_ДО_СМЕРТИ = 3


async def _демон_жив(host: str, port: int, *, timeout: float = 1.0) -> bool:
    """Асинхронная TCP-проверка «жив ли демон» для сторожка `_с_проверкой_живости` — аналог
    `daemon.is_listening`, но без блокировки цикла событий: тот использует синхронный
    `socket.create_connection`, а сторожок вызывается ПАРАЛЛЕЛЬНО с апстрим-вызовом внутри того же
    процесса лаунчера — блокировать цикл на каждой проверке нельзя."""
    try:
        _, писатель = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError):
        return False
    писатель.close()
    with contextlib.suppress(OSError):
        await писатель.wait_closed()
    return True


def _host_port_из_адреса(адрес: str) -> tuple[str, int] | None:
    """host/port демона из адреса апстрима (`daemon_url(порт)` из `daemon.yaml` или явный
    `--url`) — сторожку `_с_проверкой_живости` нужен голый TCP-адрес, не HTTP URL целиком.
    `None`, если хост не разобрать (адрес без hostname) — тогда сторожок не запускается вовсе,
    поведение как до этой правки: голый `await вызов` без гонки."""
    части = urllib.parse.urlsplit(адрес)
    if not части.hostname:
        return None
    порт = части.port or (443 if части.scheme == "https" else 80)
    return части.hostname, порт


async def _с_проверкой_живости(
    host_port: tuple[str, int] | None,
    вызов,
    *,
    проверка_живости=_демон_жив,
    интервал: float = ИНТЕРВАЛ_СТОРОЖКА_С,
    допустимых_отказов: int = ПОСЛЕДОВАТЕЛЬНЫХ_ОТКАЗОВ_ДО_СМЕРТИ,
):
    """Гонка апстрим-вызова против периодической TCP-проверки демона (раунд правок 1, находка 1;
    вторая, действующая правка — первая версия ловила только исключения, брошенные СИНХРОННО
    внутри `await upstream.call_tool(...)`, чего недостаточно для настоящего обрыва).

    Почему простой `try/except` вокруг `await upstream.call_tool(...)` не работает: обрыв
    соединения обнаруживается не в вызывающей задаче, а в СОБСТВЕННОЙ внутренней задаче
    `streamable_http_client` (запрос POST — `mcp/client/streamable_http.py::_run_request_post`,
    вызывается из отдельной `anyio.create_task_group()` внутри самого `streamable_http_client`),
    и её исключение всплывает только когда закрывается ВЕСЬ `async with streamable_http_client(...)`
    — то есть когда завершается вся сессия лаунчера целиком. До этого момента
    `await upstream.call_tool(...)` просто висит, ничего не возвращая и не падая. Доказано
    прогоном `probe_death2.py` на уже слитой в этот раунд правке (без сторожка): вызов после
    смерти демона не отвечает 30+ с (испытание оборвано таймаутом пробы, реальный потолок —
    закрытие всей сессии), а 123-строчный `ExceptionGroup` печатается лаунчером в stderr только
    в момент закрытия stdio. Обычный `try/except` вокруг ОДНОГО `await` физически не может
    перехватить исключение, брошенное в ДРУГОЙ задаче того же процесса.

    `host_port is None` — сторожок не запускается вовсе (адрес апстрима не разобрать): поведение
    как до этой правки, голый `await вызов`. Иначе — `вызов` и сторожок (переодическая
    `проверка_живости`, период `интервал`) бегут в одной `anyio.create_task_group()`; кто первым
    завершится, тот и определяет исход — успешный результат отменяет сторожок, смерть порта
    отменяет зависший вызов и поднимает `_АпстримМёртв`. `anyio.create_task_group()` заворачивает
    исключение единственной упавшей задачи в `BaseExceptionGroup` — разворачиваем его обратно,
    чтобы вызывающий код (`_переслать`/`_вызвать_тул`) продолжал ловить голые типы из
    `ОШИБКИ_АПСТРИМА`, как и раньше.

    `допустимых_отказов` — раунд правок 2, находка Б.4: сторожок объявлял демон мёртвым по ОДНОЙ
    неудачной TCP-проверке — разовый локальный сбой самой проверки (эфемерные порты, антивирус)
    неотличим от настоящего обрыва и необратимо замыкал бы сессию замком (`ProxyHolder.
    апстрим_мёртв`) зря. Раскрытие как «мёртв» только после `допустимых_отказов` подряд неудач
    подряд; любой успех сбрасывает счётчик."""
    if host_port is None:
        return await вызов
    host, port = host_port
    исход: dict[str, object] = {}

    async def _основной(группа) -> None:
        исход["значение"] = await вызов
        группа.cancel_scope.cancel()

    async def _сторожок() -> None:
        подряд_неудач = 0
        while True:
            await anyio.sleep(интервал)
            if await проверка_живости(host, port):
                подряд_неудач = 0
                continue
            подряд_неудач += 1
            if подряд_неудач >= допустимых_отказов:
                raise _АпстримМёртв(
                    f"TCP {host}:{port} не отвечает {подряд_неудач} проверок подряд — "
                    "демон, судя по всему, умер"
                )

    try:
        async with anyio.create_task_group() as группа:
            группа.start_soon(_основной, группа)
            группа.start_soon(_сторожок)
    except* BaseException as исключения:
        raise исключения.exceptions[0] from None
    return исход["значение"]


class ProxyHolder:
    """Текущая downstream-сессия лаунчера (сторона stdio-клиента) — единственное изменяемое
    состояние прокси. `session` перезаписывается каждым из семи обработчиков `build_proxy` при
    входе и читается колбэком `forward_elicit`, когда демон (апстрим) просит подтверждение
    у пользователя."""

    def __init__(self, http: httpx2.AsyncClient | None = None) -> None:
        self.session: object | None = None
        # HTTP-клиент апстрима и признак, что клиент лаунчера уже передан ему заголовками (план
        # M2, задача 9). `initialize` демону лаунчер шлёт сам, раньше, чем подключится его клиент,
        # — и демон видел бы клиентом лаунчер (имя `mcp` SDK, elicitation всегда; проверено
        # исполнением). Настоящего клиента лаунчер узнаёт из первого же запроса своей
        # downstream-сессии и один раз ставит его заголовками на все следующие запросы к демону
        # (`daemon.client_headers`) — тем же путём, что область видимости. Клиент у процесса
        # лаунчера один, и значения не меняются; `None` — тесты в памяти без HTTP.
        self.http = http
        self.клиент_передан = False
        # Заверил ли лаунчер имя клиента по своему родителю (Ruling 61): выставляется в
        # `run_launcher` при старте, до первого запроса. `False` — имя `claude-code` демону
        # заверенным не пойдёт (механизм `claude_code` не выдаётся).
        self.родитель_claude_code = False
        # Раунд правок 1, находка 1 (третья правка — «замок» на сессию): выставляется первым же
        # обработчиком, поймавшим `ОШИБКИ_АПСТРИМА`. Эмпирически (`probe_death2.py` на второй
        # версии правки — сторожок сам по себе): ПЕРВЫЙ вызов после смерти демона сторожок ловит
        # штатно (TCP-проверка + отмена зависшего `upstream.call_tool`), но ВТОРОЙ вызов на ТОЙ ЖЕ
        # апстрим-сессии зависает уже без единого отклика даже от сторожка — судя по всему, после
        # первого обрыва `streamable_http_client`/`ClientSession` остаются в необратимо сломанном
        # состоянии (внутренняя задача `_run_request_post` первого запроса не освобождает то, от
        # чего зависит планирование следующей). Без замка второй и все последующие вызовы висели
        # бы снова, хотя сторожок для первого сработал безукоризненно. Замок — не таймаут
        # выполнения (Ruling 12), а решение НЕ пытаться повторно сходить к апстриму, про который
        # уже достоверно известно, что он сломан безвозвратно в рамках ЭТОЙ сессии лаунчера;
        # процесс лаунчера в целом не падает и следующий отдельный запуск `odata1c mcp`
        # (`_дождаться_демона` в начале `run_launcher`) поднимает демон заново, как обычно.
        self.апстрим_мёртв: bool = False

    def запомнить(self, ctx) -> None:
        """Вход каждого обработчика: запомнить downstream-сессию (для `forward_elicit`) и при
        первом запросе передать клиента демону заголовками. Раньше любого запроса к демону этого
        обработчика — поэтому уже первый `tools/call` идёт с настоящим клиентом."""
        self.session = ctx.session
        if self.клиент_передан or self.http is None:
            return
        self.http.headers.update(
            client_headers(ctx.session, parent_is_claude_code=self.родитель_claude_code)
        )
        self.клиент_передан = True


def client_signer(key: bytes | None):
    """Хук запроса HTTP-клиента лаунчера (`event_hooks["request"]`): подписать заголовки клиента
    ключом лаунчера для сессии этого запроса (Ruling 59, `daemon.client_signature`).

    Хук, а не заголовок, выставленный один раз, — потому что подпись привязана к `mcp-session-id`,
    а его ставит транспорт SDK на каждый запрос, и у `initialize` его ещё нет (проверено
    исполнением: хук видит заголовок сессии на всех запросах после `initialize`, и выставленная им
    подпись доходит до `ctx.headers` тула). Нет сессии, заголовков клиента или ключа — запрос
    уходит без подписи: демон сочтёт клиента непроверенным."""

    async def подписать(request: httpx2.Request) -> None:
        заголовки = request.headers
        сессия = заголовки.get(SESSION_ID_HEADER)
        имя = заголовки.get(CLIENT_NAME_HEADER)
        if key is None or not сессия or имя is None:
            заголовки.pop(CLIENT_SIG_HEADER, None)
            return
        заголовки[CLIENT_SIG_HEADER] = client_signature(
            key,
            сессия,
            имя,
            заголовки.get(CLIENT_VERSION_HEADER, ""),
            заголовки.get(CLIENT_ELICITATION_HEADER, ""),
            заголовки.get(CLIENT_PARENT_HEADER, ""),
        )

    return подписать


def forward_elicit(holder: ProxyHolder):
    """Колбэк `elicitation_callback` апстрим-сессии: переслать запрос демона вниз, downstream-
    клиенту лаунчера (`holder.session`). Если downstream-сессии ещё нет — демон спросил раньше,
    чем клиент лаунчера сделал хоть один запрос, — вежливый отказ, а не голое исключение.

    Раунд правок 1, находка 7 (Minor из отчёта, но исправлена в этом раунде): `ElicitRequestParams`
    — объединение form- и url-режимов (`ElicitRequestFormParams | ElicitRequestURLParams`), у
    url-варианта нет `requested_schema` — старый код падал на нём (`probe_proxy.py`). Оба режима
    разбираются по типу; весь колбэк обёрнут в `try/except`, чтобы отказ downstream-клиента
    (`NoBackChannelError` и подобные) или любая иная ошибка пересылки возвращались как
    `ErrorData`, а не ронял тул демона, который об этом попросил."""

    async def on_elicit(
        context: object, params: types.ElicitRequestParams
    ) -> types.ElicitResult | types.ErrorData:
        if holder.session is None:
            return types.ElicitResult(action="decline")
        try:
            if isinstance(params, types.ElicitRequestFormParams):
                return await holder.session.elicit_form(params.message, params.requested_schema)
            if isinstance(params, types.ElicitRequestURLParams):
                return await holder.session.elicit_url(
                    params.message, params.url, params.elicitation_id
                )
            return types.ElicitResult(action="decline")
        except Exception as ошибка:  # noqa: BLE001 — отказ downstream-клиента не должен ронять тул
            return types.ErrorData(code=types.INTERNAL_ERROR, message=str(ошибка))

    return on_elicit


async def _переслать(
    операция: str,
    вызов_фабрика,
    holder: ProxyHolder,
    host_port: tuple[str, int] | None = None,
):
    """Общая точка вызова апстрима для операций без «штатного отказа» на уровне результата
    (`list_tools`/`list_resources`/`list_resource_templates`/`read_resource`/`list_prompts`/
    `get_prompt`): транспортный сбой (`ОШИБКИ_АПСТРИМА`) превращается в `MCPError` с понятным
    текстом — SDK сама сериализует его в JSON-RPC-ошибку клиенту (`raise_exceptions=False`,
    `mcp/server/runner.py`), голый traceback наружу не идёт. `tools/call` — отдельная функция
    (`_вызвать_тул`): там штатный отказ оформляется результатом (`is_error=True`), не исключением
    (SPEC §5.2).

    `host_port` — сторожок `_с_проверкой_живости` (раунд правок 1, находка 1): без него завис
    бы навсегда обрыв, обнаруженный не в этом `await`, а в чужой внутренней задаче SDK.

    `holder.апстрим_мёртв` — «замок» на сессию (та же находка, третья правка): выставлен —
    апстрим уже достоверно сломан, повторный `await` на нём не предпринимается вовсе (второй и
    все последующие вызовы после первого обнаруженного обрыва сами зависают без единого отклика
    даже от сторожка — см. докстринг `ProxyHolder`). `вызов_фабрика` — НЕ готовая корутина, а
    вызываемый без аргументов конструктор корутины: при выставленном замке она не должна
    создаваться вообще (иначе — `RuntimeWarning: coroutine was never awaited`, которую
    `filterwarnings = ["error"]` в pyproject.toml превращает в ошибку теста)."""
    if holder.апстрим_мёртв:
        raise MCPError(code=types.INTERNAL_ERROR, message=ДЕМОН_НЕДОСТУПЕН)
    try:
        return await _с_проверкой_живости(host_port, вызов_фабрика())
    except ОШИБКИ_АПСТРИМА as ошибка:
        _log.warning("апстрим недоступен при %s: %s: %s", операция, type(ошибка).__name__, ошибка)
        holder.апстрим_мёртв = True
        raise MCPError(code=types.INTERNAL_ERROR, message=ДЕМОН_НЕДОСТУПЕН) from ошибка


async def _вызвать_тул(
    upstream: ClientSession,
    params: types.CallToolRequestParams,
    holder: ProxyHolder,
    host_port: tuple[str, int] | None = None,
):
    """`tools/call`: штатный отказ — `CallToolResult(is_error=True, ...)`, не исключение (SPEC
    §5.2 — исключение теряет текст у клиента). Ошибка САМОГО тула демона (например, тул поднял
    исключение внутри своей логики) в исключение `call_tool` не превращается вообще — это уже
    `CallToolResult(is_error=True)`, дошедший как обычный результат; сюда попадают только
    транспортные сбои (`ОШИБКИ_АПСТРИМА`). `host_port`/`holder.апстрим_мёртв` — см. `_переслать`."""
    if holder.апстрим_мёртв:
        return types.CallToolResult(
            is_error=True,
            content=[types.TextContent(type="text", text=ДЕМОН_НЕДОСТУПЕН)],
        )
    try:
        return await _с_проверкой_живости(
            host_port, upstream.call_tool(params.name, params.arguments or {})
        )
    except ОШИБКИ_АПСТРИМА as ошибка:
        _log.warning("апстрим недоступен при tools/call: %s: %s", type(ошибка).__name__, ошибка)
        holder.апстрим_мёртв = True
        return types.CallToolResult(
            is_error=True,
            content=[types.TextContent(type="text", text=ДЕМОН_НЕДОСТУПЕН)],
        )


def build_proxy(
    upstream: ClientSession,
    holder: ProxyHolder,
    *,
    name: str = "odata1c-mcp-launcher",
    version: str = "",
    instructions: str | None = None,
    host_port: tuple[str, int] | None = None,
) -> Server:
    """Прокси лаунчера: семь обработчиков lowlevel `Server`, каждый вызывает соответствующий
    метод апстрим-сессии (демона) и возвращает его результат как есть — своей логики здесь нет
    (SPEC §2.2). Уведомления `tools/list_changed` не пересылаются: набор тулов демона не
    меняется на лету, пересылка недостающего уведомления вреда клиенту не приносит.

    `name`/`version`/`instructions` — раунд правок 1, находка 2: без них клиент видел
    `instructions: None` и терял правила работы с токенами и запрет доверять содержимому полей
    1С (SPEC §5). `run_launcher` передаёт сюда результат `upstream.initialize()` как есть —
    прокси представляется клиенту тем же, чем демон представился прокси. Значения по умолчанию
    оставлены только ради обратной совместимости вызова без них (юнит-тесты в памяти, где
    инструкции демона не важны).

    `host_port` — раунд правок 1, находка 1 (сторожок `_с_проверкой_живости`): `run_launcher`
    передаёт сюда host/port демона, разобранные из адреса апстрима. `None` по умолчанию — тесты
    в памяти (`InMemoryTransport`) не поднимают настоящий TCP-порт, сторожку там нечего слушать."""

    async def on_list_tools(ctx, params):
        holder.запомнить(ctx)
        return await _переслать(
            "tools/list", lambda: upstream.list_tools(params=params), holder, host_port
        )

    async def on_call_tool(ctx, params: types.CallToolRequestParams):
        holder.запомнить(ctx)
        return await _вызвать_тул(upstream, params, holder, host_port)

    async def on_list_resources(ctx, params):
        holder.запомнить(ctx)
        return await _переслать(
            "resources/list", lambda: upstream.list_resources(params=params), holder, host_port
        )

    async def on_list_resource_templates(ctx, params):
        holder.запомнить(ctx)
        return await _переслать(
            "resources/templates/list",
            lambda: upstream.list_resource_templates(params=params),
            holder,
            host_port,
        )

    async def on_read_resource(ctx, params: types.ReadResourceRequestParams):
        holder.запомнить(ctx)
        return await _переслать(
            "resources/read", lambda: upstream.read_resource(params.uri), holder, host_port
        )

    async def on_list_prompts(ctx, params):
        holder.запомнить(ctx)
        return await _переслать(
            "prompts/list", lambda: upstream.list_prompts(params=params), holder, host_port
        )

    async def on_get_prompt(ctx, params: types.GetPromptRequestParams):
        holder.запомнить(ctx)
        return await _переслать(
            "prompts/get",
            lambda: upstream.get_prompt(params.name, params.arguments),
            holder,
            host_port,
        )

    return Server(
        name,
        version=version,
        instructions=instructions,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_list_resource_templates=on_list_resource_templates,
        on_read_resource=on_read_resource,
        on_list_prompts=on_list_prompts,
        on_get_prompt=on_get_prompt,
    )


def scope_headers(bases: list[str] | None, default: str | None) -> dict[str, str]:
    """Заголовки области видимости для HTTP-клиента лаунчера (SPEC §2.1, поправка задачи 5):
    `--bases`/`--default` этой команды превращаются в них один раз при построении клиента, не
    на каждый вызов тула.

    Без `bases`/`default` заголовок не выставляется вовсе — не пустой строкой: `daemon.py`
    (`scope_from_headers`) читает пустую `X-Odata1c-Bases` как явное «ни одной базы», а
    отсутствие заголовка — как «не сужено, видно всё». Молчаливая замена одного другим здесь
    была бы сужением или расширением видимости, которого никто не просил.

    Сами имена к этому моменту уже проверены (`cli.py::_разобрать_bases`/`_проверить_имя_базы`,
    раунд правок 1, находка 4): здесь их не-ASCII или иначе неверный вид уже невозможен —
    `httpx2.AsyncClient(headers=…)` кодирует заголовки в ASCII и падает `UnicodeEncodeError` на
    первом же непроверенном значении."""
    заголовки: dict[str, str] = {}
    if bases is not None:
        заголовки[SCOPE_BASES_HEADER] = ",".join(bases)
    if default is not None:
        заголовки[SCOPE_DEFAULT_HEADER] = default
    return заголовки


async def _дождаться_демона(home: pathlib.Path, port: int) -> None:
    """Поднять демон, если его порт ещё не слушается, и подождать готовность до
    `ОЖИДАНИЕ_ГОТОВНОСТИ_S`. При неудаче — сообщение в stderr (stdout этого процесса зарезервирован
    под протокол MCP, как только начнётся `proxy.run`, — сюда его печатать нельзя ни на одном
    шаге) и выход с кодом 1: `SystemExit` — обычное `BaseException`; `cli.py::cmd_mcp` перехватывает
    его отдельно от прочих ошибок запуска и просто возвращает уже готовый код.
    """
    if is_listening(port):
        return
    spawn_detached(home, port)
    предел = time.monotonic() + ОЖИДАНИЕ_ГОТОВНОСТИ_S
    while time.monotonic() < предел:
        if is_listening(port):
            return
        await asyncio.sleep(0.2)
    print(
        f"демон не ответил на порту {port} за {ОЖИДАНИЕ_ГОТОВНОСТИ_S} с — "
        f"проверьте журнал: {home / 'logs'} (daemon.log — сам демон, "
        f"daemon-launch.log — запуск через Планировщик заданий)",
        file=sys.stderr,
    )
    raise SystemExit(1)


async def run_launcher(
    home: pathlib.Path, *, bases: list[str] | None, default: str | None, url: str | None
) -> None:
    """`odata1c mcp` (SPEC §2.2): stdio наружу, Streamable HTTP внутрь.

    Адрес демона — `url`, если задан явно (обычно нестандартный порт или сеть — лаунчер тогда
    НЕ пытается поднять демон сам: это не его домашний каталог решает, кто там слушает), иначе
    `daemon_url(port)` из `daemon.yaml` этого домашнего каталога — и тогда лаунчер поднимает
    демон сам, если порт ещё не занят.

    Домашний каталог досоздаётся целиком (`ensure_home` + `ensure_templates` + `ensure_gate_secret`)
    ДО чтения настроек — раунд правок 1, находка 3а: команда подключения `claude mcp add odata1c
    -- uv run --directory <репозиторий> odata1c mcp` обязана работать на машине, где `odata1c
    init` не выполняли (SPEC §2.1 п. 1 поручает это лаунчеру), а `load_config` требует непустой
    `gate_secret` в `daemon.yaml`. Тем же способом, что и `cmd_init` — вызовы идемпотентны,
    повторный `ensure_*` на уже готовом домашнем каталоге ничего не меняет.

    Ключ лаунчера (Ruling 59) — тоже здесь и тоже до подъёма демона: демон читает его только при
    старте, и поднятый этим лаунчером демон должен его застать.
    """
    ensure_home(home)
    ensure_templates(home)
    ensure_gate_secret(home / "daemon.yaml")
    ключ = ensure_launcher_key(home)

    config = load_config(home)
    if url is not None:
        адрес = url
    else:
        порт = config.daemon.port
        await _дождаться_демона(home, порт)
        адрес = daemon_url(порт)

    # Ruling 61: заверить имя клиента `claude-code` только если лаунчер запущен исполняемым файлом
    # Claude Code (по дереву процессов, мимо шимов запуска). Считается один раз при старте.
    разрешённые = ИМЕНА_CLAUDE_CODE | {и.lower() for и in config.daemon.claude_code_parents}
    родитель_ок, значимый = родитель_заверён(разрешённые)
    _log.info(
        "родитель лаунчера: %s → имя клиента %s",
        значимый or "не определён",
        "заверяется" if родитель_ок else "не заверяется",
    )

    holder = ProxyHolder()
    holder.родитель_claude_code = родитель_ок
    таймаут = httpx2.Timeout(10, read=None)
    заголовки = scope_headers(bases, default)
    host_port = _host_port_из_адреса(адрес)
    async with (
        httpx2.AsyncClient(
            headers=заголовки,
            timeout=таймаут,
            event_hooks={"request": [client_signer(ключ)]},
        ) as http,
        streamable_http_client(адрес, http_client=http) as (up_read, up_write),
        ClientSession(up_read, up_write, elicitation_callback=forward_elicit(holder)) as upstream,
    ):
        # Клиент лаунчера узнаётся из первого запроса downstream-сессии и передаётся демону
        # заголовками этого HTTP-клиента (`ProxyHolder.запомнить`, план M2, задача 9).
        holder.http = http
        итог_инициализации = await upstream.initialize()
        proxy = build_proxy(
            upstream,
            holder,
            name=итог_инициализации.server_info.name,
            version=итог_инициализации.server_info.version,
            instructions=итог_инициализации.instructions,
            host_port=host_port,
        )
        async with stdio_server() as (read, write):
            await proxy.run(read, write, proxy.create_initialization_options())
