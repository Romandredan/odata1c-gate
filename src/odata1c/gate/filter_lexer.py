"""Лексер $filter OData v3 (SPEC §6.7).

Разбор нужен ровно для одного: отличить строковый литерал от имени поля, чтобы подставлять
реальные значения только внутрь литералов и не трогать структуру выражения.
"""

from __future__ import annotations

import dataclasses
import re

ОПЕРАТОРЫ = frozenset(
    {
        "eq",
        "ne",
        "gt",
        "ge",
        "lt",
        "le",
        "and",
        "or",
        "not",
        "add",
        "sub",
        "mul",
        "div",
        "mod",
        "asc",
        "desc",
    }
)
_ЧИСЛО = re.compile(r"\d+(?:\.\d+)?[dfmDFM]?")
_ИМЯ = re.compile(r"[A-Za-zА-Яа-яЁё_][\w./]*")


@dataclasses.dataclass(slots=True, frozen=True)
class Token:
    kind: str
    text: str
    start: int
    end: int


def lex_filter(expression: str) -> list[Token]:
    лексемы: list[Token] = []
    позиция, длина = 0, len(expression)

    while позиция < длина:
        символ = expression[позиция]

        if символ.isspace():
            начало = позиция
            while позиция < длина and expression[позиция].isspace():
                позиция += 1
            лексемы.append(Token("space", expression[начало:позиция], начало, позиция))
            continue

        if символ in "()":
            лексемы.append(Token("paren", символ, позиция, позиция + 1))
            позиция += 1
            continue

        if символ == ",":
            лексемы.append(Token("comma", символ, позиция, позиция + 1))
            позиция += 1
            continue

        if символ == "'":
            конец = _конец_строки(expression, позиция)
            лексемы.append(Token("string", expression[позиция:конец], позиция, конец))
            позиция = конец
            continue

        совпадение = _ИМЯ.match(expression, позиция)
        if совпадение:
            текст = совпадение.group()
            конец = совпадение.end()
            # guid'…' и datetime'…' — литералы с префиксом.
            if (
                конец < длина
                and expression[конец] == "'"
                and текст.lower() in ("guid", "datetime", "datetimeoffset", "time", "binary", "x")
            ):
                конец_литерала = _конец_строки(expression, конец)
                вид = "guid" if текст.lower() == "guid" else "datetime"
                лексемы.append(
                    Token(вид, expression[позиция:конец_литерала], позиция, конец_литерала)
                )
                позиция = конец_литерала
                continue
            вид = "operator" if текст.lower() in ОПЕРАТОРЫ else "identifier"
            лексемы.append(Token(вид, текст, позиция, конец))
            позиция = конец
            continue

        совпадение = _ЧИСЛО.match(expression, позиция)
        if совпадение:
            лексемы.append(Token("number", совпадение.group(), позиция, совпадение.end()))
            позиция = совпадение.end()
            continue

        лексемы.append(Token("operator", символ, позиция, позиция + 1))
        позиция += 1

    return лексемы


def _конец_строки(выражение: str, начало: int) -> int:
    """Найти закрывающую кавычку с учётом удвоения '' внутри литерала."""
    позиция = выражение.index("'", начало) + 1
    while позиция < len(выражение):
        if выражение[позиция] != "'":
            позиция += 1
            continue
        if позиция + 1 < len(выражение) and выражение[позиция + 1] == "'":
            позиция += 2
            continue
        return позиция + 1
    return len(выражение)
