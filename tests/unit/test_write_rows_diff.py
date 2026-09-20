"""`rows_diff`/`RowsDiff` и `preview.rows_preview` (план M3b, задача 6, проект §5.2).

Модуль `rows_diff.py` не знает о гейте — чистые функции над списками словарей уже показанных
(или уже реальных, для решения «изменилась ли часть») строк. Здесь — прямые тесты по сигнатуре,
без `WriteService`: сценарии на живом гейте — `tests/unit/test_write_prepare_update.py`.
"""

from odata1c.write.preview import rows_preview
from odata1c.write.rows_diff import RowsDiff, rows_diff


def test_без_изменений_пустой_дифф():
    было = [{"LineNumber": 1, "Количество": 5}, {"LineNumber": 2, "Количество": 7}]
    станет = [{"LineNumber": 1, "Количество": 5}, {"LineNumber": 2, "Количество": 7}]

    диф = rows_diff(было, станет)

    assert диф == RowsDiff(changed=[], added=[], removed=[], before_count=2, after_count=2)


def test_изменённое_поле_строки():
    было = [{"LineNumber": 1, "Количество": 5}]
    станет = [{"LineNumber": 1, "Количество": 9}]

    диф = rows_diff(было, станет)

    assert диф.changed == [
        {"line": 1, "fields": [{"field": "Количество", "before": 5, "after": 9}]}
    ]
    assert диф.added == [] and диф.removed == []


def test_сравниваются_только_поля_присланные_в_станет():
    """Как у полей шапки: поле, которого нет в «станет», не участвует в сравнении — модель не
    присылала его для этой строки, а не «очистила»."""
    было = [{"LineNumber": 1, "Количество": 5, "Цена": 100}]
    станет = [{"LineNumber": 1, "Количество": 9}]

    диф = rows_diff(было, станет)

    assert диф.changed == [
        {"line": 1, "fields": [{"field": "Количество", "before": 5, "after": 9}]}
    ]


def test_добавленная_и_удалённая_строки():
    было = [{"LineNumber": 1, "Количество": 5}, {"LineNumber": 2, "Количество": 7}]
    станет = [{"LineNumber": 1, "Количество": 5}, {"LineNumber": 3, "Количество": 1}]

    диф = rows_diff(было, станет)

    assert диф.changed == []
    assert диф.added == [{"line": 3, "fields": [{"field": "Количество", "value": 1}]}]
    assert диф.removed == [{"line": 2, "fields": [{"field": "Количество", "value": 7}]}]
    assert диф.before_count == 2 and диф.after_count == 2


def test_line_number_исключён_из_полей_сравнения():
    """Номер строки — ключ сопоставления, не содержимое: не должен попадать в списки `fields`
    changed/added/removed, даже когда он единственное поле строки."""
    было = [{"LineNumber": 1}]
    станет = [{"LineNumber": 1}]

    диф = rows_diff(было, станет)

    assert диф == RowsDiff(changed=[], added=[], removed=[], before_count=1, after_count=1)


def test_номер_строки_строкой_и_числом_совпадает():
    """Проба P9: 1С отдаёт `LineNumber` текущего состояния то строкой, то числом — сопоставление
    строк по номеру не должно зависеть от типа значения."""
    было = [{"LineNumber": "1", "Количество": 5}]
    станет = [{"LineNumber": 1, "Количество": 9}]

    диф = rows_diff(было, станет)

    assert диф.added == [] and диф.removed == []
    assert диф.changed == [
        {"line": 1, "fields": [{"field": "Количество", "before": 5, "after": 9}]}
    ]
    # Поле `line` результата — числом, не строкой (сравнение вызывающего кода — как у теста).
    assert isinstance(диф.changed[0]["line"], int)


def test_строка_без_line_number_не_сопоставляется():
    было = [{"Количество": 5}]
    станет = [{"LineNumber": 1, "Количество": 5}]

    диф = rows_diff(было, станет)

    # Строка «было» без номера не входит в сопоставление вовсе — «станет» видна как добавленная.
    assert диф.added == [{"line": 1, "fields": [{"field": "Количество", "value": 5}]}]
    assert диф.removed == [] and диф.changed == []


def test_порядок_по_номеру_а_не_по_вхождению():
    было = [{"LineNumber": 3, "x": 1}, {"LineNumber": 1, "x": 2}]
    станет = [{"LineNumber": 10, "x": 1}, {"LineNumber": 2, "x": 2}]

    диф = rows_diff(было, станет)

    # 10 идёт ПОСЛЕ 2 численно, хотя лексикографически строка «10» < «2».
    assert [строка["line"] for строка in диф.added] == [2, 10]
    assert [строка["line"] for строка in диф.removed] == [1, 3]


def test_rows_preview_форма_и_summary():
    диф = RowsDiff(
        changed=[{"line": 1, "fields": [{"field": "x", "before": 1, "after": 2}]}],
        added=[{"line": 3, "fields": [{"field": "x", "value": 5}]}],
        removed=[],
        before_count=2,
        after_count=3,
    )

    строка = rows_preview("Товары", диф)

    assert строка == {
        "field": "Товары",
        "rows": {
            "changed": диф.changed,
            "added": диф.added,
            "removed": диф.removed,
            "before_count": 2,
            "after_count": 3,
        },
        "summary": "строк было 2, станет 3",
    }
