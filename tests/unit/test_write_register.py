"""Запись независимого регистра сведений (проект M3b §5.4, SPEC §7.1, §7.3; задача 7 плана M3b).

Единственная сущность 1С, у которой есть физическое удаление записи (`odata1c_delete_record`);
ключ — измерения (у периодического ещё `Period`), `DeletionMark` нет, `Ref_Key` нет. Разрешения
(шаг 3 SPEC §7.1) — `tests/unit/test_write_permissions.py`; путь `create`/`update` через полный
`ToolService` и подготовку — `tests/unit/test_write_prepare_create_action.py` и
`test_write_prepare_update.py`; полный жизненный цикл с `commit` — `test_write_commit.py` и
`test_write_undo_journal.py`. Здесь — сфокусированные проверки задачи 7, которых нет ни в одном
из перечисленных файлов: недостающий ключ в теле `create`, отпечаток без `DataVersion` при
`commit`, и что классованное поле-ресурс записи регистра не утекает мимо превью и журнала.

Поддельная 1С и фикстуры — из `test_write_commit.py` (`Одинс` с состоянием, `Среда`, `_дом`), тем
же приёмом, что у соседних файлов задачи 7/8: своя область фикстур, без смешения с чужими тестами.
"""

import json

import httpx
import pytest
import respx
import test_write_commit as к
import yaml

from odata1c.gate.service import policy_path
from odata1c.registry.registry import SessionScope

КУРСЫ = к.КУРСЫ
ПУТЬ_КУРСА = к.ПУТЬ_КУРСА
КЛЮЧ_КУРСА = к.КЛЮЧ_КУРСА
ССЫЛКА_ВАЛЮТЫ = к.ССЫЛКА_ВАЛЮТЫ
РЕГИСТР_ИНН = к.РЕГИСТР_ИНН
ИНН = к.ИНН


@pytest.fixture
def дом(tmp_path, edmx_ut_real):
    return к._дом(tmp_path, edmx_ut_real)


@pytest.fixture
async def среда(дом, tmp_path):
    с = к.Среда(дом, tmp_path / "journal.sqlite")
    yield с
    await с.tools.aclose()


@pytest.fixture
def одинс():
    with respx.mock(assert_all_called=False) as router:
        for url in (к.URL_UT, к.URL_IDN, к.URL_LIM):
            router.get(url).mock(return_value=httpx.Response(200, json={"value": []}))
        yield к.Одинс(router)


# База `ut` из `test_write_commit.BASES_YAML` не несёт `independent_register_delete` (умолчание
# роли `prod` — `false`): `delete_record` и его последствия (превью с предупреждением, `commit`)
# нужен флаг. `дом` эту базу уже проиндексировал — только переписываем `bases.yaml`, тем же
# приёмом, что `пересоздать` в test_write_undo_journal.py.
BASES_РЕГИСТР_УДАЛЕНИЕ = к.BASES_YAML.replace(
    "    role: prod\n    write: true\n  idn:",
    "    role: prod\n    write: true\n    permissions:\n"
    "      independent_register_delete: true\n  idn:",
    1,
)
assert "independent_register_delete: true" in BASES_РЕГИСТР_УДАЛЕНИЕ


@pytest.fixture
async def среда_удаление(дом, tmp_path):
    (дом / "bases.yaml").write_text(BASES_РЕГИСТР_УДАЛЕНИЕ, encoding="utf-8")
    с = к.Среда(дом, tmp_path / "journal.sqlite")
    yield с
    await с.tools.aclose()


@pytest.fixture
async def среда_класс(tmp_path, edmx_ut_real):
    """Синтетический регистр задачи 7 (ключ — ИНН), но поле-РЕСУРС `Комментарий` классовано
    владельцем (`inn`) через `policy.yaml` — на живой УТ такого нет, но политика это позволяет
    (тот же приём, что `test_дата_токеном_в_строке_табличной_части_проверяется_после_раскрытия`,
    test_write_prepare_create_action.py). Флаг `independent_register_delete` включён — нужен для
    `commit` тела теста. Проверка: значение резервного поля-ресурса не выходит ни в превью
    `delete_record`, ни в журнал открытым — только токеном."""
    текст = edmx_ut_real.decode("utf-8")
    якорь_типа = f'<EntityType Name="{КУРСЫ}">'
    якорь_набора = f'<EntitySet Name="{КУРСЫ}"'
    текст = текст.replace(якорь_типа, к._ТИП_РЕГИСТРА_ИНН + якорь_типа, 1)
    текст = текст.replace(якорь_набора, к._НАБОР_РЕГИСТРА_ИНН + якорь_набора, 1)
    home = к._дом(tmp_path, текст.encode("utf-8"))
    путь = policy_path(home, "ut")
    политика = yaml.safe_load(путь.read_text(encoding="utf-8")) or {}
    политика.setdefault("fields", {})[f"{РЕГИСТР_ИНН}.Комментарий"] = "inn"
    путь.write_text(yaml.safe_dump(политика, allow_unicode=True), encoding="utf-8")
    (home / "bases.yaml").write_text(BASES_РЕГИСТР_УДАЛЕНИЕ, encoding="utf-8")
    с = к.Среда(home, tmp_path / "journal.sqlite")
    yield с
    await с.tools.aclose()


def _нет_записи(одинс: "к.Одинс") -> None:
    одинс.get.mock(
        return_value=httpx.Response(
            404, json={"odata.error": {"code": "9", "message": {"value": "нет"}}}
        )
    )


# ---------------------------------------------------------------------------------------------
# create: ключ в теле
# ---------------------------------------------------------------------------------------------


async def test_create_требует_все_поля_ключа_в_теле(среда, одинс):
    """Неполный ключ в теле `create` — `params_invalid` до 1С (ни один запрос не уходит): у
    OData 1С частичный составной ключ адресует выборку, а не запись, и POST по нему не строится.
    Имена НЕДОСТАЮЩИХ полей называются, значений в отказе нет."""
    отказ = к.ошибка(
        await среда.запись.create(
            SessionScope(),
            "s1",
            base="ut",
            entity=КУРСЫ,
            data={"Period": "2026-01-01T00:00:00", "Курс": 91.25, "Кратность": 1},
        )
    )

    assert отказ["code"] == "params_invalid"
    assert "Валюта_Key" in отказ["message"]
    assert одинс.get.call_count == 0 and одинс.записей == 0
    assert среда.стор._ops == {}


async def test_create_по_существующему_ключу_отвечает_record_exists(среда, одинс):
    """P9-2: повторный POST тем же ключом 1С отклоняет сама (HTTP 400, ни замещения, ни дубля),
    но шлюз проверяет существование записи GET-ом при подготовке и отвечает `record_exists` —
    отказ понятнее и раньше; POST в 1С не уходит (Ruling 105)."""
    одинс.положить(ПУТЬ_КУРСА, {**КЛЮЧ_КУРСА, "Курс": 90.5, "Кратность": 1})

    отказ = к.ошибка(
        await среда.запись.create(
            SessionScope(),
            "s1",
            base="ut",
            entity=КУРСЫ,
            data={**КЛЮЧ_КУРСА, "Курс": 91.25, "Кратность": 1},
        )
    )

    assert отказ["code"] == "record_exists"
    assert "odata1c_update" in отказ["hint"]
    assert среда.стор._ops == {} and одинс.post.call_count == 0


async def test_превью_create_предупреждает_о_недоступном_откате(среда, одинс):
    """Без `independent_register_delete` откат созданной записи (DELETE) выполнить нельзя —
    предупреждение об этом уже в превью `create`, до подтверждения пользователя."""
    _нет_записи(одинс)

    ответ = json.loads(
        await среда.запись.create(
            SessionScope(),
            "s1",
            base="ut",
            entity=КУРСЫ,
            data={**КЛЮЧ_КУРСА, "Курс": 91.25, "Кратность": 1},
        )
    )

    assert any("independent_register_delete" in п for п in ответ["warnings"])


# ---------------------------------------------------------------------------------------------
# delete_record: подготовка — только GET, превью «записи не будет»
# ---------------------------------------------------------------------------------------------


async def test_delete_record_готовит_превью_и_не_пишет_в_1с(среда_удаление, одинс):
    одинс.положить(ПУТЬ_КУРСА, {**КЛЮЧ_КУРСА, "Курс": 90.5, "Кратность": 1})

    ответ = json.loads(
        await среда_удаление.запись.delete_record(
            SessionScope(), "s1", base="ut", entity=КУРСЫ, key=КЛЮЧ_КУРСА
        )
    )

    assert ответ["pending_id"]
    assert ответ["preview"]["after"] == "записи не будет"
    assert одинс.get.call_count == 1 and одинс.записей == 0
    к.нет_реальных_значений(json.dumps(ответ, ensure_ascii=False))


async def test_delete_record_на_объекте_params_invalid(среда, одинс):
    """Не независимый регистр сведений — `params_invalid` с подсказкой на пометку удаления
    (у объекта физического удаления в шлюзе нет вовсе, инвариант 3). База `ut` без флага —
    отказ по форме сущности виден раньше, чем спросили про флаг (шаг 3 `check_write` его вообще
    не касается для объектов)."""
    отказ = к.ошибка(
        await среда.запись.delete_record(
            SessionScope(), "s1", base="ut", entity=к.КОНТРАГЕНТЫ, key=к.ССЫЛКА
        )
    )

    assert отказ["code"] == "params_invalid"
    assert "odata1c_mark_for_deletion" in отказ["hint"]
    assert одинс.get.call_count == 0 and одинс.записей == 0


# ---------------------------------------------------------------------------------------------
# Отпечаток без DataVersion — по содержимому (P9-5)
# ---------------------------------------------------------------------------------------------


async def test_отпечаток_записи_регистра_без_dataversion_pending_stale_при_расхождении(
    среда, одинс
):
    """P9-5: у записи независимого регистра сведений нет `DataVersion` — отпечаток `commit`
    строится по содержимому записи (SHA-256, `_отпечаток`). Запись изменилась между подготовкой и
    `commit` (курс правил кто-то ещё) — `pending_stale`, а не запись вслепую."""
    одинс.положить(ПУТЬ_КУРСА, {**КЛЮЧ_КУРСА, "Курс": 90.5, "Кратность": 1})
    подготовка = json.loads(
        await среда.запись.update(
            SessionScope(),
            "s1",
            base="ut",
            entity=КУРСЫ,
            key=КЛЮЧ_КУРСА,
            data={"Курс": 91.25},
        )
    )
    # Изменение «снаружи» — не через шлюз: `DataVersion` у записи регистра нет, расти нечему,
    # поэтому меняем ресурс напрямую в состоянии поддельной 1С (та же гонка, что и у объекта).
    одинс.объекты[ПУТЬ_КУРСА]["Кратность"] = 100

    отказ = к.ошибка(await к.выполнить(среда, подготовка["pending_id"], mechanism="claude_code"))

    assert отказ["code"] == "pending_stale"
    assert одинс.patch.call_count == 0


# ---------------------------------------------------------------------------------------------
# Классованное поле-ресурс: не утекает мимо delete_record и журнала
# ---------------------------------------------------------------------------------------------


КЛАССОВАННОЕ_ЗНАЧЕНИЕ = "7736050003"


async def test_delete_record_классованное_поле_ресурса_только_токеном(среда_класс, одинс):
    """Классованное поле-ресурс (`Комментарий`, класс `inn` по политике владельца) записи
    регистра — только токеном и в превью `delete_record`, и в журнале; реального значения нет
    нигде в ответах, `guard_replaced` тоже (маскировщик обязан справиться сам)."""
    среда = среда_класс
    ток = к.токен(среда.tools, ИНН, entity=РЕГИСТР_ИНН)
    одинс.положить(
        f"{РЕГИСТР_ИНН}(ИНН='{ИНН}')", {"ИНН": ИНН, "Комментарий": КЛАССОВАННОЕ_ЗНАЧЕНИЕ}
    )

    подготовка = json.loads(
        await среда.запись.delete_record(
            SessionScope(), "s1", base="ut", entity=РЕГИСТР_ИНН, key={"ИНН": ток}
        )
    )
    assert КЛАССОВАННОЕ_ЗНАЧЕНИЕ not in json.dumps(подготовка, ensure_ascii=False)
    assert "guard_replaced" not in json.dumps(подготовка, ensure_ascii=False)

    выполнено = await к.выполнить(среда, подготовка["pending_id"], mechanism="claude_code")
    ответ = json.loads(выполнено)

    assert КЛАССОВАННОЕ_ЗНАЧЕНИЕ not in выполнено and "guard_replaced" not in выполнено
    assert одинс.delete.call_count == 1
    журнал = среда.журнал(ответ["commit_id"])
    # Журнал — файл владельца, реальные значения в нём законны (инвариант 1 — про MCP).
    assert журнал.before["Комментарий"] == КЛАССОВАННОЕ_ЗНАЧЕНИЕ
