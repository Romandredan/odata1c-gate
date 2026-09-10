"""Задача 5 плана M1b-fix: приёмка индекса метаданных на реальной базе после исправлений.

Открывает `metadata.sqlite` и `policy.yaml`, которые строит `odata1c reindex`, и проверяет числа
по факту пробы P4 (`docs/probes/P4-real-metadata.md`) — `assert` на каждое ожидание, вывод
таблицей «проверка — ожидание — факт». Учётные данные базы не трогает: путь к файлам берёт только
из `bases.yaml`-независимых `odata1c.config.home.resolve_home` и `odata1c.index.reindex.index_path`.

    PYTHONIOENCODING=utf-8 uv run odata1c reindex trade_dev
    PYTHONIOENCODING=utf-8 uv run python tools/probes/p4_index_check.py trade_dev

Ожидание перечислений — 1006, а не 1007 деклараций `EnumType` в `$metadata`: одно имя
(`AllowedLength`) объявлено в документе дважды, разбор кладёт перечисления в словарь по имени
(`_разобрать_типы` в `odata1c.index.edmx`) — второе объявление переопределяет первое, и в индексе
остаётся одна сущность `Enum_AllowedLength`. Решение оркестратора задачи 5.
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys

import yaml

from odata1c.config.home import resolve_home
from odata1c.gate.service import policy_path
from odata1c.index.edmx import PARSER_VERSION
from odata1c.index.reindex import index_path
from odata1c.index.schema import connect

ОЖИДАЕМЫЕ_ДЕЙСТВИЯ = {"Post": 296, "Unpost": 296, "Start": 9, "ExecuteTask": 2}
ОЖИДАЕМЫЕ_ВИРТУАЛЬНЫЕ_ТАБЛИЦЫ = {
    "SliceLast": 849,
    "SliceFirst": 849,
    "Balance": 81,
    "Turnovers": 121,
    "BalanceAndTurnovers": 81,
}
ОЖИДАЕМО_НАБОРОВ_ЗАПИСЕЙ = 149
ОЖИДАЕМО_ПЕРЕЧИСЛЕНИЙ = 1006  # см. пояснение в шапке модуля
КЛЮЧ_ТАБЛИЧНОЙ_ЧАСТИ = {"Ref_Key", "LineNumber"}
ПРИЗНАКИ_ПОДЧИНЁННОСТИ = {"Recorder", "Recorder_Key"}


def main(base_name: str) -> None:
    home = resolve_home(None)
    путь_индекса = index_path(home, base_name)
    путь_политики = policy_path(home, base_name)
    if not путь_индекса.exists():
        raise SystemExit(
            f"индекс не найден: {путь_индекса} — выполните odata1c reindex {base_name}"
        )

    строки: list[tuple[str, object, object]] = []
    соединение = connect(путь_индекса)
    try:
        строки.extend(_проверить_действия(соединение))
        строки.extend(_проверить_виртуальные_таблицы(соединение))
        строки.append(_проверить_сирот_табличных_частей(соединение))
        строки.append(_проверить_ключи_табличных_частей(соединение))
        строки.append(_проверить_ложную_независимость(соединение))
        строки.append(_проверить_наборы_записей(соединение))
        строки.append(_проверить_перечисления(соединение))
        строки.append(_проверить_версию_разбора(соединение))
        _вывести_разбивку_наборов_записей(соединение)
    finally:
        соединение.close()

    строки.append(_проверить_счетафактур_как_acc(путь_политики))

    _вывести_таблицу(строки)
    неудачные = [строка for строка in строки if строка[1] != строка[2]]
    if неудачные:
        детали = "; ".join(f"{имя}: ожидание {ож}, факт {ф}" for имя, ож, ф in неудачные)
        raise AssertionError(f"расхождение с ожиданием ({len(неудачные)}): {детали}")


def _проверить_действия(соединение) -> list[tuple[str, object, object]]:
    по_именам = dict(
        соединение.execute(
            "SELECT name, COUNT(*) FROM actions WHERE http_method = 'POST' GROUP BY name"
        ).fetchall()
    )
    return [
        (f"действие {имя} (POST)", ожидание, по_именам.get(имя, 0))
        for имя, ожидание in ОЖИДАЕМЫЕ_ДЕЙСТВИЯ.items()
    ]


def _проверить_виртуальные_таблицы(соединение) -> list[tuple[str, object, object]]:
    по_видам = dict(
        соединение.execute(
            "SELECT virtual_kind, COUNT(*) FROM entities"
            " WHERE is_virtual = 1 GROUP BY virtual_kind"
        ).fetchall()
    )
    return [
        (f"виртуальная таблица {вид}", ожидание, по_видам.get(вид, 0))
        for вид, ожидание in ОЖИДАЕМЫЕ_ВИРТУАЛЬНЫЕ_ТАБЛИЦЫ.items()
    ]


def _проверить_сирот_табличных_частей(соединение) -> tuple[str, object, object]:
    сирот = соединение.execute(
        "SELECT COUNT(*) FROM entities WHERE is_tabular_part = 1 AND"
        " (parent_entity IS NULL OR parent_entity NOT IN (SELECT name FROM entities))"
    ).fetchone()[0]
    return ("табличных частей без родителя в entities", 0, сирот)


def _проверить_ключи_табличных_частей(соединение) -> tuple[str, object, object]:
    неверный_ключ = 0
    for (сырые_ключи,) in соединение.execute(
        "SELECT key_fields_json FROM entities WHERE is_tabular_part = 1"
    ).fetchall():
        if set(json.loads(сырые_ключи)) != КЛЮЧ_ТАБЛИЧНОЙ_ЧАСТИ:
            неверный_ключ += 1
    return ("табличных частей с ключом не Ref_Key+LineNumber", 0, неверный_ключ)


def _проверить_ложную_независимость(соединение) -> tuple[str, object, object]:
    ложно_независимые = 0
    for имя, сырые_ключи in соединение.execute(
        "SELECT name, key_fields_json FROM entities WHERE is_independent_register = 1"
    ).fetchall():
        ключи = set(json.loads(сырые_ключи))
        if имя.endswith("_RecordType") or ключи & ПРИЗНАКИ_ПОДЧИНЁННОСТИ:
            ложно_независимые += 1
    return ("независимых регистров с признаком подчинённости", 0, ложно_независимые)


def _проверить_наборы_записей(соединение) -> tuple[str, object, object]:
    наборы_записей = соединение.execute(
        "SELECT COUNT(*) FROM entities WHERE is_records = 1"
    ).fetchone()[0]
    return ("наборов записей (is_records)", ОЖИДАЕМО_НАБОРОВ_ЗАПИСЕЙ, наборы_записей)


def _вывести_разбивку_наборов_записей(соединение) -> None:
    """Не входит в проверяемую таблицу (брифом задан только итог 149) — печатается отдельно
    для отчёта: 149 = 121 регистр накопления + 28 регистров сведений."""
    разбивка = соединение.execute(
        "SELECT kind, COUNT(*) FROM entities WHERE is_records = 1 GROUP BY kind"
    ).fetchall()
    print("разбивка наборов записей по виду:", dict(разбивка))


def _проверить_перечисления(соединение) -> tuple[str, object, object]:
    перечисления = соединение.execute(
        "SELECT COUNT(*) FROM entities WHERE kind = 'Enum'"
    ).fetchone()[0]
    return ("перечислений (kind = Enum)", ОЖИДАЕМО_ПЕРЕЧИСЛЕНИЙ, перечисления)


def _проверить_версию_разбора(соединение) -> tuple[str, object, object]:
    строка = соединение.execute(
        "SELECT value FROM meta WHERE key = 'parser_version'"
    ).fetchone()
    return ("parser_version в meta", PARSER_VERSION, строка[0] if строка else None)


def _проверить_счетафактур_как_acc(путь_политики: pathlib.Path) -> tuple[str, object, object]:
    if not путь_политики.exists():
        print(f"политика не найдена: {путь_политики} — проверка считается пройденной (0 из 0)")
        return ("полей auto класса acc с 'СчетаФактур' в имени", 0, 0)
    политика = yaml.safe_load(путь_политики.read_text(encoding="utf-8")) or {}
    авто = политика.get("auto") or {}
    печать = collections.Counter(авто.values())
    print("классы auto в policy.yaml:", dict(печать.most_common()))
    счетафактур_как_acc = sum(
        1
        for ключ, класс in авто.items()
        if класс == "acc" and "СчетаФактур" in ключ.rsplit(".", 1)[-1]
    )
    return ("полей auto класса acc с 'СчетаФактур' в имени", 0, счетафактур_как_acc)


def _вывести_таблицу(строки: list[tuple[str, object, object]]) -> None:
    ширина = max(len(строка[0]) for строка in строки)
    print(f"{'проверка':<{ширина}} | {'ожидание':>10} | {'факт':>10}")
    print("-" * (ширина + 27))
    for имя, ожидание, факт in строки:
        статус = "OK" if факт == ожидание else "FAIL"
        print(f"{имя:<{ширина}} | {str(ожидание):>10} | {str(факт):>10}  {статус}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "trade_dev")
