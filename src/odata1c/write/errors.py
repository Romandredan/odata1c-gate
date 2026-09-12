"""Ошибка пишущего тула (SPEC §5.2).

Тот же протокол атрибутов (`code`, `message`, `hint`), что у `ConfigError`
(`odata1c.config.loader`), `PolicyError` (`odata1c.gate.policy`) и `_ServiceError`
(`odata1c.tools.service`) — весь стек ошибок шлюза ловится и отдаётся модели одним и тем же
способом: `{"error": {"code", "message", "hint"}}`, а не голым исключением. Отдельный класс, а
не переиспользование `_ServiceError` чтения: `write/` не знает о `tools/service.py` и не должен
знать — пакет записи (SPEC §7) закрыт от гейта и запросов к 1С по замыслу задачи 2 этого плана,
`ToolService._run` в M2 перехватывает оба класса одним блоком через общий протокол атрибутов.
"""

from __future__ import annotations


class WriteError(Exception):
    """Отказ пишущего тула — подготовки pending-операции, `commit`, `undo`, `journal`.

    `code` — из перечня SPEC §5.2 (`base_read_only`, `permission_denied`, `entity_hidden`,
    `field_write_denied`, `params_invalid`, `token_ambiguous`, `pending_unknown`,
    `pending_expired`, `pending_stale`, `commit_limit`, `write_unsupported_client`,
    `undo_unsupported`, `action_unknown`, `odata_error`, `internal` — и другие коды чтения,
    которые пишущий путь тоже может вернуть до отправки в 1С). `hint` — что сделать, чтобы отказ
    не повторился (какой ключ `bases.yaml` включить, каким тулом воспользоваться); может быть
    пустым, когда подсказывать нечего (например, `entity_hidden` — сущности как будто не
    существует, подсказка её выдала бы).
    """

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
