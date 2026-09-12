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
import pathlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from odata1c.write.errors import WriteError
from odata1c.write.pending import PendingOp

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


def _default_clock() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _dump(value: dict | None) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        try:
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
        except sqlite3.DatabaseError as ошибка:
            # Если файл существует, но не SQLite-база (или повреждён), sqlite3 узнаёт об этом не
            # на connect(), а только на первой операции — соединение к этому моменту уже открыто
            # и держит файловый дескриптор. Не закрыв его перед тем, как исключение уйдёт
            # наверх, получаем недостижимый, но не закрытый sqlite3.Connection — на сборке
            # мусора ResourceWarning, а filterwarnings=["error"] превращает его в ошибку сессии
            # pytest (тот же дефект и то же решение — index/schema.py, gate/dictionary.py).
            self._connection.close()
            raise WriteError(
                "internal",
                f"журнал записи повреждён или недоступен: {self.path}",
                hint=(
                    f"журнал — единственное место, где хранится история выполненных записей и "
                    f"опора отката (undo): восстановить его нельзя, только начать заново. "
                    f"Переместите повреждённый файл {self.path} в сторону и запустите ещё раз — "
                    f"новый журнал начнёт накапливаться с нуля, но откат записей, сделанных до "
                    f"сих пор, перестанет быть доступен ({ошибка})"
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
        except sqlite3.Error as ошибка:
            raise WriteError(
                "internal",
                "не удалось записать журнал перед выполнением записи",
                hint="запрос к 1С не выполнен — без строки журнала нечем будет опереть откат"
                " (undo). Проверьте место на диске и права на файл журнала, затем повторите"
                " подготовку операции и commit",
            ) from ошибка

    def close_commit(
        self, commit_id: str, *, after: dict | None, status: str, error: str | None = None
    ) -> None:
        """Запись «после» — запрос к 1С уже выполнен, отказ здесь его не отменяет.

        В отличие от `open_commit`, отказ этой записи не должен блокировать ответ пользователю —
        решение, что делать с успешно выполненным, но не записанным в журнал `commit`
        (предупреждение в ответе, запись в журнал демона), принимает вызывающий код
        (`WriteService`, задача 7): здесь только сам факт, что `close_commit` может бросить
        `WriteError("internal")` не хуже `open_commit`, вызывающий код обязан её ловить отдельно.
        """
        try:
            with self._connection:
                курсор = self._connection.execute(
                    "UPDATE commits SET after_json = ?, status = ?, error = ?, committed_at = ?"
                    " WHERE commit_id = ?",
                    (
                        _dump(after),
                        status,
                        error,
                        self._clock().isoformat(timespec="microseconds"),
                        commit_id,
                    ),
                )
        except sqlite3.Error as ошибка:
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

    def mark_undone(self, commit_id: str, undone_by: str) -> None:
        """Отмечает исходный коммит как отменённый откатом `undone_by` (SPEC §7.6).

        Откат — сама pending-операция со своим собственным `commit_id` (решение 9 плана M2) и
        проходит `commit` как любая другая запись; эта строка только связывает исходную запись
        с той, что её откатила — статус исходной записи (`committed`/`failed`) не меняется."""
        with self._connection:
            курсор = self._connection.execute(
                "UPDATE commits SET undone_by = ? WHERE commit_id = ?", (undone_by, commit_id)
            )
        if курсор.rowcount == 0:
            raise RuntimeError(f"mark_undone: commit_id={commit_id!r} не найден в журнале")

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
        обновляют существующую."""
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
