"""Связка гейта с индексом и настройками: политика базы, словарь, классификатор.

SPEC §4.3, §6.9. Политика — два файла (ADR-0015): `policy.yaml` владельца (эту функцию не
трогает никогда) и `policy.auto.yaml` авторазметки, который целиком пересобирает `refresh_policy`.
"""

from __future__ import annotations

import base64
import pathlib

from odata1c.config.home import base_dir
from odata1c.config.models import BaseConfig
from odata1c.gate.dictionary import Dictionary
from odata1c.gate.field_rules import classify_field
from odata1c.gate.policy import (
    dump_auto,
    generate_policy,
    load_policy,
    read_auto,
    strip_auto_section,
)
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository

КЛАССЫ_НАЗВАНИЙ = ("org", "person")


def policy_path(home: pathlib.Path, base_name: str) -> pathlib.Path:
    return base_dir(home, base_name) / "policy.yaml"


def auto_policy_path(home: pathlib.Path, base_name: str) -> pathlib.Path:
    """Файл авторазметки (ADR-0015): `bases/<база>/policy.auto.yaml`, целиком пишет
    `refresh_policy`."""
    return base_dir(home, base_name) / "policy.auto.yaml"


def gate_db_path(home: pathlib.Path) -> pathlib.Path:
    return home / "gate.sqlite"


def open_dictionary(home: pathlib.Path, secret: str) -> Dictionary:
    return Dictionary(gate_db_path(home), base64.b64decode(secret))


def owner_names_for(home: pathlib.Path, base_name: str) -> set[str] | None:
    """Список `names_for` из файла владельца; нет файла или раздела — `None` (встроенный список,
    `field_rules.DEFAULT_NAMES_FOR`, ADR-0015: список — ручной раздел `bases/<база>/policy.yaml`,
    не поле `bases.yaml`)."""
    return load_policy(policy_path(home, base_name)).names_for()


def classifier_for(home: pathlib.Path, base: BaseConfig):
    """Функция-классификатор для reindex: подставляет список названий этой базы (SPEC §6.5)."""
    список = owner_names_for(home, base.name)

    def классификатор(entity: str, field: str, edm_type: str):
        return classify_field(entity, field, edm_type, names_for=список)

    return классификатор


def refresh_policy(home: pathlib.Path, base: BaseConfig) -> list[dict]:
    """Пересобрать авторазметку по индексу — `policy.auto.yaml`, файл владельца не трогая
    (ADR-0015, SPEC §4.3 п. 3): владелец правит только `policy.yaml`, а раздел `auto` и умолчания
    авторазметки живут рядом, в отдельном файле, который правит только эта функция.

    Первый вызов после обновления шлюза на базе со старым `policy.yaml`, где раздел `auto` ещё
    лежит в файле владельца, уносит его оттуда: `strip_auto_section` удаляет раздел, не трогая
    ручные и комментарии рядом. До этого момента раздел `auto` файла владельца участвует в
    сравнении «на проверку» как СТАРОЕ состояние — иначе первый реиндекс новой версии показал бы
    на проверку все поля классов org/person, которые были в auto и раньше.

    Правка ревью задачи 9 (Important), перенесённая в `_на_проверку`: «на проверку» возвращает
    только поля классов org/person, у которых класс в СТАРОЙ авторазметке (та, что лежала в файле
    до этого вызова) отличался от новой — не весь текущий auto целиком. Без сравнения список на
    боевой базе (сотни полей классов org/person) печатался бы заново при каждом реиндексе,
    меняющем состав сущностей, хотя для подавляющего большинства полей ничего не изменилось.

    Вызывать можно на каждом реиндексе, в том числе «без изменений» (находка П1): авторазметка
    зависит не только от индекса, но и от классификатора, а он меняется с версией шлюза. Файл при
    этом переписывается, только если меняется его содержимое; сборка авторазметки на индексе УТ
    из 7224 сущностей — около половины секунды."""
    путь_владельца = policy_path(home, base.name)
    путь_авто = auto_policy_path(home, base.name)
    путь_авто.parent.mkdir(parents=True, exist_ok=True)

    хранилище = IndexRepository(index_path(home, base.name))
    try:
        собранное = generate_policy(хранилище, names_for=owner_names_for(home, base.name))
    finally:
        хранилище.close()

    прежнее = read_auto(путь_авто)
    прежний_auto = прежнее.get("auto") or {}
    if not прежний_auto and путь_владельца.exists():
        # Первый реиндекс новой версии: файла авторазметки ещё нет (или он пуст), а раздел auto
        # ещё лежит в файле владельца — сравнение «на проверку» ведётся с ним, не с пустотой.
        прежний_auto = read_auto(путь_владельца).get("auto") or {}

    новое = {"defaults": собранное["defaults"], "auto": собранное["auto"]}
    # Файл переписывается, только если его СОДЕРЖИМОЕ меняется (находка П1 приёмки через
    # настоящие инструменты, 2026-09-12) — политику теперь пересобирает каждый реиндекс, а не
    # только перестроивший индекс (см. `ToolService.reindex`), и фоновая проверка `$metadata`
    # зовёт его раз в сутки.
    if прежнее != новое:
        dump_auto(путь_авто, defaults=новое["defaults"], auto=новое["auto"])
    if путь_владельца.exists():
        strip_auto_section(путь_владельца)

    return _на_проверку(прежний_auto, собранное["auto"])


def _на_проверку(старое: dict, новое: dict) -> list[dict]:
    """Поля классов org/person, чей класс в новой авторазметке отличается от того, что был в
    старой (см. `refresh_policy`): не весь текущий auto — иначе список на боевой базе печатался бы
    заново при каждом реиндексе, меняющем состав сущностей, хотя для большинства полей ничего не
    изменилось."""
    return [
        {"entity": ключ.rsplit(".", 1)[0], "field": ключ.rsplit(".", 1)[1], "sensitivity": класс}
        for ключ, класс in новое.items()
        if класс in КЛАССЫ_НАЗВАНИЙ and старое.get(ключ) != класс
    ]
