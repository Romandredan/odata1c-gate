"""Описание сущности индекса для `odata1c_describe_entity` (SPEC §5, строка `describe_entity`).

Одна структура фактов (`build`) обслуживает оба формата ответа: `response_format="markdown"`
(человекочитаемый текст, `render_markdown`) и `response_format="json"` (тот же набор словарём,
сериализует и пропускает через гейт сам `ToolService`). Класс гейта на поле — не то, что хранит
индекс (`fields.sensitivity` — след последнего реиндекса, не действующая политика), а результат
`masking.effective_field_class(policy, entity, field, mode=...)` на теку­щей политике: тот же
источник правды, которым `BaseGate.is_protected` и обратная подмена проверяют защиту поля.
"""

from __future__ import annotations

from odata1c.gate.masking import effective_field_class
from odata1c.gate.policy import Policy
from odata1c.index.repository import EntityDescription

# Виртуальные таблицы регистров (SPEC §4.2, поправка проба P4) — тексту описания признака
# достаточно самого имени действия (`virtual_kind`); полный перечень суффиксов, по которому
# `ToolService` подбирает братьев виртуальной таблицы для подсказки `entity_unknown`, живёт там.


def build(desc: EntityDescription, *, policy: Policy, mode: str) -> dict:
    """Факты о сущности с уже разрешённым по текущей политике классом гейта на каждое поле."""
    поля = [
        {
            "name": поле["name"],
            "edm_type": поле["edm_type"],
            "nullable": bool(поле["nullable"]),
            "is_key": bool(поле["is_key"]),
            "is_ref": bool(поле["is_ref"]),
            "ref_targets": list(поле["ref_targets"]),
            "is_composite": bool(поле["is_composite"]),
            "gate_class": effective_field_class(policy, desc.name, поле["name"], mode=mode),
        }
        for поле in desc.fields
    ]
    return {
        "name": desc.name,
        "kind": desc.kind,
        "russian_kind": desc.russian_kind,
        "parent_entity": desc.parent_entity,
        "is_tabular_part": desc.is_tabular_part,
        "is_records": desc.is_records,
        "is_virtual": desc.is_virtual,
        "virtual_kind": desc.virtual_kind,
        "is_independent_register": desc.is_independent_register,
        "key_fields": list(desc.key_fields),
        "description_field": desc.description_field,
        "fields": поля,
        "children": list(desc.children),
        "actions": [dict(действие) for действие in desc.actions],
        "navigations": dict(desc.navigations),
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
