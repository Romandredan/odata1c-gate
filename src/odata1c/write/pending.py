"""Хранилище pending-операций и лимит коммитов (SPEC §7.3, §7.4; решения 3 и 7 плана M2).

Между подготовкой пишущего тула и `odata1c_commit` операция живёт здесь — в памяти демона, а не
на диске (решение 3 плана M2): файл с телом запроса стал бы вторым местом на диске с реальными
значениями защищаемых полей, помимо `journal.sqlite`, а перезапуск демона теряет только
подготовленные-но-не-подтверждённые операции — их подготовка дёшева (без обращения к 1С за
данными, которых чтение уже не видело) и модель просто готовит их заново. Персистентность нужна
только выполненным операциям — она в `journal.sqlite` (другая задача плана), не здесь.

`PendingStore` и `CommitLimiter` не знают о гейте, о 1С и о FastMCP: единственная внешняя
зависимость — протокол `WriteError` (задача 2) и часы (`clock=`), которые тесты подменяют, чтобы
проверить TTL и окно лимита без `sleep`.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from odata1c.write.errors import WriteError


@dataclass
class PendingOp:
    """Одна подготовленная операция записи между `prepare` и `commit` (SPEC §7.3).

    Граница инварианта 1 проходит внутри самого класса: `request` (тело запроса к 1С — метод,
    путь, JSON) несёт РЕАЛЬНЫЕ значения защищаемых полей, `key` тоже реален, но без противоречия
    с гейтом — ключ почти всегда `Ref_Key`, а GUID инвариант 6 не защищает ни на одном уровне;
    `preview` и `result`, наоборот, уже прошли гейт и несут токены `[[type:tail]]` — это то, что
    видит модель.

    `request` дополнительно скрыт из `repr()` через `field(repr=False)`. Дата-класс не
    переопределяет `__str__`/`__format__` — оба по умолчанию делегируют в `__repr__`, поэтому
    скрытие одного поля здесь закрывает разом `repr(op)`, `str(op)` и f-строку `f"{op}"`: ошибка,
    попавшая в `logger.exception` или в необработанное исключение демона как есть, не вынесет
    тело запроса в `logs/daemon.log` (`AGENTS.md` уже предупреждает, что этот файл содержит
    реальные адреса запросов — тело запроса в нём появляться не должно тем более).
    """

    pending_id: str
    commit_id: str
    session_id: str
    base: str
    role: str
    op: str
    entity: str
    key: dict | None
    request: dict = field(repr=False)
    preview: dict
    data_version: str | None
    created_at: float
    expires_at: float
    undo_of: str | None = None
    status: str = "pending"
    result: str | None = None


def _pending_unknown() -> WriteError:
    """Один код и один текст на «нет такой операции» и «операция чужой сессии» (решение 4 плана
    M2). Текст намеренно не называет `pending_id` — иначе ответ на чужую сессию отличался бы от
    ответа на случайный id только числом повторов, а этого достаточно, чтобы отличить «операция
    существует, но не ваша» от «такой операции никогда не было»; отдельная функция без параметра
    гарантирует, что это не восстановится случайно при будущей правке одного из двух мест ниже."""
    return WriteError(
        "pending_unknown",
        "pending-операция не найдена",
        hint="проверьте pending_id или подготовьте операцию заново — прежнюю мог взять другой "
        "сеанс или её не стало после перезапуска демона",
    )


class PendingStore:
    """Pending-операции текущих сессий: `put` кладёт, `take` отдаёт с проверкой сессии и TTL,
    `finish` фиксирует результат `commit`, `purge` убирает истёкшее (решение 3 плана M2).

    Одна `asyncio.Lock` на весь стор, а не по `pending_id`: тела методов — несколько операций со
    словарём в Python (микросекунды, без `await` внутри критической секции), пишущий путь не
    настолько горяч, чтобы делить блокировку тоньше стоило усложнения.
    """

    def __init__(self, ttl_s: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._ops: dict[str, PendingOp] = {}
        self._lock = asyncio.Lock()

    def deadline(self) -> float:
        """`clock() + ttl_s` — момент, которым вызывающий код (`WriteService`) проставляет
        `PendingOp.expires_at`: `put` не вычисляет это поле сам (оно нужно уже готовым, до
        сохранения операции — SPEC §7.3 шаг 4). Берите `expires_at` только отсюда, а не из
        своих часов: два независимых источника времени для одного TTL — верный способ получить
        операцию, которая либо истекла мгновенно, либо не истечёт никогда."""
        return self._clock() + self._ttl_s

    async def put(self, op: PendingOp) -> PendingOp:
        """Кладёт операцию в стор и возвращает её же — без копии: вызывающий код (`WriteService`)
        уже держит ссылку на тот же объект, а `finish` ниже меняет `status`/`result` через
        словарь стора, а не через отдельно всплывающую копию."""
        async with self._lock:
            self._ops[op.pending_id] = op
        return op

    async def take(self, pending_id: str, session_id: str) -> PendingOp:
        """Отдаёт операцию для `commit`/`undo`; при отказе бросает `WriteError`.

        `pending_unknown` — нет такой операции или она принадлежит другой сессии (решение 4:
        один код на оба случая). `pending_expired` — операция ещё не выполнена (`status ==
        "pending"`) и TTL истёк до подтверждения пользователем. Выполненная операция
        (`committed`/`failed`) живёт до того же `expires_at`, что и pending — окно
        идемпотентности: до истечения `take` просто возвращает её как есть (инвариант 2,
        повторный `commit` после обрыва связи получает прежний `result`, а не выполняет запись
        второй раз); после истечения операция для стора как будто не существует вовсе —
        `pending_unknown`, а не `pending_expired`: `pending_expired` означает «истекла операция,
        которую ещё можно было подтвердить», выполненная операция такой уже не является.
        """
        async with self._lock:
            op = self._ops.get(pending_id)
            if op is None or op.session_id != session_id:
                raise _pending_unknown()

            if self._clock() > op.expires_at:
                if op.status == "pending":
                    raise WriteError(
                        "pending_expired",
                        f"pending-операция «{pending_id}» истекла (TTL {self._ttl_s} с)",
                        hint="подготовьте операцию заново",
                    )
                raise _pending_unknown()

            return op

    async def finish(self, pending_id: str, *, status: str, result: str) -> None:
        """Фиксирует результат `commit`: `status` (`committed`/`failed`) и `result` (уже через
        гейт — SPEC §5.2, инвариант 1). Молча ничего не делает для неизвестного `pending_id`:
        к моменту вызова операция уже прошла `take` с проверкой сессии и TTL — второй раз
        проверять нечего, а если конкурентный `purge` успел вычистить запись между `take` и
        `finish` (TTL истёк за время самого запроса к 1С), отказывать уже бессмысленно — запись
        в 1С состоялась или нет независимо от стора, сообщить об этом больше некому.
        """
        async with self._lock:
            op = self._ops.get(pending_id)
            if op is not None:
                op.status = status
                op.result = result

    async def purge(self) -> int:
        """Удаляет операции с истёкшим `expires_at`, возвращает их число. Периодическая уборка
        демона, не часть горячего пути `commit`/`take` — те справляются с просрочкой сами."""
        async with self._lock:
            now = self._clock()
            истёкшие = [pid for pid, op in self._ops.items() if now > op.expires_at]
            for pid in истёкшие:
                del self._ops[pid]
            return len(истёкшие)


class CommitLimiter:
    """Скользящее окно коммитов на сессию (решение 7 плана M2, SPEC §3.2): предел по роли базы
    (`prod` 20, `test` 50, `dev` без лимита — `limit=0`) считается в `commit`, а не в подготовке
    — подготовка не тратит бюджет, можно готовить операции впрок и решать позже, какую
    подтвердить.

    Синхронный класс: `check_and_count` не объявлен `async def` и внутри не ждёт ничего, вызывает
    его демон один раз на `commit`, без параллельных вызовов для одной сессии в рамках одного
    цикла событий (тот же порядок, что уже сериализует `PendingStore.take` перед обращением к
    1С) — отдельная блокировка здесь была бы кодом без наблюдаемого эффекта.
    """

    def __init__(self, *, window_s: int = 600, clock: Callable[[], float] = time.monotonic) -> None:
        self._window_s = window_s
        self._clock = clock
        self._commits: dict[str, deque[float]] = {}

    def check_and_count(self, session_id: str, limit: int) -> None:
        """`limit == 0` — без лимита (роль `dev`): выходит сразу, не заводя запись для
        `session_id` вовсе — иначе список меток времени рос бы без предела для долгой сессии,
        которую и так никто не собирается ограничивать.

        Иначе выбрасывает устаревшие метки (старше `window_s` от текущего момента — скользящее,
        а не фиксированное окно) и либо отказывает `commit_limit`, либо засчитывает коммит,
        добавляя текущую метку.
        """
        if limit == 0:
            return

        now = self._clock()
        начало_окна = now - self._window_s
        окно = self._commits.setdefault(session_id, deque())
        while окно and окно[0] <= начало_окна:
            окно.popleft()

        if len(окно) >= limit:
            ждать_с = round(окно[0] + self._window_s - now)
            raise WriteError(
                "commit_limit",
                f"лимит коммитов на сессию исчерпан: {limit} за {self._window_s // 60} мин",
                hint=f"следующий коммит станет возможен примерно через {ждать_с} с — окно "
                "скользящее, предел задаёт роль базы (bases.yaml → role)",
            )

        окно.append(now)
