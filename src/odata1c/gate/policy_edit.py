"""Конструктор `policy hide | open | set`: точечная правка файла владельца (`policy.yaml`) без
потери комментариев (SPEC §6.9, ADR-0015, задача 5 плана M2b).

Разбор и запись — через `ruamel.yaml` в режиме round-trip (`YAML()`, `preserve_quotes = True`),
тот же приём, что и `strip_auto_section` (`gate/policy.py`): обычный `yaml.safe_load` +
`yaml.safe_dump` (как в `redact_policy`/`generate_policy`, тот же модуль) стирает все комментарии
файла — здесь этого быть не должно, файл владельца читает и правит своими глазами сам владелец,
а не только машина.

Запись атомарна: временный файл `<путь>.yaml.new` рядом и `os.replace` поверх исходного — тот же
приём, что у `_сохранить` внутри `strip_auto_section`/`dump_auto`. При сбое записи (диск полон,
файл занят, `os.replace` бросает) исходный файл не тронут: до этого момента менялся только
временный файл."""

from __future__ import annotations

import os
import pathlib

from odata1c.gate.policy import PolicyError, read_roundtrip


def _открыть(path: pathlib.Path):
    """Разобрать файл владельца round-trip парсером ruamel — сохраняет комментарии и порядок
    разделов для последующей записи тем же разбором. Раздел неожиданного типа на верхнем уровне
    (не словарь) — `PolicyError`, тем же протоколом (`code`, `hint`), что и остальные ошибки
    политики (`gate/policy.py::parse_owner_file`).

    Сам разбор — общий `read_roundtrip` (`gate/policy.py`): ошибка YAML и повторяющийся раздел
    (шаблонная заглушка `entities: {}` плюс раскомментированный пример `# entities:` ниже —
    `DuplicateKeyError` ruamel, который pyyaml принимает молча) выходят отсюда `PolicyError`, а не
    голой трассировкой ruamel посреди команды владельца (находка I1 итогового ревью M2b)."""
    from ruamel.yaml.comments import CommentedMap

    yaml_rt, данные = read_roundtrip(path)
    if not isinstance(данные, CommentedMap):
        raise PolicyError(
            f"policy.yaml: ожидался словарь разделов, файл {path}",
            hint="см. шаблон policy.example.yaml",
        )
    return yaml_rt, данные


def _раздел(данные, имя: str):
    """Раздел `данные[имя]` как изменяемый `CommentedMap` — для точечного добавления записи.

    Заглушка шаблона (`entities: {}`, `fields: {}`, `custom: {}`) и вовсе отсутствующий раздел
    заменяются настоящим отображением; уже непустой раздел возвращается как есть — правка
    добавляет запись, а не пересобирает раздел заново.

    Комментарий, стоявший у заглушки (`entities: {}  # сущности, которых модель не видит вовсе:`),
    по возможности переносится на новое значение через `данные.ca.items` — внутренний, но
    единственный доступный API ruamel для «привязанных» построчных комментариев. Перенос — лучшее
    усилие: если для конкретной формы комментария он не сохранится, теряется только комментарий у
    этой одной заглушки, а не шапка файла или комментарий у соседних ключей (уточнение брифа
    задачи 5: главное свойство — шапка и комментарий у `scan_free_text` остаются дословно)."""
    from ruamel.yaml.comments import CommentedMap

    if not isinstance(данные.get(имя), CommentedMap) or not данные.get(имя):
        комментарий = данные.ca.items.get(имя)
        данные[имя] = CommentedMap()
        if комментарий is not None:
            данные.ca.items[имя] = комментарий
    return данные[имя]


def _сохранить(yaml_rt, данные, path: pathlib.Path) -> None:
    временный = path.with_suffix(".yaml.new")
    with временный.open("w", encoding="utf-8") as f:
        yaml_rt.dump(данные, f)
    os.replace(временный, path)


def hide_entity(path: pathlib.Path, entity: str) -> bool:
    """`policy hide`: закрыть сущность целиком — `entities.<entity>.hide: true` (SPEC §6.9).

    Наследование запрета на дочерние объекты (табличные части, наборы записей, виртуальные
    таблицы — Ruling 30) в файл не пишется и писаться не должно: его считает гейт на чтении
    политики, по индексу метаданных (`Policy.hidden_entities()` вместе с
    `IndexRepository.descendants`) — файл владельца хранит только корень запрета.

    Возвращает `True`, если правило дописано этим вызовом; `False` — сущность уже была скрыта
    (`entities.<entity>.hide` уже `true`) — файл в этом случае не переписывается вовсе, и
    вызывающий (CLI) не печатает строку об изменении."""
    yaml_rt, данные = _открыть(path)
    сущности = _раздел(данные, "entities")
    существующее = сущности.get(entity)
    if isinstance(существующее, dict) and существующее.get("hide"):
        return False

    from ruamel.yaml.comments import CommentedMap

    сущности[entity] = CommentedMap({"hide": True})
    _сохранить(yaml_rt, данные, path)
    return True


def set_field_class(path: pathlib.Path, field: str, cls: str) -> str | None:
    """`policy open`/`policy set`: назначить полю класс гейта в `fields.<field>` (SPEC §6.4).

    `field` — в форме `Сущность.Поле`; `cls` — один из `CLASSES` (`gate/tokens.py`), `scan` или
    `custom:<имя>`. Допустимость самого класса и имени сущности/поля по индексу — забота
    вызывающего (CLI, задача 5: `policy set`/`policy open` отказывают ДО записи); здесь запись
    безусловна — правило `fields` главнее всего остального (SPEC §6.9), владелец волен назначить
    и заведомо неиспользуемый класс.

    Возвращает прежний класс, если поле уже было в разделе `fields` (вызывающий печатает «было:
    …»), иначе `None` — поле решала только авторазметка (`policy.auto.yaml`) или не решал никто;
    CLI различает эти два случая сам (см. `cmd_policy_set` в `cli.py`), здесь оба дают `None`."""
    yaml_rt, данные = _открыть(path)
    поля = _раздел(данные, "fields")
    прежнее = поля.get(field)
    поля[field] = cls
    _сохранить(yaml_rt, данные, path)
    return прежнее
