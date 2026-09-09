"""Связка гейта с индексом и настройками: политика базы, словарь, классификатор.

SPEC §4.3, §6.9.
"""

from __future__ import annotations

import base64
import pathlib

import yaml

from odata1c.config.home import base_dir
from odata1c.config.models import BaseConfig
from odata1c.gate.dictionary import Dictionary
from odata1c.gate.field_rules import classify_field
from odata1c.gate.policy import generate_policy, merge_auto
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository

КЛАССЫ_НАЗВАНИЙ = ("org", "person")


def policy_path(home: pathlib.Path, base_name: str) -> pathlib.Path:
    return base_dir(home, base_name) / "policy.yaml"


def gate_db_path(home: pathlib.Path) -> pathlib.Path:
    return home / "gate.sqlite"


def open_dictionary(home: pathlib.Path, secret: str) -> Dictionary:
    return Dictionary(gate_db_path(home), base64.b64decode(secret))


def classifier_for(base: BaseConfig):
    """Функция-классификатор для reindex: подставляет список названий этой базы (SPEC §6.5)."""
    список = set(base.gate.names_for) if base.gate.names_for else None

    def классификатор(entity: str, field: str, edm_type: str):
        return classify_field(entity, field, edm_type, names_for=список)

    return классификатор


def refresh_policy(home: pathlib.Path, base: BaseConfig) -> list[dict]:
    """Пересобрать секцию auto по индексу, сохранив ручные разделы (SPEC §4.3 п. 3)."""
    путь = policy_path(home, base.name)
    путь.parent.mkdir(parents=True, exist_ok=True)

    хранилище = IndexRepository(index_path(home, base.name))
    try:
        список = set(base.gate.names_for) if base.gate.names_for else None
        собранное = generate_policy(хранилище, names_for=список)
    finally:
        хранилище.close()

    существующая = {}
    if путь.exists():
        существующая = yaml.safe_load(путь.read_text(encoding="utf-8")) or {}
    итог = merge_auto(существующая or собранное, собранное["auto"])
    итог.setdefault("version", 2)
    итог.setdefault("scan_free_text", True)
    итог.setdefault("defaults", собранное["defaults"])
    путь.write_text(_в_yaml(итог), encoding="utf-8")

    return [
        {"entity": ключ.rsplit(".", 1)[0], "field": ключ.rsplit(".", 1)[1], "sensitivity": класс}
        for ключ, класс in собранное["auto"].items()
        if класс in КЛАССЫ_НАЗВАНИЙ
    ]


def _в_yaml(данные: dict) -> str:
    шапка = (
        "# Политика гейта для базы. Приоритет разделов: fields > entities > defaults > auto.\n"
        "# Раздел auto перезаписывается при каждом реиндексе — правки вносите в fields.\n"
        "# Класс keep оставляет значение открытым; hide: true скрывает сущность от модели.\n"
    )
    return шапка + yaml.safe_dump(данные, allow_unicode=True, sort_keys=False)
