"""Описание сущности индекса для `odata1c_describe_entity` (SPEC §5, строка `describe_entity`).

Одна структура фактов (`build`) обслуживает оба формата ответа: `response_format="markdown"`
(человекочитаемый текст, `render_markdown`) и `response_format="json"` (тот же набор словарём,
сериализует и пропускает через гейт сам `ToolService`). Класс гейта на поле — не то, что хранит
индекс (`fields.sensitivity` — след последнего реиндекса, не действующая политика), а результат
`masking.effective_field_class(policy, entity, field, mode=...)` на теку­щей политике: тот же
источник правды, которым `BaseGate.is_protected` и обратная подмена проверяют защиту поля.
"""

from __future__ import annotations

from collections.abc import Callable

from odata1c.gate.masking import effective_field_class
from odata1c.gate.policy import Policy
from odata1c.index.repository import EntityDescription

# Ruling 28 (итоговое ревью M1d, раунд 2, I2): имя скрытой сущности не называется НИГДЕ, включая
# чужое описание, — иначе нейтральность предупреждения об обрезке `$expand` обесценивается
# соседним тулом, который печатает `Контрагент → Catalog_Контрагенты` при `hide: true`. Имя
# навигационного ПОЛЯ при этом остаётся: это часть формы видимой сущности, и вычеркнув его, мы
# заставим модель ходить вслепую и придумывать несуществующие связи. Заглушка говорит модели ровно
# то, что ей нужно знать: связь есть, она закрыта владельцем, ходить по ней бесполезно.
СКРЫТАЯ_ЦЕЛЬ = "(сущность скрыта настройкой базы)"

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

    поля = [
        {
            "name": поле["name"],
            "edm_type": поле["edm_type"],
            "nullable": bool(поле["nullable"]),
            "is_key": bool(поле["is_key"]),
            "is_ref": bool(поле["is_ref"]),
            # Цель ссылки — то же имя сущности, что и цель навигации, и запрет на него тот же.
            "ref_targets": [цель(имя) for имя in поле["ref_targets"]],
            "is_composite": bool(поле["is_composite"]),
            "gate_class": effective_field_class(policy, desc.name, поле["name"], mode=mode),
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
