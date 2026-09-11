"""Приёмка M1d на живой базе 1С: десять вопросов чтения и проверка гейта на настоящих данных.

Скрипт разговаривает со шлюзом ровно так, как это делает Claude Code: поднимает настоящий
лаунчер `odata1c mcp` отдельным процессом, общается с ним по stdio-протоколу MCP и вызывает
тулы. Ничего внутрь пакета он не импортирует, кроме чтения `bases.yaml` — оттуда берутся имена
баз и (для этапа гейта) строка подключения временной копии базы.

Учётные данные из `bases.yaml` в вывод не попадают: всё, что скрипт печатает, проходит через
`Скрыватель` — он вырезает адрес, имя пользователя, пароль и имя хоста публикации.

Запуск из корня репозитория:

    uv run python tools/probes/m1d_live_check.py                # оба этапа
    uv run python tools/probes/m1d_live_check.py --phase read   # только десять вопросов
    uv run python tools/probes/m1d_live_check.py --phase gate   # только проверка гейта

Этап `gate` временно дописывает в `bases.yaml` рабочего дома две записи — копии боевой базы
с ролью `prod` (гейт `identifiers+names`) и с гейтом `off` — и удаляет их вместе с каталогами
индекса по завершении, в том числе при ошибке. Исходный `bases.yaml` сохраняется рядом
(`bases.yaml.m1d-backup`) и возвращается на место.

Демон — один на машину и читает настройки один раз при старте, поэтому перед каждым этапом
скрипт останавливает работающий демон: иначе новые записи `bases.yaml` через MCP не видны.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import datetime
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator

import yaml
from mcp import StdioServerParameters, stdio_client
from mcp.client.session import ClientSession

# Имена временных баз этапа гейта. `_prod` — роль prod (гейт identifiers+names), `_off` — та же
# база с выключенным гейтом: она даёт эталон настоящих значений, с которым сверяется первая.
БАЗА_PROD = "trade_dev_prod"
БАЗА_OFF = "trade_dev_off"

# Запас на холодный старт демона (импорт lxml/ahocorasick, разбор daemon.yaml) и на подъём
# сеанса 1С после простоя. Виртуальные таблицы идут до virtual_timeout_s = 180 с.
# Секунды числом, не `timedelta`: в mcp 2.x `read_timeout_seconds` складывается с показанием
# часов напрямую, и `timedelta` роняет вызов ошибкой типа внутри anyio.
ТАЙМАУТ_ВЫЗОВА = 240.0

# Серия из 10 или 12 цифр подряд — форма ИНН. Ищется в готовом тексте ответа, а не по полям:
# страж работает по сериализованному ответу, и проверять его надо тем же способом.
ИНН_В_ТЕКСТЕ = re.compile(r"(?<!\d)(\d{10}|\d{12})(?!\d)")


@dataclasses.dataclass
class Проверка:
    """Результат одного вопроса приёмки."""

    номер: str
    название: str
    вызов: str
    секунды: float
    вердикт: bool
    замечание: str
    выдержка: str = ""


class Скрыватель:
    """Вырезает учётные данные и адрес базы из любого текста, который скрипт печатает."""

    def __init__(self, секреты: Iterable[str]) -> None:
        # Сначала длинные: адрес целиком должен схлопнуться раньше, чем имя хоста внутри него.
        self._секреты = sorted({с for с in секреты if с and len(с) >= 4}, key=len, reverse=True)

    def __call__(self, текст: str) -> str:
        for секрет in self._секреты:
            текст = текст.replace(секрет, "<скрыто>")
        return текст


def собрать_скрыватель(bases: dict) -> Скрыватель:
    """Секреты для вырезания: адрес публикации, его хост, имя пользователя, пароль."""
    секреты: list[str] = []
    for запись in bases.values():
        if not isinstance(запись, dict):
            continue
        адрес = запись.get("url") or ""
        секреты.append(адрес)
        секреты.append(запись.get("user") or "")
        секреты.append(запись.get("password") or "")
        хост = адрес.split("//", 1)[-1].split("/", 1)[0]
        секреты.append(хост)
        if хост:
            секреты.append(хост.split(".", 1)[0])
    return Скрыватель(секреты)


def прочитать_bases(home: pathlib.Path) -> dict:
    return yaml.safe_load((home / "bases.yaml").read_text(encoding="utf-8")) or {}


# -- управление демоном и сессией ---------------------------------------------------------------


def остановить_демон(home: pathlib.Path) -> None:
    """`odata1c daemon stop`. Отсутствие демона — не ошибка: команда просто скажет, что его нет."""
    subprocess.run(  # noqa: S603 — фиксированная команда собственного пакета
        [sys.executable, "-m", "odata1c", "daemon", "stop", "--home", str(home)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        check=False,
    )
    # Демон освобождает порт не мгновенно — следующий лаунчер иначе достучится до умирающего.
    time.sleep(1.5)


@contextlib.asynccontextmanager
async def сессия(home: pathlib.Path, *, default: str | None = None):
    """Настоящий лаунчер `odata1c mcp` как stdio-сервер MCP, как его запускает Claude Code."""
    аргументы = ["-m", "odata1c", "mcp", "--home", str(home)]
    if default:
        аргументы += ["--default", default]
    параметры = StdioServerParameters(
        command=sys.executable,
        args=аргументы,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    async with stdio_client(параметры) as (чтение, запись):
        async with ClientSession(чтение, запись) as сеанс:
            await сеанс.initialize()
            yield сеанс


async def вызов(сеанс: ClientSession, имя: str, аргументы: dict) -> tuple[str, float]:
    """Вызвать тул и вернуть текст ответа и время вызова в секундах."""
    начало = time.perf_counter()
    ответ = await сеанс.call_tool(имя, аргументы, read_timeout_seconds=ТАЙМАУТ_ВЫЗОВА)
    секунды = time.perf_counter() - начало
    куски = [часть.text for часть in ответ.content if getattr(часть, "text", None)]
    return "\n".join(куски), секунды


def как_json(текст: str) -> dict:
    """Разобрать ответ тула как JSON; не-JSON (markdown или мусор) — пустой словарь."""
    try:
        разобранное = json.loads(текст)
    except json.JSONDecodeError:
        return {}
    return разобранное if isinstance(разобранное, dict) else {}


def код_ошибки(текст: str) -> str | None:
    ошибка = как_json(текст).get("error")
    return ошибка.get("code") if isinstance(ошибка, dict) else None


def выдержка(текст: str, скрыть: Скрыватель, длина: int = 400) -> str:
    очищенное = скрыть(текст).replace("\n", " ")
    return очищенное if len(очищенное) <= длина else очищенное[:длина] + "…"


# -- этап 1: десять вопросов чтения -------------------------------------------------------------


async def вопросы_чтения(home: pathlib.Path, скрыть: Скрыватель) -> tuple[list[Проверка], float]:
    """Десять вопросов приёмки SPEC §12 на базе по умолчанию. Второй результат — холодный старт."""
    проверки: list[Проверка] = []
    остановить_демон(home)

    начало_старта = time.perf_counter()
    async with сессия(home) as сеанс:
        холодный_старт = time.perf_counter() - начало_старта
        перечень = await сеанс.list_tools()
        проверки.append(
            Проверка(
                "0",
                "холодный старт: лаунчер поднял демон и отдал перечень тулов",
                "initialize + list_tools",
                холодный_старт,
                bool(перечень.tools),
                f"тулов объявлено: {len(перечень.tools)}",
                ", ".join(т.name for т in перечень.tools),
            )
        )

        проверки.append(await вопрос_1_базы(сеанс, скрыть))
        проверки.append(await вопрос_2_поиск(сеанс, скрыть))
        проверки.append(await вопрос_3_описание(сеанс, скрыть))
        проверки.append(await вопрос_4_валюты(сеанс, скрыть))
        пятый, ключ = await вопрос_5_реализация(сеанс, скрыть)
        проверки.append(пятый)
        проверки.append(await вопрос_6_объект(сеанс, скрыть, ключ))
        проверки.append(await вопрос_7_остатки(сеанс, скрыть))
        проверки.append(await вопрос_8_обороты_без_периода(сеанс, скрыть, home))
        проверки.append(await вопрос_9_balance_у_оборотного(сеанс, скрыть))
        проверки.extend(await вопрос_10_рецепты(сеанс, скрыть))
    return проверки, холодный_старт


async def вопрос_1_базы(сеанс: ClientSession, скрыть: Скрыватель) -> Проверка:
    текст, секунды = await вызов(сеанс, "odata1c_bases", {})
    конверт = как_json(текст)
    базы = {строка.get("name"): строка for строка in конверт.get("bases", [])}
    запись = базы.get("trade_dev", {})
    вердикт = запись.get("role") == "dev" and конверт.get("default") == "trade_dev"
    замечание = (
        f"роль={запись.get('role')}, гейт={запись.get('gate')}, "
        f"индекс={запись.get('indexed')}, сущностей={запись.get('entity_count')}, "
        f"по умолчанию={конверт.get('default')}"
    )
    return Проверка("1", "odata1c_bases: видна trade_dev", "bases", секунды, вердикт, замечание)


async def вопрос_2_поиск(сеанс: ClientSession, скрыть: Скрыватель) -> Проверка:
    текст, секунды = await вызов(сеанс, "odata1c_find_entity", {"query": "контрагенты"})
    сущности = как_json(текст).get("entities", [])
    имена = [с.get("name") for с in сущности]
    вердикт = bool(имена) and имена[0] == "Catalog_Контрагенты"
    return Проверка(
        "2",
        "odata1c_find_entity('контрагенты'): Catalog_Контрагенты первым",
        "find_entity",
        секунды,
        вердикт,
        f"найдено {len(имена)}, первые: {', '.join(имена[:5])}",
    )


async def вопрос_3_описание(сеанс: ClientSession, скрыть: Скрыватель) -> Проверка:
    текст, секунды = await вызов(
        сеанс, "odata1c_describe_entity", {"entity": "Document_РеализацияТоваровУслуг"}
    )
    есть_навигация = "Контрагент" in текст
    есть_проведение = "Post" in текст
    return Проверка(
        "3",
        "odata1c_describe_entity('Document_РеализацияТоваровУслуг')",
        "describe_entity",
        секунды,
        есть_навигация and есть_проведение,
        f"навигация Контрагент={есть_навигация}, Post={есть_проведение}, "
        f"длина ответа {len(текст)} симв.",
        выдержка(текст, скрыть, 300),
    )


async def вопрос_4_валюты(сеанс: ClientSession, скрыть: Скрыватель) -> Проверка:
    текст, секунды = await вызов(
        сеанс,
        "odata1c_query",
        {
            "entity": "Catalog_Валюты",
            "select": ["Ref_Key", "Code", "Description"],
            "top": 5,
            "inlinecount": True,
        },
    )
    конверт = как_json(текст)
    всего = конверт.get("total")
    return Проверка(
        "4",
        "odata1c_query Catalog_Валюты с select и inlinecount",
        "query",
        секунды,
        isinstance(всего, int),
        f"total={всего!r} ({type(всего).__name__}), записей={конверт.get('count')}",
        выдержка(текст, скрыть, 300),
    )


async def вопрос_5_реализация(
    сеанс: ClientSession, скрыть: Скрыватель
) -> tuple[Проверка, str | None]:
    текст, секунды = await вызов(
        сеанс,
        "odata1c_query",
        {
            "entity": "Document_РеализацияТоваровУслуг",
            "select": ["Ref_Key", "Number", "Date", "Контрагент"],
            "expand": ["Контрагент"],
            "top": 3,
        },
    )
    записи = как_json(текст).get("items", [])
    раскрыт = bool(записи) and all(
        isinstance(з.get("Контрагент"), dict) and "Description" in з["Контрагент"] for з in записи
    )
    ключ = записи[0].get("Ref_Key") if записи else None
    замечание = f"записей={len(записи)}, Контрагент.Description у всех={раскрыт}"
    if записи and not раскрыт:
        замечание += f"; вид поля Контрагент: {type(записи[0].get('Контрагент')).__name__}"
    return (
        Проверка(
            "5",
            "odata1c_query Document_РеализацияТоваровУслуг с expand=Контрагент",
            "query",
            секунды,
            раскрыт,
            замечание,
            выдержка(текст, скрыть, 400),
        ),
        ключ,
    )


async def вопрос_6_объект(сеанс: ClientSession, скрыть: Скрыватель, ключ: str | None) -> Проверка:
    if not ключ:
        return Проверка(
            "6", "odata1c_get документа по Ref_Key", "get", 0.0, False, "вопрос 5 не дал Ref_Key"
        )
    текст, секунды = await вызов(
        сеанс,
        "odata1c_get",
        {
            "entity": "Document_РеализацияТоваровУслуг",
            "key": ключ,
            "select": ["Ref_Key", "Number", "Date", "СуммаДокумента"],
        },
    )
    конверт = как_json(текст)
    объект = конверт.get("item") or конверт.get("items", [{}])[0] if конверт else {}
    вердикт = isinstance(объект, dict) and объект.get("Ref_Key") == ключ
    return Проверка(
        "6",
        "odata1c_get документа по Ref_Key из вопроса 5",
        "get",
        секунды,
        вердикт,
        f"ключи конверта: {', '.join(конверт)}",
        выдержка(текст, скрыть, 300),
    )


def момент_остатков() -> str:
    """Полночь сегодняшнего дня в формате литерала OData 1С."""
    сегодня = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return сегодня.strftime("%Y-%m-%dT%H:%M:%S")


async def вопрос_7_остатки(сеанс: ClientSession, скрыть: Скрыватель) -> Проверка:
    текст, секунды = await вызов(
        сеанс,
        "odata1c_query",
        {
            "entity": "AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance",
            "params": {"Period": момент_остатков()},
            "top": 10,
        },
    )
    конверт = как_json(текст)
    вердикт = "error" not in конверт and секунды < 180
    return Проверка(
        "7",
        "остатки AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance на дату",
        "query (виртуальная таблица)",
        секунды,
        вердикт,
        f"записей={конверт.get('count')}, ошибка={код_ошибки(текст)}, "
        f"предел virtual_timeout_s=180 с",
        выдержка(текст, скрыть, 300),
    )


def строк_в_журнале(home: pathlib.Path) -> int:
    журнал = home / "logs" / "daemon.log"
    if not журнал.exists():
        return 0
    return len(журнал.read_text(encoding="utf-8", errors="replace").splitlines())


def запросов_к_1с_после(home: pathlib.Path, было: int) -> int:
    """Сколько строк «HTTP Request» дописал httpx в журнал демона после отметки `было`."""
    журнал = home / "logs" / "daemon.log"
    if not журнал.exists():
        return 0
    строки = журнал.read_text(encoding="utf-8", errors="replace").splitlines()[было:]
    return sum(1 for строка in строки if "HTTP Request" in строка)


async def вопрос_8_обороты_без_периода(
    сеанс: ClientSession, скрыть: Скрыватель, home: pathlib.Path
) -> Проверка:
    было = строк_в_журнале(home)
    текст, секунды = await вызов(
        сеанс,
        "odata1c_query",
        {"entity": "AccumulationRegister_РасчетыСКлиентамиПланОплат_Turnovers", "top": 5},
    )
    код = код_ошибки(текст)
    # Журнал демона пишется асинхронно — дать ему дописаться, прежде чем считать строки.
    await asyncio.sleep(1.0)
    запросов = запросов_к_1с_после(home, было)
    return Проверка(
        "8",
        "обороты без периода: отказ до обращения к 1С",
        "query (виртуальная таблица)",
        секунды,
        код == "params_invalid" and запросов == 0,
        f"код={код}, запросов к 1С по журналу демона={запросов}",
        выдержка(текст, скрыть, 400),
    )


async def вопрос_9_balance_у_оборотного(сеанс: ClientSession, скрыть: Скрыватель) -> Проверка:
    текст, секунды = await вызов(
        сеанс,
        "odata1c_query",
        {"entity": "AccumulationRegister_ВыручкаИСебестоимостьПродаж_Balance", "top": 5},
    )
    код = код_ошибки(текст)
    подсказка = (как_json(текст).get("error") or {}).get("hint", "")
    есть_список = "Turnovers" in текст
    return Проверка(
        "9",
        "Balance у оборотного регистра: entity_unknown со списком виртуальных таблиц",
        "query",
        секунды,
        код == "entity_unknown" and есть_список,
        f"код={код}, в подсказке названы виртуальные таблицы={есть_список}",
        выдержка(скрыть(подсказка) or текст, скрыть, 400),
    )


async def вопрос_10_рецепты(сеанс: ClientSession, скрыть: Скрыватель) -> list[Проверка]:
    """Перечень рецептов и выполнение первого применимого."""
    текст, секунды = await вызов(сеанс, "odata1c_recipe", {})
    конверт = как_json(текст)
    рецепты = конверт.get("recipes", [])
    перечень = Проверка(
        "10a",
        "odata1c_recipe без name: перечень рецептов базы",
        "recipe",
        секунды,
        bool(рецепты),
        f"рецептов={len(рецепты)}: {', '.join(str(р.get('name')) for р in рецепты[:8])}",
        выдержка(текст, скрыть, 400),
    )
    if not рецепты:
        return [перечень]

    применимый = next((р for р in рецепты if р.get("applicable") is not False), рецепты[0])
    имя = применимый.get("name")
    # `params` перечня — список описаний, не словарь: [{name, type, required, description}, …].
    # Обязательные параметры рецептов УТ — моменты времени, их и подставляем.
    параметры = {
        описание["name"]: момент_остатков()
        for описание in применимый.get("params") or []
        if описание.get("required")
    }
    текст2, секунды2 = await вызов(сеанс, "odata1c_recipe", {"name": имя, "params": параметры})
    конверт2 = как_json(текст2)
    выполнение = Проверка(
        "10b",
        f"odata1c_recipe name={имя}: выполнение",
        "recipe",
        секунды2,
        "error" not in конверт2,
        f"записей={конверт2.get('count')}, ошибка={код_ошибки(текст2)}",
        выдержка(текст2, скрыть, 400),
    )
    return [перечень, выполнение]


@contextlib.contextmanager
def шаблон_рецептов_ут(home: pathlib.Path) -> Iterator[None]:
    """Временно положить в базу шаблон рецептов УТ из поставки и вернуть прежний файл.

    Нужно, потому что база рабочего дома заведена без `--recipes ut`, и её `recipes.yaml` пуст.
    Файл пользователя правится только на время прогона: приёмка не должна менять чужие настройки.
    """
    целевой = home / "bases" / "trade_dev" / "recipes.yaml"
    было = целевой.read_text(encoding="utf-8") if целевой.exists() else None
    шаблон = pathlib.Path(__file__).resolve().parents[2] / "src/odata1c/templates/recipes/ut.yaml"
    целевой.write_text(шаблон.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        yield
    finally:
        if было is None:
            целевой.unlink(missing_ok=True)
        else:
            целевой.write_text(было, encoding="utf-8")


# -- этап 2: гейт на живых данных ---------------------------------------------------------------


@contextlib.contextmanager
def временные_базы(home: pathlib.Path) -> Iterator[None]:
    """Дописать в bases.yaml копии боевой базы с ролью prod и с гейтом off, убрать по выходе.

    Каталог индекса копируется с исходной базы, а не строится заново: 16,8 МБ `$metadata` и
    7224 сущности — это минуты на каждую копию, а в `meta` индекса имени базы нет, только
    контрольная сумма `$metadata`, дата и версия разборщика.
    """
    файл = home / "bases.yaml"
    запасной = home / "bases.yaml.m1d-backup"
    исходный_текст = файл.read_text(encoding="utf-8")
    запасной.write_text(исходный_текст, encoding="utf-8")

    данные = yaml.safe_load(исходный_текст) or {}
    образец = данные["bases"]["trade_dev"]
    for имя, роль, гейт in ((БАЗА_PROD, "prod", "identifiers+names"), (БАЗА_OFF, "dev", "off")):
        запись = {
            "label": f"копия базы приёмки M1d ({роль}, гейт {гейт})",
            "url": образец["url"],
            "user": образец["user"],
            "password": образец.get("password", ""),
            "role": роль,
            "gate": {"mode": гейт},
        }
        данные["bases"][имя] = запись
        каталог = home / "bases" / имя
        if not каталог.exists():
            shutil.copytree(home / "bases" / "trade_dev", каталог)
    файл.write_text(yaml.safe_dump(данные, allow_unicode=True, sort_keys=False), encoding="utf-8")
    try:
        yield
    finally:
        остановить_демон(home)
        файл.write_text(исходный_текст, encoding="utf-8")
        запасной.unlink(missing_ok=True)
        for имя in (БАЗА_PROD, БАЗА_OFF):
            shutil.rmtree(home / "bases" / имя, ignore_errors=True)


# Поля выборки подобраны по реальному описанию базы (`odata1c_describe_entity`), а не по памяти:
# у справочника контрагентов этой УТ нет поля `Code` (нумерация отключена), а флаг проведения
# документа в OData называется `Posted`, не `Проведен`.
ПОЛЯ_КОНТРАГЕНТА = [
    "Ref_Key",
    "DeletionMark",
    "Description",
    "НаименованиеПолное",
    "ИНН",
    "КПП",
    "КодПоОКПО",
    "ЮрФизЛицо",
    "Партнер_Key",
]
ПОЛЯ_ДОКУМЕНТА = ["Ref_Key", "Number", "Date", "СуммаДокумента", "Posted", "Контрагент_Key"]

СТРОКА_ОПИСАНИЯ = re.compile(r"^\|\s*([^|]+?)\s*\|.*\|\s*([a-z_]*)\s*\|$")


def классы_полей(описание: str) -> dict[str, str]:
    """Разобрать markdown-таблицу `describe_entity`: поле → класс гейта (пусто — не защищено)."""
    классы: dict[str, str] = {}
    for строка in описание.splitlines():
        совпадение = СТРОКА_ОПИСАНИЯ.match(строка.strip())
        if совпадение and совпадение[1] not in ("Поле", "---"):
            классы[совпадение[1]] = совпадение[2]
    return классы


async def проверка_гейта(home: pathlib.Path, скрыть: Скрыватель) -> list[Проверка]:
    """Одна и та же выборка на базе с гейтом и на базе без него; сверка текст-в-текст.

    Эталон — ответ базы с гейтом `off`: в нём настоящие значения. Проверяемое — ответ базы с
    ролью `prod` (гейт `identifiers+names`) на ту же выборку. Класс каждого поля берётся из
    `describe_entity` той же базы, поэтому проверка не зависит от того, что автор помнит о политике.
    """
    проверки: list[Проверка] = []
    остановить_демон(home)
    async with сессия(home) as сеанс:
        описание_контрагентов, _ = await вызов(
            сеанс,
            "odata1c_describe_entity",
            {"base": БАЗА_PROD, "entity": "Catalog_Контрагенты"},
        )
        классы = классы_полей(описание_контрагентов)

        отбор = {
            "entity": "Catalog_Контрагенты",
            "select": ПОЛЯ_КОНТРАГЕНТА,
            "filter": "DeletionMark eq false",
            "orderby": "Ref_Key asc",
            "top": 25,
        }
        контрагенты_off, _ = await вызов(сеанс, "odata1c_query", {**отбор, "base": БАЗА_OFF})
        контрагенты_prod, секунды = await вызов(
            сеанс, "odata1c_query", {**отбор, "base": БАЗА_PROD}
        )
        проверки.extend(
            сверить_контрагентов(контрагенты_off, контрагенты_prod, классы, секунды, скрыть)
        )

        отбор_документов = {
            "entity": "Document_РеализацияТоваровУслуг",
            "select": ПОЛЯ_ДОКУМЕНТА,
            "orderby": "Date desc",
            "top": 10,
        }
        документы_off, _ = await вызов(
            сеанс, "odata1c_query", {**отбор_документов, "base": БАЗА_OFF}
        )
        документы_prod, секунды = await вызов(
            сеанс, "odata1c_query", {**отбор_документов, "base": БАЗА_PROD}
        )
        проверки.append(сверить_документы(документы_off, документы_prod, секунды, скрыть))
    проверки.append(проверить_словарь(home))
    return проверки


# Правдоподобная длина значения класса: ИНН — 10 или 12 цифр, КПП — 9, счёт — 20, телефон — 10.
# Значение короче взято из мусорной записи базы, и страж будет подменять им всё подряд.
МИНИМАЛЬНАЯ_ДЛИНА_КЛАССА = {"inn": 10, "kpp": 9, "account": 20, "phone": 10, "passport": 10}


def проверить_словарь(home: pathlib.Path) -> Проверка:
    """В словаре гейта не должно быть значений короче правдоподобной длины своего класса.

    Проверка не по MCP, а прямо по `gate.sqlite` только на чтение: это причина, а не следствие.
    Значение длиной в один-два символа попадает в словарь из мусорной записи базы (КПП «1»),
    после чего страж заменяет токеном каждое его вхождение в любом ответе — в номере документа,
    в дате, в коде ОКПО. Настоящие значения при этом не раскрываются, но ответ становится
    непригодным, а инвариант 6 нарушается.
    """
    файл = home / "gate.sqlite"
    if not файл.exists():
        return Проверка("Г7", "словарь гейта", "gate.sqlite", 0.0, True, "словаря ещё нет")
    соединение = sqlite3.connect(f"file:{файл}?mode=ro", uri=True)
    try:
        строки = соединение.execute(
            "SELECT token, type, LENGTH(normalized) AS длина, first_field FROM tokens"
        ).fetchall()
    finally:
        соединение.close()
    короткие = [
        f"{токен} (класс {класс}, длина {длина}, поле {поле})"
        for токен, класс, длина, поле in строки
        if длина < МИНИМАЛЬНАЯ_ДЛИНА_КЛАССА.get(класс, 0)
    ]
    return Проверка(
        "Г7",
        "в словаре гейта нет значений короче правдоподобной длины класса",
        "gate.sqlite (только чтение)",
        0.0,
        not короткие,
        f"токенов в словаре={len(строки)}, слишком коротких={len(короткие)}",
        "; ".join(короткие[:10]),
    )


def значения_класса(записи: list[dict], классы: dict[str, str], класс: str) -> set[str]:
    """Настоящие значения полей заданного класса из эталонного ответа."""
    собрано = set()
    for запись in записи:
        for поле, значение in запись.items():
            if классы.get(поле) == класс and isinstance(значение, str) and значение.strip():
                собрано.add(значение.strip())
    return собрано


def сверить_контрагентов(
    сырой_off: str,
    сырой_prod: str,
    классы: dict[str, str],
    секунды: float,
    скрыть: Скрыватель,
) -> list[Проверка]:
    записи_off = как_json(сырой_off).get("items", [])
    записи_prod = как_json(сырой_prod).get("items", [])

    настоящие_инн = {и for и in значения_класса(записи_off, классы, "inn") if len(и) >= 10}
    названия = значения_класса(записи_off, классы, "org")
    # Короткие названия отсекаются: «ООО» или «АО» встретятся в любом тексте и дали бы ложное
    # срабатывание. Отсечённые называются в замечании, чтобы проверка не выглядела полнее, чем есть.
    длинные = {н for н in названия if len(н) >= 8}
    короткие = названия - длинные

    утёкшие_инн = sorted(и for и in настоящие_инн if и in сырой_prod)
    утёкшие_названия = sorted(н for н in длинные if н in сырой_prod)
    цифровые_серии = sorted(set(ИНН_В_ТЕКСТЕ.findall(сырой_prod)))
    токенов = len(re.findall(r"\[\[[a-z_]+:[^\]]+\]\]", сырой_prod))

    # Поля с классом гейта обязаны прийти токеном, поля без класса — прийти как есть (инвариант 6).
    не_токен: list[str] = []
    расхождения: list[str] = []
    for слева, справа in zip(записи_off, записи_prod, strict=False):
        for поле, значение in справа.items():
            if классы.get(поле):
                if isinstance(слева.get(поле), str) and слева[поле].strip():
                    if not (isinstance(значение, str) and значение.startswith("[[")):
                        не_токен.append(поле)
            elif значение != слева.get(поле):
                расхождения.append(поле)

    return [
        Проверка(
            "Г1",
            "ИНН из базы не встречается в ответе базы с гейтом",
            "query Catalog_Контрагенты",
            секунды,
            not утёкшие_инн and bool(настоящие_инн),
            f"эталонных ИНН={len(настоящие_инн)}, утекло={len(утёкшие_инн)}, "
            f"токенов в ответе={токенов}",
        ),
        Проверка(
            "Г2",
            "названия организаций не встречаются в ответе базы с гейтом",
            "query Catalog_Контрагенты",
            секунды,
            not утёкшие_названия and bool(длинные),
            f"эталонных названий длиной ≥8={len(длинные)}, утекло={len(утёкшие_названия)}; "
            f"не проверялись короткие ({len(короткие)})",
        ),
        Проверка(
            "Г3",
            "каждое защищённое поле пришло токеном",
            "query Catalog_Контрагенты",
            секунды,
            not не_токен and bool(записи_prod),
            f"записей={len(записи_prod)}, классы полей выборки: "
            + ", ".join(f"{п}={классы.get(п) or '—'}" for п in ПОЛЯ_КОНТРАГЕНТА)
            + f"; пришли не токеном: {', '.join(sorted(set(не_токен))) or 'нет'}",
        ),
        Проверка(
            "Г4",
            "незащищённые поля контрагента совпадают побайтно (инвариант 6)",
            "query Catalog_Контрагенты",
            секунды,
            not расхождения and bool(записи_off),
            f"записей off={len(записи_off)}, prod={len(записи_prod)}, "
            f"расхождения: {', '.join(sorted(set(расхождения))) or 'нет'}",
        ),
        Проверка(
            "Г5",
            "серии из 10/12 цифр в ответе с гейтом — только незащищённые коды",
            "query Catalog_Контрагенты",
            секунды,
            not (set(цифровые_серии) & настоящие_инн),
            f"найдено серий={len(цифровые_серии)}, из них настоящих ИНН="
            f"{len(set(цифровые_серии) & настоящие_инн)}",
            "; ".join(
                f"{с} — поле {', '.join(sorted(поля_со_значением(записи_prod, с))) or 'не найдено'}"
                for с in цифровые_серии[:10]
            ),
        ),
    ]


def поля_со_значением(записи: list[dict], значение: str) -> set[str]:
    """В каких полях выборки встречается такая строка — чтобы назвать источник цифровой серии."""
    найдено = set()
    for запись in записи:
        for поле, текущее in запись.items():
            if isinstance(текущее, str) and значение in текущее:
                найдено.add(поле)
    return найдено


def сверить_документы(
    сырой_off: str, сырой_prod: str, секунды: float, скрыть: Скрыватель
) -> Проверка:
    записи_off = как_json(сырой_off).get("items", [])
    записи_prod = как_json(сырой_prod).get("items", [])
    поля = ("Ref_Key", "Number", "Date", "СуммаДокумента", "Posted", "Контрагент_Key")
    расхождения = []
    for слева, справа in zip(записи_off, записи_prod, strict=False):
        for поле in поля:
            if слева.get(поле) != справа.get(поле):
                расхождения.append(поле)
    return Проверка(
        "Г6",
        "номер, дата, сумма, GUID и флаг проведения не тронуты гейтом (инвариант 6)",
        "query Document_РеализацияТоваровУслуг",
        секунды,
        not расхождения and bool(записи_off),
        f"записей off={len(записи_off)}, prod={len(записи_prod)}, "
        f"расхождения по полям: {', '.join(sorted(set(расхождения))) or 'нет'}",
        выдержка(сырой_prod, скрыть, 400),
    )
# -- вывод --------------------------------------------------------------------------------------


def напечатать(проверки: list[Проверка], скрыть: Скрыватель) -> None:
    print("\n| № | Проверка | Тул | Время, с | Итог | Факты |")
    print("|---|---|---|---|---|---|")
    for п in проверки:
        print(
            f"| {п.номер} | {скрыть(п.название)} | {п.вызов} | {п.секунды:.2f} | "
            f"{'да' if п.вердикт else 'НЕТ'} | {скрыть(п.замечание)} |"
        )
    print("\nВыдержки из ответов:")
    for п in проверки:
        if п.выдержка:
            print(f"\n[{п.номер}] {скрыть(п.выдержка)}")


async def главная(аргументы: argparse.Namespace) -> int:
    home = pathlib.Path(аргументы.home).expanduser()
    скрыть = собрать_скрыватель(прочитать_bases(home).get("bases", {}))
    все: list[Проверка] = []

    if аргументы.phase in ("read", "all"):
        if аргументы.recipes_template:
            with шаблон_рецептов_ут(home):
                проверки, холодный = await вопросы_чтения(home, скрыть)
        else:
            проверки, холодный = await вопросы_чтения(home, скрыть)
        все += проверки
        print(f"\nХолодный старт демона: {холодный:.2f} с")

    if аргументы.phase in ("gate", "all"):
        with временные_базы(home):
            все += await проверка_гейта(home, скрыть)
        # После восстановления bases.yaml демон снова поднимается на исходных настройках —
        # следующий запуск Claude Code не должен видеть временных баз.
        остановить_демон(home)

    напечатать(все, скрыть)
    провалов = sum(1 for п in все if not п.вердикт)
    print(f"\nИтого проверок: {len(все)}, не прошло: {провалов}")
    if аргументы.json:
        pathlib.Path(аргументы.json).write_text(
            json.dumps(
                [
                    {**dataclasses.asdict(п), "замечание": скрыть(п.замечание)}
                    for п in все
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0 if провалов == 0 else 1


def main() -> int:
    разборщик = argparse.ArgumentParser(description="Приёмка M1d на живой базе 1С")
    разборщик.add_argument(
        "--home",
        default=str(pathlib.Path.home() / ".claude" / "odata1c"),
        help="домашний каталог шлюза с рабочим bases.yaml",
    )
    разборщик.add_argument("--phase", choices=("read", "gate", "all"), default="all")
    разборщик.add_argument("--json", help="куда сложить результаты машиночитаемо")
    разборщик.add_argument(
        "--recipes-template",
        action="store_true",
        help="на время прогона положить в базу шаблон рецептов УТ из поставки",
    )
    return asyncio.run(главная(разборщик.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
