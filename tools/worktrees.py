"""Обзор параллельных работ: рабочие копии git, их задачи и расхождение с веткой разработки.

Зачем: несколько сессий агентов ведут задачи каждая в своей рабочей копии
(`.claude/worktrees/<имя>`), и по одному `git worktree list` не видно, что где делается, насколько
ветка разошлась с `dev` и не взялись ли две задачи за один слой. Отчёт отвечает на это одной
командой; порядок работы, который он обслуживает, — раздел «Процесс разработки» в `AGENTS.md`.

Источник описания задачи — `git config branch.<ветка>.description`: конфигурация у рабочих копий
общая, поэтому описание, записанное одной сессией, видно всем. Слой — первый сегмент имени задачи
(`worktree-gate-guard-word-boundaries` → `gate`) из перечня `СЛОИ`.

Запуск: `uv run python tools/worktrees.py [--base origin/dev] [--fetch]`. По умолчанию сеть не
трогается и расхождение считается от последнего полученного `origin/dev`; `--fetch` сначала
обновляет его. Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import subprocess
import sys

ПРЕФИКС_КОПИИ = "worktree-"

# Слой задачи — первый сегмент её имени. Подпакеты `src/odata1c` названы как есть, кроме `tools`:
# слой тулов MCP — `mcp`, а `devtools` — каталог `tools/` репозитория, чтобы имена не совпадали.
СЛОИ: dict[str, str] = {
    "config": "src/odata1c/config — настройки, bases.yaml, daemon.yaml, политика",
    "registry": "src/odata1c/registry — реестр баз",
    "client1c": "src/odata1c/client1c — клиент OData 1С",
    "index": "src/odata1c/index — индекс метаданных",
    "gate": "src/odata1c/gate — гейт, словарь, страж",
    "write": "src/odata1c/write — запись: pending, commit, журнал, откат",
    "recipes": "src/odata1c/recipes и шаблоны рецептов",
    "mcp": "src/odata1c/tools — тулы MCP",
    "cli": "cli, launcher, daemon, doctor — командная строка и процессы",
    "plugin": "plugin/ — навыки, хук, агент, evals",
    "probes": "tools/probes и docs/probes — технические проверки и приёмки",
    "devtools": "tools/ — скрипты разработки",
    "docs": "документация без кода",
    "ci": ".github/workflows",
    "release": "выпуск версии",
}


@dataclasses.dataclass
class Копия:
    """Одна запись `git worktree list --porcelain`."""

    путь: str
    head: str = ""
    ветка: str | None = None
    locked: bool = False
    prunable: bool = False
    bare: bool = False


def разобрать_список(текст: str) -> list[Копия]:
    """Разбирает вывод `git worktree list --porcelain`: блоки через пустую строку."""
    копии: list[Копия] = []
    текущая: Копия | None = None
    for строка in текст.splitlines():
        if not строка.strip():
            текущая = None
            continue
        ключ, _, значение = строка.partition(" ")
        if ключ == "worktree":
            текущая = Копия(путь=значение)
            копии.append(текущая)
        elif текущая is None:
            continue
        elif ключ == "HEAD":
            текущая.head = значение
        elif ключ == "branch":
            текущая.ветка = значение.removeprefix("refs/heads/")
        elif ключ == "locked":
            текущая.locked = True
        elif ключ == "prunable":
            текущая.prunable = True
        elif ключ == "bare":
            текущая.bare = True
    return копии


def имя_задачи(ветка: str) -> str:
    """Имя задачи без префикса, который добавляет `EnterWorktree`."""
    return ветка.removeprefix(ПРЕФИКС_КОПИИ)


def слой(ветка: str | None) -> str | None:
    """Слой задачи по первому сегменту имени; не из перечня — `None`."""
    if not ветка:
        return None
    первый = имя_задачи(ветка).split("-", 1)[0]
    return первый if первый in СЛОИ else None


def повторы_слоёв(ветки: list[str]) -> dict[str, list[str]]:
    """Слои, за которые взялись две задачи и больше: слой → ветки."""
    по_слою: dict[str, list[str]] = {}
    for ветка in ветки:
        найденный = слой(ветка)
        if найденный:
            по_слою.setdefault(найденный, []).append(ветка)
    return {ключ: список for ключ, список in по_слою.items() if len(список) > 1}


def разобрать_расхождение(текст: str) -> tuple[int, int] | None:
    """`git rev-list --left-right --count база...ветка` → (отстаёт, опережает)."""
    части = текст.split()
    if len(части) != 2 or not all(часть.isdigit() for часть in части):
        return None
    return int(части[0]), int(части[1])


def _git(*аргументы: str, cwd: str | None = None) -> tuple[int, str]:
    результат = subprocess.run(
        ["git", *аргументы],
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return результат.returncode, результат.stdout.strip()


def _база(запрошенная: str | None) -> str:
    if запрошенная:
        return запрошенная
    код, _ = _git("rev-parse", "--verify", "--quiet", "origin/dev")
    return "origin/dev" if код == 0 else "dev"


def _блок(копия: Копия, база: str, основной: bool) -> list[str]:
    путь = pathlib.Path(копия.путь)
    ветка = копия.ветка
    заголовок = "основной каталог" if основной else путь.name
    строки = [f"{заголовок}  [{ветка or 'detached ' + копия.head[:7]}]"]

    признаки: list[str] = []
    if ветка and ветка != база:
        код, вывод = _git("rev-list", "--left-right", "--count", f"{база}...{ветка}")
        расхождение = разобрать_расхождение(вывод) if код == 0 else None
        if расхождение:
            отстаёт, опережает = расхождение
            признак = f"опережает {база} на {опережает}, отстаёт на {отстаёт}"
            if основной and отстаёт:
                признак += " — git pull --ff-only"
            признаки.append(признак)
    if not путь.is_dir():
        признаки.append("каталога нет (git worktree prune)")
    else:
        код, вывод = _git("status", "--porcelain", cwd=str(путь))
        правок = len(вывод.splitlines()) if код == 0 else None
        признаки.append(
            "незафиксированных правок нет"
            if правок == 0
            else f"незафиксированных файлов: {правок}"
            if правок
            else "состояние не прочитано"
        )
    if копия.locked:
        признаки.append("locked")
    if копия.prunable:
        признаки.append("prunable")
    найденный = слой(ветка)
    if ветка and not основной:
        признаки.append(f"слой {найденный}" if найденный else "слой не по соглашению")
    строки.append("  " + "; ".join(признаки))

    if ветка:
        _, последний = _git("log", "-1", "--date=format:%d.%m %H:%M", "--format=%cd  %s", ветка)
        if последний:
            строки.append(f"  последний коммит: {последний}")
        _, описание = _git("config", "--get", f"branch.{ветка}.description")
        if описание:
            строки.append(f"  задача: {' '.join(описание.split())}")
        elif not основной:
            строки.append(f'  задача: не описана — git config branch.{ветка}.description "…"')
    return строки


def main(argv: list[str] | None = None) -> int:
    разбор = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    разбор.add_argument("--base", help="с чем сравнивать ветки (умолчание — origin/dev, иначе dev)")
    разбор.add_argument("--fetch", action="store_true", help="сначала git fetch origin")
    аргументы = разбор.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if аргументы.fetch:
        код, _ = _git("fetch", "--quiet", "--prune", "origin")
        if код != 0:
            print("git fetch не удался — расхождение считается по последнему полученному состоянию")
    код, вывод = _git("worktree", "list", "--porcelain")
    if код != 0:
        print("git worktree list не удался: запускать из рабочей копии этого репозитория")
        return 1
    копии = [копия for копия in разобрать_список(вывод) if not копия.bare]
    база = _база(аргументы.base)

    print(f"Рабочих копий: {len(копии)}; расхождение — от {база}\n")
    for номер, копия in enumerate(копии):
        print("\n".join(_блок(копия, база, основной=номер == 0)))
        print()

    задачи = [копия.ветка for копия in копии[1:] if копия.ветка]
    for найденный, ветки in повторы_слоёв(задачи).items():
        print(f"Внимание: слой {найденный} занят несколькими задачами — {', '.join(ветки)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
