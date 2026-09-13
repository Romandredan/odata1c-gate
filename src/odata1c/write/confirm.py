"""Механизм подтверждения записи по возможностям клиента (ADR-0012, SPEC §7.2; план M2, 7 и 9).

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

Модуль не знает ни о гейте, ни о MCP: имя, версию клиента и признак elicitation ему передаёт слой
демона, текст подтверждения — `WriteService.commit` (уже через стража).
"""

from __future__ import annotations

import re
from typing import Literal, Protocol

Механизм = Literal["claude_code", "elicitation", "deny", "trust"]

# Имена клиента в `clientInfo.name`, чей диалог разрешения подтверждает запись сам (ADR-0012).
# Сравнение ТОЧНОЕ, по перечню известных имён, без приведения регистра, пробелов и
# подчёркиваний (задача 9, опасение 8 задачи 7): механизм `claude_code` значит «сервер не
# спрашивает, подтверждает клиент», и похожее имя другого клиента («Claude Code», «claude_code»,
# «claude-code-fork») получило бы запись без единого подтверждения — если его диалог разрешения
# `requiresUserInteraction` не понимает. Ложное несовпадение безопасно (второй вопрос или отказ),
# ложное совпадение — нет.
#
# Имя проверено исполнением (задача 9, 2026-09-13): тело `claude.exe` 2.1.267 создаёт клиента MCP
# для stdio и HTTP одним и тем же `new …({name:"claude-code",title:"Claude Code",version:…})`;
# лаунчер пересылает это имя демону заголовком (`daemon.CLIENT_NAME_HEADER`). Живая сессия Claude
# Code — приёмка задачи 10.
ИМЕНА_CLAUDE_CODE: frozenset[str] = frozenset({"claude-code"})

# С этой версии диалог разрешения по `requiresUserInteraction` не предлагает «не спрашивать
# больше» (SPEC §15: «первая исправлена в 2.1.246»). Раньше пользователь мог разрешить
# `odata1c_commit` навсегда — и `claude_code` там означал бы запись без подтверждения. Старый или
# нераспознанный номер версии — не `claude_code`, а обычный клиент: elicitation (Claude Code
# умеет её с 2.1.238) или запасной механизм.
МИНИМАЛЬНАЯ_ВЕРСИЯ_CLAUDE_CODE = (2, 1, 246)

# Только «число.число.число» целиком: предварительный выпуск «2.1.246-rc1» по семантике версий
# СТАРШЕ не бывает, а угадывать чужие суффиксы — значит однажды угадать неверно в опасную сторону.
_ВЕРСИЯ = re.compile(r"(\d{1,6})\.(\d{1,6})\.(\d{1,6})")


def _версия(текст: str | None) -> tuple[int, int, int] | None:
    if not isinstance(текст, str):
        return None
    найдено = _ВЕРСИЯ.fullmatch(текст)
    if найдено is None:
        return None
    return int(найдено[1]), int(найдено[2]), int(найдено[3])


def is_claude_code(client_name: str | None, client_version: str | None) -> bool:
    """Клиент — Claude Code той версии, где диалог разрешения по `requiresUserInteraction`
    нельзя снять навсегда: имя из `ИМЕНА_CLAUDE_CODE` дословно и версия не ниже
    `МИНИМАЛЬНАЯ_ВЕРСИЯ_CLAUDE_CODE`."""
    if client_name not in ИМЕНА_CLAUDE_CODE:
        return False
    версия = _версия(client_version)
    return версия is not None and версия >= МИНИМАЛЬНАЯ_ВЕРСИЯ_CLAUDE_CODE


def choose_mechanism(
    client_name: str | None,
    client_version: str | None,
    has_elicitation: bool,
    fallback: Literal["deny", "trust_client"],
) -> Механизм:
    """ADR-0012: Claude Code → `claude_code` (подтверждает клиент диалогом разрешения по `_meta`
    тула); клиент с elicitation → `elicitation`; прочие → `deny` | `trust`.

    Claude Code проверяется первым: он поддерживает elicitation (с 2.1.238), но второй диалог
    поверх диалога разрешения ADR-0012 отвергает. `trust` — только при запасном `trust_client`:
    любое другое значение, в том числе неожиданное, — `deny`."""
    if is_claude_code(client_name, client_version):
        return "claude_code"
    if has_elicitation:
        return "elicitation"
    return "trust" if fallback == "trust_client" else "deny"


class Confirmer(Protocol):
    """Спросить пользователя. `True` — только явное «yes»; отказ, отмена и любой иной ответ —
    `False`. Реализация — elicitation в слое демона (`daemon.elicitation_confirmer`)."""

    async def __call__(self, message: str) -> bool: ...
