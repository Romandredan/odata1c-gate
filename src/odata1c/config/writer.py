"""Дописывание записи базы в bases.yaml в стиле шаблона: с комментариями, не трогая остальное."""

from __future__ import annotations

import pathlib

ШАБЛОН_ЗАПИСИ = """\
  {name}:
    label: {label}
    url: {url}
    user: {user}
    password: "{password}"
    role: {role}

    # --- соединение (умолчания показаны, раскомментируйте для изменения) ---
    # verify_tls: true               # true | false | путь к CA-сертификату (PEM)
    # timeout_s: 60                  # таймаут обычного запроса; виртуальные таблицы — 180
    # concurrency: 2                 # одновременных запросов к этой базе от всех сессий
    # ib_session: true               # держать сеанс 1С (IBSession) между запросами

    # --- запись (умолчание роли {role}) ---
{write_line}
    # permissions:
    #   post_documents: true
    #   mark_deletion: true
    #   independent_register_delete: false
    #   register_direct_write: false
    #   deny_entities: []
    #   deny_fields: []

    # --- гейт (умолчание роли {role}) ---
    # gate:
    #   mode: identifiers+names      # off | identifiers | identifiers+names
"""


def render_base(name: str, values: dict) -> str:
    write = values.get("write")
    write_line = (
        "    write: true                    # разрешить пишущие тулы"
        if write
        else "    # write: false                 # разрешить пишущие тулы"
    )
    return ШАБЛОН_ЗАПИСИ.format(
        name=name,
        label=values.get("label", name),
        url=values["url"],
        user=values.get("user", ""),
        password=values.get("password", ""),
        role=values.get("role", "prod"),
        write_line=write_line,
    )


def append_base(path: pathlib.Path, name: str, values: dict) -> None:
    """Дописать базу в конец раздела bases, сохранив комментарии остального файла."""
    текст = path.read_text(encoding="utf-8") if path.exists() else ""
    if "bases:" not in текст:
        текст = (текст + "\n" if текст and not текст.endswith("\n") else текст) + "bases:\n"
    if not текст.endswith("\n"):
        текст += "\n"
    path.write_text(текст + "\n" + render_base(name, values), encoding="utf-8")
