"""Обратная подмена: токены модели → реальные значения перед отправкой в 1С (SPEC §6.7).

Точки входа: $filter, ключи в URL, params виртуальных таблиц и рецептов, тела create/update,
params действий, query у raw_get. Обратимость существует только внутри гейта.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from odata1c.gate.detectors import inn_valid, ogrn_valid, snils_valid
from odata1c.gate.dictionary import Dictionary
from odata1c.gate.filter_lexer import Token, lex_filter
from odata1c.gate.tokens import TOKEN_RE, find_tokens, is_partial_token, parse_token

ФУНКЦИИ_ПОДСТРОКИ = ("substringof", "startswith", "endswith")
ПРОВЕРКИ = {"inn": inn_valid, "ogrn": ogrn_valid, "snils": snils_valid}


class GateError(Exception):
    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


class Unmasker:
    def __init__(
        self, dictionary: Dictionary, *, base: str, field_class: Callable[[str, str], str | None]
    ) -> None:
        self._dictionary = dictionary
        self._base = base
        self._field_class = field_class

    def filter(self, expression: str, *, entity: str) -> str:
        лексемы = lex_filter(expression)
        переписанное = self._переписать_функции(лексемы, expression, entity=entity)
        if переписанное is not None:
            return переписанное

        куски: list[str] = []
        for лексема in лексемы:
            if лексема.kind != "string":
                куски.append(лексема.text)
                continue
            куски.append(self._подставить_в_литерал(лексема, лексемы, entity=entity))
        return "".join(куски)

    def value(self, text: str, *, entity: str, field: str) -> str:
        """Одно значение: параметр рецепта, элемент ключа, аргумент действия."""
        if not isinstance(text, str):
            return text
        целиком = parse_token(text)
        if целиком:
            return self._раскрыть(целиком, entity=entity, field=field)
        if is_partial_token(text):
            raise GateError(
                "token_partial",
                "в значении обрезанный токен; передайте токен целиком",
                "токены непрозрачны: их нельзя достраивать и обрезать",
            )
        if find_tokens(text):
            return self._подставить_внутри_текста(text, entity=entity, field=field)
        self._проверить_реальное_значение(text, entity=entity, field=field)
        return text

    def body(self, data, *, entity: str):
        if isinstance(data, dict):
            return {
                ключ: self._обойти_значение(значение, entity=entity, field=ключ)
                for ключ, значение in data.items()
            }
        if isinstance(data, list):
            return [self.body(элемент, entity=entity) for элемент in data]
        return data

    def key(self, value, *, entity: str):
        if isinstance(value, dict):
            return {
                ключ: self.value(часть, entity=entity, field=ключ)
                if isinstance(часть, str)
                else часть
                for ключ, часть in value.items()
            }
        return self.value(value, entity=entity, field="") if isinstance(value, str) else value

    def _обойти_значение(self, значение, *, entity: str, field: str):
        if isinstance(значение, str):
            return self.value(значение, entity=entity, field=field)
        if isinstance(значение, dict):
            return self.body(значение, entity=entity)
        if isinstance(значение, list):
            return [
                self._обойти_значение(элемент, entity=entity, field=field) for элемент in значение
            ]
        return значение

    def _переписать_функции(
        self, лексемы: list[Token], expression: str, *, entity: str
    ) -> str | None:
        """substringof/startswith/endswith с полным токеном → строгое равенство (SPEC §6.7)."""
        значимые = [лексема for лексема in лексемы if лексема.kind != "space"]
        if not значимые or значимые[0].kind != "identifier":
            return None
        имя = значимые[0].text.lower()
        if имя not in ФУНКЦИИ_ПОДСТРОКИ or len(значимые) < 6:
            return None
        аргументы = [
            лексема for лексема in значимые[1:] if лексема.kind in ("string", "identifier")
        ]
        строковые = [лексема for лексема in аргументы if лексема.kind == "string"]
        поля = [лексема for лексема in аргументы if лексема.kind == "identifier"]
        if len(строковые) != 1 or len(поля) != 1:
            return None
        целиком = parse_token(_снять_кавычки(строковые[0].text))
        if not целиком:
            return None
        поле = поля[0].text
        реальное = self._раскрыть(целиком, entity=entity, field=поле)
        return f"{поле} eq '{_экранировать(реальное)}'"

    def _подставить_в_литерал(self, лексема: Token, лексемы: list[Token], *, entity: str) -> str:
        содержимое = _снять_кавычки(лексема.text)
        поле = _поле_слева(лексемы, лексема)
        целиком = parse_token(содержимое)
        if целиком:
            return f"'{_экранировать(self._раскрыть(целиком, entity=entity, field=поле))}'"
        if is_partial_token(содержимое) or (
            TOKEN_RE.search(содержимое) and содержимое.strip() != содержимое
        ):
            raise GateError(
                "token_partial",
                f"в литерале «{содержимое}» токен использован как часть строки",
                "передайте токен целиком и сравнивайте через eq",
            )
        if find_tokens(содержимое):
            raise GateError(
                "token_partial",
                f"в литерале «{содержимое}» токен смешан с текстом",
                "передайте токен целиком и сравнивайте через eq",
            )
        self._проверить_реальное_значение(содержимое, entity=entity, field=поле)
        return лексема.text

    def _подставить_внутри_текста(self, текст: str, *, entity: str, field: str) -> str:
        куски, позиция = [], 0
        for начало, конец, класс, хвост in find_tokens(текст):
            куски.append(текст[позиция:начало])
            куски.append(self._раскрыть((класс, хвост), entity=entity, field=field))
            позиция = конец
        куски.append(текст[позиция:])
        return "".join(куски)

    def _раскрыть(self, разобранный: tuple[str, str], *, entity: str, field: str) -> str:
        класс, хвост = разобранный
        токен = f"[[{класс}:{хвост}]]"
        ожидаемый = self._field_class(entity, field) if field else None
        if ожидаемый and ожидаемый not in ("keep", "scan") and ожидаемый != класс:
            raise GateError(
                "token_type_mismatch",
                f"токен класса {класс} подставлен в поле {field} класса {ожидаемый}",
                "проверьте, из какого поля взят токен",
            )
        реальное = self._dictionary.reveal(токен, base=self._base, field=field or None)
        if реальное is None:
            raise GateError(
                "token_unknown",
                f"токен {токен} не найден в словаре",
                "токены непрозрачны: используйте только те, что пришли в ответах",
            )
        return реальное

    def _проверить_реальное_значение(self, значение: str, *, entity: str, field: str) -> None:
        """Реальное значение от модели (ИНН из промпта) проходит контрольную сумму (SPEC §6.7).

        Код ошибки — filter_syntax: отдельного кода для «значение не прошло контрольную сумму»
        в перечне SPEC §5.2 нет, а новые коды заводятся только через ADR с правкой §5.2.
        Смысл сохранён: запрос от модели синтаксически неприемлем и в 1С не уходит.
        """
        класс = self._field_class(entity, field) if field else None
        проверка = ПРОВЕРКИ.get(класс or "")
        if проверка is None or not значение:
            return
        цифры = re.sub(r"\D", "", значение)
        if цифры and not проверка(цифры):
            raise GateError(
                "filter_syntax",
                f"значение «{значение}» не проходит контрольную сумму класса {класс}",
                "проверьте значение, полученное от пользователя",
            )


def _снять_кавычки(литерал: str) -> str:
    return литерал[1:-1].replace("''", "'") if литерал.startswith("'") else литерал


def _экранировать(значение: str) -> str:
    return значение.replace("'", "''")


def _поле_слева(лексемы: list[Token], литерал: Token) -> str:
    """Имя поля, стоящее слева от литерала: нужно для выбора варианта и проверки класса."""
    предыдущие = [
        лексема
        for лексема in лексемы
        if лексема.end <= литерал.start and лексема.kind == "identifier"
    ]
    return предыдущие[-1].text if предыдущие else ""
