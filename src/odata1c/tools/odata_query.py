"""Построение запроса к 1С из аргументов тула и описания сущности индекса (SPEC §9).

Чистые функции: ничего не знают о сети и о гейте. `build_query`/`build_get` берут
`EntityDescription` (`IndexRepository.describe`) и аргументы тула чтения (`odata1c_query`,
`odata1c_get`) и собирают путь и параметры запроса OData — путь всегда относительно
`standard.odata/`, без `$format` (его добавляет `Client1C`). `orderby_fields` — отдельная чистая
функция для гейта (SPEC §6): он запрещает сортировку по защищаемому полю тем же способом, что и
`$filter`, но сам это делает по именам полей из `$orderby`, а не здесь.

Все ошибки построения — код `params_invalid` (SPEC §5.2, поправка 2026-09-10): неверные
аргументы тула, а не отказ 1С.

Раунд правок 2 (проверено на живой базе 2026-09-10, публикация УТ за IIS 10): значения, которые
попадают в ПУТЬ запроса (аргументы виртуальной таблицы, литералы ключа), не переносят `?`, `+`,
`\\` и управляющие символы (< 0x20, DEL) — даже процентно закодированными. `?` 1С режет как
разделитель внутри уже раскодированного IIS пути («Параметр … не поддерживается»), `+` и
управляющие символы IIS отклоняет своей страницей 404/400 раньше, чем запрос доходит до 1С, а
`\\` тише всех — IIS молча заменяет его на `/`, искажая значение без единой ошибки. `#`, `%`, `&`,
`(`, `)`, `,`, `=`, `/`, `;`, `"`, `<`, `*`, `№`, пробел и кириллица доходят без искажений
(подтверждено текстом ошибки 1С, который их повторяет). `$filter`/`$select`/`$expand`/`$orderby`
идут через `params=` (штатное кодирование query httpx), этого ограничения не касаются —
`build_query`/`build_get` проверяют только то, что сами вставляют в путь, и на исходном значении,
до кодирования.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable

from odata1c.config.models import Limits
from odata1c.index.repository import EntityDescription

# Обязательные параметры виртуальных таблиц (проба P4, docs/probes/P4-real-metadata.md):
# `Balance` — момент; `Turnovers`/`BalanceAndTurnovers` — интервал. У остальных действий
# (`SliceLast`, `SliceFirst`, `Post`, `Unpost`, `Start`, `ExecuteTask`) обязательных нет — без
# периода расчёт на боевой базе идёт по всей истории (проба P4: 24,6 с и дольше).
_ОБЯЗАТЕЛЬНЫЕ_ПАРАМЕТРЫ_ТАБЛИЦЫ: dict[str, frozenset[str]] = {
    "Balance": frozenset({"Period"}),
    "Turnovers": frozenset({"StartPeriod", "EndPeriod"}),
    "BalanceAndTurnovers": frozenset({"StartPeriod", "EndPeriod"}),
}

# Представление цели раскрытия, которое авто-добавляется в $select (решение плана 5): все из
# перечисленных полей, что есть у цели, — не только одно каноническое, потому что у сущности
# может не быть Description (документ), но быть Number и Date, или наоборот.
_ПОЛЯ_ПРЕДСТАВЛЕНИЯ = ("Description", "Code", "Number", "Date")

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ДАТА = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ДАТА_ВРЕМЯ = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
_ЦЕЛЫЕ_ТИПЫ = frozenset({"Edm.Int16", "Edm.Int32", "Edm.Int64"})
_ДРОБНЫЕ_ТИПЫ = frozenset({"Edm.Decimal", "Edm.Double"})


class QueryError(Exception):
    """Аргументы тула построению запроса не годятся — код всегда `params_invalid`."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclasses.dataclass(slots=True)
class QuerySpec:
    path: str
    """Относительно `standard.odata/`, без `$format` — его подставляет `Client1C`. Читаемый
    OData-текст, не экранированный под URL (кириллица, `'`, `,`, `=` — буквально): экранирует
    его `Client1C` перед отправкой (раунд правок 1, задача 2 плана M1d)."""
    params: dict[str, str]
    """`$filter`, `$select`, `$expand`, `$orderby`, `$top`, `$skip` и служебные `$inlinecount`,
    `allowedOnly`."""
    timeout_s: int | None
    """`virtual_timeout_s` для виртуальных таблиц, иначе `None` — таймаут базы по умолчанию."""
    top: int
    """Фактический `$top` после отсечения по `limits.top_max` — для расчёта `has_more`."""
    skip: int
    warnings: list[str]


Describe = Callable[[str], EntityDescription | None]


def _ошибка(message: str, hint: str = "") -> QueryError:
    return QueryError("params_invalid", message, hint)


def _проверить_целое(значение, имя: str) -> None:
    """`top`/`skip` — только `int`, не `bool` (раунд правок 1, Important): `bool` — подкласс
    `int` в Python, `isinstance(True, int)` истинно, и без явной проверки `top=True` тихо ушёл бы
    в 1С как `$top=True` вместо ожидаемого числа; строка вроде `skip="5"` дала бы голый
    `TypeError` при сравнении с 0, а не `params_invalid`. `0` — допустимое значение обоих (проба
    P4: `$top=0` отвечает `200` с пустым `value`), проверка на отрицательность — отдельно."""
    if значение is not None and (isinstance(значение, bool) or not isinstance(значение, int)):
        raise _ошибка(f"{имя} должен быть целым числом, получено {значение!r}")


_ЗАПРЕЩЁННЫЕ_СИМВОЛЫ_ПУТИ = frozenset("?+\\")


def _проверить_литерал_пути(value, аргумент: str) -> None:
    """`?`, `+`, `\\` и управляющие символы (< 0x20, DEL) не переносятся в пути запроса 1С за
    IIS — раунд правок 2, проверено на живой базе 2026-09-10 (см. docstring модуля). Проверка на
    исходном значении, до `odata_literal`: кодирование процентами эти символы не спасает — IIS и
    1С искажают или отклоняют их даже закодированными."""
    for символ in str(value):
        if символ in _ЗАПРЕЩЁННЫЕ_СИМВОЛЫ_ПУТИ or ord(символ) < 0x20 or ord(символ) == 0x7F:
            # Сам символ в сообщение не попадает (Ruling 20, пункт 2): значение здесь уже
            # раскрытое, и знак из него — тоже байт реального значения. Перечень запрещённого
            # закрытый и короткий, назвать его целиком не менее понятно.
            raise _ошибка(
                f"значение параметра {аргумент} содержит символ, который 1С не переносит в пути "
                "запроса (?, +, обратная косая черта или управляющий символ)",
                hint=(
                    "в пути запроса 1С (публикация через IIS) этот символ не передаётся; для "
                    "виртуальной таблицы перенесите условие из Condition в filter результата, "
                    "для ключа — найдите запись через query с filter"
                ),
            )


def _к_списку(значение: list[str] | str | None) -> list[str]:
    """Список или строка с запятыми (разбивается); пустые элементы отбрасываются."""
    if значение is None:
        return []
    сырые = значение.split(",") if isinstance(значение, str) else list(значение)
    return [элемент.strip() for элемент in сырые if элемент and элемент.strip()]


_ФОРМА_GUID = (
    "ожидается GUID вида xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx — как есть или в записи OData "
    "guid'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'; значение Ref_Key берите из ответа query/get"
)


def _без_обёртки(текст: str, префикс: str) -> str:
    """Снять обёртку литерала OData — `guid'…'`, `datetime'…'` (находка П5 приёмки через
    настоящие инструменты, 2026-09-12). Модели пишут ключ в синтаксисе OData именно так — так он
    выглядит в адресе запроса и в документации платформы, — и каждый такой вызов упирался в отказ
    «не похоже на GUID». Литерал шлюз всё равно собирает сам, поэтому принимаются оба написания;
    регистр префикса не важен (`GUID'…'`). Обёртка снимается только целиком — с префиксом и
    закрывающей кавычкой: половинчатая (`guid'…` без кавычки) остаётся как есть и отклоняется
    проверкой формата ниже."""
    начало = f"{префикс}'"
    if len(текст) > len(начало) and текст[: len(начало)].lower() == начало and текст[-1] == "'":
        return текст[len(начало) : -1]
    return текст


def odata_literal(edm_type: str, value, *, name: str = "") -> str:
    """Значение аргумента виртуальной таблицы/ключа как литерал OData (SPEC §9).

    `Edm.Guid` и `Edm.DateTime` проверяются на формат — иначе `params_invalid`. `Edm.String`
    удваивает внутренние одинарные кавычки (SQL-стиль OData). `Edm.Boolean` — `true`/`false`.
    Числовые типы — как есть, после проверки, что значение действительно число.

    Текст отказа НИКОГДА не повторяет само значение (Ruling 20, пункт 2, ревью 2026-09-11):
    сюда значение приходит уже ПОСЛЕ обратной подмены, то есть реальным, а ответ об ошибке
    уходит модели тем же путём, что и данные. Прежнее «„г. Москва, ул. Тверская…“ не похоже на
    дату/время» и было той самой утечкой: маскировщик и страж собирают обратно не все классы —
    у `addr` и `dob` последнего рубежа нет вовсе. Понятность отказа держится на имени параметра
    (`name`) и ожидаемом типе, а этого модели достаточно, чтобы исправить вызов."""
    что = f"значение параметра «{name}»" if name else "значение"
    if edm_type == "Edm.Guid":
        текст = _без_обёртки(str(value), "guid")
        if not _GUID.fullmatch(текст):
            # Подсказка — форма, а не значение (находка П5): до правки она была пустой, и модель,
            # написавшая ключ в синтаксисе OData, не узнавала, что не так.
            raise _ошибка(f"{что} не похоже на GUID (Edm.Guid)", hint=_ФОРМА_GUID)
        return f"guid'{текст}'"
    if edm_type == "Edm.DateTime":
        текст = _без_обёртки(str(value), "datetime")
        if _ДАТА.fullmatch(текст):
            текст = f"{текст}T00:00:00"
        elif not _ДАТА_ВРЕМЯ.fullmatch(текст):
            raise _ошибка(
                f"{что} не похоже на дату/время (Edm.DateTime)",
                hint="формат YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS",
            )
        return f"datetime'{текст}'"
    if edm_type == "Edm.String":
        return "'" + str(value).replace("'", "''") + "'"
    if edm_type == "Edm.Boolean":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower()
        raise _ошибка(f"{что} не похоже на булево значение (Edm.Boolean)")
    if edm_type in _ЦЕЛЫЕ_ТИПЫ:
        try:
            return str(int(value))
        except (TypeError, ValueError) as ошибка:
            raise _ошибка(f"{что} не похоже на целое число ({edm_type})") from ошибка
    if edm_type in _ДРОБНЫЕ_ТИПЫ:
        try:
            return str(float(value))
        except (TypeError, ValueError) as ошибка:
            raise _ошибка(f"{что} не похоже на число ({edm_type})") from ошибка
    raise _ошибка(f"построение запроса не поддерживает тип поля {edm_type}")


def orderby_fields(orderby: str) -> list[str]:
    """Имена полей из `$orderby` без `asc`/`desc` — для проверки гейтом (SPEC §6): сортировка по
    защищаемому полю запрещена тем же порядком, что и в `$filter` (оракул сравнения — порядок
    строк раскрывает исходное значение не хуже самого значения)."""
    поля = []
    for часть in orderby.split(","):
        часть = часть.strip()
        if not часть:
            continue
        слова = часть.split()
        if слова and слова[-1].lower() in ("asc", "desc"):
            слова = слова[:-1]
        поля.append(" ".join(слова).strip())
    return [поле for поле in поля if поле]


def _обработать_expand(
    desc: EntityDescription,
    describe: Describe,
    limits: Limits,
    путь_раскрытия: str,
    накопленный_select: list[str],
    *,
    добавлять_представление: bool,
) -> None:
    сегменты = путь_раскрытия.split("/")
    if len(сегменты) > limits.expand_depth:
        raise _ошибка(
            f"$expand «{путь_раскрытия}» глубже лимита {limits.expand_depth}",
            hint=f"сократите путь раскрытия до {limits.expand_depth} сегментов",
        )
    текущая = desc
    for сегмент in сегменты:
        цель_имя = текущая.navigations.get(сегмент)
        if цель_имя is None:
            доступные = ", ".join(sorted(текущая.navigations)) if текущая.navigations else "нет"
            raise _ошибка(
                f"неизвестная навигация «{сегмент}» у сущности {текущая.name}",
                hint=f"доступные навигации: {доступные}",
            )
        цель = describe(цель_имя)
        if цель is None:
            raise _ошибка(
                f"навигация «{сегмент}» ведёт к неизвестной сущности {цель_имя}",
                hint="обновите индекс: odata1c reindex <база>",
            )
        текущая = цель
    if добавлять_представление:
        накопленный_select.append(f"{путь_раскрытия}/Ref_Key")
        имена_полей_цели = {поле["name"] for поле in текущая.fields}
        for кандидат in _ПОЛЯ_ПРЕДСТАВЛЕНИЯ:
            if кандидат in имена_полей_цели:
                накопленный_select.append(f"{путь_раскрытия}/{кандидат}")


def _путь_виртуальной_таблицы(desc: EntityDescription, params: dict | None) -> str:
    if not desc.actions:
        # Индекс помечает сущность виртуальной таблицей только при найденном FunctionImport
        # (index/edmx.py) — пустой actions здесь означает рассинхронизацию индекса, а не
        # ошибку аргументов, но клиенту тула всё равно нужен код и подсказка, а не голое
        # исключение (глобальное ограничение плана).
        raise _ошибка(
            f"виртуальная таблица {desc.name} без описания действия в индексе",
            hint="обновите индекс: odata1c reindex <база>",
        )
    действие = desc.actions[0]
    схема_параметров: dict[str, str] = действие["params"]
    заданные = dict(params or {})

    неизвестные = set(заданные) - set(схема_параметров)
    if неизвестные:
        допустимые = ", ".join(sorted(схема_параметров)) or "нет"
        raise _ошибка(
            f"неизвестный параметр {desc.virtual_kind}: {', '.join(sorted(неизвестные))}",
            hint=f"допустимые параметры: {допустимые}",
        )

    обязательные = _ОБЯЗАТЕЛЬНЫЕ_ПАРАМЕТРЫ_ТАБЛИЦЫ.get(desc.virtual_kind or "", frozenset())
    недостающие = обязательные - set(заданные)
    if недостающие:
        raise _ошибка(
            f"для {desc.virtual_kind} обязателен параметр {', '.join(sorted(недостающие))}",
            hint=f"обязательные параметры: {', '.join(sorted(обязательные))}",
        )

    for имя, значение in заданные.items():
        _проверить_литерал_пути(значение, имя)

    # «В порядке имён» (бриф) — порядок объявления параметров в $metadata, как их сохраняет
    # парсер (index/edmx.py: словарь params строится по document order Parameter, не
    # пересортировывается); раунд правок 1 (Minor) подтвердил это чтение брифа.
    порядок = [имя for имя in схема_параметров if имя in заданные]
    аргументы = ",".join(
        f"{имя}={odata_literal(схема_параметров[имя], заданные[имя], name=имя)}" for имя in порядок
    )
    return f"{desc.parent_entity}/{desc.virtual_kind}({аргументы})"


def build_query(
    desc: EntityDescription,
    *,
    describe: Describe,
    limits: Limits,
    virtual_timeout_s: int,
    filter: str | None = None,
    select: list[str] | str | None = None,
    expand: list[str] | str | None = None,
    orderby: str | None = None,
    top: int | None = None,
    skip: int | None = None,
    inlinecount: bool = False,
    params: dict | None = None,
    allowed_only: bool = False,
) -> QuerySpec:
    _проверить_целое(top, "top")
    _проверить_целое(skip, "skip")
    if top is not None and top < 0:
        raise _ошибка(f"top не может быть отрицательным: {top}")
    if skip is not None and skip < 0:
        raise _ошибка(f"skip не может быть отрицательным: {skip}")
    if params is not None and not isinstance(params, dict):
        raise _ошибка("params должен быть словарём имя параметра → значение")

    if desc.is_virtual:
        путь = _путь_виртуальной_таблицы(desc, params)
        timeout_s = virtual_timeout_s
    else:
        if params:
            raise _ошибка(
                f"параметры виртуальной таблицы переданы для обычной сущности {desc.name}",
                hint="params применим только к виртуальным таблицам (Balance, Turnovers, …)",
            )
        путь = desc.name
        timeout_s = None

    предупреждения: list[str] = []
    top_факт = top if top is not None else limits.top_default
    if top_факт > limits.top_max:
        предупреждения.append(f"top уменьшен до {limits.top_max}")
        top_факт = limits.top_max
    skip_факт = skip if skip is not None else 0

    query_params: dict[str, str] = {"$top": str(top_факт)}
    if skip_факт:
        query_params["$skip"] = str(skip_факт)
    if filter:
        query_params["$filter"] = filter
    if orderby:
        query_params["$orderby"] = orderby

    select_список = _к_списку(select)
    expand_список = _к_списку(expand)
    добавлять_представление = bool(select_список)
    for путь_раскрытия in expand_список:
        _обработать_expand(
            desc,
            describe,
            limits,
            путь_раскрытия,
            select_список,
            добавлять_представление=добавлять_представление,
        )
    if select_список:
        query_params["$select"] = ",".join(select_список)
    if expand_список:
        query_params["$expand"] = ",".join(expand_список)

    if inlinecount:
        query_params["$inlinecount"] = "allpages"
    if allowed_only:
        query_params["allowedOnly"] = "true"

    return QuerySpec(
        path=путь,
        params=query_params,
        timeout_s=timeout_s,
        top=top_факт,
        skip=skip_факт,
        warnings=предупреждения,
    )


def _литерал_ключа(части: list[tuple[str, str]], key) -> str:
    """Литерал ключа для пути `Сущность(<литерал>)`: простой ключ строкой — голый `guid'…'`,
    словарь — `Поле=литерал` через запятую."""
    if isinstance(key, str):
        return части[0][1]
    return ",".join(f"{имя}={литерал}" for имя, литерал in части)


def _части_ключа(desc: EntityDescription, key) -> list[tuple[str, str]]:
    """Поля ключа и их литералы OData — общая часть обеих форм `get`: пути по ключу и отбора по
    ключу (находка П7). Проверки одни и те же, чтобы две формы не разошлись в том, какой ключ
    считают годным."""
    if isinstance(key, str):
        if desc.key_fields != ["Ref_Key"]:
            raise _ошибка(
                f"сущность {desc.name} требует составной ключ",
                hint=f"передайте ключ словарём с полями: {', '.join(desc.key_fields)}",
            )
        _проверить_литерал_пути(key, "key")
        return [("Ref_Key", odata_literal("Edm.Guid", key, name="key"))]

    if not isinstance(key, dict):
        raise _ошибка(
            f"ключ {desc.name} должен быть строкой GUID или словарём полей",
            hint=f"ожидаются поля: {', '.join(desc.key_fields)}",
        )

    заданные = dict(key)
    недостающие = [имя for имя in desc.key_fields if имя not in заданные]
    лишние = [имя for имя in заданные if имя not in desc.key_fields]
    if недостающие or лишние:
        части = []
        if недостающие:
            части.append(f"недостающие поля: {', '.join(недостающие)}")
        if лишние:
            части.append(f"лишние поля: {', '.join(лишние)}")
        raise _ошибка(
            f"неверный составной ключ {desc.name}: {'; '.join(части)}",
            hint=f"ожидаются поля: {', '.join(desc.key_fields)}",
        )

    for имя in desc.key_fields:
        _проверить_литерал_пути(заданные[имя], имя)

    типы_полей = {поле["name"]: поле["edm_type"] for поле in desc.fields}
    return [
        (имя, odata_literal(типы_полей[имя], заданные[имя], name=имя)) for имя in desc.key_fields
    ]


def build_get(
    desc: EntityDescription,
    key,
    *,
    describe: Describe,
    limits: Limits,
    select: list[str] | str | None = None,
    expand: list[str] | str | None = None,
) -> QuerySpec:
    """Объект по ключу. Две формы запроса (находка П7 приёмки через настоящие инструменты):

    - без `expand` — путь по ключу, `Сущность(guid'…')`, как было;
    - с `expand` у сущности с простым ключом — ВЫБОРКА с отбором `Ref_Key eq guid'…'` и `$top=1`.
      1С на пути по ключу `$expand` не выполняет: «Опция $expand не поддерживается при запросе
      одиночных сущностей» (живая база, справочник и строка табличной части), — а в выборке
      выполняет. Ответ приходит списком из одной записи; слой тулов отдаёт её в форме `get`, а
      пустой список для него — «объект не найден», как 404 на пути по ключу.

    Составной ключ отбором не выражается: 1С отклоняет отбор по полям составного ключа — и у
    строки табличной части (`Ref_Key`, `LineNumber`), и у записи регистра (`Recorder`,
    `LineNumber`, `Recorder_Type`) одинаково, HTTP 500 «Операция не разрешена в предложении
    "ГДЕ"» (живая проба 2026-09-12). Запись регистра `$expand` на пути по ключу при этом получает
    (200), поэтому для неё путь остаётся прежним. Строке табличной части не помогает ни одна
    форма — отказ `params_invalid` до обращения к 1С, с подсказкой, как получить связанный объект.
    """
    select_список = _к_списку(select)
    expand_список = _к_списку(expand)
    отбором = bool(expand_список) and desc.key_fields == ["Ref_Key"]
    if expand_список and not отбором and desc.is_tabular_part:
        raise _ошибка(
            f"у строки табличной части {desc.name} 1С не раскрывает связи ($expand)",
            hint=(
                "запросите строку без expand и возьмите поле <Связь>_Key: связанный объект "
                "читается отдельным get по этому ключу; либо запросите документ-владелец "
                "с expand — его табличная часть придёт в ответе целиком"
            ),
        )
    части = _части_ключа(desc, key)
    путь = desc.name if отбором else f"{desc.name}({_литерал_ключа(части, key)})"
    добавлять_представление = bool(select_список)
    for путь_раскрытия in expand_список:
        _обработать_expand(
            desc,
            describe,
            limits,
            путь_раскрытия,
            select_список,
            добавлять_представление=добавлять_представление,
        )

    query_params: dict[str, str] = {}
    if отбором:
        query_params["$filter"] = " and ".join(f"{имя} eq {литерал}" for имя, литерал in части)
        query_params["$top"] = "1"
    if select_список:
        query_params["$select"] = ",".join(select_список)
    if expand_список:
        query_params["$expand"] = ",".join(expand_список)

    return QuerySpec(path=путь, params=query_params, timeout_s=None, top=1, skip=0, warnings=[])
