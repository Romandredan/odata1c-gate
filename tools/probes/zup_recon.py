"""Разведка `$metadata` ЗУП против правил гейта — только метаданные (отчёт `docs/probes/ZUP-recon.md`).

Данных 1С скрипт не касается: читает полные дампы `$metadata` ЗУП, БП и УТ и прогоняет по ним
классификатор полей по имени (`gate.field_rules.classify_field` — тот же, которым реиндекс пишет
раздел `auto` политики) и сторож справочников людей (Ruling 36). Доработки базы (объекты и поля с
префиксами `лкс`, `ик`, `сап`) учитываются только числом: их имена в репозиторий не попадают, а
правила для них — каталог владельца (`<дом>/gate/*.yaml`), а не поставки.

Что печатается: счётчики, имена типовых сущностей и полей. Реальных значений нет — их нет и во
входе.

Вход — полные дампы, в git не хранятся (`*.full.edmx`): `tests/fixtures/edmx/zup.full.edmx`
(копия `bases/<база ЗУП>/metadata.edmx`), `bp.full.edmx`, `probe.full.edmx` (УТ, проба P4).

    uv run python tools/probes/zup_recon.py --out build/zup-recon.json
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re

from odata1c.gate.field_rules import classify_field
from odata1c.gate.rules import package_rules
from odata1c.index.edmx import parse_edmx

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
ДАМПЫ = {
    "zup": КОРЕНЬ / "tests/fixtures/edmx/zup.full.edmx",
    "bp": КОРЕНЬ / "tests/fixtures/edmx/bp.full.edmx",
    "ut": КОРЕНЬ / "tests/fixtures/edmx/probe.full.edmx",
}
НЕТИПОВОЕ = re.compile(r"^(?:[A-Za-z]+_)?(?:лкс|ик|сап)[_А-ЯA-Z]")
# Признаки человека — те же, что у сторожа Ruling 36 в `tests/unit/test_gate_field_rules.py`.
ПОЛЯ_ЧЕЛОВЕКА = {"Фамилия", "Отчество", "ДатаРождения", "Пол"}
# Сущность о человеке: поле person с частью ФИО, либо поле snils или doc.
ЧАСТЬ_ФИО = re.compile(r"фамили|отчеств|фио|^имя$", re.IGNORECASE)
# Широкий невод на поля без класса в сущностях о людях; перечень разбирается вручную.
ПОХОЖЕ_НА_ПДН = re.compile(
    r"пасп|серия|номер|фамили|имя|отчеств|фио|снилс|инн|адрес|телеф|почт|рожд|счет|карт|полис"
    r"|родствен|представлени",
    re.IGNORECASE,
)
СЛУЖЕБНЫЕ = ("Ref_Key", "Code", "Number", "DataVersion", "Description", "PredefinedDataName")


def _разобрать(путь: pathlib.Path) -> list:
    return [с for с in parse_edmx(путь.read_bytes()).entities if not с.is_virtual]


def _классы(сущности: list, типовые: bool | None) -> collections.Counter:
    счёт: collections.Counter = collections.Counter()
    for с in сущности:
        нетип = bool(НЕТИПОВОЕ.match(с.name))
        for поле in с.fields:
            if типовые is not None and (нетип or bool(НЕТИПОВОЕ.match(поле.name))) == типовые:
                continue
            итог = classify_field(с.name, поле.name, поле.edm_type)
            if итог:
                счёт[итог[0]] += 1
    return счёт


def _справочники_людей(сущности: list) -> dict:
    каталоги = {с.name: с for с in сущности if с.name.startswith("Catalog_")}

    def человек(имя: str) -> bool:
        поля = {п.name for п in каталоги[имя].fields}
        return bool(
            поля & ПОЛЯ_ЧЕЛОВЕКА
            or any("СНИЛС" in п.upper() for п in поля)
            or f"{имя}_ФИО" in каталоги
        )

    найдены = sorted(
        имя
        for имя, с in каталоги.items()
        if с.parent_entity is None and not НЕТИПОВОЕ.match(имя) and человек(имя)
    )
    люди = package_rules().people
    return {
        "в_каталоге": [и for и in найдены if и in люди],
        "не_в_каталоге": [и for и in найдены if и not in люди],
    }


def _без_класса(сущности: list) -> dict[str, list[str]]:
    свод: dict[str, list[str]] = collections.defaultdict(list)
    for с in сущности:
        if НЕТИПОВОЕ.match(с.name):
            continue
        классы = {п.name: classify_field(с.name, п.name, п.edm_type) for п in с.fields}
        о_человеке = any(
            к and ((к[0] == "person" and ЧАСТЬ_ФИО.search(п)) or к[0] in ("snils", "doc"))
            for п, к in классы.items()
        )
        if not о_человеке:
            continue
        for п in с.fields:
            if (
                п.edm_type == "Edm.String"
                and классы[п.name] is None
                and п.name not in СЛУЖЕБНЫЕ
                and not п.name.endswith("_Type")
                and not НЕТИПОВОЕ.match(п.name)
                and ПОХОЖЕ_НА_ПДН.search(п.name)
            ):
                свод[п.name].append(с.name)
    return dict(sorted(свод.items(), key=lambda пара: (-len(пара[1]), пара[0])))


def main() -> None:
    разбор = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    разбор.add_argument("--out", type=pathlib.Path, default=КОРЕНЬ / "build/zup-recon.json")
    аргументы = разбор.parse_args()

    дампы = {имя: _разобрать(путь) for имя, путь in ДАМПЫ.items()}
    зуп = дампы["zup"]
    итог: dict = {
        "сущностей_невиртуальных": {имя: len(с) for имя, с in дампы.items()},
        "нетиповых_сущностей_зуп": sum(1 for с in зуп if НЕТИПОВОЕ.match(с.name)),
        "классы_зуп_типовые": dict(_классы(зуп, True).most_common()),
        "классы_зуп_нетиповые": dict(_классы(зуп, False).most_common()),
        "классы_бп": dict(_классы(дампы["bp"], None).most_common()),
        "классы_ут": dict(_классы(дампы["ut"], None).most_common()),
        "r36_зуп": _справочники_людей(зуп),
    }
    без = _без_класса(зуп)
    итог["без_класса_в_сущностях_о_людях"] = {
        "имён": len(без),
        "поля": {имя: {"сущностей": len(где), "пример": где[:3]} for имя, где in без.items()},
    }
    аргументы.out.parent.mkdir(parents=True, exist_ok=True)
    аргументы.out.write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        json.dumps(
            {к: в for к, в in итог.items() if к != "без_класса_в_сущностях_о_людях"},
            ensure_ascii=False,
            indent=1,
        )
    )
    print(f"полей без класса в сущностях о людях (имён): {len(без)}; итог — {аргументы.out}")


if __name__ == "__main__":
    main()
