"""Разрешения записи: строгий порядок проверок SPEC §7.1, инвариант 4 `AGENTS.md`.

Проверка идёт **до** создания pending-операции (до всякого обращения к 1С за текущим
состоянием) — не вежливость, а инвариант: пишущий тул не должен ходить в 1С только для того,
чтобы затем отказать по правам. Отсюда и требование строгого порядка: первая сработавшая
проверка отдаёт код и подсказку, остальные не выполняются вовсе — если поменять две проверки
местами, поменяется код или подсказка отказа на пограничных случаях (см. тесты порядка в
`tests/unit/test_write_permissions.py`), а это уже наблюдаемое поведение для модели.

Регистры и понятие «независимый» (SPEC §7.1, CONTEXT.md «Независимый регистр»): независимым
бывает только регистр сведений без регистратора в ключе (`is_independent_register` из индекса,
`odata1c.index.edmx`). `register_direct_write` — единственный рубеж для прямой записи в набор
записей ЛЮБОГО регистра (сведений с регистратором, накопления, бухгалтерии, расчёта): включён —
разрешено (роль `dev` включает его по умолчанию), выключен — `permission_denied` с подсказкой,
что регистр обычно меняется через документ-регистратор, а прямую запись открывает этот же флаг.
Независимый регистр сведений этого флага не спрашивает вовсе — ему хватает `write: true` (шаг 3
его не касается).

Решение владельца (Ruling 39, 2026-09-12) — исправление собственной ошибки задачи 2: первая
редакция этого модуля отказывала записи в ЛЮБОЙ зависимый регистр безусловно, независимо от
`register_direct_write` (шаг 4), из-за чего флаг не разрешал вообще ничего. Авторитет — сам текст
SPEC §7.1 («`register_direct_write` закрывает POST/PATCH ко всем `*Register_*`, кроме
независимых регистров сведений»): флаг — рубеж, а не подсказка к безусловному отказу. Отказ
«всегда, независимо от флага» верен только для виртуальных таблиц (`is_virtual`: `Balance`,
`Turnovers`, `SliceLast` и подобные — вычисляемые выборки, в них не пишется ничего и никогда) —
это шаг 4 ниже.

Ruling 57 (2026-09-13, находка раунда 3 задачи 6) сужает Ruling 39, а не отменяет его. Шаг 3
узнавал регистр по `is_records`, а индекс ставит этот признак только наборам `…_RecordType`
(`odata1c.index.edmx`, `это_набор_записей`): основной набор `AccumulationRegister_X` (ключ
`Recorder`) проходил мимо `register_direct_write`, и `create` готовил `POST AccumulationRegister_X`
в базе без флага. Теперь регистр определяется по виду сущности (`kind` из `РЕГИСТР_ВИДЫ` —
основной набор, `…_RecordType`, наборы записей; виртуальные таблицы — шаг 4). И в первой
поставке запись (`create`/`update`/`mark_for_deletion`) в регистр, подчинённый регистратору,
отклоняется при любом флаге: POST набора с `Recorder` может переписать движения документа, проба
P8 этого не проверяла, а запись регистров в первую поставку владелец не заказывал. Движения
формирует проведение документа.

Ruling 60 (2026-09-13, И-3 итогового ревью M2) закрыл и независимый регистр сведений — «до
отдельной поставки»: запись в регистр сведений ни разу не проверялась на живой 1С, а если POST по
существующему набору измерений замещает запись (менеджер записи по умолчанию пишет с замещением),
прежние ресурсы терялись бы без «до» в журнале и без отката.

M3b (задача 7, 2026-09-20) закрывает этот вопрос пробой P9 (`docs/probes/P9-write-forms.md`) и
открывает ровно то, что Ruling 60 отложил — независимый регистр сведений, и ничего сверх: регистр,
подчинённый регистратору (Ruling 57), остаётся закрытым при любом значении `register_direct_write`
без исключений. Независимому регистру сведений (`is_independent_register`) достаточно `write: true`
для `create`/`update` — ровно то, что SPEC §7.1 говорил до Ruling 60; на физическое удаление записи
(`delete_record`, единственная операция шлюза с настоящим DELETE) нужен ещё флаг
`independent_register_delete` (умолчания ролей: `prod`/`test` — `false`, `dev` — `true`). Пометки
удаления у записи регистра нет вовсе — `mark_for_deletion` по ней не запрет по разрешениям, а
неверный запрос: `params_invalid`, а не `permission_denied`, с подсказкой на
`odata1c_delete_record`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Literal

from odata1c.config.models import BaseConfig
from odata1c.index.edmx import РЕГИСТР_ВИДЫ
from odata1c.index.repository import EntityDescription
from odata1c.write.errors import WriteError

Операция = Literal["create", "update", "mark_for_deletion", "delete_record", "action", "undo"]

# Действия документов, разрешённые в первой поставке (решение 6 плана M2, задача 6) и закрытые
# флагом `permissions.post_documents`. Публичное имя — потому что это ОДИН перечень на два места:
# флаг здесь и отказ `action_unknown` в `WriteService.action` (задача 6). Разойдись два списка,
# действие прошло бы проверку «разрешено в поставке» мимо флага. `check_write` не знает, объявлено
# ли действие вообще — это вопрос индекса, а не прав, поэтому неизвестное имя здесь не совпадёт ни
# с одним условием шага 3 и пройдёт дальше без отказа; отказывает ему `WriteService.action`.
ДЕЙСТВИЯ_ПРОВЕДЕНИЯ = ("Post", "Unpost")


def _отказ_флага(base: BaseConfig, флаг: str, message: str) -> WriteError:
    return WriteError(
        "permission_denied",
        message,
        hint=f"включите permissions.{флаг}: true в bases.yaml, раздел «{base.name}»",
    )


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

    # Шаг 3: регистр (Ruling 57, Ruling 60 — закрыт поставкой M3b), затем флаг операции.
    #
    # Независимый регистр сведений (`is_independent_register`: регистр сведений без регистратора
    # в ключе) — единственный вид регистра, в который эта поставка пишет, и единственная сущность
    # 1С, у которой физическое удаление записи вообще предусмотрено (CONTEXT.md). Ему достаточно
    # `write: true` для `create`/`update` — ровно то, что SPEC §7.1 говорил до Ruling 60; на
    # `delete_record` нужен ещё `independent_register_delete` (умолчания ролей: prod/test —
    # false, dev — true). Пометки удаления у записи регистра нет вовсе, и `mark_for_deletion`
    # по ней — не запрет по разрешениям, а неверный запрос: `params_invalid`.
    #
    # Всё остальное — как было: регистр, подчинённый регистратору (Ruling 57), наборы
    # `…_RecordType` и основной набор отклоняются при любом значении `register_direct_write`;
    # виртуальные таблицы — шаг 4 своим текстом. Регистр узнаётся по виду сущности, а не по
    # `is_records`: основной набор (`AccumulationRegister_X`, ключ `Recorder`) — такой же вход в
    # набор записей, как `…_RecordType`.
    регистр = op in ("create", "update", "mark_for_deletion", "delete_record") and (
        entity.kind in РЕГИСТР_ВИДЫ and not entity.is_virtual
    )
    if регистр and entity.is_independent_register:
        if op == "mark_for_deletion":
            raise WriteError(
                "params_invalid",
                f"у записи регистра «{entity.name}» нет пометки удаления",
                hint="для независимого регистра сведений — odata1c_delete_record",
            )
        if op == "delete_record" and not base.permissions.independent_register_delete:
            raise _отказ_флага(
                base,
                "independent_register_delete",
                f"удаление записи регистра «{entity.name}» запрещено",
            )
    elif регистр:
        raise WriteError(
            "permission_denied",
            f"запись в регистр «{entity.name}» не поддерживается — при любом значении "
            "permissions.register_direct_write",
            hint="запись регистров, подчинённых регистратору, в этой поставке не поддерживается "
            "— движения формирует проведение документа (odata1c_action Post)",
        )
    # Флаги операций ниже взаимоисключающие по `op` (для данного вызова сработает не больше
    # одного), порядок между ними поэтому не наблюдаем — записан в порядке брифа.
    if op == "action" and action in ДЕЙСТВИЯ_ПРОВЕДЕНИЯ and not base.permissions.post_documents:
        raise _отказ_флага(
            base,
            "post_documents",
            f"действие «{action}» запрещено: проведение документов выключено",
        )
    if op == "mark_for_deletion" and not base.permissions.mark_deletion:
        raise _отказ_флага(
            base, "mark_deletion", "пометка удаления запрещена: permissions.mark_deletion выключен"
        )

    # Шаг 4: виртуальная таблица — отказ безусловный: виртуальные таблицы (`Balance`,
    # `Turnovers`, `SliceLast`, …) — вычисляемые выборки, у них нет собственных записей и писать в
    # них нельзя ни при каком разрешении. Шаг 3 их пропускает явно (`not entity.is_virtual`),
    # хотя вид у них — вид регистра. Набор записей самого регистра сюда не попадает: зависимый
    # отказан на шаге 3, независимый регистр сведений пишется.
    if entity.is_virtual:
        raise WriteError(
            "permission_denied",
            f"виртуальная таблица «{entity.name}» не принимает запись",
            hint="виртуальные таблицы (Balance, Turnovers, SliceLast, …) — это вычисляемые "
            "выборки для чтения, своих записей у них нет",
        )

    # Шаг 5: список сущностей базы. `deny_entities` проверяется первым: он приоритетнее пустого
    # `allow_entities` (пустой список — «без ограничения», а не «ничего нельзя»).
    check_entity_denied(base, entity.name)
    if base.permissions.allow_entities and entity.name not in base.permissions.allow_entities:
        raise WriteError(
            "permission_denied",
            f"запись разрешена только в перечисленные сущности, «{entity.name}» среди них нет",
            hint=f"добавьте «{entity.name}» в permissions.allow_entities в bases.yaml",
        )

    # Шаг 6: запрещённые поля.
    check_fields(base, entity.name, fields)


def check_entity_denied(base: BaseConfig, entity: str) -> None:
    """Запретная половина шага 5 SPEC §7.1: сущность в `deny_entities` → `permission_denied`.

    Отдельно — ради строк табличных частей в `create` (Н6-2 ревью задачи 6 M2): владелец, который
    запретил запись в табличную часть, не должен получить её строки через тело `create` объекта.
    `allow_entities` к сущности строки не применяется (и поэтому не здесь): он перечисляет объекты,
    а не их табличные части, — разрешение владельца покрывает его строки. Имя в тексте — из
    `deny_entities` владельца, не ввод модели."""
    if entity in base.permissions.deny_entities:
        raise WriteError(
            "permission_denied",
            f"запись в сущность «{entity}» запрещена политикой базы",
            hint=f"уберите «{entity}» из permissions.deny_entities в bases.yaml",
        )


def check_fields(base: BaseConfig, entity: str, fields: Iterable[str]) -> None:
    """Шаг 6 SPEC §7.1 отдельно от остальных: поле тела записи из `deny_fields` (форма
    `Сущность.Поле`) → `field_write_denied`.

    Отдельной функцией — ради строк табличных частей в `create` (задача 6 плана M2): строка
    табличной части — поле своей сущности (`Catalog_X_КонтактнаяИнформация.Представление`), а
    `check_write` видит только имена шапки. Полный `check_write` на сущность табличной части
    звать нельзя: непустой `allow_entities` перечисляет объекты, а не их табличные части, и отказал
    бы строке объекта, которому запись разрешена. Здесь — только запрет полей, тот же текст.

    Называем поле — не значение: сюда и не передаётся ничего, кроме имён, так что назвать значение
    отказ не может по построению. Имя поля в тексте — из `deny_fields` владельца, не ввод модели:
    отказ срабатывает только на совпадении с его списком."""
    for имя_поля in fields:
        if f"{entity}.{имя_поля}" in base.permissions.deny_fields:
            raise WriteError(
                "field_write_denied",
                f"поле «{имя_поля}» запрещено к записи политикой базы",
                hint=f"уберите «{entity}.{имя_поля}» из permissions.deny_fields, "
                "чтобы разрешить запись",
            )
