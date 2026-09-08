"""Запись настроек: дописывание базы в bases.yaml, обеспечение секрета гейта в daemon.yaml.

Обе операции — не «перезаписать файл», а «дописать одно, не тронув остальное» (комментарии
шаблона, записи других баз): читаем текст, добавляем к нему, пишем через временный файл и
атомарную замену (os.replace), чтобы обрыв посередине не оставил обрезанный файл.
"""

from __future__ import annotations

import base64
import os
import pathlib
import secrets

import yaml

from odata1c.config.loader import ConfigError

ШАБЛОН_ЗАПИСИ = """\
  {name}:
    label: {label}
    url: {url}
    user: {user}
    password: {password}
    role: {role}

    # --- соединение (умолчания показаны, раскомментируйте для изменения) ---
    # verify_tls: true               # true | false | путь к CA-сертификату (PEM)
    # timeout_s: 60                  # таймаут обычного запроса; виртуальные таблицы — 180
    # concurrency: 2                 # одновременных запросов к этой базе от всех сессий
    # ib_session: true               # держать сеанс 1С (IBSession) между запросами

    # --- запись (умолчание роли {role}) ---
{write_line}
    # permissions:
    #   post_documents: true         # действия Post/Unpost из $metadata
    #   mark_deletion: true          # PATCH DeletionMark у объектов
    #   independent_register_delete: false   # DELETE записей регистров сведений без регистратора
    #   register_direct_write: false # POST/PATCH в регистры накопления, бухгалтерии, расчёта
    #   allow_entities: []           # если не пусто — запись только в эти сущности
    #   deny_entities: []            # запрет записи в сущности, например [Catalog_Пользователи]
    #   deny_fields: []              # запрет записи в поля, например [Catalog_Контрагенты.ИНН]
    #   commit_limit: 20             # коммитов за 10 минут на сессию; 0 = без лимита

    # --- гейт (умолчание роли {role}) ---
    # gate:
    #   mode: identifiers+names      # off | identifiers | identifiers+names
    #   names_for: [...]             # сущности, чьи Description и поля ФИО заменяются
    #   scan_free_text: true         # искать реквизиты и известные названия в любых строках ответа

    # --- рецепты ---
    # recipes: bases/{name}/recipes.yaml   # путь относительно домашнего каталога
"""


def _скаляр(value: str) -> str:
    """YAML-представление одного значения решает библиотека, а не подстановка в строку-шаблон:
    кавычка, двоеточие, решётка или апостроф в значении (пароль, подпись, адрес, имя пользователя)
    иначе ломают разметку файла — и следующее чтение настроек падает с текстом самого значения
    (например, пароля) внутри сообщения об ошибке YAML."""
    строка = yaml.safe_dump({"значение": value}, allow_unicode=True, default_flow_style=False)
    return строка[len("значение: ") :].rstrip("\n")


def render_base(name: str, values: dict) -> str:
    write = values.get("write")
    write_line = (
        "    write: true                    # разрешить пишущие тулы"
        if write
        else "    # write: false                 # разрешить пишущие тулы"
    )
    return ШАБЛОН_ЗАПИСИ.format(
        name=name,
        label=_скаляр(values.get("label", name)),
        url=_скаляр(values["url"]),
        user=_скаляр(values.get("user", "")),
        password=_скаляр(values.get("password", "")),
        role=values.get("role", "prod"),
        write_line=write_line,
    )


def append_base(path: pathlib.Path, name: str, values: dict) -> None:
    """Дописать базу в конец файла bases.yaml, сохранив комментарии остального файла.

    Запись добавляется в конец файла, а не строго в конец раздела bases: если после этого
    раздела в файле стоит ещё один ключ верхнего уровня (например, default, дописанный туда
    вручную), новая запись окажется под этим ключом и файл перестанет разбираться. От этого
    класса ошибок не защититься само́й вставкой — вместо этого результат проверяется: файл
    читается заново и разбирается как YAML, и только если он разбирается и новая база в нём
    действительно появилась, изменение считается успешным. Иначе исходное содержимое
    восстанавливается и поднимается ConfigError — файл с учётными данными остаётся
    нетронутым, а не тихо портится.
    """
    исходный_текст = path.read_text(encoding="utf-8") if path.exists() else None
    текст = исходный_текст or ""
    if "bases:" not in текст:
        текст = (текст + "\n" if текст and not текст.endswith("\n") else текст) + "bases:\n"
    if not текст.endswith("\n"):
        текст += "\n"
    path.write_text(текст + "\n" + render_base(name, values), encoding="utf-8")

    испорчен = False
    try:
        данные = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(данные, dict) or name not in (данные.get("bases") or {}):
            испорчен = True
    except yaml.YAMLError:
        испорчен = True

    if испорчен:
        if исходный_текст is None:
            path.unlink()
        else:
            path.write_text(исходный_текст, encoding="utf-8")
        raise ConfigError(
            f"не удалось дописать базу «{name}» в {path}: файл оставлен нетронутым",
            hint="после раздела bases в файле стоит ещё один ключ верхнего уровня — "
            "перенесите его выше bases или допишите запись вручную",
        )


def ensure_gate_secret(path: pathlib.Path) -> str:
    """Обеспечить непустой gate_secret в daemon.yaml: создать команда создания домашнего
    каталога (SPEC §6.3), не чтение настроек — см. odata1c.config.loader._load_daemon.

    Если секрет уже есть — вернуть его, не трогая файл (идемпотентность: повторный вызов
    ничего не меняет). Если файла нет или в нём нет секрета — дописать ровно одну строку
    через временный файл и os.replace, не трогая остальное содержимое (комментарии шаблона,
    ранее заданные параметры демона). После записи секрет читается заново с диска: если эту
    функцию вызвали одновременно два процесса, оба должны увидеть и вернуть то значение,
    которое реально осталось на диске, а не то, что каждый из них сам сгенерировал.
    """
    текущий = _прочитать_секрет(path)
    if текущий:
        return текущий

    секрет = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    текст = path.read_text(encoding="utf-8") if path.exists() else ""
    if текст and not текст.endswith("\n"):
        текст += "\n"
    текст += f'gate_secret: "{секрет}"\n'

    временный = path.with_name(path.name + ".tmp")
    временный.write_text(текст, encoding="utf-8")
    os.replace(временный, path)
    if os.name != "nt":
        # На Windows chmod не управляет ACL — правами файла управляет NTFS, а не биты POSIX;
        # реальная защита закрывается на уровне всего домашнего каталога через icacls
        # в odata1c.config.home.ensure_home.
        path.chmod(0o600)

    return _прочитать_секрет(path) or секрет


def _прочитать_секрет(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    текст = path.read_text(encoding="utf-8")
    if not текст.strip():
        return None
    try:
        данные = yaml.safe_load(текст)
    except yaml.YAMLError:
        return None
    if isinstance(данные, dict):
        return данные.get("gate_secret") or None
    return None
