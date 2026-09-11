"""Сервис тулов чтения: bases, find_entity, describe_entity, query, get (SPEC §5, план M1d
задача 4). Ядро слоя тулов — собирает конвейер гейта (задача 1), построение запроса (задача 2),
клиент 1С (SPEC §9) и формирование ответа (задача 3) в готовые ответы MCP-тулов. Не знает про
MCP-транспорт (`mcp.server`) — тестируется без сети, без реального демона.

Конвейер ответа (порядок неизменен, SPEC §5.1, §6): inbound-подмена (`gate.inbound_*`) → сборка
запроса (`odata_query.build_query`/`build_get`) → клиент 1С (`Client1C.get`) → разбор ответа
(`response.items_of`) → очистка служебных полей (`response.strip_service`) → прямая подмена
(`gate.mask`) → усечение строк (`response.truncate_strings`) → конверт SPEC §5.1 → подгонка под
лимит (`response.fit_result`) → страж (`gate.finish`/`gate.finish_text`).

Инвариант 1 (реальные значения не выходят через MCP никогда, включая ошибки 1С) держится тем, что
у КАЖДОГО метода единственный выход из тела — через `gate.finish`/`gate.finish_text`/`gate.error`
(известная база) или `guard_only` (база ещё не определена — гейта для неё нет). Ни один метод
этого класса не бросает исключение наружу: `_run` — общая точка, где любое ожидаемое исключение
превращается в текст ошибки тула, а неожиданное — в код `internal` без текста исключения (в нём
могут быть данные 1С — SPEC §5.2, поправка этой задачи).
"""

from __future__ import annotations

import dataclasses
import json
import logging

from odata1c.client1c.client import Client1C
from odata1c.client1c.errors import OdataError
from odata1c.config.loader import ConfigError
from odata1c.config.models import AppConfig, BaseConfig
from odata1c.gate.dictionary import Dictionary
from odata1c.gate.guard import Guard
from odata1c.gate.masking import ПРЕДУПРЕЖДЕНИЕ_СКРЫТОЙ_СВЯЗИ, Resolve
from odata1c.gate.pipeline import BaseGate, guard_only
from odata1c.gate.policy import PolicyError
from odata1c.gate.revealed import RevealedValues
from odata1c.gate.service import classifier_for, open_dictionary, policy_path, refresh_policy
from odata1c.gate.unmasking import GateError
from odata1c.index.edmx import EdmxError
from odata1c.index.reindex import index_path
from odata1c.index.reindex import reindex as rebuild_index
from odata1c.index.repository import (
    EntityDescription,
    FoundEntity,
    IndexCorruptError,
    IndexRepository,
)
from odata1c.recipes.model import Recipe, RecipeBook, RecipeError, load_recipes, recipes_path
from odata1c.recipes.render import probe as probe_recipe
from odata1c.recipes.render import render as render_recipe
from odata1c.registry.registry import Registry, SessionScope
from odata1c.tools import describe as describe_tool
from odata1c.tools import info as info_topics
from odata1c.tools.odata_query import QueryError, build_get, build_query, orderby_fields
from odata1c.tools.response import fit_result, items_of, page_info, strip_service, truncate_strings

_log = logging.getLogger(__name__)

# Виртуальные таблицы регистров (SPEC §4.2, поправка проба P4): полный перечень действий,
# которые индекс хранит как виртуальную таблицу родителя. Используется только для одной вещи —
# по имени вида `<регистр>_<Суффикс>`, которого нет в индексе, найти родительский регистр и
# предложить модели те виртуальные таблицы, что у него ДЕЙСТВИТЕЛЬНО есть (частый промах: модель
# запрашивает `_Balance` у регистра оборотов, где есть только `_Turnovers`).
_ВИРТУАЛЬНЫЕ_СУФФИКСЫ = (
    "_BalanceAndTurnovers",
    "_RecordsWithExtDimensions",
    "_ActualActionPeriod",
    "_DrCrTurnovers",
    "_ExtDimensions",
    "_ScheduleData",
    "_SliceFirst",
    "_Turnovers",
    "_SliceLast",
    "_Balance",
    "_Base",
)

# Крайний рубеж инварианта 1: если даже `gate.error` (маскировка сообщения) упадёт — например,
# сам словарь или страж в этот момент повреждены — наружу должен уйти текст без единого байта из
# текущего вызова, а не голое исключение (оно потеряло бы ответ у клиента MCP) и не текст падения
# `gate.error` (в нём могут остаться данные о неудачной попытке замены). Константа, а не вызов
# `json.dumps` на каждый случай: собирать её из текущих данных значило бы повторять тот же риск.
_ОТКАЗ_НА_КРАЙНИЙ_СЛУЧАЙ = (
    '{"error": {"code": "internal", '
    '"message": "внутренняя ошибка шлюза, подробности в журнале демона", "hint": ""}}'
)


class _ServiceError(Exception):
    """Ошибка самого сервиса тулов, не сборки запроса и не 1С: сущность не проиндексирована,
    не найдена в индексе, скрыта политикой гейта, сортировка по защищаемому полю. Тот же протокол
    атрибутов (code, message, hint), что у OdataError/QueryError/IndexCorruptError/PolicyError —
    `_run` перехватывает их одним блоком."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


# Сколько строк разницы реиндекса показывать модели по каждому списку (бриф задачи 7): полный
# список на боевой базе — тысячи имён, и он вытеснит из ответа всё остальное. Полное число
# отдаётся рядом отдельным полем `*_total`.
_ПРЕДЕЛ_РАЗНИЦЫ = 50

# Символы, при которых путь `raw_get` отклоняется целиком (SPEC §9, проба P4 — и сверх неё):
# `?` и `#` обрезали бы путь, отправив остаток в query или во фрагмент; `\` публикация за IIS
# молча заменяет на `/`, то есть меняет адрес; `%` запрещён не из-за 1С, а из-за нас самих —
# процентная запись позволила бы записать `..` как `%2e%2e` и пройти проверку на выход за
# пределы публикации, а кодирует путь всё равно сам клиент (`client1c._экранировать_путь`),
# так что писать её руками и незачем.
_ЗАПРЕЩЕНО_В_ПУТИ = ("?", "#", "\\", "%")


def _проверить_путь(path: str) -> str:
    """Путь `raw_get` — ОТНОСИТЕЛЬНЫЙ путь внутри `standard.odata/` базы, и ничего сверх того.

    Отклоняются: пустой путь, абсолютный адрес (`http://…`, `//host/…`), ведущий `/`, сегменты
    `.` и `..` (выход за пределы публикации увёл бы запрос на произвольный адрес того же сервера,
    где ни гейт, ни разрешения базы уже ничего не значат), `$metadata` (десятки мегабайт мимо
    индекса — для него есть `reindex`) и символы `_ЗАПРЕЩЕНО_В_ПУТИ`.
    """
    путь = (path or "").strip()
    if not путь:
        raise _ServiceError("params_invalid", "путь не указан")
    if "://" in путь or путь.startswith("//"):
        raise _ServiceError(
            "params_invalid",
            "путь должен быть относительным, без адреса сервера",
            "адрес базы шлюз подставляет сам: путь начинается с имени сущности",
        )
    if путь.startswith("/"):
        raise _ServiceError("params_invalid", "путь не должен начинаться с «/»: он относительный")
    for символ in _ЗАПРЕЩЕНО_В_ПУТИ:
        if символ in путь:
            raise _ServiceError(
                "params_invalid",
                f"путь не должен содержать «{символ}»",
                "параметры запроса передавайте аргументом query, кодировать путь не нужно",
            )
    if any(ord(символ) < 0x20 or ord(символ) == 0x7F for символ in путь):
        raise _ServiceError("params_invalid", "путь содержит управляющие символы")
    сегменты = путь.split("/")
    if any(сегмент in ("..", ".") for сегмент in сегменты):
        raise _ServiceError(
            "params_invalid",
            "путь не должен выходить за пределы публикации OData базы",
            "уберите сегменты «..» и «.»",
        )
    if сегменты[0].split("(")[0] == "$metadata":
        raise _ServiceError(
            "params_invalid",
            "$metadata через raw_get не отдаётся",
            "описание метаданных читает индекс: odata1c_reindex, затем find_entity/describe_entity",
        )
    if any(сегмент.split("(")[0] == "$value" for сегмент in сегменты):
        # Пункт 5 дополнения к раунду правок 1 задачи 8 (Critical): `…/ИНН/$value` отдаёт голое
        # значение без JSON-обёртки — вместе с обёрткой теряется и имя реквизита, по которому
        # гейт только и может понять, что это ИНН. Реальный ИНН уходил модели числом при пустом
        # `masked_fields`. Поверх `…/ИНН` этот хвост не даёт ничего, поэтому запрещён целиком —
        # как `$metadata`, и по той же причине: путь, который штатным способом выражается лучше.
        raise _ServiceError(
            "params_invalid",
            "$value через raw_get не отдаётся",
            "запросите само свойство без «/$value» — ответ придёт с именем реквизита",
        )
    return путь


@dataclasses.dataclass(slots=True)
class _ЦельПути:
    """Разбор пути `raw_get` по индексу: сущность, по классам полей которой маскируется ответ;
    разрешился ли путь по индексу ЦЕЛИКОМ; имя реквизита из неразрешённого хвоста пути."""

    entity: str
    resolved: bool
    field: str | None = None


# Ruling 18: строгая политика — не оправдание утечки, а объяснение, почему замаскировано больше
# обычного, и подсказка, чем это лечится.
_ПРЕДУПРЕЖДЕНИЕ_ВНЕ_ИНДЕКСА = (
    "путь не разрешён по индексу целиком (сущность или сегмент вне индекса): применена строгая "
    "политика — наименования и ФИО маскируются, поиск реквизитов по значению включён "
    "принудительно; для точной разметки полей обновите индекс: odata1c_reindex"
)


# Системные сегменты OData: именем реквизита не бывают никогда (пункт 5 дополнения). `$value`
# запрещён отдельно и раньше (`_проверить_путь`); остальные — законный хвост пути, но
# переименовывать по ним ответ нельзя: `{"$count": 42}` вместо `{"value": 42}` — искажение.
_СИСТЕМНЫЕ_СЕГМЕНТЫ = frozenset({"$value", "$count", "$ref", "$links", "$skiptoken", "$batch"})


def _без_повторов(предупреждения: list[str]) -> list[str]:
    """Порядок сохранён, повторы убраны: предупреждение об изъятой по политике связи приходит с
    двух сторон сразу — от обрезки `$expand` в запросе и от изъятия объекта из ответа (см.
    `masking.ПРЕДУПРЕЖДЕНИЕ_СКРЫТОЙ_СВЯЗИ`), а модели оно нужно один раз."""
    return list(dict.fromkeys(предупреждения))


def _скаляром(значение):
    """Скаляр ответа 1С — строкой, до маскировки (пункт 5 дополнения, третье звено).

    Число маскировщик не трогает и трогать не должен (инвариант 6: суммы и количества), а страж
    числовые литералы внутри JSON-конверта пропускает сознательно — и об этом прямо сказано в его
    докстринге. Значит реквизит, пришедший числом (`{"value": 7707083893}` на пути
    `…(guid'…')/ИНН`), закрыть можно только ДО маскировки. Строковый вид числа проверяется теми
    же детекторами, что любое строковое поле, а суммы, даты, коды и номера они не трогают —
    инвариант 6 от приведения не страдает (проверено отдельным параметризованным тестом).
    """
    if isinstance(значение, bool):
        return "true" if значение else "false"
    if значение is None or isinstance(значение, dict | list | str):
        return значение
    return str(значение)


def _восстановить_имя_поля(записи: list, хвост: str | None) -> list:
    """Вернуть записи ответа имя реквизита, взятое из неразрешённого хвоста пути.

    Ответ 1С на примитивное свойство — `{"value": <скаляр>}` (`…(guid'…')/ИНН`): имени поля в
    теле ответа нет вовсе, оно стоит сегментом пути. Без него не работает ни классификация по
    имени поля, ни детектор контекста (10-значный ИНН неотличим от номера документа и требует
    слова «ИНН» рядом — `detectors._подтверждено`), и реальный ИНН уходил модели открытым при
    `masked_fields: []` (форма (в) ревью 2026-09-11).

    Хвост берётся ПЕРВЫМ неразрешённым сегментом и только когда он единственный (пункт 5
    дополнения): имя реквизита стоит сразу после последней разрешённой сущности, а прежний
    «последний сегмент» вытеснялся любым продолжением пути — `…/ИНН/$value` давал хвост `$value`,
    и имя `ИНН`, ради которого эта функция и написана, терялось.

    Затрагивает только форму «словарь из одного ключа `value` со скаляром» — ту самую, где имя
    поля потеряно. Список записей, объект с полями, вложенные структуры не трогаются: там имена
    полей пришли от 1С и переименовывать их значило бы искажать ответ.
    """
    if not хвост:
        return записи
    return [
        {хвост: _скаляром(запись["value"])}
        if isinstance(запись, dict)
        and set(запись) == {"value"}
        and not isinstance(запись["value"], dict | list)
        else запись
        for запись in записи
    ]


def _целое_или_ноль(значение: str | None) -> int:
    try:
        return max(int(значение), 0)
    except (TypeError, ValueError):
        return 0


def _как_список(значение: list[str] | str | None) -> list[str]:
    """Список полей `select`/`orderby`: строка с запятыми или готовый список — тот же разбор
    аргумента тула, что и в `odata_query._к_списку`, но не оттуда: та функция приватная, а нужна
    здесь только для одной проверки (`DataVersion` явно запрошен явным `select`)."""
    if значение is None:
        return []
    сырые = значение.split(",") if isinstance(значение, str) else list(значение)
    return [элемент.strip() for элемент in сырые if элемент and элемент.strip()]


def _рецепты_markdown(конверт: dict) -> str:
    """Читаемый вид перечня рецептов для ресурса `odata1c://recipes/{base}`."""
    строки = [f"# Рецепты базы {конверт['base']} ({конверт['role']}, гейт {конверт['gate']})", ""]
    if конверт.get("hint"):
        строки += [конверт["hint"], ""]
    for рецепт in конверт["recipes"]:
        заголовок = рецепт["title"] or рецепт["name"]
        строки.append(f"## {рецепт['name']} — {заголовок}")
        if рецепт["description"]:
            строки.append(рецепт["description"])
        строки.append(f"Сущность: `{рецепт['entity']}`")
        # Строго `is False`: `None` — «применимость неизвестна, базы нет в индексе», и объявлять
        # такой рецепт неприменимым нельзя (подсказка про реиндекс стоит у всего перечня).
        if рецепт["applicable"] is False:
            строки.append(f"**Неприменим к этой базе**: {рецепт.get('hint', '')}")
        if рецепт["params"]:
            строки.append("")
            строки.append("| Параметр | Тип | Обязателен | Описание |")
            строки.append("|---|---|---|---|")
            for параметр in рецепт["params"]:
                обязателен = "да" if параметр["required"] else "нет"
                строки.append(
                    f"| {параметр['name']} | {параметр['type']} | {обязателен} | "
                    f"{параметр['description']} |"
                )
        строки.append("")
    return "\n".join(строки)


class ToolService:
    """Фасад слоя тулов чтения на все базы процесса. Словарь и страж — по одному на сервис
    (SPEC §6.6, §6.8: словарь копит токены всех баз, страж собирает по нему один автомат).
    `BaseGate` и `Client1C` — по одному на базу, лениво, по первому обращению: гейт держит
    собранные на политике маскировщик/размаскировщик, клиент — пул соединений и семафор базы."""

    def __init__(self, config: AppConfig, *, client_factory=Client1C) -> None:
        self._config = config
        self._client_factory = client_factory
        self._registry = Registry(config)
        self._dictionary: Dictionary = open_dictionary(config.home, config.daemon.gate_secret)
        self._guard = Guard(self._dictionary)
        self._gates: dict[str, BaseGate] = {}
        self._clients: dict[str, Client1C] = {}

    # -- жизненный цикл -----------------------------------------------------------------

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        self._dictionary.close()

    def _gate_for(self, base: BaseConfig) -> BaseGate:
        гейт = self._gates.get(base.name)
        if гейт is None:
            гейт = BaseGate(
                base=base,
                dictionary=self._dictionary,
                guard=self._guard,
                policy_path=policy_path(self._config.home, base.name),
            )
            self._gates[base.name] = гейт
        return гейт

    def _client_for(self, base: BaseConfig) -> Client1C:
        клиент = self._clients.get(base.name)
        if клиент is None:
            клиент = self._client_factory(base)
            self._clients[base.name] = клиент
        return клиент

    # -- общий конвейер вызова тула -------------------------------------------------------

    def _safe_error(
        self,
        gate: BaseGate,
        code: str,
        message: str,
        hint: str = "",
        revealed: RevealedValues | None = None,
    ) -> str:
        """`revealed` — набор раскрытого в этом вызове (задача N1 M1d). Путь ошибки нуждается
        в нём не меньше успешного, а по сути больше: 1С повторяет выражение отбора в тексте
        ошибки, и раскрытое значение возвращается модели именно здесь."""
        try:
            return gate.error(code, message, hint, revealed)
        except Exception:
            _log.exception("gate.error упал при обработке ошибки тула — отдан отказ без данных")
            return _ОТКАЗ_НА_КРАЙНИЙ_СЛУЧАЙ

    def note_error(self, base: str, message: str) -> None:
        """Запомнить последнюю ошибку базы в реестре — её показывает `odata1c base list` и
        диагностика демона. Нужна фоновой проверке `$metadata` (SPEC §4.3, задача 7): она
        вызывает `reindex` мимо MCP, и её отказы иначе не видно нигде, кроме журнала.

        Имя базы, которой нет в реестре, здесь не ошибка и не повод падать: реестр собран из
        того же `bases.yaml`, что и вызывающий цикл, а конкурентная перечитка настроек — не
        причина ронять фоновую задачу.
        """
        try:
            self._registry.set_error(base, message)
        except ConfigError:
            _log.warning("ошибка базы %s не записана в реестр: база в реестре не значится", base)

    def _guard_error(self, code: str, message: str, hint: str = "") -> str:
        """Ошибка тула БЕЗ гейта конкретной базы: либо база ещё не определена, либо гейт этой
        базы не удалось построить (см. `_run`) — в обоих случаях `gate.error`/`_safe_error`
        вызвать нечем, страж живёт на сервисе независимо от гейта (SPEC §6.8)."""
        return guard_only(self._guard, {"error": {"code": code, "message": message, "hint": hint}})

    async def _run(
        self,
        scope: SessionScope,
        base: str | None,
        body,
        *,
        finisher=None,
        with_index: bool = True,
    ) -> str:
        """Общая точка входа тулов на одну базу: разрешить базу → получить/обновить гейт →
        открыть индекс на время вызова → выполнить `body(base_config, gate, repo)` → отдать
        результат через `finisher` (по умолчанию `gate.finish`). Любое ожидаемое исключение —
        код ошибки тула через гейт; неожиданное — `internal` без текста исключения.

        `with_index=False` — не открывать индекс вовсе, `body` получает `None` третьим аргументом
        (план M1d, задача 7). Нужно двум тулам, и по разным причинам. `reindex` на Windows иначе
        не работал бы вообще: `index/reindex.py::_заменить` подменяет файл индекса через
        `os.replace`, а открытый в этом же процессе файл Windows заменить не даёт. `info` и
        `resource_policy` индекса не читают, а на непроиндексированной базе `_open_index` отказал
        бы кодом `entity_unknown` — то есть реиндекс нельзя было бы вызвать ровно там, где он и
        нужен."""
        # Набор раскрытого — на один вызов и только на него (задача N1 M1d): `BaseGate`
        # кэшируется на базу и делится между сессиями, поэтому хранить «что раскрыли» на
        # гейте нельзя — это было бы пересечением сессий, то есть утечкой данных одной
        # сессии в другую. Создаётся здесь, в единственной общей точке входа тулов, и
        # отдаётся и телу вызова (там раскрывают), и стражу на выходе (там ищут обратно),
        # включая оба пути ошибки ниже.
        раскрытое = RevealedValues()
        итог = finisher or (lambda gate, result, revealed: gate.finish(result, revealed))
        try:
            base_config = self._registry.get(base, scope)
        except ConfigError as ошибка:
            # База не определена — гейта для неё нет и быть не может (нет ни политики, ни
            # известного режима): страж на строжайшем уровне, не gate.error.
            return self._guard_error(ошибка.code, str(ошибка), ошибка.hint)

        try:
            гейт = self._gate_for(base_config)
        except (ConfigError, PolicyError, IndexCorruptError) as ошибка:
            # Критично (ревью, раунд 1): `_gate_for` конструирует `BaseGate` при первом
            # обращении, а конструктор сам вызывает `refresh()` → `load_policy()` — на
            # СУЩЕСТВУЮЩЕМ, но синтаксически битом policy.yaml это `PolicyError` ДО входа во
            # второй try ниже (тот перехватывает `PolicyError` только у вызовов `refresh()`
            # ПОСЛЕ успешного первого построения гейта). Без отдельного try этот путь ронял бы
            # голое исключение мимо клиента MCP, а сам гейт — раз конструктор упал — не
            # закэширован, и следующий вызов падал бы точно так же. Здесь и гейта, чтобы
            # замаскировать сообщение через `gate.error`, ещё нет — тот же приём, что и для
            # неопределённой базы выше: `guard_only` на страже сервиса.
            return self._guard_error(
                getattr(ошибка, "code", "internal"), str(ошибка), getattr(ошибка, "hint", "")
            )
        except Exception:
            _log.exception(
                "внутренняя ошибка при построении гейта базы odata1c — детали в журнале демона"
            )
            return self._guard_error(
                "internal", "внутренняя ошибка шлюза, подробности в журнале демона"
            )

        репозиторий: IndexRepository | None = None
        try:
            гейт.refresh()
            if with_index:
                репозиторий = self._open_index(base_config)
            результат = await body(base_config, гейт, репозиторий, раскрытое)
            return итог(гейт, результат, раскрытое)
        except (
            OdataError,
            EdmxError,
            GateError,
            QueryError,
            IndexCorruptError,
            PolicyError,
            RecipeError,
            _ServiceError,
        ) as ошибка:
            return self._safe_error(
                гейт,
                ошибка.code,
                str(ошибка),
                getattr(ошибка, "hint", ""),
                revealed=раскрытое,
            )
        except Exception:
            _log.exception("внутренняя ошибка тула odata1c — детали в журнале демона")
            return self._safe_error(
                гейт,
                "internal",
                "внутренняя ошибка шлюза, подробности в журнале демона",
                revealed=раскрытое,
            )
        finally:
            if репозиторий is not None:
                репозиторий.close()

    def _open_index(self, base: BaseConfig) -> IndexRepository:
        путь = index_path(self._config.home, base.name)
        if not путь.exists():
            # Файл не открываем: sqlite3.connect на несуществующем пути молча создал бы пустую
            # базу вместо понятной диагностики (тот же риск, что и в bases() ниже).
            raise _ServiceError(
                "entity_unknown",
                f"база «{base.name}» не проиндексирована",
                "вызовите odata1c_reindex(base) или odata1c reindex <база>",
            )
        репозиторий = IndexRepository(путь)
        try:
            репозиторий.require_current_version()
        except IndexCorruptError:
            репозиторий.close()
            raise
        return репозиторий

    def _resolve_entity(
        self, repo: IndexRepository, gate: BaseGate, entity: str
    ) -> EntityDescription:
        # Скрытость проверяется ДО существования: иначе ответ на скрытую и на опечатанную
        # сущность различался бы кодом ошибки, а это и есть утечка факта существования скрытой
        # сущности модели (ревью плана, раунд 1).
        if gate.is_hidden(entity):
            raise _ServiceError("entity_hidden", f"сущность «{entity}» скрыта политикой гейта")
        описание = repo.describe(entity)
        if описание is None:
            raise _ServiceError(
                "entity_unknown",
                f"сущность «{entity}» не найдена в индексе базы",
                self._entity_hint(repo, gate, entity),
            )
        return описание

    def _entity_hint(self, repo: IndexRepository, gate: BaseGate, entity: str) -> str:
        родитель = self._виртуальный_родитель(repo, gate, entity)
        if родитель is not None:
            дети_виртуальные = sorted(
                имя
                for имя in родитель.children
                if not gate.is_hidden(имя)
                and (описание := repo.describe(имя))
                and описание.is_virtual
            )
            if дети_виртуальные:
                return (
                    f"у регистра {родитель.name} нет такой виртуальной таблицы; есть: "
                    + ", ".join(дети_виртуальные)
                )
        кандидаты = self._видимые_кандидаты(repo, gate, entity, limit=5)
        if кандидаты:
            return "похожие сущности в индексе: " + ", ".join(f.name for f in кандидаты)
        return "обновите индекс: odata1c reindex <база>, либо проверьте имя сущности"

    def _виртуальный_родитель(
        self, repo: IndexRepository, gate: BaseGate, entity: str
    ) -> EntityDescription | None:
        for суффикс in _ВИРТУАЛЬНЫЕ_СУФФИКСЫ:
            if entity.endswith(суффикс):
                имя_родителя = entity[: -len(суффикс)]
                if gate.is_hidden(имя_родителя):
                    return None
                return repo.describe(имя_родителя)
        return None

    def _навигации(self, repo: IndexRepository) -> Resolve:
        """Резолвер «сущность и ключ ответа → сущность вложенного объекта» для маскировки
        (итоговое ревью M1d, C1): раскрытый через `$expand` объект и строка табличной части
        обрабатываются по политике своей сущности (SPEC §6.5), а не той, что наверху.

        Два источника, оба из индекса, а не из списка имён (список имён навигаций — гонка, в
        которой у настоящей УТ уже есть `КлиентКонтрагент`, `Курьер`, `Сборщик`, `Отпустил`):
        `navigations` знает цель навигации точно, а табличная часть навигацией не приходит вовсе
        (проба P4) — она дочерняя сущность с именем `<родитель>_<ключ>` (`children`).

        Описания кэшируются на вызов: обход ответа спрашивает резолвер на каждый вложенный
        объект каждой записи выборки, а `describe` — это несколько запросов к SQLite."""
        кэш: dict[str, EntityDescription | None] = {}

        def описание(имя: str) -> EntityDescription | None:
            if имя not in кэш:
                кэш[имя] = repo.describe(имя)
            return кэш[имя]

        def резолвер(entity: str, key: str) -> str | None:
            текущая = описание(entity)
            if текущая is None:
                return None
            цель = текущая.navigations.get(key)
            if цель is not None:
                return цель
            дочерняя = f"{entity}_{key}"
            return дочерняя if дочерняя in текущая.children else None

        return резолвер

    def _обрезать_expand(
        self,
        repo: IndexRepository,
        gate: BaseGate,
        описание: EntityDescription | None,
        пути: list[str],
    ) -> tuple[list[str], list[str]]:
        """Пути `$expand`, ведущие к скрытой сущности, вырезаются из запроса (SPEC §6.9, строка
        1068: «`hide: true` … `$expand` на неё обрезается»).

        Способ отказа — именно обрезание, а не `entity_hidden`: код ошибки сообщил бы модели о
        существовании скрытой сущности, чего `_resolve_entity` сознательно избегает. Обрезка
        видна в ответе предупреждением, но без имени сущности и без имени навигации — иначе
        предупреждение вернуло бы ровно тот факт, ради сокрытия которого делается обрезка.

        Путь обрезается ЦЕЛИКОМ, а не до последнего разрешённого звена: `А/Б` со скрытой `Б`
        нельзя спасти, оставив `А`.

        Звено, которого индекс не знает (неизвестная навигация, цель вне индекса), проверить на
        скрытость нечем — то же правило, что у `_цель_пути`: там, где у владельца есть правила
        `hide`, неразрешённое звено не обслуживается; где скрывать нечего, оно уходит в 1С как
        есть. У `query`/`get` до этого случая дело обычно не доходит — построитель запроса
        отклоняет неизвестную навигацию своей ошибкой, — но на `raw_get` он штатный.
        """
        оставленные: list[str] = []
        обрезано = False
        for путь in пути:
            if self._скрыт_путь_раскрытия(repo, gate, описание, путь):
                обрезано = True
            else:
                оставленные.append(путь)
        предупреждения = [ПРЕДУПРЕЖДЕНИЕ_СКРЫТОЙ_СВЯЗИ] if обрезано else []
        return оставленные, предупреждения

    def _скрыт_путь_раскрытия(
        self,
        repo: IndexRepository,
        gate: BaseGate,
        описание: EntityDescription | None,
        путь: str,
    ) -> bool:
        текущая = описание
        for сегмент in путь.split("/"):
            цель = текущая.navigations.get(сегмент) if текущая is not None else None
            if цель is None:
                return gate.has_hidden_entities()
            if gate.is_hidden(цель):
                return True
            текущая = repo.describe(цель)
        return False

    def _поле_защищено_по_пути(
        self, repo: IndexRepository, gate: BaseGate, desc: EntityDescription, path: str
    ) -> bool:
        """Защищено ли конечное поле пути `$orderby` — путь может идти через навигацию
        (`Контрагент/ИНН`): сортировка по такому полю раскрывает исходное значение не хуже
        прямого (ревью, раунд 1, Minor) — `is_protected(entity, "Контрагент/ИНН")` с сущностью
        верхнего уровня всегда возвращал бы «не защищено», потому что ключи политики —
        `сущность.поле`, а не путь. Путь резолвится через `EntityDescription.navigations` той же
        цепочкой, что `odata_query._обработать_expand` резолвит `$expand` — но без ограничения
        по `limits.expand_depth`: здесь не строится запрос, а только проверяется защита.

        `build_query` НЕ проверяет путь `$orderby` вообще — он копирует строку `orderby` в
        `$orderby` как есть (в отличие от `$expand`, который и правда идёт через
        `_обработать_expand`); это единственная проверка навигационного пути orderby во всём
        конвейере. Нераспознанный сегмент (неизвестная навигация или сущность-цель) — отказ
        консервативный (`True`, «защищено»): раскрывать оракул сравнения на пути, который сама
        эта проверка не может объяснить, опаснее, чем излишне запретить сортировку."""
        *навигации, поле = path.split("/")
        текущая = desc
        for сегмент in навигации:
            цель_имя = текущая.navigations.get(сегмент)
            if цель_имя is None:
                return True
            следующая = repo.describe(цель_имя)
            if следующая is None:
                return True
            текущая = следующая
        return gate.is_protected(текущая.name, поле)

    def _видимые_кандидаты(
        self,
        repo: IndexRepository,
        gate: BaseGate,
        query: str,
        *,
        kind: str | None = None,
        limit: int,
    ) -> list[FoundEntity]:
        # find() и так сканирует все строки таблицы независимо от limit (сортировка по счёту —
        # последний шаг) — запас с лихвой не добавляет накладных расходов, но не даёт скрытым
        # сущностям вытеснить настоящих кандидатов из первых `limit` результатов.
        запас = max(limit * 4, 20)
        найдено = repo.find(query, kind, limit=запас)
        видимые = [сущность for сущность in найдено if not gate.is_hidden(сущность.name)]
        return видимые[:limit]

    # -- тулы ------------------------------------------------------------------------------

    async def bases(self, scope: SessionScope) -> str:
        строки = []
        for состояние in self._registry.visible(scope):
            строка = {
                "name": состояние.name,
                "label": состояние.label,
                "role": состояние.role,
                "gate": состояние.gate_mode,
                "write": состояние.write,
                "indexed": False,
                "indexed_at": None,
                "entity_count": None,
            }
            путь = index_path(self._config.home, состояние.name)
            if путь.exists():
                try:
                    репозиторий = IndexRepository(путь)
                except IndexCorruptError as ошибка:
                    # Ключ НЕ "last_error" (ревью, раунд 1, Minor): так называется поле
                    # BaseState, которое заполняет Registry.set_error текстом OdataError на
                    # путях реиндекса — 1С-данные. Здесь же — локальный текст IndexCorruptError
                    # (путь к файлу и сообщение sqlite, без содержимого 1С), и разные имена не
                    # дают их перепутать и однажды случайно вывести первое под видом второго.
                    строка["index_error"] = str(ошибка)
                else:
                    try:
                        репозиторий.require_current_version()
                        строка["indexed"] = True
                        строка["indexed_at"] = репозиторий.meta("indexed_at")
                        число_сущностей = репозиторий.meta("entity_count")
                        строка["entity_count"] = (
                            int(число_сущностей) if число_сущностей is not None else None
                        )
                    except IndexCorruptError as ошибка:
                        # Одна база со старым индексом не должна обнулять список остальных —
                        # состояние остаётся indexed=False с пояснением, а не отказ всего тула.
                        строка["index_error"] = str(ошибка)
                    finally:
                        репозиторий.close()
            строки.append(строка)

        # Умолчание объявляется, только если оно видимо сессии (Important итогового ревью M1d):
        # суженной сессии сообщалось `default: "ut"` — имя базы вне её области видимости, — а
        # любой вызов без явного `base` отвечал ей `base_unknown`. Объявленное умолчание должно
        # быть либо рабочим, либо не объявляться вовсе; заодно имя чужой базы не называется.
        умолчание = scope.default or self._config.default
        видимые = {строка["name"] for строка in строки}
        конверт: dict = {
            "bases": строки,
            "default": умолчание if умолчание in видимые else None,
        }
        if not строки:
            конверт["hint"] = (
                f"опишите базы в {self._config.home / 'bases.yaml'} или перенесите из прежнего "
                "сервера: odata1c base import <путь к env>"
            )
        # Не guard_only (ревью, раунд 1, Minor — обсуждено и оставлено как есть): guard_only —
        # строжайший уровень ДЛЯ ДАННЫХ 1С у ещё не определённой базы (см. докстринг
        # pipeline.guard_only), а здесь база у каждой строки определена, и то, что уходит в
        # конверт, — ТОЛЬКО локальные данные: bases.yaml (name, label, role, gate, write) и
        # метаданные индекса (indexed_at, entity_count, index_error — текст IndexCorruptError,
        # см. выше). Пропустить их через страж на строжайшем уровне ломает, а не защищает:
        # доказано исполнением — label "Песочница 7707083893" у базы с gate=off после
        # guard_only на identifiers+names превращался в "Песочница [[inn:…]]", потому что цифры
        # совпали с уже токенизированным в словаре значением другой базы (тест
        # test_bases_не_пропускает_локальные_данные_через_страж). BaseState.last_error
        # (Registry.set_error, текст OdataError на путях реиндекса — уже данные 1С) сюда
        # СОЗНАТЕЛЬНО не выводится и не должен появиться без прогона через гейт своей базы.
        return json.dumps(конверт, ensure_ascii=False)

    async def find_entity(
        self,
        scope: SessionScope,
        *,
        base: str | None = None,
        query: str,
        kind: str | None = None,
        limit: int = 10,
    ) -> str:
        async def тело(base_config, гейт, репозиторий, _раскрытое):
            кандидаты = self._видимые_кандидаты(репозиторий, гейт, query, kind=kind, limit=limit)
            return {
                "base": base_config.name,
                "role": base_config.role,
                "gate": гейт.mode,
                "entities": [
                    {
                        "name": сущность.name,
                        "kind": сущность.russian_kind,
                        "key_fields": сущность.key_fields,
                        "fields": сущность.field_preview,
                    }
                    for сущность in кандидаты
                ],
                "warnings": [],
            }

        return await self._run(scope, base, тело)

    async def describe_entity(
        self,
        scope: SessionScope,
        *,
        base: str | None = None,
        entity: str,
        response_format: str = "markdown",
    ) -> str:
        async def тело(base_config, гейт, репозиторий, _раскрытое):
            описание = self._resolve_entity(репозиторий, гейт, entity)
            # Реализация обязана читать РЕАЛЬНУЮ структуру EntityDescription/describe(), а не
            # полагаться на бриф вслепую (решение оркестратора) — отсюда прямой проброс полей
            # через describe_tool.build, без домысливания недостающих атрибутов.
            #
            # `гейт._policy` — обращение к приватному атрибуту BaseGate (ревью, раунд 1, Minor):
            # `pipeline.py` не даёт публичного доступа к `Policy`, а бриф этой задачи прямо
            # называет `masking.effective_field_class(policy, entity, field, mode=...)` как
            # источник класса поля для describe, и `pipeline.py` вне зоны правок задачи 4. Связ-
            # анность осознанная, не забытая — публичный аксессор (`BaseGate.policy`/`.field_
            # class(...)`) числится долгом следующей правки `gate/pipeline.py` (задача демона
            # или M1e), не этой задачи.
            факты = describe_tool.build(описание, policy=гейт._policy, mode=гейт.mode)
            факты["entity"] = описание.name
            факты["base"] = base_config.name
            факты["role"] = base_config.role
            факты["gate"] = гейт.mode
            return факты

        def итог(гейт: BaseGate, факты: dict, раскрытое: RevealedValues) -> str:
            if response_format == "markdown":
                return гейт.finish_text(describe_tool.render_markdown(факты), раскрытое)
            return гейт.finish(факты, раскрытое)

        return await self._run(scope, base, тело, finisher=итог)

    async def query(
        self,
        scope: SessionScope,
        *,
        base: str | None = None,
        entity: str,
        filter: str | None = None,
        select: list[str] | str | None = None,
        expand: list[str] | str | None = None,
        orderby: str | None = None,
        top: int | None = None,
        skip: int | None = None,
        inlinecount: bool = False,
        params: dict | None = None,
        allowed_only: bool = False,
    ) -> str:
        async def тело(base_config, гейт, репозиторий, раскрытое):
            описание = self._resolve_entity(репозиторий, гейт, entity)
            self._проверить_сортировку(репозиторий, гейт, описание, orderby)

            реальный_filter = (
                гейт.inbound_filter(filter, entity=описание.name, revealed=раскрытое)
                if filter
                else filter
            )
            реальные_params = params
            if описание.is_virtual and params and "Condition" in params:
                реальные_params = dict(params)
                реальные_params["Condition"] = гейт.inbound_filter(
                    params["Condition"], entity=описание.name, revealed=раскрытое
                )

            return await self._выборка(
                base_config,
                гейт,
                репозиторий,
                описание,
                раскрытое,
                filter=реальный_filter,
                select=select,
                expand=expand,
                orderby=orderby,
                top=top,
                skip=skip,
                inlinecount=inlinecount,
                params=реальные_params,
                allowed_only=allowed_only,
            )

        return await self._run(scope, base, тело)

    def _проверить_сортировку(
        self,
        repo: IndexRepository,
        gate: BaseGate,
        desc: EntityDescription,
        orderby: str | None,
    ) -> None:
        """Сортировка по защищаемому полю запрещена (SPEC §6): порядок строк раскрывает исходное
        значение не хуже самого значения. Проверка общая для `query` и `recipe` — рецепт пишет
        владелец машины, но читает ответ модель, и оракул сравнения от авторства не зависит."""
        if not orderby:
            return
        for поле in orderby_fields(orderby):
            if self._поле_защищено_по_пути(repo, gate, desc, поле):
                raise _ServiceError(
                    "params_invalid",
                    "сортировка по защищаемому полю недоступна: порядок раскрывает значения",
                )

    async def _выборка(
        self,
        base_config: BaseConfig,
        гейт: BaseGate,
        репозиторий: IndexRepository,
        описание: EntityDescription,
        раскрытое: RevealedValues,
        *,
        filter: str | None = None,  # noqa: A002 — имя аргумента тула зафиксировано SPEC §5
        select: list[str] | str | None = None,
        expand: list[str] | str | None = None,
        orderby: str | None = None,
        top: int | None = None,
        skip: int | None = None,
        inlinecount: bool = False,
        params: dict | None = None,
        allowed_only: bool = False,
        extra: dict | None = None,
    ) -> dict:
        """Общий хвост выборки для `query` и `recipe` (план M1d, задача 8): построение запроса →
        клиент 1С → очистка служебных полей → маска → усечение строк → конверт SPEC §5.1 →
        подгонка под `result_chars`.

        ВАЖНО: `filter` и `params` приходят сюда УЖЕ проведёнными через обратную подмену гейта —
        в них реальные значения, а не токены. Вызывающий отвечает за то, чтобы каждое значение,
        пришедшее от модели, прошло `inbound_filter`/`inbound_value` до этого места. Причина
        такого разделения — в том, что у `query` и `recipe` источники разные: у первого модель
        присылает целое выражение `$filter` (его разбирает лексер гейта), у второго — отдельные
        значения параметров при доверенном тексте условия из `recipes.yaml`.

        `extra` — поля конверта сверх общих (`recipe`, `title` у рецепта): добавляются ДО `items`,
        чтобы не мешать `fit_result` выбрасывать записи при подгонке под лимит.
        """
        раскрытия, обрезка = self._обрезать_expand(репозиторий, гейт, описание, _как_список(expand))
        spec = build_query(
            описание,
            describe=репозиторий.describe,
            limits=self._config.daemon.limits,
            virtual_timeout_s=base_config.virtual_timeout_s,
            filter=filter,
            select=select,
            expand=раскрытия,
            orderby=orderby,
            top=top,
            skip=skip,
            inlinecount=inlinecount,
            params=params,
            allowed_only=allowed_only,
        )

        клиент = self._client_for(base_config)
        try:
            сырой_ответ = await клиент.get(
                spec.path,
                spec.params,
                timeout=spec.timeout_s,
                scrub=гейт.scrubber(раскрытое),
            )
        except OdataError as ошибка:
            self._уточнить_404(ошибка)
            raise

        записи, всего = items_of(сырой_ответ)
        записи = strip_service(записи, keep_data_version="DataVersion" in _как_список(select))
        маска = гейт.mask(записи, entity=описание.name, resolve=self._навигации(репозиторий))
        усечённые, _ = truncate_strings(маска.data, self._config.daemon.limits.string_chars)

        конверт = {
            "entity": описание.name,
            "base": base_config.name,
            "role": base_config.role,
            "gate": гейт.mode,
            **(extra or {}),
            **page_info(count=len(записи), total=всего, top=spec.top, skip=spec.skip),
            "items": усечённые,
            "masked_fields": маска.masked_fields,
            "warnings": _без_повторов([*spec.warnings, *обрезка, *маска.warnings]),
        }
        return fit_result(конверт, self._config.daemon.limits.result_chars, skip=spec.skip)

    async def get(
        self,
        scope: SessionScope,
        *,
        base: str | None = None,
        entity: str,
        key,
        select: list[str] | str | None = None,
        expand: list[str] | str | None = None,
    ) -> str:
        async def тело(base_config, гейт, репозиторий, раскрытое):
            описание = self._resolve_entity(репозиторий, гейт, entity)
            реальный_ключ = гейт.inbound_key(key, entity=описание.name, revealed=раскрытое)

            раскрытия, обрезка = self._обрезать_expand(
                репозиторий, гейт, описание, _как_список(expand)
            )
            spec = build_get(
                описание,
                реальный_ключ,
                describe=репозиторий.describe,
                limits=self._config.daemon.limits,
                select=select,
                expand=раскрытия,
            )

            клиент = self._client_for(base_config)
            try:
                сырой_ответ = await клиент.get(
                    spec.path, spec.params, scrub=гейт.scrubber(раскрытое)
                )
            except OdataError as ошибка:
                self._уточнить_404(ошибка)
                raise

            записи, _ = items_of(сырой_ответ)
            записи = strip_service(записи, keep_data_version="DataVersion" in _как_список(select))
            маска = гейт.mask(записи, entity=описание.name, resolve=self._навигации(репозиторий))
            усечённые, _ = truncate_strings(маска.data, self._config.daemon.limits.string_chars)

            конверт = {
                "entity": описание.name,
                "base": base_config.name,
                "role": base_config.role,
                "gate": гейт.mode,
                "item": усечённые[0] if усечённые else None,
                "masked_fields": маска.masked_fields,
                "warnings": _без_повторов([*обрезка, *маска.warnings]),
            }
            return fit_result(конверт, self._config.daemon.limits.result_chars)

        return await self._run(scope, base, тело)

    # -- рецепты (SPEC §8, план M1d задача 8) ----------------------------------------------

    async def recipe(
        self,
        scope: SessionScope,
        *,
        base: str | None = None,
        name: str | None = None,
        params: dict | None = None,
    ) -> str:
        """Рецепт базы: без `name` — список рецептов с параметрами, с `name` — выполнить (SPEC §8).

        Рецепт — данные из `recipes.yaml`, а не код: из него берутся имя сущности, набор полей и
        текст условий, значения параметров попадают в запрос только литералами (`render`). Поэтому
        путь выполнения ничем не отличается от `query`: та же проверка сущности по индексу, тот же
        запрет сортировки по защищаемому полю, та же выборка и тот же конвейер ответа.

        Значения параметров приходят от модели и могут быть токенами гейта — каждое проходит
        `inbound_value` ДО рендеринга, с именем поля, с которым параметр сравнивается в условии
        (`Recipe.param_field`): от класса этого поля зависят и проверка типа токена, и контрольная
        сумма открытого значения. Раскрытые значения наружу не возвращаются: в конверт ответа
        попадают только имя рецепта и заголовок, но не то, с чем он был вызван.
        """

        async def тело(base_config, гейт, репозиторий, раскрытое):
            книга = self._книга_рецептов(base_config)
            if not name:
                return self._перечислить(base_config, гейт, книга)

            рецепт = книга.recipes.get(name) if книга is not None else None
            if рецепт is None:
                raise RecipeError(
                    "recipe_unknown",
                    f"рецепт «{name}» у базы «{base_config.name}» не описан",
                    self._подсказка_рецептов(книга),
                )

            описание = self._resolve_entity(репозиторий, гейт, рецепт.entity)
            self._проверить_сортировку(репозиторий, гейт, описание, рецепт.orderby)
            self._проверить_условия(гейт, рецепт, описание.name, params, раскрытое)
            значения = self._значения_рецепта(гейт, рецепт, описание.name, params, раскрытое)
            аргументы = render_recipe(рецепт, значения)

            return await self._выборка(
                base_config,
                гейт,
                репозиторий,
                описание,
                раскрытое,
                filter=аргументы.filter,
                select=аргументы.select,
                orderby=аргументы.orderby,
                top=аргументы.top,
                params=аргументы.params,
                extra={"recipe": name, "title": рецепт.title},
            )

        # Индекс открывает не `_run`, а сам перечень (`_перечислить`): на НЕПРОИНДЕКСИРОВАННОЙ
        # базе `_open_index` отказал бы кодом `entity_unknown` и вместо списка рецептов модель
        # получила бы «база не проиндексирована» — при том что список рецептов от индекса не
        # зависит, от него зависит только пометка применимости.
        return await self._run(scope, base, тело, with_index=bool(name))

    def _книга_рецептов(self, base: BaseConfig) -> RecipeBook | None:
        """Рецепты базы; `None` — файла нет вовсе (это не ошибка: рецепты необязательны).

        Файл читается на каждый вызов, без кэша по mtime: он маленький, вызовы редки, а правка
        рецепта должна действовать сразу, без перезапуска демона.
        """
        путь = recipes_path(self._config.home, base)
        if not путь.exists():
            return None
        return load_recipes(путь)

    def _проверить_условия(
        self,
        gate: BaseGate,
        recipe: Recipe,
        entity: str,
        params: dict | None,
        revealed: RevealedValues,
    ) -> None:
        """Условия рецепта с подставленными значениями — через тот же разбор `$filter`, что и
        отбор обычного `query` (Ruling 20, пункт 3, ревью 2026-09-11).

        До этой правки анти-оракульные правила жили в двух реализациях: разбор выражения в гейте
        и отдельный список в `BaseGate.inbound_param`. Списки разошлись — из пяти правил в
        рецепте работали три, и те не срабатывали на поле через связанный объект, — поэтому одно
        и то же условие (`substringof('Тверск', Контрагент/АдресДоставки)`) через `query`
        отклонялось, а через рецепт выполнялось. Ещё два правила (защищаемое поле только целиком;
        токен, смешанный с текстом) в рецепте не воспроизводились вовсе, и условие БЕЗ
        подстановки не проверялось ни одним из них — а его пишет человек, которому достаточно
        написать внешне безобидное «найти по части адреса доставки».

        Дублирование устранено, а не синхронизировано: правила остались там, где были, а рецепт
        отдаёт им своё условие целиком. Результат разбора отбрасывается — запрос собирает
        `render` (см. `recipes.render.probe`).
        """
        # Негодный тип `params` — забота `_значения_рецепта` (там отказ `recipe_param` с внятным
        # текстом); здесь просто нечего подставлять.
        if not isinstance(params, dict):
            params = {}
        сырые = {str(имя): значение for имя, значение in params.items()}
        результат, таблица = probe_recipe(recipe, сырые)
        for выражение in (результат, таблица):
            if выражение:
                gate.inbound_filter(выражение, entity=entity, revealed=revealed)

    def _значения_рецепта(
        self,
        gate: BaseGate,
        recipe: Recipe,
        entity: str,
        params: dict | None,
        revealed: RevealedValues,
    ) -> dict:
        """Значения параметров рецепта после обратной подмены (токен → реальное значение).

        Имя поля, с которым параметр сравнивается в условии (`Recipe.param_field`), передаётся
        гейту обязательно: от класса этого поля зависят и право раскрыть токен (Ruling 20,
        пункт 1: нет известного поля с тем же классом — нет раскрытия), и контрольная сумма
        открытого значения. Параметр, который ни с чем не сравнивается (`Period` и подобные, так
        устроены все шесть поставляемых рецептов УТ), приходит сюда с пустым именем поля — и
        токен в нём отклоняется, а не раскрывается «на всякий случай». Анти-оракульные правила
        здесь не повторяются: их применил разбор условия (`_проверить_условия`).

        Незаявленные имена проходят сюда нетронутыми — о них отказывает `render` кодом
        `recipe_param`; раскрывать токен для параметра, которого у рецепта нет, незачем, и отказ
        `token_unknown` вместо `recipe_param` только запутал бы вызывающего.
        """
        if params is None:
            return {}
        if not isinstance(params, dict):
            raise RecipeError(
                "recipe_param", "params рецепта должен быть словарём «параметр: значение»"
            )
        готовые = {}
        for сырое_имя, значение in params.items():
            имя = str(сырое_имя)
            if имя in recipe.params:
                значение = gate.inbound_value(
                    значение,
                    entity=entity,
                    field=recipe.param_field(имя),
                    revealed=revealed,
                )
            готовые[имя] = значение
        return готовые

    def _перечислить(self, base: BaseConfig, gate: BaseGate, book: RecipeBook | None) -> dict:
        """Перечень рецептов с индексом базы, если он есть: непроиндексированная база — не повод
        отказывать в списке, применимость в этом случае просто неизвестна."""
        try:
            репозиторий = self._open_index(base)
        except _ServiceError:
            репозиторий = None
        try:
            return self._список_рецептов(base, gate, репозиторий, book)
        finally:
            if репозиторий is not None:
                репозиторий.close()

    def _список_рецептов(
        self,
        base: BaseConfig,
        gate: BaseGate,
        repo: IndexRepository | None,
        book: RecipeBook | None,
    ) -> dict:
        """Перечень рецептов базы с параметрами и пометкой применимости (SPEC §8): сущность
        рецепта может отсутствовать в базе (шаблон УТ на базе БП), быть скрыта политикой или не
        быть виртуальной таблицей, хотя рецепт задаёт её параметры — такой рецепт помечается
        `applicable: false` с подсказкой, а не молча остаётся в списке наравне с рабочими.

        `repo is None` — база ещё не проиндексирована: сами рецепты видны (они лежат в файле, а
        не в индексе), но применимость неизвестна — `applicable: null` и подсказка про реиндекс.
        """
        конверт: dict = {
            "base": base.name,
            "role": base.role,
            "gate": gate.mode,
            "recipes": [],
        }
        if book is None:
            конверт["hint"] = (
                f"у базы «{base.name}» нет файла рецептов "
                f"({recipes_path(self._config.home, base)}); шаблон копируется командой "
                "odata1c base add --recipes ut|bp|zup"
            )
            return конверт

        for имя, рецепт in book.recipes.items():
            строка: dict = {
                "name": имя,
                "title": рецепт.title,
                "description": рецепт.description,
                "entity": рецепт.entity,
                "params": [
                    {
                        "name": имя_параметра,
                        "type": параметр.type,
                        "required": параметр.required,
                        "description": параметр.description,
                    }
                    for имя_параметра, параметр in рецепт.params.items()
                ],
                "applicable": True if repo is not None else None,
            }
            описание = (
                None
                if repo is None or gate.is_hidden(рецепт.entity)
                else repo.describe(рецепт.entity)
            )
            if repo is None:
                pass  # применимость неизвестна: индекса нет, сверять имя сущности не с чем
            elif gate.is_hidden(рецепт.entity):
                строка["applicable"] = False
                строка["hint"] = f"сущность {рецепт.entity} скрыта политикой гейта"
            elif описание is None:
                строка["applicable"] = False
                строка["hint"] = self._entity_hint(repo, gate, рецепт.entity)
            elif рецепт.virtual and not описание.is_virtual:
                # Иначе рецепт числился бы применимым, а при вызове отказывал бы `build_query`
                # («параметры виртуальной таблицы переданы для обычной сущности»): список должен
                # говорить о выполнимости правду, а не сверять одно только имя сущности.
                строка["applicable"] = False
                строка["hint"] = (
                    f"сущность {рецепт.entity} не виртуальная таблица регистра, а рецепт задаёт "
                    "её параметры (virtual)"
                )
            конверт["recipes"].append(строка)
        if not конверт["recipes"]:
            конверт["hint"] = "файл рецептов базы пуст"
        elif repo is None:
            конверт["hint"] = (
                f"база «{base.name}» не проиндексирована: применимость рецептов неизвестна — "
                "вызовите odata1c_reindex(base)"
            )
        return конверт

    @staticmethod
    def _подсказка_рецептов(book: RecipeBook | None) -> str:
        if book is None:
            return "у базы нет файла рецептов: odata1c base add --recipes ut|bp|zup"
        имена = ", ".join(book.recipes) or "ни одного"
        return f"рецепты базы: {имена}"

    async def resource_recipes(self, scope: SessionScope, base: str) -> str:
        """Ресурс `odata1c://recipes/{base}` — список рецептов базы в читаемом виде (SPEC §8).

        Тот же перечень, что и `odata1c_recipe` без имени, только markdown: ресурс читают, а не
        разбирают. Через страж (`finish_text`), как и политика: заголовки и описания пишет
        человек, и вписать туда он может что угодно.
        """

        async def тело(base_config, гейт, репозиторий, _раскрытое):
            return self._список_рецептов(
                base_config, гейт, репозиторий, self._книга_рецептов(base_config)
            )

        return await self._run(
            scope,
            base,
            тело,
            finisher=lambda гейт, конверт, раскрытое: гейт.finish_text(
                _рецепты_markdown(конверт), раскрытое
            ),
        )

    # -- реиндекс, справочник, аварийный GET, ресурсы (план M1d, задача 7) ------------------

    async def reindex(
        self, scope: SessionScope, *, base: str | None = None, force: bool = False
    ) -> str:
        """Обновить индекс метаданных базы и вернуть разницу (SPEC §4.3).

        При перестройке (`changed`) политика гейта пересобирается по новому индексу
        (`refresh_policy` — раздел `auto`, ручные разделы не трогаются), гейт этой базы
        перечитывает её ПРИНУДИТЕЛЬНО (`refresh(force=True)`: сверка mtime здесь неприменима —
        файл переписан секунду назад этим же вызовом), страж пересобирает автомат по словарю,
        а реестр запоминает момент индексации. Порядок обязателен: пока политика не перечитана,
        маскировщик знает прежние классы полей, и новое защищаемое поле ушло бы модели открытым.

        Известное узкое окно (M3 ревью 2026-09-11, не закрыто): файл индекса подменяется внутри
        `rebuild_index`, а политика пересобирается уже после возврата из него — между `os.replace`
        и `refresh_policy` лежит как минимум один переход планировщика asyncio, и параллельный
        `query` другой сессии успевает прочитать НОВЫЙ индекс со СТАРОЙ политикой. Утечки из
        этого не следует: политика только что была верна для прежнего состава полей, а поле,
        появившееся в новом индексе, в ответе сессии, отправившей запрос раньше, взяться не
        может. Закрыть окно дёшево не выходит — пришлось бы переносить пересборку политики внутрь
        замены файла, то есть смешивать слой индекса со слоем гейта; названо здесь, а не
        исправлено.
        """

        async def тело(base_config, гейт, _репозиторий, раскрытое):
            клиент = self._client_for(base_config)
            try:
                результат = await rebuild_index(
                    base_config,
                    клиент,
                    self._config.home,
                    force=force,
                    classifier=classifier_for(base_config),
                )
            except PermissionError as ошибка:
                # Windows: готовый индекс подменяется через `os.replace`, а файл, открытый
                # ЧИТАЮЩИМ вызовом другой сессии (`_run` держит его на время вызова), заменить
                # нельзя — WinError 32 (ERROR_SHARING_VIOLATION). Несколько сессий на одной
                # машине — условие продукта (AGENTS.md), так что это штатное совпадение, а не
                # поломка: без этой ветки общий перехват `_run` отдал бы `internal` с текстом
                # «подробности в журнале», по которому вызывающий не поймёт, что нужно просто
                # повторить вызов.
                #
                # Сверка кода Windows обязательна (M1 ревью 2026-09-11): `PermissionError`
                # бывает и от отсутствия прав на домашний каталог, и от каталога только для
                # чтения — «повторите через несколько секунд» на такое заставит владельца
                # повторять вызов бесконечно. Всё, кроме кода 32, уходит общим путём.
                if getattr(ошибка, "winerror", None) != 32:
                    raise
                raise _ServiceError(
                    "index_busy",
                    f"индекс базы «{base_config.name}» занят другой сессией и не заменён",
                    "повторите вызов через несколько секунд",
                ) from ошибка

            if результат.changed:
                refresh_policy(self._config.home, base_config)
                гейт.refresh(force=True)
                self._guard.rebuild()
                self._registry.set_indexed(
                    base_config.name, результат.indexed_at, результат.entity_count
                )

            предупреждения = list(результат.warnings)
            if результат.new_sensitive_fields:
                # Класс поля входит в подпись токена (`tokens.make_token`): после смены класса
                # то же значение приходит ДРУГИМ токеном. Ранее выданные токены работать не
                # перестают (`Dictionary.issued_for`, Ruling 19), но «у того же контрагента вдруг
                # другой токен» модель обязана понимать, а не считать ошибкой шлюза.
                предупреждения.append(
                    "классы полей изменились: то же значение теперь приходит другим токеном; "
                    "токены, выданные раньше, в filter и key по-прежнему работают"
                )
            if результат.unresolved_entity_sets:
                предупреждения.append(
                    f"наборов с испорченной ссылкой на тип: "
                    f"{len(результат.unresolved_entity_sets)} — признак повреждённого $metadata, "
                    "а не удалённых объектов"
                )
            конверт = {
                "base": base_config.name,
                "role": base_config.role,
                "gate": гейт.mode,
                "changed": результат.changed,
                "message": результат.message,
                "entity_count": результат.entity_count,
                "indexed_at": результат.indexed_at,
                "warnings": предупреждения,
            }
            for ключ, значения in (
                ("added_entities", результат.added_entities),
                ("removed_entities", результат.removed_entities),
                ("new_sensitive_fields", результат.new_sensitive_fields),
            ):
                конверт[ключ] = значения[:_ПРЕДЕЛ_РАЗНИЦЫ]
                конверт[f"{ключ}_total"] = len(значения)
            return конверт

        return await self._run(scope, base, тело, with_index=False)

    async def info(self, topic: str = "all") -> str:
        """Справочник по OData 1С (SPEC §5): готовый текст темы из `tools/info.py`.

        Единственный ответ тула, который НЕ проходит страж утечек, — и по той же причине, по
        которой его не проходит `bases()` (см. комментарий там): страж на строжайшем уровне
        портит текст, в котором нечего защищать. Здесь основание даже прямее, чем у `bases()`:
        ответ целиком собран из констант модуля, ни один байт данных 1С в него попасть физически
        не может — ни из ответа базы, ни из аргументов вызова (`topic` в успешный ответ не
        входит). Обратное — прогон констант через страж — не добавляет защиты, но даёт цифровым
        примерам справочника («2026-01-01», guid в примере ключа) шанс совпасть с чем-нибудь из
        словаря и превратиться в токен посреди объяснения.

        Ошибка (неизвестная тема) — наоборот, через страж (`_guard_error`): в ней повторяется
        аргумент вызова, то есть текст, пришедший снаружи.
        """
        текст = info_topics.render(topic)
        if текст is None:
            return self._guard_error(
                "params_invalid",
                f"неизвестная тема справочника: {topic}",
                "доступные темы: " + ", ".join(info_topics.TOPICS),
            )
        return текст

    async def raw_get(
        self,
        scope: SessionScope,
        *,
        base: str | None = None,
        path: str,
        query: dict | None = None,
    ) -> str:
        """Аварийный GET произвольного пути внутри `standard.odata/` (SPEC §5) — для случаев,
        когда `query`/`get` не выражают нужного обращения (нестандартная публикация, действие
        только для чтения, свежая сущность, которой ещё нет в индексе).

        «Сырой» здесь относится к ПУТИ, а не к ответу: ответ проходит ровно тот же конвейер, что
        и у `query` — очистка служебных полей, маска гейта, усечение строк, подгонка под лимит,
        страж (инвариант 1). Иначе тул был бы дырой в гейте, а не запасным входом.

        Путь, который не разрешается по индексу целиком, не отключает защиту, а усиливает её
        (Ruling 18, раунд правок 1): маска и проверка `$orderby` идут в строгом режиме, и об этом
        говорится в `warnings`. До этой правки было наоборот — неизвестная сущность оставалась без
        разметки полей, и названия организаций уходили модели открытым текстом.
        """

        async def тело(base_config, гейт, репозиторий, раскрытое):
            очищенный = _проверить_путь(path)
            цель = self._цель_пути(репозиторий, гейт, очищенный)
            параметры, обрезка = self._подготовить_параметры(
                репозиторий, гейт, query, цель, раскрытое
            )

            клиент = self._client_for(base_config)
            сырой = await клиент.get(очищенный, параметры or None, scrub=гейт.scrubber(раскрытое))
            if not isinstance(сырой, dict):
                # Путь здесь произвольный, и 1С отвечает не только объектом: `…/$count` отдаёт
                # число, примитивное свойство — скаляр. `items_of` ждёт словарь, и без этой
                # обёртки такой ответ уходил бы в общий перехват кодом `internal`.
                #
                # Скаляр кладётся СТРОКОЙ (пункт 5 дополнения): завернув число в конверт, эта
                # обёртка обходила специальную ветку стража под ровно такой ответ — внутри
                # JSON-документа числовые литералы он пропускает сознательно, — и реальный ИНН
                # выходил открытым. См. `_скаляром`.
                сырой = {"value": _скаляром(сырой)}

            список = isinstance(сырой.get("value"), list)
            записи, всего = items_of(сырой)
            записи = strip_service(
                записи, keep_data_version="DataVersion" in параметры.get("$select", "")
            )
            записи = _восстановить_имя_поля(записи, цель.field)
            маска = гейт.mask(
                записи,
                entity=цель.entity,
                resolve=self._навигации(репозиторий),
                strict=not цель.resolved,
            )
            усечённые, _ = truncate_strings(маска.data, self._config.daemon.limits.string_chars)

            предупреждения = _без_повторов([*обрезка, *маска.warnings])
            if not цель.resolved:
                предупреждения.append(_ПРЕДУПРЕЖДЕНИЕ_ВНЕ_ИНДЕКСА)

            конверт = {
                "path": очищенный,
                "entity": цель.entity,
                "base": base_config.name,
                "role": base_config.role,
                "gate": гейт.mode,
                "masked_fields": маска.masked_fields,
                "warnings": предупреждения,
            }
            if список:
                смещение = _целое_или_ноль(параметры.get("$skip"))
                верх = (
                    _целое_или_ноль(параметры.get("$top")) or self._config.daemon.limits.top_default
                )
                конверт.update(
                    page_info(count=len(усечённые), total=всего, top=верх, skip=смещение)
                )
                конверт["items"] = усечённые
                return fit_result(конверт, self._config.daemon.limits.result_chars, skip=смещение)
            конверт["item"] = усечённые[0] if усечённые else None
            return fit_result(конверт, self._config.daemon.limits.result_chars)

        return await self._run(scope, base, тело)

    async def resource_policy(self, scope: SessionScope, base: str) -> str:
        """Ресурс `odata1c://policy/{base}` — текст `policy.yaml` базы через страж.

        В политике только имена сущностей и полей с классами защиты, самих значений там нет, —
        поэтому её можно показать модели целиком: это и есть ответ на вопрос «почему это поле
        пришло токеном, а это нет». Страж всё равно последним проходом по тексту (инвариант 1):
        файл правит человек, и в комментарий к правилу он может вписать что угодно.

        `base` обязателен и проверяется по области видимости сессии (`_run` → `Registry.get`):
        сессия, суженная заголовком `X-Odata1c-Bases`, не должна читать политику чужой базы.
        """

        async def тело(base_config, _гейт, _репозиторий, _раскрытое):
            путь = policy_path(self._config.home, base_config.name)
            if not путь.exists():
                raise _ServiceError(
                    "entity_unknown",
                    f"политика базы «{base_config.name}» ещё не создана",
                    "она собирается при первом реиндексе: odata1c_reindex(base)",
                )
            return путь.read_text(encoding="utf-8")

        return await self._run(
            scope,
            base,
            тело,
            finisher=lambda гейт, текст, раскрытое: гейт.finish_text(текст, раскрытое),
            with_index=False,
        )

    async def resource_index(self, scope: SessionScope, base: str) -> str:
        """Ресурс `odata1c://index/{base}` — сводка индекса: когда построен, сколько сущностей
        всего и по видам.

        Как и `bases()`, НЕ проходит страж: в сводке только локальные данные — отметка времени
        индексации и числа. Отметка времени — строка сплошных цифр, и страж на строжайшем уровне
        ищет в таких сериях известные словарю числа (именно так `bases()` однажды превратил
        цифры в подписи базы в токен чужого ИНН), а защищать в ней нечего.
        """

        async def тело(base_config, гейт, репозиторий, _раскрытое):
            число = репозиторий.meta("entity_count")
            return {
                "base": base_config.name,
                "role": base_config.role,
                "gate": гейт.mode,
                "indexed_at": репозиторий.meta("indexed_at"),
                "entity_count": int(число) if число is not None else None,
                "kinds": репозиторий.kind_counts(),
            }

        return await self._run(
            scope,
            base,
            тело,
            finisher=lambda _гейт, конверт, _раскрытое: json.dumps(конверт, ensure_ascii=False),
        )

    # -- разбор аргументов raw_get ----------------------------------------------------------

    def _цель_пути(self, repo: IndexRepository, gate: BaseGate, path: str) -> _ЦельПути:
        """Сущность, по классам полей которой маскируется ответ `raw_get`, признак «путь разрешён
        индексом целиком» и имя реквизита из хвоста пути. Плюс проверка скрытости.

        Первый сегмент пути — не всегда та сущность, чьи записи вернутся: путь
        `Catalog_Договоры(guid'…')/Владелец` отдаёт запись КОНТРАГЕНТА, и маска по классам
        `Catalog_Договоры` на ней не сработает — `Description` контрагента не отнесён там к
        классу `org`, то есть название организации ушло бы открытым (бриф задачи говорит про
        первый сегмент; расхождение подтверждено ревью как более строгое). Поэтому сегменты
        проходятся по навигациям индекса, и цель — последняя РАЗРЕШЁННАЯ сущность цепочки.

        Нерешённый сегмент больше не «останавливает разбор и маскирует по последней известной»
        (Critical ревью 2026-09-11): такой ответ уходил модели с открытыми названиями. Теперь он
        снимает признак `resolved`, и дальше работает строгий режим маскировки (Ruling 18). Сам
        неразрешённый хвост возвращается отдельно: для ответа на примитивное свойство
        (`…(guid'…')/ИНН` → `{"value": …}`) это единственное место, где вообще есть имя поля.

        Скрытость (`entities.hide` политики) проверяется на КАЖДОМ звене цепочки: иначе
        `raw_get` стал бы обходом запрета, который `query`/`get` соблюдают, — а скрытая сущность
        скрыта и через навигацию тоже. Звено, которого индекс не знает, проверить нечем: имя
        сущности за ним неизвестно, а само имя набора шлюз отправляет ДОСЛОВНО и канонизацию
        видит только 1С за IIS (завершающая точка, невидимый пробел, комбинирующее ударение —
        до 1С дойдёт каноническое имя, а запрет владельца шлюз на нём не узнает). Ruling 21
        дополнения: не гоняться за нормализациями, а отказывать — но только там, где владельцу
        есть что скрывать (`Policy.has_hidden`). Где правил `hide` нет, неизвестное имя остаётся
        штатным сценарием `raw_get` и работает со строгой политикой маскировки (Ruling 18).
        """
        сегменты = [сегмент.split("(")[0] for сегмент in path.split("/") if сегмент]
        # Имя набора приводится к каноническому ДО проверки скрытости (форма (д) ревью
        # 2026-09-11): иначе запрет, выписанный на `Catalog_Контрагенты`, обходится одной сменой
        # регистра буквы, если публикация 1С к регистру нечувствительна.
        цель = repo.resolve_name(сегменты[0]) or сегменты[0]
        for имя in {цель, сегменты[0]}:
            if gate.is_hidden(имя):
                raise _ServiceError("entity_hidden", f"сущность «{имя}» скрыта политикой гейта")
        текущая = repo.describe(цель)
        # Набор, которого нет в индексе, — это уже неразрешённый путь: политика о нём молчит,
        # и раньше именно этот случай (устаревший индекс — заявленный сценарий самого raw_get)
        # отдавал названия открытым текстом.
        разрешён = текущая is not None
        неразрешённые: list[str] = [] if разрешён else [сегменты[0]]
        for сегмент in сегменты[1:]:
            следующая = текущая.navigations.get(сегмент) if текущая is not None else None
            if следующая is None:
                разрешён, текущая = False, None
                неразрешённые.append(сегмент)
                continue
            if gate.is_hidden(следующая):
                raise _ServiceError(
                    "entity_hidden", f"сущность «{следующая}» скрыта политикой гейта"
                )
            цель, текущая = следующая, repo.describe(следующая)
            if текущая is None:
                разрешён = False
                неразрешённые.append(сегмент)

        if неразрешённые and gate.has_hidden_entities():
            raise _ServiceError(
                "entity_unknown",
                f"сегмент пути «{неразрешённые[0]}» не разрешается по индексу базы",
                "обновите индекс: odata1c_reindex — на базе со скрытыми сущностями "
                "неразрешённый путь не обслуживается",
            )
        if len(неразрешённые) > 1:
            # Больше одного неразрешённого сегмента — имя реквизита по хвосту не восстановить,
            # а угадывать нельзя: на пути `Catalog_A(guid)/НеизвестнаяНавигация/Поле` первым
            # неразрешённым окажется имя навигации, и переименование даст не то имя (пункт 5
            # дополнения). Отказ вместо угадывания.
            raise _ServiceError(
                "entity_unknown",
                f"путь не разрешается по индексу: неизвестные сегменты "
                f"«{'», «'.join(неразрешённые)}»",
                "обновите индекс: odata1c_reindex, либо проверьте путь",
            )
        # ПЕРВЫЙ неразрешённый сегмент: имя реквизита стоит сразу после последней разрешённой
        # сущности (пункт 5 дополнения; прежний «последний» вытеснялся любым продолжением пути).
        # После отказа выше здесь остаётся ровно один неразрешённый сегмент, то есть первый и
        # последний совпадают — выбор `[0]` выражает намерение, а защиту несёт сам отказ. Снимете
        # отказ — эта строка снова станет наблюдаемой, и её придётся закрыть своим тестом.
        хвост = неразрешённые[0] if len(неразрешённые) == 1 else None
        # Сущность вне индекса (единственный неразрешённый сегмент — сам набор) именем реквизита
        # не бывает: переименовывать в неё нечего, и `resolved=False` уже включил строгий режим.
        if хвост is not None and (хвост in _СИСТЕМНЫЕ_СЕГМЕНТЫ or хвост == сегменты[0]):
            хвост = None
        return _ЦельПути(entity=цель, resolved=разрешён, field=хвост)

    def _подготовить_параметры(
        self,
        repo: IndexRepository,
        gate: BaseGate,
        query: dict | None,
        цель: _ЦельПути,
        revealed: RevealedValues,
    ) -> tuple[dict[str, str], list[str]]:
        """Параметры запроса `raw_get`: проверка, приведение к строкам, обратная подмена токенов,
        обрезка `$expand` по политике. Возвращает пару «параметры, предупреждения».

        Токен разворачивается в реальное значение только в `$filter` (`gate.inbound_filter`) —
        там гейт знает синтаксис и разбирает выражение лексером. В остальных параметрах стоят
        имена полей, а не значения, поэтому токен в них — либо ошибка модели, либо попытка
        протащить значение мимо разбора; и то и другое отклоняется, а не подставляется вслепую.

        На неразрешённом пути `$filter` разбирается в СТРОГОМ режиме — том же, в котором идёт
        маска (Ruling 18, пункт 7 дополнения: «любые другие параметры, которые на разрешённом
        пути отклоняются как оракул, на неразрешённом отклоняются тем более»). Строгим был
        сделан только `$orderby`, и асимметрия была рабочей: `Description ge 'М'` на известной
        сущности отклонялся, а на сущности вне индекса уходил в 1С — двоичный поиск по названию.
        """
        if query is None:
            return {}, []
        if not isinstance(query, dict):
            raise _ServiceError("params_invalid", "query должен быть словарём «параметр: значение»")

        готовые: dict[str, str] = {}
        for сырое_имя, значение in query.items():
            имя = str(сырое_имя)
            if имя == "$format":
                raise _ServiceError(
                    "params_invalid",
                    "формат ответа задаёт сам шлюз",
                    "уберите $format — ответ всегда разбирается как JSON",
                )
            if isinstance(значение, bool):
                текст = "true" if значение else "false"
            elif isinstance(значение, list | tuple):
                # `query` принимает `select`/`expand` и списком, и строкой через запятую
                # (`odata_query._к_списку`); у `raw_get` список отклонялся как «не строка и не
                # число», хотя модель приходит сюда с той же привычкой (M2 ревью 2026-09-11).
                текст = ",".join(
                    str(элемент).strip() for элемент in значение if str(элемент).strip()
                )
            elif isinstance(значение, str | int | float):
                текст = str(значение)
            else:
                raise _ServiceError(
                    "params_invalid",
                    f"значение параметра {имя} должно быть строкой, числом или списком строк",
                )
            if имя == "$filter":
                текст = gate.inbound_filter(
                    текст, entity=цель.entity, strict=not цель.resolved, revealed=revealed
                )
            elif "[[" in текст:
                raise _ServiceError(
                    "params_invalid",
                    f"токен в параметре {имя} подставлен быть не может",
                    "токены разворачиваются только в $filter; в остальных параметрах стоят имена "
                    "полей",
                )
            готовые[имя] = текст

        сортировка = готовые.get("$orderby")
        if сортировка:
            # На неразрешённом пути проверка идёт в том же строгом режиме, что и маска
            # (Ruling 18): иначе запрет на оракул порядка снимался бы ровно там, где снимается
            # защита названий, — форма (г) ревью 2026-09-11.
            описание = repo.describe(цель.entity) if цель.resolved else None
            for поле in orderby_fields(сортировка):
                защищено = (
                    self._поле_защищено_по_пути(repo, gate, описание, поле)
                    if описание is not None
                    else gate.is_protected(
                        цель.entity, поле.split("/")[-1], strict=not цель.resolved
                    )
                )
                if защищено:
                    raise _ServiceError(
                        "params_invalid",
                        "сортировка по защищаемому полю недоступна: порядок раскрывает значения",
                    )

        обрезка = self._проверить_expand_сырого(repo, gate, цель, готовые)
        return готовые, обрезка

    def _проверить_expand_сырого(
        self,
        repo: IndexRepository,
        gate: BaseGate,
        цель: _ЦельПути,
        готовые: dict[str, str],
    ) -> list[str]:
        """`$expand` у `raw_get`: лимит глубины и обрезка скрытых целей (итоговое ревью M1d, C2).

        `raw_get` копировал параметр в запрос дословно — то есть мимо обеих проверок, которые
        `query`/`get` проходят через построитель запроса. Это ровно та «вторая дверь к той же
        структуре», про которую записан урок этапа: покрытия по классам мало, нужно покрытие по
        путям, которыми данные приходят.

        Глубина — отказ (как у построителя запроса: лимит владельца, а не политика гейта, и
        молчаливое урезание пути сделало бы ответ не тем, о чём просили); скрытая цель —
        обрезка (см. `_обрезать_expand`)."""
        сырой = готовые.get("$expand")
        if not сырой:
            return []
        пути = [часть.strip() for часть in сырой.split(",") if часть.strip()]
        предел = self._config.daemon.limits.expand_depth
        for путь in пути:
            if len(путь.split("/")) > предел:
                raise _ServiceError(
                    "params_invalid",
                    f"$expand «{путь}» глубже лимита {предел}",
                    f"сократите путь раскрытия до {предел} сегментов",
                )
        описание = repo.describe(цель.entity) if цель.resolved else None
        оставленные, предупреждения = self._обрезать_expand(repo, gate, описание, пути)
        if оставленные:
            готовые["$expand"] = ",".join(оставленные)
        else:
            готовые.pop("$expand")
        return предупреждения

    @staticmethod
    def _уточнить_404(ошибка: OdataError) -> None:
        """SPEC §4.3: ответ 1С 404 на сущность, которая есть в индексе — подсказка про reindex,
        а не универсальная подсказка `map_error` («если сущность точно есть...»), звучащая как
        сомнение в том, что мы сами уже проверили по индексу до обращения к 1С."""
        if ошибка.code == "entity_unknown":
            ошибка.hint = "структура базы могла измениться: вызовите odata1c_reindex"
