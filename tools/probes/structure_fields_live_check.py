"""Структурные поля без суффикса в имени (Ruling 43, Р2-1) на живой базе — только чтение.

Демон не поднимается (слой тулов в процессе), дом ВРЕМЕННЫЙ: запись базы, индекс и политика —
копией из рабочего дома, словарь пустой, секрет свой; рабочий дом владельца, его словарь и демон на
7171 не трогаются. К 1С уходят одни GET. Обратная подмена (`inbound_write`) вызывается в памяти —
тело записи никуда не отправляется. Печатаются счётчики, имена сущностей и полей и коды отказов —
ни одного значения.

Что меряется.

1. Индекс: строковые поля `X`, у сущности которых есть поле-брат `XСтрокой`, — сколько из них
   признавал структурными прежний признак (суффикс имени) и сколько признаёт новый
   (`Unmasker._поле_структурное` со строением из индекса).
2. `InformationRegister_НастройкиОбменаСУЗ.ПроизводственныйОбъектАдрес`: сколько непустых значений
   в базе — структура (`contact_info.is_structure`), и приходит ли поле токеном.
3. Запись в памяти: токен адреса, который шлюз видел только текстом (`ЗаказКлиента.АдресДоставки`),
   в `ПроизводственныйОбъектАдрес` документа `ЗаказНаЭмиссиюКодовМаркировкиСУЗ` (create) — ждём
   отказ `token_ambiguous`; тот же токен в текстовое `АдресДоставки` заказа — текст (контроль).

Запуск из корня репозитория:

    uv run python tools/probes/structure_fields_live_check.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import shutil
import sqlite3
import tempfile

import yaml

from odata1c.cli import main as cli_main
from odata1c.config.loader import load_config
from odata1c.gate import contact_info
from odata1c.gate.revealed import RevealedValues
from odata1c.gate.unmasking import GateError
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository
from odata1c.registry.registry import SessionScope
from odata1c.tools.service import ToolService

РАБОЧИЙ_ДОМ = pathlib.Path.home() / ".claude" / "odata1c"
БАЗА = "trade_dev"
СУЗ = "InformationRegister_НастройкиОбменаСУЗ"
ЭМИССИЯ = "Document_ЗаказНаЭмиссиюКодовМаркировкиСУЗ"
ЗАКАЗ = "Document_ЗаказКлиента"
ПОЛЕ = "ПроизводственныйОбъектАдрес"


def подготовить(дом: pathlib.Path) -> None:
    cli_main(["init", "--home", str(дом)])
    запись = dict(
        yaml.safe_load((РАБОЧИЙ_ДОМ / "bases.yaml").read_text(encoding="utf-8"))["bases"][БАЗА]
    )
    запись["role"] = "prod"
    запись["gate"] = {"mode": "identifiers+names"}
    запись.pop("recipes", None)
    (дом / "bases.yaml").write_text(
        yaml.safe_dump({"default": БАЗА, "bases": {БАЗА: запись}}, allow_unicode=True),
        encoding="utf-8",
    )
    каталог = дом / "bases" / БАЗА
    каталог.mkdir(parents=True, exist_ok=True)
    for имя in ("metadata.sqlite", "metadata.edmx", "policy.yaml"):
        shutil.copy2(РАБОЧИЙ_ДОМ / "bases" / БАЗА / имя, каталог / имя)


def поля_с_братом(путь: pathlib.Path) -> list[tuple[str, str]]:
    соединение = sqlite3.connect(f"file:{путь.as_posix()}?mode=ro", uri=True)
    try:
        строки = соединение.execute(
            "SELECT e.name, f.name FROM fields f JOIN entities e ON e.id = f.entity_id"
            " WHERE f.edm_type = 'Edm.String' AND EXISTS ("
            "   SELECT 1 FROM fields b WHERE b.entity_id = f.entity_id"
            "   AND b.name = f.name || 'Строкой')"
            " ORDER BY e.name, f.name"
        ).fetchall()
    finally:
        соединение.close()
    return [(сущность, поле) for сущность, поле in строки]


async def запрос(сервис: ToolService, entity: str, **параметры) -> dict:
    return json.loads(await сервис.query(SessionScope(), base=БАЗА, entity=entity, **параметры))


def вид_значения(значение) -> str:
    """Что пришло через шлюз — без самого значения: класс токена, открытая структура или текст."""
    if not isinstance(значение, str) or not значение.strip():
        return "пусто"
    найдено = re.match(r"\[\[(\w+):", значение)
    if найдено:
        return f"токен {найдено.group(1)}"
    return "ОТКРЫТАЯ структура" if contact_info.is_structure(значение) else "открытый текст"


def записать(гейт, строение, entity: str, поле: str, токен: str) -> str:
    try:
        тело = гейт.inbound_write(
            {поле: токен}, entity=entity, shape=строение, current=None, revealed=RevealedValues()
        )
    except GateError as ошибка:
        return f"отказ {ошибка.code}"
    return "структура" if contact_info.is_structure(тело[поле]) else "текст"


async def проверить(дом: pathlib.Path) -> None:
    config = load_config(дом)
    сервис = ToolService(config)
    try:
        база = config.bases[БАЗА]
        гейт = сервис._gate_for(база)
        строение = сервис._строение(IndexRepository(index_path(дом, БАЗА)))
        подмена = гейт._обратная_подмена(строение)

        кандидаты = поля_с_братом(index_path(дом, БАЗА))
        по_имени = sum(contact_info.is_structure_field(поле) for _, поле in кандидаты)
        по_новому = sum(подмена._поле_структурное(сущность, поле) for сущность, поле in кандидаты)
        print(
            f"1. поля с братом «Строкой»: {len(кандидаты)}; структурными по имени — {по_имени}, "
            f"по Ruling 43 — {по_новому}"
        )
        for сущность, поле in кандидаты:
            print(f"   {сущность}.{поле} — класс на входе {подмена._класс_поля(сущность, поле)}")

        клиент = сервис._client_for(база)
        сырые = await клиент.get(СУЗ, {"$select": ПОЛЕ, "$top": "40"})
        значения = [r.get(ПОЛЕ) for r in сырые.get("value") or [] if isinstance(r.get(ПОЛЕ), str)]
        непустые = [з for з in значения if з.strip()]
        print(
            f"2. {СУЗ}.{ПОЛЕ}: строк {len(значения)}, непустых {len(непустые)}, "
            f"структур {sum(contact_info.is_structure(з) for з in непустые)}"
        )
        ответ = await запрос(сервис, СУЗ, select=[ПОЛЕ], top=40)
        виды = [вид_значения(r.get(ПОЛЕ)) for r in ответ.get("items", [])]
        print(f"   через шлюз: {виды}")
        for сущность in (СУЗ, ЭМИССИЯ):
            print(
                f"   класс поля {сущность}.{ПОЛЕ} на входе: {подмена._класс_поля(сущность, ПОЛЕ)}"
            )

        заказы = await запрос(
            сервис, ЗАКАЗ, select=["АдресДоставки"], filter="АдресДоставки ne ''", top=20
        )
        токен = next(
            (
                r["АдресДоставки"]
                for r in заказы.get("items", [])
                if str(r.get("АдресДоставки", "")).startswith("[[addr:")
            ),
            None,
        )
        if токен is None:
            print("3. в заказах нет адреса доставки токеном — проверка записи пропущена")
            return
        print(
            f"3. токен текстового адреса из заказа → {ЭМИССИЯ}.{ПОЛЕ}: "
            f"{записать(гейт, строение, ЭМИССИЯ, ПОЛЕ, токен)}"
        )
        print(
            f"   тот же токен → {ЗАКАЗ}.АдресДоставки (контроль): "
            f"{записать(гейт, строение, ЗАКАЗ, 'АдресДоставки', токен)}"
        )
        print(
            f"   тот же токен → {ЭМИССИЯ}.{ПОЛЕ} без строения: "
            f"{записать(гейт, lambda _: None, ЭМИССИЯ, ПОЛЕ, токен)}"
        )
    finally:
        await сервис.aclose()


def main() -> None:
    дом = pathlib.Path(tempfile.mkdtemp(prefix="odata1c-struct-"))
    try:
        подготовить(дом)
        asyncio.run(проверить(дом))
    finally:
        shutil.rmtree(дом, ignore_errors=True)


if __name__ == "__main__":
    main()
