"""Раунд 4 блокеров M2, Р3-1 (ревью раунда 3): признак 3 структурного поля ключуется сущностью.

Признак 3 Ruling 43 — «словарь видел структуру в поле с этим именем» — ключевался базой и именем
поля. Структура, прочитанная в `ЗаявкаНаВыпускКиЗГИСМ.АдресДоставки` (у поля брат `…Строкой`),
делала структурным текстовое `ЗаказКлиента.АдресДоставки` — по всей базе и навсегда: одно значение
(параметр рецепта, аргумент) уходило в 1С JSON-ом чужой строки контактной информации, отбор,
который работал, отказывал `token_ambiguous`, запись адреса в заказ — тоже.

Решение раунда 4: ключ признака — база, СУЩНОСТЬ, куда приходит путь (`contact_info.
entity_of_path`, та же, что у правила брата), и имя поля — одинаково на чтении и на записи. Формат
у поля один: поле не может быть структурным для записи и текстовым для отбора.
"""

import json
import sqlite3

import pytest
from conftest import без_класса_пути

from odata1c.gate.contact_info import EntityShape
from odata1c.gate.dictionary import Dictionary
from odata1c.gate.revealed import RevealedValues
from odata1c.gate.unmasking import GateError, Unmasker

ЗАКАЗ = "Document_ЗаказКлиента"
ЗАЯВКА = "Document_ЗаявкаНаВыпускКиЗГИСМ"
КИП = "Catalog_Партнеры_КонтактнаяИнформация"
ПЕРЕМЕЩЕНИЕ = "Document_ПеремещениеТоваров"
ТОВАРЫ = f"{ПЕРЕМЕЩЕНИЕ}_Товары"
КЛАССЫ = {
    (ЗАКАЗ, "АдресДоставки"): "addr",
    (ЗАЯВКА, "АдресДоставки"): "addr",
    (КИП, "Представление"): "addr",
    (КИП, "Значение"): "addr",
    (ПЕРЕМЕЩЕНИЕ, "АдресЯчейки"): "addr",
}
# Строение по индексу: у заявки брат `АдресДоставкиСтрокой`, у заказа — нет; у табличной части
# перемещения поле `АдресЯчейки` без брата.
СТРОЕНИЕ = {
    ЗАКАЗ: EntityShape(fields=frozenset({"Ref_Key", "АдресДоставки"})),
    ЗАЯВКА: EntityShape(fields=frozenset({"Ref_Key", "АдресДоставки", "АдресДоставкиСтрокой"})),
    ПЕРЕМЕЩЕНИЕ: EntityShape(fields=frozenset({"Ref_Key", "Товары", "АдресЯчейки"})),
    ТОВАРЫ: EntityShape(fields=frozenset({"LineNumber", "АдресЯчейки"}), parent=ПЕРЕМЕЩЕНИЕ),
}
А = "125047, Москва г, Лесная ул, дом 5, квартира 17"
Б = "301000, Тульская обл, Тула г, Садовая ул, дом 9"


def _бсп(адрес: str) -> str:
    """Значение адреса БСП обычного объёма (живьём адресный JSON — сотни знаков)."""
    return json.dumps(
        {
            "value": адрес,
            "type": "Адрес",
            "country": "Россия",
            "addressType": "Муниципальный",
            "countryCode": "643",
            "ZIPcode": адрес[:6],
            "area": "Регион",
            "areaType": "обл",
            "city": "Город",
            "cityType": "г",
            "street": "Улица",
            "streetType": "ул",
            "houseType": "Дом",
            "houseNumber": "9",
            "apartments": [{"type": "Квартира", "number": "17"}],
            "munLevels": ["area", "city", "street"],
            "admLevels": ["area", "city", "street"],
            "comment": "звонить за час",
        },
        ensure_ascii=False,
    )


def _подмена(с: Dictionary, строение=СТРОЕНИЕ.get) -> Unmasker:
    return Unmasker(
        с,
        base="ut",
        field_class=lambda e, f, *, strict=False: КЛАССЫ.get((e, f)),
        path_class=без_класса_пути,
        shape=строение,
    )


@pytest.fixture
def связка(tmp_path):
    """Словарь видел структуру в `ЗаявкаНаВыпускКиЗГИСМ.АдресДоставки`, а адрес партнёра Б —
    текстом в `Представление` и JSON в `Значение` его строки КИ. В поле `АдресДоставки` написаний
    Б нет ни у одной сущности (фикстура ревьюера, scratchpad/test_m2_r3_repro.py)."""
    с = Dictionary(tmp_path / "gate.sqlite", "секрет ревью раунда 3".encode())
    с.token_for("addr", _бсп(А), base="ut", entity=ЗАЯВКА, field="АдресДоставки", source=А)
    т = с.token_for("addr", Б, base="ut", entity=КИП, field="Представление")
    с.token_for("addr", _бсп(Б), base="ut", entity=КИП, field="Значение", source=Б)
    yield с, т
    с.close()


# --- Воспроизведения ревьюера (два теста, на aa5818d красные) -----------------------------------


def test_одно_значение_в_текстовое_поле_не_json(связка):
    """Параметр рецепта / аргумент по текстовому `ЗаказКлиента.АдресДоставки`: признак 3 без
    сущности сделал поле «структурным» из-за заявки ГИСМ, и в 1С уходил JSON строки КИ."""
    с, т = связка
    u = _подмена(с, строение=None)
    try:
        значение = u.value(т, entity=ЗАКАЗ, field="АдресДоставки", revealed=RevealedValues())
    except GateError:
        return
    assert not значение.lstrip().startswith("{"), "в текстовое поле ушёл JSON"


def test_отбор_по_текстовому_полю_не_отказывает(связка):
    """До признака 3 тот же отбор уходил группой «представление или нормализованная форма»."""
    с, т = связка
    u = _подмена(с, строение=None)
    ушло = u.filter(f"АдресДоставки eq '{т}'", entity=ЗАКАЗ, revealed=RevealedValues())
    assert f"'{Б}'" in ушло and "{" not in ушло


# --- Строже: точный результат, со строением и без -----------------------------------------------


@pytest.mark.parametrize("строение", [СТРОЕНИЕ.get, None], ids=["со-строением", "без-строения"])
def test_одно_значение_в_заказ_текстом(связка, строение):
    с, т = связка

    значение = _подмена(с, строение).value(
        т, entity=ЗАКАЗ, field="АдресДоставки", revealed=RevealedValues()
    )

    assert значение == Б


@pytest.mark.parametrize("строение", [СТРОЕНИЕ.get, None], ids=["со-строением", "без-строения"])
def test_отбор_по_заказу_группа_текстом(связка, строение):
    с, т = связка

    ушло = _подмена(с, строение).filter(
        f"АдресДоставки eq '{т}'", entity=ЗАКАЗ, revealed=RevealedValues()
    )

    # Текст Б и представление JSON строки КИ — одно написание, нормализованная форма та же.
    assert ушло == f"АдресДоставки eq '{Б}'"


def test_запись_в_заказ_текстом_при_структуре_в_другой_сущности(связка):
    """Сторона записи (решение раунда 4 — сущность в ключе и здесь): поле без суффикса, без брата,
    `current` нет, структура видена только в ДРУГОЙ сущности той же базы — пишется текстом, а не
    отказ. Прежний ключ «база + имя поля» давал здесь `token_ambiguous` на самом ходовом адресном
    поле УТ после одного чтения заявки ГИСМ."""
    с, т = связка
    u = _подмена(с)

    assert u.write({"АдресДоставки": т}, entity=ЗАКАЗ, current=None, revealed=RevealedValues()) == {
        "АдресДоставки": Б
    }
    assert u.write(
        {"АдресДоставки": т},
        entity=ЗАКАЗ,
        current={"АдресДоставки": ""},
        revealed=RevealedValues(),
    ) == {"АдресДоставки": Б}


# --- Первая ступень лестницы — тоже формата поля (находка советника раунда 4) -------------------
# Первая ступень берёт написания по базе и ИМЕНИ поля, без сущности. Токен адреса А словарь видел
# только структурой в `ЗаявкаНаВыпускКиЗГИСМ.АдресДоставки`: для `ЗаказКлиента.АдресДоставки` это
# «свои написания поля», и без фильтра формата в текстовую колонку заказа уходил JSON заявки — на
# записи (после сущностного признака 3 отказа больше нет) и на чтении (давняя вторая дверь Р3-1).


def _токен_а(с: Dictionary) -> str:
    """Токен адреса А — фикстура уже положила его структуру в заявку."""
    return с.token_for("addr", _бсп(А), base="ut", entity=ЗАЯВКА, field="АдресДоставки", source=А)


def test_запись_в_заказ_написания_из_заявки_текстом(связка):
    с, _ = связка
    т = _токен_а(с)

    тело = _подмена(с).write(
        {"АдресДоставки": т}, entity=ЗАКАЗ, current=None, revealed=RevealedValues()
    )

    assert тело == {"АдресДоставки": А}


@pytest.mark.parametrize("строение", [СТРОЕНИЕ.get, None], ids=["со-строением", "без-строения"])
def test_одно_значение_и_отбор_в_заказ_написания_из_заявки_текстом(связка, строение):
    с, _ = связка
    т = _токен_а(с)
    u = _подмена(с, строение)

    assert u.value(т, entity=ЗАКАЗ, field="АдресДоставки", revealed=RevealedValues()) == А
    assert u.filter(f"АдресДоставки eq '{т}'", entity=ЗАКАЗ, revealed=RevealedValues()) == (
        f"АдресДоставки eq '{А}'"
    )


def test_структурное_поле_не_берёт_текст_первой_ступени(tmp_path):
    """Обратная сторона: текстовое написание, виденное в `ЗаказКлиента.АдресДоставки`, для поля с
    братом `ЗаявкаНаВыпускКиЗГИСМ.АдресДоставки` — чужой формат. Одно значение берёт структуру
    этого адреса (из строки КИ), отбор — структуру и нормализованную форму."""
    с = Dictionary(tmp_path / "gate.sqlite", "секрет ревью раунда 3".encode())
    try:
        в = "г. Тула,  ул. Садовая, д. 1"
        т = с.token_for("addr", в, base="ut", entity=ЗАКАЗ, field="АдресДоставки")
        структура = json.dumps({"value": в, "type": "Адрес"}, ensure_ascii=False)
        с.token_for("addr", структура, base="ut", entity=КИП, field="Значение", source=в)
        u = _подмена(с)

        значение = u.value(т, entity=ЗАЯВКА, field="АдресДоставки", revealed=RevealedValues())
        ушло = u.filter(f"АдресДоставки eq '{т}'", entity=ЗАЯВКА, revealed=RevealedValues())
    finally:
        с.close()

    assert значение == структура
    нормализованная = " ".join(в.split())
    assert ушло == f"(АдресДоставки eq '{структура}' or АдресДоставки eq '{нормализованная}')"


def test_выдача_токена_во_второй_сущности_засчитывается(связка):
    """Ruling 19 (`issued_for`): ключ `variants` с сущностью записывает выдачу того же написания
    и во второй сущности — токен работает там, где его выдал шлюз, и только там."""
    с, _ = связка
    т = _токен_а(с)
    с.token_for("addr", _бсп(А), base="ut", entity=ЗАКАЗ, field="АдресДоставки", source=А)

    assert с.issued_for(т, base="ut", entity=ЗАЯВКА, field="АдресДоставки")
    assert с.issued_for(т, base="ut", entity=ЗАКАЗ, field="АдресДоставки")
    assert not с.issued_for(т, base="ut", entity=ПЕРЕМЕЩЕНИЕ, field="АдресДоставки")


# --- Та же сущность: признак работает, как прежде ------------------------------------------------


def _структура_в_заказе(с: Dictionary) -> None:
    """Словарь видел структуру адреса А в `ЗаказКлиента.АдресДоставки` другой записи."""
    с.token_for("addr", _бсп(А), base="ut", entity=ЗАКАЗ, field="АдресДоставки", source=А)


def test_структура_в_той_же_сущности_делает_поле_структурным_на_записи(связка):
    с, т = связка
    _структура_в_заказе(с)

    with pytest.raises(GateError) as пойманное:
        _подмена(с).write(
            {"АдресДоставки": т}, entity=ЗАКАЗ, current=None, revealed=RevealedValues()
        )

    assert пойманное.value.code == "token_ambiguous"


def test_структура_в_той_же_сущности_делает_поле_структурным_на_чтении(связка):
    """Отбор по полю, где словарь видел структуру этой же сущности, берёт структуры (и
    нормализованную форму, Ruling 38, пункт 5), а не текстовые написания: в структурной колонке
    текст не найдёт ничего (пустой отбор в M2 значил бы «объекта нет»). Адрес В — с коротким JSON,
    чтобы группа не упёрлась в предел длины отбора."""
    с, _ = связка
    _структура_в_заказе(с)
    в = "г. Тула,  ул. Садовая, д. 1"
    т = с.token_for("addr", в, base="ut", entity=КИП, field="Представление")
    структура = json.dumps({"value": в, "type": "Адрес"}, ensure_ascii=False)
    с.token_for("addr", структура, base="ut", entity=КИП, field="Значение", source=в)

    ушло = _подмена(с).filter(f"АдресДоставки eq '{т}'", entity=ЗАКАЗ, revealed=RevealedValues())

    нормализованная = " ".join(в.split())
    assert ушло == (f"(АдресДоставки eq '{структура}' or АдресДоставки eq '{нормализованная}')")


def test_структура_в_другой_базе_не_считается(tmp_path):
    с = Dictionary(tmp_path / "gate.sqlite", "секрет ревью раунда 3".encode())
    try:
        с.token_for("addr", _бсп(А), base="bp", entity=ЗАКАЗ, field="АдресДоставки", source=А)
        т = с.token_for("addr", Б, base="ut", entity=КИП, field="Представление")

        assert _подмена(с).write(
            {"АдресДоставки": т}, entity=ЗАКАЗ, current=None, revealed=RevealedValues()
        ) == {"АдресДоставки": Б}
    finally:
        с.close()


# --- Сущность пути, а не корень запроса ----------------------------------------------------------


def test_структура_в_табличной_части_по_сущности_пути(связка):
    """Путь `Товары/АдресЯчейки` приходит в сущность табличной части: структура, виденная там,
    делает поле структурным — и на записи строки, и в отборе по пути. Одноимённое поле корня тут
    ни при чём."""
    с, т = связка
    с.token_for("addr", _бсп(А), base="ut", entity=ТОВАРЫ, field="АдресЯчейки", source=А)
    u = _подмена(с)

    with pytest.raises(GateError) as пойманное:
        u.write(
            {"Товары": [{"LineNumber": "1", "АдресЯчейки": т}]},
            entity=ПЕРЕМЕЩЕНИЕ,
            current=None,
            revealed=RevealedValues(),
        )
    assert пойманное.value.code == "token_ambiguous"

    # Одноимённое поле корня документа — текстовое: структура табличной части его не задевает.
    assert u.write(
        {"АдресЯчейки": т}, entity=ПЕРЕМЕЩЕНИЕ, current=None, revealed=RevealedValues()
    ) == {"АдресЯчейки": Б}


def test_структура_корня_не_делает_структурной_табличную_часть(связка):
    с, т = связка
    с.token_for("addr", _бсп(А), base="ut", entity=ПЕРЕМЕЩЕНИЕ, field="АдресЯчейки", source=А)

    тело = _подмена(с).write(
        {"Товары": [{"LineNumber": "1", "АдресЯчейки": т}]},
        entity=ПЕРЕМЕЩЕНИЕ,
        current=None,
        revealed=RevealedValues(),
    )

    assert тело == {"Товары": [{"LineNumber": "1", "АдресЯчейки": Б}]}


def test_словарь_ключует_структуру_сущностью(связка):
    с, _ = связка

    assert с.structured_field(base="ut", entity=ЗАЯВКА, field="АдресДоставки")
    assert not с.structured_field(base="ut", entity=ЗАКАЗ, field="АдресДоставки")
    assert not с.structured_field(base="bp", entity=ЗАЯВКА, field="АдресДоставки")


# --- Ключ таблицы написаний с сущностью ----------------------------------------------------------


def test_одно_написание_в_двух_сущностях_записано_дважды(связка):
    """Та же структура того же адреса в одноимённом поле второй сущности (адрес доставки,
    скопированный из заказа в заявку или обратно): прежний ключ `variants` без сущности терял
    вторую сущность на INSERT OR IGNORE, и признак по сущности её не видел — в структурное поле
    ушёл бы текст. Сущность в ключе записывает обе."""
    с, _ = связка
    _структура_в_заказе(с)  # тот же JSON адреса А, что уже лежит у заявки

    assert с.structured_field(base="ut", entity=ЗАКАЗ, field="АдресДоставки")
    assert с.structured_field(base="ut", entity=ЗАЯВКА, field="АдресДоставки")


_ПРЕЖНЯЯ_СХЕМА = """
CREATE TABLE tokens (
    token TEXT PRIMARY KEY, type TEXT NOT NULL, normalized TEXT NOT NULL, seq INTEGER,
    first_seen_at TEXT NOT NULL, first_base TEXT, first_entity TEXT, first_field TEXT,
    UNIQUE (type, normalized)
);
CREATE TABLE variants (
    token TEXT NOT NULL REFERENCES tokens(token) ON DELETE CASCADE,
    base TEXT NOT NULL, entity TEXT NOT NULL, field TEXT NOT NULL,
    raw_value TEXT NOT NULL, seen_at TEXT NOT NULL,
    PRIMARY KEY (token, base, field, raw_value)
);
CREATE TABLE name_variants (
    token TEXT NOT NULL REFERENCES tokens(token) ON DELETE CASCADE,
    variant_norm TEXT NOT NULL,
    PRIMARY KEY (token, variant_norm)
);
CREATE INDEX idx_variants_lookup ON variants(token, base, field);
PRAGMA user_version = 2;
"""


def _ключ_написаний(путь) -> list[str]:
    соединение = sqlite3.connect(путь)
    try:
        строки = соединение.execute("PRAGMA table_info(variants)").fetchall()
    finally:
        соединение.close()
    return [строка[1] for строка in sorted(строки, key=lambda строка: строка[5]) if строка[5]]


def test_словарь_с_прежним_ключом_перестраивается(tmp_path):
    """Словарь, заведённый до сущности в ключе: при открытии таблица написаний перестраивается,
    строки сохраняются, и вторая сущность того же написания дальше записывается."""
    путь = tmp_path / "gate.sqlite"
    момент = "2026-09-12T00:00:00+00:00"
    соединение = sqlite3.connect(путь)
    соединение.executescript(_ПРЕЖНЯЯ_СХЕМА)
    with соединение:
        соединение.execute(
            "INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?)",
            ("[[addr:X]]", "addr", А, None, момент, "ut", ЗАЯВКА, "АдресДоставки"),
        )
        соединение.execute(
            "INSERT INTO variants VALUES (?,?,?,?,?,?)",
            ("[[addr:X]]", "ut", ЗАЯВКА, "АдресДоставки", _бсп(А), момент),
        )
    соединение.close()

    с = Dictionary(путь, "секрет ревью раунда 3".encode())
    try:
        assert с.structured_field(base="ut", entity=ЗАЯВКА, field="АдресДоставки")
        assert с.spellings("[[addr:X]]") == [_бсп(А)]

        токен = с.token_for(
            "addr", _бсп(А), base="ut", entity=ЗАКАЗ, field="АдресДоставки", source=А
        )

        assert токен == "[[addr:X]]"
        assert с.structured_field(base="ut", entity=ЗАКАЗ, field="АдресДоставки")
        assert с.spellings("[[addr:X]]") == [_бсп(А)]
    finally:
        с.close()
    assert _ключ_написаний(путь) == ["token", "base", "entity", "field", "raw_value"]

    # Повторное открытие ничего не перестраивает и строк не теряет.
    с = Dictionary(путь, "секрет ревью раунда 3".encode())
    try:
        assert с.structured_field(base="ut", entity=ЗАКАЗ, field="АдресДоставки")
        assert с.structured_field(base="ut", entity=ЗАЯВКА, field="АдресДоставки")
    finally:
        с.close()
