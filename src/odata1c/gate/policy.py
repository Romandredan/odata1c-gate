"""Политика гейта на базу: какие поля к какому классу, что скрыто, что открыто (SPEC §6.9).

Приоритет: fields > entities > defaults > auto. Секцию auto перезаписывает реиндекс,
ручные разделы не трогаются никогда.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

import yaml

from odata1c.gate.field_rules import classify_field

ОТКРЫТЫЕ_ПО_УМОЛЧАНИЮ = ("corr", "bic")


@dataclasses.dataclass(slots=True)
class Policy:
    scan_free_text: bool = True
    _defaults: dict = dataclasses.field(default_factory=dict)
    _entities: dict = dataclasses.field(default_factory=dict)
    _fields: dict = dataclasses.field(default_factory=dict)
    _auto: dict = dataclasses.field(default_factory=dict)
    _custom: dict = dataclasses.field(default_factory=dict)
    _names_for: list | None = None

    def sensitivity_of(self, entity: str, field: str) -> str | None:
        ключ = f"{entity}.{field}"
        значение = self._fields.get(ключ) or self._auto.get(ключ)
        if значение is None:
            return None
        if значение in ОТКРЫТЫЕ_ПО_УМОЛЧАНИЮ and self._defaults.get(значение) == "keep":
            return "keep"
        правило_адреса = self._defaults.get("addr")
        if значение == "addr" and isinstance(правило_адреса, dict):
            разрешено = правило_адреса.get("mask_for") or []
            return "addr" if entity in разрешено else "keep"
        return значение

    def is_hidden(self, entity: str) -> bool:
        настройки = self._entities.get(entity) or {}
        return bool(настройки.get("hide"))

    def names_for(self) -> set[str] | None:
        return set(self._names_for) if self._names_for is not None else None

    def custom_patterns(self) -> dict[str, re.Pattern]:
        собранное: dict[str, re.Pattern] = {}
        for имя, описание in self._custom.items():
            выражение = (описание or {}).get("regex")
            if выражение:
                собранное[f"custom:{имя}"] = re.compile(выражение)
        return собранное

    def custom_fields(self) -> dict[str, str]:
        """Имя поля → класс custom:*, из раздела custom политики."""
        собранное: dict[str, str] = {}
        for имя, описание in self._custom.items():
            for поле in (описание or {}).get("fields", []):
                собранное[поле] = f"custom:{имя}"
        return собранное


def load_policy(path: pathlib.Path) -> Policy:
    if not pathlib.Path(path).exists():
        return Policy()
    данные = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    return Policy(
        scan_free_text=bool(данные.get("scan_free_text", True)),
        _defaults=данные.get("defaults") or {},
        _entities=данные.get("entities") or {},
        _fields=данные.get("fields") or {},
        _auto=данные.get("auto") or {},
        _custom=данные.get("custom") or {},
        _names_for=данные.get("names_for"),
    )


def generate_policy(index, *, names_for: set[str] | None = None) -> dict:
    """Собрать секцию auto по индексу: классификация каждого строкового поля (SPEC §4.3 п. 3)."""
    авто: dict[str, str] = {}
    for имя_сущности in sorted(index.entity_names()):
        описание = index.describe(имя_сущности)
        if описание is None:
            continue
        for поле in описание.fields:
            решение = classify_field(
                имя_сущности, поле["name"], поле["edm_type"], names_for=names_for
            )
            if решение:
                авто[f"{имя_сущности}.{поле['name']}"] = решение[0]
    return {
        "version": 2,
        "scan_free_text": True,
        "defaults": {"corr": "keep", "bic": "keep"},
        "entities": {},
        "fields": {},
        "custom": {},
        "auto": авто,
    }


def merge_auto(existing: dict, generated_auto: dict) -> dict:
    результат = dict(existing)
    результат["auto"] = dict(generated_auto)
    return результат
