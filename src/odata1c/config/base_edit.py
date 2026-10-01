"""`odata1c base set`: точечная правка записи базы в файле баз без потери комментариев (проект
`docs/superpowers/specs/2026-10-01-base-set-design.md`, §3.2; ADR-0017).

Файл правится текстом по разметке разбора: `yaml.compose` даёт номер строки каждого ключа, по ним
находится блок базы и строки её активных ключей; закомментированные образцы умолчаний
(`# write: false`, `#   mode: off`) находятся регулярным выражением внутри блока. Строки рисуют те
же помощники, что у `base add` (`config/writer.py::строка_записи`), поэтому файл после правки
выглядит так, как будто его написал `base add`: образцы остаются верными, комментарии владельца —
на месте. Почему не `ruamel.yaml`, как у конструктора политики: новый ключ он добавил бы в конец
записи, а закомментированный образец с прежним значением остался бы выше и врал; при смене роли
комментарии «умолчание роли prod» устаревали бы (проект, подход A).

Каждая правка — одна операция над списком строк между двумя разборами: вставка сдвигает номера
строк, и дешевле разобрать файл заново (`_правка`), чем вести сдвиги вручную. Перед записью новый
текст проверяется той же моделью настроек, что читает демон (`loader.parse_bases`); запись атомарна
(`<путь>.yaml.new` + `os.replace`), при любом отказе исходный файл не тронут."""

from __future__ import annotations

import dataclasses
import pathlib
import re
from collections.abc import Callable

import yaml

from odata1c.config.loader import ConfigError

ПО_РОЛИ = object()
"""Значение изменения «снять явное поле — действует умолчание роли» (`--<ключ> default`)."""

ПОЛЯ = (
    "label",
    "role",
    "write",
    "gate.mode",
    "permissions.post_documents",
    "permissions.mark_deletion",
    "permissions.independent_register_delete",
    "permissions.commit_limit",
)
_РОДИТЕЛИ = ("gate", "permissions")
# Поля, чей закомментированный образец показывает умолчание роли: при смене роли перерисовываются
# (`render_base` рисует их по роли записи).
_ОБРАЗЦЫ_ПО_РОЛИ = (
    "write",
    "permissions.independent_register_delete",
    "permissions.commit_limit",
    "gate.mode",
)
# После какой строки вставлять новое поле, если в записи нет ни активной строки, ни образца.
_ПОСЛЕ_КАКИХ = ("role", "password", "user", "url", "label")

# Строка записи: отступ, необязательная решётка с пробелами (образец), ключ, остаток после
# двоеточия. Строки-заголовки (`# --- запись … ---`) и чистые комментарии (`#   # что именно…`)
# под образец не подходят: после решётки у них не ключ с двоеточием.
_КЛЮЧ_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<comm>#[ \t]*)?(?P<key>[A-Za-z_][\w.-]*):(?P<rest>.*)$"
)


@dataclasses.dataclass
class _Запись:
    """Разметка записи базы в списке строк файла: номера строк 0-based, как `start_mark.line`."""

    начало: int  # строка ключа базы (`  ut:`)
    конец: int  # первая строка после блока записи
    отступ: int  # колонка ключей записи (у `base add` — 4)
    активные: dict[str, int]  # "write" → строка; вложенные — "gate.mode", "permissions.<флаг>"
    родители: dict[str, int]  # активные `gate` / `permissions` → строка ключа раздела


def _ошибка_yaml(exc: yaml.YAMLError) -> ConfigError:
    """Как `loader._разобрать_yaml`: из исключения берётся только место — текст PyYAML вклеивает
    фрагмент файла вокруг ошибки, а в нём может быть пароль."""
    mark = getattr(exc, "problem_mark", None)
    место = (
        f"строка {mark.line + 1}, колонка {mark.column + 1}"
        if mark is not None
        else "точное место в файле не определено"
    )
    return ConfigError(f"файл баз повреждён и не разбирается как YAML: {место}")


def _найти_запись(текст: str, name: str, число_строк: int) -> _Запись:
    try:
        корень = yaml.compose(текст)
    except yaml.YAMLError as exc:
        raise _ошибка_yaml(exc) from exc
    if not isinstance(корень, yaml.MappingNode):
        raise ConfigError("файл баз: ожидался словарь разделов")
    bases_ключ = bases = None
    for k, v in корень.value:
        if k.value == "bases":
            bases_ключ, bases = k, v
    if not isinstance(bases, yaml.MappingNode):
        raise ConfigError("файл баз: раздел bases не найден или не является словарём")
    записи = list(bases.value)
    совпали = [(i, k, v) for i, (k, v) in enumerate(записи) if k.value == name]
    if not совпали:
        известные = ", ".join(sorted(k.value for k, _ in записи)) or "ни одной"
        raise ConfigError(
            f"база «{name}» не описана", code="base_unknown", hint=f"известные базы: {известные}"
        )
    if len(совпали) > 1:
        raise ConfigError(f"база «{name}» описана дважды — поправьте файл вручную")
    i, k, v = совпали[0]
    нестандартная = ConfigError(
        f"база «{name}»: запись в нестандартной форме, поправьте файл вручную",
        hint="нужна запись блоком «ключ: значение» по строкам, как её пишет base add",
    )
    if not isinstance(v, yaml.MappingNode) or v.flow_style or not v.value:
        raise нестандартная
    if i + 1 < len(записи):
        конец = записи[i + 1][0].start_mark.line
    else:
        после_bases = [
            kk.start_mark.line
            for kk, _ in корень.value
            if kk.start_mark.line > bases_ключ.start_mark.line
        ]
        конец = min(после_bases) if после_bases else число_строк
    ключи = [kk.value for kk, _ in v.value]
    if len(set(ключи)) != len(ключи) or "<<" in ключи:
        raise нестандартная
    активные = {kk.value: kk.start_mark.line for kk, _ in v.value}
    родители: dict[str, int] = {}
    for kk, vv in v.value:
        if kk.value not in _РОДИТЕЛИ:
            continue
        if not isinstance(vv, yaml.MappingNode) or vv.flow_style:
            raise нестандартная
        дети = [c.value for c, _ in vv.value]
        if len(set(дети)) != len(дети):
            raise нестандартная
        родители[kk.value] = kk.start_mark.line
        for c, _ in vv.value:
            активные[f"{kk.value}.{c.value}"] = c.start_mark.line
    return _Запись(k.start_mark.line, конец, v.value[0][0].start_mark.column, активные, родители)


def _правка(текст: str, name: str, операция: Callable[[list[str], _Запись], None]) -> str:
    """Одна операция над строками записи между двумя разборами: `операция` меняет список строк на
    месте, пользуясь свежей разметкой."""
    строки = текст.split("\n")
    запись = _найти_запись(текст, name, len(строки))
    операция(строки, запись)
    return "\n".join(строки)


# Временные заглушки: настоящие `SetResult` и `set_base_fields` появятся в задаче 4.
@dataclasses.dataclass(frozen=True)
class SetResult:
    label: str
    изменено: bool
    изменения: list
    действует: dict
    явные_поля: list[str]


def set_base_fields(path: pathlib.Path, name: str, changes: dict[str, object]) -> SetResult:
    raise NotImplementedError("задача 4")
