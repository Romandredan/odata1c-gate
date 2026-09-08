"""Токены: нормализация, детерминированность, формат, разбор (SPEC §6.3)."""

import pytest

from odata1c.gate.tokens import (
    find_tokens,
    is_partial_token,
    make_token,
    normalize_value,
    parse_token,
)

СЕКРЕТ = "тридцать два байта секрета для проб!!".encode()


@pytest.mark.parametrize(
    ("класс", "исходное", "нормализованное"),
    [
        ("inn", "7707 083893", "7707083893"),
        ("acc", "40702810-900000000001", "40702810900000000001"),
        ("phone", "+7 (495) 123-45-67", "74951234567"),
        ("email", "  Ivan.Petrov@Example.COM ", "ivan.petrov@example.com"),
        ("dob", "1980-05-17T00:00:00", "1980-05-17"),
        ("addr", "  г. Москва,   ул. Тверская,  1 ", "г. Москва, ул. Тверская, 1"),
        ("doc", "45 08 123456", "4508123456"),
    ],
)
def test_нормализация_по_классу(класс, исходное, нормализованное):
    assert normalize_value(класс, исходное) == нормализованное


def test_токен_детерминирован():
    первый = make_token(СЕКРЕТ, "inn", "7707083893")
    второй = make_token(СЕКРЕТ, "inn", "7707083893")
    assert первый == второй


def test_токен_зависит_от_секрета():
    assert make_token(СЕКРЕТ, "inn", "7707083893") != make_token(
        "другой секрет".encode(), "inn", "7707083893"
    )


def test_токен_зависит_от_класса():
    assert make_token(СЕКРЕТ, "inn", "7707083893") != make_token(СЕКРЕТ, "kpp", "7707083893")


def test_формат_токена():
    токен = make_token(СЕКРЕТ, "acc", "40702810900000000001")
    assert токен.startswith("[[acc:")
    assert токен.endswith("]]")
    хвост = токен[len("[[acc:") : -2]
    assert len(хвост) == 10
    assert set(хвост) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")  # Crockford base32


def test_удлинение_хвоста_при_коллизии():
    """Хвост строится как младшие разряды одного числа (см. _в_base32): при увеличении
    tail_length добавляются новые разряды СЛЕВА, а исходные младшие разряды остаются
    неизменным суффиксом справа. Проверено фактическим прогоном: короткий хвост
    "DG3C6ZJMW0" целиком является суффиксом длинного "ETZJX9DG3C6ZJMW0"."""
    короткий = make_token(СЕКРЕТ, "inn", "7707083893")
    длинный = make_token(СЕКРЕТ, "inn", "7707083893", tail_length=16)
    assert len(длинный) == len(короткий) + 6
    короткий_хвост = короткий[len("[[inn:") : -2]
    длинный_хвост = длинный[len("[[inn:") : -2]
    assert длинный_хвост.endswith(короткий_хвост)  # младшие разряды хвоста не меняются


def test_токен_не_похож_на_реальное_значение():
    """Не format-preserving: токен нельзя спутать с ИНН (SPEC §6.1 п. 3)."""
    хвост = make_token(СЕКРЕТ, "inn", "7707083893")[len("[[inn:") : -2]
    assert not хвост.isdigit()


def test_разбор_целого_токена():
    assert parse_token("[[org:17]]") == ("org", "17")
    assert parse_token("[[inn:M4T2Q9XZ7K]]") == ("inn", "M4T2Q9XZ7K")


def test_разбор_не_токена():
    assert parse_token("ООО Ромашка") is None
    assert parse_token("[[org:17]] и ещё текст") is None


def test_поиск_токенов_в_тексте():
    найдено = find_tokens("оплата от [[org:17]] по счёту [[acc:M4T2Q9XZ7K]]")
    assert [(класс, хвост) for _, _, класс, хвост in найдено] == [
        ("org", "17"),
        ("acc", "M4T2Q9XZ7K"),
    ]


@pytest.mark.parametrize("текст", ["[[inn:M4T2Q9XZ", "inn:M4T2Q9XZ7K]]", "[[inn:]]", "[[:M4T2]]"])
def test_обрезанный_токен_распознан(текст):
    assert is_partial_token(текст) is True


def test_целый_токен_не_считается_обрезанным():
    assert is_partial_token("[[inn:M4T2Q9XZ7K]]") is False
