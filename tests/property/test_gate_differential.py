"""Одна и та же запись с отбором по токену и без него приходит одними и теми же токенами
(инвариант 5) — свойство на сгенерированных ответах, а не на отдельных примерах.

Зачем отдельным свойством. Дважды подряд (находка П2 приёмки и C-1 ревью 6) дефект гейта
проявлялся не утечкой, а тем, что отбор по токену менял токены в ответе: ранний проход (Ruling 25)
переписывает раскрытое значение в сыром теле, и маскировщик, получив строку с токеном вместо
значения, выдавал полю другой токен. Точечные тесты ловят известную форму; ревью 6 и 7 просили
сделать это постоянным сторожем. Здесь он: ответ 1С с одним значением в нескольких написаниях,
накрывающим известным названием, свободным текстом с тем же значением внутри и строкой, дословно
равной токену шлюза, — маскируется без отбора и с отбором `поле eq '<токен>' or DeletionMark eq
false` (1С отвечает одинаково: отбор нужен только затем, чтобы гейт раскрыл токен). Токены обязаны
совпасть поле в поле.

Цепочка — настоящая, от `BaseGate`: `inbound_filter` → ранний проход клиента (`scrubber().load`)
→ `mask` → `finish`. До сравнения ответ «прогревается» одним проходом без отбора: словарь узнаёт
названия по ходу обхода, и в самом первом ответе название в свободном тексте может встретиться
раньше поля, из которого словарь его узнает, — это не расхождение отбора, а холодный словарь.
"""

import contextlib
import json
import pathlib
import tempfile
import typing

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from odata1c.config.models import BaseConfig, GateSettings
from odata1c.gate.detectors import ВЕСА_ИНН10
from odata1c.gate.dictionary import Dictionary
from odata1c.gate.guard import Guard
from odata1c.gate.pipeline import BaseGate
from odata1c.gate.revealed import RevealedValues
from odata1c.gate.tokens import parse_token

СЕКРЕТ = b"property-tests-secret-0123456789"
СУЩНОСТЬ = "Catalog_Контрагенты"
ПОЛИТИКА_ТЕКСТ = (
    "version: 2\nscan_free_text: true\nauto:\n"
    f"  {СУЩНОСТЬ}.ИНН: inn\n"
    f"  {СУЩНОСТЬ}.Description: org\n"
    f"  {СУЩНОСТЬ}.НаименованиеПолное: org\n"
)
ПОЛЯ_ОТБОРА = ("ИНН", "Description", "НаименованиеПолное")


def _инн_из_ядра(ядро: int) -> str:
    цифры = [int(символ) for символ in f"{ядро:09d}"]
    контроль = sum(цифра * вес for цифра, вес in zip(цифры, ВЕСА_ИНН10, strict=True)) % 11 % 10
    return f"{ядро:09d}{контроль}"


ИНН = st.integers(min_value=10**8, max_value=10**9 - 1).map(_инн_из_ядра)
КИРИЛЛИЦА = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
СЛОВО = st.text(alphabet=КИРИЛЛИЦА, min_size=4, max_size=8).map(str.capitalize)
ФОРМА = st.sampled_from(["ООО", "АО", "ПАО", "ИП"])
ГОРОД = st.sampled_from(["Москва", "Рязань", "Тверь"])


def _написание_названия(форма: str, слова: list[str], вид: int) -> str:
    """Одно юрлицо в разных написаниях, которые словарь сводит к одному токену (SPEC §6.3):
    кавычки-ёлочки и прямые, регистр, «ё»/«е», краевые и двойные пробелы."""
    имя = " ".join(слова)
    варианты = [
        f"{форма} {имя}",
        f"{форма} «{имя}»",
        f'{форма} "{имя}"',
        f"{форма} {имя}".upper(),
        f"{форма} {имя}".replace("ё", "е"),
        f"{форма}  {имя} ",
    ]
    return варианты[вид % len(варианты)]


def _написание_инн(инн: str, вид: int) -> str:
    варианты = [инн, f"{инн[:4]} {инн[4:]}", f" {инн}", f"{инн} "]
    return варианты[вид % len(варианты)]


class Сценарий(typing.NamedTuple):
    записи: list[dict]
    поле_отбора: str
    запись_отбора: int


@st.composite
def сценарии(draw) -> Сценарий:
    форма = draw(ФОРМА)
    слова = draw(st.lists(СЛОВО, min_size=1, max_size=2))
    чужие_слова = draw(st.lists(СЛОВО, min_size=1, max_size=2))
    assume(чужие_слова != слова)
    инн = draw(ИНН)
    город = draw(ГОРОД)
    число = draw(st.integers(min_value=2, max_value=4))
    записи = []
    for номер in range(число):
        своя = draw(st.booleans())
        вид = draw(st.integers(min_value=0, max_value=11))
        название = _написание_названия(форма, слова if своя else чужие_слова, вид)
        запись = {
            "Ref_Key": f"00000000-0000-0000-0000-00000000000{номер}",
            "Description": название,
            # Накрывающее название: поле целиком содержит значение другого поля своей записи.
            "НаименованиеПолное": draw(st.sampled_from([f"{название} ({город})", название, ""])),
            "ИНН": _написание_инн(инн, draw(st.integers(0, 7))) if своя else "",
            "Комментарий": draw(
                st.sampled_from(
                    [
                        f"договор с {название}",
                        f"ИНН {_написание_инн(инн, вид)} сверен",
                        "без замечаний",
                        "",
                        # Строка, дословно равная токену шлюза, — подставляется ниже, когда
                        # словарь уже выдал токены (Б-2): в 1С её мог вписать человек.
                        "<токен-названия>",
                        "<токен-инн>",
                    ]
                )
            ),
            "DeletionMark": False,
        }
        записи.append(запись)
    поле = draw(st.sampled_from(ПОЛЯ_ОТБОРА))
    запись_отбора = draw(st.integers(min_value=0, max_value=число - 1))
    return Сценарий(записи, поле, запись_отбора)


@contextlib.contextmanager
def гейт() -> typing.Iterator[tuple[BaseGate, Dictionary]]:
    with tempfile.TemporaryDirectory() as каталог:
        путь = pathlib.Path(каталог)
        (путь / "policy.yaml").write_text(ПОЛИТИКА_ТЕКСТ, encoding="utf-8")
        словарь = Dictionary(путь / "gate.sqlite", СЕКРЕТ)
        try:
            yield (
                BaseGate(
                    base=BaseConfig(
                        name="ut",
                        label="ut",
                        url="http://host/base/odata/standard.odata/",
                        user="agent",
                        gate=GateSettings(mode="identifiers+names"),
                    ),
                    dictionary=словарь,
                    guard=Guard(словарь),
                    policy_path=путь / "policy.yaml",
                ),
                словарь,
            )
        finally:
            словарь.close()


def _ответ(врата: BaseGate, тело: str, отбор: str | None) -> tuple[list, str]:
    """Один вызов тула по настоящей цепочке гейта: отбор (если есть) раскрывается в набор вызова,
    сырое тело проходит ранний проход клиента и маскировщик, конверт — `finish`.

    Сравнивается то, что видит модель, — записи ПОСЛЕ стража, а не выход маскировщика. Раскрытое
    значение в свободном тексте (`ИНН 1000 000002 сверен`) с отбором закрывает ранний проход, без
    отбора — страж в `finish` (детектор такой записи не узнаёт, цифровая серия словаря —
    узнаёт); модель в обоих случаях получает один и тот же токен, и инвариант 5 о нём."""
    набор = RevealedValues()
    if отбор is not None:
        врата.inbound_filter(отбор, entity=СУЩНОСТЬ, revealed=набор, shape=lambda _: None)
    данные = врата.scrubber(набор).load(тело)
    маска = врата.mask(
        данные["value"],
        entity=СУЩНОСТЬ,
        resolve=lambda сущность, ключ: None,
        hidden=lambda сущность: False,
        revealed=набор,
        shape=lambda _: None,
    )
    текст = врата.finish({"items": маска.data, "warnings": []}, набор)
    return json.loads(текст).get("items", []), текст


@given(сценарий=сценарии())
@settings(max_examples=150, deadline=None)
def test_отбор_по_токену_не_меняет_токены_ответа(сценарий):
    with гейт() as (врата, словарь):
        записи = [dict(запись) for запись in сценарий.записи]
        образец = next(з for з in записи if з["ИНН"]) if any(з["ИНН"] for з in записи) else None
        токен_названия = словарь.token_for(
            "org", записи[0]["Description"], base="ut", entity=СУЩНОСТЬ, field="Description"
        )
        токен_инн = (
            словарь.token_for("inn", образец["ИНН"], base="ut", entity=СУЩНОСТЬ, field="ИНН")
            if образец
            else "[[inn:ABCDEFGHJK]]"
        )
        for запись in записи:
            if запись["Комментарий"] == "<токен-названия>":
                запись["Комментарий"] = токен_названия
            elif запись["Комментарий"] == "<токен-инн>":
                запись["Комментарий"] = токен_инн
        тело = json.dumps({"value": записи}, ensure_ascii=False)

        _ответ(врата, тело, None)  # прогрев словаря, см. докстринг модуля
        без_отбора, _ = _ответ(врата, тело, None)
        токен = без_отбора[сценарий.запись_отбора][сценарий.поле_отбора]
        assume(parse_token(токен) is not None)
        с_отбором, текст = _ответ(
            врата, тело, f"{сценарий.поле_отбора} eq '{токен}' or DeletionMark eq false"
        )
        снова_без_отбора, _ = _ответ(врата, тело, None)

        assert "error" not in json.loads(текст)
        for номер, (без, с) in enumerate(zip(без_отбора, с_отбором, strict=True)):
            assert с == без, f"запись {номер}: с отбором и без — разные токены"
        assert снова_без_отбора == без_отбора, "отбор изменил словарь: ответ без отбора другой"
