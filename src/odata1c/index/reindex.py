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
from odata1c.index.edmx import PARSER_VERSION, ParsedMetadata, parse_edmx
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
    # Предупреждения разбора (действие не привязано, имя виртуальной таблицы или перечисления
    # занято — см. edmx.py) — только на ветке перестройки, задача 3 плана M1b-fix.
    warnings: list[str] = dataclasses.field(default_factory=list)


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

    # Разбор EDMX — работа процессора на десятки мегабайт: уводим из событийного цикла (SPEC §2.2).
    разобрано = await asyncio.to_thread(parse_edmx, сырой)

    прежнее = _прежнее_состояние(путь)
    if (
        not force
        and прежнее["sha256"] == разобрано.edmx_sha256
        and прежнее["parser_version"] == PARSER_VERSION
    ):
        return ReindexResult(
            changed=False,
            entity_count=прежнее["count"],
            indexed_at=прежнее["indexed_at"] or "",
            message="без изменений: $metadata тот же",
            unresolved_entity_sets=прежнее["unresolved_entity_sets"],
        )

    # Сырое описание сохраняется только когда индекс действительно перестраивается (правка по
    # итогам ревью задачи 5, Important): на базе уровня ERP это 40+ МБ, а фоновая проверка хэша
    # идёт раз в reindex_check_hours на каждую базу (SPEC §4.3) — писать файл в ветке «без
    # изменений» значит платить полным объёмом диска за проверку, которая обычно ничего не находит.
    (путь.parent / "metadata.edmx").write_bytes(сырой)

    временный = путь.with_suffix(".sqlite.new")
    _удалить_с_журналами(временный)
    # Любое исключение при построении временного индекса (открытие файла, запись, подсчёт
    # разницы) не должно оставлять мусор на диске — временный файл и его журналы могли уже
    # получить часть данных к этому моменту (см. правку по итогам ревью задачи 5: обрыв
    # на этапе записи оставлял .sqlite.new и -wal/-shm до следующего обычного запуска).
    try:
        новое = IndexRepository(временный)
        try:
            новое.write(разобрано)
            новые_классы = _классифицировать(
                новое, разобрано, classifier, прежнее["field_sensitivity"]
            )
            момент = новое.meta("indexed_at") or datetime.datetime.now(datetime.UTC).isoformat()
            имена = новое.entity_names()
            поля = {(имя, поле) for имя in имена for поле in новое.field_names(имя)}
        finally:
            новое.close()
    except Exception:
        _удалить_с_журналами(временный)
        raise

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
        warnings=list(разобрано.warnings),
    )


def _классифицировать(
    хранилище: IndexRepository,
    разобрано: ParsedMetadata,
    classifier: Classifier | None,
    прежние_классы: dict[tuple[str, str], str | None],
) -> list[dict]:
    """Классы полей вычисляет гейт (план M1c); здесь они только сохраняются (SPEC §4.3 п. 3).

    Правка по итогам ревью задачи 5 (Important): класс проставляется в индекс на КАЖДОМ поле,
    которое классификатор узнал, — это не обсуждается, иначе поле останется без класса и уйдёт
    модели в открытом виде. В список «новые поля под защитой» попадают только те, у которых в
    прежнем индексе класса не было или он был другим (`прежние_классы`, из `_прежнее_состояние`,
    считана ДО перестройки — старый файл ещё цел). Без сверки два прогона подряд при пустой
    разнице оба возвращали одно и то же поле как новое: на базе уровня ERP это тысячи полей
    на каждый запуск, то есть сигнал уничтожен.
    """
    if classifier is None:
        return []
    новые: list[dict] = []
    for сущность in разобрано.entities:
        for поле in сущность.fields:
            решение = classifier(сущность.name, поле.name, поле.edm_type)
            if решение is None:
                continue
            класс, источник = решение
            if not хранилище.set_field_sensitivity(сущность.name, поле.name, класс, источник):
                # Не должно случаться: сущность и поле только что взяты из того же разбора,
                # который хранилище только что записало. Если всё же случилось — рассинхрон
                # между write() и классификацией, молчать нельзя (см. правку 6 того же ревью).
                raise RuntimeError(
                    f"класс поля не записан: {сущность.name}.{поле.name} — сущность или поле "
                    "не найдены в только что построенном индексе"
                )
            if прежние_классы.get((сущность.name, поле.name)) != класс:
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
        "field_sensitivity": {},
        "unresolved_entity_sets": [],
        "parser_version": None,
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
            "field_sensitivity": хранилище.field_sensitivities(),
            "fields": {(имя, поле) for имя in имена for поле in хранилище.field_names(имя)},
            "unresolved_entity_sets": (
                json.loads(сырые_нераспознанные) if сырые_нераспознанные else []
            ),
            # Ключ версии разбора мог не существовать вовсе (индекс построен до задачи 3
            # плана M1b-fix) — meta() тогда вернёт None, что не совпадёт с PARSER_VERSION и
            # заставит перестроить индекс без --force (см. условие ветки «без изменений» выше).
            "parser_version": хранилище.meta("parser_version"),
        }
    finally:
        хранилище.close()


def _заменить(временный: pathlib.Path, целевой: pathlib.Path) -> None:
    # Правка по итогам ревью задачи 5 (без удаления самого целевого файла): os.replace штатно
    # перезаписывает существующий файл атомарно — предварительное удаление не нужно и вредно.
    # Оно открывает окно, в котором индекса на диске нет вовсе: если сама подмена не пройдёт
    # (файл держит открытым читающая сессия или антивирус), прежний индекс уже уничтожен, хотя
    # os.replace мог бы отработать поверх него. Журналы (-wal, -shm) удаляются по-прежнему —
    # чужой журнал, прицепившийся к новой базе (тот же inode, устаревшее содержимое), реальная
    # проблема; сам файл базы в их числе больше нет.
    _удалить_журналы(целевой)
    os.replace(временный, целевой)


def _удалить_с_журналами(путь: pathlib.Path) -> None:
    файл = pathlib.Path(путь)
    if файл.exists():
        файл.unlink()
    _удалить_журналы(файл)


def _удалить_журналы(путь: pathlib.Path) -> None:
    for суффикс in ("-wal", "-shm"):
        файл = pathlib.Path(str(путь) + суффикс)
        if файл.exists():
            файл.unlink()
