"""Обратная подмена: $filter, тела записи, ключи, ошибки токенов (SPEC §6.7)."""

import pytest

from odata1c.gate.dictionary import Dictionary
from odata1c.gate.unmasking import GateError, Unmasker

СЕКРЕТ = "секрет ровно для обратной подмены".encode()


@pytest.fixture
def связка(tmp_path):
    словарь = Dictionary(tmp_path / "gate.sqlite", СЕКРЕТ)
    классы = {
        ("Catalog_Контрагенты", "ИНН"): "inn",
        ("Catalog_БанковскиеСчета", "НомерСчета"): "acc",
        ("Catalog_Контрагенты", "Description"): "org",
    }

    def класс_поля(сущность, поле):
        return классы.get((сущность, поле))

    yield словарь, Unmasker(словарь, base="ut", field_class=класс_поля)
    словарь.close()


def test_токен_в_фильтре_заменяется_реальным_значением(связка):
    словарь, обратно = связка
    токен = словарь.token_for(
        "inn", "7707083893", base="ut", entity="Catalog_Контрагенты", field="ИНН"
    )
    результат = обратно.filter(f"ИНН eq '{токен}'", entity="Catalog_Контрагенты")
    assert результат == "ИНН eq '7707083893'"


def test_вариант_базы_важнее_нормализованного(связка):
    словарь, обратно = связка
    токен = словарь.token_for(
        "inn", "7707 083893", base="ut", entity="Catalog_Контрагенты", field="ИНН"
    )
    результат = обратно.filter(f"ИНН eq '{токен}'", entity="Catalog_Контрагенты")
    assert результат == "ИНН eq '7707 083893'"


def test_substringof_с_полным_токеном_превращается_в_равенство(связка):
    словарь, обратно = связка
    токен = словарь.token_for(
        "org", "ООО Ромашка", base="ut", entity="Catalog_Контрагенты", field="Description"
    )
    результат = обратно.filter(f"substringof('{токен}', Description)", entity="Catalog_Контрагенты")
    assert результат == "Description eq 'ООО Ромашка'"


def test_startswith_с_полным_токеном_превращается_в_равенство(связка):
    словарь, обратно = связка
    токен = словарь.token_for(
        "org", "ООО Ромашка", base="ut", entity="Catalog_Контрагенты", field="Description"
    )
    результат = обратно.filter(f"startswith(Description, '{токен}')", entity="Catalog_Контрагенты")
    assert результат == "Description eq 'ООО Ромашка'"


def test_неизвестный_токен(связка):
    _, обратно = связка
    with pytest.raises(GateError) as ошибка:
        обратно.filter("ИНН eq '[[inn:ZZZZZZZZZZ]]'", entity="Catalog_Контрагенты")
    assert ошибка.value.code == "token_unknown"


def test_обрезанный_токен(связка):
    _, обратно = связка
    with pytest.raises(GateError) as ошибка:
        обратно.filter("ИНН eq '[[inn:M4T2Q9'", entity="Catalog_Контрагенты")
    assert ошибка.value.code == "token_partial"


def test_токен_чужого_класса_в_поле(связка):
    словарь, обратно = связка
    токен = словарь.token_for(
        "inn", "7707083893", base="ut", entity="Catalog_Контрагенты", field="ИНН"
    )
    with pytest.raises(GateError) as ошибка:
        обратно.filter(f"НомерСчета eq '{токен}'", entity="Catalog_БанковскиеСчета")
    assert ошибка.value.code == "token_type_mismatch"


def test_обычный_фильтр_без_токенов_не_меняется(связка):
    _, обратно = связка
    выражение = "Date ge datetime'2026-09-07T00:00:00' and Сумма gt 1000"
    assert обратно.filter(выражение, entity="Document_РеализацияТоваровУслуг") == выражение


def test_имя_из_промпта_уходит_как_есть(связка):
    """Пользователь назвал контрагента сам — строка идёт в 1С без изменений (SPEC §6.5)."""
    _, обратно = связка
    выражение = "substringof('Ромашка', Description)"
    assert обратно.filter(выражение, entity="Catalog_Контрагенты") == выражение


def test_тело_записи_обходится_рекурсивно(связка):
    словарь, обратно = связка
    токен = словарь.token_for(
        "inn", "7707083893", base="ut", entity="Catalog_Контрагенты", field="ИНН"
    )
    тело = {
        "Description": "Новый контрагент",
        "ИНН": токен,
        "КонтактнаяИнформация": [{"Представление": f"ИНН {токен}"}],
    }
    результат = обратно.body(тело, entity="Catalog_Контрагенты")
    assert результат["ИНН"] == "7707083893"
    assert результат["КонтактнаяИнформация"][0]["Представление"] == "ИНН 7707083893"


def test_обрезанный_токен_в_теле(связка):
    _, обратно = связка
    with pytest.raises(GateError) as ошибка:
        обратно.body({"ИНН": "[[inn:M4T2"}, entity="Catalog_Контрагенты")
    assert ошибка.value.code == "token_partial"


def test_составной_ключ_проходит_обратную_подмену(связка):
    словарь, обратно = связка
    токен = словарь.token_for(
        "inn", "7707083893", base="ut", entity="Catalog_Контрагенты", field="ИНН"
    )
    ключ = обратно.key(
        {"Period": "2026-09-07T00:00:00", "ИНН": токен}, entity="Catalog_Контрагенты"
    )
    assert ключ["ИНН"] == "7707083893"
    assert ключ["Period"] == "2026-09-07T00:00:00"


def test_реальное_значение_от_модели_проверяется_контрольной_суммой(связка):
    """ИНН из промпта пользователя допустим, но неверный — ошибка (SPEC §6.7)."""
    _, обратно = связка
    assert обратно.body({"ИНН": "7707083893"}, entity="Catalog_Контрагенты")["ИНН"] == "7707083893"
    with pytest.raises(GateError) as ошибка:
        обратно.body({"ИНН": "1234567890"}, entity="Catalog_Контрагенты")
    assert ошибка.value.code == "filter_syntax"
    assert "контрольн" in str(ошибка.value).lower()
