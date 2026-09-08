"""Поиск реквизитов в произвольной строке: контрольные суммы, контекст, порядок (SPEC §6.4).

Правило ложных срабатываний: значение, не подтверждённое контрольной суммой или контекстом,
остаётся как есть. Пропуск менее вреден, чем токенизация половины номеров документов.
"""

from __future__ import annotations

import dataclasses
import re

SCAN_ORDER: tuple[str, ...] = (
    "email",
    "phone",
    "iban",
    "card",
    "acc",
    "corr",
    "ogrn",
    "inn",
    "snils",
    "doc",
    "kpp",
)

ВЕСА_ИНН10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
ВЕСА_ИНН12_1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
ВЕСА_ИНН12_2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
ПРЕФИКСЫ_СЧЕТА = (
    "401",
    "402",
    "403",
    "404",
    "405",
    "406",
    "407",
    "408",
    "420",
    "421",
    "422",
    "423",
    "424",
    "425",
    "426",
)
ПРЕФИКСЫ_СЧЕТА_5 = ("40102", "03100", "03212", "03214", "03222", "03224", "03231", "03241")

ШАБЛОНЫ = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b"),
    "phone": re.compile(
        r"(?<![\d-])(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}"
        r"(?![\d-])"
        r"|(?<![\d-])\+\d{7,15}(?![\d-])"
    ),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "card": re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])"),
    "acc": re.compile(r"(?<![\d-])\d{20}(?![\d-])"),
    "corr": re.compile(r"(?<![\d-])301\d{17}(?![\d-])"),
    "ogrn": re.compile(r"(?<![\d-])\d{13}(?:\d{2})?(?![\d-])"),
    "inn": re.compile(r"(?<![\d-])\d{10}(?:\d{2})?(?![\d-])"),
    "snils": re.compile(r"(?<![\d-])\d{3}[- ]?\d{3}[- ]?\d{3}[- ]?\d{2}(?![\d-])"),
    "doc": re.compile(r"(?<![\d-])\d{2}\s?\d{2}\s?\d{6}(?![\d-])"),
    "kpp": re.compile(r"(?<![\dA-Z-])\d{4}[\dA-Z]{2}\d{3}(?![\dA-Z-])"),
}

КОНТЕКСТ = {
    "inn": re.compile(r"инн", re.IGNORECASE),
    "kpp": re.compile(r"кпп|инн", re.IGNORECASE),
    "doc": re.compile(r"паспорт|серия|выдан|удостоверени", re.IGNORECASE),
}

# GUID и даты не трогаем ни при каких условиях (инвариант 6).
ИСКЛЮЧЕНИЯ = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    r"|\b\d{4}-\d{2}-\d{2}(?:T[\d:.]+)?\b"
    r"|\b\d{2}\.\d{2}\.\d{4}\b"
    r"|(?:ОКПО|ОКВЭД|ОКТМО|ОКАТО|ОКОПФ|SWIFT)[\s:№]*[\w.]+",
)


@dataclasses.dataclass(slots=True, frozen=True)
class Match:
    start: int
    end: int
    type: str
    value: str


def inn_valid(digits: str) -> bool:
    if not digits.isdigit():
        return False
    цифры = [int(символ) for символ in digits]
    if len(digits) == 10:
        return _контроль(цифры[:9], ВЕСА_ИНН10) == цифры[9]
    if len(digits) == 12:
        return (
            _контроль(цифры[:10], ВЕСА_ИНН12_1) == цифры[10]
            and _контроль(цифры[:11], ВЕСА_ИНН12_2) == цифры[11]
        )
    return False


def ogrn_valid(digits: str) -> bool:
    if not digits.isdigit():
        return False
    if len(digits) == 13:
        return int(digits[:12]) % 11 % 10 == int(digits[12])
    if len(digits) == 15:
        return int(digits[:14]) % 13 % 10 == int(digits[14])
    return False


def snils_valid(digits: str) -> bool:
    if not digits.isdigit() or len(digits) != 11:
        return False
    сумма = sum(int(цифра) * (9 - позиция) for позиция, цифра in enumerate(digits[:9]))
    контроль = сумма if сумма < 100 else (0 if сумма in (100, 101) else сумма % 101 % 100)
    return контроль == int(digits[9:])


def luhn_valid(digits: str) -> bool:
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    сумма = 0
    for позиция, символ in enumerate(reversed(digits)):
        цифра = int(символ)
        if позиция % 2 == 1:
            цифра *= 2
            цифра = цифра - 9 if цифра > 9 else цифра
        сумма += цифра
    return сумма % 10 == 0


def iban_valid(value: str) -> bool:
    сжатое = re.sub(r"\s", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", сжатое):
        return False
    переставленное = сжатое[4:] + сжатое[:4]
    число = "".join(
        str(ord(символ) - 55) if символ.isalpha() else символ for символ in переставленное
    )
    return int(число) % 97 == 1


def account_prefix_known(digits: str) -> bool:
    """Первые разряды счёта из групп, перечисленных в SPEC §6.4."""
    if len(digits) != 20 or not digits.isdigit():
        return False
    return digits[:3] in ПРЕФИКСЫ_СЧЕТА or digits[:5] in ПРЕФИКСЫ_СЧЕТА_5


def scan_value(text: str, *, context_window: int = 40) -> list[Match]:
    """Найти реквизиты в строке. Порядок SCAN_ORDER, перекрытия отбрасываются."""
    запрещённые = [
        (совпадение.start(), совпадение.end()) for совпадение in ИСКЛЮЧЕНИЯ.finditer(text)
    ]
    найденное: list[Match] = []
    занято: list[tuple[int, int]] = list(запрещённые)

    for класс in SCAN_ORDER:
        for совпадение in ШАБЛОНЫ[класс].finditer(text):
            начало, конец = совпадение.start(), совпадение.end()
            if _перекрывается(начало, конец, занято):
                continue
            значение = совпадение.group()
            if not _подтверждено(класс, значение, text, начало, context_window):
                continue
            занято.append((начало, конец))
            найденное.append(Match(start=начало, end=конец, type=класс, value=значение))

    найденное.sort(key=lambda совпадение: совпадение.start)
    return найденное


def _подтверждено(класс: str, значение: str, текст: str, начало: int, окно: int) -> bool:
    цифры = re.sub(r"\D", "", значение)
    if класс == "inn":
        if not inn_valid(цифры):
            return False
        # 10-значный ИНН неотличим от номера документа: требуется слово «ИНН» рядом.
        return len(цифры) == 12 or _есть_контекст("inn", текст, начало, окно=20)
    if класс == "ogrn":
        return ogrn_valid(цифры)
    if класс == "snils":
        return snils_valid(цифры)
    if класс == "card":
        return luhn_valid(цифры)
    if класс == "iban":
        return iban_valid(значение)
    if класс == "acc":
        return account_prefix_known(цифры)
    if класс == "corr":
        return len(цифры) == 20 and цифры.startswith("301")
    if класс in ("doc", "kpp"):
        return _есть_контекст(класс, текст, начало, окно)
    return True


def _есть_контекст(класс: str, текст: str, начало: int, окно: int) -> bool:
    шаблон = КОНТЕКСТ.get(класс)
    if шаблон is None:
        return True
    слева = текст[max(0, начало - окно) : начало]
    return bool(шаблон.search(слева))


def _перекрывается(начало: int, конец: int, занято: list[tuple[int, int]]) -> bool:
    return any(
        начало < чужой_конец and чужое_начало < конец for чужое_начало, чужой_конец in занято
    )


def _контроль(цифры: list[int], веса: tuple[int, ...]) -> int:
    return sum(цифра * вес for цифра, вес in zip(цифры, веса, strict=True)) % 11 % 10
