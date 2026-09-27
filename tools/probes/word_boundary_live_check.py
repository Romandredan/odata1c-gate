"""Приёмка Ruling 106 (issue №2) на живой базе 1С: название из словаря не заменяется внутри слова.

Скрипт разговаривает со шлюзом так, как Claude Code: поднимает настоящий лаунчер `odata1c mcp`
(stdio MCP), тот — свой демон. Демон и всё его состояние живут во ВРЕМЕННОМ домашнем каталоге:
рабочий дом владельца (его `bases.yaml`, словарь, политика, демон на 7171) не трогается. Во
временный дом копируются запись базы (уровень гейта принудительно `identifiers+names`), индекс
метаданных, политика и рецепты; словарь создаётся пустым, со своим секретом.

Порядок. Сначала словарь наполняется так, как его наполнила бы работа модели: чтением названий
справочников контрагентов, партнёров, организаций и людей. Затем снимаются ответы, где названий
организаций быть не должно или где они стоят в свободном тексте:

* `odata1c_recipe` без имени — перечень рецептов (имена сущностей и описания);
* `odata1c_find_entity` по нескольким запросам — имена сущностей и полей из индекса;
* свободный текст — `Description` номенклатуры, `Комментарий` и `НазначениеПлатежа` документов.

Что печатается — только счётчики, реальные значения никогда: сколько токенов `org`/`person` стоит
ВНУТРИ слова (буква вплотную к токену слева или справа) и сколько — отдельным словом; сколько
ответов пришло с `guard_replaced`; форма окружения токенов внутри слова (буква → a, цифра → 9).
После Ruling 106 токенов внутри слова быть не должно, а в метаданных токенов нет вовсе.

Сравнение «до» и «после» — одним скриптом на разном коде: ключ `--src` подставляет каталог `src`
другой версии (например, выгруженный `git archive dev src`) через `PYTHONPATH` лаунчера и демона.

    uv run python tools/probes/word_boundary_live_check.py --label after
    uv run python tools/probes/word_boundary_live_check.py --label before --src <каталог>/src
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter

import yaml
from mcp import StdioServerParameters, stdio_client
from mcp.client.session import ClientSession

БАЗА = "trade_dev"
ПОРТ = 7197
ТАЙМАУТ = 240.0
СТРАНИЦА = 1000

# Словарь наполняется названиями этих справочников — как при обычной работе модели.
НАПОЛНЕНИЕ = (
    "Catalog_Контрагенты",
    "Catalog_Партнеры",
    "Catalog_Организации",
    "Catalog_ФизическиеЛица",
    "Catalog_КонтактныеЛицаПартнеров",
)
НАПОЛНЕНИЕ_ЗАПИСЕЙ = 5000

ЗАПРОСЫ_ИНДЕКСА = (
    "Организации",
    "Себестоимость",
    "Выручка",
    "Сотрудники",
    "Партнеры",
    "Номенклатура",
    "Контрагенты",
    "Товары",
)

# (сущность, поля свободного текста). Поле, которого у сущности нет, пропускается.
СВОБОДНЫЙ_ТЕКСТ = (
    ("Catalog_Номенклатура", ("Description",)),
    ("Document_РеализацияТоваровУслуг", ("Комментарий",)),
    ("Document_ЗаказКлиента", ("Комментарий",)),
    ("Document_ПоступлениеТоваровУслуг", ("Комментарий",)),
    ("Document_ПоступлениеБезналичныхДенежныхСредств", ("Комментарий", "НазначениеПлатежа")),
    ("Document_СписаниеБезналичныхДенежныхСредств", ("Комментарий", "НазначениеПлатежа")),
)
СВОБОДНЫЙ_ТЕКСТ_ЗАПИСЕЙ = 3000

ТОКЕН_НАЗВАНИЯ = re.compile(r"\[\[(?:org|person):[0-9A-Z]{1,16}\]\]")
ЛЮБОЙ_ТОКЕН = re.compile(r"\[\[[a-z][a-z0-9_]*:[0-9A-Z]{1,16}\]\]")


def форма(текст: str) -> str:
    """Форма окружения без значений: буква → a, цифра → 9, токен → T."""
    текст = ЛЮБОЙ_ТОКЕН.sub("T", текст)
    return "".join("a" if с.isalpha() and с != "T" else "9" if с.isdigit() else с for с in текст)


def строки(данные):
    if isinstance(данные, str):
        yield данные
    elif isinstance(данные, dict):
        for ключ, значение in данные.items():
            yield ключ
            yield from строки(значение)
    elif isinstance(данные, list):
        for элемент in данные:
            yield from строки(элемент)


class Счёт:
    """Токены названий в ответах раздела: внутри слова и отдельным словом."""

    def __init__(self) -> None:
        self.ответов = 0
        self.строк = 0
        self.внутри = 0
        self.отдельно = 0
        self.guard_replaced = 0
        self.формы: Counter[str] = Counter()

    def учесть(self, ответ: dict) -> None:
        self.ответов += 1
        if any(str(п).startswith("guard_replaced") for п in ответ.get("warnings") or []):
            self.guard_replaced += 1
        for строка in строки(ответ):
            self.строк += 1
            for найдено in ТОКЕН_НАЗВАНИЯ.finditer(строка):
                слева = строка[найдено.start() - 1 : найдено.start()]
                справа = строка[найдено.end() : найдено.end() + 1]
                if слева.isalpha() or справа.isalpha():
                    self.внутри += 1
                    окно = строка[max(0, найдено.start() - 4) : найдено.end() + 4]
                    self.формы[форма(окно)] += 1
                else:
                    self.отдельно += 1

    def итог(self) -> dict:
        return {
            "ответов": self.ответов,
            "строк": self.строк,
            "токенов_внутри_слова": self.внутри,
            "токенов_отдельным_словом": self.отдельно,
            "ответов_с_guard_replaced": self.guard_replaced,
            "формы_внутри_слова": dict(self.формы.most_common(10)),
        }


def собрать_дом(рабочий: pathlib.Path) -> pathlib.Path:
    """Временный домашний каталог: запись базы, индекс, политика и рецепты — копией из рабочего
    дома; уровень гейта — `identifiers+names`; словарь пустой, секрет свой."""
    дом = pathlib.Path(tempfile.mkdtemp(prefix="odata1c-wb-"))
    for имя in ("bases", "logs"):
        (дом / имя).mkdir()
    настройки = yaml.safe_load((рабочий / "bases.yaml").read_text(encoding="utf-8"))
    запись = dict(настройки["bases"][БАЗА])
    запись["gate"] = {**(запись.get("gate") or {}), "mode": "identifiers+names"}
    (дом / "bases.yaml").write_text(
        yaml.safe_dump({"default": БАЗА, "bases": {БАЗА: запись}}, allow_unicode=True),
        encoding="utf-8",
    )
    (дом / "daemon.yaml").write_text(
        f'port: {ПОРТ}\nreindex_check_hours: 0\ngate_secret: "{secrets.token_hex(32)}"\n',
        encoding="utf-8",
    )
    исходный = рабочий / "bases" / БАЗА
    каталог = дом / "bases" / БАЗА
    каталог.mkdir()
    for имя in ("metadata.sqlite", "policy.yaml", "policy.auto.yaml", "recipes.yaml"):
        if (исходный / имя).exists():
            shutil.copy2(исходный / имя, каталог / имя)
    return дом


def окружение(src: str | None) -> dict[str, str]:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if src:
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    return env


def остановить_демон(дом: pathlib.Path, src: str | None) -> None:
    subprocess.run(  # noqa: S603 — фиксированная команда собственного пакета
        [sys.executable, "-m", "odata1c", "daemon", "stop", "--home", str(дом)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=окружение(src),
        check=False,
    )
    time.sleep(1.5)


@contextlib.asynccontextmanager
async def сессия(дом: pathlib.Path, src: str | None):
    параметры = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odata1c", "mcp", "--home", str(дом)],
        env=окружение(src),
    )
    async with stdio_client(параметры) as (чтение, запись):
        async with ClientSession(чтение, запись) as сеанс:
            await сеанс.initialize()
            yield сеанс


async def тул(сеанс: ClientSession, имя: str, аргументы: dict) -> dict:
    ответ = await сеанс.call_tool(имя, аргументы, read_timeout_seconds=ТАЙМАУТ)
    текст = "\n".join(ч.text for ч in ответ.content if getattr(ч, "text", None))
    try:
        return json.loads(текст)
    except ValueError:
        return {"error": {"code": "not_json"}}


async def постранично(сеанс, сущность: str, поля: list[str], всего: int, счёт: Счёт | None):
    """До `всего` записей страницами; каждый ответ — в `счёт`. Возвращает число записей или код
    ошибки."""
    получено = 0
    пропуск = 0
    while получено < всего:
        аргументы = {"entity": сущность, "select": поля, "top": min(СТРАНИЦА, всего - получено)}
        if пропуск:
            аргументы["skip"] = пропуск
        ответ = await тул(сеанс, "odata1c_query", аргументы)
        if "error" in ответ:
            return ответ["error"].get("code") or "error"
        if счёт is not None:
            счёт.учесть(ответ)
        получено += len(ответ.get("items") or [])
        if not ответ.get("has_more"):
            break
        пропуск = ответ.get("next_skip") or (пропуск + СТРАНИЦА)
    return получено


async def прогон(сеанс, итог: dict) -> None:
    наполнение = {}
    for сущность in НАПОЛНЕНИЕ:
        наполнение[сущность] = await постранично(
            сеанс, сущность, ["Ref_Key", "Description"], НАПОЛНЕНИЕ_ЗАПИСЕЙ, None
        )
    итог["наполнение_словаря"] = наполнение
    print("словарь наполнен:", наполнение)

    рецепты = Счёт()
    ответ = await тул(сеанс, "odata1c_recipe", {})
    рецепты.учесть(ответ)
    итог["перечень_рецептов"] = {**рецепты.итог(), "ошибка": (ответ.get("error") or {}).get("code")}

    индекс = Счёт()
    for запрос in ЗАПРОСЫ_ИНДЕКСА:
        индекс.учесть(await тул(сеанс, "odata1c_find_entity", {"query": запрос, "limit": 20}))
    итог["find_entity"] = индекс.итог()

    текст = Счёт()
    по_сущностям = {}
    for сущность, поля in СВОБОДНЫЙ_ТЕКСТ:
        for поле in поля:
            по_сущностям[f"{сущность}.{поле}"] = await постранично(
                сеанс, сущность, ["Ref_Key", поле], СВОБОДНЫЙ_ТЕКСТ_ЗАПИСЕЙ, текст
            )
    итог["свободный_текст"] = {**текст.итог(), "записей": по_сущностям}


async def main() -> int:
    разбор = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    разбор.add_argument("--label", required=True, help="метка прогона: before / after")
    разбор.add_argument(
        "--home",
        default=str(pathlib.Path.home() / ".claude" / "odata1c"),
        help="рабочий домашний каталог (только чтение)",
    )
    разбор.add_argument("--src", help="каталог src другой версии кода (через PYTHONPATH)")
    разбор.add_argument("--out", help="куда сохранить итог JSON (счётчики, без значений)")
    аргументы = разбор.parse_args()

    дом = собрать_дом(pathlib.Path(аргументы.home))
    итог: dict = {"label": аргументы.label}
    print(f"прогон {аргументы.label}: временный дом создан, порт демона {ПОРТ}")
    try:
        async with сессия(дом, аргументы.src) as сеанс:
            await прогон(сеанс, итог)
    finally:
        остановить_демон(дом, аргументы.src)
        shutil.rmtree(дом, ignore_errors=True)
        print(f"временный дом удалён: {not дом.exists()}")
    print(json.dumps(итог, ensure_ascii=False, indent=2))
    if аргументы.out:
        pathlib.Path(аргументы.out).write_text(
            json.dumps(итог, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
