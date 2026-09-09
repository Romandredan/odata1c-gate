"""Лексер $filter OData v3: строки, guid, datetime, функции (SPEC §6.7)."""

from odata1c.gate.filter_lexer import lex_filter


def виды(выражение):
    return [
        (лексема.kind, лексема.text) for лексема in lex_filter(выражение) if лексема.kind != "space"
    ]


def test_простое_сравнение():
    assert виды("ИНН eq '7707083893'") == [
        ("identifier", "ИНН"),
        ("operator", "eq"),
        ("string", "'7707083893'"),
    ]


def test_экранированная_кавычка_внутри_строки():
    лексемы = виды("Description eq 'ООО ''Ромашка'''")
    assert лексемы[-1] == ("string", "'ООО ''Ромашка'''")


def test_guid_литерал():
    лексемы = виды("Контрагент_Key eq guid'8f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8'")
    assert лексемы[-1][0] == "guid"


def test_datetime_литерал():
    лексемы = виды("Date ge datetime'2026-09-07T00:00:00'")
    assert лексемы[-1][0] == "datetime"


def test_функция_подстроки():
    лексемы = виды("substringof('Ромашка', Description)")
    assert лексемы[0] == ("identifier", "substringof")
    assert ("string", "'Ромашка'") in лексемы


def test_логические_операторы_и_скобки():
    лексемы = виды("(A eq 1) and (B eq 2)")
    assert ("operator", "and") in лексемы
    assert ("paren", "(") in лексемы


def test_числа():
    assert ("number", "145200.5") in виды("Сумма gt 145200.5")


def test_позиции_лексем_точные():
    выражение = "ИНН eq '7707083893'"
    строковая = next(лексема for лексема in lex_filter(выражение) if лексема.kind == "string")
    assert выражение[строковая.start : строковая.end] == "'7707083893'"
