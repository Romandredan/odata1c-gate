"""Воспроизведение C-1 ревью 8 (оракул через путь коллекции), перенесено в набор как есть —
изменены только порядок импортов и пометки линтера. На HEAD dde3780 все четыре сторожа были
красными (проверено ревьюером), после Ruling 37 — зелёные. Остальные формы пути (навигация,
несколько звеньев, `Condition`, рецепт, `$orderby`) — `test_gate_contact_info.py` и
`test_tools_service.py`, тесты `r37`."""

import pytest
from test_gate_contact_info import КИ_КОНТРАГЕНТОВ, маскировщик, обратная, строение  # noqa: F401

from odata1c.gate.revealed import RevealedValues
from odata1c.gate.unmasking import GateError


def _резолвер(entity, ключ):
    if entity == "Catalog_Контрагенты" and ключ == "КонтактнаяИнформация":
        return КИ_КОНТРАГЕНТОВ
    return None


@pytest.mark.parametrize(
    "форма",
    [
        "substringof('495', КонтактнаяИнформация/Представление)",
        "substringof('495', КонтактнаяИнформация/Значение)",
        "substringof('mail.ru', КонтактнаяИнформация/ДоменноеИмяСервера)",
        "КонтактнаяИнформация/Представление eq '84951234567'",
    ],
)
def test_оракул_через_путь_коллекции_отклонён(маскировщик, форма):  # noqa: F811
    # Отбор по полю значения контактной информации через путь коллекции от родителя обязан
    # отклоняться так же, как прямой отбор по …_КонтактнаяИнформация (Ruling 35).
    with pytest.raises(GateError) as ошибка:
        обратная(маскировщик).filter(форма, entity="Catalog_Контрагенты", revealed=RevealedValues())
    assert ошибка.value.code in ("filter_syntax", "token_partial")
