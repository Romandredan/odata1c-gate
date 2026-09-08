"""Перевод ответов 1С в коды ошибок SPEC §5.2."""

from __future__ import annotations

import json


class OdataError(Exception):
    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def map_error(status: int, body: str) -> OdataError:
    текст = _текст_ошибки_платформы(body) or body.strip()[:500]
    if status in (401, 403):
        return OdataError(
            "auth_failed",
            текст or "1С отклонила учётные данные",
            "проверьте user и password базы командой odata1c base test",
        )
    if status == 404:
        return OdataError(
            "entity_unknown",
            текст or "1С не нашла указанный путь",
            "если сущность точно есть, обновите индекс: odata1c reindex <база>",
        )
    if status in (408, 504):
        return OdataError(
            "timeout",
            текст or "1С не ответила вовремя",
            "сузьте выборку через $select и $top или увеличьте timeout_s базы",
        )
    return OdataError(
        "odata_error", текст or f"1С вернула HTTP {status}", "текст выше — сообщение платформы"
    )


def _текст_ошибки_платформы(body: str) -> str:
    """Из тела odata.error достать человекочитаемое сообщение.

    Тело приходит из сети — доверять его форме нельзя. Если платформа вместо объекта
    отдала список, число или что-то ещё не-словарное (на любом уровне вложенности),
    считаем сообщение неразобранным и возвращаем пустую строку: вызывающий код (map_error)
    в этом случае берёт сырой текст тела как есть, но не падает необработанным исключением.
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    ошибка = data.get("odata.error") or data.get("error") or {}
    if not isinstance(ошибка, dict):
        return ""
    сообщение = ошибка.get("message")
    if isinstance(сообщение, dict):
        return str(сообщение.get("value", ""))
    return str(сообщение or "")
