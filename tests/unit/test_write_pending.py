"""Хранилище pending-операций и лимит коммитов (SPEC §7.3, §7.4; решения 3 и 7 плана M2).

`ЧасыЗаглушка` подменяет часы `PendingStore`/`CommitLimiter` явным сдвигом времени — TTL и
скользящее окно лимита проверяются без `sleep` (решение брифа: тесты идут на подменённых часах).
"""

from __future__ import annotations

import asyncio

import pytest

from odata1c.write.errors import WriteError
from odata1c.write.pending import CommitLimiter, PendingOp, PendingStore


class ЧасыЗаглушка:
    """Часы с ручным сдвигом — `clock=` ожидает `Callable[[], float]`, вызов возвращает `t`."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def сдвинуть(self, dt: float) -> None:
        self.t += dt


def операция(
    *,
    pending_id: str = "p1",
    session_id: str = "sess-1",
    request: dict | None = None,
    created_at: float = 0.0,
    expires_at: float = 600.0,
) -> PendingOp:
    """Готовая pending-операция для тестов: `request` несёт реальное значение защищаемого поля
    (ИНН), `preview` — тот же факт уже в токене, как отдаёт гейт."""
    return PendingOp(
        pending_id=pending_id,
        commit_id="c1",
        session_id=session_id,
        base="trade_dev",
        role="dev",
        op="update",
        entity="Catalog_Контрагенты",
        key={"Ref_Key": "11111111-1111-1111-1111-111111111111"},
        request=request
        if request is not None
        else {"method": "PATCH", "json": {"ИНН": "7707083893"}},
        preview={"было": "[[inn:AAAAAAAAAA]]", "станет": "[[inn:BBBBBBBBBB]]"},
        data_version="42",
        created_at=created_at,
        expires_at=expires_at,
    )


# --- PendingStore: put/take ------------------------------------------------------------------


async def test_put_и_take_возвращают_ту_же_операцию():
    # Часы фиксированы явно (а не оставлены на умолчание `time.monotonic`): `операция()` кладёт
    # `expires_at=600.0` в предположении часов, начинающихся с нуля — с настоящими часами
    # реального времени работы процесса это значение уже в прошлом.
    store = PendingStore(ttl_s=600, clock=ЧасыЗаглушка(0.0))
    op = операция()
    await store.put(op)

    взятая = await store.take("p1", "sess-1")

    assert взятая is op


async def test_чужая_сессия_и_несуществующий_id_дают_один_код_и_текст():
    # Решение 4 плана: разница в ответе сообщала бы модели, что у другой сессии есть операция
    # с таким номером — поэтому оба случая отвечают ОДНИМ кодом с ОДНИМ и тем же текстом.
    store = PendingStore(ttl_s=600, clock=ЧасыЗаглушка(0.0))
    await store.put(операция(pending_id="p1", session_id="sess-1"))

    with pytest.raises(WriteError) as чужая_сессия:
        await store.take("p1", "sess-2")
    with pytest.raises(WriteError) as неизвестный_id:
        await store.take("p2", "sess-1")

    assert чужая_сессия.value.code == "pending_unknown"
    assert неизвестный_id.value.code == "pending_unknown"
    assert чужая_сессия.value.message == неизвестный_id.value.message


async def test_два_одновременных_take_одного_id_не_ошибка():
    # Требование брифа: конкурентный take не должен отказывать второму вызову — двойное
    # выполнение предотвращает статус в commit (задача 7), не take.
    store = PendingStore(ttl_s=600, clock=ЧасыЗаглушка(0.0))
    op = операция()
    await store.put(op)

    первая, вторая = await asyncio.gather(
        store.take("p1", "sess-1"),
        store.take("p1", "sess-1"),
    )

    assert первая is op
    assert вторая is op


# --- PendingStore: TTL -------------------------------------------------------------------------


async def test_ttl_истёк_до_выполнения_pending_expired():
    часы = ЧасыЗаглушка(0.0)
    store = PendingStore(ttl_s=600, clock=часы)
    await store.put(операция(created_at=0.0, expires_at=600.0))

    часы.сдвинуть(600.1)

    with pytest.raises(WriteError) as отказ:
        await store.take("p1", "sess-1")
    assert отказ.value.code == "pending_expired"


async def test_выполненная_операция_до_ttl_отдаёт_прежний_result():
    часы = ЧасыЗаглушка(0.0)
    store = PendingStore(ttl_s=600, clock=часы)
    await store.put(операция(created_at=0.0, expires_at=600.0))
    await store.finish("p1", status="committed", result="[[ok:done]]")

    часы.сдвинуть(300.0)
    снова = await store.take("p1", "sess-1")

    assert снова.status == "committed"
    assert снова.result == "[[ok:done]]"


async def test_выполненная_операция_после_ttl_pending_unknown():
    часы = ЧасыЗаглушка(0.0)
    store = PendingStore(ttl_s=600, clock=часы)
    await store.put(операция(created_at=0.0, expires_at=600.0))
    await store.finish("p1", status="committed", result="[[ok:done]]")

    часы.сдвинуть(600.1)

    with pytest.raises(WriteError) as отказ:
        await store.take("p1", "sess-1")
    assert отказ.value.code == "pending_unknown"


async def test_deadline_считает_от_часов_стора_а_не_от_настоящего_времени():
    # Ловушка, которая уже один раз подвела этот файл (см. отчёт задачи 3): если вызывающий код
    # проставляет `expires_at` от СВОИХ часов, а стор проверяет TTL от СВОИХ — при подмене часов
    # только в одном месте операция становится либо мгновенно просроченной, либо бессмертной.
    # `deadline()` — единственный источник времени для `expires_at`, откуда бы его ни звали.
    часы = ЧасыЗаглушка(100.0)
    store = PendingStore(ttl_s=600, clock=часы)

    assert store.deadline() == 700.0

    часы.сдвинуть(50.0)
    assert store.deadline() == 750.0


async def test_purge_удаляет_истёкшие_и_возвращает_число():
    часы = ЧасыЗаглушка(0.0)
    store = PendingStore(ttl_s=600, clock=часы)
    await store.put(операция(pending_id="p1", expires_at=600.0))
    await store.put(операция(pending_id="p2", expires_at=1200.0))

    часы.сдвинуть(700.0)
    удалено = await store.purge()

    assert удалено == 1
    with pytest.raises(WriteError):
        await store.take("p1", "sess-1")
    оставшаяся = await store.take("p2", "sess-1")
    assert оставшаяся.pending_id == "p2"


# --- PendingOp: реальные значения не в repr/str/f-строке ----------------------------------------


def test_repr_str_и_f_строка_не_несут_request():
    op = операция(request={"method": "PATCH", "json": {"ИНН": "СЕКРЕТНЫЙ_ИНН_777"}})

    for текст in (repr(op), str(op), f"{op}"):
        assert "СЕКРЕТНЫЙ_ИНН_777" not in текст
        assert "request" not in текст
    # Остальные поля (в том числе preview — уже в токенах) в repr остаются: скрыто только
    # реальное значение, а не вся диагностика.
    assert "pending_id" in repr(op)
    assert "preview" in repr(op)


# --- CommitLimiter -------------------------------------------------------------------------------


def test_лимит_20_за_10_минут_21_й_коммит_отказ():
    часы = ЧасыЗаглушка(0.0)
    лимитер = CommitLimiter(window_s=600, clock=часы)

    for _ in range(20):
        лимитер.check_and_count("sess-1", limit=20)

    with pytest.raises(WriteError) as отказ:
        лимитер.check_and_count("sess-1", limit=20)
    assert отказ.value.code == "commit_limit"


def test_лимит_после_окончания_окна_снова_можно():
    часы = ЧасыЗаглушка(0.0)
    лимитер = CommitLimiter(window_s=600, clock=часы)

    for _ in range(20):
        лимитер.check_and_count("sess-1", limit=20)
    часы.сдвинуть(600.1)

    лимитер.check_and_count("sess-1", limit=20)  # не должно бросить


def test_лимит_0_без_ограничения():
    часы = ЧасыЗаглушка(0.0)
    лимитер = CommitLimiter(window_s=600, clock=часы)

    for _ in range(200):
        лимитер.check_and_count("sess-1", limit=0)


def test_окна_разных_сессий_независимы():
    часы = ЧасыЗаглушка(0.0)
    лимитер = CommitLimiter(window_s=600, clock=часы)

    for _ in range(20):
        лимитер.check_and_count("sess-1", limit=20)

    лимитер.check_and_count("sess-2", limit=20)  # своя сессия — свой счётчик, не должно бросить
