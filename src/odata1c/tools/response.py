"""Формирование ответа тулов чтения: ответ OData 1С → конверт SPEC §5.1.

Чистые функции, каждая — один шаг конвейера ответа (порядок неизменен, план M1d):
очистка служебных полей (`strip_service`) → маскировщик (`gate/masking.py`, не здесь) →
усечение строк, не разрезая токен (`truncate_strings`) → подгонка под `result_chars`
выбрасыванием записей целиком (`fit_result`) → сериализация → страж (`gate/guard.py`, не здесь).
`items_of` и `page_info` — разбор списка и страницы из ответа 1С.

Ни одна функция не меняет переданные объекты: результат — новые словари и списки.
"""

from __future__ import annotations

import json

from odata1c.gate.tokens import find_tokens

# Ключи верхнего уровня ответа OData 1С, которые модели не нужны (SPEC §5.1, проба P4).
_СЛУЖЕБНЫЕ_КЛЮЧИ = frozenset({"odata.metadata", "odata.type", "odata.count", "odata.nextLink"})
# Аннотации полей: `Имя@navigationLinkUrl` (проба P4), `Имя@odata.*` (медиа-ссылки и прочее).
_СЛУЖЕБНЫЕ_СУФФИКСЫ = ("@navigationLinkUrl",)
_СЛУЖЕБНЫЕ_ФРАГМЕНТЫ = ("@odata.",)
# Двоичные поля: base64 картинок и файлов, хранилища значений — вырезаются всегда (SPEC §5.1).
# По суффиксу, как `gate/masking.py::ДВОИЧНЫЕ_СУФФИКСЫ` (раунд правок 1): вхождение подстроки
# вырезало бы законный реквизит вроде `ЕстьХранилищеЗначенияКартинки`.
_ДВОИЧНЫЕ_СУФФИКСЫ = ("_Base64Data", "ХранилищеЗначения")

_ПОМЕТКА_УСЕЧЕНИЯ = "…[обрезано: {n} симв.]"
_ПРЕДУПРЕЖДЕНИЕ_ПОДГОНКИ = "результат усечён до {n} записей по лимиту result_chars"
_ПРЕДУПРЕЖДЕНИЕ_ПРОПУСКА = (
    "запись {skip} не помещается в лимит result_chars ({limit} симв.) и пропущена — сузьте select"
)


def strip_service(obj, *, keep_data_version: bool = False):
    """Рекурсивно убирает служебные ключи из ответа 1С.

    Вырезаются `odata.metadata`, `odata.type`, `odata.count`, `odata.nextLink`, все
    `*@navigationLinkUrl` и `*@odata.*`, `DataVersion` (если не `keep_data_version` — модель
    попросила его явным `select`, он нужен для отпечатка записи), двоичные поля по суффиксу
    `_Base64Data` и `ХранилищеЗначения`. Раскрытый `$expand` приходит вложенным словарём под
    именем навигации,
    табличные части — списками словарей (проба P4), поэтому обход рекурсивный по словарям и
    спискам; скаляры возвращаются как есть.
    """
    if isinstance(obj, dict):
        return {
            ключ: strip_service(значение, keep_data_version=keep_data_version)
            for ключ, значение in obj.items()
            if not _служебный_ключ(ключ, keep_data_version=keep_data_version)
        }
    if isinstance(obj, list):
        return [strip_service(элемент, keep_data_version=keep_data_version) for элемент in obj]
    return obj


def _служебный_ключ(ключ: str, *, keep_data_version: bool) -> bool:
    if ключ in _СЛУЖЕБНЫЕ_КЛЮЧИ:
        return True
    if ключ == "DataVersion":
        return not keep_data_version
    if ключ.endswith(_СЛУЖЕБНЫЕ_СУФФИКСЫ) or ключ.endswith(_ДВОИЧНЫЕ_СУФФИКСЫ):
        return True
    return any(фрагмент in ключ for фрагмент in _СЛУЖЕБНЫЕ_ФРАГМЕНТЫ)


def items_of(raw: dict) -> tuple[list, int | None]:
    """Список записей и общее число из ответа 1С.

    Список берётся из `value`; ответ на запрос одного объекта (`get`) списка не содержит и
    заворачивается в список из одного элемента. `odata.count` при `$inlinecount=allpages` 1С
    отдаёт строкой (`"1103"`, проба P4) — приводится к числу; без него `total` неизвестен.

    Счётчик не из одних цифр (пустая строка, текст) читается как НЕИЗВЕСТНЫЙ, а не роняет разбор
    (минор ревью задачи 3): `int("")` поднимал `ValueError` посреди обработки ответа, и тул отвечал
    `internal` из-за служебного поля, которого модель всё равно не видит (оно вырезается,
    `_СЛУЖЕБНЫЕ_КЛЮЧИ`). `None` здесь — та же «сколько всего неизвестно», что и при ответе без
    `$inlinecount`. Гейт с ранним проходом такую пару до сюда не доносит (`BaseGate
    ._вынуть_счётчик` берёт только цифры), но `items_of` зовут и мимо него — слой записи и
    `raw_get` по произвольному пути.
    """
    записи = raw["value"] if isinstance(raw.get("value"), list) else [raw]
    всего = raw.get("odata.count")
    if isinstance(всего, str):
        всего = всего.strip()
        # `isdigit` шире `int`: «²» — цифра, но не число; арабско-индийские цифры `int` прочёл бы.
        всего = int(всего) if всего.isascii() and всего.isdigit() else None
    return записи, всего


def truncate_strings(obj, limit: int) -> tuple[object, int]:
    """Усекает строки длиннее `limit` символов, не разрезая токен `[[type:tail]]`.

    Если граница усечения попадает внутрь токена, срез переносится на начало этого токена:
    обрезок токена модель могла бы достроить, а страж — счесть утечкой (SPEC §6.7). Строка,
    целиком укладывающаяся в лимит, не трогается, даже если содержит токены. К обрезанной
    строке добавляется пометка `…[обрезано: N симв.]`, где N — число отрезанных символов
    исходной строки. Второй результат — число обрезанных строк. Ключи словарей не усекаются:
    это имена полей, а не данные.
    """
    счётчик = [0]
    результат = _усечь(obj, limit, счётчик)
    return результат, счётчик[0]


def _усечь(obj, limit: int, счётчик: list[int]):
    if isinstance(obj, str):
        if len(obj) <= limit:
            return obj
        счётчик[0] += 1
        граница = _граница_вне_токена(obj, limit)
        return obj[:граница] + _ПОМЕТКА_УСЕЧЕНИЯ.format(n=len(obj) - граница)
    if isinstance(obj, dict):
        return {ключ: _усечь(значение, limit, счётчик) for ключ, значение in obj.items()}
    if isinstance(obj, list):
        return [_усечь(элемент, limit, счётчик) for элемент in obj]
    return obj


def _граница_вне_токена(text: str, limit: int) -> int:
    """Позиция среза: `limit`, а если она внутри токена — начало этого токена."""
    for начало, конец, _, _ in find_tokens(text):
        if начало < limit < конец:
            return начало
        if начало >= limit:
            break
    return limit


def page_info(*, count: int, total: int | None, top: int, skip: int) -> dict:
    """Поля страницы конверта SPEC §5.1: `count`, `total`, `has_more`, `next_skip`.

    При известном `total` (запрос с `$inlinecount`) продолжение есть, пока `skip + count < total`;
    без него — эвристика «страница полная» (`count == top`): последняя ровно полная страница даст
    ложное `has_more`, следующий запрос вернёт пустой список — это дешевле лишнего `$inlinecount`.
    """
    есть_ещё = skip + count < total if total is not None else count == top
    return {
        "count": count,
        "total": total,
        "has_more": есть_ещё,
        "next_skip": skip + count if есть_ещё else None,
    }


def fit_result(envelope: dict, limit_chars: int, *, skip: int | None = None) -> dict:
    """Подгоняет конверт под `result_chars`, выбрасывая записи `items` с конца целиком.

    Длина считается по `json.dumps(..., ensure_ascii=False)` — так же сериализуется ответ тула
    (кириллица в `\\uXXXX` заняла бы вшестеро больше и обманула бы подсчёт). Записи выбрасываются
    целиком, а не режутся: половина записи модели бесполезна. При выбрасывании `has_more=True`,
    `count` и `next_skip` пересчитываются от оставшихся записей, в `warnings` добавляется
    предупреждение.

    Смещение страницы — аргумент `skip`, который вызывающий (сервис тулов) передаёт явно:
    в конверте SPEC §5.1 поля `skip` нет. Без аргумента смещение выводится как
    `next_skip - count` конверта, а без `next_skip` (страница была последней) — 0, что для
    страницы с ненулевым `$skip` даст неверный `next_skip`; поэтому явная передача обязательна.

    Если в лимит не помещается даже первая запись (раунд правок 1), она пропускается:
    `items=[]`, `count=0`, `has_more=True`, `next_skip = skip + 1` и предупреждение с советом
    сузить `select`. Иначе `next_skip` остался бы равен `skip`, и клиент, листающий по
    `next_skip`, зацикливался бы на той же записи.

    Конверт без `items` (ответ `get`) возвращается как есть: там выбрасывать нечего.
    Исходный конверт не меняется.
    """
    результат = {ключ: list(v) if isinstance(v, list) else v for ключ, v in envelope.items()}
    записи = envelope.get("items")
    if not isinstance(записи, list) or _длина(результат) <= limit_chars:
        return результат

    смещение = _смещение_страницы(envelope, skip)

    def собрать(сколько: int) -> dict:
        конверт = dict(результат)
        конверт["items"] = записи[:сколько]
        конверт["count"] = сколько
        конверт["has_more"] = True
        конверт["next_skip"] = смещение + сколько
        конверт["warnings"] = [
            *envelope.get("warnings", []),
            _ПРЕДУПРЕЖДЕНИЕ_ПОДГОНКИ.format(n=сколько),
        ]
        return конверт

    # Длина монотонно растёт с числом записей — двоичный поиск наибольшего подходящего числа
    # вместо выбрасывания по одной (при сотнях записей и лимите в 120 000 символов линейный
    # проход — сотни сериализаций по 100 КБ).
    низ, верх = 0, len(записи) - 1
    while низ < верх:
        середина = (низ + верх + 1) // 2
        if _длина(собрать(середина)) <= limit_chars:
            низ = середина
        else:
            верх = середина - 1
    if низ > 0:
        return собрать(низ)

    # Не помещается даже одна запись — пропустить её, продвинув next_skip за неё.
    конверт = собрать(0)
    конверт["next_skip"] = смещение + 1
    конверт["warnings"] = [
        *envelope.get("warnings", []),
        _ПРЕДУПРЕЖДЕНИЕ_ПРОПУСКА.format(skip=смещение, limit=limit_chars),
    ]
    return конверт


def _смещение_страницы(envelope: dict, skip: int | None) -> int:
    if skip is not None:
        return skip
    следующее, число = envelope.get("next_skip"), envelope.get("count")
    if isinstance(следующее, int) and isinstance(число, int):
        return следующее - число
    return 0


def _длина(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False))
