"""Словарь токенов: детерминированность, номера названий, варианты, обратное чтение (SPEC §6.6)."""

import sqlite3

import pytest

from odata1c.gate.dictionary import (
    СХЕМА,
    Dictionary,
    DictionaryCorruptError,
    name_variants_of,
    normalize_text_with_map,
)

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


def test_любой_вариант_отдаёт_исходное_написание_без_базы_и_поля(словарь):
    """Локальное раскрытие без --base/--field (SPEC §6.3, §14.5): показываем исходное
    написание, а не нормализованное значение, даже когда неизвестно, в какой базе и поле
    оно встретилось."""
    токен = словарь.token_for(
        "org", 'ООО "Ромашка"', base="ut", entity="Catalog_Контрагенты", field="Description"
    )
    assert словарь.any_variant(токен) == 'ООО "Ромашка"'


def test_любой_вариант_реквизита_показывает_написание_а_не_только_цифры(словарь):
    токен = словарь.token_for(
        "phone", "+7 (999) 123-45-67", base="ut", entity="Catalog_Контрагенты", field="Телефон"
    )
    assert словарь.any_variant(токен) == "+7 (999) 123-45-67"


def test_любой_вариант_неизвестного_токена(словарь):
    assert словарь.any_variant("[[inn:ZZZZZZZZZZ]]") is None


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


# --- M1b-fix, задача 4 (Ruling 20 M1c): «ё» приравнивается к «е» (SPEC §6.3) ----------------


def test_варианты_названия_приравнивают_ё_к_е():
    assert name_variants_of("ООО «Ёлка»") == ['ооо "елка"', "елка"]
    assert name_variants_of("Пётр Ёлкин") == ["петр елкин"]


def test_одно_название_через_ё_и_е_даёт_один_токен(словарь):
    """Иначе два токена одного юрлица, их свёрнутые варианты совпадают и становятся
    неоднозначными — страж перестал бы заменять оба написания."""
    первый = словарь.token_for("org", "ООО «Василёк»", base="ut", entity="E", field="Description")
    второй = словарь.token_for("org", "ООО «Василек»", base="buh", entity="E", field="Description")
    assert первый == второй
    assert словарь.ambiguous_name_variants() == {}
    assert словарь.name_variants()['ооо "василек"'] == первый
    # Исходное написание каждой базы не теряется (SPEC §6.3).
    assert словарь.reveal(первый, base="ut", field="Description") == "ООО «Василёк»"
    assert словарь.reveal(первый, base="buh", field="Description") == "ООО «Василек»"


def test_нормализация_текста_сворачивает_ё_без_сдвига_позиций():
    текст = "звонили из ООО «Ёлка», Пётр"
    нормализованный, карта = normalize_text_with_map(текст)
    assert нормализованный == 'звонили из ооо "елка", петр'
    assert len(нормализованный) == len(текст)
    assert [карта[индекс] for индекс in range(len(текст))] == [
        (индекс, индекс + 1) for индекс in range(len(текст))
    ]


def _словарь_прежней_версии(путь) -> None:
    """Файл словаря в том виде, в каком его оставила версия до свёртки «ё»: нормализованные
    значения и варианты названий с «ё». Одно юрлицо записано дважды — через «ё» и через «е»
    (два токена, как выдавала прежняя нормализация)."""
    соединение = sqlite3.connect(путь)
    соединение.executescript(СХЕМА)
    момент = "2026-09-09T00:00:00+00:00"
    токены = [
        ("[[org:1]]", "org", "ооо василёк", 1, "ООО Василёк", ["ооо василёк", "василёк"]),
        ("[[org:2]]", "org", "ооо василек", 2, "ООО Василек", ["ооо василек", "василек"]),
        ("[[org:3]]", "org", "ооо «ёлка»", 3, "ООО «Ёлка»", ['ооо "ёлка"', "ёлка"]),
        ("[[person:1]]", "person", "пётр ёлкин", 1, "Пётр Ёлкин", ["пётр ёлкин"]),
    ]
    with соединение:
        for токен, класс, нормализованное, номер, исходное, варианты in токены:
            соединение.execute(
                "INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?)",
                (токен, класс, нормализованное, номер, момент, "ut", "E", "Description"),
            )
            соединение.execute(
                "INSERT INTO variants VALUES (?,?,?,?,?,?)",
                (токен, "ut", "E", "Description", исходное, момент),
            )
            соединение.executemany(
                "INSERT INTO name_variants VALUES (?,?)", [(токен, в) for в in варианты]
            )
    соединение.close()


def test_словарь_прежней_версии_пересчитывается_при_открытии(tmp_path):
    """Варианты названий хранятся нормализованными — без пересчёта старые записи с «ё» не
    совпали бы с текстом, свёрнутым в «е». Пересчёт однократный (PRAGMA user_version)."""
    путь = tmp_path / "gate.sqlite"
    _словарь_прежней_версии(путь)
    словарь = Dictionary(путь, СЕКРЕТ)
    try:
        # Двойник через «ё» уступает свёрнутый ключ двойнику через «е»; его варианты сняты,
        # иначе совпавшие варианты стали бы неоднозначными и страж не заменял бы ни один.
        assert словарь.name_variants() == {
            "ооо василек": "[[org:2]]",
            "василек": "[[org:2]]",
            'ооо "елка"': "[[org:3]]",
            "елка": "[[org:3]]",
            "петр елкин": "[[person:1]]",
        }
        assert словарь.ambiguous_name_variants() == {}
        было = словарь.revision()
        assert словарь.token_for("org", "ООО Василёк", base="ut", entity="E", field="F") == (
            "[[org:2]]"
        )
        assert словарь.token_for("org", "ооо «Елка»", base="ut", entity="E", field="F") == (
            "[[org:3]]"
        )
        assert словарь.token_for("person", "Петр Елкин", base="ut", entity="E", field="F") == (
            "[[person:1]]"
        )
        assert словарь.revision() == было  # новых токенов не появилось
        # Старый токен двойника по-прежнему раскрывается исходным написанием.
        assert словарь.reveal("[[org:1]]", base="ut", field="Description") == "ООО Василёк"
    finally:
        словарь.close()

    # Повторное открытие ничего не меняет.
    повторно = Dictionary(путь, СЕКРЕТ)
    try:
        assert повторно.name_variants()["василек"] == "[[org:2]]"
        assert повторно.ambiguous_name_variants() == {}
    finally:
        повторно.close()
