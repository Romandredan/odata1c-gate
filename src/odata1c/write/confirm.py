"""Механизм подтверждения записи по возможностям клиента (ADR-0012, SPEC §7.2; план M2, задача 7).

Подтверждение — действие пользователя, которое модель не может выполнить сама. Кто его
спрашивает, зависит от клиента, и выбирается один раз на сессию (решение 6 плана; слой демона —
задача 9):

- **Claude Code** — `claude_code`: подтверждает сам клиент диалогом разрешения по
  `_meta["anthropic/requiresUserInteraction"]` у `odata1c_commit`. Сервер ничего не спрашивает —
  второй диалог подряд на один `commit` утомляет и обесценивает подтверждение (ADR-0012), даже
  если клиент умеет elicitation;
- **клиент с elicitation** — `elicitation`: перед выполнением демон показывает текст превью и
  ждёт «yes»; всё прочее (`no`, `decline`, `cancel`, сбой) — отказ;
- **прочие** — `write_confirm_fallback` демона: `deny` (по умолчанию) — запись недоступна
  (`write_unsupported_client`), `trust_client` — выполнение без вопроса на ответственность
  пользователя (`trust`).

Модуль не знает ни о гейте, ни о MCP: имя клиента и признак elicitation ему передаёт слой
демона, текст подтверждения — `WriteService.commit` (уже через стража).
"""

from __future__ import annotations

from typing import Literal, Protocol

Механизм = Literal["claude_code", "elicitation", "deny", "trust"]

# Нормализованное имя Claude Code в `clientInfo.name`. Сравнение — по нормализованной форме
# (регистр, пробелы и подчёркивания как дефис), а не по одному написанию: какое именно имя
# присылает клиент через лаунчер, проверяется исполнением в задаче 9, и «Claude Code» с
# «claude_code» не должны молча уводить сессию в другой механизм.
_CLAUDE_CODE = "claude-code"


def _нормализовать(имя: str) -> str:
    return "-".join(имя.strip().lower().replace("_", " ").split())


def choose_mechanism(
    client_name: str | None,
    has_elicitation: bool,
    fallback: Literal["deny", "trust_client"],
) -> Механизм:
    """ADR-0012: Claude Code → `claude_code` (подтверждает клиент диалогом разрешения по `_meta`
    тула); клиент с elicitation → `elicitation`; прочие → `deny` | `trust`.

    Claude Code проверяется первым: он поддерживает elicitation (с 2.1.238), но второй диалог
    поверх диалога разрешения ADR-0012 отвергает."""
    if client_name and _нормализовать(client_name) == _CLAUDE_CODE:
        return "claude_code"
    if has_elicitation:
        return "elicitation"
    return "trust" if fallback == "trust_client" else "deny"


class Confirmer(Protocol):
    """Спросить пользователя. `True` — только явное «yes»; отказ, отмена и любой иной ответ —
    `False`. Реализация — elicitation в слое демона (задача 9)."""

    async def __call__(self, message: str) -> bool: ...
