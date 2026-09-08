"""Словарь токенов: детерминированность, номера названий, варианты, обратное чтение (SPEC §6.6)."""

import pytest

from odata1c.gate.dictionary import Dictionary, DictionaryCorruptError, name_variants_of

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


# --- Правки ревью задачи 4, 2026-09-09 --------------------------------------------------------


def test_две_организации_с_общей_короткой_формой_получают_разные_токены(словарь):
    """SPEC §6.5, слой 3. Ревьюер: ООО «Ромашка» и АО «Ромашка» — разные юрлица, разные токены."""
    ооо = словарь.token_for("org", 'ООО "Ромашка"', base="ut", entity="E", field="F")
    ао = словарь.token_for("org", 'АО "Ромашка"', base="ut", entity="E", field="F")
    assert ооо != ао


def test_полные_названия_обеих_организаций_остаются_однозначными(словарь):
    ооо = словарь.token_for("org", 'ООО "Ромашка"', base="ut", entity="E", field="F")
    ао = словарь.token_for("org", 'АО "Ромашка"', base="ut", entity="E", field="F")
    варианты = словарь.name_variants()
    assert варианты['ооо "ромашка"'] == ооо
    assert варианты['ао "ромашка"'] == ао


def test_общая_короткая_форма_не_заменяется_и_попадает_в_неоднозначные(словарь):
    """Заменить «ромашка» правильно нельзя — не заменяется вовсе (решение координатора)."""
    ооо = словарь.token_for("org", 'ООО "Ромашка"', base="ut", entity="E", field="F")
    ао = словарь.token_for("org", 'АО "Ромашка"', base="ut", entity="E", field="F")

    варианты = словарь.name_variants()
    assert "ромашка" not in варианты

    неоднозначные = словарь.ambiguous_name_variants()
    assert set(неоднозначные["ромашка"]) == {ооо, ао}


def test_уникальное_короткое_название_остаётся_однозначным(словарь):
    токен = словарь.token_for("org", "ООО Вектор", base="ut", entity="E", field="F")
    варианты = словарь.name_variants()
    assert варианты["вектор"] == токен
    assert словарь.ambiguous_name_variants() == {}


def test_повреждённый_файл_словаря_даёт_понятную_ошибку(tmp_path):
    путь = tmp_path / "gate.sqlite"
    путь.write_bytes(b"not a real sqlite database, just random junk bytes 12345")
    with pytest.raises(DictionaryCorruptError) as ошибка:
        Dictionary(путь, СЕКРЕТ)
    assert str(путь) in str(ошибка.value)
    assert ошибка.value.code == "dictionary_corrupt"
    assert str(путь) in ошибка.value.hint


def test_одно_название_в_разных_регистрах_даёт_один_токен(словарь):
    """Причина бага — нормализация из задачи 1 схлопывала пробелы, но не приводила к регистру."""
    первый = словарь.token_for("org", "ООО Ромашка", base="ut", entity="E", field="F")
    второй = словарь.token_for("org", "ооо ромашка", base="ut", entity="E", field="F")
    третий = словарь.token_for("org", "Ооо Ромашка", base="ut", entity="E", field="F")
    assert первый == второй == третий


def test_обратное_чтение_после_регистронезависимой_нормализации_возвращает_написание_базы(словарь):
    токен = словарь.token_for(
        "org", "ООО Ромашка", base="ut", entity="Catalog_Контрагенты", field="Description"
    )
    словарь.token_for(
        "org", "ооо ромашка", base="buh", entity="Catalog_Контрагенты", field="Description"
    )
    assert словарь.reveal(токен, base="ut", field="Description") == "ООО Ромашка"
    assert словарь.reveal(токен, base="buh", field="Description") == "ооо ромашка"


def test_коллизия_хвоста_удлиняет_токен_второго_значения(словарь, monkeypatch):
    """SPEC §6.3: коллизия хвоста — другое значение с тем же токеном, хвост удлиняется до 16.

    Смоделировано подменой make_token в модуле dictionary: для длины 10 он всегда возвращает
    один и тот же хвост независимо от значения — второе значение обязано столкнуться с первым
    и получить удлинённый (16-символьный) токен вместо перезаписи чужой записи.
    """
    from odata1c.gate import dictionary as модуль
    from odata1c.gate.tokens import make_token as настоящий_make_token

    def сталкивающийся_make_token(secret, type_, normalized, tail_length=10):
        if tail_length == 10:
            return f"[[{type_}:COLLISION1]]"
        return настоящий_make_token(secret, type_, normalized, tail_length=tail_length)

    monkeypatch.setattr(модуль, "make_token", сталкивающийся_make_token)

    первый = словарь.token_for("inn", "1111111111", base="ut", entity="E", field="F")
    второй = словарь.token_for("inn", "2222222222", base="ut", entity="E", field="F")

    assert первый == "[[inn:COLLISION1]]"
    assert второй != первый
    хвост_второго = второй[len("[[inn:") : -2]
    assert len(хвост_второго) == 16
    # Первое значение по-прежнему раскрывается своим (нерасширенным) токеном.
    assert словарь.reveal(первый) == "1111111111"
    assert словарь.reveal(второй) == "2222222222"
