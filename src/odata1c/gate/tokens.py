"""Токены гейта: единая грамматика [[type:tail]], нормализация, детерминированный хвост.

SPEC §6.3. Для реквизитов хвост — HMAC-SHA256 от секрета и нормализованного значения,
записанный в Crockford base32 (без I, L, O, U — не спутать с цифрами). Для названий и ФИО хвост —
порядковый номер из словаря, его выдаёт словарь (задача 3), а не этот модуль.
"""

from __future__ import annotations

import hmac
import re

# Crockford base32: цифры и буквы без I, L, O, U.
АЛФАВИТ = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

CLASSES = frozenset(
    {
        "inn",
        "kpp",
        "ogrn",
        "acc",
        "corr",
        "bic",
        "iban",
        "card",
        "snils",
        "doc",
        "phone",
        "email",
        "dob",
        "addr",
        "org",
        "person",
        "keep",
    }
)

TOKEN_RE = re.compile(r"\[\[(?P<type>[a-z][a-z0-9_:]{0,31}):(?P<tail>[0-9A-Z]{1,16})\]\]")
_ЦЕЛИКОМ = re.compile(rf"^{TOKEN_RE.pattern}$")
_ОБРЕЗОК = re.compile(
    r"\[\[[a-z0-9_:]*:?[0-9A-Z]*$"
    r"|^[a-zA-Z0-9_:]*\]\]"
    r"|\[\[[a-z0-9_:]*:\]\]"
    r"|\[\[:"
    # Испорченный токен: хвост длиннее допустимых 16 символов — TOKEN_RE такой не берёт ни
    # целиком, ни частично, а такое значение обязано вызывать отказ, а не проходить молча.
    r"|\[\[[a-z][a-z0-9_:]*:[0-9A-Z]{17,}\]\]"
)

ТОЛЬКО_ЦИФРЫ = ("inn", "kpp", "ogrn", "acc", "corr", "bic", "snils", "card", "phone", "doc")


def normalize_value(type_: str, value: str) -> str:
    """Нормализация перед хэшированием (SPEC §6.3).

    Поправка (amended, 2026-09-09): для `org`/`person` нормализация приводит и к нижнему
    регистру, а не только схлопывает пробелы — иначе «ООО Ромашка» и «ооо ромашка» (одно и то
    же юридическое лицо) получали два разных токена, что нарушает основное свойство словаря:
    одно значение с разным написанием — один токен. Написание с исходным регистром при этом не
    теряется: словарь (`gate/dictionary.py`) хранит его отдельно, в вариантах по базе и полю, и
    именно его возвращает `reveal()` при обратной подмене.
    """
    if type_ in ТОЛЬКО_ЦИФРЫ:
        return re.sub(r"\D", "", value)
    if type_ == "email":
        return value.strip().lower()
    if type_ == "iban":
        return re.sub(r"\s", "", value).upper()
    if type_ == "dob":
        return value.strip()[:10]
    if type_ == "addr":
        return " ".join(value.split())
    if type_ in ("org", "person"):
        return " ".join(value.split()).lower()
    return value.strip()


def make_token(secret: bytes, type_: str, normalized: str, tail_length: int = 10) -> str:
    """Токен реквизита: тип, нулевой байт, нормализованное значение под HMAC (SPEC §6.3)."""
    подпись = hmac.new(
        secret, type_.encode("utf-8") + b"\x00" + normalized.encode("utf-8"), "sha256"
    ).digest()
    return f"[[{type_}:{_в_base32(подпись, tail_length)}]]"


def parse_token(text: str) -> tuple[str, str] | None:
    """Класс и хвост, если строка целиком является токеном."""
    совпадение = _ЦЕЛИКОМ.match(text.strip())
    return (совпадение["type"], совпадение["tail"]) if совпадение else None


def find_tokens(text: str) -> list[tuple[int, int, str, str]]:
    """Вхождения токенов: начало, конец, класс, хвост."""
    return [
        (совпадение.start(), совпадение.end(), совпадение["type"], совпадение["tail"])
        for совпадение in TOKEN_RE.finditer(text)
    ]


def is_partial_token(text: str) -> bool:
    """Признак обрезанного или испорченного токена: модель не должна их достраивать (SPEC §6.7).

    Целый токен в строке не отменяет проверку остальной строки: сначала из текста удаляются
    все найденные целые токены, и на обрезок проверяется то, что осталось. Иначе строка из
    целого и обрезанного токена (например, в выражении отбора из нескольких условий) прошла бы
    как чистая — целый токен «прикрывал» бы остаток строки.
    """
    остаток = TOKEN_RE.sub("", text)
    return bool(_ОБРЕЗОК.search(остаток))


def _в_base32(данные: bytes, длина: int) -> str:
    число = int.from_bytes(данные, "big")
    символы = []
    for _ in range(длина):
        символы.append(АЛФАВИТ[число % 32])
        число //= 32
    return "".join(reversed(символы))
