"""Обратная подмена: токены модели → реальные значения перед отправкой в 1С (SPEC §6.7).

Точки входа: $filter, ключи в URL, params виртуальных таблиц и рецептов, тела create/update,
params действий, query у raw_get. Обратимость существует только внутри гейта.

Ревью задачи 7 (2026-09-09): поле, к которому относится строковый литерал внутри `$filter`,
определяется по СТРУКТУРЕ сравнения (что стоит по другую сторону оператора или какой аргумент
функции — идентификатор), а не по близости («последний идентификатор левее литерала» обходился
перестановкой операндов и открывал оракул подбора через `substringof`). Если поле определить не
удалось — запрос отклоняется, а не уходит в 1С непроверенным (Правка 1).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from odata1c.gate.detectors import inn_valid, ogrn_valid, snils_valid
from odata1c.gate.dictionary import НУМЕРУЕМЫЕ, Dictionary
from odata1c.gate.filter_lexer import Token, lex_filter
from odata1c.gate.tokens import TOKEN_RE, find_tokens, is_partial_token, parse_token

ФУНКЦИИ_ПОДСТРОКИ = ("substringof", "startswith", "endswith")
ПРОВЕРКИ = {"inn": inn_valid, "ogrn": ogrn_valid, "snils": snils_valid}
СРАВНЕНИЯ = frozenset({"eq", "ne", "gt", "ge", "lt", "le"})
# org/person — пользователь называет их сам, поиск по вхождению текста, который он написал,
# разрешён без токена (SPEC §6.5). Для остальных защищаемых классов (номера, коды, документы)
# модель никогда не видит реальное значение и не может знать, что искать частью строки —
# единственная причина такого запроса — подбор по символу (Правка 1, «закрыть оракул»).
ИМЕНОВАННЫЕ_КЛАССЫ = frozenset(НУМЕРУЕМЫЕ)


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
        значимые = [лексема for лексема in lex_filter(expression) if лексема.kind != "space"]
        вызовы = self._вызовы_функций(значимые)

        занято: set[int] = set()
        for вызов in вызовы:
            занято.update(range(вызов["start"], вызов["close"] + 1))

        поля_сравнений = self._поля_сравнений(значимые, занято)

        сегменты: list[tuple[int, int, str]] = []
        for вызов in вызовы:
            сегмент = self._обработать_вызов(вызов, значимые, entity=entity)
            if сегмент is not None:
                сегменты.append(сегмент)

        for индекс, поле in поля_сравнений.items():
            сегмент = self._обработать_сравнение(значимые[индекс], поле, entity=entity)
            if сегмент is not None:
                сегменты.append(сегмент)

        обработанные = {
            вызов["literal_idx"] for вызов in вызовы if вызов["literal_idx"] is not None
        }
        обработанные |= set(поля_сравнений)
        for индекс, токен in enumerate(значимые):
            if токен.kind != "string" or индекс in обработанные or индекс in занято:
                continue
            raise GateError(
                "filter_syntax",
                f"не удалось определить поле для литерала «{токен.text}»: структура сравнения "
                "не распознана",
                "сравнивайте литерал с полем через eq/ne/gt/ge/lt/le или через substringof/"
                "startswith/endswith",
            )

        return _собрать(expression, сегменты)

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
        # Верхний уровень без словаря (голая строка или список — например, список значений
        # табличной части без обёртки-объекта) обходится тем же путём, что и вложенное значение
        # под пустым именем поля: раньше здесь была отдельная ветка для list, которая рекурсивно
        # звала body() для каждого элемента, а body() строку саму по себе не обрабатывал вовсе —
        # токен в такой строке уходил в 1С дословно, обрезанный не отклонялся (Правка 4, ревью
        # задачи 7). `_обойти_значение` уже умеет строку/словарь/список/прочее в одном месте.
        return self._обойти_значение(data, entity=entity, field="")

    def key(self, value, *, entity: str):
        if isinstance(value, dict):
            результат = {}
            for ключ, часть in value.items():
                # Испорченный токен, случайно оказавшийся в ИМЕНИ поля составного ключа —
                # подставлять там нечего (это не значение), но он всё равно обязан вызвать
                # отказ, а не пройти незамеченным (Правка 5, ревью задачи 7).
                if is_partial_token(ключ):
                    raise GateError(
                        "token_partial",
                        f"в имени поля составного ключа «{ключ}» обрезанный токен",
                        "токены непрозрачны: их нельзя достраивать и обрезать",
                    )
                результат[ключ] = (
                    self.value(часть, entity=entity, field=ключ)
                    if isinstance(часть, str)
                    else часть
                )
            return результат
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

    # ------------------------------------------------------------------
    # Разбор структуры $filter (Правка 1, 3 — ревью задачи 7)
    # ------------------------------------------------------------------

    def _вызовы_функций(self, значимые: list[Token]) -> list[dict]:
        """Найти вызовы substringof/startswith/endswith: поле — аргумент-идентификатор,
        образец — аргумент-строка, независимо от того, какой из них идёт первым."""
        вызовы: list[dict] = []
        i, n = 0, len(значимые)
        while i < n:
            токен = значимые[i]
            if (
                токен.kind == "identifier"
                and токен.text.lower() in ФУНКЦИИ_ПОДСТРОКИ
                and i + 1 < n
                and значимые[i + 1].kind == "paren"
                and значимые[i + 1].text == "("
            ):
                открывающая = i + 1
                глубина = 1
                j = открывающая + 1
                while j < n and глубина > 0:
                    if значимые[j].kind == "paren":
                        глубина += 1 if значимые[j].text == "(" else -1
                    j += 1
                закрывающая = j - 1
                внутренние = list(
                    enumerate(значимые[открывающая + 1 : закрывающая], start=открывающая + 1)
                )
                строки = [idx for idx, t in внутренние if t.kind == "string"]
                поля = [idx for idx, t in внутренние if t.kind == "identifier"]
                вызовы.append(
                    {
                        "name": токен.text.lower(),
                        "start": i,
                        "close": закрывающая,
                        "literal_idx": строки[0] if len(строки) == 1 else None,
                        "field_idx": поля[0] if len(поля) == 1 else None,
                    }
                )
                i = закрывающая + 1
                continue
            i += 1
        return вызовы

    def _поля_сравнений(self, значимые: list[Token], занято: set[int]) -> dict[int, str]:
        """Поле для литерала в бинарном сравнении: идентификатор по другую сторону оператора
        от литерала, в любом порядке (Правка 1)."""
        поля: dict[int, str] = {}
        for i in range(len(значимые) - 2):
            if i in занято or i + 1 in занято or i + 2 in занято:
                continue
            левый, оператор, правый = значимые[i], значимые[i + 1], значимые[i + 2]
            if оператор.kind != "operator" or оператор.text.lower() not in СРАВНЕНИЯ:
                continue
            if левый.kind == "identifier" and правый.kind == "string":
                поля[i + 2] = левый.text
            elif левый.kind == "string" and правый.kind == "identifier":
                поля[i] = правый.text
        return поля

    def _обработать_вызов(
        self, вызов: dict, значимые: list[Token], *, entity: str
    ) -> tuple[int, int, str] | None:
        if вызов["literal_idx"] is None:
            return None  # нет строкового аргумента — нечего подставлять и нечем злоупотребить
        if вызов["field_idx"] is None:
            raise GateError(
                "filter_syntax",
                f"не удалось определить поле для {вызов['name']}(...): нужен ровно один "
                "аргумент-идентификатор рядом с образцом",
                "функция поиска подстроки сравнивает образец с одним полем",
            )
        литерал = значимые[вызов["literal_idx"]]
        поле = значимые[вызов["field_idx"]].text
        содержимое = _снять_кавычки(литерал.text)
        целиком = parse_token(содержимое)
        if целиком:
            реальное = self._раскрыть(целиком, entity=entity, field=поле)
            замена = f"{поле} eq '{_экранировать(реальное)}'"
            return (значимые[вызов["start"]].start, значимые[вызов["close"]].end, замена)
        self._проверить_литерал_текстом(содержимое, entity=entity, field=поле, оракул=True)
        return None  # реальный текст на разрешённом классе — вызов остаётся как есть

    def _обработать_сравнение(
        self, литерал: Token, поле: str, *, entity: str
    ) -> tuple[int, int, str] | None:
        содержимое = _снять_кавычки(литерал.text)
        целиком = parse_token(содержимое)
        if целиком:
            замена = f"'{_экранировать(self._раскрыть(целиком, entity=entity, field=поле))}'"
            return (литерал.start, литерал.end, замена)
        self._проверить_литерал_текстом(содержимое, entity=entity, field=поле, оракул=False)
        return None

    def _проверить_литерал_текстом(
        self, содержимое: str, *, entity: str, field: str, оракул: bool
    ) -> None:
        """Литерал — не целый токен: закрыть смешение токена с текстом и (внутри функции
        поиска подстроки на защищаемом классе) закрыть оракул подбора по символу (Правка 1)."""
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
        if оракул:
            класс = self._field_class(entity, field) if field else None
            if класс and класс not in ("keep", "scan") and класс not in ИМЕНОВАННЫЕ_КЛАССЫ:
                raise GateError(
                    "filter_syntax",
                    f"поиск по вхождению подстроки запрещён для поля «{field}» класса "
                    f"{класс}: образец не является токеном целиком",
                    "модель не знает защищённое значение — передайте токен целиком, запрос "
                    "перепишется в точное сравнение",
                )
        self._проверить_реальное_значение(содержимое, entity=entity, field=field)

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
        Смысл сохранён: запрос от модели синтаксически неприемлем и в 1С не уходит. Тот же код
        переиспользован для «поле не определено» и «оракул подбора» (Правка 1) — по той же
        причине: это не новый смысл ошибки, а тот же самый «запрос от модели неприемлем».
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


def _собрать(expression: str, сегменты: list[tuple[int, int, str]]) -> str:
    """Склеить исходное выражение с точечными заменами (Правка 3): вне сегментов текст остаётся
    посимвольно как был — переписывание одного вызова не должно задевать соседние условия."""
    куски: list[str] = []
    позиция = 0
    for начало, конец, замена in sorted(сегменты, key=lambda сегмент: сегмент[0]):
        куски.append(expression[позиция:начало])
        куски.append(замена)
        позиция = конец
    куски.append(expression[позиция:])
    return "".join(куски)
