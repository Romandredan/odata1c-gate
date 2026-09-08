"""Перенос баз из env-файла прежнего сервера 1c-odata-mcp (SPEC §3.5).

Читаются ключи ODATA_DB_<NAME>_BASE_URL | _USERNAME | _PASSWORD | _LABEL | _WRITABLE,
а также ODATA_DEFAULT_DB и READ_ONLY. _WRITABLE=true даёт write: true, роль по умолчанию prod.
"""

from __future__ import annotations

import re

КЛЮЧ = re.compile(
    r"^ODATA_DB_(?P<база>[A-Z0-9_\-]+)_(?P<поле>BASE_URL|USERNAME|PASSWORD|LABEL|WRITABLE)$"
)
ПОЛЯ = {"BASE_URL": "url", "USERNAME": "user", "PASSWORD": "password", "LABEL": "label"}


def normalize_name(raw: str) -> str:
    """Имя базы в env написано заглавными и может содержать дефис: приводим к [a-z0-9_]."""
    имя = re.sub(r"[^a-z0-9_]", "_", raw.lower())
    return имя[:32] or "base"


def parse_env(text: str) -> tuple[str | None, list[dict]]:
    значения: dict[str, dict] = {}
    по_умолчанию: str | None = None
    только_чтение = False

    for строка in text.splitlines():
        строка = строка.strip()
        if not строка or строка.startswith("#") or "=" not in строка:
            continue
        ключ, _, значение = строка.partition("=")
        ключ, значение = ключ.strip(), значение.strip().strip('"').strip("'")

        if ключ == "ODATA_DEFAULT_DB":
            по_умолчанию = normalize_name(значение)
            continue
        if ключ == "READ_ONLY":
            только_чтение = значение.lower() in ("1", "true", "yes")
            continue

        совпадение = КЛЮЧ.match(ключ)
        if not совпадение:
            continue
        имя = normalize_name(совпадение["база"])
        запись = значения.setdefault(имя, {"name": имя, "role": "prod", "write": False})
        поле = совпадение["поле"]
        if поле == "WRITABLE":
            запись["write"] = значение.lower() in ("1", "true", "yes")
        else:
            запись[ПОЛЯ[поле]] = значение

    базы = [запись for запись in значения.values() if "url" in запись]
    for запись in базы:
        запись.setdefault("label", запись["name"])
        запись.setdefault("user", "")
        запись.setdefault("password", "")
        if только_чтение:
            запись["write"] = False
    return по_умолчанию, базы
