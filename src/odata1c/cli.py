"""Командная строка odata1c (SPEC §3.5).

В этой задаче реализованы init, base list и base test; остальные команды добавляются
следующими задачами и планами.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.resources
import pathlib

from odata1c.client1c.client import Client1C
from odata1c.client1c.errors import OdataError
from odata1c.config.home import ensure_home, resolve_home
from odata1c.config.importer import parse_env
from odata1c.config.loader import ConfigError, load_config
from odata1c.config.models import BaseConfig
from odata1c.config.writer import append_base
from odata1c.registry.registry import Registry, SessionScope

ШАБЛОНЫ = {"bases.yaml": "bases.example.yaml", "daemon.yaml": "daemon.example.yaml"}


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
    except ConfigError as ошибка:
        print(f"[{ошибка.code}] {ошибка}")
        if ошибка.hint:
            print(f"подсказка: {ошибка.hint}")
        return 1
    return 2


def cmd_init(home: pathlib.Path) -> int:
    status = ensure_home(home)
    for имя_файла, имя_шаблона in ШАБЛОНЫ.items():
        назначение = home / имя_файла
        if назначение.exists():
            continue
        шаблон = importlib.resources.files("odata1c.templates").joinpath(имя_шаблона)
        назначение.write_text(шаблон.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"домашний каталог: {home}")
    print(f"опишите базы в {home / 'bases.yaml'}")
    print("перенести базы из прежнего сервера: odata1c base import <путь к 1c-odata.env>")
    if status.warning:
        print(f"предупреждение: {status.warning}")
    return 0


def cmd_base_list(home: pathlib.Path) -> int:
    config = load_config(home)
    for предупреждение in config.warnings:
        print(f"предупреждение: {предупреждение}")
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
    registry = Registry(config)
    base = registry.get(name, SessionScope())
    return asyncio.run(_проверить_соединение(base))


async def _проверить_соединение(base) -> int:
    client = Client1C(base)
    try:
        данные = await client.get_raw("$metadata", accept="application/xml", add_format=False)
    except OdataError as ошибка:
        print(f"[{ошибка.code}] {ошибка.message}")
        if ошибка.hint:
            print(f"подсказка: {ошибка.hint}")
        return 1
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


def cmd_base_add(home: pathlib.Path, name: str, role: str, recipes: str | None) -> int:
    cmd_init(home)
    print(f"добавляю базу «{name}» с ролью {role}")
    url = input("адрес (оканчивается на /odata/standard.odata/): ").strip()
    values = {
        "label": input("подпись для модели: ").strip() or name,
        "url": url,
        "user": input("пользователь 1С: ").strip(),
        "password": getpass.getpass("пароль 1С (не отображается): "),
        "role": role,
    }
    BaseConfig(name=name, **values)  # проверка имени и адреса до записи в файл
    append_base(home / "bases.yaml", name, values)
    print(f"база «{name}» дописана в {home / 'bases.yaml'}")
    if recipes:
        _скопировать_рецепты(home, name, recipes)
    print(f"проверить соединение: odata1c base test {name}")
    return 0


def cmd_base_import(home: pathlib.Path, path: pathlib.Path) -> int:
    if not path.exists():
        print(f"файл не найден: {path}")
        return 1
    cmd_init(home)
    по_умолчанию, базы = parse_env(path.read_text(encoding="utf-8"))
    if not базы:
        print(f"в {path} не нашлось ключей ODATA_DB_<ИМЯ>_BASE_URL")
        return 1
    существующие = set((load_config(home)).bases)
    добавлено = 0
    for запись in базы:
        имя = запись.pop("name")
        if имя in существующие:
            print(f"база «{имя}» уже описана, пропускаю")
            continue
        append_base(home / "bases.yaml", имя, запись)
        добавлено += 1
        print(f"перенесена база «{имя}»: {запись['url']}")
    if по_умолчанию and добавлено:
        _записать_базу_по_умолчанию(home / "bases.yaml", по_умолчанию)
    print(f"перенесено баз: {добавлено}; проверьте: odata1c base list")
    return 0


def _записать_базу_по_умолчанию(path: pathlib.Path, name: str) -> None:
    текст = path.read_text(encoding="utf-8")
    if текст.lstrip().startswith("default:") or "\ndefault:" in текст:
        return
    path.write_text(f"default: {name}\n{текст}", encoding="utf-8")


def _скопировать_рецепты(home: pathlib.Path, name: str, шаблон: str) -> None:
    источник = importlib.resources.files("odata1c.templates.recipes").joinpath(f"{шаблон}.yaml")
    назначение = home / "bases" / name / "recipes.yaml"
    назначение.parent.mkdir(parents=True, exist_ok=True)
    if назначение.exists():
        print(f"рецепты уже есть: {назначение}, не трогаю")
        return
    назначение.write_text(источник.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"скопированы рецепты {шаблон}: {назначение}")
