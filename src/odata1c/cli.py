"""Командная строка odata1c (SPEC §3.5).

В этой задаче реализованы init, base list и base test; остальные команды добавляются
следующими задачами и планами.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.resources
import pathlib
import sys

from odata1c.client1c.client import Client1C
from odata1c.client1c.errors import OdataError
from odata1c.config.home import ensure_home, resolve_home
from odata1c.config.loader import ConfigError, load_config
from odata1c.registry.registry import Registry, SessionScope

ШАБЛОНЫ = {"bases.yaml": "bases.example.yaml", "daemon.yaml": "daemon.example.yaml"}


def main(argv: list[str] | None = None) -> int:
    # --home общий для всех команд. argparse не пробрасывает опции родителя в подкоманду:
    # без parents=[домашний] значение после имени подкоманды («init --home X») не распознаётся,
    # поэтому опция добавлена явно на каждый уровень, где она может встретиться.
    домашний = argparse.ArgumentParser(add_help=False)
    домашний.add_argument(
        "--home", help="домашний каталог шлюза (иначе ODATA1C_HOME или ~/.claude/odata1c)"
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

    args = parser.parse_args(argv)
    home = resolve_home(args.home)

    try:
        if args.команда == "init":
            return cmd_init(home)
        if args.команда == "base" and args.подкоманда == "list":
            return cmd_base_list(home)
        if args.команда == "base" and args.подкоманда == "test":
            return cmd_base_test(home, args.name)
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


if __name__ == "__main__":
    sys.exit(main())
