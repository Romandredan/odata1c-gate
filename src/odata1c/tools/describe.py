"""Описание сущности индекса для `odata1c_describe_entity` (SPEC §5, строка `describe_entity`).

Одна структура фактов (`build`) обслуживает оба формата ответа: `response_format="markdown"`
(человекочитаемый текст, `render_markdown`) и `response_format="json"` (тот же набор словарём,
сериализует и пропускает через гейт сам `ToolService`). Класс гейта на поле — не то, что хранит
индекс (`fields.sensitivity` — след последнего реиндекса, не действующая политика), а результат
`masking.effective_field_class(policy, entity, field, mode=...)` на теку­щей политике: тот же
источник правды, которым `BaseGate.is_protected` и обратная подмена проверяют защиту поля.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from odata1c.gate.contact_info import EntityShape
from odata1c.gate.masking import inbound_field_class
from odata1c.gate.policy import Policy
from odata1c.index.repository import EntityDescription

# Ruling 28 (итоговое ревью M1d, раунд 2, I2): имя скрытой сущности не называется НИГДЕ, включая
# чужое описание, — иначе нейтральность предупреждения об обрезке `$expand` обесценивается
# соседним тулом, который печатает `Контрагент → Catalog_Контрагенты` при `hide: true`. Имя
# навигационного ПОЛЯ при этом остаётся: это часть формы видимой сущности, и вычеркнув его, мы
# заставим модель ходить вслепую и придумывать несуществующие связи. Заглушка говорит модели ровно
# то, что ей нужно знать: связь есть, она закрыта владельцем, ходить по ней бесполезно.
СКРЫТАЯ_ЦЕЛЬ = "(сущность скрыта настройкой базы)"

# Тип строки табличной части: 1С публикует его ровно одной формой — `Collection(StandardODATA.
# <имя дочерней сущности>_RowType)` (проверено на `$metadata` УТ: других форм с `_RowType` в
# индексе нет). Разбор регулярным выражением нужен, чтобы сверять имя ЦЕЛИКОМ, а не подстрокой.
_ТИП_СТРОКИ = re.compile(r"Collection\(StandardODATA\.(?P<имя>.+)_RowType\)")

# Виртуальные таблицы регистров (SPEC §4.2, поправка проба P4) — тексту описания признака
# достаточно самого имени действия (`virtual_kind`); полный перечень суффиксов, по которому
# `ToolService` подбирает братьев виртуальной таблицы для подсказки `entity_unknown`, живёт там.


def build(
    desc: EntityDescription,
    *,
    policy: Policy,
    mode: str,
    hidden: Callable[[str], bool] | None = None,
) -> dict:
    """Факты о сущности с уже разрешённым по текущей политике классом гейта на каждое поле.

    `hidden` — скрыта ли сущность политикой базы (`entities.hide`, SPEC §6.9). Имена скрытых
    сущностей в ответ не попадают (Ruling 28, см. `СКРЫТАЯ_ЦЕЛЬ`): цель навигации и цель ссылки
    заменяются заглушкой, скрытая дочерняя сущность вычёркивается из списка. Имена полей — и
    навигационных, и ссылочных — остаются: они часть формы самой сущности."""

    def скрыта(имя: str | None) -> bool:
        return bool(имя) and hidden is not None and hidden(имя)

    def цель(имя: str) -> str:
        return СКРЫТАЯ_ЦЕЛЬ if скрыта(имя) else имя

    скрытые_дети = {имя for имя in desc.children if скрыта(имя)}

    # Класс поля — входного пути (`inbound_field_class`, Ruling 35): поле значения контактной
    # информации показывается классом `contact` — по нему модель узнаёт заранее, что отбор по
    # полю принимает только токен. Строение этой сущности — из её же описания.
    своё_строение = EntityShape(frozenset(поле["name"] for поле in desc.fields), desc.parent_entity)

    def строение(имя: str) -> EntityShape | None:
        return своё_строение if имя == desc.name else None

    def тип(edm_type: str) -> str:
        """Имя типа строки табличной части (`Collection(…Document_X_Товары_RowType)`) содержит имя
        самой табличной части, и по нему скрытое имя восстанавливается целиком. Функционального
        смысла для модели в нём нет — тип строки не адресуют, — а список дочерних объектов это имя
        уже не печатает: оставить его значило бы отдать в одной колонке то, что вычеркнуто в
        другой. Поэтому имя заменяется заглушкой, а форма типа (коллекция ли это) сохраняется.

        Имя сравнивается ЦЕЛИКОМ, а не подстрокой (Minor итогового ревью M1d, раунд 3): пара
        детей с общим префиксом (`Товары` и `ТоварыДоп` — в типовых конфигурациях обычное дело)
        при поиске подстрокой страдала дважды. Тип ВИДИМОГО соседа портился
        (`(сущность скрыта…)Доп_RowType` — настоящий тип уже не прочитать), а оставшийся хвост
        `Доп` выдавал структуру: скрытое имя восстанавливается как «видимое минус суффикс», то
        есть замена не скрывала, а подсказывала."""
        совпадение = _ТИП_СТРОКИ.fullmatch(edm_type)
        if совпадение is not None and совпадение["имя"] in скрытые_дети:
            return f"Collection(StandardODATA.{СКРЫТАЯ_ЦЕЛЬ}_RowType)"
        return edm_type

    поля = [
        {
            "name": поле["name"],
            "edm_type": тип(поле["edm_type"]),
            "nullable": bool(поле["nullable"]),
            "is_key": bool(поле["is_key"]),
            "is_ref": bool(поле["is_ref"]),
            # Цель ссылки — то же имя сущности, что и цель навигации, и запрет на него тот же.
            "ref_targets": [цель(имя) for имя in поле["ref_targets"]],
            "is_composite": bool(поле["is_composite"]),
            "gate_class": inbound_field_class(
                policy, desc.name, поле["name"], mode=mode, shape=строение
            ),
        }
        for поле in desc.fields
    ]
    return {
        "name": desc.name,
        "kind": desc.kind,
        "russian_kind": desc.russian_kind,
        "parent_entity": цель(desc.parent_entity) if desc.parent_entity else desc.parent_entity,
        "is_tabular_part": desc.is_tabular_part,
        "is_records": desc.is_records,
        "is_virtual": desc.is_virtual,
        "virtual_kind": desc.virtual_kind,
        "is_independent_register": desc.is_independent_register,
        "key_fields": list(desc.key_fields),
        "description_field": desc.description_field,
        "fields": поля,
        # Дочерняя сущность вычёркивается целиком, а не заменяется заглушкой: в отличие от
        # навигации, по ней не строят `$expand` — это отдельный набор, который и запрашивают
        # отдельно, а скрытый запрошен быть не может. Знать о его существовании модели незачем.
        "children": [имя for имя in desc.children if not скрыта(имя)],
        "actions": [dict(действие) for действие in desc.actions],
        "navigations": {имя: цель(куда) for имя, куда in desc.navigations.items()},
        "members": list(desc.members),
    }


def render_markdown(data: dict) -> str:
    """Markdown-таблица полей плюс признаки, навигации, дети, действия, значения перечисления
    (для `Enum_*`) — вид, ключ, табличная часть/набор записей/виртуальная таблица и её вызов,
    независимый регистр (SPEC §5, строка `describe_entity`)."""
    строки = [f"# {data['name']} ({data['russian_kind']})", ""]

    признаки: list[str] = []
    if data["is_tabular_part"]:
        признаки.append(f"табличная часть сущности {data['parent_entity']}")
    if data["is_records"]:
        признаки.append("набор записей регистра")
    if data["is_virtual"]:
        признаки.append(
            f"виртуальная таблица {data['virtual_kind']} регистра {data['parent_entity']}"
        )
    if data["is_independent_register"]:
        признаки.append("независимый регистр сведений (без регистратора)")
    if признаки:
        строки.append("Признаки: " + "; ".join(признаки))
        строки.append("")

    if data["key_fields"]:
        строки.append("Ключ: " + ", ".join(data["key_fields"]))
        строки.append("")

    if data["fields"]:
        строки.append("| Поле | Тип | Цель ссылки | Составное | Класс гейта |")
        строки.append("|---|---|---|---|---|")
        for поле in data["fields"]:
            цель = ", ".join(поле["ref_targets"])
            составное = "да" if поле["is_composite"] else ""
            класс = поле["gate_class"] or ""
            строки.append(
                f"| {поле['name']} | {поле['edm_type']} | {цель} | {составное} | {класс} |"
            )
        строки.append("")

    if data["navigations"]:
        навигации = ", ".join(
            f"{имя} → {цель}" for имя, цель in sorted(data["navigations"].items())
        )
        строки.append("Навигации: " + навигации)
        строки.append("")

    if data["children"]:
        строки.append(
            "Дочерние объекты (табличные части, наборы записей, виртуальные таблицы): "
            + ", ".join(data["children"])
        )
        строки.append("")

    if data["actions"]:
        действия = ", ".join(
            действие["name"] + (" (изменяет данные)" if действие["side_effecting"] else "")
            for действие in data["actions"]
        )
        строки.append("Действия: " + действия)
        строки.append("")

    if data["kind"] == "Enum" and data["members"]:
        строки.append("Значения перечисления: " + ", ".join(data["members"]))
        строки.append("")

    return "\n".join(строки).rstrip() + "\n"
