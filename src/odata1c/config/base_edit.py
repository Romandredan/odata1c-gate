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


_НУЛЬ = "tag:yaml.org,2002:null"


def _есть_якоря(текст: str) -> bool:
    """Есть ли в файле хоть один якорь (`&имя`) или ссылка (`*имя`): по событиям разбора, не по
    тексту — решётка и звёздочка в значениях под это не подпадают."""
    return any(getattr(событие, "anchor", None) is not None for событие in yaml.parse(текст))


def _разметка_верна(строки: list[str], запись: _Запись) -> bool:
    """Каждый номер строки разметки лежит внутри блока записи и указывает на строку с тем же
    ключом без решётки: так виден сдвиг номеров, который разбор даёт на разделителях строк,
    неизвестных делению по `\\n` (U+2028, U+2029, U+0085, одиночный `\\r`)."""
    ключи = [(поле.rsplit(".", 1)[-1], номер) for поле, номер in запись.активные.items()]
    ключи += list(запись.родители.items())
    for ключ, номер in ключи:
        if not запись.начало < номер < min(запись.конец, len(строки)):
            return False
        совпало = _КЛЮЧ_RE.match(строки[номер])
        if совпало is None or совпало["comm"] or совпало["key"] != ключ:
            return False
    return True


def _найти_запись(текст: str, name: str, число_строк: int) -> _Запись:
    """Разметка записи базы `name`: границы блока и строки активных ключей по `yaml.compose`.

    Границы: `начало` — строка ключа базы; `конец` — первая строка после блока, одна из трёх:
    строка ключа следующей базы, строка ближайшего ключа верхнего уровня после `bases` (например
    `default:` ниже перечня), иначе `число_строк` (конец файла). Ключ верхнего уровня выше `bases`
    блок не ограничивает.

    `активные` — "write" и прочие ключи записи → строка; вложенные ключи `gate` и `permissions`
    — "gate.mode", "permissions.<флаг>". `родители` — строки ключей раздела `gate` / `permissions`,
    которые есть в записи. Закомментированные образцы сюда не попадают: их ищет правка по строкам.

    Что отклоняется («нестандартная форма») и почему: запись и разделы `gate` / `permissions` в
    поточной форме (`{…}`), пустые, повторы ключа, ключ слияния `<<`, составной ключ — по ним нет
    однозначной строки; любой якорь или ссылка YAML в файле — `compose` у ссылки отдаёт узел якоря с
    его номерами строк, и правка записи-источника молча поменяла бы все ссылающиеся записи. Файл
    без раздела `bases` или с пустым `bases:` — перечень баз пуст, как у загрузчика. Номера строк
    разбора сверяются со строками файла (`_разметка_верна`): PyYAML считает переводом строки и
    U+2028, U+2029, U+0085, одиночный `\\r`, а правка делит текст по `\\n`.

    Тексты отказов не содержат значений из файла: только имя базы, место ошибки и имена баз."""
    try:
        корень = yaml.compose(текст)
    except yaml.YAMLError as exc:
        raise _ошибка_yaml(exc) from exc
    if корень is None:
        пары_корня: list = []
    elif isinstance(корень, yaml.MappingNode):
        пары_корня = list(корень.value)
    else:
        raise ConfigError("файл баз: ожидался словарь разделов")
    bases_ключ = bases = None
    for k, v in пары_корня:
        if isinstance(k, yaml.ScalarNode) and k.value == "bases":
            bases_ключ, bases = k, v
    if bases is None or (isinstance(bases, yaml.ScalarNode) and bases.tag == _НУЛЬ):
        записи: list = []
    elif isinstance(bases, yaml.MappingNode):
        записи = list(bases.value)
    else:
        raise ConfigError("файл баз: раздел bases не найден или не является словарём")
    совпали = [
        (i, k, v)
        for i, (k, v) in enumerate(записи)
        if isinstance(k, yaml.ScalarNode) and k.value == name
    ]
    if not совпали:
        известные = (
            ", ".join(sorted(k.value for k, _ in записи if isinstance(k, yaml.ScalarNode)))
            or "ни одной"
        )
        raise ConfigError(
            f"база «{name}» не описана", code="base_unknown", hint=f"известные базы: {известные}"
        )
    if len(совпали) > 1:
        raise ConfigError(f"база «{name}» описана дважды — поправьте файл вручную")
    сообщение = f"база «{name}»: запись в нестандартной форме, поправьте файл вручную"
    нестандартная = ConfigError(
        сообщение, hint="нужна запись блоком «ключ: значение» по строкам, как её пишет base add"
    )
    if _есть_якоря(текст):
        raise ConfigError(
            сообщение, hint="в файле якоря или ссылки YAML (&имя, *имя) — команда их не правит"
        )
    i, k, v = совпали[0]
    if not isinstance(v, yaml.MappingNode) or v.flow_style or not v.value:
        raise нестандартная
    if i + 1 < len(записи):
        конец = записи[i + 1][0].start_mark.line
    else:
        после_bases = [
            kk.start_mark.line
            for kk, _ in пары_корня
            if kk.start_mark.line > bases_ключ.start_mark.line
        ]
        конец = min(после_bases) if после_bases else число_строк
    if any(not isinstance(kk, yaml.ScalarNode) for kk, _ in v.value):
        raise нестандартная
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
        if any(not isinstance(c, yaml.ScalarNode) for c, _ in vv.value):
            raise нестандартная
        дети = [c.value for c, _ in vv.value]
        if len(set(дети)) != len(дети) or "<<" in дети:
            raise нестандартная
        родители[kk.value] = kk.start_mark.line
        for c, _ in vv.value:
            активные[f"{kk.value}.{c.value}"] = c.start_mark.line
    запись = _Запись(k.start_mark.line, конец, v.value[0][0].start_mark.column, активные, родители)
    if not _разметка_верна(текст.split("\n"), запись):
        raise ConfigError(
            f"база «{name}»: разметка записи не совпала со строками файла — поправьте файл вручную",
            hint="в файле необычные разделители строк или якоря YAML",
        )
    return запись


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
