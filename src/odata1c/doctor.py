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
    же убирает путь, которого `_без_учётных_данных` не трогает).

    `.port` у `urlsplit` бросает `ValueError`, если порт в адресе не число (ревью раунда 1,
    находка M-3, п. 2 — `BaseConfig._проверить_url` проверяет только окончание строки, нечисловой
    порт в неё проходит) — перехватываем здесь же и отдаём безопасный запасной текст вместо того,
    чтобы уронить `run()` целиком на одной кривой записи `bases.yaml`."""
    части = urllib.parse.urlsplit(url)
    try:
        порт = f":{части.port}" if части.port else ""
    except ValueError:
        return "адрес базы не разбирается (порт не число)"
    хост = части.hostname or ""
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
    # Шаг 3 брифа требует check_file_permissions и на дом, и на сам bases.yaml (m-1 ревью раунда
    # 1) — файл с паролями 1С открытым текстом, права на него важнее прав на пустой каталог вокруг.
    предупреждение_прав = check_file_permissions(путь)
    данные, ошибка = _разобрать_yaml_доктор(путь)
    if ошибка is not None:
        деталь = ошибка if предупреждение_прав is None else f"{ошибка}; {предупреждение_прав}"
        return Check("bases.yaml", "FAIL", деталь)
    # `bases:` без значения (шаблон поставки, ключ есть — строк под ним нет) разбирается как
    # `None`, а не как пустой словарь — тот же приём, что `config.loader._load_bases`
    # (`raw_bases = data.get("bases") or {}`): это не ошибка формата, а «баз нет».
    базы = (данные.get("bases") if isinstance(данные, dict) else None) or {}
    if not isinstance(базы, dict):
        деталь = f"{путь}: раздел bases должен быть словарём"
        if предупреждение_прав:
            деталь = f"{деталь}; {предупреждение_прав}"
        return Check("bases.yaml", "FAIL", деталь)
    if предупреждение_прав:
        return Check("bases.yaml", "WARN", предупреждение_прав)
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
    этот модуль для команды `doctor`, обратный импорт замкнул бы цикл.

    `IndexRepository(..., read_only=True)` (ревью раунда 1, M-2/Ruling 75) — `doctor` только
    смотрит на индекс, не работает с ним, и не должен ни разу написать в файл владельца.
    Конструктор — ВНУТРИ `try` (ревью раунда 1, M-3 п. 1): он сам поднимает `IndexCorruptError`
    на файле, который вообще не открывается как SQLite (был снаружи `try`, и такой файл ронял
    `run()` целиком вместо одной строки WARN)."""
    detail_parts = [_хост(base.url)]

    путь_индекса = index_path(home, name)
    репозиторий: IndexRepository | None = None
    индекс_свежий = False
    if not путь_индекса.exists():
        detail_parts.append("индекса нет")
    else:
        try:
            репозиторий = IndexRepository(путь_индекса, read_only=True)
            репозиторий.require_current_version()
            индекс_свежий = True
        except IndexCorruptError as ошибка:
            detail_parts.append(f"индекс повреждён или устарел: {ошибка.message}")
            if репозиторий is not None:
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


def _класс_отказа(ошибка: OdataError) -> str:
    """Класс исхода `--online` (Ruling 74, замена находки M-1): текст ответа 1С или посредника
    перед ней никогда не попадает в `detail` — там бывают имя пользователя 1С (сообщение
    платформы про отказ доступа) или путь публикации (страница IIS/Apache под 1С, когда отвечает
    не сама 1С — `platform_error=False`), а `_без_адреса` их не ловила, потому что вырезала
    только `base.url` целиком, а не произвольный текст. Пять допустимых значений всей строки:
    `OK`, `отказ аутентификации (401|403)`, `HTTP <код>`, `сеть/таймаут`, `TLS` (последнее — только
    при отказе конструктора `Client1C`, см. `_check_connection`). Подробности владелец смотрит
    локально — `odata1c base test <имя>`, как у прежнего поведения `cmd_base_test` (страж на эту
    команду тоже не заведён — расхождение с design §8 «текст через страж» уже существовало и не в
    объёме этой задачи, только сужен риск в самом `doctor`, который агент видит через Bash в
    первую очередь)."""
    if ошибка.status in (401, 403):
        return f"отказ аутентификации ({ошибка.status})"
    if ошибка.status is not None:
        return f"HTTP {ошибка.status}"
    # status is None — сама 1С не ответила (сеть, таймаут, ошибка формирования запроса): у
    # OdataError.status его выставляет только map_error() на реальном HTTP-ответе (см. докстринг
    # выше). Код client1c/errors.py различает здесь "timeout" и "odata_error", но design §8 не
    # заводит для сети отдельного от таймаута класса — оба одинаково «сеть/таймаут» владельцу.
    return "сеть/таймаут"


async def _check_connection(base: BaseConfig) -> Check:
    """`--online`: то же обращение, что `cli.cmd_base_test` (`Client1C.get_raw("$metadata", …)`),
    но результат — строка таблицы с классом исхода (`_класс_отказа`), не печать текста ошибки.
    Конструктор `Client1C` тоже может поднять `OdataError` синхронно (сертификат не найден,
    единственный такой путь в клиенте) — до входа в `try` тела запроса, отдельным классом `TLS`."""
    имя_строки = f"соединение с {base.name}"
    try:
        client = Client1C(base)
    except OdataError:
        return Check(имя_строки, "FAIL", "TLS")
    try:
        await client.get_raw("$metadata", accept="application/xml", add_format=False)
    except OdataError as ошибка:
        return Check(имя_строки, "FAIL", _класс_отказа(ошибка))
    finally:
        await client.close()
    return Check(имя_строки, "OK", "")


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


def _порт_демона(home: pathlib.Path) -> int:
    """Порт для строки `демон` — независимо от того, разобрался ли `bases.yaml` (ревью раунда 1,
    m-2): битый `bases.yaml` не должен прятать непорядковый порт исправного `daemon.yaml` за
    умолчанием 7171 — `config` в этом случае остаётся `None`, а раньше порт брался только из
    него. Свой мелкий разбор, а не `_check_daemon_yaml`: та возвращает `Check` для печати, а не
    порт для дальнейшего использования, и незачем тянуть на себя её форматирование ошибок ради
    одного числа. Любая проблема (файла нет, не разобрался, поля нет, значение не число) — тихий
    откат на умолчание `DaemonConfig().port`: строка `daemon.yaml` уже сказала об этом отдельно."""
    данные, ошибка = _разобрать_yaml_доктор(home / "daemon.yaml")
    if ошибка is not None or not isinstance(данные, dict):
        return DaemonConfig().port
    порт = данные.get("port")
    return порт if isinstance(порт, int) else DaemonConfig().port


def _безопасно(имя: str, проверка: Callable[[], Check]) -> Check:
    """Общий перехват (ревью раунда 1, Ruling 76): любое исключение, не пойманное самой
    проверкой, — строка FAIL с ИМЕНЕМ КЛАССА исключения в `detail`, без текста самого исключения
    (текст может повторять значение из настроек или данных — тот же класс риска, что нашёлся в
    M-1). `run()` не должно падать ни при каких условиях; эта функция — единственное место,
    которое это гарантирует централизованно, а не разбросанными по каждой проверке try/except."""
    try:
        return проверка()
    except Exception as ошибка:  # noqa: BLE001 — намеренно: последняя линия защиты, см. докстринг
        return Check(имя, "FAIL", f"внутренняя ошибка проверки: {type(ошибка).__name__}")


def run(
    home: pathlib.Path,
    *,
    online: bool,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    connect: Callable[[int], str | None] = опросить_демон,
) -> list[Check]:
    """Все десять строк таблицы (design §8) — см. докстринг модуля. Только читает (Ruling 75):
    ни `ensure_home`, ни `ensure_gate_secret`, ни создание шаблонов, ни запись авторазметки —
    отсутствие файла или каталога само по себе становится строкой WARN/FAIL, а не поводом что-то
    создать. Никогда не бросает: `_безопасно` — общий перехват вокруг каждой проверки (Ruling 76);
    неожиданный сбой настроек (`ConfigError` от `load_config`, когда оба файла по отдельности
    разобрались, но их содержимое не проходит более глубокую проверку — неизвестная роль, битый
    URL и т.п.) превращается в отдельную строку `настройки`, а не прерывает построение таблицы."""
    checks: list[Check] = [
        _безопасно("Python", _check_python),
        _безопасно("uv", lambda: _check_uv(which, run)),
        _безопасно("дом шлюза", lambda: _check_home(home)),
    ]

    config = None
    if home.is_dir():
        проверка_bases = _безопасно("bases.yaml", lambda: _check_bases_yaml(home))
        проверка_daemon = _безопасно("daemon.yaml", lambda: _check_daemon_yaml(home))
        checks.append(проверка_bases)
        checks.append(проверка_daemon)
        # launcher.key не зависит от bases.yaml/daemon.yaml вовсе (читает свой файл напрямую) —
        # вне условия на config (ревью раунда 1, m-3): битый bases.yaml не должен прятать
        # диагностику ключа лаунчера; порядок строки в таблице — как в design §8.
        checks.append(_безопасно("launcher.key", lambda: _check_launcher_key(home)))
        if проверка_bases.status != "FAIL" and проверка_daemon.status != "FAIL":
            try:
                config = load_config(home)
            except ConfigError as ошибка:
                checks.append(Check("настройки", "FAIL", f"[{ошибка.code}] {ошибка}"))
            except Exception as ошибка:  # noqa: BLE001 — Ruling 76, а не только ConfigError:
                # load_config доходит и до `_из_keyring` (`password: keyring`), а тот может
                # поднять `keyring.errors.KeyringError` — класс, не ловящийся `except ConfigError`
                # выше и не обёрнутый `_безопасно` (этот вызов — единственный за пределами
                # `_check_*`-функций). Без этой ветки такой сбой уходил бы из run() наружу — то
                # самое M-3 через другую дверь (находка ревью до коммита раунда 1).
                текст_ошибки = f"внутренняя ошибка проверки: {type(ошибка).__name__}"
                checks.append(Check("настройки", "FAIL", текст_ошибки))
        if config is not None:
            for name in sorted(config.bases):
                base = config.bases[name]
                checks.append(
                    _безопасно(f"база {name}", lambda b=base, n=name: _check_base(home, n, b))
                )
                if online:
                    checks.append(
                        _безопасно(
                            f"соединение с {name}", lambda b=base: asyncio.run(_check_connection(b))
                        )
                    )

    port = config.daemon.port if config is not None else _порт_демона(home)
    checks.append(_безопасно("демон", lambda: _check_daemon(port, connect)))
    checks.append(_безопасно("Claude Code", lambda: _check_claude(which, run)))
    return checks
