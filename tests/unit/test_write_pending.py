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


def _поля_отказа(ошибка: WriteError) -> tuple[str, str, str]:
    """Отказ целиком, а не только `.code` — находка ревью (раунд 2): совпадение по одному полю
    не гарантирует совпадение по остальным, если однажды код и текст разъедутся по двум разным
    веткам, которые сейчас совпадают только потому, что вызывают одну функцию `_pending_unknown`."""
    return (ошибка.code, ошибка.message, ошибка.hint)


async def test_чужая_сессия_и_несуществующий_id_дают_один_код_и_текст():
    # Решение 4 плана: разница в ответе сообщала бы модели, что у другой сессии есть операция
    # с таким номером — поэтому оба случая отвечают ОДНИМ кодом, ОДНИМ текстом и ОДНИМ hint'ом.
    # Сравниваем отказ целиком (код+текст+hint), а не только code: ревью показало, что тест по
    # одному code пропустил бы расхождение hint между веткам — при разном hint модель отличила бы
    # «операцию взяла другая сессия» от «такой операции не было», то же раскрытие другим путём.
    store = PendingStore(ttl_s=600, clock=ЧасыЗаглушка(0.0))
    await store.put(операция(pending_id="p1", session_id="sess-1"))

    with pytest.raises(WriteError) as чужая_сессия:
        await store.take("p1", "sess-2")
    with pytest.raises(WriteError) as неизвестный_id:
        await store.take("p2", "sess-1")

    assert _поля_отказа(чужая_сессия.value) == _поля_отказа(неизвестный_id.value)


async def test_чужая_сессия_на_просроченной_операции_даёт_pending_unknown():
    # Находка ревью (раунд 2, Important): порядок проверок в take — сессия РАНЬШЕ TTL, это не
    # случайность, а Решение 4 целиком. Если бы TTL проверялся первым, чужая сессия, обратившаяся
    # к просроченной операции другой сессии, получила бы pending_expired (текст называет
    # pending_id и TTL) — подтверждение, что операция с таким pending_id существовала и когда-то
    # кому-то принадлежала. Ровно то раскрытие, которое Решение 4 запрещает, но через другую
    # ветку (TTL), чем «чужая сессия у живой операции» из теста выше.
    часы = ЧасыЗаглушка(0.0)
    store = PendingStore(ttl_s=600, clock=часы)
    await store.put(операция(pending_id="p1", session_id="sess-1"))
    часы.сдвинуть(600.1)  # операция истекла, но статус ещё "pending"

    with pytest.raises(WriteError) as отказ:
        await store.take("p1", "чужая-сессия")

    assert отказ.value.code == "pending_unknown"


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


async def test_ruling_40_finish_продлевает_окно_на_полный_ttl_а_не_остаток():
    # Ruling 40: подтверждение может прийти под самый конец исходного TTL (здесь — t=ttl-1).
    # Без продления окно идемпотентности после finish длилось бы секунду, а не 10 минут — и
    # повторный commit после обрыва связи почти сразу получил бы pending_unknown вместо прежнего
    # result. finish продлевает expires_at до clock() + ttl_s (полный TTL от момента finish).
    ttl = 600
    часы = ЧасыЗаглушка(0.0)
    # grace_s=0: эта проверка про Ruling 40 (продление expires_at в finish), не про Ruling 41
    # (запас purge) — с умолчанием grace_s=300 purge() ниже не удалил бы запись на t=1200.
    store = PendingStore(ttl_s=ttl, clock=часы, grace_s=0)
    await store.put(операция(created_at=0.0, expires_at=часы.t + ttl))

    часы.t = ttl - 1  # 599 — почти весь TTL подготовки истёк, операция ещё pending
    await store.take("p1", "sess-1")
    await store.finish("p1", status="committed", result="[[ok:done]]")

    часы.t = ttl + ttl // 2  # 900: по старому правилу (expires_at не продлён) — уже за 600
    снова = await store.take("p1", "sess-1")
    assert снова.status == "committed"
    assert снова.result == "[[ok:done]]"

    часы.t = 2 * ttl  # 1200: новое окно (599 + 600 = 1199) тоже истекло
    with pytest.raises(WriteError) as отказ:
        await store.take("p1", "sess-1")
    assert отказ.value.code == "pending_unknown"

    удалено = await store.purge()
    assert удалено == 1


async def test_take_на_границе_expires_at_включительно_ещё_действительна():
    # Находка ревью (раунд 2, Minor): граница TTL не была закреплена тестом ни в одну сторону
    # (`>` в take/purge против `>=` — оба варианта валидны как дизайн, но код должен выбрать один
    # и держаться его). Выбор: TTL включителен — ровно в момент clock() == expires_at операция
    # ещё действительна, `take` не бросает.
    часы = ЧасыЗаглушка(0.0)
    store = PendingStore(ttl_s=600, clock=часы)
    await store.put(операция(expires_at=600.0))

    часы.t = 600.0  # ровно граница

    взятая = await store.take("p1", "sess-1")
    assert взятая.pending_id == "p1"


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
    # grace_s=0: эта проверка про базовую механику purge, не про запас Ruling 41 (тот проверяют
    # отдельные тесты ниже с явным grace_s).
    часы = ЧасыЗаглушка(0.0)
    store = PendingStore(ttl_s=600, clock=часы, grace_s=0)
    await store.put(операция(pending_id="p1", expires_at=600.0))
    await store.put(операция(pending_id="p2", expires_at=1200.0))

    часы.сдвинуть(700.0)
    удалено = await store.purge()

    assert удалено == 1
    with pytest.raises(WriteError):
        await store.take("p1", "sess-1")
    оставшаяся = await store.take("p2", "sess-1")
    assert оставшаяся.pending_id == "p2"


# --- PendingStore: Ruling 41 (запас purge перед удалением) -------------------------------------


async def test_ruling_41_purge_не_удаляет_раньше_запаса():
    # Ruling 41 (правка до ревью): purge() удаляет операцию не раньше, чем expires_at + grace_s,
    # а не сразу по истечении expires_at — запас на длительность запроса к 1С между take() и
    # finish(), чтобы purge не мог удалить операцию, которую в этот момент выполняет commit
    # (иначе Ruling 40 не успевает продлить expires_at — см. docstring finish/purge).
    часы = ЧасыЗаглушка(0.0)
    store = PendingStore(ttl_s=600, clock=часы, grace_s=300)
    await store.put(операция(expires_at=600.0))

    часы.t = 601.0  # expires_at + 1 — TTL истёк, но запас (300 с) ещё далеко не выработан

    assert await store.purge() == 0


async def test_ruling_41_purge_удаляет_ровно_на_границе_expires_at_плюс_grace_s():
    часы = ЧасыЗаглушка(0.0)
    store = PendingStore(ttl_s=600, clock=часы, grace_s=300)
    await store.put(операция(expires_at=600.0))

    часы.t = 900.0  # ровно expires_at + grace_s (600 + 300) — граница включительна

    assert await store.purge() == 1


async def test_ruling_41_take_считает_срок_по_expires_at_без_запаса_purge_pending():
    # take() не пользуется grace_s вовсе — запас Ruling 41 продлевает только жизнь записи в
    # сторе для purge, не окно, которое видит take(). На expires_at + 1 pending-операция уже
    # pending_expired.
    часы = ЧасыЗаглушка(0.0)
    store = PendingStore(ttl_s=600, clock=часы, grace_s=300)
    await store.put(операция(pending_id="p1", expires_at=600.0))

    часы.t = 601.0  # expires_at + 1
    with pytest.raises(WriteError) as отказ_pending:
        await store.take("p1", "sess-1")
    assert отказ_pending.value.code == "pending_expired"


async def test_ruling_41_take_считает_срок_по_expires_at_без_запаса_purge_выполненная():
    # Тот же принцип для выполненной операции: отдельный стор и отдельные часы, чтобы не
    # отматывать время назад в середине теста (monotonic-часы этого не могли бы в проде).
    # finish при t=0 не продлевает expires_at (Ruling 40: max(600, 0+600) == 600), поэтому на
    # той же границе expires_at + 1 take отвечает pending_unknown, а не pending_expired.
    часы = ЧасыЗаглушка(0.0)
    store = PendingStore(ttl_s=600, clock=часы, grace_s=300)
    await store.put(операция(pending_id="p2", expires_at=600.0))
    await store.finish("p2", status="committed", result="[[ok:done]]")

    часы.t = 601.0  # expires_at + 1
    with pytest.raises(WriteError) as отказ_done:
        await store.take("p2", "sess-1")
    assert отказ_done.value.code == "pending_unknown"


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


def test_request_не_участвует_в_сравнении_и_не_течёт_через_diff_pytest():
    # Находка ревью (раунд 2, Important): field(repr=False) НЕ закрывает pytest'овский diff при
    # падении `assert a == b` — pytest обходит dataclasses.fields(), а не __repr__, и без
    # compare=False разница в request печаталась бы прямо в вывод теста. Проверяем и то, что две
    # операции, отличающиеся ТОЛЬКО request, равны, и что сама разница не всплывает — если бы
    # compare=False не было, обе операции ниже были бы НЕ равны (провал этого assert), а не только
    # печатался бы secret в diff.
    a = операция(request={"method": "PATCH", "json": {"ИНН": "СЕКРЕТНЫЙ_ИНН_777"}})
    b = операция(request={"method": "PATCH", "json": {"ИНН": "ДРУГОЕ_ЗНАЧЕНИЕ"}})

    assert a == b


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


def test_лимит_метка_ровно_на_границе_окна_вытесняется():
    # Находка ревью (раунд 2, Minor): граница окна (`timestamp == now - window_s`) не была
    # закреплена тестом — существующий тест окна сдвигает часы за границу (600.1), а не ровно на
    # неё. Выбор: окно исключает свою левую границу — метка, сделанная ровно `window_s` назад,
    # уже вытеснена (не считается «в окне»), поэтому 20-я метка на этой границе не блокирует
    # 21-й коммит.
    часы = ЧасыЗаглушка(0.0)
    лимитер = CommitLimiter(window_s=600, clock=часы)

    лимитер.check_and_count("sess-1", limit=1)  # метка на t=0
    часы.t = 600.0  # ровно now - window_s для будущей проверки: 600 - 600 = 0

    лимитер.check_and_count("sess-1", limit=1)  # не должно бросить — прежняя метка вытеснена


def test_окна_разных_сессий_независимы():
    часы = ЧасыЗаглушка(0.0)
    лимитер = CommitLimiter(window_s=600, clock=часы)

    for _ in range(20):
        лимитер.check_and_count("sess-1", limit=20)

    лимитер.check_and_count("sess-2", limit=20)  # своя сессия — свой счётчик, не должно бросить
