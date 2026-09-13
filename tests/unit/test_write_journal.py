"""Журнал записи journal.sqlite (SPEC §7.5, §7.6; задача 4 плана M2).

`операция()` — тот же приём, что в test_write_pending.py: готовая `PendingOp` с реальным
значением защищаемого поля в `request`, чтобы тесты на утечку в ошибку были не абстрактными.

`open_journal` — фабрика-фикстура, а не голый `Journal(...)` в теле каждого теста: `close()`
вызывается в teardown фикстуры, а не последней строкой теста — иначе упавшее до неё `assert`
пропускает `close()`, соединение остаётся открытым, и `filterwarnings=["error"]` превращает
утечку в `PytestUnraisableExceptionWarning`, которая перекрывает исходную причину падения в
выводе pytest (так уже было при отладке этого файла — см. отчёт задачи). Повторный `close()` в
teardown для тестов, которые закрывают соединение сами (имитация обрыва) — безопасен: у
`sqlite3.Connection.close()` нет эффекта на уже закрытом соединении.
"""

from __future__ import annotations

import datetime
import decimal
import math
import sqlite3
import sys

import pytest

from odata1c.config.home import check_file_permissions, ensure_home
from odata1c.write.errors import WriteError
from odata1c.write.journal import ВЕРСИЯ_СХЕМЫ, Journal
from odata1c.write.pending import PendingOp


def операция(
    *,
    commit_id: str = "c1",
    base: str = "trade_dev",
    entity: str = "Catalog_Контрагенты",
    session_id: str = "sess-1",
    op: str = "update",
    request: dict | None = None,
) -> PendingOp:
    return PendingOp(
        pending_id="p1",
        commit_id=commit_id,
        session_id=session_id,
        base=base,
        role="dev",
        op=op,
        entity=entity,
        key={"Ref_Key": "11111111-1111-1111-1111-111111111111"},
        request=request
        if request is not None
        else {"method": "PATCH", "json": {"ИНН": "7707083893"}},
        preview={"было": "[[inn:AAAAAAAAAA]]", "станет": "[[inn:BBBBBBBBBB]]"},
        data_version="42",
        created_at=0.0,
        expires_at=600.0,
    )


class ЧасыЗаглушка:
    """Часы журнала с ручным сдвигом — `clock=` ожидает `Callable[[], datetime.datetime]`."""

    def __init__(self, t: datetime.datetime) -> None:
        self.t = t

    def __call__(self) -> datetime.datetime:
        return self.t

    def сдвинуть(self, dt: datetime.timedelta) -> None:
        self.t += dt


МОМЕНТ = datetime.datetime(2026, 9, 12, 12, 0, 0, tzinfo=datetime.UTC)


@pytest.fixture
def open_journal(tmp_path):
    """`open_journal(clock=..., path=...) -> Journal` — путь по умолчанию внутри `tmp_path`,
    часы по умолчанию — настоящие. Каждый открытый экземпляр закрывается в teardown, независимо
    от того, дошёл тест до собственного `close()` или упал раньше."""
    открытые: list[Journal] = []

    def _open(*, clock=None, path=None):
        кварги = {"clock": clock} if clock is not None else {}
        журнал = Journal(path if path is not None else tmp_path / "journal.sqlite", **кварги)
        открытые.append(журнал)
        return журнал

    yield _open

    for журнал in открытые:
        журнал.close()


# --- полный цикл open/close/get ------------------------------------------------------------------


def test_открыть_закрыть_прочитать_полный_цикл(open_journal):
    журнал = open_journal(clock=ЧасыЗаглушка(МОМЕНТ))
    op = операция()

    журнал.open_commit(op, client="claude-code", before={"ИНН": "СТАРЫЙ_7707083893"})
    журнал.close_commit("c1", after={"ИНН": "7707083893"}, status="committed")

    запись = журнал.get("c1")
    assert запись is not None
    assert запись.commit_id == "c1"
    assert запись.base == "trade_dev"
    assert запись.entity == "Catalog_Контрагенты"
    assert запись.op == "update"
    assert запись.session_id == "sess-1"
    assert запись.client == "claude-code"
    assert запись.key == {"Ref_Key": "11111111-1111-1111-1111-111111111111"}
    assert запись.before == {"ИНН": "СТАРЫЙ_7707083893"}
    assert запись.after == {"ИНН": "7707083893"}
    assert запись.request == {"method": "PATCH", "json": {"ИНН": "7707083893"}}
    assert запись.status == "committed"
    assert запись.error is None
    assert запись.undone_by is None
    assert запись.committed_at is not None


def test_get_неизвестного_commit_id_возвращает_none(open_journal):
    журнал = open_journal()
    assert журнал.get("нет-такого") is None


def test_started_без_close_видна_в_recent_честно(open_journal):
    журнал = open_journal(clock=ЧасыЗаглушка(МОМЕНТ))
    журнал.open_commit(операция(), client="claude-code", before=None)

    запись = журнал.get("c1")
    assert запись.status == "started"
    assert запись.after is None
    assert запись.committed_at is None

    недавние = журнал.recent(base=None, limit=10)
    assert [строка.commit_id for строка in недавние] == ["c1"]
    assert недавние[0].status == "started"


def test_close_commit_status_failed_с_ошибкой(open_journal):
    журнал = open_journal()
    журнал.open_commit(операция(), client="claude-code", before={"ИНН": "7707083893"})

    журнал.close_commit("c1", after=None, status="failed", error="odata_error: 500")

    запись = журнал.get("c1")
    assert запись.status == "failed"
    assert запись.error == "odata_error: 500"
    assert запись.after is None


def test_close_commit_дописывает_ключ_create_которого_не_было_до_запроса(open_journal):
    """Задача 7: у `create` ключ выдаёт 1С в ответе POST, а строка «до» пишется раньше запроса —
    с `key_json` NULL. `close_commit(key=…)` дописывает его; без `key` прежний ключ не трогается."""
    журнал = open_journal()
    создание = операция(commit_id="c1", op="create")
    создание.key = None
    журнал.open_commit(создание, client="trust", before=None)
    assert журнал.get("c1").key is None

    журнал.close_commit("c1", after={"Ref_Key": "r"}, status="committed", key={"Ref_Key": "r"})
    журнал.open_commit(операция(commit_id="c2"), client="trust", before=None)
    журнал.close_commit("c2", after=None, status="committed")

    assert журнал.get("c1").key == {"Ref_Key": "r"}
    assert журнал.get("c2").key == {"Ref_Key": "11111111-1111-1111-1111-111111111111"}


# --- mark_undone -----------------------------------------------------------------------------


def test_mark_undone_связывает_с_откатившим_commit_id(open_journal):
    журнал = open_journal()
    журнал.open_commit(операция(commit_id="c1"), client="claude-code", before=None)
    журнал.close_commit("c1", after=None, status="committed")

    журнал.mark_undone("c1", "c2")

    запись = журнал.get("c1")
    assert запись.undone_by == "c2"
    assert запись.status == "committed"  # статус исходной записи не меняется


def test_mark_undone_неизвестный_commit_id_ошибка_программы(open_journal):
    журнал = open_journal()
    with pytest.raises(RuntimeError):
        журнал.mark_undone("нет-такого", "c2")


# --- recent: несколько записей, базы, порядок -------------------------------------------------


def test_recent_по_убыванию_времени(open_journal):
    часы = ЧасыЗаглушка(МОМЕНТ)
    журнал = open_journal(clock=часы)

    журнал.open_commit(операция(commit_id="c1"), client="claude-code", before=None)
    часы.сдвинуть(datetime.timedelta(seconds=1))
    журнал.open_commit(операция(commit_id="c2"), client="claude-code", before=None)
    часы.сдвинуть(datetime.timedelta(seconds=1))
    журнал.open_commit(операция(commit_id="c3"), client="claude-code", before=None)

    недавние = журнал.recent(base=None, limit=10)
    assert [строка.commit_id for строка in недавние] == ["c3", "c2", "c1"]


def test_recent_устойчивый_порядок_при_равном_времени(open_journal):
    # Требование брифа: часы подменяемы и в тестах вполне возвращают одно и то же значение для
    # двух коммитов подряд — recent() не должен полагаться на неопределённый порядок SQLite,
    # вторичный ключ (rowid, порядок вставки) делает результат детерминированным.
    журнал = open_journal(clock=ЧасыЗаглушка(МОМЕНТ))
    журнал.open_commit(операция(commit_id="c1"), client="claude-code", before=None)
    журнал.open_commit(операция(commit_id="c2"), client="claude-code", before=None)

    первый_прогон = [строка.commit_id for строка in журнал.recent(base=None, limit=10)]
    второй_прогон = [строка.commit_id for строка in журнал.recent(base=None, limit=10)]

    assert первый_прогон == второй_прогон == ["c2", "c1"]


def test_recent_base_none_все_базы(open_journal):
    журнал = open_journal()
    журнал.open_commit(операция(commit_id="c1", base="trade_dev"), client="c", before=None)
    журнал.open_commit(операция(commit_id="c2", base="bp_test"), client="c", before=None)

    все = журнал.recent(base=None, limit=10)
    только_trade_dev = журнал.recent(base="trade_dev", limit=10)

    assert {строка.commit_id for строка in все} == {"c1", "c2"}
    assert [строка.commit_id for строка in только_trade_dev] == ["c1"]


def test_recent_соблюдает_limit(open_journal):
    журнал = open_journal()
    for i in range(5):
        журнал.open_commit(операция(commit_id=f"c{i}"), client="c", before=None)

    недавние = журнал.recent(base=None, limit=2)

    assert len(недавние) == 2


# --- права файла (Windows) --------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="icacls работает только на Windows")
def test_файл_создаётся_с_правами_домашнего_каталога(tmp_path, open_journal):
    home = ensure_home(tmp_path / "home").path
    путь = home / "journal.sqlite"
    журнал = open_journal(path=путь)
    журнал.open_commit(операция(), client="claude-code", before=None)
    журнал.close()  # проверка прав идёт по закрытому файлу — icacls не требует эксклюзивности,
    # но правило «Journal открыт только на время вызова» проверяется здесь же явным close()

    assert check_file_permissions(путь) is None


def test_close_отпускает_файл_для_удаления(tmp_path):
    # На Windows открытое соединение SQLite мешает удалению/переносу файла — Journal
    # держит соединение открытым только на срок вызова (как index/gate), close() обязан
    # снять блокировку, а не оставить файл занятым до сборки мусора. Без фикстуры — тест
    # сам проверяет close() и не должен закрывать журнал повторно в teardown.
    путь = tmp_path / "journal.sqlite"
    журнал = Journal(путь)
    журнал.open_commit(операция(), client="claude-code", before=None)
    журнал.close()

    путь.unlink()

    assert not путь.exists()


# --- повреждённый файл ------------------------------------------------------------------------


def test_повреждённый_файл_даёт_writeerror_с_подсказкой(tmp_path):
    путь = tmp_path / "journal.sqlite"
    путь.write_bytes(b"not a real sqlite database, just random junk bytes 12345")

    with pytest.raises(WriteError) as ошибка:
        Journal(путь)

    assert ошибка.value.code == "internal"
    assert str(путь) in str(ошибка.value)
    assert str(путь) in ошибка.value.hint


# --- двойной open_commit / close_commit без open_commit — ошибка программы --------------------


def test_open_commit_повторно_для_того_же_commit_id_ошибка_программы(open_journal):
    журнал = open_journal()
    журнал.open_commit(операция(commit_id="c1"), client="claude-code", before=None)

    with pytest.raises(RuntimeError):
        журнал.open_commit(операция(commit_id="c1"), client="claude-code", before=None)


def test_close_commit_без_open_commit_ошибка_программы(open_journal):
    журнал = open_journal()
    with pytest.raises(RuntimeError):
        журнал.close_commit("нет-такого", after=None, status="committed")


# --- реальные значения не в repr/str/f-строке, не в ошибке -------------------------------------


def test_journalentry_repr_str_и_f_строка_не_несут_before_after_request(open_journal):
    журнал = open_journal()
    журнал.open_commit(
        операция(request={"method": "PATCH", "json": {"ИНН": "МЕТКА_ЖУРНАЛА_777"}}),
        client="claude-code",
        before={"ИНН": "МЕТКА_ДО_777"},
    )
    журнал.close_commit("c1", after={"ИНН": "МЕТКА_ПОСЛЕ_777"}, status="committed")
    запись = журнал.get("c1")

    for текст in (repr(запись), str(запись), f"{запись}"):
        assert "МЕТКА_ЖУРНАЛА_777" not in текст
        assert "МЕТКА_ДО_777" not in текст
        assert "МЕТКА_ПОСЛЕ_777" not in текст
        # "=" отличает имя самого поля от совпадения по подстроке в другом имени
        # (`requested_at` содержит "request", но это не то же самое поле).
        assert "before=" not in текст
        assert "after=" not in текст
        assert "request=" not in текст
    # Остальные поля в repr остаются — скрыто только то, что несёт реальное значение.
    assert "commit_id" in repr(запись)
    assert "status" in repr(запись)


def test_отказ_open_commit_до_запроса_не_несёт_before_ни_в_сообщении_ни_в_cause(open_journal):
    # Имитация обрыва перед самой записью (диск, права): закрываем соединение так, чтобы
    # следующий execute() внутри open_commit бросил sqlite3.Error, не связанный с дублем
    # commit_id. Требование брифа: реальное значение из `before` не должно всплыть ни в
    # str(ошибка), ни в repr(ошибка), ни в цепочке __cause__/__context__.
    журнал = open_journal()
    журнал._connection.close()

    метка = "СЕКРЕТНАЯ_МЕТКА_ДО_ЗАПРОСА_42"
    with pytest.raises(WriteError) as ошибка:
        журнал.open_commit(
            операция(request={"method": "PATCH", "json": {"ИНН": метка}}),
            client="claude-code",
            before={"ИНН": метка},
        )

    assert ошибка.value.code == "internal"
    assert метка not in str(ошибка.value)
    assert метка not in repr(ошибка.value)
    assert ошибка.value.__cause__ is not None
    assert метка not in repr(ошибка.value.__cause__)
    assert метка not in str(ошибка.value.__cause__)


def test_отказ_close_commit_не_несёт_after_ни_в_сообщении_ни_в_cause(open_journal):
    журнал = open_journal()
    журнал.open_commit(операция(), client="claude-code", before=None)
    журнал._connection.close()

    метка = "СЕКРЕТНАЯ_МЕТКА_ПОСЛЕ_ЗАПРОСА_42"
    with pytest.raises(WriteError) as ошибка:
        журнал.close_commit("c1", after={"ИНН": метка}, status="committed")

    assert ошибка.value.code == "internal"
    assert метка not in str(ошибка.value)
    assert метка not in repr(ошибка.value)
    assert ошибка.value.__cause__ is not None
    assert метка not in repr(ошибка.value.__cause__)


# =================================================================================================
# Раунд 2 (ревью Changes Requested, m2-task4-review.md)
# =================================================================================================


# --- B-1(а): mkdir()/connect() внутри try — файл-каталог, блокированный путь -------------------


def test_путь_журнала_это_каталог_даёт_writeerror(tmp_path):
    путь = tmp_path / "journal.sqlite"
    путь.mkdir()  # путь существует как директория, а не файл

    with pytest.raises(WriteError) as ошибка:
        Journal(путь)

    assert ошибка.value.code == "internal"


def test_путь_журнала_блокирован_файлом_даёт_writeerror(tmp_path):
    # mkdir(parents=True) должен создать tmp_path/"blocker"/"sub", но "blocker" — обычный файл,
    # а не каталог: раньше это падало голым FileExistsError/OSError (B-1а ревью, воспроизведено
    # ревьюером на Windows как WinError 183).
    заблокировано = tmp_path / "blocker"
    заблокировано.write_text("не каталог")

    with pytest.raises(WriteError) as ошибка:
        Journal(заблокировано / "sub" / "journal.sqlite")

    assert ошибка.value.code == "internal"


# --- B-1(б): несериализуемые значения before/after/request — WriteError, не голый Type/ValueError


def test_decimal_в_before_даёт_writeerror_не_голый_typeerror(open_journal):
    журнал = open_journal()

    with pytest.raises(WriteError) as ошибка:
        журнал.open_commit(
            операция(), client="claude-code", before={"Сумма": decimal.Decimal("10.50")}
        )

    assert ошибка.value.code == "internal"
    assert ошибка.value.__cause__ is not None
    assert isinstance(ошибка.value.__cause__, TypeError)
    # После неудавшегося open_commit строки в журнале нет вовсе — запрос к 1С не выполнялся бы.
    assert журнал.get("c1") is None


def test_datetime_в_request_даёт_writeerror_не_голый_typeerror(open_journal):
    журнал = open_journal()

    with pytest.raises(WriteError) as ошибка:
        журнал.open_commit(
            операция(request={"method": "PATCH", "json": {"Дата": datetime.datetime.now()}}),
            client="claude-code",
            before=None,
        )

    assert ошибка.value.code == "internal"
    assert isinstance(ошибка.value.__cause__, TypeError)


def test_nan_в_after_close_commit_даёт_writeerror_не_голый_valueerror(open_journal):
    журнал = open_journal()
    журнал.open_commit(операция(), client="claude-code", before=None)

    with pytest.raises(WriteError) as ошибка:
        журнал.close_commit("c1", after={"Курс": math.nan}, status="committed")

    assert ошибка.value.code == "internal"
    assert isinstance(ошибка.value.__cause__, ValueError)


# --- B-1б (находка исходного ревью): mark_undone не перезаписывает другим undone_by -------------


def test_mark_undone_тем_же_undone_by_повторно_идемпотентно(open_journal):
    журнал = open_journal()
    журнал.open_commit(операция(commit_id="c1"), client="claude-code", before=None)
    журнал.mark_undone("c1", "c2")

    журнал.mark_undone("c1", "c2")  # повтор с тем же значением — не ошибка

    assert журнал.get("c1").undone_by == "c2"


def test_mark_undone_другим_undone_by_повторно_ошибка_программы(open_journal):
    журнал = open_journal()
    журнал.open_commit(операция(commit_id="c1"), client="claude-code", before=None)
    журнал.mark_undone("c1", "c2")

    with pytest.raises(RuntimeError):
        журнал.mark_undone("c1", "c3")

    # Первый факт (кто отменил) не потерян при отказе второй попытки.
    assert журнал.get("c1").undone_by == "c2"


# --- B-4: PRAGMA user_version — якорь миграций ---------------------------------------------------


def test_новый_журнал_проставляет_версию_схемы(tmp_path, open_journal):
    путь = tmp_path / "journal.sqlite"
    open_journal(path=путь)

    проверочное_соединение = sqlite3.connect(путь)
    try:
        версия = проверочное_соединение.execute("PRAGMA user_version").fetchone()[0]
    finally:
        проверочное_соединение.close()

    assert версия == ВЕРСИЯ_СХЕМЫ


def test_несовместимая_версия_схемы_даёт_writeerror(tmp_path):
    путь = tmp_path / "journal.sqlite"
    # Файл с корректной (для текущего кода) схемой commits, но версией из будущего — тот же
    # сценарий, что у "устаревшего разбора" индекса
    # (index/repository.py::require_current_version), только на открытии, а не на отдельном
    # методе: журнал не поддерживает миграцию "на лету".
    подготовка = sqlite3.connect(путь)
    try:
        подготовка.executescript(
            """
            CREATE TABLE commits (
                commit_id TEXT PRIMARY KEY, base TEXT NOT NULL, entity TEXT NOT NULL,
                key_json TEXT, op TEXT NOT NULL, session_id TEXT NOT NULL, client TEXT,
                requested_at TEXT NOT NULL, committed_at TEXT, before_json TEXT,
                after_json TEXT, request_json TEXT, status TEXT NOT NULL, error TEXT,
                undone_by TEXT
            );
            """
        )
        подготовка.execute(f"PRAGMA user_version = {ВЕРСИЯ_СХЕМЫ + 1}")
        подготовка.commit()
    finally:
        подготовка.close()

    with pytest.raises(WriteError) as ошибка:
        Journal(путь)

    assert ошибка.value.code == "internal"
    assert str(путь) in str(ошибка.value)


# --- B-5: recent(limit) — границы -----------------------------------------------------------------


@pytest.mark.parametrize("плохой_limit", [0, -1, -100])
def test_recent_limit_меньше_1_даёт_valueerror(open_journal, плохой_limit):
    журнал = open_journal()
    журнал.open_commit(операция(), client="claude-code", before=None)

    with pytest.raises(ValueError):
        журнал.recent(base=None, limit=плохой_limit)


def test_recent_limit_1_работает(open_journal):
    журнал = open_journal()
    журнал.open_commit(операция(commit_id="c1"), client="claude-code", before=None)
    журнал.open_commit(операция(commit_id="c2"), client="claude-code", before=None)

    недавние = журнал.recent(base=None, limit=1)

    assert len(недавние) == 1
