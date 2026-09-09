"""Страж утечек: последний проход по сериализованному ответу любого тула (SPEC §6.8).

Инвариант, а не тест: ответ, в котором осталось известное словарю значение, наружу не выходит.
Работает независимо от политики базы и уровня — кроме off.
"""

from __future__ import annotations

import dataclasses
import re

from ahocorasick_rs import AhoCorasick, MatchKind

from odata1c.gate.dictionary import Dictionary

ЦИФРОВАЯ_ПОСЛЕДОВАТЕЛЬНОСТЬ = re.compile(r"\d[\d\s\-]{7,}\d")


@dataclasses.dataclass(slots=True)
class GuardResult:
    text: str
    replacements: list[dict]
    warnings: list[str]


class Guard:
    def __init__(self, dictionary: Dictionary) -> None:
        self._dictionary = dictionary
        self._revision = -1
        self._numbers: dict[str, str] = {}
        self._automaton: AhoCorasick | None = None
        self._variants: dict[str, str] = {}
        self.rebuild()

    def rebuild(self) -> None:
        """Пересобрать множество чисел и автомат названий по текущему словарю."""
        self._numbers = self._dictionary.number_tokens()
        self._variants = self._dictionary.name_variants()
        ключи = list(self._variants)
        self._automaton = AhoCorasick(ключи, matchkind=MatchKind.LeftmostLongest) if ключи else None
        self._revision = self._dictionary.revision()

    def check(self, serialized: str, *, mode: str) -> GuardResult:
        if mode == "off":
            return GuardResult(text=serialized, replacements=[], warnings=[])
        if self._revision != self._dictionary.revision():
            self.rebuild()

        замены: list[dict] = []
        текст = self._заменить_числа(serialized, замены)
        if mode == "identifiers+names":
            текст = self._заменить_названия(текст, замены)
        предупреждения = ["guard_replaced"] if замены else []
        return GuardResult(text=текст, replacements=замены, warnings=предупреждения)

    def _заменить_числа(self, текст: str, замены: list[dict]) -> str:
        if not self._numbers:
            return текст

        def подставить(совпадение: re.Match) -> str:
            найденное = совпадение.group()
            нормализованное = re.sub(r"\D", "", найденное)
            токен = self._numbers.get(нормализованное)
            if токен is None:
                return найденное
            замены.append({"value_hint": f"…{нормализованное[-4:]}", "token": токен})
            return токен

        return ЦИФРОВАЯ_ПОСЛЕДОВАТЕЛЬНОСТЬ.sub(подставить, текст)

    def _заменить_названия(self, текст: str, замены: list[dict]) -> str:
        if self._automaton is None:
            return текст
        нижний = текст.lower()
        совпадения = self._automaton.find_matches_as_indexes(нижний)
        if not совпадения:
            return текст
        куски, позиция = [], 0
        for _, начало, конец in совпадения:
            вариант = нижний[начало:конец]
            токен = self._variants.get(вариант)
            if токен is None or начало < позиция:
                continue
            куски.append(текст[позиция:начало])
            куски.append(токен)
            замены.append({"value_hint": вариант[:12], "token": токен})
            позиция = конец
        куски.append(текст[позиция:])
        return "".join(куски)
