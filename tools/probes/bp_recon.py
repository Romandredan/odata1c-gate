"""Разведка `$metadata` Бухгалтерии предприятия против правил гейта — только метаданные.

Данных 1С скрипт не касается: читает полный дамп `$metadata` БП и УТ, прогоняет по ним
классификатор полей по имени (`gate.field_rules.classify_field` — тот же, которым реиндекс
пишет раздел `auto` политики) и сторож справочников людей (Ruling 36), а детекторы значений
(`gate.detectors.scan_value`) — на синтетических строках в форме, в которой их хранит 1С.
Отчёт по итогам — `docs/probes/BP-recon.md`.

Что печатается: счётчики, имена сущностей и полей. Реальных значений нет — их нет и во входе.

Вход — полные дампы, в git не хранятся (`*.full.edmx`):

* `tests/fixtures/edmx/bp.full.edmx` — копия `bases/<база БП>/metadata.edmx` домашнего каталога;
* `tests/fixtures/edmx/probe.full.edmx` — УТ 11, дамп пробы P4.

Запуск из корня репозитория:

    uv run python tools/probes/bp_recon.py --out build/bp-recon.json
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re

from odata1c.gate.detectors import scan_value, snils_valid
from odata1c.gate.field_rules import DEFAULT_NAMES_FOR, СУЩНОСТИ_ФИЗЛИЦ, classify_field
from odata1c.index.edmx import parse_edmx

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
ДАМП_БП = КОРЕНЬ / "tests/fixtures/edmx/bp.full.edmx"
ДАМП_УТ = КОРЕНЬ / "tests/fixtures/edmx/probe.full.edmx"

# Признаки человека — те же, что у сторожа Ruling 36 в `tests/unit/test_gate_field_rules.py`.
ПОЛЯ_ЧЕЛОВЕКА = {"Фамилия", "Отчество", "ДатаРождения", "Пол"}
# Имена строковых полей, похожие на персональные данные или реквизиты, — для перечня полей без
# класса. Широкий невод: перечень разбирается вручную.
ПОХОЖЕ_НА_ПДН = re.compile(
    r"регистрац|регном|пфр|фсс|сфр|страхов|паспорт|удостов|лицев|карт|рожден|латиниц|инн|снилс"
    r"|наиморг|фио|почт|email|телеф|банковск|эмбосс|миграц|^номер$|серия|представление$",
    re.I,
)
# Части ФИО внутри составного имени поля (`РебенокФамилия`, `ДоверительФЛ_Отчество`, `ФЛ_Имя`)
# и названия организации в сокращении форматов ФНС (`ДоверительЮЛ_НаимОрг`).
ФИО_В_СОСТАВНОМ_ИМЕНИ = re.compile(r"фамили|отчеств|имя$|_имя|наиморг", re.I)
СЛУЖЕБНОЕ_ИМЯ = re.compile(r"имяфайла|файлимя|полноеимя|табличнаячастьимя|служебн", re.I)
СЛУЖЕБНЫЕ = ("Ref_Key", "Code", "Number", "DataVersion", "Description", "PredefinedDataName")


def _снилс(первые9: str) -> str:
    сумма = sum(int(ц) * (9 - i) for i, ц in enumerate(первые9)) % 101
    return первые9 + f"{0 if сумма == 100 else сумма:02d}"


def _синтетика() -> dict[str, str]:
    снилс = _снилс("112233445")
    assert snils_valid(снилс)
    снилс_1с = f"{снилс[:3]}-{снилс[3:6]}-{снилс[6:9]} {снилс[9:]}"
    return {
        "СНИЛС в форме 1С (СтраховойНомерПФР)": снилс_1с,
        "номер паспорта (ДокументыФизическихЛиц.Номер)": "123456",
        "серия паспорта отдельно": "45 07",
        "Представление документа физлица": (
            "Паспорт гражданина РФ, серия: 45 07, № 123456, выдан: 01.02.2010, ОВД района, "
            "№ подр. 770-001"
        ),
        "лицевой счёт сотрудника (НомерЛицевогоСчета)": "40817810099910004312",
        "регистрационный номер ПФР": "087-104-012345",
        "регистрационный номер ФСС": "7701234567",
        "регистрационный номер СФР": "0871040123",
        "миграционная карта": "4618 1234567",
        "место рождения": "г. Москва",
        "фамилия латиницей": "IVANOV",
        "эмбоссированный текст карты": "IVAN IVANOV",
        "почта в поле АдресEmail": "ivanov@example.ru",
        "свидетельство о рождении": "IV-МЮ 123456",
    }


def _справочники_людей(сущности: dict) -> tuple[list[str], list[str]]:
    каталоги = {имя: с for имя, с in сущности.items() if имя.startswith("Catalog_")}

    def человек(имя: str) -> bool:
        поля = {поле.name for поле in каталоги[имя].fields}
        return bool(
            поля & ПОЛЯ_ЧЕЛОВЕКА
            or any("СНИЛС" in поле.upper() for поле in поля)
            or f"{имя}_ФИО" in каталоги
        )

    корневые = sorted(
        имя for имя, с in каталоги.items() if с.parent_entity is None and человек(имя)
    )
    нет_в_базе = sorted((DEFAULT_NAMES_FOR | СУЩНОСТИ_ФИЗЛИЦ) - set(каталоги))
    return корневые, нет_в_базе


def _классы(сущности: dict) -> tuple[collections.Counter, dict]:
    счёт: collections.Counter = collections.Counter()
    по_полю: dict[tuple[str, str], str] = {}
    for с in сущности.values():
        for поле in с.fields:
            итог = classify_field(с.name, поле.name, поле.edm_type)
            if итог:
                счёт[итог[0]] += 1
                по_полю[(с.name, поле.name)] = итог[0]
    return счёт, по_полю


def main() -> None:
    разбор = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    разбор.add_argument("--out", type=pathlib.Path, default=КОРЕНЬ / "build/bp-recon.json")
    аргументы = разбор.parse_args()

    бп = {с.name: с for с in parse_edmx(ДАМП_БП.read_bytes()).entities}
    ут = {с.name: с for с in parse_edmx(ДАМП_УТ.read_bytes()).entities}
    итог: dict = {"сущностей": {"бп": len(бп), "ут": len(ут)}}

    корневые, нет_в_базе = _справочники_людей(бп)
    итог["r36"] = {
        "в_списке": [имя for имя in корневые if имя in СУЩНОСТИ_ФИЗЛИЦ],
        "не_в_списке": [имя for имя in корневые if имя not in СУЩНОСТИ_ФИЗЛИЦ],
        "имена_списка_нет_в_бп": нет_в_базе,
    }

    счёт_бп, по_полю = _классы(бп)
    счёт_ут, _ = _классы(ут)
    итог["классы_бп_ут"] = {
        к: [счёт_бп.get(к, 0), счёт_ут.get(к, 0)] for к in sorted(set(счёт_бп) | set(счёт_ут))
    }

    без_класса: dict[str, list[str]] = collections.defaultdict(list)
    фио_без_класса: dict[str, list[str]] = collections.defaultdict(list)
    for с in бп.values():
        if с.is_virtual:
            continue
        for поле in с.fields:
            if (
                поле.edm_type != "Edm.String"
                or поле.name in СЛУЖЕБНЫЕ
                or поле.name.endswith("_Type")
                or (с.name, поле.name) in по_полю
            ):
                continue
            if ПОХОЖЕ_НА_ПДН.search(поле.name):
                без_класса[поле.name].append(с.name)
            if ФИО_В_СОСТАВНОМ_ИМЕНИ.search(поле.name) and not СЛУЖЕБНОЕ_ИМЯ.search(поле.name):
                фио_без_класса[поле.name].append(с.name)
    итог["без_класса"] = {
        имя: {"сущностей": len(где), "пример": где[:3]}
        for имя, где in sorted(без_класса.items(), key=lambda пара: -len(пара[1]))
    }
    итог["фио_без_класса"] = {
        "имён": len(фио_без_класса),
        "полей": sum(len(где) for где in фио_без_класса.values()),
        "поля": {
            имя: {"сущностей": len(где), "пример": где[:2]}
            for имя, где in sorted(фио_без_класса.items(), key=lambda пара: -len(пара[1]))
        },
    }

    итог["класс_по_полю"] = {
        f"{сущность}.{поле}": класс
        for (сущность, поле), класс in sorted(по_полю.items())
        if re.search(r"ФизическиеЛица|Сотрудник|ЛицевыхСчетов|РодственникиФиз", сущность)
    }

    итог["детекторы"] = {
        имя: [совпадение.type for совпадение in scan_value(значение)]
        for имя, значение in _синтетика().items()
    }

    аргументы.out.parent.mkdir(parents=True, exist_ok=True)
    аргументы.out.write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        json.dumps(
            {к: итог[к] for к in ("сущностей", "r36", "классы_бп_ут", "детекторы")},
            ensure_ascii=False,
            indent=1,
        )
    )
    print(f"полей без класса, похожих на ПДн: {len(итог['без_класса'])}; итог — {аргументы.out}")


if __name__ == "__main__":
    main()
