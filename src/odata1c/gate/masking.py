"""Прямая подмена: ответ 1С → токены (SPEC §6.2, §6.4, §6.5, §5.1).

Порядок из SPEC §6.4: поля keep пропускаются целиком; поле с известным классом заменяется целиком;
остальные строки сканируются детекторами; известные значения словаря заменяются всегда.
"""

from __future__ import annotations

import dataclasses

from odata1c.gate.detectors import scan_value
from odata1c.gate.dictionary import Dictionary
from odata1c.gate.policy import Policy
from odata1c.gate.tokens import CLASSES

# Вырезается из ответа всегда (SPEC §5.1).
СЛУЖЕБНЫЕ_ПОЛЯ = ("odata.metadata", "odata.type", "DataVersion")
ДВОИЧНЫЕ_СУФФИКСЫ = ("_Base64Data", "ХранилищеЗначения")
КЛАССЫ_НАЗВАНИЙ = ("org", "person")


@dataclasses.dataclass(slots=True)
class MaskResult:
    data: object
    masked_fields: list[str]
    warnings: list[str]


class Masker:
    def __init__(self, dictionary: Dictionary, policy: Policy, *, mode: str, base: str) -> None:
        self._dictionary = dictionary
        self._policy = policy
        self._mode = mode
        self._base = base

    def mask(self, data, *, entity: str) -> MaskResult:
        замаскированные: list[str] = []
        предупреждения: list[str] = []
        if self._mode == "off":
            return MaskResult(data=data, masked_fields=[], warnings=[])
        результат = self._обойти(data, entity=entity, замаскированные=замаскированные)
        return MaskResult(
            data=результат, masked_fields=sorted(set(замаскированные)), warnings=предупреждения
        )

    def mask_text(self, text: str, *, entity: str, field: str) -> str:
        """Подмена внутри произвольной строки: ошибки 1С, превью, сообщения (инвариант 1)."""
        if self._mode == "off":
            return text
        return self._обработать_строку(
            text, entity=entity, field=field, класс=None, замаскированные=[]
        )

    def _обойти(
        self, значение, *, entity: str, замаскированные: list[str], field: str | None = None
    ):
        if isinstance(значение, dict):
            результат = {}
            for ключ, вложенное in значение.items():
                if ключ in СЛУЖЕБНЫЕ_ПОЛЯ or any(
                    ключ.endswith(суффикс) for суффикс in ДВОИЧНЫЕ_СУФФИКСЫ
                ):
                    continue
                if isinstance(вложенное, str):
                    класс = self._класс_поля(entity, ключ)
                    результат[ключ] = self._обработать_строку(
                        вложенное,
                        entity=entity,
                        field=ключ,
                        класс=класс,
                        замаскированные=замаскированные,
                    )
                else:
                    результат[ключ] = self._обойти(
                        вложенное, entity=entity, замаскированные=замаскированные, field=ключ
                    )
            return результат
        if isinstance(значение, list):
            return [
                self._обойти(элемент, entity=entity, замаскированные=замаскированные, field=field)
                for элемент in значение
            ]
        if isinstance(значение, str) and field is not None:
            # Строка внутри списка (без собственного ключа) — например, элемент табличной части,
            # который в OData представлен не объектом, а голым значением. Класс поля наследуется
            # от ключа, которым эта коллекция была найдена, иначе такое значение обходило бы
            # проверку насквозь — требование «обход доходит до значений внутри вложенных структур
            # и табличных частей» иначе не выполняется.
            класс = self._класс_поля(entity, field)
            return self._обработать_строку(
                значение, entity=entity, field=field, класс=класс, замаскированные=замаскированные
            )
        return значение

    def _класс_поля(self, entity: str, field: str) -> str | None:
        класс = self._policy.sensitivity_of(entity, field)
        if класс is None:
            класс = self._policy.custom_fields().get(field)
        if класс in ("keep", None, "scan"):
            return класс
        if класс in КЛАССЫ_НАЗВАНИЙ and self._mode != "identifiers+names":
            # Названия защищаются только на верхнем уровне (SPEC §6.2), но поле всё равно
            # сканируется: None ведёт к сканированию, keep отключил бы и его. В Description
            # контрагента вполне может лежать ИНН, и на уровне identifiers он обязан быть заменён.
            return None
        return класс

    def _обработать_строку(
        self, текст: str, *, entity: str, field: str, класс: str | None, замаскированные: list[str]
    ) -> str:
        if not текст:
            return текст
        if класс == "keep":
            return текст
        if класс and класс not in ("scan",) and (класс in CLASSES or класс.startswith("custom:")):
            замаскированные.append(field)
            return self._dictionary.token_for(
                класс, текст, base=self._base, entity=entity, field=field
            )

        обработанное = self._заменить_известные_названия(текст, entity=entity, field=field)
        обработанное = self._заменить_найденные_реквизиты(обработанное, entity=entity, field=field)
        if обработанное != текст:
            замаскированные.append(field)
        return обработанное

    def _заменить_найденные_реквизиты(self, текст: str, *, entity: str, field: str) -> str:
        if not self._policy.scan_free_text:
            return текст
        совпадения = scan_value(текст)
        if not совпадения:
            return текст
        куски, позиция = [], 0
        for совпадение in совпадения:
            куски.append(текст[позиция : совпадение.start])
            куски.append(
                self._dictionary.token_for(
                    совпадение.type, совпадение.value, base=self._base, entity=entity, field=field
                )
            )
            позиция = совпадение.end
        куски.append(текст[позиция:])
        return "".join(куски)

    def _заменить_известные_названия(self, текст: str, *, entity: str, field: str) -> str:
        """Слой 3 SPEC §6.5: известные варианты названий в любой строке."""
        if self._mode != "identifiers+names":
            return текст
        варианты = self._dictionary.name_variants()
        if not варианты:
            return текст
        нижний = текст.lower()
        for вариант in sorted(варианты, key=len, reverse=True):
            начало = нижний.find(вариант)
            if начало == -1:
                continue
            текст = текст[:начало] + варианты[вариант] + текст[начало + len(вариант) :]
            нижний = текст.lower()
        return текст
