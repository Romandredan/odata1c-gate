"""Словарь токенов: детерминированность, номера названий, варианты, обратное чтение (SPEC §6.6)."""

import pytest

from odata1c.gate.dictionary import Dictionary, name_variants_of

СЕКРЕТ = "секрет ровно для тестов словаря!!".encode()


@pytest.fixture
def словарь(tmp_path):
    справочник = Dictionary(tmp_path / "gate.sqlite", СЕКРЕТ)
    yield справочник
    справочник.close()


def test_реквизит_получает_один_и_тот_же_токен(словарь):
    первый = словарь.token_for(
        "inn", "7707083893", base="ut", entity="Catalog_Контрагенты", field="ИНН"
    )
    второй = словарь.token_for(
        "inn", "7707 083893", base="buh", entity="Catalog_Контрагенты", field="ИНН"
    )
    assert первый == второй  # нормализация убирает пробел


def test_названия_нумеруются_по_порядку(словарь):
    первый = словарь.token_for(
        "org", "ООО Ромашка", base="ut", entity="Catalog_Контрагенты", field="Description"
    )
    второй = словарь.token_for(
        "org", "АО Вектор", base="ut", entity="Catalog_Контрагенты", field="Description"
    )
    assert первый == "[[org:1]]"
    assert второй == "[[org:2]]"


def test_номер_названия_не_меняется(словарь):
    первый = словарь.token_for(
        "org", "ООО Ромашка", base="ut", entity="Catalog_Контрагенты", field="Description"
    )
    словарь.token_for(
        "org", "АО Вектор", base="ut", entity="Catalog_Контрагенты", field="Description"
    )
    повтор = словарь.token_for(
        "org", "ООО  Ромашка", base="ut", entity="Catalog_Контрагенты", field="Description"
    )
    assert повтор == первый


def test_нумерация_раздельная_по_классам(словарь):
    орг = словарь.token_for("org", "ООО Ромашка", base="ut", entity="E", field="F")
    лицо = словарь.token_for("person", "Петров Иван Сергеевич", base="ut", entity="E", field="F")
    assert орг == "[[org:1]]"
    assert лицо == "[[person:1]]"


def test_обратное_чтение_реквизита(словарь):
    токен = словарь.token_for(
        "inn", "7707 083893", base="ut", entity="Catalog_Контрагенты", field="ИНН"
    )
    assert словарь.reveal(токен) == "7707083893"


def test_обратное_чтение_возвращает_вариант_базы(словарь):
    """Как значение записано в конкретной базе и поле (SPEC §6.7)."""
    токен = словарь.token_for(
        "inn", "7707 083893", base="ut", entity="Catalog_Контрагенты", field="ИНН"
    )
    assert словарь.reveal(токен, base="ut", field="ИНН") == "7707 083893"
    assert словарь.reveal(токен, base="buh", field="ИНН") == "7707083893"  # варианта нет — норма


def test_неизвестный_токен(словарь):
    assert словарь.reveal("[[inn:ZZZZZZZZZZ]]") is None


def test_цифровые_значения_с_токенами_для_стража(словарь):
    инн = словарь.token_for("inn", "7707083893", base="ut", entity="E", field="F")
    счёт = словарь.token_for("acc", "40702810900000000001", base="ut", entity="E", field="F")
    словарь.token_for("org", "ООО Ромашка", base="ut", entity="E", field="F")

    числа = словарь.number_tokens()
    assert числа == {"7707083893": инн, "40702810900000000001": счёт}


def test_названия_попадают_в_варианты_для_стража(словарь):
    токен = словарь.token_for("org", 'ООО "Ромашка"', base="ut", entity="E", field="F")
    варианты = словарь.name_variants()
    assert варианты['ооо "ромашка"'] == токен
    assert варианты["ромашка"] == токен


def test_версия_растёт_при_добавлении(словарь):
    было = словарь.revision()
    словарь.token_for("org", "ООО Ромашка", base="ut", entity="E", field="F")
    assert словарь.revision() > было


def test_повтор_версию_не_меняет(словарь):
    словарь.token_for("org", "ООО Ромашка", base="ut", entity="E", field="F")
    было = словарь.revision()
    словарь.token_for("org", "ООО Ромашка", base="ut", entity="E", field="F")
    assert словарь.revision() == было


def test_короткие_названия_не_идут_в_варианты(словарь):
    """SPEC §6.5, слой 3: варианты длиной от 4 символов — иначе ложные срабатывания."""
    словарь.token_for("org", "ИП А", base="ut", entity="E", field="F")
    assert all(len(вариант) >= 4 for вариант in словарь.name_variants())


@pytest.mark.parametrize(
    ("название", "ожидаемые"),
    [
        ('ООО "Ромашка"', ['ооо "ромашка"', "ромашка"]),
        ("АО Вектор-Строй", ["ао вектор-строй", "вектор-строй"]),
        ("ИП Петров Иван Сергеевич", ["ип петров иван сергеевич", "петров иван сергеевич"]),
        ("Петров Иван Сергеевич", ["петров иван сергеевич"]),
    ],
)
def test_варианты_названия(название, ожидаемые):
    assert name_variants_of(название) == ожидаемые
