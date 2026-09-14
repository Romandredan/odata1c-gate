"""Командная строка odata1c (SPEC §3.5).

В этой задаче реализованы init, base list, base test, daemon и daemon stop; остальные команды
добавляются следующими задачами и планами.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import getpass
import importlib.resources
import pathlib
import sys
import time

import httpx2
import pydantic
from mcp.shared.exceptions import MCPError

from odata1c.client1c.client import Client1C
from odata1c.client1c.errors import OdataError
from odata1c.config.home import base_dir, ensure_home, resolve_home
from odata1c.config.importer import parse_env
from odata1c.config.loader import ConfigError, format_validation_error, load_config
from odata1c.config.models import ИМЯ_БАЗЫ, BaseConfig
from odata1c.config.writer import (
    append_base,
    ensure_gate_secret,
    ensure_launcher_key,
    ensure_policy_template,
    ensure_templates,
)
from odata1c.daemon import DaemonError, daemon_url, is_listening, serve, spawn_detached
from odata1c.daemon import остановить as остановить_демон
from odata1c.gate.dictionary import DictionaryBusyError, DictionaryCorruptError
from odata1c.gate.policy import PolicyError, load_policy, parse_owner_file
from odata1c.gate.policy_check import (
    БАЗОВЫЕ_КЛАССЫ,
    Finding,
    check_policy,
    render_effective,
    suggest_names,
)
from odata1c.gate.policy_edit import hide_entity, set_field_class
from odata1c.gate.service import (
    auto_policy_path,
    classifier_for,
    open_dictionary,
    policy_path,
    refresh_policy,
)
from odata1c.index.edmx import EdmxError
from odata1c.index.reindex import index_path, reindex
from odata1c.index.repository import IndexCorruptError, IndexRepository
from odata1c.launcher import run_launcher
from odata1c.registry.registry import Registry, SessionScope

# Ошибки старта лаунчера (odata1c mcp), которые cmd_mcp форматирует сама, в stderr — не через
# общий перехват main() (тот пишет в stdout, а stdout команды mcp — канал JSON-RPC клиента;
# раунд правок 1, находка 3б). ConfigError — свои настройки/имена баз; MCPError — демон ответил,
# но не как MCP (чужой процесс на порту, план M1d задача 6 раунд правок 1 находка 4);
# httpx2.HTTPError — адрес недостижим или без схемы; OSError/UnicodeError — сеть/кодировка.
_ОШИБКИ_ЗАПУСКА_MCP = (ConfigError, MCPError, httpx2.HTTPError, OSError, UnicodeError)

# Сколько ждать порт демона после spawn_detached (бриф задачи 5) — и в лаунчере (SPEC §2.1, п. 2),
# и в этой команде: обе стороны наблюдают один и тот же холодный старт (импорт lxml/ahocorasick,
# разбор daemon.yaml).
ОЖИДАНИЕ_ГОТОВНОСТИ_S = 15


def main(argv: list[str] | None = None) -> int:
    # --home общий для всех команд, в любой позиции: до подкоманды, между уровнями подкоманд
    # или после них. Наивное решение — добавить --home через parents=[...] на каждый уровень —
    # не работает: argparse разбирает хвост, доставшийся подпарсеру, в отдельное пространство
    # имён и затем ЦЕЛИКОМ копирует его поверх пространства имён родителя
    # (`_SubParsersAction.__call__`).
    # Если подпарсер не получил --home в своей части хвоста, он подставляет СВОЙ default (None)
    # и это None затирает уже распознанное родителем значение — независимо от того, что дальше
    # по цепочке использует parents. Проверено матрицей из шести форм записи --home
    # (tests/unit/test_cli_base.py::test_home_разбирается_в_любой_позиции).
    # Лечится default=argparse.SUPPRESS: тогда при отсутствии --home в конкретном хвосте
    # атрибут просто не появляется в подпространстве имён и копирование его не трогает.
    домашний = argparse.ArgumentParser(add_help=False)
    домашний.add_argument(
        "--home",
        default=argparse.SUPPRESS,
        help="домашний каталог шлюза (иначе ODATA1C_HOME или ~/.claude/odata1c)",
    )

    parser = argparse.ArgumentParser(
        prog="odata1c", description="Шлюз к OData 1С с гейтом", parents=[домашний]
    )
    команды = parser.add_subparsers(dest="команда", required=True)

    команды.add_parser(
        "init", help="создать домашний каталог и шаблоны настроек", parents=[домашний]
    )

    base = команды.add_parser("base", help="работа с базами", parents=[домашний])
    подкоманды = base.add_subparsers(dest="подкоманда", required=True)
    подкоманды.add_parser("list", help="список описанных баз", parents=[домашний])
    test = подкоманды.add_parser("test", help="проверить соединение с базой", parents=[домашний])
    test.add_argument("name", help="имя базы из bases.yaml")
    add = подкоманды.add_parser("add", help="добавить базу", parents=[домашний])
    add.add_argument("name", help="имя базы: строчные латинские буквы, цифры, подчёркивание")
    add.add_argument("--role", choices=("prod", "test", "dev"), default="prod")
    add.add_argument(
        "--recipes",
        choices=("ut", "bp", "zup"),
        help="скопировать шаблон рецептов для типовой конфигурации",
    )
    импорт = подкоманды.add_parser(
        "import", help="перенести базы из env-файла прежнего сервера", parents=[домашний]
    )
    импорт.add_argument("path", help="путь к 1c-odata.env")

    reindex_parser = команды.add_parser(
        "reindex", help="обновить индекс метаданных базы", parents=[домашний]
    )
    reindex_parser.add_argument("name", help="имя базы")
    reindex_parser.add_argument(
        "--force", action="store_true", help="перестроить, даже если $metadata не менялся"
    )

    policy = команды.add_parser("policy", help="политика гейта", parents=[домашний])
    policy_sub = policy.add_subparsers(dest="подкоманда", required=True)
    show = policy_sub.add_parser("show", help="показать политику базы", parents=[домашний])
    show.add_argument("name", help="имя базы")
    check = policy_sub.add_parser(
        "check", help="проверить файл владельца по индексу", parents=[домашний]
    )
    check.add_argument("name", help="имя базы")
    hide = policy_sub.add_parser(
        "hide", help="скрыть сущность целиком (с дочерними)", parents=[домашний]
    )
    hide.add_argument("name", help="имя базы")
    hide.add_argument("entity", help="имя сущности индекса")
    hide.add_argument(
        "--yes", action="store_true", help="не спрашивать подтверждение (для скриптов)"
    )
    policy_open = policy_sub.add_parser(
        "open", help="открыть поле (класс keep)", parents=[домашний]
    )
    policy_open.add_argument("name", help="имя базы")
    policy_open.add_argument("field", help="Сущность.Поле")
    policy_set = policy_sub.add_parser("set", help="назначить полю класс", parents=[домашний])
    policy_set.add_argument("name", help="имя базы")
    policy_set.add_argument("field", help="Сущность.Поле")
    policy_set.add_argument(
        "cls", metavar="класс", help="CLASSES (gate/tokens.py) | scan | custom:<имя>"
    )

    daemon_parser = команды.add_parser(
        "daemon", help="запустить MCP-демон (Streamable HTTP)", parents=[домашний]
    )
    daemon_parser.add_argument(
        "--foreground",
        action="store_true",
        help="работать в текущем процессе (без этого — порождает фоновый процесс и ждёт порт)",
    )
    daemon_подкоманды = daemon_parser.add_subparsers(dest="действие")
    daemon_подкоманды.add_parser("stop", help="остановить демон по daemon.pid", parents=[домашний])

    mcp_parser = команды.add_parser(
        "mcp",
        help="лаунчер: stdio-прокси демону (подключение к Claude Code)",
        parents=[домашний],
    )
    mcp_parser.add_argument(
        "--bases", help="видимые сессии базы через запятую (иначе видны все базы)"
    )
    mcp_parser.add_argument("--default", help="база по умолчанию для этой сессии")
    mcp_parser.add_argument(
        "--url", help="адрес демона явно (иначе daemon_url из порта daemon.yaml)"
    )

    reveal = команды.add_parser(
        "reveal", help="реальное значение токена (только для пользователя)", parents=[домашний]
    )
    reveal.add_argument("token", help="токен вида [[inn:M4T2Q9XZ7K]]")
    reveal.add_argument(
        "--base", help="имя базы — вместе с --field вернуть написание именно из неё"
    )
    reveal.add_argument(
        "--field", help="имя поля — вместе с --base вернуть написание именно из этого поля"
    )

    args = parser.parse_args(argv)
    home = resolve_home(getattr(args, "home", None))

    try:
        if args.команда == "init":
            return cmd_init(home)
        if args.команда == "base" and args.подкоманда == "list":
            return cmd_base_list(home)
        if args.команда == "base" and args.подкоманда == "test":
            return cmd_base_test(home, args.name)
        if args.команда == "base" and args.подкоманда == "add":
            return cmd_base_add(home, args.name, args.role, args.recipes)
        if args.команда == "base" and args.подкоманда == "import":
            return cmd_base_import(home, pathlib.Path(args.path))
        if args.команда == "reindex":
            return cmd_reindex(home, args.name, args.force)
        if args.команда == "policy" and args.подкоманда == "show":
            return cmd_policy_show(home, args.name)
        if args.команда == "policy" and args.подкоманда == "check":
            return cmd_policy_check(home, args.name)
        if args.команда == "policy" and args.подкоманда == "hide":
            return cmd_policy_hide(home, args.name, args.entity, args.yes)
        if args.команда == "policy" and args.подкоманда == "open":
            return cmd_policy_open(home, args.name, args.field)
        if args.команда == "policy" and args.подкоманда == "set":
            return cmd_policy_set(home, args.name, args.field, args.cls)
        if args.команда == "daemon" and getattr(args, "действие", None) == "stop":
            return cmd_daemon_stop(home)
        if args.команда == "daemon":
            return cmd_daemon(home, args.foreground)
        if args.команда == "mcp":
            return cmd_mcp(home, args.bases, args.default, args.url)
        if args.команда == "reveal":
            return cmd_reveal(home, args.token, base=args.base, field=args.field)
    except (
        ConfigError,
        OdataError,
        EdmxError,
        IndexCorruptError,
        PolicyError,
        DictionaryCorruptError,
        DictionaryBusyError,
        DaemonError,
    ) as ошибка:
        # ConfigError (настройки), OdataError (ответ 1С), EdmxError (не удалось разобрать
        # $metadata), IndexCorruptError (файл индекса повреждён — reindex открывает прежний
        # индекс перед перестройкой, см. правку по итогам ревью задачи 5), PolicyError
        # (policy.yaml повреждён или разобран неверно) и DictionaryCorruptError (файл словаря
        # гейта — не SQLite или повреждён; правка по итогам ревью задачи 9: раньше эти два
        # класса были объявлены с тем же протоколом code/hint, что и остальные, но не попадали
        # в общий перехват — команды policy show и reveal роняли голый traceback вместо
        # понятного сообщения) и DaemonError (план M1d, задача 5: порт демона уже занят) —
        # разные классы, но у всех есть code и hint, и str() на всех даёт человекочитаемое
        # сообщение (Exception.__init__ получает его же); одно место форматирования вместо
        # шести копий.
        print(f"[{ошибка.code}] {ошибка}")
        if ошибка.hint:
            print(f"подсказка: {ошибка.hint}")
        return 1
    return 2


def cmd_init(home: pathlib.Path) -> int:
    status = ensure_home(home)
    ensure_templates(home)
    # Секрет гейта создаётся здесь, а не при чтении настроек: чтение больше ничего не пишет
    # на диск. Вызов идемпотентен — если секрет уже есть (файл существовал и до этой команды),
    # он не меняется.
    ensure_gate_secret(home / "daemon.yaml")
    # Ключ лаунчера (Ruling 59): им лаунчер подписывает своего клиента для демона. Тоже
    # идемпотентно; ни ключ, ни его файл команда не печатает.
    ensure_launcher_key(home)
    print(f"домашний каталог: {home}")
    print(f"опишите базы в {home / 'bases.yaml'}")
    print("перенести базы из прежнего сервера: odata1c base import <путь к 1c-odata.env>")
    if status.warning:
        print(f"предупреждение: {status.warning}")
    return 0


def _печать_предупреждений(config) -> None:
    for предупреждение in config.warnings:
        print(f"предупреждение: {предупреждение}")


def cmd_base_list(home: pathlib.Path) -> int:
    config = load_config(home)
    _печать_предупреждений(config)
    registry = Registry(config)
    состояния = registry.visible(SessionScope())
    if not состояния:
        print(f"баз не описано; опишите их в {home / 'bases.yaml'}")
        print("или перенесите из прежнего сервера: odata1c base import <путь к 1c-odata.env>")
        return 0
    print(f"{'база':<16}{'роль':<8}{'гейт':<20}{'запись':<8}подпись")
    for состояние in состояния:
        запись = "да" if состояние.write else "нет"
        по_умолчанию = " (по умолчанию)" if состояние.name == config.default else ""
        print(
            f"{состояние.name:<16}{состояние.role:<8}{состояние.gate_mode:<20}"
            f"{запись:<8}{состояние.label}{по_умолчанию}"
        )
    return 0


def cmd_base_test(home: pathlib.Path, name: str) -> int:
    config = load_config(home)
    _печать_предупреждений(config)
    registry = Registry(config)
    base = registry.get(name, SessionScope())
    return asyncio.run(_проверить_соединение(base))


async def _проверить_соединение(base) -> int:
    # OdataError (сертификат не найден, отказ аутентификации, таймаут и т.д.) здесь
    # не перехватывается — единое место форматирования [code] сообщение + подсказка
    # в main() обрабатывает оба класса ошибок, ConfigError и OdataError.
    client = Client1C(base)
    try:
        данные = await client.get_raw("$metadata", accept="application/xml", add_format=False)
    finally:
        await client.close()
    print(
        f"база {base.name}: соединение установлено, роль {base.role}, "
        f"уровень гейта {base.gate.mode}"
    )
    print(
        f"$metadata получен: {len(данные) / 1024:.1f} КБ; "
        f"следующий шаг — odata1c reindex {base.name}"
    )
    return 0


def cmd_reindex(home: pathlib.Path, name: str, force: bool) -> int:
    config = load_config(home)
    base = Registry(config).get(name, SessionScope())
    return asyncio.run(_реиндекс(base, home, force))


async def _реиндекс(base: BaseConfig, home: pathlib.Path, force: bool) -> int:
    # OdataError (недоступный адрес, отказ аутентификации и т.д.) и EdmxError (не удалось
    # разобрать $metadata) здесь не перехватываются — единое место форматирования
    # [code] сообщение + подсказка в main() обрабатывает все три класса ошибок одинаково
    # (тот же приём, что в cmd_base_test._проверить_соединение).
    client = Client1C(base)
    try:
        результат = await reindex(
            base, client, home, force=force, classifier=classifier_for(home, base)
        )
    finally:
        await client.close()

    print(результат.message)
    # Файл владельца создаёт `base add`; здесь — запасной путь для баз, заведённых до задачи 3
    # (ADR-0015) или добавленных в обход CLI (`bases.yaml` вручную) — реиндекс не должен требовать
    # от владельца ручного создания `policy.yaml` перед первым запуском.
    if ensure_policy_template(home, base.name):
        print(f"создан файл политики владельца: {policy_path(home, base.name)}")
    # Политика — на каждом реиндексе, а не только перестроившем индекс (находка П1, тот же довод,
    # что у `ToolService.reindex`): раздел `auto` зависит и от классификатора, а тот меняется с
    # версией шлюза. Сообщение печатается, только если файл авторазметки действительно переписан
    # (ADR-0015: реиндекс пишет только `policy.auto.yaml`, файл владельца `policy.yaml` не трогает).
    путь_авторазметки = auto_policy_path(home, base.name)
    было = путь_авторазметки.read_bytes() if путь_авторазметки.exists() else None
    на_проверку = refresh_policy(home, base)
    if путь_авторазметки.read_bytes() != было:
        print(f"политика обновлена: {путь_авторазметки}")
        if на_проверку:
            print(f"поля классов org и person на проверку ({len(на_проверку)}):")
            for поле in на_проверку[:20]:
                print(f"  {поле['entity']}.{поле['field']} → {поле['sensitivity']}")
            print("ложное срабатывание переводится в keep в разделе fields политики")
    if результат.added_entities:
        print(
            f"добавлены сущности ({len(результат.added_entities)}): "
            f"{', '.join(результат.added_entities[:20])}"
        )
    if результат.removed_entities:
        print(
            f"удалены сущности ({len(результат.removed_entities)}): "
            f"{', '.join(результат.removed_entities[:20])}"
        )
    if результат.new_sensitive_fields:
        print(
            f"новые поля под защитой ({len(результат.new_sensitive_fields)}) — проверьте политику:"
        )
        for поле in результат.new_sensitive_fields[:20]:
            print(f"  {поле['entity']}.{поле['field']} → {поле['sensitivity']}")
    if результат.unresolved_entity_sets:
        print(
            f"наборы с испорченной ссылкой на тип ({len(результат.unresolved_entity_sets)}) — "
            f"это признак повреждённого $metadata, а не удалённых объектов:"
        )
        for имя in результат.unresolved_entity_sets[:20]:
            print(f"  {имя}")
    if результат.warnings:
        print(f"предупреждения разбора ({len(результат.warnings)}):")
        for предупреждение in результат.warnings[:20]:
            print(f"  {предупреждение}")
    return 0


def _открыть_индекс_для_политики(home: pathlib.Path, name: str) -> IndexRepository | None:
    """Открыть индекс базы для `policy show`/`policy check`, если файл уже есть — с проверкой
    версии разбора (тот же приём, что и `ToolService._open_index`): устаревший файл индекса не
    должен ронять команду сырым `sqlite3.OperationalError` вместо понятной диагностики.
    `IndexCorruptError` уходит наверх — общий перехват `main()` печатает код и подсказку. Нет
    файла — `None`, обе команды работают и без индекса (см. их докстринги)."""
    путь = index_path(home, name)
    if not путь.exists():
        return None
    репозиторий = IndexRepository(путь)
    try:
        репозиторий.require_current_version()
    except IndexCorruptError:
        репозиторий.close()
        raise
    return репозиторий


def cmd_policy_show(home: pathlib.Path, name: str) -> int:
    """`odata1c policy show <база>` (задача 4 плана M2b, ADR-0015): файл владельца как есть,
    плюс объединённый вид (владелец поверх авторазметки) с источником каждой строки —
    `render_effective` (`gate/policy_check.py`).

    Индекс открывается, только если файл индекса уже есть (`index_path`) — на свежей базе,
    ещё не прошедшей `reindex`, команда работает и без него, просто без счётчика дочерних
    у скрытых сущностей (см. докстринг `render_effective`); это НЕ отказ, в отличие от
    `resource_policy` — там нужна гарантия наследования запрета `hide` на дочерние объекты,
    здесь — только диагностика для владельца, ошибиться в счётчике не опасно."""
    config = load_config(home)
    base = Registry(config).get(name, SessionScope())
    путь = policy_path(home, base.name)
    if not путь.exists():
        print(f"политика ещё не создана; выполните: odata1c reindex {base.name}")
        return 1
    # Валидация тем же способом, что и остальной код (gate/policy.py::parse_owner_file/
    # load_policy), а не молчаливая печать сырого текста: испорченный YAML или раздел
    # неожиданного типа должны остановить команду понятной ошибкой, а не мусором на экране
    # (правка по итогам ревью задачи 9). PolicyError уходит наверх — форматирует общий перехват
    # в main() (код + подсказка).
    owner_data = parse_owner_file(путь)
    policy = load_policy(путь, auto_policy_path(home, base.name))
    репозиторий = _открыть_индекс_для_политики(home, base.name)
    try:
        # Полный набор скрытых (корни + поддерево) — обязанность вызывающего (находка 1 ревью
        # задачи 4, Important): `render_effective` больше не принимает индекс и не строит набор
        # сама. Без индекса поддерево неизвестно — это диагностика, не отказ (в отличие от
        # `resource_policy`, см. докстринг выше).
        корни = policy.hidden_entities()
        скрытые = корни | репозиторий.descendants(корни) if репозиторий is not None else корни
        текст = render_effective(
            путь.read_text(encoding="utf-8"), policy, owner_data, hidden=set(скрытые)
        )
    finally:
        if репозиторий is not None:
            репозиторий.close()
    print(f"# {путь}")
    print(текст)
    return 0


def cmd_policy_check(home: pathlib.Path, name: str) -> int:
    """`odata1c policy check <база>` (задача 4 плана M2b): находки `check_policy` —
    опечатки в именах сущностей и полей (по индексу, если он есть), неизвестные классы, свой
    класс без `fields`/`regex`, `keep` на сущности, которую владелец сам же скрыл. Код возврата
    1 только если есть хоть одна `error`; одни `warning` возврату не мешают."""
    config = load_config(home)
    base = Registry(config).get(name, SessionScope())
    путь = policy_path(home, base.name)
    if not путь.exists():
        print(f"политика ещё не создана; выполните: odata1c reindex {base.name}")
        return 1
    репозиторий = _открыть_индекс_для_политики(home, base.name)
    try:
        находки = check_policy(путь, репозиторий)
    finally:
        if репозиторий is not None:
            репозиторий.close()
    if not находки:
        print("замечаний нет")
        return 0
    _напечатать_находки(находки)
    return 1 if any(находка.level == "error" for находка in находки) else 0


def _напечатать_находки(находки: list[Finding]) -> None:
    """Одна строка на находку — тот же формат, что печатает `cmd_policy_check`; переиспользуют
    его и конструкторы `policy hide|open|set` (задача 5) после записи правила: показать владельцу
    сразу, не испортило ли новое правило остальную политику."""
    for находка in находки:
        подсказка = f" ({находка.hint})" if находка.hint else ""
        print(f"{находка.level}: {находка.where}: {находка.message}{подсказка}")


# --- policy hide | open | set (задача 5 плана M2b, ADR-0015) --------------------------------


def _похожие_сущности(repo: IndexRepository, query: str) -> str:
    похожие = suggest_names(repo, query)
    return f" (похожие имена: {', '.join(похожие)})" if похожие else ""


def _разобрать_ключ_поля(ключ: str) -> tuple[str, str] | None:
    """`Сущность.Поле` → (сущность, поле). Нет точки или одна из частей пуста (`.Поле`,
    `Сущность.`, `Сущность`) — `None`, неверный формат."""
    сущность, точка, поле = ключ.partition(".")
    if not точка or not сущность or not поле:
        return None
    return сущность, поле


def _канонизировать_сущность(repo: IndexRepository, entity: str) -> tuple[str | None, str]:
    """Сообщение отказа и каноническое имя сущности по индексу — `(None, каноническое)`, если
    сущность нашлась.

    Индекс здесь ОБЯЗАТЕЛЕН (находка I4 итогового ревью M2b): прежняя ветка `repo is None`
    возвращала имя дословно, и `policy hide`/`policy set` записывали в файл непроверенное имя,
    напечатав при этом «скрыто». Теперь оба конструктора отказывают раньше — см. `cmd_policy_hide`
    и `_cmd_policy_записать_класс`, — и звать эту функцию без индекса больше некому.

    `IndexRepository.resolve_name` нарочно нечувствителен к регистру («имя набора в чужом
    регистре… публикация 1С такой путь, по всей видимости, принимает», докстринг
    `resolve_name`): без канонизации перед записью правило, набранное `catalog_контрагенты`, легло
    бы в `policy.yaml` этим написанием, а `Policy.is_hidden`/`sensitivity_of` сравнивают ключ
    ТОЧНО — запрет молча не действовал бы, при этом `resolve_name` из `check_policy` его бы
    принял и находок не нашёл (находка ревью задачи 5: тот самый обход, о котором предупреждает
    докстринг `resolve_name`, конструктор мог бы сам и порождать)."""
    каноническое = repo.resolve_name(entity)
    if каноническое is None:
        return (
            f"сущность «{entity}» не найдена в индексе базы{_похожие_сущности(repo, entity)}",
            entity,
        )
    return None, каноническое


def _канонизировать_поле(repo: IndexRepository | None, ключ: str) -> tuple[str | None, str]:
    """Сообщение отказа и канонический ключ `Сущность.Поле` по индексу — `(None, ключ)`, если
    индекса нет вовсе (запись остаётся дословной) или и сущность, и поле нашлись. Имя поля
    сравнивается с индексом уже точно (`field_names`) — канонизации в отличие от сущности не
    требует, но входит в возвращаемый ключ, чтобы `set_field_class` получила ровно
    `<каноническая сущность>.<поле>`, а не смесь регистров.

    Форма ключа разбирается ПЕРВОЙ, до ветки «индекса нет» (находка I3 итогового ревью M2b): без
    точки правило мёртвое при любом состоянии индекса — `Policy.sensitivity_of` ищет ключ
    `Сущность.Поле` и `fields: {ДопИдентификатор: inn}` не найдёт никогда, — а прежний порядок
    веток такую запись на непроиндексированной базе принимал и печатал успех."""
    разбор = _разобрать_ключ_поля(ключ)
    if разбор is None:
        return (
            f"«{ключ}» не похоже на «Сущность.Поле»: имя поля пишется через точку, "
            f"например Catalog_Контрагенты.ИНН",
            ключ,
        )
    if repo is None:
        return None, ключ
    сущность, поле = разбор
    ошибка, каноническое = _канонизировать_сущность(repo, сущность)
    if ошибка:
        return ошибка, ключ
    if поле not in repo.field_names(каноническое):
        похожие = difflib.get_close_matches(поле, repo.field_names(каноническое), n=3)
        подсказка = f" (похожие поля: {', '.join(похожие)})" if похожие else ""
        return f"поле «{поле}» не найдено у сущности «{сущность}»{подсказка}", ключ
    return None, f"{каноническое}.{поле}"


def _проверить_класс(cls: str, owner_data: dict) -> str | None:
    """Сообщение отказа, если `cls` недопустим для `policy set`/`policy open`: не из
    `БАЗОВЫЕ_КЛАССЫ` (`gate/policy_check.py` — `CLASSES` из `gate/tokens.py` плюс `scan`, тот же
    набор, что и `policy check`, задача 4) и не объявленный `custom:<имя>` (раздел `custom` файла
    владельца, `parse_owner_file(path)["custom"]`) — `None`, если класс годится."""
    допустимые = БАЗОВЫЕ_КЛАССЫ | {f"custom:{имя}" for имя in (owner_data.get("custom") or {})}
    if cls in допустимые:
        return None
    перечень = ", ".join(sorted(БАЗОВЫЕ_КЛАССЫ))
    if cls.startswith("custom:"):
        return (
            f"класс «{cls}» не объявлен: нет раздела custom.{cls.split(':', 1)[1]} в файле "
            f"владельца; допустимые: {перечень}, custom:<имя из раздела custom>"
        )
    return f"класс «{cls}» неизвестен; допустимые: {перечень}, custom:<имя из раздела custom>"


def _отказ_без_индекса(base_name: str) -> None:
    """Отказ конструктора, когда индекса базы нет (находка I4 итогового ревью M2b, решение
    контроллера). Непроверенное имя, записанное в политику, — правило, которое никогда не
    сработает, а команда о том не сказала: `policy hide` печатал «скрыто», `policy set` — «класс
    поля …», и обе строки означали ровно ничего.

    Ошибиться в сторону ЗАКРЫТИЯ допустимо, в сторону открытия — нет, поэтому `policy open`
    (и `policy set … keep`) без индекса по-прежнему разрешён: лишнее правило `keep` на
    несуществующем поле никого не раскрывает, а мёртвый `hide`/`inn` оставляет данные открытыми,
    хотя владелец считает их закрытыми."""
    print(f"имя не проверено: индекса базы нет; сначала выполните odata1c reindex {base_name}")


def _путь_политики_или_отказ(home: pathlib.Path, base_name: str) -> pathlib.Path | None:
    путь = policy_path(home, base_name)
    if not путь.exists():
        print(f"политика ещё не создана; выполните: odata1c reindex {base_name}")
        return None
    return путь


def cmd_policy_hide(home: pathlib.Path, name: str, entity: str, yes: bool) -> int:
    """`odata1c policy hide <база> <сущность> [--yes]` (задача 5 плана M2b, ADR-0015): закрыть
    сущность целиком — конструктор поверх `hide_entity` (`gate/policy_edit.py`).

    Порядок: имя сущности проверяется по индексу и приводится к каноническому
    написанию (`_канонизировать_сущность` — иначе `catalog_контрагенты` легло бы в файл этим
    написанием и не совпало бы точным сравнением `Policy.is_hidden`, находка ревью); при промахе
    — отказ с подсказкой похожих имён и код 1, файл не трогается. Иначе печатается число и первые
    10 дочерних (`IndexRepository.descendants`, Ruling 30 — сам запрет распространяется на них
    при чтении политики, не здесь) и, без `--yes`, спрашивается подтверждение (`input()`; любой
    ответ, кроме `y`/`д`, — отказ без записи). После записи — находки `check_policy` (тем же
    форматом, что `policy check`) и строка итога.

    Без индекса команда ОТКАЗЫВАЕТ (находка I4 итогового ревью M2b, решение контроллера): имя
    сущности проверить нечем, а запрет по непроверенному имени — правило, которое никогда не
    сработает, при том что владелец после строки «скрыто: …» считает сущность закрытой. Прежде
    команда записывала такое имя и честно сообщала лишь, что число дочерних неизвестно."""
    config = load_config(home)
    base = Registry(config).get(name, SessionScope())
    путь = _путь_политики_или_отказ(home, base.name)
    if путь is None:
        return 1

    репозиторий = _открыть_индекс_для_политики(home, base.name)
    if репозиторий is None:
        _отказ_без_индекса(base.name)
        return 1
    try:
        ошибка, каноническое = _канонизировать_сущность(репозиторий, entity)
        if ошибка:
            print(ошибка)
            return 1

        дочерние = репозиторий.descendants({каноническое})
        перечень = ", ".join(sorted(дочерние)[:10])
        хвост = f" и ещё {len(дочерние) - 10}" if len(дочерние) > 10 else ""
        if дочерние:
            print(f"дочерних сущностей: {len(дочерние)} ({перечень}{хвост})")
        else:
            print("дочерних сущностей: 0")
        число_дочерних = str(len(дочерние))

        if not yes:
            ответ = input(f"скрыть {каноническое} и {число_дочерних} дочерних? [y/N] ")
            if ответ.strip().lower() not in ("y", "д"):
                print("отменено")
                return 1

        изменено = hide_entity(путь, каноническое)
        if not изменено:
            print(f"сущность «{каноническое}» уже скрыта")
            return 0

        находки = check_policy(путь, репозиторий)
        _напечатать_находки(находки)
        print(f"скрыто: {каноническое} и {число_дочерних} дочерних")
        return 0
    finally:
        репозиторий.close()


def _cmd_policy_записать_класс(
    home: pathlib.Path, name: str, field: str, cls: str, *, открытие: bool
) -> int:
    """Общая часть `policy open` (частный случай — класс всегда `keep`) и `policy set`
    (произвольный класс): проверка класса, проверка и канонизация поля по индексу
    (`_канонизировать_поле` — та же находка, что и у `cmd_policy_hide`: без приведения к
    каноническому написанию правило `Catalog_x.ИНН` не совпало бы с точным сравнением
    `Policy.sensitivity_of`), запись `set_field_class`, находки `check_policy`, строка итога.
    `открытие` меняет только последнюю строку — `открыто: <поле>` вместо
    `класс поля <поле>: <класс> (было: …)`.

    Порядок проверок без индекса важен (находки I3 и I4 итогового ревью M2b): сначала форма ключа
    (`ДопИдентификатор` без точки — отказ с указанием вида `Сущность.Поле` при любом состоянии
    индекса), затем отказ «имя не проверено» для всех классов, кроме `keep`."""
    config = load_config(home)
    base = Registry(config).get(name, SessionScope())
    путь = _путь_политики_или_отказ(home, base.name)
    if путь is None:
        return 1

    owner_data = parse_owner_file(путь)
    ошибка_класса = _проверить_класс(cls, owner_data)
    if ошибка_класса:
        print(ошибка_класса)
        return 1

    репозиторий = _открыть_индекс_для_политики(home, base.name)
    try:
        ошибка_поля, каноническое_поле = _канонизировать_поле(репозиторий, field)
        if ошибка_поля:
            print(ошибка_поля)
            return 1

        # Форма ключа проверена выше и без индекса (находка I3), а вот само ИМЯ без индекса
        # проверить нечем: закрывающий класс по непроверенному имени — мёртвое правило, из-за
        # которого владелец считает поле закрытым (находка I4). `keep` — единственное исключение:
        # ошибка в сторону закрытия допустима, в сторону открытия нет.
        if репозиторий is None and cls != "keep":
            _отказ_без_индекса(base.name)
            return 1

        прежнее = set_field_class(путь, каноническое_поле, cls)
        находки = check_policy(путь, репозиторий)
        _напечатать_находки(находки)
        if открытие:
            print(f"открыто: {каноническое_поле}")
        else:
            было = прежнее if прежнее is not None else "авторазметка"
            print(f"класс поля {каноническое_поле}: {cls} (было: {было})")
        return 0
    finally:
        if репозиторий is not None:
            репозиторий.close()


def cmd_policy_open(home: pathlib.Path, name: str, field: str) -> int:
    """`odata1c policy open <база> <Сущность.Поле>` (задача 5 плана M2b, ADR-0015): сократить
    `policy set ... keep` — конструктор поверх `set_field_class` (`gate/policy_edit.py`)."""
    return _cmd_policy_записать_класс(home, name, field, "keep", открытие=True)


def cmd_policy_set(home: pathlib.Path, name: str, field: str, cls: str) -> int:
    """`odata1c policy set <база> <Сущность.Поле> <класс>` (задача 5 плана M2b, ADR-0015):
    назначить полю произвольный класс — `CLASSES` (SPEC §6.4), `scan` или `custom:<имя>`
    (раздел `custom` файла владельца) — конструктор поверх `set_field_class`."""
    return _cmd_policy_записать_класс(home, name, field, cls, открытие=False)


def cmd_daemon(home: pathlib.Path, foreground: bool) -> int:
    """`odata1c daemon [--foreground]` (SPEC §2.2, бриф плана M1d задачи 5).

    `--foreground`: работать в текущем процессе — `asyncio.run(serve(...))` держит консоль,
    Ctrl+C отменяет корутину и даёт `serve()` остановить uvicorn штатно (`finally` внутри неё).
    Без `--foreground`: тот же `python -m odata1c daemon --foreground` отдельным процессом
    (`spawn_detached`), эта команда лишь ждёт готовность порта до 15 с и возвращает управление —
    сама она демон не держит, поэтому дважды `ensure_home`/`ensure_gate_secret` не проблема:
    и здесь (чтобы прочитать порт из daemon.yaml до того, как дочерний процесс его создаст на
    пустом домашнем каталоге), и внутри `serve()` — обе стороны идемпотентны.
    """
    ensure_home(home)
    ensure_gate_secret(home / "daemon.yaml")
    if foreground:
        asyncio.run(serve(home))
        return 0

    config = load_config(home)
    _печать_предупреждений(config)
    порт = config.daemon.port
    if is_listening(порт):
        print(f"демон уже слушает {daemon_url(порт)}")
        return 0

    spawn_detached(home, порт)
    предел = time.monotonic() + ОЖИДАНИЕ_ГОТОВНОСТИ_S
    while time.monotonic() < предел:
        if is_listening(порт):
            print(f"демон запущен: {daemon_url(порт)}")
            return 0
        time.sleep(0.2)
    print(
        f"демон не ответил на порту {порт} за {ОЖИДАНИЕ_ГОТОВНОСТИ_S} с — "
        f"проверьте журнал: {home / 'logs'} (daemon.log — сам демон, "
        f"daemon-launch.log — запуск через Планировщик заданий)"
    )
    return 1


def cmd_daemon_stop(home: pathlib.Path) -> int:
    """`odata1c daemon stop`.

    Отказ `stop()` бывает двух разных видов, и говорить о них одно и то же нельзя (ревью M1d,
    раунд 4, пункт 4): «pid-файла нет» — это «демон не запущен», а «файл на месте» — это «демон
    жив, снять его не удалось». Прежний текст объявлял вторым первое, то есть повторял ту же ложь,
    которую только что перестал говорить сам `stop()`, только с другой стороны.

    Раунд правок 2 по `stop()` и журналу, пункт 5: исходов отказа на деле три — файла нет, файл не
    разобран (пуст, мусор, не читается), процесс жив и не снят, — и прежний текст на пустом или
    мусорном файле объявлял процесс живым, хотя номера в файле нет. Причину теперь называет
    `daemon.остановить`, а команда печатает её сама, без отсылки в `daemon.log`: процесс команды
    журнал не настраивает, и туда ничего не попадало.
    """
    итог = остановить_демон(home)
    print(итог.причина)
    return 0 if итог.снят else 1


def _разобрать_bases(raw: str | None) -> list[str] | None:
    """`--bases` в список имён баз (SPEC §2.1, раунд правок 1, находка 6, Ruling 11).

    `None` — аргумент вообще не задан, видимость сессии не сужена (та же трактовка, что и у
    `daemon.scope_from_headers` для отсутствующего заголовка). Задан, но после разбора по запятой
    и обрезки пробелов не осталось ни одного имени (`--bases ""`, `--bases ","`, `--bases " "`) —
    это ОШИБКА запуска, а не «видно всё» (молчаливое расширение) и не «видно ничего» (молчаливое
    сужение до пустоты): пользователь, написавший `--bases`, явно хотел сузить видимость, и обе
    молчаливые трактовки одинаково опасны. Каждое распознанное имя дополнительно проверяется
    правилом `ИМЯ_БАЗЫ` (`config/models.py`) — до любого сетевого обращения: не-ASCII или иначе
    неверное имя (например, случайно продиктованное голосом) валится `UnicodeEncodeError` внутри
    конструктора `httpx2.AsyncClient`, а не понятной ошибкой (находка 4, проявление 3)."""
    if raw is None:
        return None
    имена = [имя.strip() for имя in raw.split(",") if имя.strip()]
    if not имена:
        raise ConfigError(
            f"--bases задан, но не содержит ни одного имени базы: {raw!r}",
            hint="перечислите имена через запятую (odata1c mcp --bases ut,buh) или уберите "
            "--bases совсем, чтобы видеть все базы",
        )
    for имя in имена:
        _проверить_имя_базы(имя, "--bases")
    return имена


def _проверить_имя_базы(имя: str, откуда: str) -> None:
    if not ИМЯ_БАЗЫ.match(имя):
        raise ConfigError(
            f"{откуда}: «{имя}» не похоже на имя базы",
            hint="имя базы — строчные латинские буквы, цифры и подчёркивание, до 32 символов",
        )


def _печать_ошибки_mcp(ошибка: Exception) -> None:
    """Одна строка в stderr — не в stdout (раунд правок 1, находка 3б): stdout команды `mcp` —
    канал JSON-RPC клиента, человеческий текст там для клиента не сообщение, а протокольный шум."""
    if isinstance(ошибка, ConfigError):
        print(f"[{ошибка.code}] {ошибка}", file=sys.stderr)
        if ошибка.hint:
            print(f"подсказка: {ошибка.hint}", file=sys.stderr)
        return
    print(f"не удалось подключиться к демону 1С-шлюза: {ошибка}", file=sys.stderr)


def cmd_mcp(home: pathlib.Path, bases: str | None, default: str | None, url: str | None) -> int:
    """`odata1c mcp [--bases a,b] [--default a] [--url URL]` (SPEC §2.2, план M1d задача 6).

    Держит stdio, пока клиент (Claude Code) не отключится — обычный код 0 по завершении. Все
    ошибки этой команды — разбора `--bases`/`--default`, отказа поднять свой демон
    (`SystemExit` из `launcher.py::_дождаться_демона`), обрыва при подключении к чужому процессу
    на порту демона, `--url` без схемы — перехватываются ЗДЕСЬ и печатаются в stderr
    (`_печать_ошибки_mcp`), а не общим перехватом `main()`, который пишет в stdout (раунд правок 1,
    находки 3б, 4).
    """
    try:
        список_баз = _разобрать_bases(bases)
        if default is not None:
            _проверить_имя_базы(default, "--default")
        asyncio.run(run_launcher(home, bases=список_баз, default=default, url=url))
    except SystemExit as выход:
        # launcher.py::_дождаться_демона уже напечатал причину в stderr — этот код возврата и
        # есть отказ команды, повторно печатать нечего.
        return выход.code if isinstance(выход.code, int) else 1
    except _ОШИБКИ_ЗАПУСКА_MCP as ошибка:
        _печать_ошибки_mcp(ошибка)
        return 1
    return 0


def cmd_reveal(
    home: pathlib.Path, token: str, *, base: str | None = None, field: str | None = None
) -> int:
    """Раскрытие токена только локально: наружу реальное значение не выходит (SPEC §14.5).

    Без --base/--field показывается любое сохранённое исходное написание токена (SPEC §6.3) —
    точное, как оно встретилось в данных 1С, а не нормализованная форма из tokens (нижний
    регистр, выпрямленные кавычки) — правка по итогам ревью задачи 9: раньше эта ветка
    показывала именно нормализованную форму, что расходится со SPEC §6.3 («написание с
    исходным регистром не теряется»). Нормализованное значение остаётся запасным вариантом —
    только когда у токена вообще нет ни одного сохранённого варианта написания. С --base и
    --field вместе — точное написание именно из указанной базы и поля.
    """
    config = load_config(home)
    словарь = open_dictionary(home, config.daemon.gate_secret)
    try:
        if base and field:
            значение = словарь.reveal(token, base=base, field=field)
        else:
            значение = словарь.any_variant(token)
            if значение is None:
                значение = словарь.reveal(token)
    finally:
        словарь.close()
    if значение is None:
        print(f"[token_unknown] токен {token} не найден в словаре")
        return 1
    print(значение)
    return 0


def cmd_base_add(home: pathlib.Path, name: str, role: str, recipes: str | None) -> int:
    cmd_init(home)
    config = load_config(home)
    _печать_предупреждений(config)
    if name in config.bases:
        raise ConfigError(
            f"база «{name}» уже описана в bases.yaml",
            hint="поправьте существующую запись вручную или выберите другое имя",
        )
    print(f"добавляю базу «{name}» с ролью {role}")
    url = input("адрес (оканчивается на /odata/standard.odata/): ").strip()
    values = {
        "label": input("подпись для модели: ").strip() or name,
        "url": url,
        "user": input("пользователь 1С: ").strip(),
        "password": getpass.getpass("пароль 1С (не отображается): "),
        "role": role,
    }
    try:
        BaseConfig(name=name, **values)  # проверка имени и адреса до записи в файл
    except pydantic.ValidationError as ошибка:
        raise ConfigError(
            f"база «{name}» описана неверно: {format_validation_error(ошибка)}"
        ) from ошибка
    append_base(home / "bases.yaml", name, values)
    print(f"база «{name}» дописана в {home / 'bases.yaml'}")
    if ensure_policy_template(home, name):
        print(f"создан файл политики владельца: {policy_path(home, name)}")
    if recipes:
        _скопировать_рецепты(home, name, recipes)
    print(f"проверить соединение: odata1c base test {name}")
    return 0


def cmd_base_import(home: pathlib.Path, path: pathlib.Path) -> int:
    if not path.exists():
        print(f"файл не найден: {path}")
        return 1
    cmd_init(home)
    текст = _прочитать_env(path)
    по_умолчанию, базы = parse_env(текст)
    if not базы:
        print(f"в {path} не нашлось ключей ODATA_DB_<ИМЯ>_BASE_URL")
        return 1
    config = load_config(home)
    _печать_предупреждений(config)
    существующие = set(config.bases)
    добавлено = 0
    for запись in базы:
        имя = запись.pop("name")
        переименовано_из = запись.pop("renamed_from", None)
        if переименовано_из:
            print(
                f"предупреждение: имя базы «{переименовано_из}» после обрезки до 32 символов "
                f"совпало с уже перенесённой базой, использую «{имя}»"
            )
        if имя in существующие:
            print(f"база «{имя}» уже описана, пропускаю")
            continue
        append_base(home / "bases.yaml", имя, запись)
        добавлено += 1
        print(f"перенесена база «{имя}»: {_без_учётных_данных(запись['url'])}")
    if по_умолчанию and добавлено:
        _записать_базу_по_умолчанию(home / "bases.yaml", по_умолчанию)
    print(f"перенесено баз: {добавлено}; проверьте: odata1c base list")
    return 0


def _прочитать_env(path: pathlib.Path) -> str:
    """Файл окружения прежнего сервера мог остаться в кодировке Windows (cp1251) — так его
    писали старые версии на локализованной Windows; текущий сервер и большинство редакторов
    пишут utf-8. Пробуем оба варианта по очереди вместо того, чтобы падать необработанным
    UnicodeDecodeError на первой же кириллической подписи базы."""
    сырые = path.read_bytes()
    for кодировка in ("utf-8", "cp1251"):
        try:
            return сырые.decode(кодировка)
        except UnicodeDecodeError:
            continue
    raise ConfigError(
        f"не удалось определить кодировку файла {path}: это не utf-8 и не cp1251",
        hint="сохраните файл в кодировке utf-8 и повторите перенос",
    )


def _без_учётных_данных(url: str) -> str:
    """Убрать user:password@ из адреса перед печатью. Прежний сервер иногда хранил их прямо
    в URL (https://имя:пароль@сервер/...) — переносим значение в bases.yaml как есть, но
    в консоль такое печатать нельзя."""
    if "://" not in url:
        return url
    схема, _, остаток = url.partition("://")
    конец_адреса = остаток.find("/")
    адрес = остаток[:конец_адреса] if конец_адреса != -1 else остаток
    хвост = остаток[конец_адреса:] if конец_адреса != -1 else ""
    if "@" not in адрес:
        return url
    _, _, узел = адрес.rpartition("@")
    return f"{схема}://{узел}{хвост}"


def _записать_базу_по_умолчанию(path: pathlib.Path, name: str) -> None:
    текст = path.read_text(encoding="utf-8")
    if текст.lstrip().startswith("default:") or "\ndefault:" in текст:
        return
    path.write_text(f"default: {name}\n{текст}", encoding="utf-8")


def _скопировать_рецепты(home: pathlib.Path, name: str, шаблон: str) -> None:
    источник = importlib.resources.files("odata1c.templates.recipes").joinpath(f"{шаблон}.yaml")
    назначение = base_dir(home, name) / "recipes.yaml"
    назначение.parent.mkdir(parents=True, exist_ok=True)
    if назначение.exists():
        print(f"рецепты уже есть: {назначение}, не трогаю")
        return
    назначение.write_text(источник.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"скопированы рецепты {шаблон}: {назначение}")
