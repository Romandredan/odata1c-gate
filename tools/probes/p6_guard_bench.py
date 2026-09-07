"""Проба P6 (SPEC §15.6): стоимость прохода стража и пересборки автомата.

Замеряет: сборку автомата на 100 000 вариантов названий, поиск в ответе размером 120 000 символов
(предел result_chars из SPEC §3.3), пересборку после добавления одного значения и проверку
цифровых последовательностей по множеству нормализованных значений словаря.
"""
import random
import re
import string
import time

from ahocorasick_rs import AhoCorasick, MatchKind

DICT_SIZE = 100_000
RESPONSE_CHARS = 120_000
DIGITS_RE = re.compile(r"\d[\d\s\-]{7,}\d")


def make_names(n: int) -> list[str]:
    random.seed(20260907)
    parts = ["ромашка", "вектор", "альфа", "строй", "торг", "сервис", "групп", "инвест"]
    return [f"{random.choice(parts)}-{random.choice(parts)}-{i}" for i in range(n)]


def make_numbers(n: int) -> set[str]:
    random.seed(20260908)
    return {"".join(random.choices(string.digits, k=random.choice((10, 12, 20)))) for _ in range(n)}


def make_response(names: list[str], numbers: set[str]) -> str:
    """Ответ с редкими вхождениями: типичный случай — страж ничего не находит."""
    random.seed(20260909)
    filler = "оплата по счёту от 2026-09-07 на сумму 145 200,00 руб. без НДС; "
    text = filler * (RESPONSE_CHARS // len(filler))
    return text + random.choice(names) + " " + random.choice(list(numbers))


def bench(label: str, fn, repeats: int = 5) -> float:
    best = min(_timed(fn) for _ in range(repeats))
    print(f"{label}: {best * 1000:.1f} мс")
    return best


def _timed(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def main() -> None:
    names = make_names(DICT_SIZE)
    numbers = make_numbers(DICT_SIZE)
    response = make_response(names, numbers)
    print(f"словарь: {len(names)} названий, {len(numbers)} значений; ответ: {len(response)} символов")

    automaton: AhoCorasick | None = None

    def build() -> None:
        nonlocal automaton
        automaton = AhoCorasick(names, matchkind=MatchKind.LeftmostLongest)

    bench("сборка автомата", build)
    bench("поиск названий в ответе", lambda: automaton.find_matches_as_indexes(response.lower()))
    bench("пересборка после добавления одного названия",
          lambda: AhoCorasick(names + ["новое-название-1"], matchkind=MatchKind.LeftmostLongest))

    def scan_numbers() -> None:
        for match in DIGITS_RE.finditer(response):
            normalized = re.sub(r"\D", "", match.group())
            if normalized in numbers:
                pass

    bench("сверка цифровых последовательностей", scan_numbers)


if __name__ == "__main__":
    main()
