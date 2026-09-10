"""Конвейер гейта на одну базу (SPEC §6.1-6.9): фасад над прямой подменой (`masking.Masker`),
обратной подменой (`unmasking.Unmasker`), стражем утечек (`guard.Guard`) и политикой
(`policy.Policy`). Слой тулов (M1d, задача 4) вызывает `BaseGate` на каждый запрос к 1С —
входящий текст/значение/ключ через `inbound_*` перед отправкой, ответ через `mask` и `finish`
перед возвратом модели.
"""

from __future__ import annotations

import json
import logging
import pathlib

from odata1c.config.models import BaseConfig
from odata1c.gate.dictionary import Dictionary
from odata1c.gate.guard import Guard
from odata1c.gate.masking import Masker, MaskResult, effective_field_class
from odata1c.gate.policy import load_policy
from odata1c.gate.unmasking import Unmasker

# Уровень для ответов, у которых база не определена (неизвестная база в запросе): классов полей
# по политике конкретной базы нет, поэтому страж проверяет по максимально строгому уровню —
# известные словарю значения (числа, названия) всё равно не должны выйти наружу (инвариант 1).
СТРОЖАЙШИЙ_УРОВЕНЬ = "identifiers+names"

_log = logging.getLogger(__name__)


class BaseGate:
    """Фасад гейта на одну базу. Держит собранные на актуальной политике `Masker`/`Unmasker`;
    `Guard` и `Dictionary` общие на процесс — база отличает их только параметром `base` вызовов."""

    def __init__(
        self,
        *,
        base: BaseConfig,
        dictionary: Dictionary,
        guard: Guard,
        policy_path: pathlib.Path,
    ) -> None:
        self._base = base
        self._dictionary = dictionary
        self._guard = guard
        self._policy_path = pathlib.Path(policy_path)
        self.mode = base.gate.mode
        self._mtime: float | None = None
        self._masker: Masker | None = None
        self._unmasker: Unmasker | None = None
        self.refresh()

    def refresh(self) -> None:
        """Перечитать политику, если файл изменился с прошлого раза. Политику перезаписывает и
        реиндекс (раздел auto), и пользователь (fields) — демон живёт дольше одной версии файла.
        Сверка mtime дешевле разбора YAML на каждый вызов. Отсутствующий файл политики —
        не ошибка и не повод падать (`load_policy` отдаёт пустую `Policy`): демон поднимается
        и без политики, с пустыми классами."""
        mtime = self._policy_path.stat().st_mtime if self._policy_path.exists() else None
        if mtime == self._mtime and self._masker is not None:
            return
        policy = load_policy(self._policy_path)
        self._policy, self._mtime = policy, mtime
        self._masker = Masker(self._dictionary, policy, mode=self.mode, base=self._base.name)
        self._unmasker = Unmasker(
            self._dictionary,
            base=self._base.name,
            field_class=lambda entity, field: effective_field_class(
                policy, entity, field, mode=self.mode
            ),
        )

    def is_hidden(self, entity: str) -> bool:
        return self._policy.is_hidden(entity)

    def is_protected(self, entity: str, field: str) -> bool:
        """Класс поля — что-то, кроме «не защищён» (`None`), «оставить как есть» (`keep`) или
        «только сканировать значение» (`scan`, значение целиком не заменяется по классу поля)."""
        класс = effective_field_class(self._policy, entity, field, mode=self.mode)
        return класс not in (None, "keep", "scan")

    def inbound_filter(self, expression: str, *, entity: str) -> str:
        if self.mode == "off":
            return expression
        return self._unmasker.filter(expression, entity=entity)

    def inbound_value(self, text: str, *, entity: str, field: str) -> str:
        if self.mode == "off":
            return text
        return self._unmasker.value(text, entity=entity, field=field)

    def inbound_key(self, key, *, entity: str):
        if self.mode == "off":
            return key
        return self._unmasker.key(key, entity=entity)

    def mask(self, data, *, entity: str) -> MaskResult:
        return self._masker.mask(data, entity=entity)

    def finish(self, envelope: dict) -> str:
        """Сериализация ответа тула (`ensure_ascii=False` — страж должен видеть кириллицу как
        есть, не в `\\uXXXX`-экранировании) и страж утечек как последний проход по готовому
        тексту (SPEC §6.8, инвариант 1). Заменивший что-то страж помечает ответ `guard_replaced`
        в `warnings` — маскировщик пропустил значение, страж поймал его отдельно.

        Штатно `guard.py` сохраняет валидность JSON у изменённого текста (F2 M1c) — `json.loads`
        ниже на это опирается. `try/except` вокруг него — страховка последнего рубежа (Important,
        ревью 2026-09-10): finish — часть инварианта 1, и голое исключение здесь потеряло бы
        текст ответа у клиента. Если страж когда-нибудь вернёт невалидный JSON, отдаём его текст
        как есть (он уже прошёл страж — утечки в нём нет, только `warnings` не допишутся) и
        логируем сам факт, без текста ответа (в нём могут быть данные)."""
        текст = json.dumps(envelope, ensure_ascii=False)
        проверено = self._guard.check(текст, mode=self.mode)
        if not проверено.replacements:
            return проверено.text
        try:
            данные = json.loads(проверено.text)
        except json.JSONDecodeError:
            _log.error("страж вернул невалидный JSON при непустых replacements")
            return проверено.text
        данные.setdefault("warnings", []).append(
            f"guard_replaced: страж заменил {len(проверено.replacements)} значений, "
            "не распознанных маскировщиком"
        )
        return json.dumps(данные, ensure_ascii=False)

    def finish_text(self, text: str) -> str:
        """Страж по готовому тексту целиком, без обёртки в JSON-конверт — для markdown-ответов
        (`describe` и подобные), а не JSON-тулов."""
        return self._guard.check(text, mode=self.mode).text

    def error(self, code: str, message: str, hint: str = "") -> str:
        """Ошибка тула — обычный текст с JSON `{"error": {...}}` (SPEC §5.2), не исключение.
        Сообщение 1С и подсказка могут содержать реальное значение (инвариант 1: ошибки 1С —
        такой же путь утечки, как обычный ответ) — маскируются `mask_text` тем же маскировщиком,
        что и ответ, следом идёт обычный `finish` со стражем."""
        сообщение = self._masker.mask_text(message, entity="", field="error")
        подсказка = self._masker.mask_text(hint, entity="", field="error") if hint else hint
        return self.finish({"error": {"code": code, "message": сообщение, "hint": подсказка}})


def guard_only(guard: Guard, envelope: dict) -> str:
    """Сериализация и страж на строжайшем уровне — для ответов, у которых база не определена
    (запрос на неизвестную/недоступную базу): политики и классов полей ещё нет, но известные
    словарю значения всё равно не должны выйти наружу (инвариант 1)."""
    текст = json.dumps(envelope, ensure_ascii=False)
    return guard.check(текст, mode=СТРОЖАЙШИЙ_УРОВЕНЬ).text
