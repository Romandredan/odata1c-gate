"""Политика гейта на базу: какие поля к какому классу, что скрыто, что открыто (SPEC §6.9).

Приоритет источников класса поля: `fields` (ручной раздел) > `custom` (свои классы, по имени
поля) > `defaults` (глобальные умолчания, применяются к уже найденному значению) > `auto`
(автоматика reindex). Признак скрытости сущности (`entities.hide`) — отдельная настройка и в
этом приоритете не участвует (поправка SPEC §6.9, 2026-09-09: исходная формула ошибочно смешивала
обе оси).

Поправка 2026-09-14 (ADR-0015): политика живёт в двух файлах. `policy.yaml` — только владелец,
машина его не переписывает; всё, что вычислил реиндекс (раздел `auto` и умолчания авторазметки),
лежит в `policy.auto.yaml` рядом (`odata1c.gate.service.refresh_policy`). `load_policy` собирает
действующую политику в памяти: правила владельца поверх авторазметки. Ручные разделы (`fields`,
`entities`, `custom`, `names_for`, `defaults` владельца) не трогаются никогда.
"""

from __future__ import annotations

import copy
import dataclasses
import os
import pathlib
import re

import yaml

from odata1c.gate.field_rules import СУЩНОСТИ_ФИЗЛИЦ, classify_field

ОТКРЫТЫЕ_ПО_УМОЛЧАНИЮ = ("corr", "bic")

# Ruling 34, пункт 3 (решение владельца, 2026-09-12): адрес доставки маскируется ВСЕГДА, в любой
# сущности, без исключения по контрагенту, — по самому адресу склад организации от квартиры
# покупателя не отличить. Узнаётся по имени поля класса `addr`: `АдресДоставки`,
# `АдресДоставкиЗначение`, `АдресДоставкиПеревозчика…`, `АдресДоставкиДляПоставщика`,
# `АдресПоставки`, `АдресПогрузки`, `АдресРазгрузки`/`Выгрузки` — адреса движения товара к
# получателю и от отправителя. Чтение намеренно по имени, а не «любой адрес в документе»: у
# документов есть и поля класса `addr`, которые адресом лица не являются (`АдресРасчетов` кассы,
# `АдресПлощадки` маркировки, адреса серверов) — их правило не трогает (см. отчёт задачи).
АДРЕС_ДВИЖЕНИЯ_ТОВАРА = re.compile(r"доставк|поставк|погрузк|разгрузк|выгрузк", re.IGNORECASE)
# Табличная часть маршрута документа перевозки (`Document_ЗаданиеНаПеревозку_Маршрут.Адрес`,
# маршруты ВЕТИС): точки маршрута — те же адреса доставки, только под общим именем поля `Адрес`.
МАРШРУТ_ДОКУМЕНТА = re.compile(r"^Document_.+_Маршрут")


class PolicyError(Exception):
    """Ошибка политики базы: неверная разметка `policy.yaml`, раздел неожиданного типа,
    недопустимое регулярное выражение своего класса. Тот же протокол атрибутов (code, hint), что
    у `odata1c.config.loader.ConfigError` и `odata1c.index.repository.IndexCorruptError` — CLI и
    демон различают причину отказа одинаково."""

    def __init__(self, message: str, code: str = "policy_invalid", hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


@dataclasses.dataclass(slots=True)
class Policy:
    scan_free_text: bool = True
    _defaults: dict = dataclasses.field(default_factory=dict)
    _entities: dict = dataclasses.field(default_factory=dict)
    _fields: dict = dataclasses.field(default_factory=dict)
    _auto: dict = dataclasses.field(default_factory=dict)
    _custom: dict = dataclasses.field(default_factory=dict)
    _names_for: list | None = None

    def sensitivity_of(self, entity: str, field: str) -> str | None:
        ключ = f"{entity}.{field}"
        if ключ in self._fields:
            значение = self._fields[ключ]
        else:
            значение = self.custom_fields().get(field)
            if значение is None:
                значение = self._auto.get(ключ)
        if значение is None:
            return None
        return self._применить_умолчания(значение, entity, field)

    def manual_sensitivity_of(self, entity: str, field: str) -> str | None:
        """Класс поля из РУЧНЫХ разделов политики — `fields` и `custom`, с умолчаниями; раздел
        `auto` не смотрится. Нужен правилам, которые решают класс поля сами, по строению ответа
        (контактная информация, Ruling 33): такое правило главнее автоматики реиндекса, но не
        главнее того, что владелец написал рукой (итоговое ревью M1d, C1: объявленный владельцем
        `keep` не отменяется автоматическим правилом)."""
        ключ = f"{entity}.{field}"
        значение = self._fields.get(ключ)
        if значение is None:
            значение = self.custom_fields().get(field)
        if значение is None:
            return None
        return self._применить_умолчания(значение, entity, field)

    def addr_masked(self, entity: str, field: str = "") -> bool:
        """Закрыт ли адрес в этом поле этой сущности (SPEC §6.9, Ruling 34). Единственное место,
        где правило адресов читается: им пользуются и класс поля (`_применить_умолчания`), и
        правило контактной информации (строка с `Тип` = «Адрес», Ruling 33).

        1. Адрес движения товара — доставки, поставки, погрузки, точки маршрута перевозки —
           закрыт всегда (Ruling 34, пункт 3; см. `АДРЕС_ДВИЖЕНИЯ_ТОВАРА`).
        2. Без правила `defaults.addr` — закрыт.
        3. С правилом — закрыт у сущностей из `mask_for` И У ИХ ДОЧЕРНИХ ОБЪЕКТОВ (Ruling 34,
           пункт 1, тем же способом, каким Ruling 30 распространил `hide`): адрес физлица лежит в
           табличной части `Catalog_ФизическиеЛица_КонтактнаяИнформация`, а правило, выписанное
           на справочник, до неё прежде не доходило — гейт не выполнял собственную политику.

        Родство — по имени: дочерний объект 1С OData называется `<родитель>_<имя>`. Сверено с
        индексом живой УТ (3393 сущности с родителем): у справочников и документов имя ребёнка
        начинается с имени родителя без исключений; 56 расхождений — срезы регистров сведений,
        чей родитель в индексе — набор записей `…_RecordType`, а имя всё равно начинается с имени
        регистра. Чтение по имени работает и без индекса (политика его не видит, а класс поля
        нужен и обратной подмене), а ошибается только в сторону закрытия: чужой справочник,
        чьё имя начинается с `Catalog_ФизическиеЛица_`, получил бы закрытый адрес."""
        if АДРЕС_ДВИЖЕНИЯ_ТОВАРА.search(field) or МАРШРУТ_ДОКУМЕНТА.match(entity):
            return True
        правило_адреса = self._defaults.get("addr")
        if not isinstance(правило_адреса, dict):
            return True
        return any(
            entity == имя or entity.startswith(f"{имя}_")
            for имя in правило_адреса.get("mask_for") or []
            if isinstance(имя, str) and имя
        )

    def _применить_умолчания(self, значение: str, entity: str, field: str = "") -> str:
        if значение in ОТКРЫТЫЕ_ПО_УМОЛЧАНИЮ and self._defaults.get(значение) == "keep":
            return "keep"
        if значение == "addr":
            return "addr" if self.addr_masked(entity, field) else "keep"
        return значение

    def is_hidden(self, entity: str) -> bool:
        настройки = self._entities.get(entity) or {}
        return bool(настройки.get("hide"))

    def hidden_entities(self) -> set[str]:
        """Имена, на которые владелец выписал `hide: true`, — без наследования.

        Наследование (Ruling 30: скрытие распространяется на дочерние объекты) политике не
        подчинено и подчинено быть не может: родство сущностей знает индекс метаданных, а не
        `policy.yaml`. Поэтому политика отдаёт только корни запрета, а поддерево достраивает
        `ToolService._скрытые` по индексу."""
        return {имя for имя, настройки in self._entities.items() if (настройки or {}).get("hide")}

    def has_hidden(self) -> bool:
        """Есть ли у базы хоть одно правило `hide` (Ruling 21, дополнение к раунду правок 1
        задачи 8). `raw_get` отправляет путь ДОСЛОВНО, а канонизацию имени видит только 1С за
        IIS: имя с завершающей точкой, невидимым пробелом или комбинирующим ударением до 1С
        доходит канонизированным, а шлюз своего запрета на нём не узнаёт. Гоняться за
        нормализациями бессмысленно — их всегда окажется на одну больше, — поэтому неизвестное
        индексу имя на базе, где владелец что-то закрыл, просто не обслуживается. Там, где
        скрывать нечего, неизвестное имя остаётся штатным сценарием `raw_get` и работает
        по-прежнему, со строгой политикой маскировки (Ruling 18)."""
        return any(bool((настройки or {}).get("hide")) for настройки in self._entities.values())

    def names_for(self) -> set[str] | None:
        return set(self._names_for) if self._names_for is not None else None

    def custom_patterns(self) -> dict[str, re.Pattern]:
        собранное: dict[str, re.Pattern] = {}
        for имя, описание in self._custom.items():
            выражение = (описание or {}).get("regex")
            if выражение:
                собранное[f"custom:{имя}"] = re.compile(выражение)
        return собранное

    def custom_fields(self) -> dict[str, str]:
        """Имя поля → класс custom:*, из раздела custom политики (SPEC §6.4, строка `custom:*`:
        свой класс задаётся и по имени поля, и по выражению для значения — здесь первая
        половина, используется в sensitivity_of наравне с ручным разделом fields)."""
        собранное: dict[str, str] = {}
        for имя, описание in self._custom.items():
            for поле in (описание or {}).get("fields", []):
                собранное[поле] = f"custom:{имя}"
        return собранное

    def auto_items(self) -> dict[str, str]:
        """Копия раздела `auto` действующей политики (`Сущность.Поле` → класс) — задача 4
        (`policy show`/`policy check`, ADR-0015): `effective_rows` строит объединённый вид
        (владелец поверх авторазметки) и не должен получить доступ к приватному `_auto`
        напрямую. Копия — вызывающий может фильтровать и сортировать результат, не трогая
        саму политику."""
        return dict(self._auto)


def load_policy(path: pathlib.Path, auto_path: pathlib.Path | None = None) -> Policy:
    """Собрать действующую политику базы (ADR-0015): правила владельца (`path`, `policy.yaml`)
    поверх авторазметки (`auto_path`, `policy.auto.yaml`, см. `read_auto`).

    Без `auto_path` — прежнее поведение (один файл, раздел `auto` в нём же): нужен и тестам
    старого формата, и `owner_names_for` — вызывающему, которому авторазметка не нужна вовсе.

    Раздел `auto` в файле владельца ещё возможен до первого реиндекса новой версии
    (`service.refresh_policy` уносит его в `policy.auto.yaml` только на первом вызове) — он
    участвует в слиянии, но уступает файлу авторазметки. Умолчания собираются `merge_defaults`:
    владелец поверх авторазметки, `defaults.addr.mask_for` — объединением списков."""
    path = pathlib.Path(path)
    данные = parse_owner_file(path)

    if auto_path is not None:
        авто = read_auto(auto_path)
        _auto = {**(данные.get("auto") or {}), **(авто.get("auto") or {})}
        _defaults = merge_defaults(авто.get("defaults") or {}, данные.get("defaults") or {})
    else:
        _auto = данные.get("auto") or {}
        _defaults = данные.get("defaults") or {}

    return Policy(
        scan_free_text=bool(данные.get("scan_free_text", True)),
        _defaults=_defaults,
        _entities=данные.get("entities") or {},
        _fields=данные.get("fields") or {},
        _auto=_auto,
        _custom=данные.get("custom") or {},
        _names_for=данные.get("names_for"),
    )


ПРИМЕЧАНИЕ_О_СКРЫТЫХ = (
    "# Часть сущностей скрыта настройкой базы: ни их имена, ни их правила здесь не показаны."
)

# Чем заменяется `defaults.addr.mask_for` в ресурсе политики базы со скрытыми сущностями (см.
# `redact_policy`). Сама защита адресов не меняется: гейт читает список из файла, а не из ресурса.
ПОМЕТКА_СПИСОК_НЕ_ПОКАЗАН = "<перечень не показан: у базы есть скрытые сущности>"


def redact_policy(text: str, hidden: set[str]) -> str:
    """Текст политики без единого упоминания скрытых сущностей (Ruling 29, итоговое ревью M1d,
    раунд 3).

    Ресурс `odata1c://policy/{base}` печатал файл дословно — вместе с секцией `entities`, где
    стоит `hide: true`, и со всеми правилами закрытой сущности. Это противоречит SPEC §6.9
    (Ruling 28): имя скрытой сущности не появляется ни в одном ответе, а здесь выдавалось и имя,
    и сам факт сокрытия.

    Вычёркивается КАЖДАЯ строка, ключом которой стоит скрытая сущность, а не одна секция
    `entities`: имя лежит и в `fields`/`auto` (ключ `Сущность.Поле`), и в списках `names_for` и
    `defaults.addr.mask_for`. Отсечь только `entities` значило бы убрать имя из одной строки и
    напечатать тремя ниже. `mask_for` при этом не вычёркивается по одному, а заменяется пометкой
    целиком (`ПОМЕТКА_СПИСОК_НЕ_ПОКАЗАН`, причина — у места замены). Раздел `custom` не
    трогается: там имена КЛАССОВ и имена полей, имён сущностей нет.

    Прочие настройки остаются: они объясняют модели поведение гейта («почему это поле пришло
    токеном») и имён не выдают.

    Пересборка YAML, а не правка строк: правило `hide` владелец мог сопроводить комментарием с
    тем же именем, а построчная замена комментарий бы не увидела. Цена — комментарии в ответе
    теряются; поэтому вызывать `redact_policy` стоит только там, где есть что вычёркивать
    (`Policy.has_hidden`), а не на каждой выдаче ресурса.
    """
    данные = yaml.safe_load(text) or {}

    def под_запретом(имя) -> bool:
        return isinstance(имя, str) and имя.split(".", 1)[0] in hidden

    for раздел in ("entities", "fields", "auto"):
        значение = данные.get(раздел)
        if isinstance(значение, dict):
            данные[раздел] = {
                ключ: правило for ключ, правило in значение.items() if not под_запретом(ключ)
            }
    имена = данные.get("names_for")
    if isinstance(имена, list):
        данные["names_for"] = [имя for имя in имена if not под_запретом(имя)]
    адрес = (данные.get("defaults") or {}).get("addr")
    if hidden and isinstance(адрес, dict) and isinstance(адрес.get("mask_for"), list):
        # Не вычёркивание по одному, а замена целиком — и ВСЕГДА, когда у базы есть скрытые
        # (раунд правок 2 по `stop()` и журналу, пункт 4; поправка ревьюера раунда 5). Список в
        # сгенерированной политике — имена из `СУЩНОСТИ_ФИЗЛИЦ`, одинаковые у всех баз:
        # дыра в известном эталоне называет скрытое имя так же точно, как само имя. Замена только
        # при пересечении со скрытыми сама сообщала бы, что скрыто одно из них. Тип поля —
        # список — сохранён: разбирающий ресурс не должен споткнуться о смену формы.
        адрес["mask_for"] = [ПОМЕТКА_СПИСОК_НЕ_ПОКАЗАН]

    свод = yaml.safe_dump(данные, allow_unicode=True, sort_keys=False)
    return f"{ПРИМЕЧАНИЕ_О_СКРЫТЫХ}\n{свод}"


def _разобрать_yaml(path: pathlib.Path) -> dict:
    """Прочитать YAML политики; синтаксическая ошибка — PolicyError с местом (строка, колонка),
    без фрагмента файла в сообщении (по образцу odata1c.config.loader._разобрать_yaml — то же
    соображение: в policy.yaml секреты не хранятся, но принцип «не цитировать файл целиком»
    единый для всех загрузчиков настроек)."""
    try:
        данные = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            место = f"строка {mark.line + 1}, колонка {mark.column + 1}"
        else:
            место = "точное место в файле не определено"
        raise PolicyError(
            f"файл {path.name} повреждён и не разбирается как YAML: {место}",
            hint=f"проверьте синтаксис файла {path}",
        ) from exc
    return данные or {}


def _проверить_тип_раздела(данные: dict, ключ: str, path: pathlib.Path) -> None:
    значение = данные.get(ключ)
    if значение is not None and not isinstance(значение, dict):
        raise PolicyError(
            f"{path.name}: раздел {ключ} должен быть словарём, получено {type(значение).__name__}"
        )


def _проверить_ключи_строками(данные: dict, раздел: str, path: pathlib.Path) -> None:
    """Ключи `entities`/`fields`/`auto`/`custom` — это `Сущность.Поле`, имя сущности или имя
    класса, а не произвольное значение; YAML без кавычек охотно разберёт `123: keep` как ключ-
    число или `true: {...}` как ключ-булево. Без этой проверки такой ключ доходил бы необработанным
    до `check_policy`/`effective_rows` (`.partition` на не-строке — `AttributeError`, смешанная
    сортировка строк и чисел — `TypeError`) и до `redact_policy`, которая эту форму уже переживала
    через `isinstance` (находка 2 ревью задачи 4, Important: то же самое, только раньше, здесь
    честной ошибкой политики, а не голым исключением посреди ответа тула).

    Сообщение называет только тип и порядковый номер записи в разделе, БЕЗ самого значения
    ключа (находка 4 повторного ревью задачи 4, Important): файл владельца не цитируется ни
    при разборе YAML (`_разобрать_yaml`), ни соседней `_проверить_тип_раздела` — печатать
    буквальное значение здесь было бы отступлением от того же принципа."""
    значение = данные.get(раздел)
    if not isinstance(значение, dict):
        return
    for позиция, ключ in enumerate(значение, start=1):
        if not isinstance(ключ, str):
            raise PolicyError(
                f"{path.name}: раздел {раздел}: ключ {позиция}-й записи должен быть строкой, "
                f"получен {type(ключ).__name__}",
                hint=f"проверьте раздел {раздел} в {path}",
            )


def _проверить_список_строк(значение, где: str, path: pathlib.Path) -> None:
    """`names_for`/`defaults.addr.mask_for` — список имён сущностей: сам контейнер обязан быть
    списком (находка 3 повторного ревью задачи 4, Important: `mask_for: 5` итерировался бы как
    число до этой проверки — непойманный `TypeError` вместо `PolicyError`, тот же класс дефекта,
    что и находка 2 первого ревью), и каждый элемент обязан быть строкой (находка 2 первого
    ревью: нестроковый элемент иначе доходил бы до `resolve_name`/`startswith` необработанным).
    Сообщение — тип и позиция, без значения (находка 4, тот же принцип, что и у
    `_проверить_ключи_строками`)."""
    if значение is None:
        return
    if not isinstance(значение, list):
        raise PolicyError(
            f"{path.name}: раздел {где} должен быть списком, получен {type(значение).__name__}"
        )
    for позиция, элемент in enumerate(значение, start=1):
        if not isinstance(элемент, str):
            raise PolicyError(
                f"{path.name}: раздел {где}: элемент {позиция}-й записи должен быть строкой, "
                f"получен {type(элемент).__name__}",
                hint=f"проверьте раздел {где} в {path}",
            )


def _проверить_разделы(данные: dict, path: pathlib.Path) -> None:
    """Раздел неожиданного типа, нестроковый ключ/элемент и недопустимое regex своего класса —
    ошибка при чтении политики, а не при первом обращении к полю посреди обработки ответа тула
    (SPEC §6.9)."""
    for раздел in ("defaults", "entities", "fields", "auto", "custom"):
        _проверить_тип_раздела(данные, раздел, path)
    for раздел in ("entities", "fields", "auto", "custom"):
        _проверить_ключи_строками(данные, раздел, path)
    # Контейнер и элементы — обе проверки внутри `_проверить_список_строк` (находка 3 повторного
    # ревью задачи 4, Important: раньше отдельная проверка типа стояла только перед `names_for`,
    # а `defaults.addr.mask_for` доходил до цикла без неё — `mask_for: 5` итерировался бы как
    # число, `TypeError` вместо `PolicyError`).
    _проверить_список_строк(данные.get("names_for"), "names_for", path)
    адрес = (данные.get("defaults") or {}).get("addr")
    if isinstance(адрес, dict):
        _проверить_список_строк(адрес.get("mask_for"), "defaults.addr.mask_for", path)
    for имя, описание in (данные.get("custom") or {}).items():
        if not isinstance(описание, dict):
            raise PolicyError(
                f"policy.yaml: свой класс custom:{имя} должен быть набором полей "
                f"(fields/regex), получено {type(описание).__name__}"
            )
        выражение = описание.get("regex")
        if выражение:
            try:
                re.compile(выражение)
            except re.error as exc:
                raise PolicyError(
                    f"policy.yaml: свой класс custom:{имя} — недопустимое регулярное "
                    f"выражение: {exc}",
                    hint=f"проверьте regex своего класса custom:{имя} в {path}",
                ) from exc


def parse_owner_file(path: pathlib.Path) -> dict:
    """Прочитать и провалидировать файл владельца (`policy.yaml`) — публичная обёртка над
    `_разобрать_yaml` + `_проверить_разделы` (задача 4, ADR-0015: `policy_check.py` и
    `load_policy` разбирают один и тот же файл одним и тем же способом — раздвоения правил
    валидации нет). Нет файла — пустой словарь, как и раньше в `load_policy`; синтаксическая
    ошибка или раздел неожиданного типа — `PolicyError` наружу."""
    path = pathlib.Path(path)
    данные = _разобрать_yaml(path) if path.exists() else {}
    _проверить_разделы(данные, path)
    return данные


def generate_policy(index, *, names_for: set[str] | None = None) -> dict:
    """Собрать секцию auto по индексу: классификация каждого строкового поля (SPEC §4.3 п. 3)."""
    авто: dict[str, str] = {}
    for имя_сущности in sorted(index.entity_names()):
        описание = index.describe(имя_сущности)
        if описание is None:
            continue
        for поле in описание.fields:
            решение = classify_field(
                имя_сущности, поле["name"], поле["edm_type"], names_for=names_for
            )
            if решение:
                авто[f"{имя_сущности}.{поле['name']}"] = решение[0]
    return {
        "version": 2,
        "scan_free_text": True,
        "defaults": {
            "corr": "keep",
            "bic": "keep",
            # Адрес — персональные данные только у сущностей физлиц (SPEC §6.9): склад, магазин,
            # банк защиты не требуют. Список сущностей — та же константа, что определяет класс
            # person слоя 1 (SPEC §6.5), не дублируется здесь отдельно.
            "addr": {"mask_for": sorted(СУЩНОСТИ_ФИЗЛИЦ)},
        },
        "entities": {},
        "fields": {},
        "custom": {},
        "auto": авто,
    }


АВТОРАЗМЕТКА_ШАПКА = (
    "# odata1c: авторазметка гейта — классы полей, вычисленные реиндексом по индексу (SPEC §4.3).\n"
    "# НЕ РЕДАКТИРОВАТЬ: файл пересобирается каждым реиндексом.\n"
    "# Правила владельца — в policy.yaml рядом.\n"
)


def read_auto(path: pathlib.Path) -> dict:
    """Прочитать `policy.auto.yaml` (или, до первого реиндекса новой версии, раздел `auto` файла
    владельца — формат обоих файлов одинаков): разделы `defaults` и `auto`. Нет файла — демон ещё
    не индексировал базу, это не ошибка, а пустой словарь `{}` (а не словарь с пустыми `defaults`
    и `auto` — вызывающий отличает «файла нет совсем» от «файл есть, но пуст»)."""
    if not path.exists():
        return {}
    данные = _разобрать_yaml(path)
    _проверить_тип_раздела(данные, "auto", path)
    _проверить_тип_раздела(данные, "defaults", path)
    return {"defaults": данные.get("defaults") or {}, "auto": данные.get("auto") or {}}


def dump_auto(path: pathlib.Path, *, defaults: dict, auto: dict) -> None:
    """Переписать `policy.auto.yaml` целиком: атомарно (временный файл рядом, `os.replace`), с
    предупреждающей шапкой (`АВТОРАЗМЕТКА_ШАПКА`). Комментарии владельца здесь беречь не от чего —
    файл целиком принадлежит машине (ADR-0015); обратимый разбор (`ruamel.yaml`) нужен только
    `strip_auto_section` — там, где правки владельца рядом с разделом `auto` теряться не должны."""
    тело = yaml.safe_dump(
        {"version": 2, "defaults": defaults, "auto": auto}, allow_unicode=True, sort_keys=True
    )
    временный = path.with_suffix(".yaml.new")
    временный.write_text(АВТОРАЗМЕТКА_ШАПКА + тело, encoding="utf-8")
    os.replace(временный, path)


def merge_defaults(авто: dict, владелец: dict) -> dict:
    """Умолчания классов: владелец поверх авторазметки; `addr.mask_for` — объединение списков
    (авторазметка знает справочники людей по индексу, Ruling 36; владелец мог добавить своих —
    ни один список не должен вытеснить другой)."""
    итог = copy.deepcopy(авто)
    for ключ, значение in владелец.items():
        if ключ == "addr" and isinstance(значение, dict):
            адрес = итог.setdefault("addr", {})
            список = list(
                dict.fromkeys([*(адрес.get("mask_for") or []), *(значение.get("mask_for") or [])])
            )
            адрес.update({к: в for к, в in значение.items() if к != "mask_for"})
            адрес["mask_for"] = список
        else:
            итог[ключ] = значение
    return итог


def strip_auto_section(path: pathlib.Path) -> bool:
    """Унести раздел `auto` из файла владельца (первый реиндекс новой версии, ADR-0015, SPEC
    §4.3): обратимый разбор `ruamel.yaml` сохраняет комментарии и порядок остальных разделов —
    обычный `yaml.safe_dump` их бы стёр. `True` — раздел был и удалён; `False` — файла владельца
    эта версия уже не касается (обычный случай на каждом следующем реиндексе)."""
    from ruamel.yaml import YAML

    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    with path.open(encoding="utf-8") as f:
        данные = yaml_rt.load(f)
    if not isinstance(данные, dict) or "auto" not in данные:
        return False
    del данные["auto"]
    временный = path.with_suffix(".yaml.new")
    with временный.open("w", encoding="utf-8") as f:
        yaml_rt.dump(данные, f)
    os.replace(временный, path)
    return True
