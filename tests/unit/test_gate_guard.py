"""Страж: последний проход по сериализованному ответу (SPEC §6.8, инвариант 1)."""

import pytest

from odata1c.gate.dictionary import Dictionary
from odata1c.gate.guard import Guard

СЕКРЕТ = "секрет ровно для тестов стража!!!".encode()


@pytest.fixture
def страж(tmp_path):
    словарь = Dictionary(tmp_path / "gate.sqlite", СЕКРЕТ)
    yield словарь, Guard(словарь)
    словарь.close()


def test_известный_реквизит_не_выходит(страж):
    словарь, охрана = страж
    токен = словарь.token_for("inn", "7707083893", base="ut", entity="E", field="ИНН")
    результат = охрана.check('{"поле": "7707083893"}', mode="identifiers")

    assert "7707083893" not in результат.text
    assert токен in результат.text
    assert результат.warnings == ["guard_replaced"]


def test_реквизит_с_разделителями_ловится(страж):
    словарь, охрана = страж
    словарь.token_for("acc", "40702810900000000001", base="ut", entity="E", field="НомерСчета")
    результат = охрана.check('{"поле": "40702810-9000-00000001"}', mode="identifiers")
    assert "40702810" not in результат.text.replace("[[acc:", "")
    assert результат.warnings == ["guard_replaced"]


def test_известное_название_не_выходит(страж):
    словарь, охрана = страж
    словарь.token_for("org", "ООО Ромашка", base="ut", entity="E", field="Description")
    результат = охрана.check('{"комментарий": "звонили из ООО Ромашка"}', mode="identifiers+names")
    assert "Ромашка" not in результат.text
    assert результат.warnings == ["guard_replaced"]


def test_название_не_проверяется_на_уровне_реквизитов(страж):
    словарь, охрана = страж
    словарь.token_for("org", "ООО Ромашка", base="ut", entity="E", field="Description")
    результат = охрана.check('{"комментарий": "ООО Ромашка"}', mode="identifiers")
    assert "Ромашка" in результат.text
    assert результат.warnings == []


def test_уровень_off_страж_не_работает(страж):
    словарь, охрана = страж
    словарь.token_for("inn", "7707083893", base="ut", entity="E", field="ИНН")
    результат = охрана.check('{"поле": "7707083893"}', mode="off")
    assert результат.text == '{"поле": "7707083893"}'


def test_чистый_ответ_не_меняется(страж):
    _, охрана = страж
    ответ = '{"Сумма": 145200.5, "Date": "2026-09-07T00:00:00", "Number": "ТД-004512"}'
    результат = охрана.check(ответ, mode="identifiers+names")
    assert результат.text == ответ
    assert результат.warnings == []


def test_сломанная_политика_ловится_стражем(страж):
    """SPEC §12: keep на поле с ИНН — страж всё равно заменяет и пишет guard_replaced."""
    словарь, охрана = страж
    словарь.token_for("inn", "7707083893", base="ut", entity="E", field="ИНН")
    ответ = '{"items": [{"ИНН": "7707083893", "КодПоОКПО": "00242766"}]}'
    результат = охрана.check(ответ, mode="identifiers")

    assert "7707083893" not in результат.text
    assert "00242766" in результат.text  # ОКПО не защищается (инвариант 6)
    assert результат.replacements[0]["token"].startswith("[[inn:")


def test_автомат_пересобирается_после_новых_значений(страж):
    словарь, охрана = страж
    охрана.check('{"поле": "новое"}', mode="identifiers+names")
    словарь.token_for("org", "АО Вектор", base="ut", entity="E", field="Description")
    результат = охрана.check('{"поле": "платёж от АО Вектор"}', mode="identifiers+names")
    assert "Вектор" not in результат.text
