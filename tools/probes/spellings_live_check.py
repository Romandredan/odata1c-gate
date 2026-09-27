"""Проверка отбора по токену с несколькими написаниями значения (Б-1) на живой базе 1С.

Только чтение: к 1С уходят одни GET — прокси-счётчик между временным демоном и базой других
методов не пропускает вовсе. Демон и словарь живут во ВРЕМЕННОМ домашнем каталоге (запись базы,
индекс и политика — копией из рабочего дома, словарь пустой, секрет свой); рабочий дом владельца
и его демон на 7171 не трогаются.

Что печатается. Реальные значения — никогда: только счётчики, имена сущностей и полей и формы
написаний (цифра → 9, буква → a). Учётные данные читаются из `bases.yaml` и никуда не выводятся.

Что меряется.

1. Прямым чтением (мимо шлюза): у каких значений защищаемых полей в базе несколько написаний —
   значения группируются по нормализованной форме (`tokens.normalize_value`, та же свёртка, что у
   словаря), и группа с двумя и более разными строками — одно значение в нескольких написаниях.
2. Для каждой такой группы (до `--groups` на поле) — прямым чтением: сколько записей находит `eq`
   по каждому написанию отдельно (так искал шлюз до правки — одним, самым свежим написанием).
3. Через шлюз (настоящий лаунчер `odata1c mcp`, stdio): записи группы читаются по `Ref_Key` —
   словарь узнаёт все написания; у всех записей группы один токен (инвариант 5); затем отбор
   `поле eq '<токен>'` — сколько записей группы найдено, сколько условий ушло в 1С (по прокси) и
   нет ли в ответе ни одного написания значения.

Запуск из корня репозитория:

    uv run python tools/probes/spellings_live_check.py
    uv run python tools/probes/spellings_live_check.py --groups 3 --out итог.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import http.server
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import defaultdict

import httpx
import yaml
from mcp import StdioServerParameters, stdio_client
from mcp.client.session import ClientSession

from odata1c.config.models import адрес_odata
from odata1c.gate.tokens import TOKEN_RE, normalize_value

БАЗА = "trade_dev"
ПОРТ = 7193
ПОРТ_ПРОКСИ = 7194
ТАЙМАУТ = 240.0
СТРАНИЦА_СЫРАЯ = 1000

# (сущность, поле, класс) — поля с классом в политике базы, где у одного значения бывают разные
# написания: реквизиты (пробелы), названия (кавычки, регистр), ФИО.
ПОЛЯ = (
    ("Catalog_Контрагенты", "ИНН", "inn"),
    ("Catalog_Контрагенты", "КПП", "kpp"),
    ("Catalog_Контрагенты", "Description", "org"),
    ("Catalog_Контрагенты", "НаименованиеПолное", "org"),
    ("Catalog_Партнеры", "Description", "org"),
    ("Catalog_Партнеры", "НаименованиеПолное", "org"),
    ("Catalog_ФизическиеЛица", "Description", "person"),
    ("Catalog_ФизическиеЛица", "ИНН", "inn"),
    ("Catalog_КонтактныеЛицаПартнеров", "Description", "person"),
    ("Catalog_БанковскиеСчетаКонтрагентов", "НаименованиеБанка", "org"),
    ("Catalog_БанковскиеСчетаКонтрагентов", "НомерСчета", "acc"),
)


def форма(текст: str) -> str:
    return re.sub(r"[^\W\d_]", "a", re.sub(r"\d", "9", текст))


def нормализовать(класс: str, значение: str) -> str:
    """Та же свёртка, что у `Dictionary.token_for`: `normalize_value`, пустое — по пробелам."""
    return normalize_value(класс, значение) or " ".join(значение.split())


def литерал(значение: str) -> str:
    return "'" + значение.replace("'", "''") + "'"


class Прокси:
    """Прокси между временным демоном и 1С: пропускает только GET, считает обращения и помнит
    последний адрес (в вывод адрес не попадает — только число условий в `$filter`)."""

    def __init__(self, настоящий_адрес: str) -> None:
        разбор = urllib.parse.urlsplit(настоящий_адрес)
        self.источник = f"{разбор.scheme}://{разбор.netloc}"
        self.адрес = f"http://127.0.0.1:{ПОРТ_ПРОКСИ}{разбор.path}"
        self.счёт = 0
        self.последний = ""
        self.отклонено = 0
        self._клиент = httpx.Client(timeout=120)
        прокси = self

        class Обработчик(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — имя метода задаёт http.server
                прокси.счёт += 1
                прокси.последний = self.path
                заголовки = {
                    к: з
                    for к, з in self.headers.items()
                    if к.lower() not in ("host", "accept-encoding", "connection")
                }
                ответ = прокси._клиент.get(прокси.источник + self.path, headers=заголовки)
                тело = ответ.content
                self.send_response(ответ.status_code)
                for к, з in ответ.headers.multi_items():
                    if к.lower() not in (
                        "content-length",
                        "transfer-encoding",
                        "content-encoding",
                        "connection",
                    ):
                        self.send_header(к, з)
                self.send_header("Content-Length", str(len(тело)))
                self.end_headers()
                self.wfile.write(тело)

            def _запрет(self):
                # В этом раунде в базу не пишем: любой не-GET отклоняется, не доходя до 1С.
                прокси.отклонено += 1
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = do_PATCH = do_PUT = do_DELETE = _запрет  # noqa: N815

            def log_message(self, *аргументы):
                return

        self._сервер = http.server.ThreadingHTTPServer(("127.0.0.1", ПОРТ_ПРОКСИ), Обработчик)
        self._поток = threading.Thread(target=self._сервер.serve_forever, daemon=True)
        self._поток.start()

    def условий_в_отборе(self) -> int:
        отбор = urllib.parse.parse_qs(urllib.parse.urlsplit(self.последний).query).get(
            "$filter", [""]
        )[0]
        return len(re.findall(r" eq ", отбор))

    def закрыть(self) -> None:
        self._сервер.shutdown()
        self._клиент.close()


def собрать_дом(рабочий: pathlib.Path, адрес: str) -> pathlib.Path:
    дом = pathlib.Path(tempfile.mkdtemp(prefix="odata1c-b1-"))
    for имя in ("bases", "logs"):
        (дом / имя).mkdir()
    настройки = yaml.safe_load((рабочий / "bases.yaml").read_text(encoding="utf-8"))
    запись = dict(настройки["bases"][БАЗА])
    запись["url"] = адрес
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
    for имя in ("metadata.sqlite", "policy.yaml"):
        shutil.copy2(исходный / имя, каталог / имя)
    return дом


def остановить_демон(дом: pathlib.Path) -> None:
    subprocess.run(  # noqa: S603 — фиксированная команда собственного пакета
        [sys.executable, "-m", "odata1c", "daemon", "stop", "--home", str(дом)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        check=False,
    )
    time.sleep(1.5)


@contextlib.asynccontextmanager
async def сессия(дом: pathlib.Path):
    параметры = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odata1c", "mcp", "--home", str(дом)],
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    async with (
        stdio_client(параметры) as (чтение, запись),
        ClientSession(чтение, запись) as сеанс,
    ):
        await сеанс.initialize()
        yield сеанс


async def тул(сеанс: ClientSession, имя: str, аргументы: dict) -> tuple[dict, str]:
    ответ = await сеанс.call_tool(имя, аргументы, read_timeout_seconds=ТАЙМАУТ)
    текст = "\n".join(ч.text for ч in ответ.content if getattr(ч, "text", None))
    try:
        return json.loads(текст), текст
    except ValueError:
        return {"error": {"code": "not_json"}}, текст


def прочесть(сырой: httpx.Client, адрес: str, params: dict) -> httpx.Response:
    """GET с повтором: база разработки под нагрузкой изредка отвечает 500 на первом обращении
    (первый прогон этой проверки — восемь 500 из одиннадцати, повтор той же выборки — 200)."""
    # Строка запроса собирается вручную с `%20`: httpx кодирует пробел в параметрах плюсом, а 1С
    # плюс за пробел не считает (`ИНН+eq+'…'` — синтаксическая ошибка). Тот же приём, что у
    # клиента шлюза (`client1c._собрать_запрос`).
    запрос = "&".join(
        f"{urllib.parse.quote(к, safe='$')}={urllib.parse.quote(з, safe='')}"
        for к, з in params.items()
    )
    for попытка in range(4):
        ответ = сырой.get(f"{адрес}?{запрос}")
        if ответ.status_code < 500 or попытка == 3:
            if ответ.status_code >= 400:
                # Только форма сообщения 1С: в тексте ошибки бывает эхо запроса.
                print(f"  {ответ.status_code}: {форма(ответ.text[:240])}")
            ответ.raise_for_status()
            return ответ
        time.sleep(2.0 * (попытка + 1))
    raise AssertionError("недостижимо")


def прочитать_поле(сырой: httpx.Client, адрес: str, сущность: str, поле: str) -> list[dict]:
    """Все записи сущности: `Ref_Key` и значение поля — прямым чтением, постранично."""
    записи: list[dict] = []
    пропуск = 0
    while True:
        ответ = прочесть(
            сырой,
            адрес + сущность,
            {
                "$format": "json",
                "$select": f"Ref_Key,{поле}",
                # Без порядка страницы 1С нестабильны: первый прогон этой проверки получил одну
                # запись дважды, а соседнюю — ни разу.
                "$orderby": "Ref_Key",
                "$top": str(СТРАНИЦА_СЫРАЯ),
                "$skip": str(пропуск),
            },
        )
        страница = ответ.json().get("value", [])
        записи += страница
        if len(страница) < СТРАНИЦА_СЫРАЯ:
            return записи
        пропуск += СТРАНИЦА_СЫРАЯ


def найдено_сырым(
    сырой: httpx.Client, адрес: str, сущность: str, поле: str, значение: str
) -> set[str]:
    """`Ref_Key` записей, которые 1С находит `eq` по одному написанию — прямым чтением."""
    ответ = прочесть(
        сырой,
        адрес + сущность,
        {
            "$format": "json",
            "$select": "Ref_Key",
            "$filter": f"{поле} eq {литерал(значение)}",
            "$top": "200",
        },
    )
    return {з["Ref_Key"] for з in ответ.json().get("value", [])}


async def проверить_поле(
    сеанс, сырой, адрес, прокси: Прокси, сущность: str, поле: str, класс: str, групп: int
) -> dict:
    записи = прочитать_поле(сырой, адрес, сущность, поле)
    группы: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for запись in записи:
        значение = запись.get(поле)
        if not isinstance(значение, str) or not значение.strip() or TOKEN_RE.search(значение):
            continue
        группы[нормализовать(класс, значение)][значение].append(запись["Ref_Key"])
    несколько = {н: г for н, г in группы.items() if len(г) > 1}
    итог: dict = {
        "записей": len(записи),
        "значений": len(группы),
        "значений_с_несколькими_написаниями": len(несколько),
        "наибольшее_число_написаний": max((len(г) for г in несколько.values()), default=1),
        "группы": [],
    }
    # Сначала группы поменьше: чтение по `Ref_Key` одним отбором, записей немного.
    выбранные = sorted(несколько.values(), key=lambda г: sum(len(с) for с in г.values()))[:групп]
    for группа in выбранные:
        if sum(len(ссылки) for ссылки in группа.values()) > 12:
            continue
        # Правда о базе — то, что 1С находит по каждому написанию прямым чтением; ожидаемое от
        # шлюза — их объединение (сравнение строк у 1С своё: регистр и хвостовые пробелы она,
        # как видно по счётчикам, не различает, кавычки и ведущий пробел — различает).
        найдено_по_написаниям = {
            написание: найдено_сырым(сырой, адрес, сущность, поле, написание)
            for написание in группа
        }
        ожидаемые = set().union(*найдено_по_написаниям.values())
        ключи = sorted(ожидаемые | {к for ссылки in группа.values() for к in ссылки})
        # Список, а не словарь: у двух написаний форма бывает одна («ООО Ромашка» и «ооо ромашка»).
        по_одному = sorted(
            (форма(написание), len(найдено)) for написание, найдено in найдено_по_написаниям.items()
        )
        отбор_ключей = " or ".join(f"Ref_Key eq guid'{к}'" for к in ключи)
        прочитано, текст_чтения = await тул(
            сеанс,
            "odata1c_query",
            {"entity": сущность, "select": ["Ref_Key", поле], "filter": отбор_ключей, "top": 50},
        )
        if "error" in прочитано:
            итог["группы"].append({"ошибка_чтения": прочитано["error"].get("code")})
            continue
        токены = {з.get(поле) for з in прочитано.get("items", [])}
        токен = next(iter(токены)) if len(токены) == 1 else None
        запись_группы: dict = {
            "формы_написаний": sorted(форма(н) for н in группа),
            "записей_в_группе": len(ожидаемые),
            "по_одному_написанию_находит": по_одному,
            "один_токен_на_группу": len(токены) == 1 and bool(токен and TOKEN_RE.fullmatch(токен)),
        }
        if токен and TOKEN_RE.fullmatch(токен):
            ответ, текст = await тул(
                сеанс,
                "odata1c_query",
                {
                    "entity": сущность,
                    "select": ["Ref_Key"],
                    "filter": f"{поле} eq '{токен}'",
                    "top": 50,
                },
            )
            if "error" in ответ:
                запись_группы["отбор_через_шлюз"] = {"ошибка": ответ["error"].get("code")}
            else:
                найдены = {з["Ref_Key"] for з in ответ.get("items", [])}
                запись_группы["отбор_через_шлюз"] = {
                    "найдено_записей_группы": len(найдены & ожидаемые),
                    "найдено_лишних": len(найдены - ожидаемые),
                    "условий_ушло_в_1С": прокси.условий_в_отборе(),
                    "написание_в_ответе": any(н in текст for н in группа),
                    "guard_replaced": "guard_replaced" in текст,
                }
        запись_группы["написание_в_ответе_чтения"] = any(н in текст_чтения for н in группа)
        итог["группы"].append(запись_группы)
    return итог


async def main() -> int:
    разбор = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    разбор.add_argument(
        "--home",
        default=str(pathlib.Path.home() / ".claude" / "odata1c"),
        help="рабочий домашний каталог (только чтение)",
    )
    разбор.add_argument("--groups", type=int, default=3, help="групп на поле")
    разбор.add_argument("--out", help="куда сохранить итог JSON (счётчики и формы, без значений)")
    аргументы = разбор.parse_args()

    рабочий = pathlib.Path(аргументы.home)
    настройки = yaml.safe_load((рабочий / "bases.yaml").read_text(encoding="utf-8"))["bases"][БАЗА]
    адрес = адрес_odata(настройки["url"])
    сырой = httpx.Client(auth=(настройки["user"], настройки["password"]), timeout=120)
    прокси = Прокси(адрес)
    дом = собрать_дом(рабочий, прокси.адрес)
    итог: dict = {}
    print(f"временный дом создан, порт демона {ПОРТ}")
    try:
        async with сессия(дом) as сеанс:
            for сущность, поле, класс in ПОЛЯ:
                try:
                    итог[f"{сущность}.{поле}"] = await проверить_поле(
                        сеанс, сырой, адрес, прокси, сущность, поле, класс, аргументы.groups
                    )
                except httpx.HTTPStatusError as ошибка:
                    итог[f"{сущность}.{поле}"] = {"ошибка_чтения": ошибка.response.status_code}
                print(
                    f"{сущность}.{поле}: "
                    + json.dumps(итог[f"{сущность}.{поле}"], ensure_ascii=False)
                )
    finally:
        остановить_демон(дом)
        прокси.закрыть()
        сырой.close()
        shutil.rmtree(дом, ignore_errors=True)
        print(f"\nвременный дом удалён: {not дом.exists()}")
        print(f"запросов к 1С через шлюз: {прокси.счёт}, отклонено не-GET: {прокси.отклонено}")
    группы = [г for поле in итог.values() for г in поле.get("группы", [])]
    через_шлюз = [
        г["отбор_через_шлюз"]
        for г in группы
        if "найдено_записей_группы" in г.get("отбор_через_шлюз", {})
    ]
    свод = {
        "групп_проверено": len(группы),
        "один_токен_на_группу": sum(г.get("один_токен_на_группу", False) for г in группы),
        "одно_написание_находит_не_все": sum(
            any(найдено < г["записей_в_группе"] for _, найдено in г["по_одному_написанию_находит"])
            for г in группы
        ),
        "шлюз_нашёл_всю_группу": sum(
            г["отбор_через_шлюз"].get("найдено_записей_группы") == г["записей_в_группе"]
            for г in группы
            if "отбор_через_шлюз" in г
        ),
        "написание_в_ответе": sum(о["написание_в_ответе"] for о in через_шлюз)
        + sum(г.get("написание_в_ответе_чтения", False) for г in группы),
        "guard_replaced": sum(о["guard_replaced"] for о in через_шлюз),
    }
    итог["свод"] = свод
    print("свод: " + json.dumps(свод, ensure_ascii=False))
    if аргументы.out:
        pathlib.Path(аргументы.out).write_text(
            json.dumps(итог, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
