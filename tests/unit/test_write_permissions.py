"""Разрешения записи: строгий порядок проверок SPEC §7.1 (инвариант 4 — до pending-операции).

Порядок (первый отказ побеждает): 1) сущность скрыта гейтом → `entity_hidden`; 2) база только для
чтения → `base_read_only`; 3) флаг операции (`post_documents`/`mark_deletion`/
`register_direct_write` — единственный рубеж для прямой записи в набор записей любого регистра,
независимого или зависимого) → `permission_denied`; 4) виртуальная таблица — отказ всегда, флаг
не спасает → `permission_denied` (Ruling 39: это единственный безусловный случай — регистр таким
не является, его открывает флаг шага 3); 5) `deny_entities`/`allow_entities` → `permission_denied`;
6) поле из `deny_fields` → `field_write_denied`.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from odata1c.config.models import BaseConfig, Permissions
from odata1c.index.repository import EntityDescription
from odata1c.write.errors import WriteError
from odata1c.write.permissions import check_write

# Разрешения роли `dev` — как их раскладывает `loader.УМОЛЧАНИЯ_РОЛЕЙ` (не сам `check_write`: он
# принимает уже готовый `BaseConfig` и о ролях не знает — умолчания роли накладывает загрузчик
# настроек до вызова). Здесь — те же значения, чтобы тест «разрешено на dev» проверял то, что
# реально приходит из `bases.yaml`, а не облегчённые умолчания `Permissions()`.
РАЗРЕШЕНИЯ_DEV = Permissions(
    post_documents=True,
    mark_deletion=True,
    independent_register_delete=True,
    register_direct_write=True,
    commit_limit=0,
)


def база(
    *,
    write: bool = True,
    permissions: Permissions | None = None,
    name: str = "trade_dev",
) -> BaseConfig:
    return BaseConfig(
        name=name,
        label=name,
        url="https://host/base/odata/standard.odata/",
        user="user",
        role="dev",
        write=write,
        permissions=permissions if permissions is not None else РАЗРЕШЕНИЯ_DEV,
    )


def сущность(
    name: str = "Catalog_Контрагенты",
    kind: str = "Catalog",
    *,
    is_records: bool = False,
    is_virtual: bool = False,
    is_independent_register: bool = False,
    is_tabular_part: bool = False,
) -> EntityDescription:
    return EntityDescription(
        name=name,
        kind=kind,
        russian_kind="Справочник",
        parent_entity=None,
        is_tabular_part=is_tabular_part,
        is_records=is_records,
        is_virtual=is_virtual,
        virtual_kind="Balance" if is_virtual else None,
        key_fields=["Ref_Key"],
        description_field="Description",
        fields=[],
        children=[],
        actions=[],
        members=[],
        navigations={},
        is_independent_register=is_independent_register,
    )


def _не_скрыта(_имя: str) -> bool:
    return False


def _скрыта(_имя: str) -> bool:
    return True


def проверить(
    b: BaseConfig,
    e: EntityDescription,
    op: str,
    fields: Iterable[str] = (),
    *,
    action: str | None = None,
    hidden=_не_скрыта,
) -> None:
    check_write(b, e, op, fields, action=action, hidden=hidden)


# --- по умолчанию разрешено -------------------------------------------------------------------


def test_на_роли_dev_по_умолчанию_разрешено():
    """Каталог, обычное изменение поля, ничего не запрещено — `check_write` ничего не бросает."""
    проверить(база(), сущность(), "update", ["Comment"])


def test_независимый_регистр_сведений_на_create_при_register_direct_write_false_разрешено():
    """Независимый регистр сведений пишется по одному `write: true`, флаг `register_direct_write`
    его не касается вовсе (SPEC §7.1) — в отличие от любого набора записей регистра с
    обязательным регистратором (накопления, бухгалтерии, расчёта, регистра сведений с
    регистратором), которому этот же флаг нужен явно включённым (Ruling 39, тесты
    `test_накопления_с_флагом_включён_разрешено` и
    `test_зависимый_регистр_сведений_с_флагом_включён_разрешён`)."""
    b = база(permissions=Permissions(register_direct_write=False))
    e = сущность(
        "InformationRegister_КурсыВалют",
        "InformationRegister",
        is_records=True,
        is_independent_register=True,
    )
    проверить(b, e, "create")


def test_накопления_с_флагом_включён_разрешено():
    """Ruling 39: `register_direct_write` — единственный рубеж прямой записи в набор записей
    регистра, включая накопление (у него нет понятия «независимый/зависимый» вовсе — регистратор
    обязателен всегда, CONTEXT.md «Независимый регистр»). Включённый флаг разрешает запись, шаг 4
    («отказ всегда») его не касается — он только про виртуальные таблицы."""
    b = база(permissions=Permissions(register_direct_write=True))
    e = сущность("AccumulationRegister_ТоварыНаСкладах", "AccumulationRegister", is_records=True)
    проверить(b, e, "update")


def test_зависимый_регистр_сведений_с_флагом_включён_разрешён():
    """Тот же рубеж — для регистра сведений с регистратором («зависимого» в терминах CONTEXT.md):
    включённый `register_direct_write` разрешает прямую запись, несмотря на регистратор."""
    b = база(permissions=Permissions(register_direct_write=True))
    e = сущность(
        "InformationRegister_СтоимостьТоваров",
        "InformationRegister",
        is_records=True,
        is_independent_register=False,
    )
    проверить(b, e, "update")


def test_зависимый_регистр_сведений_с_флагом_выключен_permission_denied():
    b = база(permissions=Permissions(register_direct_write=False))
    e = сущность(
        "InformationRegister_СтоимостьТоваров",
        "InformationRegister",
        is_records=True,
        is_independent_register=False,
    )
    with pytest.raises(WriteError) as инфо:
        проверить(b, e, "update")
    assert инфо.value.code == "permission_denied"
    assert "register_direct_write" in инфо.value.hint


# --- каждый код отказа -----------------------------------------------------------------------


def test_скрытая_сущность_entity_hidden():
    with pytest.raises(WriteError) as инфо:
        проверить(база(), сущность(), "update", hidden=_скрыта)
    assert инфо.value.code == "entity_hidden"


def test_база_только_для_чтения_base_read_only():
    with pytest.raises(WriteError) as инфо:
        проверить(база(write=False), сущность(), "update")
    assert инфо.value.code == "base_read_only"


def test_запрет_проведения_post_documents():
    b = база(permissions=Permissions(post_documents=False))
    with pytest.raises(WriteError) as инфо:
        check_write(
            b,
            сущность("Document_ПересчетТоваров", "Document"),
            "action",
            action="Post",
            hidden=_не_скрыта,
        )
    assert инфо.value.code == "permission_denied"
    assert "post_documents" in инфо.value.hint


def test_запрет_пометки_удаления_mark_deletion():
    b = база(permissions=Permissions(mark_deletion=False))
    with pytest.raises(WriteError) as инфо:
        проверить(b, сущность(), "mark_for_deletion")
    assert инфо.value.code == "permission_denied"
    assert "mark_deletion" in инфо.value.hint


def test_запрет_прямой_записи_в_регистр_register_direct_write():
    b = база(permissions=Permissions(register_direct_write=False))
    e = сущность("AccumulationRegister_ТоварыНаСкладах", "AccumulationRegister", is_records=True)
    with pytest.raises(WriteError) as инфо:
        проверить(b, e, "create")
    assert инфо.value.code == "permission_denied"
    assert "register_direct_write" in инфо.value.hint


def test_виртуальная_таблица_отказ_всегда():
    b = база(permissions=Permissions(register_direct_write=True))
    e = сущность(
        "AccumulationRegister_ТоварыНаСкладах_Balance",
        "AccumulationRegister",
        is_virtual=True,
    )
    with pytest.raises(WriteError) as инфо:
        проверить(b, e, "update")
    assert инфо.value.code == "permission_denied"


def test_deny_entities_permission_denied():
    b = база(permissions=Permissions(deny_entities=["Catalog_Пользователи"]))
    with pytest.raises(WriteError) as инфо:
        проверить(b, сущность("Catalog_Пользователи"), "update")
    assert инфо.value.code == "permission_denied"


def test_allow_entities_не_пуст_и_не_содержит_permission_denied():
    b = база(permissions=Permissions(allow_entities=["Catalog_Номенклатура"]))
    with pytest.raises(WriteError) as инфо:
        проверить(b, сущность("Catalog_Контрагенты"), "update")
    assert инфо.value.code == "permission_denied"


def test_allow_entities_содержит_разрешено():
    b = база(permissions=Permissions(allow_entities=["Catalog_Контрагенты"]))
    проверить(b, сущность("Catalog_Контрагенты"), "update")


def test_field_write_denied_называет_поле_но_не_значение():
    b = база(permissions=Permissions(deny_fields=["Catalog_Контрагенты.ИНН"]))
    with pytest.raises(WriteError) as инфо:
        проверить(b, сущность(), "update", ["ИНН"])
    assert инфо.value.code == "field_write_denied"
    assert "ИНН" in инфо.value.message
    # Значения в вызов не передаются вовсе (сигнатура — Iterable[str] имён полей), поэтому текст
    # отказа не может назвать значение по построению; на всякий случай сверяем явно с меткой,
    # которая точно не была бы взята из имени поля.
    assert "секретное-значение" not in инфо.value.message


# --- порядок: первый отказ побеждает ----------------------------------------------------------


def test_порядок_скрытая_и_в_deny_entities_побеждает_entity_hidden():
    b = база(permissions=Permissions(deny_entities=["Catalog_Контрагенты"]))
    with pytest.raises(WriteError) as инфо:
        проверить(b, сущность(), "update", hidden=_скрыта)
    assert инфо.value.code == "entity_hidden"


@pytest.mark.parametrize(
    ("b", "e", "op", "fields", "action"),
    [
        pytest.param(база(write=False), сущность(), "update", (), None, id="шаг2-read_only"),
        pytest.param(
            база(permissions=Permissions(post_documents=False)),
            сущность("Document_Заказ", "Document"),
            "action",
            (),
            "Post",
            id="шаг3-post_documents",
        ),
        pytest.param(
            база(permissions=Permissions(mark_deletion=False)),
            сущность(),
            "mark_for_deletion",
            (),
            None,
            id="шаг3-mark_deletion",
        ),
        pytest.param(
            база(permissions=Permissions(register_direct_write=False)),
            сущность("AccumulationRegister_Остатки", "AccumulationRegister", is_records=True),
            "create",
            (),
            None,
            id="шаг3-register_direct_write",
        ),
        pytest.param(
            база(),
            сущность(
                "AccumulationRegister_Остатки_Balance", "AccumulationRegister", is_virtual=True
            ),
            "update",
            (),
            None,
            id="шаг4-виртуальная",
        ),
        pytest.param(
            база(permissions=Permissions(deny_fields=["Catalog_Контрагенты.ИНН"])),
            сущность(),
            "update",
            ("ИНН",),
            None,
            id="шаг6-deny_fields",
        ),
    ],
)
def test_порядок_скрытая_сущность_побеждает_любой_следующий_отказ(b, e, op, fields, action):
    """Скрытая сущность отвечает `entity_hidden` раньше всего: любой другой код для неё —
    `base_read_only` на базе `prod`, имя флага, «виртуальная таблица» — подтвердил бы, что
    сущность существует. Каждый случай сначала проверен без скрытия: отказ в нём настоящий,
    иначе тест ловил бы не порядок, а пустой случай."""
    with pytest.raises(WriteError) as без_скрытия:
        проверить(b, e, op, fields, action=action)
    assert без_скрытия.value.code != "entity_hidden"
    with pytest.raises(WriteError) as инфо:
        проверить(b, e, op, fields, action=action, hidden=_скрыта)
    assert инфо.value.code == "entity_hidden"


def test_порядок_read_only_и_deny_fields_побеждает_base_read_only():
    b = база(write=False, permissions=Permissions(deny_fields=["Catalog_Контрагенты.ИНН"]))
    with pytest.raises(WriteError) as инфо:
        проверить(b, сущность(), "update", ["ИНН"])
    assert инфо.value.code == "base_read_only"


def test_порядок_read_only_и_флаг_операции_побеждает_base_read_only():
    """Мутационная проверка задачи 2 (Step 4 плана): если поменять местами шаги 2 и 3 — база
    только для чтения одновременно с выключенным `mark_deletion` перестанет ловиться как
    `base_read_only` и станет `permission_denied`. Здесь фиксируется правильный порядок."""
    b = база(write=False, permissions=Permissions(mark_deletion=False))
    with pytest.raises(WriteError) as инфо:
        проверить(b, сущность(), "mark_for_deletion")
    assert инфо.value.code == "base_read_only"


def test_порядок_allow_entities_и_deny_fields_побеждает_permission_denied():
    b = база(
        permissions=Permissions(
            allow_entities=["Catalog_Номенклатура"],
            deny_fields=["Catalog_Контрагенты.ИНН"],
        )
    )
    with pytest.raises(WriteError) as инфо:
        проверить(b, сущность("Catalog_Контрагенты"), "update", ["ИНН"])
    assert инфо.value.code == "permission_denied"
