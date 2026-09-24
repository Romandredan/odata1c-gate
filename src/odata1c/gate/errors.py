"""Ошибка политики гейта — общая для политики базы (`policy.py`) и каталога правил (`rules.py`).

Отдельный модуль ради порядка импорта: каталог правил читает классификатор полей
(`field_rules`), а классификатор читает политика, — определённая в `policy.py`, ошибка замкнула бы
импорт в круг. Прежний путь `odata1c.gate.policy.PolicyError` остаётся: политика её
переэкспортирует.
"""

from __future__ import annotations


class PolicyError(Exception):
    """Ошибка политики базы: неверная разметка `policy.yaml` или файла каталога правил, раздел
    неожиданного типа, недопустимое регулярное выражение своего класса. Тот же протокол атрибутов
    (code, hint), что у `odata1c.config.loader.ConfigError` и
    `odata1c.index.repository.IndexCorruptError` — CLI и демон различают причину отказа
    одинаково."""

    def __init__(self, message: str, code: str = "policy_invalid", hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint
