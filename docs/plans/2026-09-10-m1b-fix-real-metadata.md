# M1b-fix — индекс и правила полей по реальному `$metadata` 1С

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** индекс метаданных и авто-классификация полей строятся на реальной базе 1С правильно:
действия и виртуальные таблицы привязаны к сущностям, табличные части и независимые регистры
определяются без ложных срабатываний, составные поля распознаны, а гейт не токенизирует GUID
ссылок и номера счетов-фактур.

**Architecture:** разбор EDMX (`src/odata1c/index/edmx.py`) перестаёт выводить структуру из
разбиения имени по `_` и опирается на факты описания: список наборов, ключи, `bindingParameter`
действий, `ComplexType` результатов, `EnumType`. Разбор имени (`naming.py`) сводится к отделению
вида. Хранилище получает признак набора записей регистра, таблицу перечислений и версию разбора,
чтобы старый неверный индекс перестраивался сам. В гейте — два точечных правила.

**Tech Stack:** Python 3.12, `lxml` (iterparse), SQLite, `pytest`, `ruff`.

**Spec:** `SPEC.md` §4.2 (поправка 2026-09-10), §6.4 (сноска ⁴), §9 (поправка 2026-09-10);
факты — `docs/probes/P4-real-metadata.md`. Образцы: `tests/fixtures/edmx/ut-real.edmx` (урезанная
реальная УТ, в git), `tests/fixtures/edmx/synthetic.edmx` (синтетика прежних тестов).

## Global Constraints

- Язык комментариев, docstring, сообщений коммитов — русский; идентификаторы кода — как в коде
  (в модулях индекса локальные имена кириллицей — соблюдай стиль файла).
- Байтовый литерал с кириллицей (`b"…"`) синтаксически невозможен — только `"…".encode()`.
- Инвариант 3: физическое удаление разрешено только записям **независимых** регистров сведений —
  ложный признак независимости открывает удаление подчинённым регистрам.
- Инвариант 6: суммы, количества, даты, GUID, коды и номера документов не защищаются ни на одном
  уровне.
- Команды: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .` — чисто перед
  каждым коммитом. Коммит заканчивается строкой `Co-Authored-By:` исполнителя.
- Тесты, закреплявшие выдуманную структуру (виртуальные таблицы-наборы с суффиксами, действия с
  атрибутом `EntitySet`), не ослабляются, а переводятся на реальный образец.

## Файлы

| Файл | Что меняется | Задача |
|---|---|---|
| `src/odata1c/index/naming.py` | `parse_entity_name` только отделяет вид; `VIRTUAL_SUFFIXES` удалён | 1 |
| `src/odata1c/index/edmx.py` | родитель по списку наборов, наборы записей, регистратор, составные строки; действия по `bindingParameter`; виртуальные таблицы; перечисления; `PARSER_VERSION` | 1, 2, 3 |
| `tests/fixtures/edmx/synthetic.edmx` | убраны выдуманные виртуальные наборы; действия в реальной форме | 2 |
| `tests/unit/test_index_edmx.py`, `test_index_naming.py` | переписаны тесты структуры; новые — на `ut-real.edmx` | 1, 2, 3 |
| `src/odata1c/index/schema.py`, `repository.py` | `is_records`, таблица `enums`, `parser_version`, расширенный `describe` | 3 |
| `src/odata1c/index/reindex.py`, `src/odata1c/cli.py` | перестройка при смене версии разбора; предупреждения разбора | 3 |
| `tests/unit/test_index_repository.py`, `test_index_reindex.py` | новые проверки | 3 |
| `src/odata1c/gate/field_rules.py`, `masking.py` | `номерсчетафактур` не `acc`; GUID не токенизируется | 4 |
| `tests/unit/test_gate_field_rules.py`, `test_gate_masking.py` | новые проверки | 4 |
| `tools/probes/p4_metadata_probe.py` | убрать импорт `VIRTUAL_SUFFIXES` | 1 |
| `tools/probes/p4_index_check.py`, `docs/probes/P4-real-metadata.md` | приёмка на живой базе | 5 |
| `SPEC.md` §4.2 | схема `metadata.sqlite` дополнена | 3 |

Порядок: 1 → 2 → 3 → 5; задача 4 от 1–3 не зависит и может идти параллельно с ними.

---

### Задача 1: структура наборов — родитель, набор записей, регистратор, составные поля

**Files:**
- Modify: `src/odata1c/index/naming.py` (`EntityName`, `parse_entity_name`, удалить `VIRTUAL_SUFFIXES`)
- Modify: `src/odata1c/index/edmx.py` (`ParsedEntity`, `parse_edmx`, `_собрать_сущность`, `_пометить_ссылки_и_составные`)
- Modify: `tools/probes/p4_metadata_probe.py` (строка импорта `VIRTUAL_SUFFIXES` и блок «известные суффиксы среди EntitySet»)
- Test: `tests/unit/test_index_edmx.py`, `tests/unit/test_index_naming.py`, `tests/unit/conftest.py`

**Interfaces:**
- Produces: `parse_entity_name(name) -> EntityName(full, kind, russian_kind, base_name)` — без
  признаков структуры; `ParsedEntity.is_records: bool` (новое поле, после `is_tabular_part`);
  функция `_родитель(имя: str, наборы: Collection[str]) -> tuple[str, str] | None`;
  константа `РЕГИСТРАТОРЫ = ("Recorder", "Recorder_Key")`.

- [ ] **Шаг 1: фикстура реального образца**

В `tests/unit/conftest.py` рядом с `edmx_synthetic` добавить:

```python
@pytest.fixture(scope="session")
def edmx_ut_real() -> bytes:
    """Урезанный реальный $metadata УТ (проба P4): структура, которую синтетика не воспроизводит."""
    return (ОБРАЗЦЫ / "ut-real.edmx").read_bytes()
```

(Если `edmx_synthetic` объявлена без `scope`, объяви новую так же — стиль файла важнее.)

- [ ] **Шаг 2: падающие тесты на реальном образце**

В конец `tests/unit/test_index_edmx.py`:

```python
ТАБЛИЧНЫЕ_ЧАСТИ_UT = {
    "BusinessProcess_пр_БизнесПроцессСогласованияОрдеров_РезультатыСогласования",
    "Catalog_Контрагенты_КонтактнаяИнформация",
    "Catalog_СерииНоменклатуры_ДополнительныеРеквизиты",
    "Document_ВводОстатковРасчетовПоЭквайрингу_РасчетыПоЭквайрингу",
    "Document_РеализацияТоваровУслуг_Товары",
    "Task_пр_ЗадачаСогласования_ОбъектыСогласования",
}
НАБОРЫ_ЗАПИСЕЙ_UT = {
    "AccumulationRegister_ДвиженияДенежныеСредстваКонтрагент_RecordType",
    "AccumulationRegister_РасчетыСКлиентамиПланОплат_RecordType",
    "AccumulationRegister_пр_ВыпускПроукцииПоСменам_RecordType",
    "InformationRegister_ЖурналУчетаСчетовФактур_RecordType",
    "InformationRegister_СтоимостьТоваров_RecordType",
    "InformationRegister_пр_ОчередьДействий_RecordType",
}
НЕЗАВИСИМЫЕ_UT = {
    "InformationRegister_ДокументыФизическихЛиц",
    "InformationRegister_КурсыВалют",
    "InformationRegister_ПоследнийОбменСБанками",
}


def _наборы(разобрано):
    return [с for с in разобрано.entities if not с.is_virtual and с.kind != "Enum"]


def test_ut_табличные_части_ровно_по_списку_наборов(edmx_ut_real):
    разобрано = parse_edmx(edmx_ut_real)
    assert {с.name for с in _наборы(разобрано) if с.is_tabular_part} == ТАБЛИЧНЫЕ_ЧАСТИ_UT


def test_ut_подчёркивание_в_имени_объекта_не_табличная_часть(edmx_ut_real):
    разобрано = parse_edmx(edmx_ut_real)
    for имя, основа in [
        ("InformationRegister_пр_ОчередьДействий", "пр_ОчередьДействий"),
        ("Constant_Xx_АвтоматическиСоздаватьАктыРасхождений", "Xx_АвтоматическиСоздаватьАктыРасхождений"),
        (
            "ExchangePlan_Удалить_ОбменУправлениеТорговлей_11_0_РозничнаяТорговля_1_0",
            "Удалить_ОбменУправлениеТорговлей_11_0_РозничнаяТорговля_1_0",
        ),
    ]:
        сущность = найти(разобрано, имя)
        assert сущность.is_tabular_part is False, имя
        assert сущность.parent_entity is None, имя
        assert сущность.base_name == основа, имя


def test_ut_наборы_записей_регистров(edmx_ut_real):
    разобрано = parse_edmx(edmx_ut_real)
    записи = {с.name for с in _наборы(разобрано) if с.is_records}
    assert записи == НАБОРЫ_ЗАПИСЕЙ_UT
    for имя in записи:
        сущность = найти(разобрано, имя)
        assert сущность.is_tabular_part is False
        assert сущность.parent_entity == имя.removesuffix("_RecordType")
        assert сущность.base_name == найти(разобрано, сущность.parent_entity).base_name


def test_ut_регистратор_с_одним_типом(edmx_ut_real):
    разобрано = parse_edmx(edmx_ut_real)
    assert найти(разобрано, "InformationRegister_пр_ОчередьДействий").has_recorder is True
    assert найти(разобрано, "InformationRegister_пр_ОчередьДействий_RecordType").has_recorder is True


def test_ut_набор_записей_наследует_регистратор_основного_набора(edmx_ut_real):
    # Ключ СтоимостьТоваров_RecordType — Period и измерения, без Recorder; регистр подчинённый.
    записи = найти(parse_edmx(edmx_ut_real), "InformationRegister_СтоимостьТоваров_RecordType")
    assert "Recorder" not in записи.key_fields
    assert записи.has_recorder is True
    assert записи.is_independent_register is False


def test_ut_независимые_регистры_ровно_по_списку(edmx_ut_real):
    разобрано = parse_edmx(edmx_ut_real)
    assert {с.name for с in разобрано.entities if с.is_independent_register} == НЕЗАВИСИМЫЕ_UT


def test_ut_составное_строковое_поле_это_ссылка(edmx_ut_real):
    журнал = найти(parse_edmx(edmx_ut_real), "InformationRegister_ЖурналУчетаСчетовФактур_RecordType")
    поля = {п.name: п for п in журнал.fields}
    for имя in ("Контрагент", "Recorder", "СчетФактура"):
        assert поля[имя].edm_type == "Edm.String"
        assert поля[имя].is_ref is True, имя
        assert поля[имя].is_composite is True, имя
    assert поля["Контрагент_Type"].is_ref is False
    assert поля["НомерСчетаФактуры"].is_ref is False  # строка без пары _Type — значение
```

- [ ] **Шаг 3: убедиться, что тесты падают**

Run: `uv run pytest tests/unit/test_index_edmx.py -k ut_ -v`
Expected: FAIL — `AttributeError: … is_records`, лишние табличные части (`InformationRegister_пр_ОчередьДействий` и др.), лишние независимые регистры, `is_ref False` у `Контрагент`.

- [ ] **Шаг 4: разбор имени — только вид**

`src/odata1c/index/naming.py`: удалить `VIRTUAL_SUFFIXES` и признаки структуры из `EntityName`:

```python
@dataclasses.dataclass(slots=True)
class EntityName:
    full: str
    kind: str
    russian_kind: str
    base_name: str


def parse_entity_name(name: str) -> EntityName:
    """Вид — префикс до первого `_`; всё остальное — имя объекта целиком.

    Табличную часть, набор записей регистра и виртуальную таблицу по имени отделить нельзя:
    подчёркивания и цифры бывают в именах объектов (`InformationRegister_пр_ОчередьДействий`,
    `ExchangePlan_…_11_0_…`, проба P4). Структуру определяет разбор описания (edmx.py) по списку
    наборов, ключам и действиям.
    """
    kind, _, остаток = name.partition("_")
    return EntityName(
        full=name, kind=kind, russian_kind=KINDS.get(kind, kind), base_name=остаток or name
    )
```

В `tests/unit/test_index_naming.py` удалить тесты, проверявшие табличные части и виртуальные
суффиксы по имени (`test_табличная_часть_документа`, `test_виртуальная_таблица_остатков`,
`test_виртуальная_таблица_среза_последних`, `test_составной_суффикс_остатков_и_оборотов`,
`test_двойное_подчёркивание_в_имени`), а в `test_справочник` убрать проверки `parent`,
`is_tabular_part`, `is_virtual`. Добавить:

```python
def test_подчёркивание_в_имени_объекта_остаётся_в_имени():
    имя = parse_entity_name("InformationRegister_пр_ОчередьДействий")
    assert имя.kind == "InformationRegister"
    assert имя.base_name == "пр_ОчередьДействий"
```

В `tools/probes/p4_metadata_probe.py` убрать импорт `VIRTUAL_SUFFIXES` и цикл «известные
суффиксы среди EntitySet» (проба остаётся исполняемой: `uv run python -c "import ast,sys;
ast.parse(open('tools/probes/p4_metadata_probe.py',encoding='utf-8').read())"`).

- [ ] **Шаг 5: структура в разборе описания**

`src/odata1c/index/edmx.py`. Добавить поле в `ParsedEntity` (после `is_tabular_part`):

```python
    is_records: bool
```

Константы и функция родителя:

```python
РЕГИСТРАТОРЫ = ("Recorder", "Recorder_Key")
НАБОР_ЗАПИСЕЙ = "RecordType"
КЛЮЧ_ТАБЛИЧНОЙ_ЧАСТИ = frozenset({"Ref_Key", "LineNumber"})


def _родитель(имя: str, наборы) -> tuple[str, str] | None:
    """Самый длинный опубликованный набор P, для которого имя == P + "_" + хвост.

    Поиск справа налево: `Catalog_A_B_C` сначала проверяет `Catalog_A_B`, затем `Catalog_A`.
    Имя объекта с подчёркиванием (`InformationRegister_пр_ОчередьДействий`) родителя не находит:
    набора `InformationRegister_пр` нет.
    """
    позиция = len(имя)
    while (позиция := имя.rfind("_", 0, позиция)) > 0:
        кандидат = имя[:позиция]
        if кандидат in наборы:
            хвост = имя[позиция + 1 :]
            return (кандидат, хвост) if хвост else None
    return None
```

В `parse_edmx` заменить сборку сущностей: сначала для каждого набора определить роль, затем
собрать. Роль:
- `(P, "RecordType")` → набор записей регистра: `is_records=True`, родитель `P`;
- `(P, хвост)` и множество ключей равно `КЛЮЧ_ТАБЛИЧНОЙ_ЧАСТИ` → табличная часть, родитель `P`;
- иначе — самостоятельный набор, родителя нет.

```python
    for имя_набора, имя_типа in наборы.items():
        поля = типы.get(имя_типа) or типы.get(имя_набора)
        if поля is None:
            нераспознанные_наборы.append(имя_набора)
            continue
        родитель = _родитель(имя_набора, наборы)
        записи = родитель is not None and родитель[1] == НАБОР_ЗАПИСЕЙ
        табличная = (
            родитель is not None and not записи and set(поля[1]) == КЛЮЧ_ТАБЛИЧНОЙ_ЧАСТИ
        )
        основной = родитель[0] if записи else имя_набора
        ключи_основного = (типы.get(наборы.get(основной, "")) or поля)[1]
        сущности.append(
            _собрать_сущность(
                имя_набора,
                поля,
                parent=родитель[0] if (записи or табличная) else None,
                is_tabular_part=табличная,
                is_records=записи,
                has_recorder=any(ключ in РЕГИСТРАТОРЫ for ключ in ключи_основного),
                base_name=(
                    parse_entity_name(родитель[0]).base_name
                    if (записи or табличная)
                    else parse_entity_name(имя_набора).base_name
                ),
            )
        )
```

`_собрать_сущность` принимает эти значения вместо вывода из имени:

```python
def _собрать_сущность(
    имя: str,
    поля_и_ключи: tuple[list[ParsedField], list[str]],
    *,
    parent: str | None,
    is_tabular_part: bool,
    is_records: bool,
    has_recorder: bool,
    base_name: str,
    is_virtual: bool = False,
    virtual_kind: str | None = None,
) -> ParsedEntity:
    поля, ключи = поля_и_ключи
    вид = parse_entity_name(имя)
    имена_полей = {поле.name for поле in поля}
    return ParsedEntity(
        name=имя,
        kind=вид.kind,
        russian_kind=вид.russian_kind,
        base_name=base_name,
        parent_entity=parent,
        is_tabular_part=is_tabular_part,
        is_records=is_records,
        is_virtual=is_virtual,
        virtual_kind=virtual_kind,
        key_fields=list(ключи),
        description_field=ПОЛЕ_ОПИСАНИЯ if ПОЛЕ_ОПИСАНИЯ in имена_полей else None,
        has_posted="Posted" in имена_полей,
        has_recorder=has_recorder,
        # Независимость — свойство регистра, а не набора: у набора записей подчинённого регистра
        # регистратора в ключе может не быть (СтоимостьТоваров_RecordType), признак берётся
        # у основного набора через has_recorder. Инвариант 3: ложная независимость открывает
        # физическое удаление подчинённому регистру.
        is_independent_register=вид.kind == "InformationRegister"
        and not is_virtual
        and not has_recorder,
        fields=поля,
    )
```

Составные поля: в `_пометить_ссылки_и_составные` признак составного поля распространить на
`Edm.String`:

```python
def _пометить_ссылки_и_составные(поля: list[ParsedField]) -> None:
    """Ссылочные и составные поля (SPEC §4.2, поправка 2026-09-10; §9).

    `Edm.Guid` с суффиксом `_Key` — ссылка одного типа. Составное поле опознаётся по парному
    `<база>_Type` (суффикс `_Key` при сравнении отбрасывается): у ссылки составного типа в 1С тип
    `Edm.String` — значение приходит строкой GUID, в `_Type` имя набора; для примитивного типа
    в поле само значение (проба P4). Такое поле помечается ссылкой-кандидатом; решение по
    конкретному значению принимает гейт.
    """
    имена = {поле.name for поле in поля}
    for поле in поля:
        if поле.name.endswith("_Type"):
            continue
        базовое_имя = поле.name.removesuffix("_Key")
        составное = f"{базовое_имя}_Type" in имена
        if поле.edm_type == "Edm.Guid":
            поле.is_ref = поле.name.endswith("_Key") or составное
            поле.is_composite = составное
        elif поле.edm_type == "Edm.String" and составное:
            поле.is_ref = True
            поле.is_composite = True
```

`СЛУЖЕБНЫЕ_ТИПЫ` больше не используется — удалить.

- [ ] **Шаг 6: прежние тесты синтетики**

Run: `uv run pytest tests/unit -q`
Тесты `test_табличная_часть_привязана_к_родителю`, `test_независимый_регистр_сведений`,
`test_регистр_с_регистратором_не_независимый`, `test_регистратор_с_несколькими_типами…`,
`test_регистр_накопления_не_независимый_регистр_сведений` должны пройти без правки ожиданий. Тесты
виртуальных таблиц и действий синтетики (`test_виртуальная_таблица_*`, `test_действия_разобраны_с_параметрами`)
упадут или изменят смысл — их правит задача 2; в этой задаче пометь их
`@pytest.mark.skip(reason="переводится на реальную форму в задаче 2 плана M1b-fix")`.
Все прочие тесты — зелёные; тесты `ut_` из шага 2 — зелёные.

- [ ] **Шаг 7: мутационная проверка**

Проверь, что тесты ловят каждую из поломок (по одной, с возвратом кода):
1. в `_родитель` вернуть `None` всегда → падает `test_ut_табличные_части…` и `test_ut_наборы_записей…`;
2. `РЕГИСТРАТОРЫ = ("Recorder",)` → падает `test_ut_регистратор_с_одним_типом`;
3. `has_recorder` считать по ключам самого набора, а не основного → падает `test_ut_набор_записей_наследует…`;
4. убрать ветку `Edm.String` в `_пометить_ссылки_и_составные` → падает `test_ut_составное_строковое_поле…`.

`git diff src` после проверки — только твои правки.

- [ ] **Шаг 8: коммит**

```bash
uv run ruff format src tests tools && uv run ruff check . && uv run pytest -q
git add src/odata1c/index/naming.py src/odata1c/index/edmx.py tests/unit/conftest.py tests/unit/test_index_edmx.py tests/unit/test_index_naming.py tools/probes/p4_metadata_probe.py
git commit -m "fix: структура наборов по описанию — табличные части, записи регистров, регистратор, составные поля"
```

---

### Задача 2: действия и виртуальные таблицы

**Files:**
- Modify: `src/odata1c/index/edmx.py` (`ParsedAction`, `ParsedMetadata`, `_разобрать_типы`, `_разобрать_контейнер`, `_разобрать_действие`, `parse_edmx`)
- Modify: `tests/fixtures/edmx/synthetic.edmx`
- Test: `tests/unit/test_index_edmx.py`, `tests/unit/test_index_repository.py`

**Interfaces:**
- Consumes: задача 1 — `_собрать_сущность(..., is_virtual=, virtual_kind=)`, `_родитель`.
- Produces: `ParsedAction(entity, name, params, http_method, returns, side_effecting: bool)`;
  `ParsedMetadata.warnings: list[str]`; виртуальная таблица — `ParsedEntity` с именем
  `<основной набор регистра>_<Действие>` (`AccumulationRegister_X_Balance`,
  `InformationRegister_X_SliceLast`), `is_virtual=True`, `virtual_kind=<Действие>`,
  `parent_entity=<набор, к которому привязано действие>` (адрес вызова:
  `<parent_entity>/<virtual_kind>(…)`), `key_fields=[]`, поля — из `ComplexType` результата либо
  из типа записи; её параметры — одно действие в `actions` с `entity=<имя виртуальной таблицы>`,
  `name=<Действие>`, `http_method="GET"`, `side_effecting=False`.

Правила разбора `FunctionImport` (SPEC §4.2, поправка 2026-09-10):
- привязка — тип параметра `bindingParameter` (без пространства имён) → набор, чей
  `EntityType` равен этому типу; атрибут `EntitySet` не используется; параметр `bindingParameter`
  в `params` не попадает;
- `side_effecting = IsSideEffecting != "false"`; `http_method = "POST"` для действий с побочным
  эффектом, `"GET"` — без;
- действие без побочного эффекта с `ReturnType="Collection(…)"` — виртуальная таблица; прочие —
  действия сущности (`Post`, `Unpost`, `Start`, `ExecuteTask`);
- имя виртуальной таблицы строится от основного набора регистра: для привязки к
  `X_RecordType` — `X_<Действие>`, иначе `<набор>_<Действие>`; если такое имя уже занято набором
  или другой виртуальной таблицей — таблица не индексируется, в `warnings` пишется
  `"виртуальная таблица <набор>/<Действие> не проиндексирована: имя <имя> занято"`;
- действие, чей тип привязки не найден среди наборов, — в `warnings`:
  `"действие <Имя> не привязано: тип <тип> не опубликован"`.

- [ ] **Шаг 1: синтетика в реальной форме**

В `tests/fixtures/edmx/synthetic.edmx`:
- удалить `EntityType` и `EntitySet` `InformationRegister_КурсыВалют_SliceLast` и
  `AccumulationRegister_ТоварыНаСкладах_Balance`;
- заменить оба `FunctionImport` на реальную форму:

```xml
        <FunctionImport Name="Post" IsBindable="true" IsSideEffecting="true">
          <Parameter Name="bindingParameter" Type="StandardODATA.Document_РеализацияТоваровУслуг"/>
          <Parameter Name="PostingModeOperational" Type="Edm.Boolean"/>
        </FunctionImport>
        <FunctionImport Name="Unpost" IsBindable="true" IsSideEffecting="true">
          <Parameter Name="bindingParameter" Type="StandardODATA.Document_РеализацияТоваровУслуг"/>
        </FunctionImport>
        <FunctionImport Name="SliceLast" IsBindable="true" IsSideEffecting="false"
                        ReturnType="Collection(StandardODATA.InformationRegister_КурсыВалют)">
          <Parameter Name="bindingParameter" Type="StandardODATA.InformationRegister_КурсыВалют"/>
          <Parameter Name="Condition" Type="Edm.String"/>
          <Parameter Name="Period" Type="Edm.DateTime"/>
        </FunctionImport>
```

- в `test_index_edmx.py` снять `skip`, поставленные в задаче 1, и переписать эти тесты:
  `test_виртуальная_таблица_не_независимый_регистр` проверяет `InformationRegister_КурсыВалют_SliceLast`
  (теперь она строится из действия: `is_virtual`, `virtual_kind == "SliceLast"`,
  `parent_entity == "InformationRegister_КурсыВалют"`, `is_independent_register is False`);
  `test_действия_разобраны_с_параметрами` ищет действие `Post` с `entity ==
  "Document_РеализацияТоваровУслуг"`, `params == {"PostingModeOperational": "Edm.Boolean"}`,
  `http_method == "POST"`, `side_effecting is True`; тест `_Balance` синтетики удалить (остатки
  проверяются на реальном образце). Прочие тесты, упоминающие удалённые наборы
  (`test_index_repository.py`: имя действия `Document_РеализацияТоваровУслуг_Post` → `Post`), поправить
  под новые имена, не меняя того, что они проверяют.

- [ ] **Шаг 2: падающие тесты на реальном образце**

```python
ДЕЙСТВИЯ_UT = {
    ("Document_РеализацияТоваровУслуг", "Post"),
    ("Document_РеализацияТоваровУслуг", "Unpost"),
    ("Document_ВводОстатковРасчетовПоЭквайрингу", "Post"),
    ("Document_ВводОстатковРасчетовПоЭквайрингу", "Unpost"),
    ("BusinessProcess_пр_БизнесПроцессСогласованияОрдеров", "Start"),
    ("Task_пр_ЗадачаСогласования", "ExecuteTask"),
}
ВИРТУАЛЬНЫЕ_UT = {
    "AccumulationRegister_ДвиженияДенежныеСредстваКонтрагент_Turnovers":
        "AccumulationRegister_ДвиженияДенежныеСредстваКонтрагент",
    "AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance":
        "AccumulationRegister_РасчетыСКлиентамиПланОплат",
    "AccumulationRegister_РасчетыСКлиентамиПланОплат_Turnovers":
        "AccumulationRegister_РасчетыСКлиентамиПланОплат",
    "AccumulationRegister_РасчетыСКлиентамиПланОплат_BalanceAndTurnovers":
        "AccumulationRegister_РасчетыСКлиентамиПланОплат",
    "AccumulationRegister_пр_ВыпускПроукцииПоСменам_Turnovers":
        "AccumulationRegister_пр_ВыпускПроукцииПоСменам",
    "InformationRegister_пр_ОчередьДействий_SliceLast": "InformationRegister_пр_ОчередьДействий_RecordType",
    "InformationRegister_пр_ОчередьДействий_SliceFirst": "InformationRegister_пр_ОчередьДействий_RecordType",
    "InformationRegister_СтоимостьТоваров_SliceLast": "InformationRegister_СтоимостьТоваров_RecordType",
    "InformationRegister_СтоимостьТоваров_SliceFirst": "InformationRegister_СтоимостьТоваров_RecordType",
    "InformationRegister_ЖурналУчетаСчетовФактур_SliceLast": "InformationRegister_ЖурналУчетаСчетовФактур_RecordType",
    "InformationRegister_ЖурналУчетаСчетовФактур_SliceFirst": "InformationRegister_ЖурналУчетаСчетовФактур_RecordType",
    "InformationRegister_ПоследнийОбменСБанками_SliceLast": "InformationRegister_ПоследнийОбменСБанками",
    "InformationRegister_ПоследнийОбменСБанками_SliceFirst": "InformationRegister_ПоследнийОбменСБанками",
    "InformationRegister_КурсыВалют_SliceLast": "InformationRegister_КурсыВалют",
    "InformationRegister_КурсыВалют_SliceFirst": "InformationRegister_КурсыВалют",
    "InformationRegister_ДокументыФизическихЛиц_SliceLast": "InformationRegister_ДокументыФизическихЛиц",
    "InformationRegister_ДокументыФизическихЛиц_SliceFirst": "InformationRegister_ДокументыФизическихЛиц",
}


def test_ut_действия_привязаны_по_типу_параметра(edmx_ut_real):
    разобрано = parse_edmx(edmx_ut_real)
    с_эффектом = {(д.entity, д.name) for д in разобрано.actions if д.side_effecting}
    assert с_эффектом == ДЕЙСТВИЯ_UT
    post = next(д for д in разобрано.actions if д.name == "Post")
    assert post.params == {"PostingModeOperational": "Edm.Boolean"}
    assert post.http_method == "POST"
    assert разобрано.warnings == []


def test_ut_виртуальные_таблицы_из_действий(edmx_ut_real):
    разобрано = parse_edmx(edmx_ut_real)
    виртуальные = {с.name: с for с in разобрано.entities if с.is_virtual}
    assert {имя: с.parent_entity for имя, с in виртуальные.items()} == ВИРТУАЛЬНЫЕ_UT
    for с in виртуальные.values():
        assert с.virtual_kind == с.name.rsplit("_", 1)[1]
        assert с.is_independent_register is False
        assert с.key_fields == []


def test_ut_поля_остатков_из_сложного_типа(edmx_ut_real):
    остатки = найти(parse_edmx(edmx_ut_real), "AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance")
    поля = {п.name: п for п in остатки.fields}
    assert "КОплатеBalance" in поля
    assert поля["ОбъектРасчетов_Key"].is_ref is True
    assert поля["ДокументПлан"].is_composite is True  # пара ДокументПлан_Type в ComplexType


def test_ut_параметры_виртуальной_таблицы(edmx_ut_real):
    разобрано = parse_edmx(edmx_ut_real)
    параметры = {
        д.entity: д for д in разобрано.actions if not д.side_effecting
    }["AccumulationRegister_РасчетыСКлиентамиПланОплат_Turnovers"]
    assert параметры.name == "Turnovers"
    assert параметры.http_method == "GET"
    assert set(параметры.params) == {"Condition", "Dimensions", "StartPeriod", "EndPeriod"}


def test_занятое_имя_виртуальной_таблицы_даёт_предупреждение():
    тело = """
      <EntityType Name="AccumulationRegister_А"><Key><PropertyRef Name="Recorder_Key"/></Key>
        <Property Name="Recorder_Key" Type="Edm.Guid" Nullable="false"/></EntityType>
      <EntityType Name="AccumulationRegister_А_Balance"><Key><PropertyRef Name="Ref_Key"/></Key>
        <Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/></EntityType>
      <ComplexType Name="AccumulationRegister_А_BalanceRow">
        <Property Name="СуммаBalance" Type="Edm.Decimal"/></ComplexType>
      <EntityContainer Name="StandardODATA">
        <EntitySet Name="AccumulationRegister_А" EntityType="StandardODATA.AccumulationRegister_А"/>
        <EntitySet Name="AccumulationRegister_А_Balance" EntityType="StandardODATA.AccumulationRegister_А_Balance"/>
        <FunctionImport Name="Balance" IsBindable="true" IsSideEffecting="false"
            ReturnType="Collection(StandardODATA.AccumulationRegister_А_BalanceRow)">
          <Parameter Name="bindingParameter" Type="StandardODATA.AccumulationRegister_А"/>
        </FunctionImport>
      </EntityContainer>"""
    разобрано = parse_edmx(обёртка_эдмкс(тело))
    assert len(разобрано.warnings) == 1
    assert "AccumulationRegister_А/Balance" in разобрано.warnings[0]
    assert not any(с.is_virtual for с in разобрано.entities)
```

(`обёртка_эдмкс` из `tests/unit/conftest.py` оборачивает только тело контейнера; если она
принимает лишь содержимое `EntityContainer`, расширь её необязательным параметром для типов
схемы или собери документ в тесте вручную по образцу функции.)

- [ ] **Шаг 3: убедиться, что тесты падают**

Run: `uv run pytest tests/unit/test_index_edmx.py -k "ut_ or занятое" -v`
Expected: FAIL — нет `side_effecting`/`warnings`, виртуальных таблиц 0.

- [ ] **Шаг 4: реализация**

`_разобрать_типы` читает и `EntityType`, и `ComplexType` в один словарь «имя типа → (поля,
ключи)» (у `ComplexType` ключей нет — пустой список; `_пометить_ссылки_и_составные` применяется
к обоим). Коллизия имён между `EntityType` и `ComplexType` в описании 1С невозможна (одно
пространство имён схемы) — отдельной обработки не нужно.

`_разобрать_контейнер` сохраняет для каждого `FunctionImport` сырые данные: имя,
`IsSideEffecting`, `ReturnType`, список параметров (имя, тип) — привязку выполняет `parse_edmx`,
когда известны наборы. Освобождение узлов (`элемент.clear()`) остаётся: всё нужное копируется
в структуру до очистки.

```python
@dataclasses.dataclass(slots=True)
class ParsedAction:
    entity: str
    name: str
    params: dict[str, str]
    http_method: str
    returns: str | None = None
    side_effecting: bool = True


def _сырое_действие(элемент) -> dict:
    параметры = [
        (параметр.get("Name"), _без_пространства(параметр.get("Type", "")))
        for параметр in элемент.iter()
        if etree.QName(параметр).localname == "Parameter"
    ]
    return {
        "name": элемент.get("Name", ""),
        "side_effecting": элемент.get("IsSideEffecting", "true") != "false",
        "returns": элемент.get("ReturnType"),
        "params": параметры,
    }


def _тип_коллекции(значение: str | None) -> str | None:
    """`Collection(StandardODATA.X)` → `X`; не коллекция → None."""
    if not значение or not значение.startswith("Collection(") or not значение.endswith(")"):
        return None
    return _без_пространства(значение[len("Collection(") : -1])
```

`_без_пространства` должен снимать и префикс внутри `Collection(...)`: применяй его к
внутреннему типу, как в `_тип_коллекции`.

В `parse_edmx`, после сборки сущностей наборов:

```python
    набор_по_типу = {имя_типа: имя_набора for имя_набора, имя_типа in наборы.items()}
    занятые = set(наборы)
    for сырое in сырые_действия:
        параметры = dict(сырое["params"])
        тип_привязки = параметры.pop("bindingParameter", None)
        набор = набор_по_типу.get(тип_привязки or "")
        if набор is None:
            предупреждения.append(
                f"действие {сырое['name']} не привязано: тип {тип_привязки} не опубликован"
            )
            continue
        результат = _тип_коллекции(сырое["returns"])
        if сырое["side_effecting"] or результат is None:
            действия.append(
                ParsedAction(
                    entity=набор,
                    name=сырое["name"],
                    params=параметры,
                    http_method="POST" if сырое["side_effecting"] else "GET",
                    returns=_без_пространства(сырое["returns"] or "") or None,
                    side_effecting=сырое["side_effecting"],
                )
            )
            continue
        родитель = _родитель(набор, наборы)
        основной = родитель[0] if родитель and родитель[1] == НАБОР_ЗАПИСЕЙ else набор
        имя = f"{основной}_{сырое['name']}"
        поля = типы.get(результат)
        if имя in занятые or поля is None:
            причина = f"имя {имя} занято" if имя in занятые else f"тип {результат} не описан"
            предупреждения.append(
                f"виртуальная таблица {набор}/{сырое['name']} не проиндексирована: {причина}"
            )
            continue
        занятые.add(имя)
        сущности.append(
            _собрать_сущность(
                имя,
                (copy.deepcopy(поля[0]), []),
                parent=набор,
                is_tabular_part=False,
                is_records=False,
                has_recorder=False,
                base_name=parse_entity_name(основной).base_name,
                is_virtual=True,
                virtual_kind=сырое["name"],
            )
        )
        действия.append(
            ParsedAction(
                entity=имя,
                name=сырое["name"],
                params=параметры,
                http_method="GET",
                returns=результат,
                side_effecting=False,
            )
        )
```

(Добавь `import copy` в заголовок модуля.) `copy.deepcopy` обязателен: тип записи `SliceLast` делит список полей с самим набором, а
классификатор реиндекса записывает класс на объект поля. `ParsedMetadata` получает поле
`warnings: list[str] = dataclasses.field(default_factory=list)`.

- [ ] **Шаг 5: прогон и мутации**

Run: `uv run pytest -q` — всё зелёное.
Мутации (по одной, с возвратом): привязка по атрибуту `EntitySet` вместо `bindingParameter` →
падает `test_ut_действия_привязаны…`; имя виртуальной таблицы от набора привязки без снятия
`_RecordType` → падает `test_ut_виртуальные_таблицы…`; без `deepcopy` → проверь вручную, что
класс поля в виртуальной таблице не меняет поле набора (добавь тест, если мутация проходит
незамеченной).

- [ ] **Шаг 6: коммит**

```bash
uv run ruff format src tests && uv run ruff check . && uv run pytest -q
git add src/odata1c/index/edmx.py tests/fixtures/edmx/synthetic.edmx tests/unit/
git commit -m "fix: действия и виртуальные таблицы по привязке FunctionImport"
```

---

### Задача 3: хранилище — набор записей, перечисления, версия разбора

**Files:**
- Modify: `src/odata1c/index/edmx.py` (`ParsedMetadata.enums`, чтение `EnumType`, `PARSER_VERSION`)
- Modify: `src/odata1c/index/schema.py`, `src/odata1c/index/repository.py`
- Modify: `src/odata1c/index/reindex.py`, `src/odata1c/cli.py` (печать предупреждений)
- Modify: `SPEC.md` §4.2 (блок схемы)
- Test: `tests/unit/test_index_edmx.py`, `tests/unit/test_index_repository.py`, `tests/unit/test_index_reindex.py`

**Interfaces:**
- Consumes: задачи 1–2 — `ParsedEntity.is_records`, `ParsedMetadata.warnings`.
- Produces: `PARSER_VERSION: str = "2"` в `edmx.py`; `ParsedMetadata.enums: dict[str, list[str]]`;
  перечисление в `entities` — строка `name="Enum_<Имя>"`, `kind="Enum"`,
  `russian_kind="Перечисление"`, без полей; `EntityDescription` дополнен полями
  `parent_entity: str | None`, `is_tabular_part: bool`, `is_records: bool`, `is_virtual: bool`,
  `virtual_kind: str | None`, `members: list[str]`; `IndexRepository.meta("parser_version")`;
  `ReindexResult.warnings: list[str]`.

- [ ] **Шаг 1: падающие тесты**

`tests/unit/test_index_edmx.py`:

```python
def test_ut_перечисления_разобраны(edmx_ut_real):
    разобрано = parse_edmx(edmx_ut_real)
    assert set(разобрано.enums) == {"ХозяйственныеОперации", "СтатусыТаможенныхДеклараций"}
    assert "ОплатаПоставщику" in разобрано.enums["ХозяйственныеОперации"]
    перечисление = найти(разобрано, "Enum_ХозяйственныеОперации")
    assert перечисление.kind == "Enum"
    assert перечисление.fields == []
```

`tests/unit/test_index_repository.py` (фикстура на реальном образце рядом с `индекс`):

```python
@pytest.fixture
def индекс_ut(tmp_path, edmx_ut_real):
    хранилище = IndexRepository(tmp_path / "ut.sqlite")
    хранилище.write(parse_edmx(edmx_ut_real))
    yield хранилище
    хранилище.close()


def test_описание_набора_записей(индекс_ut):
    описание = индекс_ut.describe("InformationRegister_СтоимостьТоваров_RecordType")
    assert описание.is_records is True
    assert описание.parent_entity == "InformationRegister_СтоимостьТоваров"
    assert описание.is_independent_register is False


def test_описание_виртуальной_таблицы_с_параметрами(индекс_ut):
    описание = индекс_ut.describe("AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance")
    assert описание.is_virtual is True
    assert описание.virtual_kind == "Balance"
    assert описание.parent_entity == "AccumulationRegister_РасчетыСКлиентамиПланОплат"
    assert [д["name"] for д in описание.actions] == ["Balance"]
    assert set(описание.actions[0]["params"]) == {"Condition", "Dimensions", "Period"}


def test_виртуальные_таблицы_видны_у_регистра_как_дети(индекс_ut):
    описание = индекс_ut.describe("AccumulationRegister_РасчетыСКлиентамиПланОплат")
    assert "AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance" in описание.children
    assert "AccumulationRegister_РасчетыСКлиентамиПланОплат_RecordType" in описание.children


def test_перечисление_описано_значениями_и_находится_поиском(индекс_ut):
    описание = индекс_ut.describe("Enum_ХозяйственныеОперации")
    assert "ОплатаПоставщику" in описание.members
    assert "Enum_ХозяйственныеОперации" in [н.name for н in индекс_ut.find("хозяйственные операции")]


def test_версия_разбора_записана(индекс_ut):
    from odata1c.index.edmx import PARSER_VERSION

    assert индекс_ut.meta("parser_version") == PARSER_VERSION
```

`tests/unit/test_index_reindex.py` — по образцу существующих тестов реиндекса с поддельным
клиентом:

```python
async def test_смена_версии_разбора_перестраивает_индекс_без_force(...):
    # 1) реиндекс образца → changed=True;
    # 2) в индексе вручную записать meta parser_version = "1" (прямым UPDATE через sqlite3);
    # 3) повторный реиндекс того же $metadata без force → changed=True (а не «без изменений»).
```

Оформи его в стиле соседних тестов файла (их фикстуры клиента и домашнего каталога); шаги в
комментарии — обязательная логика теста.

- [ ] **Шаг 2: убедиться, что тесты падают**

Run: `uv run pytest tests/unit/test_index_edmx.py tests/unit/test_index_repository.py tests/unit/test_index_reindex.py -q`
Expected: FAIL — нет `enums`, `is_records` в описании, `parser_version`.

- [ ] **Шаг 3: схема и хранилище**

`schema.py`: в `entities` колонка `is_records INTEGER NOT NULL DEFAULT 0` (после
`is_tabular_part`); новая таблица

```sql
CREATE TABLE IF NOT EXISTS enums (
    entity_id INTEGER PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    members_json TEXT NOT NULL DEFAULT '[]'
);
```

Старые файлы индекса без колонки не мигрируются: `CREATE TABLE IF NOT EXISTS` их не изменит,
а версия разбора (ниже) заставит реиндекс построить индекс заново во временном файле — старый
файл целиком заменяется. `IndexRepository.write` выполняется только над новым файлом.
Но `_прежнее_состояние` в `reindex.py` открывает **старый** файл — его запросы не должны
обращаться к новой колонке и таблице (сейчас не обращаются; сохрани это).

`repository.write`: вставка `is_records`; для сущностей `kind == "Enum"` — строка в `enums`
(`parsed.enums[сущность.base_name]`); удаление `DELETE FROM enums` в начале вместе с прочими;
`self._set_meta("parser_version", PARSER_VERSION)`.

`repository.describe`: заполнить новые поля `EntityDescription`; `members` — из `enums`
(пустой список для прочих). `children` уже выбирает по `parent_entity` — виртуальные таблицы и
наборы записей туда попадают сами. Для `InformationRegister_X_RecordType`-привязанных
`SliceLast`/`SliceFirst` дети видны у `X_RecordType` — это верно: вызывается именно он.

`edmx.py`: в проходе типов собирать `EnumType` → `{имя: [Member.Name…]}`; в `parse_edmx`
для каждого перечисления добавить сущность `Enum_<Имя>` (через `_собрать_сущность` с пустыми
полями и ключами, `base_name=<Имя>`), если имя не занято набором (иначе — в `warnings`).
`PARSER_VERSION = "2"` с комментарием: «повышать при любом изменении разбора, меняющем
содержимое индекса при том же $metadata».

- [ ] **Шаг 4: реиндекс**

`reindex.py`: `_прежнее_состояние` читает `parser_version`; ветка «без изменений» — только если
совпали и сумма, и версия:

```python
    if (
        not force
        and прежнее["sha256"] == разобрано.edmx_sha256
        and прежнее["parser_version"] == PARSER_VERSION
    ):
```

`ReindexResult.warnings = list(разобрано.warnings)` на ветке перестройки. В `cli.py` команда
`reindex` печатает предупреждения построчно после итога (найди место, где печатаются
`unresolved_entity_sets`, и сделай так же).

`SPEC.md` §4.2, блок схемы: добавить `is_records` в `entities`, строку
`enums(entity_id, members_json)`, `parser_version` в перечень ключей `meta`.

- [ ] **Шаг 5: прогон и коммит**

```bash
uv run ruff format src tests && uv run ruff check . && uv run pytest -q
git add src/odata1c/index/ src/odata1c/cli.py tests/unit/ SPEC.md
git commit -m "feat: индекс хранит наборы записей, перечисления и версию разбора"
```

---

### Задача 4: гейт — GUID не токенизируется, номер счёта-фактуры не счёт

**Files:**
- Modify: `src/odata1c/gate/field_rules.py` (правило `acc`)
- Modify: `src/odata1c/gate/masking.py` (`_обработать_строку`)
- Test: `tests/unit/test_gate_field_rules.py`, `tests/unit/test_gate_masking.py`

**Interfaces:**
- Consumes: ничего из задач 1–3.
- Produces: константа `GUID_RE` в `masking.py`; поведение: строка, целиком совпадающая с GUID,
  возвращается маскировщиком без изменений при любом классе поля и не попадает в словарь.

Обоснование (SPEC §6.4, сноска ⁴): на реальной УТ 180 из 791 авто-классифицированных полей —
составные (`Контрагент` + `Контрагент_Type`); для ссылки значение — GUID. Токен класса `org`
вместо GUID ломает навигацию модели, а сам GUID попадает в словарь как «название организации»,
и страж затем заменяет его во всех ответах, включая `Ref_Key`. Инвариант 6: GUID не защищается.

- [ ] **Шаг 1: падающие тесты**

`tests/unit/test_gate_field_rules.py`:

```python
@pytest.mark.parametrize(
    "поле",
    ["НомерСчетаФактуры", "НомерСчетаФактурыНаАванс", "НомерСчетаФактурыКомиссионера",
     "НомерСчетаФактурыПродавца"],
)
def test_номер_счета_фактуры_не_банковский_счет(поле):
    assert classify_field("InformationRegister_ЖурналУчетаСчетовФактур_RecordType", поле, "Edm.String") is None


@pytest.mark.parametrize("поле", ["НомерСчета", "НомерСчетаКонтрагента"])
def test_номер_банковского_счета_остаётся_счетом(поле):
    assert classify_field("Catalog_БанковскиеСчетаКонтрагентов", поле, "Edm.String") == ("acc", "auto")
```

`tests/unit/test_gate_masking.py` — по образцу существующих тестов файла (их фикстуры словаря,
политики и `Masker`), политика с `auto: {"Регистр.Контрагент": "org"}`, режим
`identifiers+names`:

```python
def test_guid_составной_ссылки_не_токенизируется(...):
    строка = {
        "Контрагент": "cc52b6ce-d9cc-11e4-b723-00237de09eb7",
        "Контрагент_Type": "StandardODATA.Catalog_Контрагенты",
    }
    результат = маскировщик.mask({"value": [строка]}, entity="Регистр")
    assert результат.data["value"][0]["Контрагент"] == "cc52b6ce-d9cc-11e4-b723-00237de09eb7"
    assert "Контрагент" not in результат.masked_fields
    # GUID не попал в словарь: обратного чтения токена для него нет
    # (проверь через метод словаря, которым пользуются соседние тесты для подсчёта записей).


def test_примитивное_значение_составного_поля_токенизируется(...):
    строка = {"Контрагент": 'ООО "Ромашка"', "Контрагент_Type": "Edm.String"}
    результат = маскировщик.mask({"value": [строка]}, entity="Регистр")
    assert результат.data["value"][0]["Контрагент"].startswith("[[org:")


def test_guid_в_верхнем_регистре_тоже_не_токенизируется(...):
    значение = "CC52B6CE-D9CC-11E4-B723-00237DE09EB7"
    результат = маскировщик.mask({"Контрагент": значение}, entity="Регистр")
    assert результат.data["Контрагент"] == значение
```

- [ ] **Шаг 2: убедиться, что тесты падают**

Run: `uv run pytest tests/unit/test_gate_field_rules.py tests/unit/test_gate_masking.py -q`
Expected: FAIL — `('acc', 'auto')` для `НомерСчетаФактуры`, GUID превращён в `[[org:…]]`.

- [ ] **Шаг 3: реализация**

`field_rules.py`, правило `acc`: `номерсчета` → `номерсчета(?!фактур)` с комментарием-ссылкой на
SPEC §6.4 сноску ⁴ и пробу P4.

`masking.py`:

```python
# SPEC §6.4, сноска ⁴; инвариант 6: GUID не защищается ни на одном уровне. Значение составного
# поля (`Контрагент` + `Контрагент_Type`) для ссылки — строка GUID; класс поля по имени (org)
# относится к примитивному значению того же поля, а не к ссылке.
GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
```

В `_обработать_строку` сразу после `if not текст: return текст`:

```python
        if GUID_RE.fullmatch(текст):
            return текст
```

- [ ] **Шаг 4: прогон, мутация, коммит**

Run: `uv run pytest -q` — зелёный. Мутации (по одной, с возвратом): убрать проверку `GUID_RE`
→ падают оба теста GUID; `fullmatch` → `search` → должен упасть тест с примитивным значением,
если в нём есть подстрока формата GUID, — добавь такой случай
(`'ООО "Ромашка" cc52b6ce-d9cc-11e4-b723-00237de09eb7'` токенизируется целиком); вернуть
`номерсчета` без `(?!фактур)` → падает `test_номер_счета_фактуры_не_банковский_счет`.

```bash
uv run ruff format src tests && uv run ruff check . && uv run pytest -q
git add src/odata1c/gate/field_rules.py src/odata1c/gate/masking.py tests/unit/test_gate_field_rules.py tests/unit/test_gate_masking.py
git commit -m "fix: гейт не токенизирует GUID ссылок и номера счетов-фактур"
```

---

### Задача 5: приёмка на живой базе

**Files:**
- Create: `tools/probes/p4_index_check.py`
- Modify: `docs/probes/P4-real-metadata.md` (раздел «Индекс после исправлений»)

**Interfaces:**
- Consumes: задачи 1–4; рабочий `bases.yaml` с базой `trade_dev` (есть на машине владельца).

- [ ] **Шаг 1: скрипт проверки**

`tools/probes/p4_index_check.py` — открывает `~/.claude/odata1c/bases/<база>/metadata.sqlite`
и `policy.yaml` (путь — через `odata1c.config.home.resolve_home` и
`odata1c.index.reindex.index_path`), печатает и проверяет `assert`-ами:

| Проверка | Ожидание (по `$metadata` пробы P4) |
|---|---|
| действий с побочным эффектом (`http_method = 'POST'`) | `Post` 296, `Unpost` 296, `Start` 9, `ExecuteTask` 2 |
| виртуальных таблиц по `virtual_kind` | `SliceLast` 849, `SliceFirst` 849, `Balance` 81, `Turnovers` 121, `BalanceAndTurnovers` 81 (допустимо меньше только на число строк `warnings` реиндекса) |
| табличных частей, у родителя которых нет строки в `entities` | 0 |
| табличных частей с ключом не `Ref_Key`+`LineNumber` | 0 |
| независимых регистров с `_RecordType` в имени или `Recorder`/`Recorder_Key` в ключе основного набора | 0 |
| наборов записей (`is_records`) | 149 (121 регистр накопления + 28 регистров сведений) |
| перечислений (`kind = 'Enum'`) | 1007 |
| полей `auto` в `policy.yaml` с классом `acc`, имя которых содержит `СчетаФактур` | 0 |
| `parser_version` в `meta` | `"2"` |

- [ ] **Шаг 2: реиндекс и проверка**

```bash
PYTHONIOENCODING=utf-8 uv run odata1c reindex trade_dev
PYTHONIOENCODING=utf-8 uv run python tools/probes/p4_index_check.py trade_dev
```

Expected: реиндекс перестраивает индекс без `--force` (сработала версия разбора), скрипт
завершается без `AssertionError`. Если какое-то число расходится — не подгонять ожидание:
разобрать по методике `superpowers:systematic-debugging` и сообщить оркестратору.

- [ ] **Шаг 3: отчёт и коммит**

В `docs/probes/P4-real-metadata.md` добавить раздел «Индекс после исправлений (план M1b-fix)» с
фактическими числами из вывода скрипта и временем реиндекса.

```bash
git add tools/probes/p4_index_check.py docs/probes/P4-real-metadata.md
git commit -m "probe: приёмка индекса на реальной базе после исправлений"
```

---

## Вне плана (записано, чтобы не потерялось)

- Ложные срабатывания класса `person` на поле `Имя` служебных справочников
  (`Catalog_ИдентификаторыОбъектовМетаданных`, `Catalog_КлючевыеОперации` и ещё около десяти на
  реальной УТ). Цена — удобство, не безопасность; правится через `policy.yaml` (`fields: …: keep`).
  Решение о встроенном списке — после первых сессий на реальной базе.
- Связь поля-перечисления с `EnumType` по имени (`ХозяйственнаяОперация` ↔
  `ХозяйственныеОперации`) — задача слоя тулов (M1d, `describe_entity`), индекс хранит оба конца.
- `ref_targets` по `Association` — не заполняются; нужны `describe_entity` в M1d.
- Требования к слою тулов из пробы P4 (M1d): период обязателен для `Balance`/`Turnovers`/
  `BalanceAndTurnovers`; навигационное поле автоматически добавляется в `$select` при `$expand`;
  `odata.count` приводится к числу; первый запрос после простоя — повышенный таймаут;
  принудительный UTF-8 на stdio демона и лаунчера.
