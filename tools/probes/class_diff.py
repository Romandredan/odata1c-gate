"""Сверка авторазметки «было → стало» по полным дампам `$metadata` (только метаданные).

Снимок — класс каждого строкового поля каждой невиртуальной сущности, как его даёт
`classify_field` текущего кода, с правилом адресов по умолчанию (`addr` вне `mask_for` открыт).
Первый запуск с `--save` пишет эталон; следующие без него печатают разницу с эталоном: какое поле
какой сущности сменило класс. Реальных значений нет ни во входе, ни в выводе.

    uv run python tools/probes/class_diff.py --save build/classes-before.json
    uv run python tools/probes/class_diff.py --against build/classes-before.json
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib

from odata1c.gate.field_rules import classify_field
from odata1c.index.edmx import parse_edmx

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
ДАМПЫ = {
    "ut": КОРЕНЬ / "tests/fixtures/edmx/probe.full.edmx",
    "bp": КОРЕНЬ / "tests/fixtures/edmx/bp.full.edmx",
    "zup": КОРЕНЬ / "tests/fixtures/edmx/zup.full.edmx",
}


def снимок() -> dict[str, dict[str, str]]:
    итог: dict[str, dict[str, str]] = {}
    for имя, путь in ДАМПЫ.items():
        классы: dict[str, str] = {}
        for сущность in parse_edmx(путь.read_bytes()).entities:
            if сущность.is_virtual:
                continue
            for поле in сущность.fields:
                найдено = classify_field(сущность.name, поле.name, поле.edm_type)
                if найдено is not None:
                    классы[f"{сущность.name}.{поле.name}"] = найдено[0]
        итог[имя] = классы
    return итог


def разница(было: dict, стало: dict) -> dict[str, collections.Counter]:
    """Смены класса, свёрнутые по имени поля: `Поле: было → стало` → число сущностей."""
    свод: dict[str, collections.Counter] = {}
    for дамп in ДАМПЫ:
        счёт: collections.Counter = collections.Counter()
        прежние, новые = было.get(дамп, {}), стало.get(дамп, {})
        for ключ in sorted(set(прежние) | set(новые)):
            а, б = прежние.get(ключ, "-"), новые.get(ключ, "-")
            if а != б:
                счёт[f"{ключ.split('.', 1)[1]}: {а} → {б}"] += 1
        свод[дамп] = счёт
    return свод


def main() -> None:
    разбор = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    разбор.add_argument("--save", type=pathlib.Path)
    разбор.add_argument("--against", type=pathlib.Path)
    аргументы = разбор.parse_args()
    текущий = снимок()
    if аргументы.save:
        аргументы.save.parent.mkdir(parents=True, exist_ok=True)
        аргументы.save.write_text(json.dumps(текущий, ensure_ascii=False), encoding="utf-8")
        print({дамп: len(классы) for дамп, классы in текущий.items()})
    if аргументы.against:
        было = json.loads(аргументы.against.read_text(encoding="utf-8"))
        for дамп, счёт in разница(было, текущий).items():
            print(f"== {дамп}: {sum(счёт.values())} полей, {len(счёт)} видов смены")
            for строка, число in sorted(счёт.items()):
                print(f"  {число:4}  {строка}")


if __name__ == "__main__":
    main()
