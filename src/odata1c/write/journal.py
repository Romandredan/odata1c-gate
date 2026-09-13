"""Журнал записи `journal.sqlite` (SPEC §7.5, §7.6; задача 4 плана M2).

Между `open_commit` (до запроса к 1С) и `close_commit` (после) журнал — единственное место, где
остаётся след того, что было отправлено и что получилось: без него откат (`undo`, задача 8)
невозможен, а сам `commit` не может ответить на повторный вызов после обрыва связи прежним
результатом (та же идемпотентность, что `PendingStore.finish` держит в памяти, только для уже
выполненных записей — после перезапуска демона память пуста, а `journal.sqlite` — нет).

`before_json`/`after_json`/`request_json` несут РЕАЛЬНЫЕ значения защищаемых полей — это не
противоречит инварианту 1 `AGENTS.md` ("реальные значения не выходят через MCP"): журнал не MCP,
это файл владельца на его собственном диске. Наружу эти поля отдаёт только гейт поверх
`odata1c_journal` (задача 8, не эта) — сам `Journal` о гейте не знает и знать не должен (тот же
принцип изоляции, что у `write/errors.py`: пакет записи закрыт от гейта и запросов к 1С).

Файл открывается на время вызова, как индекс метаданных и словарь гейта (`index/repository.py`,
`gate/dictionary.py`) — тот же приём и по той же причине: на Windows открытое соединение SQLite
мешает переносу и удалению файла, а `journal.sqlite` лежит в домашнем каталоге, который целиком
может понадобиться скопировать или почистить. Держать соединение открытым на весь срок жизни
демона означало бы держать файл заблокированным всё это время. Вызывающий код (`WriteService`,
задача 7) создаёт `Journal(path)` на один вызов и закрывает в `finally`.
"""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from odata1c.write.errors import WriteError
from odata1c.write.pending import PendingOp

_log = logging.getLogger(__name__)

СХЕМА = """
CREATE TABLE IF NOT EXISTS commits (
    commit_id TEXT PRIMARY KEY,
    base TEXT NOT NULL,
    entity TEXT NOT NULL,
    key_json TEXT,
    op TEXT NOT NULL,
    session_id TEXT NOT NULL,
    client TEXT,
    requested_at TEXT NOT NULL,
    committed_at TEXT,
    before_json TEXT,
    after_json TEXT,
    request_json TEXT,
    status TEXT NOT NULL,
    error TEXT,
    undone_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_commits_base ON commits(base, requested_at);
"""

# `PRAGMA user_version` — якорь для будущих миграций схемы (round 2, находка B-4 ревью): у
# `gate/dictionary.py` такой якорь появился только ПОСЛЕ того, как схема действительно поменялась
# один раз, а нужный тогда пересчёт задним числом оказался отдельной задачей. Заводим версию сразу
# на первой схеме — дешевле, чем повторять тот же долг. Файл с версией, отличной от текущей (не 0
# — 0 значит «файл только что создан этим же кодом, PRAGMA ни разу не проставлялась» — и не
# ВЕРСИЯ_СХЕМЫ), рассинхронизирован с этим кодом: читать его как есть означало бы либо упасть на
# отсутствующей колонке при первом же запросе, либо, хуже, успешно прочитать по случайному
# совпадению структуры и отдать не то. `WriteError` при открытии — тот же принцип, что у
# повреждённого файла: журналу не нужна миграция «на лету», нужен явный отказ.
ВЕРСИЯ_СХЕМЫ = 1


class _ВерсияСхемыНеСовпадает(Exception):
    """Внутренний маркер: файл существует и читаем, но его `PRAGMA user_version` — не текущая
    версия схемы и не 0 (свежесозданная база). Наружу не выходит — `__init__` перехватывает и
    переводит в `WriteError` тем же способом, что и повреждённый файл (B-4 ревью round 2)."""

    def __init__(self, найдено: int) -> None:
        super().__init__(f"версия схемы журнала {найдено}, ожидалась {ВЕРСИЯ_СХЕМЫ}")
        self.найдено = найдено


def _default_clock() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _dump(value: dict | None) -> str | None:
    # allow_nan=False (round 2, B-1б): по умолчанию json.dumps сериализует nan/inf литералами
    # `NaN`/`Infinity` — валидный Python, но не валидный JSON (1С такое не примет, а обратное
    # чтение через json.loads молча проглотило бы то же нарушение). Явный ValueError здесь —
    # то же решение, что для несериализуемых типов (Decimal, datetime): открытие/close_commit
    # отказывают до отправки в 1С, а не тихо портят файл журнала синтаксисом не-JSON.
    return None if value is None else json.dumps(value, ensure_ascii=False, allow_nan=False)


def _load(text: str | None) -> dict | None:
    return None if text is None else json.loads(text)


@dataclass
class JournalEntry:
    """Одна строка журнала — прочитанная запись о выполненной (или начатой, но не завершённой —
    `status == "started"`) записи в 1С.

    `before`/`after`/`request` скрыты из `repr()` тем же приёмом, что `PendingOp.request`
    (`write/pending.py`, `field(repr=False)`): дата-класс не переопределяет `__str__`/
    `__format__`, оба по умолчанию делегируют в `__repr__`, поэтому скрытие этих трёх полей
    разом закрывает `repr(entry)`, `str(entry)` и f-строку `f"{entry}"` — необработанное
    исключение или `logger.exception` с этой записью как аргументом не вынесет реальные
    значения в `logs/daemon.log`.
    """

    commit_id: str
    base: str
    entity: str
    key: dict | None
    op: str
    session_id: str
    client: str | None
    requested_at: str
    committed_at: str | None
    before: dict | None = field(repr=False)
    after: dict | None = field(repr=False)
    request: dict | None = field(repr=False)
    status: str
    error: str | None
    undone_by: str | None


class Journal:
    """`journal.sqlite` в домашнем каталоге (SPEC §7.5), WAL. Реальные значения — файл
    владельца, наружу — только через гейт (`odata1c_journal`, задача 8)."""

    def __init__(
        self,
        path: pathlib.Path,
        *,
        clock: Callable[[], datetime.datetime] = _default_clock,
    ) -> None:
        self.path = pathlib.Path(path)
        self._clock = clock
        # `_connection` объявлен ДО try — если исключение прилетит раньше присвоения (`connect()`
        # сам бросил, путь занят каталогом), except ниже должен отличить «соединения не было» от
        # «соединение есть, но что-то пошло не так после» без AttributeError на пути закрытия.
        self._connection: sqlite3.Connection | None = None
        try:
            # round 2, находка B-1(а) ревью: раньше `mkdir()`/`connect()` стояли ДО этого try —
            # каталог вместо файла (`sqlite3.OperationalError: unable to open database file`) или
            # файл, блокирующий путь `mkdir(parents=True)` (`FileExistsError`), уходили из
            # `__init__` голыми исключениями ОС/sqlite, а не `WriteError`. Тот же перехват ниже
            # закрывает оба случая наравне с повреждённым файлом.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path)
            self._connection.row_factory = sqlite3.Row
            # Штатное управление транзакциями (без isolation_level=None) — тот же аргумент, что
            # у index/schema.py::connect() и gate/dictionary.py::Dictionary.__init__: `with
            # self._connection:` в open_commit/close_commit/mark_undone обязано фиксировать или
            # откатывать ОДНУ транзакцию целиком, а не создавать иллюзию отката при отдельно уже
            # зафиксированной вставке.
            self._connection.execute("PRAGMA journal_mode=WAL")
            # synchronous — на умолчании (FULL), а не NORMAL, как у gate/dictionary.py: там
            # понижение синхронности объяснено тем, что потерянный токен сам восстановится при
            # следующей встрече значения. Для журнала это неверно — потерянная строка `commits`
            # невосстановима, а без неё `undo` не на что опереться. Под WAL+NORMAL фиксированная
            # транзакция переживает крах процесса, но может быть потеряна при отказе ОС или
            # питания: ровно сценарий «open_commit → запрос к 1С выполнен → крах» оставил бы
            # выполненную запись в 1С без единой строки в журнале — то, ради чего запись «до»
            # существует. Тот же выбор уже сделан для индекса (index/schema.py::connect() тоже
            # не трогает synchronous). Цена — один fsync на open_commit/close_commit, при объёме,
            # ограниченном лимитом коммитов на сессию (20–50 за 10 минут, SPEC §3.2) и диалогом
            # подтверждения перед каждым — не то, на чём стоит экономить.
            self._connection.executescript(СХЕМА)
            версия = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if версия == 0:
                self._connection.execute(f"PRAGMA user_version = {ВЕРСИЯ_СХЕМЫ}")
            elif версия != ВЕРСИЯ_СХЕМЫ:
                raise _ВерсияСхемыНеСовпадает(версия)
        except (sqlite3.DatabaseError, OSError, _ВерсияСхемыНеСовпадает) as ошибка:
            # Если файл существует, но не SQLite-база (или повреждён), sqlite3 узнаёт об этом не
            # на connect(), а только на первой операции — соединение к этому моменту уже открыто
            # и держит файловый дескриптор. Не закрыв его перед тем, как исключение уйдёт
            # наверх, получаем недостижимый, но не закрытый sqlite3.Connection — на сборке
            # мусора ResourceWarning, а filterwarnings=["error"] превращает его в ошибку сессии
            # pytest (тот же дефект и то же решение — index/schema.py, gate/dictionary.py).
            # `_connection` может быть `None` (сам `connect()` бросил) — закрываем только если
            # соединение действительно открыто.
            if self._connection is not None:
                self._connection.close()
            # Текст и подсказка НЕ содержат ни str(ошибка), ни полного пути (М-2 ревью
            # итоговых правок M2): сообщение OSError несёт путь, а сам путь — домашний каталог с
            # именем пользователя ОС, и уходит он не владельцу, а модели в ответе тула. Модели
            # хватает имени файла и того, где он лежит; полный путь пишется в журнал демона. У
            # общего перехвата на будущее нет гарантии, что текст исключения безобиден на каждой
            # ОС, — проще держать инвариант «наш текст не цитирует исключение». Цепочка
            # `from ошибка` сохранена: трассировка и исходное исключение остаются в логе демона
            # (`__cause__`), только не пересказываются в тексте, который видит модель.
            _log.error("журнал записи недоступен: %s (%s)", self.path, type(ошибка).__name__)
            файл = f"{self.path.name} в домашнем каталоге шлюза"
            if isinstance(ошибка, _ВерсияСхемыНеСовпадает):
                raise WriteError(
                    "internal",
                    f"журнал записи собран другой версией схемы: {файл}",
                    hint=(
                        f"формат журнала не совпадает с этой версией odata1c — обновите пакет"
                        f" или откатите его до версии, которая создала {файл}. Если это"
                        f" невозможно, переместите файл в сторону и начните новый журнал"
                        f" (история выполненных записей и опора отката для них будет потеряна);"
                        f" полный путь — в журнале демона"
                    ),
                ) from ошибка
            raise WriteError(
                "internal",
                f"журнал записи повреждён или недоступен: {файл}",
                hint=(
                    f"журнал — единственное место, где хранится история выполненных записей и"
                    f" опора отката (undo): восстановить его нельзя, только начать заново."
                    f" Переместите повреждённый {файл} в сторону и запустите ещё раз — новый"
                    f" журнал начнёт накапливаться с нуля, но откат записей, сделанных до сих"
                    f" пор, перестанет быть доступен; полный путь — в журнале демона"
                ),
            ) from ошибка

    def close(self) -> None:
        self._connection.close()

    def open_commit(self, op: PendingOp, *, client: str, before: dict | None) -> None:
        """Запись «до» — строго до запроса к 1С (SPEC §7.3): обрыв демона между этим вызовом и
        `close_commit` оставляет строку со `status == "started"` — `recent`/`get` показывают её
        честно, как незавершённую, а не молчат о ней.

        Отказ этой записи (диск, права, повреждённый файл) останавливает сам запрос к 1С — без
        строки журнала не на что опереться откату (`undo`), и выполнять запрос вслепую незачем:
        `WriteError("internal")`, без `before`/`request` ни в `message`, ни в `hint` — реальные
        значения не уходят даже в диагностику демона (инвариант 1; тест на `str`/`repr` этой
        ошибки и на цепочку `__cause__` — намеренно проверяет именно текст исключения, а не
        только код).
        """
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO commits (commit_id, base, entity, key_json, op, session_id,"
                    " client, requested_at, before_json, request_json, status)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        op.commit_id,
                        op.base,
                        op.entity,
                        _dump(op.key),
                        op.op,
                        op.session_id,
                        client,
                        self._clock().isoformat(timespec="microseconds"),
                        _dump(before),
                        _dump(op.request),
                        "started",
                    ),
                )
        except sqlite3.IntegrityError as ошибка:
            # commit_id уже есть в журнале — повторный вызов open_commit с тем же commit_id, а
            # не повторный commit после обрыва связи: тот идёт по идемпотентности PendingStore
            # (op ещё числится "pending"/"committed" в памяти демона, take() отдаёт прежний
            # результат, до второго open_commit дело не доходит — pending.py::PendingStore.take)
            # и сюда не доходит вовсе. Раз строка с этим commit_id уже есть — вызывающий код
            # ошибся, а не пользователь; тот же класс исключения, что у необъяснимой коллизии
            # токена гейта (gate/dictionary.py::_свободный_токен).
            raise RuntimeError(
                f"open_commit вызван повторно для commit_id={op.commit_id!r}"
            ) from ошибка
        except (sqlite3.Error, TypeError, ValueError) as ошибка:
            # round 2, находка B-1(б) ревью: `_dump()` вызывается как аргумент execute() — то
            # есть внутри этого же try, — но `json.dumps` бросает `TypeError` на несериализуемый
            # тип (`Decimal`, `datetime`) и `ValueError` на `nan`/`inf`; ни один не наследник
            # `sqlite3.Error`, и раньше уходил голым. `before`/`request` формально приходят из
            # JSON (GET-ответ 1С, тело WriteService), но сама эта гарантия — не то, что обязан
            # проверять `Journal`: несериализуемое значение здесь останавливает запрос к 1С тем
            # же способом, что и отказ диска — строки журнала всё равно не будет. Текст ошибки
            # не цитирует `ошибка` (см. `__init__`) — сообщение `TypeError`/`ValueError` от
            # `json.dumps` не несёт реальное значение (только имя типа), но и это не проверяется
            # каждый раз индивидуально, правило общее.
            raise WriteError(
                "internal",
                "не удалось записать журнал перед выполнением записи",
                hint="запрос к 1С не выполнен — без строки журнала нечем будет опереть откат"
                " (undo). Проверьте место на диске и права на файл журнала, затем повторите"
                " подготовку операции и commit",
            ) from ошибка

    def close_commit(
        self,
        commit_id: str,
        *,
        after: dict | None,
        status: str,
        error: str | None = None,
        key: dict | None = None,
    ) -> None:
        """Запись «после» — запрос к 1С уже выполнен, отказ здесь его не отменяет.

        `key` — ключ объекта, которого не было при `open_commit` (задача 7 плана M2): у `create`
        ключ выдаёт 1С в ответе POST, а строка «до» пишется раньше запроса, с `key_json` NULL.
        Без ключа в журнале откат `create` (пометка удаления, SPEC §7.6) не знал бы, какой объект
        помечать. `None` — ключ не меняется: у прочих операций он записан уже в `open_commit`.

        В отличие от `open_commit`, отказ этой записи не должен блокировать ответ пользователю —
        решение, что делать с успешно выполненным, но не записанным в журнал `commit`
        (предупреждение в ответе, запись в журнал демона), принимает вызывающий код
        (`WriteService`, задача 7): здесь только сам факт, что `close_commit` может бросить
        `WriteError("internal")` не хуже `open_commit`, вызывающий код обязан её ловить отдельно.
        """
        try:
            with self._connection:
                курсор = self._connection.execute(
                    "UPDATE commits SET after_json = ?, status = ?, error = ?, committed_at = ?,"
                    " key_json = COALESCE(?, key_json) WHERE commit_id = ?",
                    (
                        _dump(after),
                        status,
                        error,
                        self._clock().isoformat(timespec="microseconds"),
                        _dump(key),
                        commit_id,
                    ),
                )
        except (sqlite3.Error, TypeError, ValueError) as ошибка:
            # round 2, находка B-1(б) ревью — тот же случай, что в open_commit, только для
            # `_dump(after)`.
            raise WriteError(
                "internal",
                "не удалось дописать журнал после выполненной записи",
                hint="сама запись в 1С уже прошла — не повторяйте commit; проверьте место на"
                " диске и права на файл журнала",
            ) from ошибка
        if курсор.rowcount == 0:
            # Нет строки — close_commit вызван без предшествующего open_commit с этим
            # commit_id. При штатной работе WriteService это невозможно: open_commit
            # выполняется первым и, если он бросил исключение, запрос к 1С (а значит, и
            # close_commit) не происходит вовсе. Ошибка программы, а не отказ пользователю.
            raise RuntimeError(f"close_commit вызван для commit_id={commit_id!r} без open_commit")

    def set_after(self, commit_id: str, after: dict | None) -> None:
        """Дописать состояние «после» к уже закрытой записи (Т7-1 ревью задачи 7).

        `commit` закрывает запись (`close_commit(status="committed", key=…)`) сразу по ответу 1С,
        до повторного чтения объекта: отмена вызова или сбой чтения иначе оставили бы выполненную
        запись `started`, а у `create` — без ключа. Состояние «после» приходит отдельным шагом и
        пишется отдельным вызовом; статус, ключ и время выполнения он не трогает. Отказ — тот же
        `WriteError("internal")`, что у `close_commit`; строки нет — ошибка программы."""
        try:
            with self._connection:
                курсор = self._connection.execute(
                    "UPDATE commits SET after_json = ? WHERE commit_id = ?",
                    (_dump(after), commit_id),
                )
        except (sqlite3.Error, TypeError, ValueError) as ошибка:
            raise WriteError(
                "internal",
                "не удалось дописать журнал после выполненной записи",
                hint="сама запись в 1С уже прошла — не повторяйте commit; проверьте место на"
                " диске и права на файл журнала",
            ) from ошибка
        if курсор.rowcount == 0:
            raise RuntimeError(f"set_after вызван для commit_id={commit_id!r} без open_commit")

    def mark_undone(self, commit_id: str, undone_by: str) -> None:
        """Отмечает исходный коммит как отменённый откатом `undone_by` (SPEC §7.6).

        Откат — сама pending-операция со своим собственным `commit_id` (решение 9 плана M2) и
        проходит `commit` как любая другая запись; эта строка только связывает исходную запись
        с той, что её откатила — статус исходной записи (`committed`/`failed`) не меняется.

        round 2, находка B-1б ревью: раньше второй вызов с ДРУГИМ `undone_by` молча
        перезатирал первый — факт «`c1` отменён откатом `c2`» терялся без следа и без ошибки,
        стоило кому-то (по ошибке) вызвать `mark_undone("c1", "c3")`. В проде второй откат для
        уже отменённого коммита не должна готовить сама задача 8 — но раз журнал существует
        именно для того, чтобы такие факты не терялись бесследно, это тот же класс «ошибка
        программы», что и повторный `open_commit`/`close_commit` без предшественника:
        `RuntimeError`, а не тихая перезапись. Повторный вызов с ТЕМ ЖЕ `undone_by` — идемпотентный
        повтор (например, после обрыва связи между записью в журнал и ответом вызывающему коду) и
        остаётся no-op, как раньше."""
        строка = self._connection.execute(
            "SELECT undone_by FROM commits WHERE commit_id = ?", (commit_id,)
        ).fetchone()
        if строка is None:
            raise RuntimeError(f"mark_undone: commit_id={commit_id!r} не найден в журнале")
        прежний = строка["undone_by"]
        if прежний == undone_by:
            return
        if прежний is not None:
            raise RuntimeError(
                f"mark_undone: commit_id={commit_id!r} уже отменён {прежний!r}, повторная"
                f" попытка отметить отмену {undone_by!r} — ошибка программы"
            )
        with self._connection:
            self._connection.execute(
                "UPDATE commits SET undone_by = ? WHERE commit_id = ?", (undone_by, commit_id)
            )

    def undo_of(self, commit_id: str) -> str | None:
        """Откатом какой записи является `commit_id` — обратный запрос по `undone_by` (находка
        B-2 ревью задачи 4: `undo_of` отдельным столбцом не хранится, связь извлекается из
        `undone_by` исходной). `None` — запись не откат или её исходная не отмечена."""
        строка = self._connection.execute(
            "SELECT commit_id FROM commits WHERE undone_by = ? ORDER BY rowid LIMIT 1",
            (commit_id,),
        ).fetchone()
        return None if строка is None else строка["commit_id"]

    def get(self, commit_id: str) -> JournalEntry | None:
        строка = self._connection.execute(
            "SELECT * FROM commits WHERE commit_id = ?", (commit_id,)
        ).fetchone()
        return None if строка is None else self._entry(строка)

    def recent(self, base: str | None, limit: int) -> list[JournalEntry]:
        """Последние записи, по убыванию `requested_at`; при равном времени — по убыванию
        `rowid` (порядок вставки), устойчивый вторичный ключ.

        Часы вызывающего кода подменяемы (`clock=`) и в тестах вполне возвращают одно и то же
        значение для двух коммитов подряд — без вторичного ключа порядок таких строк решал бы
        сам SQLite, то есть непредсказуемо и для теста, и для `odata1c_journal` в проде (тот же
        реальный момент времени у двух разных коммитов — не гипотетический случай: часы ОС
        не гарантируют разрешение точнее миллисекунд на всех платформах). `rowid` — неявный
        столбец обычной (не `WITHOUT ROWID`) таблицы; монотонен по порядку вставки, пока строки
        не удаляются, а журнал строки не удаляет никогда — `close_commit`/`mark_undone` только
        обновляют существующую.

        round 2, находка B-5 ревью: `limit < 1` — `ValueError`, а не молчаливое поведение
        SQLite. `LIMIT 0` вернул бы пустой список — не ошибка, но и не то, что вызывающий код
        почти наверняка имел в виду; `LIMIT -1` для SQLite означает «без ограничения», то есть
        отрицательный `limit` тихо вернул бы ВЕСЬ журнал целиком вместо отказа или пустого
        ответа — неожиданно и потенциально дорого для `odata1c_journal` (задача 8), если та
        передаст пользовательский `limit` без собственной проверки границ."""
        if limit < 1:
            raise ValueError(f"limit должен быть не меньше 1, получено {limit}")
        sql = "SELECT * FROM commits"
        параметры: tuple = ()
        if base is not None:
            sql += " WHERE base = ?"
            параметры = (base,)
        sql += " ORDER BY requested_at DESC, rowid DESC LIMIT ?"
        строки = self._connection.execute(sql, (*параметры, limit)).fetchall()
        return [self._entry(строка) for строка in строки]

    @staticmethod
    def _entry(строка: sqlite3.Row) -> JournalEntry:
        return JournalEntry(
            commit_id=строка["commit_id"],
            base=строка["base"],
            entity=строка["entity"],
            key=_load(строка["key_json"]),
            op=строка["op"],
            session_id=строка["session_id"],
            client=строка["client"],
            requested_at=строка["requested_at"],
            committed_at=строка["committed_at"],
            before=_load(строка["before_json"]),
            after=_load(строка["after_json"]),
            request=_load(строка["request_json"]),
            status=строка["status"],
            error=строка["error"],
            undone_by=строка["undone_by"],
        )
