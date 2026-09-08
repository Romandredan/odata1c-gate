"""Обновление индекса: скачать $metadata, сверить контрольную сумму, перестроить, вернуть разницу.

Порядок из SPEC §4.3: сумма совпала и force не задан — «без изменений»; иначе индекс строится
во временном файле и заменяется атомарно, чтобы читающие сессии не увидели половину.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import os
import pathlib
from collections.abc import Callable

from odata1c.client1c.client import Client1C
from odata1c.config.home import base_dir
from odata1c.config.models import BaseConfig
from odata1c.index.edmx import ParsedMetadata, parse_edmx
from odata1c.index.repository import IndexRepository

Classifier = Callable[[str, str, str], tuple[str, str] | None]


@dataclasses.dataclass(slots=True)
class ReindexResult:
    changed: bool
    entity_count: int
    indexed_at: str
    message: str
    added_entities: list[str] = dataclasses.field(default_factory=list)
    removed_entities: list[str] = dataclasses.field(default_factory=list)
    added_fields: list[str] = dataclasses.field(default_factory=list)
    removed_fields: list[str] = dataclasses.field(default_factory=list)
    new_sensitive_fields: list[dict] = dataclasses.field(default_factory=list)
    # Наборы данных, чей тип не нашёлся среди EntityType (см. edmx.py) — признак испорченного
    # описания метаданных, а не удалённых объектов; показываем пользователю на всех ветках,
    # не только при перестройке (SPEC §4.3, требование задачи 5 «на что обратить внимание»).
    unresolved_entity_sets: list[str] = dataclasses.field(default_factory=list)


def index_path(home: pathlib.Path, base_name: str) -> pathlib.Path:
    return base_dir(home, base_name) / "metadata.sqlite"


async def reindex(
    base: BaseConfig,
    client: Client1C,
    home: pathlib.Path,
    *,
    force: bool = False,
    classifier: Classifier | None = None,
) -> ReindexResult:
    путь = index_path(home, base.name)
    путь.parent.mkdir(parents=True, exist_ok=True)

    сырой = await client.get_raw("$metadata", accept="application/xml", add_format=False)
    (путь.parent / "metadata.edmx").write_bytes(сырой)

    # Разбор EDMX — работа процессора на десятки мегабайт: уводим из событийного цикла (SPEC §2.2).
    разобрано = await asyncio.to_thread(parse_edmx, сырой)

    прежнее = _прежнее_состояние(путь)
    if not force and прежнее["sha256"] == разобрано.edmx_sha256:
        return ReindexResult(
            changed=False,
            entity_count=прежнее["count"],
            indexed_at=прежнее["indexed_at"] or "",
            message="без изменений: $metadata тот же",
            unresolved_entity_sets=прежнее["unresolved_entity_sets"],
        )

    временный = путь.with_suffix(".sqlite.new")
    _удалить_с_журналами(временный)
    новое = IndexRepository(временный)
    try:
        новое.write(разобрано)
        новые_классы = _классифицировать(новое, разобрано, classifier)
        момент = новое.meta("indexed_at") or datetime.datetime.now(datetime.UTC).isoformat()
        имена = новое.entity_names()
        поля = {(имя, поле) for имя in имена for поле in новое.field_names(имя)}
    finally:
        новое.close()

    _заменить(временный, путь)

    return ReindexResult(
        changed=True,
        entity_count=len(имена),
        indexed_at=момент,
        message=f"индекс обновлён: {len(имена)} сущностей",
        added_entities=sorted(имена - прежнее["entities"]),
        removed_entities=sorted(прежнее["entities"] - имена),
        added_fields=sorted(f"{сущность}.{поле}" for сущность, поле in поля - прежнее["fields"]),
        removed_fields=sorted(f"{сущность}.{поле}" for сущность, поле in прежнее["fields"] - поля),
        new_sensitive_fields=новые_классы,
        unresolved_entity_sets=sorted(разобрано.unresolved_entity_sets),
    )


def _классифицировать(
    хранилище: IndexRepository, разобрано: ParsedMetadata, classifier: Classifier | None
) -> list[dict]:
    """Классы полей вычисляет гейт (план M1c); здесь они только сохраняются (SPEC §4.3 п. 3)."""
    if classifier is None:
        return []
    новые: list[dict] = []
    for сущность in разобрано.entities:
        for поле in сущность.fields:
            решение = classifier(сущность.name, поле.name, поле.edm_type)
            if решение is None:
                continue
            класс, источник = решение
            хранилище.set_field_sensitivity(сущность.name, поле.name, класс, источник)
            новые.append(
                {
                    "entity": сущность.name,
                    "field": поле.name,
                    "sensitivity": класс,
                    "source": источник,
                }
            )
    return новые


def _прежнее_состояние(путь: pathlib.Path) -> dict:
    пусто = {
        "sha256": None,
        "count": 0,
        "indexed_at": None,
        "entities": set(),
        "fields": set(),
        "unresolved_entity_sets": [],
    }
    if not путь.exists():
        return пусто
    хранилище = IndexRepository(путь)
    try:
        имена = хранилище.entity_names()
        сырые_нераспознанные = хранилище.meta("unresolved_entity_sets")
        return {
            "sha256": хранилище.meta("edmx_sha256"),
            "count": int(хранилище.meta("entity_count") or 0),
            "indexed_at": хранилище.meta("indexed_at"),
            "entities": имена,
            "fields": {(имя, поле) for имя in имена for поле in хранилище.field_names(имя)},
            "unresolved_entity_sets": (
                json.loads(сырые_нераспознанные) if сырые_нераспознанные else []
            ),
        }
    finally:
        хранилище.close()


def _заменить(временный: pathlib.Path, целевой: pathlib.Path) -> None:
    _удалить_с_журналами(целевой)
    os.replace(временный, целевой)


def _удалить_с_журналами(путь: pathlib.Path) -> None:
    for суффикс in ("", "-wal", "-shm"):
        файл = pathlib.Path(str(путь) + суффикс)
        if файл.exists():
            файл.unlink()
