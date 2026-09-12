"""Разрешения записи: строгий порядок проверок SPEC §7.1, инвариант 4 `AGENTS.md`.

Проверка идёт **до** создания pending-операции (до всякого обращения к 1С за текущим
состоянием) — не вежливость, а инвариант: пишущий тул не должен ходить в 1С только для того,
чтобы затем отказать по правам. Отсюда и требование строгого порядка: первая сработавшая
проверка отдаёт код и подсказку, остальные не выполняются вовсе — если поменять две проверки
местами, поменяется код или подсказка отказа на пограничных случаях (см. тесты порядка в
`tests/unit/test_write_permissions.py`), а это уже наблюдаемое поведение для модели.

Регистры и понятие «независимый» (SPEC §7.1, CONTEXT.md «Независимый регистр»): независимым
бывает только регистр сведений без регистратора в ключе (`is_independent_register` из индекса,
`odata1c.index.edmx`) — у любого другого набора записей регистра (накопления, бухгалтерии,
расчёта, или регистра сведений с регистратором) регистратор обязателен, и прямая запись минуя
его отказывает всегда на шаге 4, каким бы ни было значение `register_direct_write`. Флаг шага 3
тем не менее не бесполезен: он определяет ТЕКСТ отказа для этого же набора сущностей — выключен →
отказ называет сам флаг (шаг 3, «включите permissions.register_direct_write»); включён → отказ
называет настоящую причину (шаг 4, «регистр меняется через документ-регистратор») — то есть
рассказывает администратору базы, что включать флаг для этой сущности бессмысленно.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Literal

from odata1c.config.models import BaseConfig
from odata1c.index.repository import EntityDescription
from odata1c.write.errors import WriteError

Операция = Literal["create", "update", "mark_for_deletion", "action", "undo"]

# Действия документов, разрешённые в первой поставке (решение 6 плана M2, задача 6) и закрытые
# флагом `permissions.post_documents`. Отказ на прочих действиях (`action_unknown`) — забота
# задачи 6 (`WriteService.action`): туда действие сначала проверяется по индексу (`$metadata`),
# сюда попадают уже только объявленные. `check_write` не знает, объявлено ли действие вообще —
# это вопрос индекса, а не прав, поэтому здесь неизвестное имя просто не совпадёт ни с одним
# условием шага 3 и пройдёт дальше без отказа (шаг 3 про эти два действия — не про все остальные).
_ДЕЙСТВИЯ_ПРОВЕДЕНИЯ = ("Post", "Unpost")


def _отказ_флага(base: BaseConfig, флаг: str, message: str) -> WriteError:
    return WriteError(
        "permission_denied",
        message,
        hint=f"включите permissions.{флаг}: true в bases.yaml, раздел «{base.name}»",
    )


def _запись_зависимого_регистра(entity: EntityDescription) -> bool:
    """Набор записей любого регистра с обязательным регистратором — независимо от вида
    (сведений/накопления/бухгалтерии/расчёта). `is_independent_register` истинно только у
    регистра сведений без регистратора (см. докстринг модуля); для всех остальных наборов
    записей регистров прямая запись отказывает на шаге 4 безусловно."""
    return entity.is_records and not entity.is_independent_register


def check_write(
    base: BaseConfig,
    entity: EntityDescription,
    op: Операция,
    fields: Iterable[str] = (),
    *,
    action: str | None = None,
    hidden: Callable[[str], bool],
) -> None:
    """Отказывает `WriteError`, если запись запрещена; иначе не возвращает ничего (SPEC §7.1).

    `fields` — имена полей тела записи (не значения: см. `field_write_denied` ниже — отказ
    называет поле, но не то, что в него пишут). `action` — имя действия документа для
    `op="action"` (`Post`/`Unpost`); для прочих `op` не используется. `hidden` — тот же
    предикат, что видит чтение (`BaseGate.is_hidden`): скрытая сущность отказывает одинаково
    для записи и для чтения, чтобы её существование не раскрывалось разницей в кодах ошибок.
    """
    # Шаг 1: скрытая сущность — тот же текст, что у чтения (odata1c/tools/service.py), без
    # подсказки: подсказка «как записать» подтвердила бы то, что гейт пытается спрятать.
    if hidden(entity.name):
        raise WriteError("entity_hidden", f"сущность «{entity.name}» скрыта политикой гейта")

    # Шаг 2: база целиком только для чтения — это раньше любого флага операции: нет смысла
    # объяснять про `post_documents`, если запись в базу выключена вообще.
    if not base.write:
        raise WriteError(
            "base_read_only",
            f"база «{base.name}» открыта только для чтения",
            hint=f"включите запись: bases.yaml, раздел «{base.name}» → write: true",
        )

    # Шаг 3: флаг конкретной операции. Три условия ниже взаимоисключающие по `op`/атрибутам
    # сущности (для данного вызова сработает не больше одного), порядок между ними поэтому не
    # наблюдаем — записан в порядке брифа для читаемости.
    if op == "action" and action in _ДЕЙСТВИЯ_ПРОВЕДЕНИЯ and not base.permissions.post_documents:
        raise _отказ_флага(
            base,
            "post_documents",
            f"действие «{action}» запрещено: проведение документов выключено",
        )
    if op == "mark_for_deletion" and not base.permissions.mark_deletion:
        raise _отказ_флага(
            base, "mark_deletion", "пометка удаления запрещена: permissions.mark_deletion выключен"
        )
    if (
        op in ("create", "update")
        and entity.is_records
        and not entity.is_independent_register
        and not base.permissions.register_direct_write
    ):
        raise _отказ_флага(
            base,
            "register_direct_write",
            f"прямая запись в регистр «{entity.name}» запрещена: "
            "permissions.register_direct_write выключен",
        )

    # Шаг 4: виртуальная таблица и запись зависимого регистра — отказ безусловный, флаг шага 3
    # тут уже не спрашивается (см. докстринг модуля). Дойти сюда можно двумя путями:
    # `register_direct_write` включён (шаг 3 пропустил) или сущность — виртуальная таблица (шаг 3
    # её не касается вовсе, `is_records` у неё `False`).
    if entity.is_virtual:
        raise WriteError(
            "permission_denied",
            f"виртуальная таблица «{entity.name}» не принимает запись",
            hint="виртуальные таблицы (Balance, Turnovers, …) — это агрегаты для чтения, "
            "своих записей у них нет",
        )
    if _запись_зависимого_регистра(entity):
        raise WriteError(
            "permission_denied",
            f"регистр «{entity.name}» подчинён документу-регистратору",
            hint="измените документ-регистратор — регистр обновится проведением",
        )

    # Шаг 5: список сущностей базы. `deny_entities` проверяется первым: он приоритетнее пустого
    # `allow_entities` (пустой список — «без ограничения», а не «ничего нельзя»).
    if entity.name in base.permissions.deny_entities:
        raise WriteError(
            "permission_denied",
            f"запись в сущность «{entity.name}» запрещена политикой базы",
            hint=f"уберите «{entity.name}» из permissions.deny_entities в bases.yaml",
        )
    if base.permissions.allow_entities and entity.name not in base.permissions.allow_entities:
        raise WriteError(
            "permission_denied",
            f"запись разрешена только в перечисленные сущности, «{entity.name}» среди них нет",
            hint=f"добавьте «{entity.name}» в permissions.allow_entities в bases.yaml",
        )

    # Шаг 6: запрещённые поля. Называем поле — не значение: сюда и не передаётся ничего, кроме
    # имён (`fields: Iterable[str]`), так что назвать значение отказ не может по построению.
    for имя_поля in fields:
        if f"{entity.name}.{имя_поля}" in base.permissions.deny_fields:
            raise WriteError(
                "field_write_denied",
                f"поле «{имя_поля}» запрещено к записи политикой базы",
                hint=f"уберите «{entity.name}.{имя_поля}» из permissions.deny_fields, "
                "чтобы разрешить запись",
            )
