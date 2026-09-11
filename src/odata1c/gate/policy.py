"""Политика гейта на базу: какие поля к какому классу, что скрыто, что открыто (SPEC §6.9).

Приоритет источников класса поля: `fields` (ручной раздел) > `custom` (свои классы, по имени
поля) > `defaults` (глобальные умолчания, применяются к уже найденному значению) > `auto`
(автоматика reindex). Признак скрытости сущности (`entities.hide`) — отдельная настройка и в
этом приоритете не участвует (поправка SPEC §6.9, 2026-09-09: исходная формула ошибочно смешивала
обе оси). Секцию `auto` перезаписывает реиндекс, ручные разделы (`fields`, `entities`, `custom`,
`names_for`, `defaults`) не трогаются никогда.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

import yaml

from odata1c.gate.field_rules import СУЩНОСТИ_ФИЗЛИЦ, classify_field

ОТКРЫТЫЕ_ПО_УМОЛЧАНИЮ = ("corr", "bic")


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
        return self._применить_умолчания(значение, entity)

    def _применить_умолчания(self, значение: str, entity: str) -> str:
        if значение in ОТКРЫТЫЕ_ПО_УМОЛЧАНИЮ and self._defaults.get(значение) == "keep":
            return "keep"
        правило_адреса = self._defaults.get("addr")
        if значение == "addr" and isinstance(правило_адреса, dict):
            разрешено = правило_адреса.get("mask_for") or []
            return "addr" if entity in разрешено else "keep"
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


def load_policy(path: pathlib.Path) -> Policy:
    path = pathlib.Path(path)
    if not path.exists():
        return Policy()
    данные = _разобрать_yaml(path)
    _проверить_разделы(данные, path)
    return Policy(
        scan_free_text=bool(данные.get("scan_free_text", True)),
        _defaults=данные.get("defaults") or {},
        _entities=данные.get("entities") or {},
        _fields=данные.get("fields") or {},
        _auto=данные.get("auto") or {},
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
        # сгенерированной политике — четыре имени из `СУЩНОСТИ_ФИЗЛИЦ`, одинаковые у всех баз:
        # дыра в известном эталоне называет скрытое имя так же точно, как само имя. Замена только
        # при пересечении со скрытыми сама сообщала бы, что скрыта одна из четырёх. Тип поля —
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


def _проверить_тип_раздела(данные: dict, ключ: str) -> None:
    значение = данные.get(ключ)
    if значение is not None and not isinstance(значение, dict):
        raise PolicyError(
            f"policy.yaml: раздел {ключ} должен быть словарём, получено {type(значение).__name__}"
        )


def _проверить_разделы(данные: dict, path: pathlib.Path) -> None:
    """Раздел неожиданного типа и недопустимое regex своего класса — ошибка при чтении политики,
    а не при первом обращении к полю посреди обработки ответа тула (SPEC §6.9)."""
    for раздел in ("defaults", "entities", "fields", "auto", "custom"):
        _проверить_тип_раздела(данные, раздел)
    имена = данные.get("names_for")
    if имена is not None and not isinstance(имена, list):
        raise PolicyError(
            f"policy.yaml: раздел names_for должен быть списком, получено {type(имена).__name__}"
        )
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


def merge_auto(existing: dict, generated_auto: dict) -> dict:
    результат = dict(existing)
    результат["auto"] = dict(generated_auto)
    return результат
