"""`odata1c doctor [--online]` — проверка окружения без секретов в выводе (SPEC design §8,
план M3 «package», задача 2).

Десять строк таблицы (design §8): Python, uv, дом шлюза, bases.yaml, daemon.yaml, launcher.key,
«база <имя>» (одна на каждую описанную базу), «соединение с <имя>» (только `--online`), демон,
Claude Code. Худший статус любой строки определяет код возврата: OK → 0, WARN → 1, FAIL → 2
(`exit_code`).

Инвариант этого модуля — тот же, что у остального шлюза (AGENTS.md, инвариант 1), но применён к
собственным настройкам, а не к данным 1С: ни пароль, ни имя пользователя 1С, ни полный адрес базы
(только схема и хост — `_хост`) не попадают ни в один `Check.detail`, а значит и в `render()`.
`bases.yaml`/`daemon.yaml` разбираются здесь СВОИМ кодом (`_разобрать_yaml_доктор`), а не через
`config.loader.load_config`: при синтаксической ошибке PyYAML вклеивает в текст исключения
фрагмент исходного файла вокруг места ошибки — если повреждение пришлось на строку с паролем,
он дословно попал бы в `ConfigError.message`. `_разобрать_yaml_доктор` берёт из исключения только
позицию (номер строки, колонки) — числа, не текст файла, — тем же приёмом, что и
`config.loader._разобрать_yaml`. Разбор bases.yaml/daemon.yaml здесь ЖЕЛАТЕЛЬНО не должен зависеть
от `load_config`: та поднимает `ConfigError` на первой же проблеме (daemon ИЛИ bases) одним общим
текстом, а таблица различает эти два файла отдельными строками — атрибуция по подстроке в тексте
исключения была бы хрупкой, поэтому оба файла проверяются независимо, до попытки `load_config`.

`run()` не бросает исключений НИКОГДА (`test_нет_дома_fail`: домашнего каталога нет вовсе, и
остальные строки — uv, демон, Claude Code — всё равно должны быть построены) — каждая проверка
самостоятельно перехватывает свои ошибки и превращает их в статус строки, а не в traceback.
"""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import platform
import shutil
import subprocess
import sys
import urllib.parse
from collections.abc import Callable
from typing import Literal

import httpx2
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from odata1c import __version__
from odata1c.client1c.client import Client1C
from odata1c.client1c.errors import OdataError
from odata1c.config.home import check_file_permissions
from odata1c.config.loader import ConfigError, load_config
from odata1c.config.models import BaseConfig, DaemonConfig
from odata1c.config.writer import read_launcher_key
from odata1c.daemon import daemon_url, is_listening
from odata1c.gate.policy import PolicyError
from odata1c.gate.policy_check import check_policy
from odata1c.gate.service import policy_path
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexCorruptError, IndexRepository

Status = Literal["OK", "WARN", "FAIL"]

_ВЕС_СТАТУСА: dict[Status, int] = {"OK": 0, "WARN": 1, "FAIL": 2}

# SPEC design §8: сбой опроса демона — WARN «не запущен», не FAIL; таймаут держит доктора от
# зависания на порту, который отвечает, но не как MCP-сервер (чужой процесс).
ТАЙМАУТ_ОПРОСА_ДЕМОНА_S = 3.0


@dataclasses.dataclass(slots=True, frozen=True)
class Check:
    """Одна строка таблицы `doctor`."""

    name: str
    status: Status
    detail: str


def exit_code(checks: list[Check]) -> int:
    """0 — все строки OK; 1 — худшая строка WARN; 2 — худшая строка FAIL (пустой список — 0)."""
    return max((_ВЕС_СТАТУСА[проверка.status] for проверка in checks), default=0)


def render(checks: list[Check]) -> str:
    """Моноширинная таблица: статус, имя строки, деталь. Не добавляет и не убирает ничего из
    `detail` — секреты в вывод не попадают потому, что их нет в `Check.detail` изначально
    (см. докстринг модуля), не потому, что render() их фильтрует."""
    if not checks:
        return "проверок нет\n"
    ширина_имени = max(len(проверка.name) for проверка in checks)
    строки = (
        f"{проверка.status:<4} {проверка.name.ljust(ширина_имени)}  {проверка.detail}".rstrip()
        for проверка in checks
    )
    return "\n".join(строки) + "\n"


def _хост(url: str) -> str:
    """Схема и хост адреса базы, без пути и без учётных данных (design §8: «адрес базы — только
    схема и хост»). `urllib.parse.urlsplit` кладёт `user:password@` в `.username`/`.password`,
    а не в `.hostname` — сведения о доступе отсекаются самим разбором, а не последующей чисткой
    строки (та же ошибка класса, которую чинит `cli._без_учётных_данных`, но эта функция к тому
    же убирает путь, которого `_без_учётных_данных` не трогает)."""
    части = urllib.parse.urlsplit(url)
    хост = части.hostname or ""
    порт = f":{части.port}" if части.port else ""
    return f"{части.scheme}://{хост}{порт}"


def _check_python() -> Check:
    """Строка `Python` таблицы (design §8): FAIL, если интерпретатор ниже 3.12 (`requires-python`
    пакета). Ruff UP036 считает такое сравнение «устаревшим» относительно минимума ПРОЕКТА — это
    ложное срабатывание для строки, которая как раз существует ради интерпретатора, под которым
    сам пакет мог и не установиться (system Python, PATH перепутан); подавлено осознанно."""
    версия = platform.python_version()
    if sys.version_info >= (3, 12):  # noqa: UP036 — проверка для чужого интерпретатора, не для этого пакета
        return Check("Python", "OK", версия)
    return Check("Python", "FAIL", f"версия {версия} ниже минимальной 3.12")


def _вывод_команды(итог: subprocess.CompletedProcess) -> str:
    сырой = итог.stdout or итог.stderr or b""
    if isinstance(сырой, bytes):
        сырой = сырой.decode("utf-8", errors="replace")
    return сырой.strip()


def _версия_бинарника(путь: str, run: Callable[..., subprocess.CompletedProcess]) -> str | None:
    """Строка версии внешней программы или `None` — не удалось запустить или пустой вывод.

    `capture_output=True, text=False`: читаем байты и декодируем сами (`errors="replace"`),
    как `config.home.check_file_permissions` — синхронный `text=True` под `PYTHONUTF8=1` может
    упасть `UnicodeDecodeError` внутри `subprocess.run` на консольных утилитах с выводом не в
    utf-8; `uv --version`/`claude --version` обычно в utf-8/ascii, но декодировать самим и не
    доверять кодировке процесса дешевле, чем поймать ту же ловушку здесь."""
    try:
        итог = run([путь, "--version"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return _вывод_команды(итог) or None


def _check_uv(
    which: Callable[[str], str | None], run: Callable[..., subprocess.CompletedProcess]
) -> Check:
    путь = which("uv")
    if путь is None:
        return Check("uv", "WARN", "не найден в PATH (uvx нужен плагину, не пакету)")
    версия = _версия_бинарника(путь, run)
    if версия is None:
        return Check("uv", "WARN", "найден, но версию определить не удалось")
    return Check("uv", "OK", версия)


def _check_claude(
    which: Callable[[str], str | None], run: Callable[..., subprocess.CompletedProcess]
) -> Check:
    путь = which("claude")
    if путь is None:
        return Check("Claude Code", "WARN", "не найден в PATH")
    версия = _версия_бинарника(путь, run)
    if версия is None:
        return Check("Claude Code", "WARN", "найден, но версию определить не удалось")
    return Check("Claude Code", "OK", версия)


def _check_home(home: pathlib.Path) -> Check:
    if not home.is_dir():
        return Check("дом шлюза", "FAIL", f"каталог не найден: {home}")
    предупреждение = check_file_permissions(home)
    if предупреждение:
        return Check("дом шлюза", "WARN", предупреждение)
    return Check("дом шлюза", "OK", str(home))


def _разобрать_yaml_доктор(path: pathlib.Path) -> tuple[dict | None, str | None]:
    """`(данные, None)` при успехе, `(None, деталь)` при синтаксической ошибке — `деталь`
    называет путь и место (строка, колонка), но не содержит ни фрагмента файла, ни значения
    (см. докстринг модуля — тот же приём, что `config.loader._разобрать_yaml`)."""
    try:
        текст = path.read_text(encoding="utf-8")
    except OSError as ошибка:
        return None, f"не удалось прочитать {path}: {type(ошибка).__name__}"
    try:
        данные = yaml.safe_load(текст)
    except yaml.YAMLError as ошибка:
        mark = getattr(ошибка, "problem_mark", None)
        если_место = (
            f"строка {mark.line + 1}, колонка {mark.column + 1}"
            if mark is not None
            else "место в файле не определено"
        )
        return None, f"{path}: файл повреждён и не разбирается как YAML ({если_место})"
    return (данные or {}), None


def _check_bases_yaml(home: pathlib.Path) -> Check:
    путь = home / "bases.yaml"
    if not путь.exists():
        return Check("bases.yaml", "WARN", f"файла нет; выполните odata1c init --home {home}")
    данные, ошибка = _разобрать_yaml_доктор(путь)
    if ошибка is not None:
        return Check("bases.yaml", "FAIL", ошибка)
    # `bases:` без значения (шаблон поставки, ключ есть — строк под ним нет) разбирается как
    # `None`, а не как пустой словарь — тот же приём, что `config.loader._load_bases`
    # (`raw_bases = data.get("bases") or {}`): это не ошибка формата, а «баз нет».
    базы = (данные.get("bases") if isinstance(данные, dict) else None) or {}
    if not isinstance(базы, dict):
        return Check("bases.yaml", "FAIL", f"{путь}: раздел bases должен быть словарём")
    if not базы:
        return Check("bases.yaml", "WARN", "баз нет: опишите их в bases.yaml или odata1c base add")
    return Check("bases.yaml", "OK", f"разбирается, баз: {len(базы)}")


def _check_daemon_yaml(home: pathlib.Path) -> Check:
    путь = home / "daemon.yaml"
    if not путь.exists():
        return Check("daemon.yaml", "FAIL", f"файла нет; выполните odata1c init --home {home}")
    данные, ошибка = _разобрать_yaml_доктор(путь)
    if ошибка is not None:
        return Check("daemon.yaml", "FAIL", ошибка)
    секрет = данные.get("gate_secret") if isinstance(данные, dict) else None
    if not секрет:
        return Check("daemon.yaml", "FAIL", "секрет гейта (gate_secret) не найден")
    return Check("daemon.yaml", "OK", "разбирается, секрет на месте")


def _check_launcher_key(home: pathlib.Path) -> Check:
    if read_launcher_key(home) is not None:
        return Check("launcher.key", "OK", "есть")
    return Check(
        "launcher.key",
        "WARN",
        "нет: демон выдаст Claude Code второй вопрос (создаст odata1c init)",
    )


def _check_base(home: pathlib.Path, name: str, base: BaseConfig) -> Check:
    """Индекс базы, свежий ли (`IndexRepository.require_current_version`), и `check_policy` —
    та же проверка, что `cli.cmd_policy_check`, без печати. Открытый индекс здесь СВОЙ
    (`index_path`/`IndexRepository`), а не `cli._открыть_индекс_для_политики`: `cli` импортирует
    этот модуль для команды `doctor`, обратный импорт замкнул бы цикл."""
    detail_parts = [_хост(base.url)]

    путь_индекса = index_path(home, name)
    репозиторий: IndexRepository | None = None
    индекс_свежий = False
    if not путь_индекса.exists():
        detail_parts.append("индекса нет")
    else:
        репозиторий = IndexRepository(путь_индекса)
        try:
            репозиторий.require_current_version()
            индекс_свежий = True
        except IndexCorruptError as ошибка:
            detail_parts.append(f"индекс устарел: {ошибка.message}")
            репозиторий.close()
            репозиторий = None

    путь_политики = policy_path(home, name)
    находки = []
    сломана_политика = False
    if not путь_политики.exists():
        detail_parts.append("политика не создана")
    else:
        try:
            находки = check_policy(путь_политики, репозиторий)
        except PolicyError as ошибка:
            сломана_политика = True
            detail_parts.append(f"policy.yaml повреждена: {ошибка}")
        finally:
            if репозиторий is not None:
                репозиторий.close()
                репозиторий = None
    if репозиторий is not None:
        репозиторий.close()

    ошибки = [находка for находка in находки if находка.level == "error"]
    предупреждения = [находка for находка in находки if находка.level == "warning"]
    if ошибки:
        detail_parts.append(f"policy check: ошибок {len(ошибки)}")
    elif предупреждения:
        detail_parts.append(f"policy check: предупреждений {len(предупреждения)}")

    нет_политики = not путь_политики.exists()
    if сломана_политика or ошибки:
        статус: Status = "FAIL"
    elif not индекс_свежий or нет_политики or предупреждения:
        статус = "WARN"
    else:
        статус = "OK"
        detail_parts.append("индекс свежий, policy check без замечаний")

    return Check(f"база {name}", статус, "; ".join(detail_parts))


def _без_адреса(текст: str, base: BaseConfig) -> str:
    """Убрать из текста ошибки 1С полный адрес базы, если платформа или прокси перед ней повторили
    его целиком (проба P7: 1С эхом повторяет присланное в тексте ошибки в шести формах запроса из
    четырнадцати; страница веб-сервера/прокси перед 1С — тем более) — оставляя только схему и хост
    (design §8: «адресов … не печатает»). `OdataError.message` для сетевых ошибок и ошибок 1С не
    проходит через страж гейта (тот защищает только данные 1С, а не собственные диагностические
    сообщения `doctor`), поэтому чистка — обязанность этой функции, единственной между `Client1C`
    и `render()`."""
    хост = _хост(base.url)
    for форма in (base.url, base.url.rstrip("/")):
        текст = текст.replace(форма, хост)
    return текст


async def _check_connection(base: BaseConfig) -> Check:
    """`--online`: то же обращение, что `cli.cmd_base_test` (`Client1C.get_raw("$metadata", …)`),
    но результат — строка таблицы, а не печать. Конструктор `Client1C` тоже может поднять
    `OdataError` синхронно (сертификат не найден) — до входа в `try` тела запроса."""
    имя_строки = f"соединение с {base.name}"
    try:
        client = Client1C(base)
    except OdataError as ошибка:
        return Check(имя_строки, "FAIL", _без_адреса(f"[{ошибка.code}] {ошибка}", base))
    try:
        await client.get_raw("$metadata", accept="application/xml", add_format=False)
    except OdataError as ошибка:
        return Check(имя_строки, "FAIL", _без_адреса(f"[{ошибка.code}] {ошибка}", base))
    finally:
        await client.close()
    return Check(имя_строки, "OK", "base test прошёл")


async def _версия_демона(port: int) -> str:
    async with (
        httpx2.AsyncClient(timeout=httpx2.Timeout(ТАЙМАУТ_ОПРОСА_ДЕМОНА_S)) as http,
        streamable_http_client(daemon_url(port), http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        итог = await session.initialize()
        return итог.server_info.version


def опросить_демон(port: int) -> str | None:
    """Версия демона, отвечающего на `port`, или `None` — не слушает, ответил не как MCP-сервер
    протокола `mcp`, либо не ответил за `ТАЙМАУТ_ОПРОСА_ДЕМОНА_S` (design §8: сбой — WARN «не
    запущен», не FAIL). `is_listening` первым — короткая проверка TCP-порта дешевле, чем ждать
    полный таймаут HTTP-подключения на пустом порту (обычный случай: демон владельца не поднят).

    Исключение здесь перехватывается широко (`Exception`), намеренно: назначение этой функции —
    сказать «демон недоступен», а не различать, ЧЕМ именно он недоступен (протокол не тот порт
    занял чужой процесс, сеть моргнула, ответ не разобрался) — во всех случаях таблица говорит
    одно и то же WARN, и отказ не должен обрывать остальные строки доктора."""
    if not is_listening(port, timeout=0.5):
        return None
    try:
        return asyncio.run(asyncio.wait_for(_версия_демона(port), timeout=ТАЙМАУТ_ОПРОСА_ДЕМОНА_S))
    except Exception:  # noqa: BLE001 — см. докстринг: любой сбой опроса здесь равнозначен WARN
        return None


def _check_daemon(port: int, connect: Callable[[int], str | None]) -> Check:
    версия = connect(port)
    if версия is None:
        return Check("демон", "WARN", f"не запущен на порту {port}")
    if версия != __version__:
        return Check(
            "демон",
            "FAIL",
            f"версия демона {версия} не совпадает с версией пакета {__version__}; "
            "после обновления выполните odata1c daemon stop",
        )
    return Check("демон", "OK", f"порт {port}, версия {версия}")


def run(
    home: pathlib.Path,
    *,
    online: bool,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    connect: Callable[[int], str | None] = опросить_демон,
) -> list[Check]:
    """Все десять строк таблицы (design §8) — см. докстринг модуля. Никогда не бросает: каждая
    проверка перехватывает свои ошибки сама; неожиданный сбой настроек (`ConfigError` от
    `load_config`, когда оба файла по отдельности разобрались, но их содержимое не проходит
    более глубокую проверку — неизвестная роль, битый URL и т.п.) превращается в отдельную
    строку `настройки`, а не прерывает построение таблицы."""
    checks: list[Check] = [_check_python(), _check_uv(which, run), _check_home(home)]

    config = None
    if home.is_dir():
        проверка_bases = _check_bases_yaml(home)
        проверка_daemon = _check_daemon_yaml(home)
        checks.append(проверка_bases)
        checks.append(проверка_daemon)
        if проверка_bases.status != "FAIL" and проверка_daemon.status != "FAIL":
            try:
                config = load_config(home)
            except ConfigError as ошибка:
                checks.append(Check("настройки", "FAIL", f"[{ошибка.code}] {ошибка}"))
        if config is not None:
            checks.append(_check_launcher_key(home))
            for name in sorted(config.bases):
                base = config.bases[name]
                checks.append(_check_base(home, name, base))
                if online:
                    checks.append(asyncio.run(_check_connection(base)))

    port = config.daemon.port if config is not None else DaemonConfig().port
    checks.append(_check_daemon(port, connect))
    checks.append(_check_claude(which, run))
    return checks
