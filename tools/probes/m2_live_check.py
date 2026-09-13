"""Приёмка M2 (запись) на живой базе 1С `trade_dev` — задача 10 плана M2.

Скрипт пишет в 1С так, как это делал бы клиент MCP: поднимает настоящий лаунчер `odata1c mcp`
отдельным процессом (stdio MCP), тот — свой демон из рабочей копии. Демон и всё его состояние живут
во ВРЕМЕННОМ домашнем каталоге: рабочий дом владельца (его `bases.yaml`, словарь `gate.sqlite`,
`launcher.key`, журнал, демон на 7171) не трогается. Во временный дом копируются только запись базы
из `bases.yaml`, индекс метаданных и политика; словарь, секрет гейта и ключ лаунчера — свои.

Правила живой базы (владелец разрешил запись в `trade_dev`, базу разработки):

* меняются только объекты, созданные этим прогоном: у каждого маркер `odata1c-приёмка M2 <время>` в
  `Description` (у документа — в `Комментарий`). Существующие объекты только читаются — токен для
  сценария «изменить по токену» берётся чтением контрагента и пишется в объект приёмки;
* между демоном приёмки и 1С стоит посредник на 127.0.0.1: он пропускает запросы как есть, считает
  их по методам и **не пускает** POST/PATCH ни к чему, кроме создания в двух наборах приёмки и
  записи объектов, уже созданных прогоном (Ref_Key из ответа `commit`). DELETE и PUT не пропускает
  вовсе (инвариант 3). Нарушение — отказ посредника и провал приёмки;
* напрямую к 1С (`httpx` мимо шлюза) — только GET для сверки и уборка, если шлюз не справился;
* уборка — в `finally`, даже при падении: проведённые — `Unpost`, всем — `DeletionMark=true`,
  сначала через шлюз, иначе напрямую; поиск по маркеру прогона подбирает объекты, которые 1С
  приняла, а прогон записать не успел. Журнал созданного — `%TEMP%/odata1c-m2-created.json`.

Что печатается. Реальные значения — никогда: только счётчики, коды, имена полей, формы (цифра → 9,
буква → a) и GUID объектов приёмки (инвариант 6). Учётные данные в вывод не попадают; во временный
`bases.yaml` пишутся и удаляются вместе с каталогом.

Разделы (`--only` через запятую; разделы справочника сами добавляют «создание»): `тулы`,
`создание`, `токен`, `пометка`, `устаревание`, `запреты`, `документ`, `ошибка_1с`, `клиент`,
`подпись`, `журнал`. «От класса данных» (сценарий 7) и уборка выполняются всегда. `--only уборка` —
только поиск по общему маркеру `odata1c-приёмка M2` и уборка найденного.

Запуск из корня репозитория:

    PYTHONUTF8=1 uv run python tools/probes/m2_live_check.py --out <файл.json>
    PYTHONUTF8=1 uv run python tools/probes/m2_live_check.py --only документ
    PYTHONUTF8=1 uv run python tools/probes/m2_live_check.py --only уборка
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
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from collections import Counter
from typing import Any

import httpx
import httpx2
import mcp.types as types
import yaml
from mcp import StdioServerParameters, stdio_client
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from odata1c.client1c.client import _собрать_запрос, _экранировать_путь
from odata1c.client1c.errors import map_error
from odata1c.config.writer import read_launcher_key
from odata1c.daemon import (
    CLIENT_ELICITATION_HEADER,
    CLIENT_NAME_HEADER,
    CLIENT_SIG_HEADER,
    CLIENT_VERSION_HEADER,
    SESSION_ID_HEADER,
    client_signature,
    daemon_url,
)
from odata1c.gate.dictionary import name_variants_of
from odata1c.write.journal import Journal

БАЗА = "trade_dev"
БАЗА_ЧТЕНИЕ = "trade_dev_ro"  # копия с write: false — сценарий base_read_only
БАЗА_ЗАПРЕТ = "trade_dev_deny"  # копия с deny_fields — сценарий field_write_denied
ПОРТ_ДЕМОНА = 7195
ПОРТ_1С = 7196
ТАЙМАУТ = 240.0

СПРАВОЧНИК = "Catalog_Номенклатура"
ДОКУМЕНТ = "Document_ПересчетТоваров"
КОНТРАГЕНТЫ = "Catalog_Контрагенты"
КИ_КОНТРАГЕНТОВ = "Catalog_Контрагенты_КонтактнаяИнформация"
# Поле класса `org` у справочника приёмки (P8, политика `auto` базы) — сценарии 1 и 2.
ПОЛЕ_ORG = "см_НаименованиеКонтрагента"
# Строка без класса гейта — для записей, где важен сам факт записи (устаревание, подпись, клиент).
ПОЛЕ_ТЕКСТ = "НаименованиеПолное"
# Поле, запрещённое к записи в копии `trade_dev_deny`.
ПОЛЕ_ЗАПРЕТ = "Описание"

МАРКЕР_ОБЩИЙ = "odata1c-приёмка M2"
ЖУРНАЛ_СОЗДАННОГО = pathlib.Path(tempfile.gettempdir()) / "odata1c-m2-created.json"

ТОКЕН = re.compile(r"\[\[[a-z][a-z0-9_:]*:[0-9A-Z]{1,16}\]\]")
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ДА = types.ElicitResult(action="accept", content={"confirm": "yes"})

ТУЛЫ_ЗАПИСИ = {
    "odata1c_create",
    "odata1c_update",
    "odata1c_mark_for_deletion",
    "odata1c_action",
    "odata1c_commit",
    "odata1c_undo",
    "odata1c_journal",
}
ПОДГОТОВКА = {"odata1c_create", "odata1c_update", "odata1c_mark_for_deletion", "odata1c_action"}

РАЗДЕЛЫ_СПРАВОЧНИКА = {
    "создание",
    "токен",
    "пометка",
    "устаревание",
    "запреты",
    "ошибка_1с",
    "клиент",
    "подпись",
}
РАЗДЕЛЫ = [
    "тулы",
    "создание",
    "токен",
    "пометка",
    "устаревание",
    "запреты",
    "документ",
    "ошибка_1с",
    "клиент",
    "подпись",
    "журнал",
]


def форма(текст: str) -> str:
    return re.sub(r"[^\W\d_]", "a", re.sub(r"\d", "9", текст))


def заголовок(текст: str) -> None:
    print(f"\n=== {текст} ===", flush=True)


def код(ответ: dict) -> str | None:
    ошибка = ответ.get("error") if isinstance(ответ, dict) else None
    return ошибка.get("code") if isinstance(ошибка, dict) else None


# --- итог прогона ------------------------------------------------------------------------------


class Итог:
    """Проверки, замеры и созданные объекты — без значений."""

    def __init__(self) -> None:
        self.проверки: list[dict] = []
        self.замеры: list[dict] = []
        self.созданные: list[dict] = []
        self.сведения: dict[str, Any] = {}
        self.раздел = "подготовка"

    def проверить(self, что: str, ок: bool, подробно: str = "") -> bool:
        self.проверки.append(
            {"раздел": self.раздел, "что": что, "ок": bool(ок), "подробно": подробно}
        )
        хвост = f" — {подробно}" if подробно else ""
        print(f"  {'ОК ' if ок else 'НЕТ'} {что}{хвост}", flush=True)
        return bool(ок)

    def заметка(self, текст: str) -> None:
        print(f"  · {текст}", flush=True)


class Сбор:
    """Всё, что увидела бы модель (ответы тулов), и тексты вопросов elicitation (их видит
    пользователь) — для сценария 7 «от класса данных». В вывод не попадает."""

    def __init__(self) -> None:
        self.ответы: list[tuple[str, str, str]] = []  # (сессия, тул, текст)
        self.вопросы: list[str] = []


class Иглы:
    """Реальные значения защищаемых классов, известные приёмке из прямых GET. В вывод — только
    классы и счётчики."""

    def __init__(self) -> None:
        self._значения: dict[str, set[str]] = {}

    def добавить(self, класс: str, значение) -> None:
        if not isinstance(значение, str):
            return
        значение = значение.strip()
        if len(значение) < 4:
            return
        self._значения.setdefault(класс, set()).add(значение)
        if класс == "org":
            for вариант in name_variants_of(значение):
                вариант = вариант.strip().strip('"')
                if len(вариант) >= 4 and вариант != значение:
                    self._значения.setdefault("org: вариант без формы", set()).add(вариант)

    def сводка(self) -> dict[str, int]:
        return {класс: len(набор) for класс, набор in sorted(self._значения.items())}

    def найти(self, текст: str) -> Counter:
        найдено: Counter = Counter()
        for класс, набор in self._значения.items():
            for значение in набор:
                if значение in текст:
                    найдено[класс] += 1
        return найдено


def строки_ответа(текст: str) -> str:
    """Текст ответа и его строки после разбора JSON: в сыром JSON кавычка названия идёт как `\\"`,
    и вхождение названия с кавычками ищется по разобранным строкам."""
    части = [текст]
    try:
        данные = json.loads(текст)
    except ValueError:
        return текст

    def обойти(узел) -> None:
        if isinstance(узел, str):
            части.append(узел)
        elif isinstance(узел, dict):
            for к, з in узел.items():
                части.append(str(к))
                обойти(з)
        elif isinstance(узел, list):
            for з in узел:
                обойти(з)

    обойти(данные)
    return "\n".join(части)


# --- посредник между демоном приёмки и 1С -----------------------------------------------------


ЦЕЛЬ_ЗАПИСИ = re.compile(rf"^(?:{СПРАВОЧНИК}|{ДОКУМЕНТ})\(guid'([0-9a-fA-F-]{{36}})'\)$")
ЦЕЛЬ_СОЗДАНИЯ = re.compile(rf"^(?:{СПРАВОЧНИК}|{ДОКУМЕНТ})$")
ЦЕЛЬ_ДЕЙСТВИЯ = re.compile(rf"^{ДОКУМЕНТ}\(guid'([0-9a-fA-F-]{{36}})'\)/(?:Post|Unpost)$")
НЕ_ПЕРЕСЫЛАТЬ = {
    "host",
    "accept-encoding",
    "connection",
    "keep-alive",
    "content-length",
    "transfer-encoding",
    "proxy-connection",
}
НЕ_ВОЗВРАЩАТЬ = {"content-length", "transfer-encoding", "content-encoding", "connection"}


class Посредник1С:
    """Прокси между демоном приёмки и публикацией 1С.

    Считает запросы по методам — так «в 1С ничего не ушло» проверяется счётом, а не журналом
    (журнал демона адресов не пишет, Ruling 31). И держит правило живой базы механически: POST —
    только создание в наборах приёмки или действие над своим документом, PATCH — только свой
    объект; DELETE и PUT — никогда. Отказ посредника — HTTP 403, запрос в 1С не уходит.
    Транспорт без cookie-хранилища: сеансы 1С (`IBSession`) разных баз не смешиваются."""

    def __init__(self, настоящий_адрес: str, *, verify) -> None:
        разбор = urllib.parse.urlsplit(настоящий_адрес)
        self.источник = f"{разбор.scheme}://{разбор.netloc}"
        self.адрес = f"http://127.0.0.1:{ПОРТ_1С}{разбор.path}"
        self.счёт: Counter = Counter()
        self.нарушения: list[tuple[str, str]] = []
        self.свои: set[str] = set()
        self._замок = threading.Lock()
        self._транспорт = httpx.HTTPTransport(verify=verify, retries=0)
        посредник = self

        class Обработчик(http.server.BaseHTTPRequestHandler):
            def _передать(self, метод: str) -> None:
                длина = int(self.headers.get("Content-Length") or 0)
                тело = self.rfile.read(длина) if длина else b""
                путь = urllib.parse.unquote(self.path.split("?", 1)[0])
                хвост = путь.split("standard.odata/", 1)[-1]
                with посредник._замок:
                    посредник.счёт[метод] += 1
                    разрешено = посредник._разрешено(метод, хвост)
                    if not разрешено:
                        посредник.нарушения.append((метод, хвост.split("(", 1)[0]))
                if not разрешено:
                    отказ = json.dumps(
                        {
                            "odata.error": {
                                "code": "-1",
                                "message": {"lang": "ru", "value": "посредник приёмки: отказ"},
                            }
                        }
                    ).encode()
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(отказ)))
                    self.end_headers()
                    self.wfile.write(отказ)
                    return
                заголовки = [
                    (к, з) for к, з in self.headers.items() if к.lower() not in НЕ_ПЕРЕСЫЛАТЬ
                ]
                запрос = httpx.Request(
                    метод,
                    посредник.источник + self.path,
                    headers=заголовки,
                    content=тело if (тело or метод != "GET") else None,
                    extensions={"timeout": httpx.Timeout(300.0).as_dict()},
                )
                ответ = посредник._транспорт.handle_request(запрос)
                try:
                    данные = ответ.read()
                finally:
                    ответ.close()
                self.send_response(ответ.status_code)
                # Заголовки — сырыми байтами: `Location` ответа POST 1С несёт кириллицу пути в
                # UTF-8, а `send_header` кодирует строку в latin-1 и падает (так потерялся ответ
                # на первый create первого поэтапного прогона). Раскодировать latin-1 и отдать
                # `send_header` — те же байты обратно.
                for к, з in ответ.headers.raw:
                    if к.decode("latin-1").lower() not in НЕ_ВОЗВРАЩАТЬ:
                        self.send_header(к.decode("latin-1"), з.decode("latin-1"))
                self.send_header("Content-Length", str(len(данные)))
                self.end_headers()
                self.wfile.write(данные)

            def do_GET(self):  # noqa: N802 — имена методов задаёт http.server
                self._передать("GET")

            def do_POST(self):  # noqa: N802
                self._передать("POST")

            def do_PATCH(self):  # noqa: N802
                self._передать("PATCH")

            def do_DELETE(self):  # noqa: N802
                self._передать("DELETE")

            def do_PUT(self):  # noqa: N802
                self._передать("PUT")

            def do_MERGE(self):  # noqa: N802
                self._передать("MERGE")

            def log_message(self, *аргументы):
                return

        self._сервер = http.server.ThreadingHTTPServer(("127.0.0.1", ПОРТ_1С), Обработчик)
        self._сервер.daemon_threads = True
        self._поток = threading.Thread(target=self._сервер.serve_forever, daemon=True)
        self._поток.start()

    def _разрешено(self, метод: str, хвост: str) -> bool:
        if метод == "GET":
            return True
        if метод == "PATCH":
            найдено = ЦЕЛЬ_ЗАПИСИ.match(хвост)
            return bool(найдено) and найдено.group(1).lower() in self.свои
        if метод == "POST":
            if ЦЕЛЬ_СОЗДАНИЯ.match(хвост):
                return True
            найдено = ЦЕЛЬ_ДЕЙСТВИЯ.match(хвост)
            return bool(найдено) and найдено.group(1).lower() in self.свои
        return False

    def записи(self) -> int:
        with self._замок:
            return self.счёт["POST"] + self.счёт["PATCH"] + self.счёт["DELETE"] + self.счёт["PUT"]

    def всего(self) -> int:
        with self._замок:
            return sum(self.счёт.values())

    def закрыть(self) -> None:
        self._сервер.shutdown()
        self._транспорт.close()


# --- прямые запросы к 1С (сверка и запасная уборка) -------------------------------------------


class Прямой1С:
    """GET к публикации тем же кодированием, что у `Client1C` (пробел — `%20`). POST/PATCH — только
    из запасной уборки и только по объектам приёмки."""

    def __init__(self, запись: dict) -> None:
        self._http = httpx.Client(
            base_url=запись["url"],
            auth=(запись["user"], запись["password"]),
            timeout=180.0,
            headers={"Accept": "application/json"},
            verify=запись.get("verify_tls", True),
        )
        self._сеанс = False

    def запрос(self, метод: str, путь: str, params: dict | None = None, тело=None):
        params = dict(params or {})
        params.setdefault("$format", "json")
        цель = _экранировать_путь(путь) + "?" + _собрать_запрос(params)
        заголовки = {}
        if not self._сеанс:
            заголовки["IBSession"] = "start"
            self._сеанс = True
        аргументы = {"json": тело} if тело is not None else {}
        ответ = self._http.request(метод, цель, headers=заголовки, **аргументы)
        текст = ответ.content.decode("utf-8", errors="replace")
        try:
            данные = json.loads(текст) if текст else None
        except ValueError:
            данные = None
        return ответ.status_code, данные

    def объект(self, набор: str, ref: str) -> dict | None:
        статус, данные = self.запрос("GET", f"{набор}(guid'{ref}')")
        return данные if статус == 200 and isinstance(данные, dict) else None

    def выборка(self, набор: str, params: dict) -> list[dict] | None:
        статус, данные = self.запрос("GET", набор, params)
        if статус != 200 or not isinstance(данные, dict):
            return None
        return данные.get("value") or []

    def по_маркеру(self, набор: str, поле: str, маркер: str) -> list[dict]:
        отбор = "substringof('" + маркер.replace("'", "''") + f"', {поле})"
        строки = self.выборка(
            набор,
            {
                "$filter": отбор,
                "$select": f"Ref_Key,{поле},DeletionMark"
                + (",Posted" if набор == ДОКУМЕНТ else ""),
            },
        )
        return строки or []

    def close(self) -> None:
        if self._сеанс:
            with contextlib.suppress(httpx.HTTPError):
                self._http.get("", headers={"IBSession": "finish"})
        self._http.close()


# --- временный домашний каталог -----------------------------------------------------------------


def скопировать_индекс(откуда: pathlib.Path, куда: pathlib.Path) -> None:
    """Копия индекса через резервное копирование SQLite: рабочий индекс открыт демоном владельца в
    WAL, и копия одного файла `metadata.sqlite` могла бы не застать страницы из журнала WAL."""
    источник = sqlite3.connect(f"file:{откуда.as_posix()}?mode=ro", uri=True)
    try:
        приёмник = sqlite3.connect(куда)
        try:
            источник.backup(приёмник)
        finally:
            приёмник.close()
    finally:
        источник.close()


def собрать_дом(рабочий: pathlib.Path, адрес_посредника: str) -> pathlib.Path:
    """Временный дом: три записи базы — копии `trade_dev` с адресом посредника (сама, `write:
    false`, `deny_fields`), у каждой копия индекса и политики; словарь пуст, секрет гейта свой,
    `write_confirm_fallback: deny`. Ключ лаунчера создаст сам лаунчер до подъёма демона (Ruling
    59) — демон его только читает."""
    дом = pathlib.Path(tempfile.mkdtemp(prefix="odata1c-m2-"))
    for имя in ("bases", "logs"):
        (дом / имя).mkdir()
    настройки = yaml.safe_load((рабочий / "bases.yaml").read_text(encoding="utf-8"))
    запись = dict(настройки["bases"][БАЗА])
    запись["url"] = адрес_посредника
    только_чтение = {**запись, "label": "копия trade_dev только для чтения", "write": False}
    с_запретом = {
        **запись,
        "label": "копия trade_dev с запретом поля",
        "permissions": {"deny_fields": [f"{СПРАВОЧНИК}.{ПОЛЕ_ЗАПРЕТ}"]},
    }
    (дом / "bases.yaml").write_text(
        yaml.safe_dump(
            {
                "default": БАЗА,
                "bases": {БАЗА: запись, БАЗА_ЧТЕНИЕ: только_чтение, БАЗА_ЗАПРЕТ: с_запретом},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (дом / "daemon.yaml").write_text(
        f"port: {ПОРТ_ДЕМОНА}\nreindex_check_hours: 0\nwrite_confirm_fallback: deny\n"
        f'gate_secret: "{secrets.token_hex(32)}"\n',
        encoding="utf-8",
    )
    исходный = рабочий / "bases" / БАЗА
    for база in (БАЗА, БАЗА_ЧТЕНИЕ, БАЗА_ЗАПРЕТ):
        каталог = дом / "bases" / база
        каталог.mkdir()
        скопировать_индекс(исходный / "metadata.sqlite", каталог / "metadata.sqlite")
        shutil.copy2(исходный / "policy.yaml", каталог / "policy.yaml")
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


# --- клиенты MCP -------------------------------------------------------------------------------


class Сеанс:
    """Клиент MCP приёмки: вызов тула, сбор ответа для сценария 7 и замер времени."""

    def __init__(self, имя: str, сессия: ClientSession, к: Контекст, вопросы: list[str]) -> None:
        self.имя = имя
        self.сессия = сессия
        self.к = к
        self.вопросы = вопросы
        self.идентификаторы: list[str] = []

    async def вызвать(self, тул: str, аргументы: dict, *, категория: str | None = None) -> dict:
        начало = time.monotonic()
        ответ = await self.сессия.call_tool(тул, аргументы, read_timeout_seconds=ТАЙМАУТ)
        секунды = time.monotonic() - начало
        текст = "\n".join(ч.text for ч in ответ.content if getattr(ч, "text", None))
        self.к.сбор.ответы.append((self.имя, тул, текст))
        try:
            данные = json.loads(текст)
        except ValueError:
            данные = {"error": {"code": "not_json", "message": форма(текст[:80])}}
        if категория:
            self.к.итог.замеры.append(
                {"категория": категория, "тул": тул, "с": секунды, "ок": код(данные) is None}
            )
        return данные


@contextlib.asynccontextmanager
async def через_лаунчер(к: Контекст, имя: str, *, версия: str = "1.0.0", elicitation: bool = True):
    """Настоящий лаунчер отдельным процессом; клиент называет себя `имя`/`версия`. С elicitation —
    отвечает «yes» на каждый вопрос демона и запоминает текст вопроса."""
    параметры = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odata1c", "mcp", "--home", str(к.дом)],
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    вопросы: list[str] = []
    аргументы: dict = {"client_info": types.Implementation(name=имя, version=версия)}
    if elicitation:

        async def ответить(context, params):
            вопросы.append(params.message)
            к.сбор.вопросы.append(params.message)
            return ДА

        аргументы["elicitation_callback"] = ответить
    async with (
        stdio_client(параметры) as (чтение, запись),
        ClientSession(чтение, запись, **аргументы) as сессия,
    ):
        await сессия.initialize()
        yield Сеанс(имя, сессия, к, вопросы)


ЗАГОЛОВКИ_CLAUDE_CODE = {
    CLIENT_NAME_HEADER: "claude-code",
    CLIENT_VERSION_HEADER: "2.1.267",
    CLIENT_ELICITATION_HEADER: "0",
}


@contextlib.asynccontextmanager
async def напрямую(
    к: Контекст, имя: str, *, заголовки: dict | None = None, ключ: bytes | None = None
):
    """Прямой HTTP-клиент демона приёмки, без лаунчера — как `curl` или скрипт. `заголовки` —
    заголовки клиента, как у лаунчера; `ключ` — подписать их ключом лаунчера для своей сессии
    (так делает лаунчер). Идентификатор сессии запоминается в `Сеанс.идентификаторы`."""
    идентификаторы: list[str] = []

    async def хук(request: httpx2.Request) -> None:
        if заголовки:
            request.headers.update(заголовки)
        sid = request.headers.get(SESSION_ID_HEADER)
        if sid:
            if sid not in идентификаторы:
                идентификаторы.append(sid)
            if ключ is not None and заголовки:
                request.headers[CLIENT_SIG_HEADER] = client_signature(
                    ключ,
                    sid,
                    заголовки[CLIENT_NAME_HEADER],
                    заголовки[CLIENT_VERSION_HEADER],
                    заголовки[CLIENT_ELICITATION_HEADER],
                )

    async with (
        httpx2.AsyncClient(
            event_hooks={"request": [хук]}, timeout=httpx2.Timeout(30, read=None)
        ) as http,
        streamable_http_client(daemon_url(ПОРТ_ДЕМОНА), http_client=http) as (чтение, запись),
        ClientSession(
            чтение, запись, client_info=types.Implementation(name=имя, version="2.1.267")
        ) as сессия,
    ):
        await сессия.initialize()
        сеанс = Сеанс(имя, сессия, к, [])
        сеанс.идентификаторы = идентификаторы
        yield сеанс


async def сырой_вызов(к: Контекст, sid: str, номер: int, тул: str, аргументы: dict) -> dict:
    """`tools/call` одним POST в чужую живую сессию — без SDK-клиента и без подписи, как `curl`."""
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(120)) as http:
        ответ = await http.post(
            daemon_url(ПОРТ_ДЕМОНА),
            headers={
                SESSION_ID_HEADER: sid,
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "mcp-protocol-version": "2025-11-25",
            },
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": номер,
                    "method": "tools/call",
                    "params": {"name": тул, "arguments": аргументы},
                }
            ),
        )
    текст = ""
    for строка in ответ.text.splitlines():
        if строка.startswith("data:"):
            сообщение = json.loads(строка[len("data:") :])
            if сообщение.get("id") == номер and "result" in сообщение:
                текст = сообщение["result"]["content"][0]["text"]
    if not текст and ответ.headers.get("content-type", "").startswith("application/json"):
        сообщение = ответ.json()
        if "result" in сообщение:
            текст = сообщение["result"]["content"][0]["text"]
    к.сбор.ответы.append(("сырой", тул, текст or ответ.text))
    try:
        return json.loads(текст)
    except ValueError:
        return {"error": {"code": f"http_{ответ.status_code}", "message": ""}}


# --- контекст прогона --------------------------------------------------------------------------


class Контекст:
    def __init__(self, дом, прямой, посредник, метка) -> None:
        self.дом: pathlib.Path = дом
        self.прямой: Прямой1С = прямой
        self.посредник: Посредник1С = посредник
        self.метка: str = метка
        self.метка_элемента = f"{метка} элемент"
        self.итог = Итог()
        self.сбор = Сбор()
        self.иглы = Иглы()
        self.A: Сеанс | None = None
        self.кандидаты: list[dict] = []
        self.C: str | None = None
        self.D: str | None = None
        self.коммиты: dict[str, str] = {}
        self.токены: dict[str, str] = {}  # Ref_Key контрагента → токен названия
        self.отказы_токена: Counter = Counter()
        self.кандидат1: dict = {}
        self.кандидат2: dict = {}
        self.токен1: str | None = None
        self.токен2: str | None = None
        self.commit_ошибки: str | None = None
        self.текст_ошибки_1с: str | None = None
        self.контроль_игл: str | None = None

    # Объект, созданный прогоном: в список уборки, журнал созданного и разрешения посредника —
    # сразу, до следующего запроса.
    def создан(self, набор: str, ref: str, что: str) -> None:
        ref = ref.lower()
        self.посредник.свои.add(ref)
        if any(з["ref"] == ref for з in self.итог.созданные):
            return
        self.итог.созданные.append({"набор": набор, "ref": ref, "что": что})
        прежние = []
        if ЖУРНАЛ_СОЗДАННОГО.exists():
            with contextlib.suppress(ValueError):
                прежние = json.loads(ЖУРНАЛ_СОЗДАННОГО.read_text(encoding="utf-8"))
        прежние.append({"набор": набор, "ref": ref, "что": что, "метка": self.метка})
        ЖУРНАЛ_СОЗДАННОГО.write_text(
            json.dumps(прежние, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def журнал(self, commit_id: str):
        журнал = Journal(self.дом / "journal.sqlite")
        try:
            return журнал.get(commit_id)
        finally:
            журнал.close()

    def сверить_с_до(self, набор: str, ref: str, commit_id: str) -> tuple[int, int]:
        """Сверка объекта в 1С (прямой GET) с `before_json` журнала: совпавших полей из всех, кроме
        `DataVersion` (счётчик растёт на любой записи, P8)."""
        строка = self.журнал(commit_id)
        объект = self.прямой.объект(набор, ref) or {}
        до = (строка.before or {}) if строка else {}
        поля = [п for п in до if п not in ("DataVersion",)]
        совпало = sum(1 for п in поля if объект.get(п) == до[п])
        return совпало, len(поля)

    def механизм(self, commit_id: str) -> str | None:
        строка = self.журнал(commit_id)
        return строка.client if строка else None


async def выполнить(к: Контекст, сеанс: Сеанс, подготовка: dict, что: str) -> dict:
    """`commit` подготовленной операции: ответ с `commit_id`, ровно один вопрос (у сессии с
    elicitation), одна запись в 1С."""
    итог = к.итог
    if not итог.проверить(
        f"{что}: подготовка → pending_id", "pending_id" in подготовка, код(подготовка) or ""
    ):
        return {}
    вопросов = len(сеанс.вопросы)
    записей = к.посредник.записи()
    ответ = await сеанс.вызвать(
        "odata1c_commit", {"pending_id": подготовка["pending_id"]}, категория="commit"
    )
    итог.проверить(f"{что}: commit выполнен", "commit_id" in ответ, код(ответ) or "")
    итог.проверить(
        f"{что}: в 1С ушёл ровно один запрос записи",
        к.посредник.записи() - записей == 1,
        f"{к.посредник.записи() - записей}",
    )
    if сеанс is к.A:
        итог.проверить(
            f"{что}: elicitation спросила ровно один раз", len(сеанс.вопросы) - вопросов == 1
        )
    return ответ


# --- разделы ----------------------------------------------------------------------------------


async def раздел_тулы(к: Контекст) -> None:
    заголовок("0. Тулы записи сквозь лаунчер")
    к.итог.раздел = "тулы"
    тулы = {т.name: т for т in (await к.A.сессия.list_tools()).tools}
    к.итог.проверить(
        "семь тулов записи объявлены",
        set(тулы) >= ТУЛЫ_ЗАПИСИ,
        f"{len(ТУЛЫ_ЗАПИСИ & set(тулы))} из 7",
    )
    commit = тулы.get("odata1c_commit")
    мета = (commit.meta or {}) if commit else {}
    к.итог.проверить(
        "odata1c_commit: _meta requiresUserInteraction виден за лаунчером",
        мета.get("anthropic/requiresUserInteraction") is True,
    )
    к.итог.проверить(
        "odata1c_commit: destructive_hint",
        bool(commit and commit.annotations and commit.annotations.destructive_hint),
    )
    журнал = тулы.get("odata1c_journal")
    к.итог.проверить(
        "odata1c_journal: read_only_hint",
        bool(журнал and журнал.annotations and журнал.annotations.read_only_hint),
    )


def выбрать_контрагентов(к: Контекст) -> None:
    """Контрагенты — источник токена названия (только чтение, напрямую). Короткое название без
    краевых пробелов, единственное в выборке: длина поля справочника приёмки неизвестна (1С молча
    обрезает лишнее, P8), и одно написание — меньше повода для `token_ambiguous`. Реальные
    значения защищаемых полей контрагента и его контактной информации — иглы сценария 7."""
    строки = к.прямой.выборка(
        КОНТРАГЕНТЫ,
        {
            "$select": "Ref_Key,Description,НаименованиеПолное,ИНН,КПП,IsFolder",
            "$filter": "DeletionMark eq false",
            "$top": "300",
        },
    )
    if строки is None:
        строки = (
            к.прямой.выборка(
                КОНТРАГЕНТЫ,
                {
                    "$select": "Ref_Key,Description,НаименованиеПолное,ИНН,КПП",
                    "$filter": "DeletionMark eq false",
                    "$top": "300",
                },
            )
            or []
        )
    счёт = Counter((с.get("Description") or "").strip().lower() for с in строки)
    for с in строки:
        название = с.get("Description")
        if (
            not с.get("IsFolder")
            and isinstance(название, str)
            and 4 <= len(название) <= 30
            and название == название.strip()
            and счёт[название.lower()] == 1
        ):
            к.кандидаты.append(с)
        if len(к.кандидаты) >= 8:
            break
    for с in к.кандидаты:
        к.иглы.добавить("org", с.get("Description"))
        к.иглы.добавить("org", с.get("НаименованиеПолное"))
        к.иглы.добавить("inn", с.get("ИНН"))
        к.иглы.добавить("kpp", с.get("КПП"))
        ки = к.прямой.выборка(
            КИ_КОНТРАГЕНТОВ,
            {"$filter": f"Ref_Key eq guid'{с['Ref_Key']}'", "$select": "Тип,Представление"},
        )
        for строка in ки or []:
            к.иглы.добавить(f"КИ: {строка.get('Тип') or 'без типа'}", строка.get("Представление"))
    к.итог.заметка(
        f"контрагентов в выборке {len(строки)}, кандидатов — источников токена {len(к.кандидаты)}"
    )


async def токен_контрагента(к: Контекст, кандидат: dict) -> str | None:
    """Токен названия контрагента — чтением через шлюз приёмки (токены — свои у каждого
    домашнего каталога: секрет гейта свой). Чтение и кладёт значение во временный словарь."""
    ref = кандидат["Ref_Key"]
    if ref in к.токены:
        return к.токены[ref]
    ответ = await к.A.вызвать(
        "odata1c_get", {"entity": КОНТРАГЕНТЫ, "key": ref, "select": ["Ref_Key", "Description"]}
    )
    значение = (ответ.get("item") or {}).get("Description")
    if isinstance(значение, str) and ТОКЕН.fullmatch(значение):
        к.токены[ref] = значение
        return значение
    к.отказы_токена["чтение не дало токена"] += 1
    return None


async def раздел_создание(к: Контекст) -> None:
    заголовок("1. Создать элемент справочника: превью в токенах → commit → объект в 1С")
    к.итог.раздел = "создание"
    итог = к.итог
    подготовка: dict = {}
    for кандидат in к.кандидаты:
        токен = await токен_контрагента(к, кандидат)
        if токен is None:
            continue
        записей = к.посредник.записи()
        подготовка = await к.A.вызвать(
            "odata1c_create",
            {"entity": СПРАВОЧНИК, "data": {"Description": к.метка_элемента, ПОЛЕ_ORG: токен}},
            категория="подготовка",
        )
        итог.проверить("подготовка create не пишет в 1С", к.посредник.записи() == записей)
        if код(подготовка) == "token_ambiguous":
            к.отказы_токена["token_ambiguous на create"] += 1
            continue
        к.кандидат1, к.токен1 = кандидат, токен
        break
    if "pending_id" not in подготовка:
        итог.проверить("create подготовлен", False, код(подготовка) or "нет кандидата")
        return
    превью = json.dumps(подготовка.get("preview"), ensure_ascii=False)
    итог.проверить("превью create несёт токен названия контрагента", к.токен1 in превью)
    ответ = await выполнить(к, к.A, подготовка, "create")
    ref = ((ответ.get("key") or {}).get("Ref_Key") or "").lower()
    if GUID.fullmatch(ref):
        к.создан(СПРАВОЧНИК, ref, "элемент справочника приёмки")
        к.C = ref
        к.коммиты["C"] = ответ["commit_id"]
    итог.проверить("ответ commit содержит Ref_Key созданного", bool(к.C))
    if not к.C:
        return
    итог.проверить(
        "ответ commit: поле org — тот же токен",
        (ответ.get("result") or {}).get(ПОЛЕ_ORG) == к.токен1,
    )
    объект = к.прямой.объект(СПРАВОЧНИК, к.C) or {}
    итог.проверить("объект есть в 1С (прямой GET)", bool(объект))
    итог.проверить(
        "Description в 1С — маркер прогона", объект.get("Description") == к.метка_элемента
    )
    реальное = объект.get(ПОЛЕ_ORG)
    итог.проверить(
        "поле org в 1С — реальное название контрагента-источника",
        реальное == к.кандидат1["Description"],
        f"длина {len(реальное or '')} / {len(к.кандидат1['Description'])}",
    )
    итог.проверить("журнал: механизм elicitation", к.механизм(ответ["commit_id"]) == "elicitation")


async def раздел_токен(к: Контекст) -> None:
    заголовок("2. Найти → изменить по токену → перечитать; откат")
    к.итог.раздел = "токен"
    итог = к.итог
    найдено = await к.A.вызвать(
        "odata1c_query",
        {
            "entity": СПРАВОЧНИК,
            "filter": f"substringof('{к.метка_элемента}', Description)",
            "select": ["Ref_Key", "Description", ПОЛЕ_ORG],
        },
    )
    записи = найдено.get("items") or []
    итог.проверить(
        "query по маркеру находит ровно созданный объект",
        len(записи) == 1 and (записи[0].get("Ref_Key") or "").lower() == к.C,
        f"найдено {len(записи)}",
    )
    if записи:
        итог.проверить(
            "поле org в выборке — тот же токен, что у контрагента",
            записи[0].get(ПОЛЕ_ORG) == к.токен1,
        )
    подготовка: dict = {}
    for кандидат in к.кандидаты:
        if кандидат is к.кандидат1:
            continue
        токен = await токен_контрагента(к, кандидат)
        if токен is None or токен == к.токен1:
            continue
        подготовка = await к.A.вызвать(
            "odata1c_update",
            {"entity": СПРАВОЧНИК, "key": к.C, "data": {ПОЛЕ_ORG: токен}},
            категория="подготовка",
        )
        if код(подготовка) == "token_ambiguous":
            к.отказы_токена["token_ambiguous на update"] += 1
            continue
        к.кандидат2, к.токен2 = кандидат, токен
        break
    if "pending_id" not in подготовка:
        итог.проверить("update по токену подготовлен", False, код(подготовка) or "нет кандидата")
        return
    превью = json.dumps(подготовка.get("preview"), ensure_ascii=False)
    итог.проверить(
        "превью update: было и станет — токены", к.токен1 in превью and к.токен2 in превью
    )
    ответ = await выполнить(к, к.A, подготовка, "update по токену")
    if "commit_id" not in ответ:
        return
    к.коммиты["U"] = ответ["commit_id"]
    объект = к.прямой.объект(СПРАВОЧНИК, к.C) or {}
    итог.проверить(
        "в 1С — реальное название второго контрагента",
        объект.get(ПОЛЕ_ORG) == к.кандидат2["Description"],
    )
    перечитано = await к.A.вызвать(
        "odata1c_get", {"entity": СПРАВОЧНИК, "key": к.C, "select": ["Ref_Key", ПОЛЕ_ORG]}
    )
    итог.проверить(
        "перечитано шлюзом — тот же токен, что у контрагента",
        (перечитано.get("item") or {}).get(ПОЛЕ_ORG) == к.токен2,
    )

    откат = await к.A.вызвать("odata1c_undo", {"commit_id": к.коммиты["U"]}, категория="undo")
    превью = json.dumps(откат.get("preview"), ensure_ascii=False)
    итог.проверить(
        "превью отката: было токен второго → станет токен первого",
        к.токен1 in превью and к.токен2 in превью,
    )
    ответ = await выполнить(к, к.A, откат, "откат update")
    if "commit_id" in ответ:
        совпало, всего = к.сверить_с_до(СПРАВОЧНИК, к.C, к.коммиты["U"])
        итог.проверить(
            "после отката объект = before_json журнала",
            совпало == всего and всего > 0,
            f"{совпало} из {всего} полей",
        )
        итог.проверить(
            "журнал: update отмечен откаченным этим откатом",
            (к.журнал(к.коммиты["U"]).undone_by or "") == ответ["commit_id"],
        )


async def пометить(к: Контекст, набор: str, ref: str, пометка: bool, что: str) -> str | None:
    подготовка = await к.A.вызвать(
        "odata1c_mark_for_deletion",
        {"entity": набор, "key": ref, "mark": пометка},
        категория="подготовка",
    )
    ответ = await выполнить(к, к.A, подготовка, что)
    объект = к.прямой.объект(набор, ref) or {}
    к.итог.проверить(f"{что}: DeletionMark в 1С = {пометка}", объект.get("DeletionMark") is пометка)
    return ответ.get("commit_id")


async def откатить(к: Контекст, commit_id: str | None, набор: str, ref: str, что: str) -> dict:
    if not commit_id:
        к.итог.проверить(f"{что}: есть что откатывать", False)
        return {}
    откат = await к.A.вызвать("odata1c_undo", {"commit_id": commit_id}, категория="undo")
    ответ = await выполнить(к, к.A, откат, что)
    исходная = к.журнал(commit_id)
    # У `create` состояния «до» нет — объекта не было; его откат — пометка удаления, и она
    # проверяется отдельно вызывающим.
    if "commit_id" in ответ and исходная is not None and исходная.before is not None:
        совпало, всего = к.сверить_с_до(набор, ref, commit_id)
        к.итог.проверить(
            f"{что}: объект = before_json журнала",
            совпало == всего and всего > 0,
            f"{совпало} из {всего} полей",
        )
    ответ["_откат"] = откат
    return ответ


async def раздел_пометка(к: Контекст) -> None:
    заголовок("3. Пометка удаления и её снятие; откат обеих")
    к.итог.раздел = "пометка"
    м1 = await пометить(к, СПРАВОЧНИК, к.C, True, "пометка")
    м2 = await пометить(к, СПРАВОЧНИК, к.C, False, "снятие пометки")
    await откатить(к, м2, СПРАВОЧНИК, к.C, "откат снятия")
    ответ = await откатить(к, м1, СПРАВОЧНИК, к.C, "откат пометки")
    к.итог.заметка(
        "предупреждение «объект изменён после коммита» у отката пометки: "
        + ("есть" if (ответ.get("_откат") or {}).get("warning") else "нет")
    )


async def раздел_устаревание(к: Контекст) -> None:
    заголовок("6а. pending_stale: объект изменён между подготовкой и commit")
    к.итог.раздел = "устаревание"
    итог = к.итог
    первая = await к.A.вызвать(
        "odata1c_update",
        {"entity": СПРАВОЧНИК, "key": к.C, "data": {ПОЛЕ_ТЕКСТ: f"{к.метка} s1"}},
        категория="подготовка",
    )
    вторая = await к.A.вызвать(
        "odata1c_update",
        {"entity": СПРАВОЧНИК, "key": к.C, "data": {ПОЛЕ_ТЕКСТ: f"{к.метка} s2"}},
        категория="подготовка",
    )
    ответ = await выполнить(к, к.A, вторая, "вторая операция")
    записей = к.посредник.записи()
    устаревшая = await к.A.вызвать("odata1c_commit", {"pending_id": первая.get("pending_id")})
    итог.проверить("commit первой — pending_stale", код(устаревшая) == "pending_stale")
    итог.проверить("pending_stale: в 1С не ушло ни одной записи", к.посредник.записи() == записей)
    объект = к.прямой.объект(СПРАВОЧНИК, к.C) or {}
    итог.проверить("в 1С значение второй операции", объект.get(ПОЛЕ_ТЕКСТ) == f"{к.метка} s2")
    await откатить(к, ответ.get("commit_id"), СПРАВОЧНИК, к.C, "откат второй операции")


async def раздел_запреты(к: Контекст) -> None:
    заголовок("6б. Запреты до обращения к 1С: deny_fields и write: false")
    к.итог.раздел = "запреты"
    итог = к.итог
    всего = к.посредник.всего()
    ответ = await к.A.вызвать(
        "odata1c_update",
        {"base": БАЗА_ЗАПРЕТ, "entity": СПРАВОЧНИК, "key": к.C, "data": {ПОЛЕ_ЗАПРЕТ: "x"}},
    )
    итог.проверить("поле из deny_fields — field_write_denied", код(ответ) == "field_write_denied")
    итог.проверить("field_write_denied: к 1С ни одного запроса", к.посредник.всего() == всего)
    for тул, аргументы in (
        (
            "odata1c_update",
            {"entity": СПРАВОЧНИК, "key": к.C, "data": {ПОЛЕ_ТЕКСТ: "x"}},
        ),
        ("odata1c_create", {"entity": СПРАВОЧНИК, "data": {"Description": f"{к.метка} ro"}}),
        ("odata1c_mark_for_deletion", {"entity": СПРАВОЧНИК, "key": к.C}),
    ):
        ответ = await к.A.вызвать(тул, {"base": БАЗА_ЧТЕНИЕ, **аргументы})
        итог.проверить(
            f"{тул} в базе write: false — base_read_only", код(ответ) == "base_read_only"
        )
    итог.проверить("base_read_only: к 1С ни одного запроса", к.посредник.всего() == всего)


async def раздел_документ(к: Контекст) -> None:
    заголовок("4. Документ: создать → Post → Unpost; откат каждой операции")
    к.итог.раздел = "документ"
    итог = к.итог
    подготовка = await к.A.вызвать(
        "odata1c_create",
        {"entity": ДОКУМЕНТ, "data": {"Комментарий": f"{к.метка} документ"}},
        категория="подготовка",
    )
    ответ = await выполнить(к, к.A, подготовка, "create документа")
    ref = ((ответ.get("key") or {}).get("Ref_Key") or "").lower()
    if not GUID.fullmatch(ref):
        итог.проверить("документ создан", False)
        return
    к.создан(ДОКУМЕНТ, ref, "документ приёмки")
    к.D = ref
    к.коммиты["D"] = ответ["commit_id"]
    объект = к.прямой.объект(ДОКУМЕНТ, ref) or {}
    итог.проверить("документ есть в 1С, не проведён", объект.get("Posted") is False)

    async def действие(имя: str, что: str) -> str | None:
        п = await к.A.вызвать(
            "odata1c_action",
            {"entity": ДОКУМЕНТ, "key": ref, "name": имя},
            категория="подготовка",
        )
        о = await выполнить(к, к.A, п, что)
        return о.get("commit_id")

    провести = await действие("Post", "Post")
    итог.проверить(
        "Posted в 1С = true", (к.прямой.объект(ДОКУМЕНТ, ref) or {}).get("Posted") is True
    )
    записей = к.посредник.записи()
    отказ = await к.A.вызвать(
        "odata1c_mark_for_deletion", {"entity": ДОКУМЕНТ, "key": ref}, категория="подготовка"
    )
    итог.проверить(
        "пометка проведённого — отказ при подготовке (решение 12), не 500 на commit",
        код(отказ) == "params_invalid" and "Unpost" in json.dumps(отказ, ensure_ascii=False),
        код(отказ) or "",
    )
    итог.проверить("отказ пометки: к 1С ни одной записи", к.посредник.записи() == записей)
    распровести = await действие("Unpost", "Unpost")
    итог.проверить(
        "Posted в 1С = false", (к.прямой.объект(ДОКУМЕНТ, ref) or {}).get("Posted") is False
    )
    await откатить(к, распровести, ДОКУМЕНТ, ref, "откат Unpost")
    итог.проверить(
        "после отката Unpost — проведён",
        (к.прямой.объект(ДОКУМЕНТ, ref) or {}).get("Posted") is True,
    )
    await откатить(к, провести, ДОКУМЕНТ, ref, "откат Post")
    итог.проверить(
        "после отката Post — не проведён",
        (к.прямой.объект(ДОКУМЕНТ, ref) or {}).get("Posted") is False,
    )
    ответ = await откатить(к, к.коммиты["D"], ДОКУМЕНТ, ref, "откат create документа")
    объект = к.прямой.объект(ДОКУМЕНТ, ref) or {}
    итог.проверить(
        "откат create документа — пометка удаления, не проведён",
        объект.get("DeletionMark") is True and объект.get("Posted") is False,
    )
    итог.проверить(
        "откат create — операция mark_for_deletion",
        (ответ.get("_откат") or {}).get("undo_op") == "mark_for_deletion",
    )
    записей = к.посредник.записи()
    отказ = await к.A.вызвать(
        "odata1c_action", {"entity": ДОКУМЕНТ, "key": ref, "name": "Post"}, категория="подготовка"
    )
    итог.проверить(
        "проведение помеченного — отказ при подготовке (решение 12), не 500 на commit",
        код(отказ) == "params_invalid",
        код(отказ) or "",
    )
    итог.проверить("отказ проведения: к 1С ни одной записи", к.посредник.записи() == записей)


async def раздел_ошибка_1с(к: Контекст) -> None:
    заголовок("6в. Ошибка 1С на commit на уровне identifiers+names — без текста (Ruling 52)")
    к.итог.раздел = "ошибка_1с"
    итог = к.итог
    # Наименование номенклатуры уникально и среди помеченных (P8): второй элемент с тем же
    # `Description` 1С отклоняет в `ПередЗаписью` HTTP 500 — подготовка этого не видит.
    подготовка = await к.A.вызвать(
        "odata1c_create",
        {"entity": СПРАВОЧНИК, "data": {"Description": к.метка_элемента}},
        категория="подготовка",
    )
    итог.проверить(
        "дубль наименования подготовлен", "pending_id" in подготовка, код(подготовка) or ""
    )
    if "pending_id" not in подготовка:
        return
    записей = к.посредник.записи()
    ответ = await к.A.вызвать(
        "odata1c_commit", {"pending_id": подготовка["pending_id"]}, категория="commit_отказ"
    )
    итог.проверить("запрос дошёл до 1С", к.посредник.записи() - записей == 1)
    if "commit_id" in ответ:
        ref = ((ответ.get("key") or {}).get("Ref_Key") or "").lower()
        if GUID.fullmatch(ref):
            к.создан(СПРАВОЧНИК, ref, "дубль, который 1С неожиданно приняла")
        итог.проверить("1С отказала в записи дубля", False, "запись выполнена")
        return
    ошибка = ответ.get("error") or {}
    сообщение = ошибка.get("message") or ""
    итог.проверить("отказ commit — ошибка 1С", код(ответ) == "odata_error", код(ответ) or "")
    итог.проверить("в ответе HTTP-статус 500", "HTTP 500" in сообщение)
    итог.проверить("текст ошибки 1С не показан (identifiers+names)", "не показывается" in сообщение)
    номер = GUID.search(сообщение)
    строка = к.журнал(номер.group(0)) if номер else None
    итог.проверить(
        "журнал: запись failed с полным текстом 1С",
        bool(строка) and строка.status == "failed" and bool(строка.error),
        строка.status if строка else "нет строки",
    )
    if строка and строка.error:
        разбор = re.fullmatch(
            r"(?s)(?P<code>[a-z_]+) \(HTTP (?P<status>[0-9]{3})\): (?P<text>.*)", строка.error
        )
        текст_1с = (
            map_error(int(разбор["status"]), разбор["text"]).message if разбор else строка.error
        )
        к.текст_ошибки_1с = текст_1с
        кусок = текст_1с.strip()[:40]
        утекло = sum(1 for _, _, т in к.сбор.ответы if кусок and кусок in т)
        итог.проверить(
            "текст ошибки 1С из журнала не встречается ни в одном ответе",
            утекло == 0,
            f"длина текста {len(текст_1с)}, вхождений {утекло}",
        )
        к.commit_ошибки = строка.commit_id
    дубли = к.прямой.по_маркеру(СПРАВОЧНИК, "Description", к.метка_элемента)
    итог.проверить(
        "в 1С по-прежнему один элемент с этим наименованием", len(дубли) == 1, f"{len(дубли)}"
    )


async def раздел_клиент(к: Контекст) -> None:
    заголовок("6г. Клиент без elicitation при write_confirm_fallback: deny")
    к.итог.раздел = "клиент"
    итог = к.итог
    до = (к.прямой.объект(СПРАВОЧНИК, к.C) or {}).get("DataVersion")
    async with через_лаунчер(к, "odata1c-m2-no-elicitation", elicitation=False) as N:
        подготовка = await N.вызвать(
            "odata1c_update",
            {"entity": СПРАВОЧНИК, "key": к.C, "data": {ПОЛЕ_ТЕКСТ: f"{к.метка} n"}},
            категория="подготовка",
        )
        итог.проверить("подготовка у клиента без elicitation", "pending_id" in подготовка)
        записей = к.посредник.записи()
        ответ = await N.вызвать("odata1c_commit", {"pending_id": подготовка.get("pending_id")})
    итог.проверить("commit — write_unsupported_client", код(ответ) == "write_unsupported_client")
    итог.проверить("в 1С не ушло ни одной записи", к.посредник.записи() == записей)
    после = (к.прямой.объект(СПРАВОЧНИК, к.C) or {}).get("DataVersion")
    итог.проверить("DataVersion объекта не изменилась", до == после and до is not None)


async def раздел_подпись(к: Контекст) -> None:
    заголовок("6д. Подпись лаунчера (Ruling 59): claude_code — только подписанному")
    к.итог.раздел = "подпись"
    итог = к.итог

    # Настоящий лаунчер, клиент назвался claude-code 2.1.267: лаунчер подписывает его заголовки
    # ключом временного дома, демон приёмки выдаёт механизм claude_code — без вопроса демона.
    async with через_лаунчер(к, "claude-code", версия="2.1.267") as L:
        подготовка = await L.вызвать(
            "odata1c_update",
            {"entity": СПРАВОЧНИК, "key": к.C, "data": {ПОЛЕ_ТЕКСТ: f"{к.метка} cc"}},
            категория="подготовка",
        )
        ответ = await выполнить(к, L, подготовка, "лаунчер claude-code")
        итог.проверить("лаунчер claude-code: демон не спрашивал", L.вопросы == [])
        if "commit_id" in ответ:
            итог.проверить(
                "журнал: механизм claude_code", к.механизм(ответ["commit_id"]) == "claude_code"
            )
            откат = await L.вызвать(
                "odata1c_undo", {"commit_id": ответ["commit_id"]}, категория="undo"
            )
            о = await выполнить(к, L, откат, "откат из сессии claude-code")
            if "commit_id" in о:
                совпало, всего = к.сверить_с_до(СПРАВОЧНИК, к.C, ответ["commit_id"])
                итог.проверить(
                    "откат: объект = before_json",
                    совпало == всего and всего > 0,
                    f"{совпало} из {всего}",
                )

    # Прямой клиент с заголовками claude-code, подписанными ключом дома (так делает лаунчер), —
    # положительный контроль и сессия, чей идентификатор известен. Затем в ту же сессию —
    # запросы без подписи (как `curl` процесса, узнавшего идентификатор).
    ключ = read_launcher_key(к.дом)
    итог.проверить("ключ лаунчера во временном доме создан лаунчером", ключ is not None)
    if ключ is None:
        return
    async with напрямую(к, "odata1c-m2-signed", заголовки=ЗАГОЛОВКИ_CLAUDE_CODE, ключ=ключ) as S:
        первая = await S.вызвать(
            "odata1c_update",
            {"entity": СПРАВОЧНИК, "key": к.C, "data": {ПОЛЕ_ТЕКСТ: f"{к.метка} s"}},
            категория="подготовка",
        )
        ответ = await выполнить(к, S, первая, "подписанный клиент")
        if "commit_id" not in ответ:
            return
        итог.проверить(
            "подписанный: механизм claude_code", к.механизм(ответ["commit_id"]) == "claude_code"
        )
        откат = await S.вызвать("odata1c_undo", {"commit_id": ответ["commit_id"]}, категория="undo")
        итог.проверить("подписанный: откат подготовлен", "pending_id" in откат)
        [sid] = S.идентификаторы[:1] or [None]
        итог.проверить("идентификатор сессии известен", bool(sid))
        записей = к.посредник.записи()
        номер = 900
        for тул, аргументы in (
            ("odata1c_update", {"entity": СПРАВОЧНИК, "key": к.C, "data": {ПОЛЕ_ТЕКСТ: "x"}}),
            ("odata1c_create", {"entity": СПРАВОЧНИК, "data": {"Description": "x"}}),
            ("odata1c_mark_for_deletion", {"entity": СПРАВОЧНИК, "key": к.C}),
            ("odata1c_undo", {"commit_id": ответ["commit_id"]}),
            ("odata1c_commit", {"pending_id": откат.get("pending_id")}),
        ):
            номер += 1
            чужой = await сырой_вызов(к, sid, номер, тул, аргументы)
            итог.проверить(
                f"без подписи в сессии claude_code: {тул} — write_unsupported_client",
                код(чужой) == "write_unsupported_client",
                код(чужой) or "",
            )
        журнал = await сырой_вызов(к, sid, номер + 1, "odata1c_journal", {"limit": 3})
        итог.проверить("без подписи: чтение журнала отвечает", "entries" in журнал)
        итог.проверить("без подписи: в 1С не ушло ни одной записи", к.посредник.записи() == записей)
        о = await выполнить(к, S, откат, "подписанный commit отката")
        if "commit_id" in о:
            совпало, всего = к.сверить_с_до(СПРАВОЧНИК, к.C, ответ["commit_id"])
            итог.проверить(
                "откат: объект = before_json",
                совпало == всего and всего > 0,
                f"{совпало} из {всего}",
            )

    # Прямой клиент назвался claude-code в initialize, заголовков и подписи нет.
    async with напрямую(к, "claude-code") as U:
        подготовка = await U.вызвать(
            "odata1c_update",
            {"entity": СПРАВОЧНИК, "key": к.C, "data": {ПОЛЕ_ТЕКСТ: f"{к.метка} u"}},
        )
        записей = к.посредник.записи()
        ответ = await U.вызвать("odata1c_commit", {"pending_id": подготовка.get("pending_id")})
    итог.проверить(
        "claude-code в initialize без подписи — write_unsupported_client",
        код(ответ) == "write_unsupported_client",
        код(ответ) or "",
    )
    итог.проверить("самообъявление: в 1С не ушло ни одной записи", к.посредник.записи() == записей)


async def раздел_откат_создания(к: Контекст) -> None:
    заголовок("5. Откат create элемента — пометка удаления созданного")
    к.итог.раздел = "откат"
    ответ = await откатить(к, к.коммиты.get("C"), СПРАВОЧНИК, к.C, "откат create элемента")
    объект = к.прямой.объект(СПРАВОЧНИК, к.C) or {}
    к.итог.проверить("элемент помечен на удаление", объект.get("DeletionMark") is True)
    к.итог.проверить(
        "откат create — операция mark_for_deletion",
        (ответ.get("_откат") or {}).get("undo_op") == "mark_for_deletion",
    )


async def раздел_журнал(к: Контекст) -> None:
    заголовок("Журнал записи: тул и файл временного дома")
    к.итог.раздел = "журнал"
    итог = к.итог
    ответ = await к.A.вызвать("odata1c_journal", {"base": БАЗА, "limit": 100})
    строки = ответ.get("entries") or []
    статусы = Counter(с.get("status") for с in строки)
    итог.проверить("журнал отвечает", bool(строки), f"строк {len(строки)}, {dict(статусы)}")
    откаченных = sum(1 for с in строки if с.get("undone_by"))
    откатов = sum(1 for с in строки if с.get("undo_of"))
    итог.проверить(
        "цепочки откатов видны (undone_by / undo_of)",
        откаченных == откатов > 0,
        f"откачено {откаченных}, откатов {откатов}",
    )
    if getattr(к, "commit_ошибки", None):
        [строка] = [с for с in строки if с.get("commit_id") == к.commit_ошибки] or [{}]
        ошибка = строка.get("error") or {}
        итог.проверить(
            "журнал: ошибка 1С — статус, код, категория, текста нет",
            строка.get("status") == "failed"
            and ошибка.get("http_status") == 500
            and ошибка.get("text") is None
            and bool(ошибка.get("note")),
        )
    журнал = Journal(к.дом / "journal.sqlite")
    try:
        все = журнал.recent(БАЗА, 200)
    finally:
        журнал.close()
    клиенты = Counter(с.client for с in все)
    итог.заметка(f"журнал временного дома по механизмам: {dict(клиенты)}")
    к.итог.сведения["журнал_механизмы"] = dict(клиенты)
    к.итог.сведения["журнал_статусы"] = dict(Counter(с.status for с in все))


def иглы_объектов(к: Контекст) -> None:
    """Значения защищаемых полей объектов приёмки после всех записей (поле org справочника) —
    тоже иглы сценария 7. Читаются до закрытия прямого клиента."""
    for набор, ref in ((СПРАВОЧНИК, к.C), (ДОКУМЕНТ, к.D)):
        if not ref:
            continue
        объект = к.прямой.объект(набор, ref) or {}
        for поле in (ПОЛЕ_ORG, "Контрагент", "Принципал"):
            к.иглы.добавить("org", объект.get(поле))
        if набор == СПРАВОЧНИК:
            # Положительный контроль поиска: сырой ответ 1С на тот же объект (в памяти, в вывод
            # не идёт) обязан содержать иглу — иначе «ноль вхождений» ничего бы не доказывал.
            к.контроль_игл = json.dumps(объект, ensure_ascii=False)


def сценарий_7(к: Контекст) -> None:
    заголовок("7. От класса данных: реальные значения во всех ответах и вопросах")
    к.итог.раздел = "утечки"
    итог = к.итог
    сводка = к.иглы.сводка()
    итог.заметка(f"известных реальных значений по классам: {сводка}")
    по_тулам: Counter = Counter()
    найдено: Counter = Counter()
    for _, тул, текст in к.сбор.ответы:
        по_тулам[тул] += 1
        найдено.update(к.иглы.найти(строки_ответа(текст)))
    в_вопросах: Counter = Counter()
    for вопрос in к.сбор.вопросы:
        в_вопросах.update(к.иглы.найти(вопрос))
    итог.заметка(f"ответов тулов {len(к.сбор.ответы)}: {dict(по_тулам)}")
    итог.заметка(f"текстов вопросов elicitation {len(к.сбор.вопросы)}")
    итог.проверить(
        "ни одного реального значения в ответах тулов",
        not найдено,
        str(dict(найдено)) if найдено else "",
    )
    итог.проверить(
        "ни одного реального значения в текстах elicitation",
        not в_вопросах,
        str(dict(в_вопросах)) if в_вопросах else "",
    )
    if к.кандидаты:
        итог.проверить("иглы есть (проверка не пустая)", sum(сводка.values()) > 0)
        контроль = к.иглы.найти(строки_ответа(к.контроль_игл or ""))
        итог.проверить(
            "положительный контроль: в сыром ответе 1С на объект приёмки игла находится",
            bool(контроль),
            str(dict(контроль)) if контроль else "",
        )
    к.итог.сведения["иглы"] = сводка
    к.итог.сведения["ответы_по_тулам"] = dict(по_тулам)
    к.итог.сведения["вопросов"] = len(к.сбор.вопросы)


async def уборка(к: Контекст, маркер: str) -> list[dict]:
    """Проведённые — `Unpost`, всем — `DeletionMark=true`: через шлюз, иначе напрямую. Объекты —
    созданные прогоном и найденные по маркеру (прямым GET)."""
    заголовок(f"8. Уборка (маркер «{маркер}»)")
    к.итог.раздел = "уборка"
    объекты: dict[tuple[str, str], str] = {
        (з["набор"], з["ref"]): з["что"] for з in к.итог.созданные
    }
    for набор, поле in ((СПРАВОЧНИК, "Description"), (ДОКУМЕНТ, "Комментарий")):
        for строка in к.прямой.по_маркеру(набор, поле, маркер):
            ref = строка["Ref_Key"].lower()
            if (набор, ref) not in объекты:
                объекты[(набор, ref)] = "найден по маркеру"
                к.создан(набор, ref, "найден по маркеру")

    def состояние(набор: str, ref: str) -> dict:
        return к.прямой.объект(набор, ref) or {}

    нужно = [
        (набор, ref)
        for (набор, ref) in объекты
        if состояние(набор, ref).get("Posted") is True
        or состояние(набор, ref).get("DeletionMark") is not True
    ]
    через_шлюз = напрямую_сделано = 0
    if нужно:
        try:
            async with через_лаунчер(к, "odata1c-m2-cleanup") as У:
                for набор, ref in нужно:
                    с = состояние(набор, ref)
                    if с.get("Posted") is True:
                        п = await У.вызвать(
                            "odata1c_action", {"entity": набор, "key": ref, "name": "Unpost"}
                        )
                        if "pending_id" in п:
                            await У.вызвать("odata1c_commit", {"pending_id": п["pending_id"]})
                    if состояние(набор, ref).get("DeletionMark") is not True:
                        п = await У.вызвать(
                            "odata1c_mark_for_deletion", {"entity": набор, "key": ref}
                        )
                        if "pending_id" in п:
                            await У.вызвать("odata1c_commit", {"pending_id": п["pending_id"]})
                    через_шлюз += 1
        except Exception as сбой:  # noqa: BLE001 — уборка обязана дойти до запасного пути
            print(f"  уборка через шлюз не удалась: {type(сбой).__name__}", flush=True)
    for набор, ref in нужно:
        с = состояние(набор, ref)
        if с.get("Posted") is True:
            к.прямой.запрос("POST", f"{набор}(guid'{ref}')/Unpost")
            напрямую_сделано += 1
        if состояние(набор, ref).get("DeletionMark") is not True:
            к.прямой.запрос("PATCH", f"{набор}(guid'{ref}')", тело={"DeletionMark": True})
            напрямую_сделано += 1
    итог_уборки = []
    for (набор, ref), что in объекты.items():
        с = состояние(набор, ref)
        итог_уборки.append(
            {
                "набор": набор,
                "ref": ref,
                "что": что,
                "Posted": с.get("Posted"),
                "DeletionMark": с.get("DeletionMark"),
            }
        )
        print(
            f"  {набор} {ref}: Posted={с.get('Posted')} DeletionMark={с.get('DeletionMark')}"
            f" ({что})",
            flush=True,
        )
    к.итог.заметка(
        f"объектов {len(объекты)}, требовали уборки {len(нужно)}, через шлюз {через_шлюз}, "
        f"напрямую действий {напрямую_сделано}"
    )
    к.итог.проверить(
        "все объекты приёмки помечены на удаление и не проведены",
        all(о["DeletionMark"] is True and о["Posted"] is not True for о in итог_уборки),
    )
    return итог_уборки


def замеры(к: Контекст) -> dict:
    заголовок("Замеры времени")
    итоги = {}
    for категория in ("подготовка", "commit", "undo"):
        все = [з["с"] for з in к.итог.замеры if з["категория"] == категория and з["ок"]]
        if not все:
            continue
        первый, остальные = все[0], все[1:] or все
        итоги[категория] = {
            "первый_с": round(первый, 2),
            "медиана_с": round(statistics.median(остальные), 2),
            "макс_с": round(max(остальные), 2),
            "n": len(все),
        }
        print(
            f"  {категория}: первый {первый:.2f} с; медиана остальных "
            f"{statistics.median(остальные):.2f} с; макс {max(остальные):.2f} с; n={len(все)}",
            flush=True,
        )
    return итоги


async def прогон(к: Контекст, нужен) -> None:
    начало = time.monotonic()
    async with через_лаунчер(к, "odata1c-m2-acceptance") as A:
        к.A = A
        к.итог.сведения["старт_лаунчера_и_демона_с"] = round(time.monotonic() - начало, 2)
        к.итог.заметка(f"лаунчер и демон подняты за {time.monotonic() - начало:.2f} с")
        if нужен("тулы"):
            await раздел_тулы(к)
        справочник = any(нужен(р) for р in РАЗДЕЛЫ_СПРАВОЧНИКА)
        if справочник:
            выбрать_контрагентов(к)
            await раздел_создание(к)
        if к.C:
            if нужен("токен"):
                await раздел_токен(к)
            if нужен("пометка"):
                await раздел_пометка(к)
            if нужен("устаревание"):
                await раздел_устаревание(к)
            if нужен("запреты"):
                await раздел_запреты(к)
        if нужен("документ"):
            await раздел_документ(к)
        if к.C:
            if нужен("ошибка_1с"):
                await раздел_ошибка_1с(к)
            if нужен("клиент"):
                await раздел_клиент(к)
            if нужен("подпись"):
                await раздел_подпись(к)
            await раздел_откат_создания(к)
        if нужен("журнал"):
            await раздел_журнал(к)


async def main() -> int:
    разбор = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    разбор.add_argument(
        "--home",
        default=str(pathlib.Path.home() / ".claude" / "odata1c"),
        help="рабочий домашний каталог (только чтение)",
    )
    разбор.add_argument("--out", help="куда сохранить итог JSON (счётчики, без значений)")
    разбор.add_argument("--only", help="разделы через запятую; «уборка» — только уборка")
    аргументы = разбор.parse_args()
    только = set(аргументы.only.split(",")) if аргументы.only else None
    if только and "уборка" not in только:
        неизвестные = только - set(РАЗДЕЛЫ)
        if неизвестные:
            print(f"неизвестные разделы: {sorted(неизвестные)}; есть: {РАЗДЕЛЫ}")
            return 2

    def нужен(раздел: str) -> bool:
        return только is None or раздел in только

    рабочий = pathlib.Path(аргументы.home)
    запись = yaml.safe_load((рабочий / "bases.yaml").read_text(encoding="utf-8"))["bases"][БАЗА]
    прямой = Прямой1С(запись)
    посредник = Посредник1С(запись["url"], verify=запись.get("verify_tls", True))
    дом = собрать_дом(рабочий, посредник.адрес)
    метка = f"{МАРКЕР_ОБЩИЙ} {time.strftime('%d.%m %H-%M-%S')}"
    к = Контекст(дом, прямой, посредник, метка)
    print(f"прогон «{метка}»: временный дом создан, демон {ПОРТ_ДЕМОНА}, посредник {ПОРТ_1С}")
    только_уборка = только == {"уборка"}
    try:
        if not только_уборка:
            try:
                await прогон(к, нужен)
            except Exception as сбой:  # noqa: BLE001 — итог прогона и уборка важнее трассы
                место = traceback.extract_tb(сбой.__traceback__)[-1]
                к.итог.раздел = "прогон"
                к.итог.проверить(
                    "прогон без необработанного исключения",
                    False,
                    f"{type(сбой).__name__} в строке {место.lineno} ({место.name})",
                )
    finally:
        try:
            к.итог.сведения["уборка"] = await asyncio.wait_for(
                уборка(к, МАРКЕР_ОБЩИЙ if только_уборка else метка), 900
            )
            иглы_объектов(к)
        finally:
            остановить_демон(дом)
            посредник.закрыть()
            прямой.close()
            shutil.rmtree(дом, ignore_errors=True)
            print(f"\nвременный дом удалён: {not дом.exists()}", flush=True)
    if not только_уборка:
        сценарий_7(к)
    к.итог.раздел = "посредник"
    к.итог.проверить(
        "посредник: ни одной попытки записи вне объектов приёмки, ни одного DELETE/PUT",
        not посредник.нарушения,
        str(Counter(посредник.нарушения)) if посредник.нарушения else "",
    )
    к.итог.сведения["запросы_к_1с"] = dict(посредник.счёт)
    к.итог.заметка(f"запросов к 1С через посредника: {dict(посредник.счёт)}")
    к.итог.сведения["замеры"] = замеры(к)
    к.итог.сведения["отказы_токена"] = dict(к.отказы_токена)
    к.итог.сведения["метка"] = форма(метка)
    заголовок("Итог")
    по_разделам: dict[str, list[int]] = {}
    for п in к.итог.проверки:
        счёт = по_разделам.setdefault(п["раздел"], [0, 0])
        счёт[0] += п["ок"]
        счёт[1] += 1
    for раздел, (ок, всего) in по_разделам.items():
        print(f"  {раздел}: {ок} из {всего}")
    провалено = [п for п in к.итог.проверки if not п["ок"]]
    print(f"  всего проверок {len(к.итог.проверки)}, не пройдено {len(провалено)}")
    if аргументы.out:
        pathlib.Path(аргументы.out).write_text(
            json.dumps(
                {
                    "проверки": к.итог.проверки,
                    "созданные": к.итог.созданные,
                    "сведения": к.итог.сведения,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 1 if провалено else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
