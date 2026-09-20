"""Разрешения записи: строгий порядок проверок SPEC §7.1 (инвариант 4 — до pending-операции).

Порядок (первый отказ побеждает): 1) сущность скрыта гейтом → `entity_hidden`; 2) база только для
чтения → `base_read_only`; 3) регистр, подчинённый регистратору (Ruling 57), и флаги операций
(`post_documents`/`mark_deletion`/`independent_register_delete`) → `permission_denied`; 4)
виртуальная таблица — отказ всегда → `permission_denied`; 5) `deny_entities`/`allow_entities` →
`permission_denied`; 6) поле из `deny_fields` → `field_write_denied`.

Ruling 57 (2026-09-13, находка раунда 3 задачи 6) сузил Ruling 39: регистр шаг 3 узнаёт по виду
сущности (`kind`), а не по `is_records` — основной набор `AccumulationRegister_X` (ключ
`Recorder`) проходил мимо флага. Запись в зависимый регистр (или основной набор/`…_RecordType`
любого регистра) отклоняется при любом `register_direct_write` — движения формирует проведение
документа. Ruling 60 (И-3 итогового ревью M2) закрыл до отдельной поставки и независимый регистр
сведений; M3b (задача 7, проба P9, `docs/probes/P9-write-forms.md`) открывает его: `create`/`update`
идут по одному `write: true`, `delete_record` (физическое удаление, единственное такое во всём
шлюзе) — ещё и по `independent_register_delete`, `mark_for_deletion` записи регистра — не запрет
разрешений, а неверный запрос (`params_invalid`, нет пометки удаления вовсе).
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


РЕГИСТРЫ = [
    # Основной набор (ключ `Recorder`): у него `is_records=False` — прежний шаг 3 его не видел.
    pytest.param(
        сущность("AccumulationRegister_ТоварыНаСкладах", "AccumulationRegister"),
        id="накопления-основной-набор",
    ),
    pytest.param(
        сущность(
            "AccumulationRegister_ТоварыНаСкладах_RecordType",
            "AccumulationRegister",
            is_records=True,
        ),
        id="накопления-RecordType",
    ),
    pytest.param(
        сущность("InformationRegister_СтоимостьТоваров", "InformationRegister"),
        id="сведений-с-регистратором-основной-набор",
    ),
    pytest.param(
        сущность(
            "InformationRegister_СтоимостьТоваров_RecordType",
            "InformationRegister",
            is_records=True,
        ),
        id="сведений-с-регистратором-RecordType",
    ),
    pytest.param(
        сущность("AccountingRegister_Хозрасчетный", "AccountingRegister"),
        id="бухгалтерии",
    ),
    pytest.param(
        сущность("CalculationRegister_Начисления", "CalculationRegister"),
        id="расчёта",
    ),
]


@pytest.mark.parametrize("флаг", [False, True], ids=["без-флага", "с-флагом"])
@pytest.mark.parametrize("op", ["create", "update", "mark_for_deletion", "delete_record"])
@pytest.mark.parametrize("e", РЕГИСТРЫ)
def test_Ruling_57_регистр_с_регистратором_отклоняется_при_любом_флаге(e, op, флаг):
    """Ruling 57: регистр узнаётся по виду сущности; запись в регистр, подчинённый регистратору,
    не поддерживается ни при каком `register_direct_write` — движения формирует проведение
    документа. POST набора с `Recorder` мог бы переписать движения документа, а проба P8 этого не
    проверяла. M3b (задача 7) не меняет это правило: открыт только независимый регистр сведений
    (`test_независимому_регистру_хватает_write_true` и соседние ниже)."""
    b = база(
        permissions=Permissions(
            register_direct_write=флаг,
            mark_deletion=True,
            post_documents=True,
            independent_register_delete=флаг,
        )
    )
    with pytest.raises(WriteError) as инфо:
        проверить(b, e, op)
    assert инфо.value.code == "permission_denied"
    assert e.name in инфо.value.message
    assert "не поддерживается" in инфо.value.hint
    assert "odata1c_action" in инфо.value.hint and "Post" in инфо.value.hint


# --- M3b задача 7: независимый регистр сведений открыт (Ruling 60 закрыт этой поставкой) -------


def _независимый_регистр(name: str = "InformationRegister_КурсыВалют") -> EntityDescription:
    return сущность(name, "InformationRegister", is_independent_register=True)


@pytest.mark.parametrize("операция", ["create", "update"])
def test_независимому_регистру_хватает_write_true(операция):
    """Ruling 60 закрывал запись ЛЮБОГО регистра «до отдельной поставки». M3b открывает ровно
    независимый регистр сведений: ему хватает `write: true`, как и говорил SPEC §7.1 до Ruling 60
    — без `register_direct_write` и без `independent_register_delete`."""
    b = база(
        permissions=Permissions(register_direct_write=False, independent_register_delete=False)
    )
    проверить(b, _независимый_регистр(), операция)


def test_delete_record_требует_флага_independent_register_delete():
    b = база(permissions=Permissions(independent_register_delete=False))
    with pytest.raises(WriteError) as отказ:
        проверить(b, _независимый_регистр(), "delete_record")
    assert отказ.value.code == "permission_denied"
    assert "independent_register_delete" in отказ.value.hint


def test_delete_record_с_флагом_разрешён():
    b = база(permissions=Permissions(independent_register_delete=True))
    проверить(b, _независимый_регистр(), "delete_record")


def test_mark_for_deletion_независимого_регистра_params_invalid_не_permission_denied():
    """У записи регистра нет `DeletionMark` вовсе: это не запрет по разрешениям, а неверный
    запрос — код обязан отличаться от `permission_denied` и подсказывать `odata1c_delete_record`."""
    with pytest.raises(WriteError) as отказ:
        проверить(база(), _независимый_регистр(), "mark_for_deletion")
    assert отказ.value.code == "params_invalid"
    assert "odata1c_delete_record" in отказ.value.hint


def test_delete_record_на_зависимом_регистре_permission_denied():
    """Регистр с регистратором закрыт для `delete_record` так же, как для остальных операций
    (Ruling 57 не меняется этой поставкой) — даже когда `independent_register_delete` включён."""
    b = база(permissions=Permissions(register_direct_write=True, independent_register_delete=True))
    e = сущность("AccumulationRegister_ТоварыНаСкладах", "AccumulationRegister", is_records=True)
    with pytest.raises(WriteError) as отказ:
        проверить(b, e, "delete_record")
    assert отказ.value.code == "permission_denied"
    assert "не поддерживается" in отказ.value.hint


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


def test_запрет_записи_в_зависимый_регистр_permission_denied():
    b = база(permissions=Permissions(register_direct_write=False))
    e = сущность("AccumulationRegister_ТоварыНаСкладах", "AccumulationRegister", is_records=True)
    with pytest.raises(WriteError) as инфо:
        проверить(b, e, "create")
    assert инфо.value.code == "permission_denied"
    assert "odata1c_action" in инфо.value.hint


def test_виртуальная_таблица_отказ_всегда():
    """Вид виртуальной таблицы — вид её регистра (`kind == "AccumulationRegister"`), но
    отказывает ей шаг 4 своим текстом, а не правило зависимого регистра шага 3 (Ruling 57)."""
    b = база(permissions=Permissions(register_direct_write=True))
    e = сущность(
        "AccumulationRegister_ТоварыНаСкладах_Balance",
        "AccumulationRegister",
        is_virtual=True,
    )
    with pytest.raises(WriteError) as инфо:
        проверить(b, e, "update")
    assert инфо.value.code == "permission_denied"
    assert "виртуальная таблица" in инфо.value.message
    assert "регистратору" not in инфо.value.hint


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
            id="шаг3-зависимый-регистр",
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
