"""Приёмка шаблона рецептов ЗУП (`templates/recipes/zup.yaml`) на живой базе — только чтение.

Временный дом и демон — как у `zup_live_check.py` (порт 7198, рабочий дом владельца не
затрагивается); у записи базы `config: zup` и нет собственного файла рецептов, так что книга
рецептов — ровно шаблон пакета. Каждый рецепт вызывается через `odata1c_recipe` с образцом
параметров. Печатаются: код ошибки или число строк, заполненные поля, `masked_fields`.
Значений нет.

    uv run python tools/probes/zup_recipes_check.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bp_live_check as общее  # noqa: E402
import zup_live_check as зуп  # noqa: E402

НАЧАЛО, КОНЕЦ = "2025-01-01T00:00:00", "2026-09-01T00:00:00"
ОБРАЗЦЫ = {
    "staff": {"period": КОНЕЦ},
    "accruals": {"start": "2026-06-01T00:00:00", "end": КОНЕЦ},
    "salary_debt": {"period": КОНЕЦ},
    "salary_settlements": {"start": "2026-06-01T00:00:00", "end": КОНЕЦ},
    "hires": {"start": НАЧАЛО, "end": КОНЕЦ},
    "dismissals": {"start": НАЧАЛО, "end": КОНЕЦ},
    "vacations": {"start": НАЧАЛО, "end": КОНЕЦ},
    "sick_leaves": {"start": НАЧАЛО, "end": КОНЕЦ},
    "payroll_documents": {"start": НАЧАЛО, "end": КОНЕЦ},
}


async def main() -> int:
    общее.ДАМП, общее.ПОРТ, общее.БАЗА = зуп.ДАМП, зуп.ПОРТ, зуп.БАЗА
    рабочий = pathlib.Path.home() / ".claude" / "odata1c"
    дом = зуп.собрать_дом(рабочий, None)
    путь = дом / "bases.yaml"
    настройки = yaml.safe_load(путь.read_text(encoding="utf-8"))
    запись = настройки["bases"][зуп.БАЗА]
    запись["config"] = "zup"
    запись.pop("recipes", None)
    путь.write_text(yaml.safe_dump(настройки, allow_unicode=True), encoding="utf-8")
    (дом / "bases" / зуп.БАЗА / "recipes.yaml").unlink(missing_ok=True)
    итог: dict = {}
    try:
        async with общее.сессия(дом) as сеанс:
            перечень, _ = await общее.тул(сеанс, "odata1c_recipe", {})
            имена = sorted(р.get("name") for р in перечень.get("recipes", []))
            неприменимы = sorted(
                р.get("name") for р in перечень.get("recipes", []) if р.get("applicable") is False
            )
            print(f"рецептов в книге: {len(имена)}; неприменимы: {неприменимы}")
            for имя, параметры in ОБРАЗЦЫ.items():
                ответ, _ = await общее.тул(
                    сеанс, "odata1c_recipe", {"name": имя, "params": параметры}
                )
                if "error" in ответ:
                    ошибка = ответ["error"]
                    итог[имя] = {"ошибка": ошибка.get("code")}
                    # Текст ошибки — ответ тула, прошедший стража гейта: его видит и модель.
                    print(f"{имя}: ошибка {ошибка.get('code')} — {ошибка.get('message', '')[:160]}")
                    continue
                строки = ответ.get("items", [])
                заполнены = sorted(
                    {к for с in строки for к, в in с.items() if в not in (None, "", 0)}
                )
                итог[имя] = {
                    "строк": len(строки),
                    "has_more": ответ.get("has_more"),
                    "заполнены": заполнены,
                    "masked_fields": ответ.get("masked_fields"),
                }
                print(f"{имя}: строк {len(строки)}, masked {ответ.get('masked_fields')}")
    finally:
        общее.остановить_демон(дом)
        shutil.rmtree(дом, ignore_errors=True)
        print(f"временный дом удалён: {not дом.exists()}")
    ошибок = sum(1 for и in итог.values() if "ошибка" in и)
    пустых = sorted(и for и, з in итог.items() if з.get("строк") == 0)
    print(
        json.dumps({"рецептов": len(итог), "ошибок": ошибок, "пустых": пустых}, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
