"""Каталог правил сущностей — вход авторазметки гейта (ADR-0016, SPEC §6.5).

Правила, которые касаются одной сущности или одного необычного имени поля, не пишутся в коде
классификатора (`field_rules.py`), а лежат файлами YAML в едином формате:

- `people` — справочники людей: их `Description` и поля ФИО — класс `person` (SPEC §6.5, слой 1),
  их адрес — адрес физлица (SPEC §6.9);
- `orgs` — справочники организаций: `Description` и наименования — класс `org`;
- `addr_mask_for` — сущности не-справочники, чьи адреса принадлежат физлицам (SPEC §6.9);
- `fields` — класс поля одной сущности, `Сущность.Поле: класс`;
- `names` — класс поля с таким именем в любой сущности, `ИмяПоля: класс`.

Значение в `fields` и `names` — класс гейта (кроме `keep`) или `scan`: «по имени не
классифицировать» — так снимается ложное срабатывание общего правила. `keep` каталогу не доступен:
открыть поле целиком, без поиска реквизитов, вправе только владелец в `policy.yaml`.

Два слоя: каталог поставки (`odata1c/templates/gate/*.yaml`) и каталог владельца
(`<дом>/gate/*.yaml`) — правила доработанных конфигураций, общие для всех баз такой конфигурации.
Владелец главнее поставки на совпадающем ключе. Каталог не привязан к конфигурации базы: правило
действует там, где в индексе есть его сущность, — имена сущностей типовых библиотек (БСП, БЗК,
БЭД) одинаковы во всех конфигурациях, куда библиотека встроена.
"""

from __future__ import annotations

import dataclasses
import functools
import importlib.resources
import pathlib
import types
import unicodedata

import yaml

from odata1c.gate.errors import PolicyError
from odata1c.gate.tokens import CLASSES

ВЕРСИЯ = 1
РАЗДЕЛЫ_СПИСКИ = ("people", "orgs", "addr_mask_for")
РАЗДЕЛЫ_КЛАССЫ = ("fields", "names")
РАЗДЕЛЫ = {"version", "about", *РАЗДЕЛЫ_СПИСКИ, *РАЗДЕЛЫ_КЛАССЫ}
БЕЗ_КЛАССА = "scan"
ДОПУСТИМЫЕ = (CLASSES - {"keep"}) | {БЕЗ_КЛАССА}
# Виды, у которых дочерние сущности — набор записей и виртуальные таблицы с теми же полями
# (`<регистр>_RecordType`, `_SliceLast`, `_Balance`…): правило `fields`, выписанное на регистр,
# действует и на них. У справочника и документа дочерние сущности — табличные части со своим
# составом полей, и правило родителя на них не переносится.
ВИДЫ_С_ВИРТУАЛЬНЫМИ = (
    "InformationRegister_",
    "AccumulationRegister_",
    "AccountingRegister_",
    "CalculationRegister_",
)
КАТАЛОГ_ВЛАДЕЛЬЦА = "gate"


@dataclasses.dataclass(frozen=True, slots=True)
class RuleCatalog:
    """Собранный каталог: все файлы всех слоёв в одном наборе правил."""

    people: frozenset[str] = frozenset()
    orgs: frozenset[str] = frozenset()
    addr_mask_for: frozenset[str] = frozenset()
    fields: types.MappingProxyType = dataclasses.field(
        default_factory=lambda: types.MappingProxyType({})
    )
    names: types.MappingProxyType = dataclasses.field(
        default_factory=lambda: types.MappingProxyType({})
    )

    @property
    def names_for(self) -> frozenset[str]:
        """Справочники, у которых скрываются `Description` и ФИО (SPEC §6.5, слой 1)."""
        return self.people | self.orgs

    def field_rule(self, entity: str, field: str) -> str | None:
        """Класс по `fields`: сама сущность, а для регистра — и сущность-регистр его набора
        записей или виртуальной таблицы (`<регистр>_…`)."""
        точное = self.fields.get(f"{entity}.{field}")
        if точное is not None or not entity.startswith(ВИДЫ_С_ВИРТУАЛЬНЫМИ):
            return точное
        for ключ, класс in self.fields.items():
            сущность, _, поле = ключ.rpartition(".")
            if поле == field and entity.startswith(f"{сущность}_"):
                return класс
        return None

    def field_rule_canonical(self, entity: str, field: str) -> str | None:
        """Класс по `fields` для строгого режима, когда сущность индексу не известна (`raw_get`
        путём, который индекс не разрешил, Ruling 18): имя сущности там может быть искажено
        (`…ДокументыФизическихЛиц.` с точкой, невидимый символ, другой регистр), и правило,
        привязанное к точному имени, не сработало бы (ревью атакующим ветки bp-gate, находка 2).
        Имена сравниваются канонизированными (`канонизировать`); для регистра — и его наборы
        записей и срезы. Правило любой другой сущности на незнакомую сущность не переносится
        (повторное ревью, находка 2: иначе `Номер` и `Представление` закрывались бы у каждой
        неизвестной сущности). `scan` не в счёт."""
        имя = канонизировать(entity)
        for ключ, класс in self.fields.items():
            сущность, _, поле = ключ.rpartition(".")
            if класс == БЕЗ_КЛАССА or поле != field:
                continue
            своя = канонизировать(сущность)
            if имя == своя or (
                сущность.startswith(ВИДЫ_С_ВИРТУАЛЬНЫМИ) and имя.startswith(f"{своя}_")
            ):
                return класс
        return None

    def name_rule(self, field: str) -> str | None:
        """Класс по `names` — имя поля без учёта регистра."""
        return self.names.get(field.casefold())


def канонизировать(имя: str) -> str:
    """Имя сущности, каким его, вероятно, увидит 1С за IIS: NFC, без невидимых символов формата
    (категория Cf — U+200B, мягкий перенос) и комбинирующих знаков (Mn — ударение; после NFC «й»
    и «ё» составные и не страдают), без завершающих точек и любых пробельных символов (включая
    неразрывный), без учёта регистра."""
    имя = unicodedata.normalize("NFC", имя)
    имя = "".join(символ for символ in имя if unicodedata.category(символ) not in ("Cf", "Mn"))
    while имя and (имя[-1] == "." or имя[-1].isspace()):
        имя = имя[:-1]
    return имя.casefold()


def _ошибка(путь: pathlib.Path | str, текст: str) -> PolicyError:
    return PolicyError(
        f"каталог правил {путь}: {текст}",
        hint="формат файла — шапка любого файла каталога поставки (odata1c/templates/gate/) "
        "и ADR-0016",
    )


def _место(exc: Exception) -> str:
    """Строка и колонка ошибки YAML — и больше ничего из исключения (см. `policy._место_ошибки`)."""
    mark = getattr(exc, "problem_mark", None)
    return f"строка {mark.line + 1}, колонка {mark.column + 1}" if mark is not None else "?"


def parse_rules_text(text: str, путь: pathlib.Path | str) -> dict:
    """Разобрать и проверить один файл каталога. Результат — словарь разделов, ключи `names`
    уже приведены к нижнему регистру. Ошибка — `PolicyError` с путём и местом, без значений."""
    try:
        данные = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _ошибка(путь, f"YAML не разбирается ({_место(exc)})") from None
    if данные is None:
        данные = {}
    if not isinstance(данные, dict):
        raise _ошибка(путь, "корень файла — не словарь разделов")
    лишние = sorted(str(ключ) for ключ in данные if ключ not in РАЗДЕЛЫ)
    if лишние:
        raise _ошибка(путь, f"неизвестные разделы: {', '.join(лишние)}")
    if данные.get("version", ВЕРСИЯ) != ВЕРСИЯ:
        raise _ошибка(путь, f"поддерживается только version: {ВЕРСИЯ}")
    итог: dict = {}
    for раздел in РАЗДЕЛЫ_СПИСКИ:
        значение = данные.get(раздел) or []
        if not isinstance(значение, list) or not all(
            isinstance(имя, str) and имя for имя in значение
        ):
            raise _ошибка(путь, f"раздел {раздел} — список имён сущностей")
        итог[раздел] = значение
    for раздел in РАЗДЕЛЫ_КЛАССЫ:
        значение = данные.get(раздел) or {}
        if not isinstance(значение, dict):
            raise _ошибка(путь, f"раздел {раздел} — словарь «ключ: класс»")
        правила: dict[str, str] = {}
        for ключ, класс in значение.items():
            if not isinstance(ключ, str) or not ключ:
                raise _ошибка(путь, f"раздел {раздел}: ключ — непустая строка")
            if раздел == "fields" and "." not in ключ:
                raise _ошибка(путь, f"раздел fields: ключ {ключ} — не в форме Сущность.Поле")
            if класс == "keep":
                raise _ошибка(
                    путь,
                    f"раздел {раздел}, {ключ}: keep каталогу не доступен — открыть поле "
                    "целиком вправе только владелец в policy.yaml базы",
                )
            if класс not in ДОПУСТИМЫЕ:
                raise _ошибка(
                    путь,
                    f"раздел {раздел}, {ключ}: класс не из списка "
                    f"({', '.join(sorted(ДОПУСТИМЫЕ))})",
                )
            правила[ключ.casefold() if раздел == "names" else ключ] = класс
        итог[раздел] = правила
    return итог


def _собрать_слой(файлы: list[tuple[str, str]]) -> dict:
    """Файлы одного слоя в один набор; одно правило с разными значениями в двух файлах слоя —
    ошибка («правило задано дважды»): какое из двух действует, не угадать."""
    слой: dict = {раздел: set() for раздел in РАЗДЕЛЫ_СПИСКИ}
    слой |= {раздел: {} for раздел in РАЗДЕЛЫ_КЛАССЫ}
    откуда: dict[tuple[str, str], str] = {}
    for путь, текст in файлы:
        разобрано = parse_rules_text(текст, путь)
        for раздел in РАЗДЕЛЫ_СПИСКИ:
            слой[раздел].update(разобрано[раздел])
        for раздел in РАЗДЕЛЫ_КЛАССЫ:
            for ключ, класс in разобрано[раздел].items():
                прежний = слой[раздел].get(ключ)
                if прежний is not None and прежний != класс:
                    raise _ошибка(
                        путь,
                        f"правило {раздел}.{ключ} задано дважды с разными классами "
                        f"(второе место — {откуда[(раздел, ключ)]})",
                    )
                слой[раздел][ключ] = класс
                откуда[(раздел, ключ)] = str(путь)
    return слой


def _каталог(*слои: dict) -> RuleCatalog:
    """Слои по возрастанию силы: списки объединяются, классы сильного слоя перекрывают слабый."""
    списки = {
        раздел: frozenset().union(*(слой[раздел] for слой in слои)) for раздел in РАЗДЕЛЫ_СПИСКИ
    }
    классы = {
        раздел: types.MappingProxyType(
            functools.reduce(lambda итог, слой: {**итог, **слой[раздел]}, слои, {})
        )
        for раздел in РАЗДЕЛЫ_КЛАССЫ
    }
    return RuleCatalog(**списки, **классы)


def _файлы_поставки() -> list[tuple[str, str]]:
    каталог = importlib.resources.files("odata1c.templates.gate")
    return [
        (f"odata1c/templates/gate/{файл.name}", файл.read_text(encoding="utf-8"))
        for файл in sorted(каталог.iterdir(), key=lambda файл: файл.name)
        if файл.name.lower().endswith(".yaml")
    ]


@functools.cache
def _слой_поставки() -> dict:
    файлы = _файлы_поставки()
    if not файлы:
        # Отказ в закрытую сторону (ревью атакующим, находка 3): без каталога поставки пусты
        # списки справочников людей и организаций, и `Description` контрагентов и физлиц потерял
        # бы защиту и в авторазметке, и в строгом запасном пути. Такой пакет собран неверно.
        raise _ошибка(
            "odata1c/templates/gate/",
            "каталог правил поставки пуст — пакет собран без файлов каталога, переустановите шлюз",
        )
    return _собрать_слой(файлы)


@functools.cache
def package_rules() -> RuleCatalog:
    """Каталог поставки — один раз на процесс."""
    return _каталог(_слой_поставки())


def owner_rules_dir(home: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(home) / КАТАЛОГ_ВЛАДЕЛЬЦА


def owner_rules_files(home: pathlib.Path | None) -> list[pathlib.Path]:
    """Файлы каталога владельца по имени, `.yaml` в любом регистре; нет каталога — пусто."""
    if home is None:
        return []
    каталог = owner_rules_dir(home)
    if not каталог.is_dir():
        return []
    return sorted(
        (путь for путь in каталог.iterdir() if путь.is_file() and путь.suffix.lower() == ".yaml"),
        key=lambda путь: путь.name,
    )


def owner_rules_stamp(home: pathlib.Path | None) -> tuple:
    """Отпечаток каталога владельца (имя, mtime, размер каждого файла) — для перечитывания на
    ходу, как у `policy.yaml` (`BaseGate._отметка`)."""
    return tuple(
        (путь.name, путь.stat().st_mtime, путь.stat().st_size) for путь in owner_rules_files(home)
    )


def load_rules(home: pathlib.Path | None) -> RuleCatalog:
    """Каталог поставки и каталог владельца (`<дом>/gate/*.yaml`), собранные в один. Без дома —
    каталог поставки. Негодный файл владельца — `PolicyError` (тулы базы закрываются
    `config_invalid`, пока владелец не починит, как при негодном `policy.yaml`)."""
    файлы = owner_rules_files(home)
    if not файлы:
        return package_rules()
    владелец = _собрать_слой([(str(путь), _прочитать(путь)) for путь in файлы])
    return _каталог(_слой_поставки(), владелец)


def _прочитать(путь: pathlib.Path) -> str:
    """Текст файла каталога владельца; нечитаемый файл — `PolicyError`, как негодная разметка
    (ревью атакующим, находка 5: `UnicodeDecodeError` уходил мимо и закрывал тулы `internal`)."""
    try:
        return путь.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise _ошибка(путь, "файл не читается как UTF-8") from None
    except OSError:
        raise _ошибка(путь, "файл не читается") from None
