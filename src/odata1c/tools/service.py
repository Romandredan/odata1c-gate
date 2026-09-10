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

import json
import logging

from odata1c.client1c.client import Client1C
from odata1c.client1c.errors import OdataError
from odata1c.config.loader import ConfigError
from odata1c.config.models import AppConfig, BaseConfig
from odata1c.gate.dictionary import Dictionary
from odata1c.gate.guard import Guard
from odata1c.gate.pipeline import BaseGate, guard_only
from odata1c.gate.policy import PolicyError
from odata1c.gate.service import open_dictionary, policy_path
from odata1c.gate.unmasking import GateError
from odata1c.index.reindex import index_path
from odata1c.index.repository import (
    EntityDescription,
    FoundEntity,
    IndexCorruptError,
    IndexRepository,
)
from odata1c.registry.registry import Registry, SessionScope
from odata1c.tools import describe as describe_tool
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


def _как_список(значение: list[str] | str | None) -> list[str]:
    """Список полей `select`/`orderby`: строка с запятыми или готовый список — тот же разбор
    аргумента тула, что и в `odata_query._к_списку`, но не оттуда: та функция приватная, а нужна
    здесь только для одной проверки (`DataVersion` явно запрошен явным `select`)."""
    if значение is None:
        return []
    сырые = значение.split(",") if isinstance(значение, str) else list(значение)
    return [элемент.strip() for элемент in сырые if элемент and элемент.strip()]


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

    def _safe_error(self, gate: BaseGate, code: str, message: str, hint: str = "") -> str:
        try:
            return gate.error(code, message, hint)
        except Exception:
            _log.exception("gate.error упал при обработке ошибки тула — отдан отказ без данных")
            return _ОТКАЗ_НА_КРАЙНИЙ_СЛУЧАЙ

    def _guard_error(self, code: str, message: str, hint: str = "") -> str:
        """Ошибка тула БЕЗ гейта конкретной базы: либо база ещё не определена, либо гейт этой
        базы не удалось построить (см. `_run`) — в обоих случаях `gate.error`/`_safe_error`
        вызвать нечем, страж живёт на сервисе независимо от гейта (SPEC §6.8)."""
        return guard_only(self._guard, {"error": {"code": code, "message": message, "hint": hint}})

    async def _run(self, scope: SessionScope, base: str | None, body, *, finisher=None) -> str:
        """Общая точка входа тулов на одну базу: разрешить базу → получить/обновить гейт →
        открыть индекс на время вызова → выполнить `body(base_config, gate, repo)` → отдать
        результат через `finisher` (по умолчанию `gate.finish`). Любое ожидаемое исключение —
        код ошибки тула через гейт; неожиданное — `internal` без текста исключения."""
        итог = finisher or (lambda gate, result: gate.finish(result))
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
            репозиторий = self._open_index(base_config)
            результат = await body(base_config, гейт, репозиторий)
            return итог(гейт, результат)
        except (
            OdataError,
            GateError,
            QueryError,
            IndexCorruptError,
            PolicyError,
            _ServiceError,
        ) as ошибка:
            return self._safe_error(гейт, ошибка.code, str(ошибка), getattr(ошибка, "hint", ""))
        except Exception:
            _log.exception("внутренняя ошибка тула odata1c — детали в журнале демона")
            return self._safe_error(
                гейт, "internal", "внутренняя ошибка шлюза, подробности в журнале демона"
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

        конверт: dict = {
            "bases": строки,
            "default": scope.default or self._config.default,
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
        async def тело(base_config, гейт, репозиторий):
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
        async def тело(base_config, гейт, репозиторий):
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

        def итог(гейт: BaseGate, факты: dict) -> str:
            if response_format == "markdown":
                return гейт.finish_text(describe_tool.render_markdown(факты))
            return гейт.finish(факты)

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
        async def тело(base_config, гейт, репозиторий):
            описание = self._resolve_entity(репозиторий, гейт, entity)

            if orderby:
                for поле in orderby_fields(orderby):
                    if self._поле_защищено_по_пути(репозиторий, гейт, описание, поле):
                        raise _ServiceError(
                            "params_invalid",
                            "сортировка по защищаемому полю недоступна: порядок раскрывает "
                            "значения",
                        )

            реальный_filter = (
                гейт.inbound_filter(filter, entity=описание.name) if filter else filter
            )
            реальные_params = params
            if описание.is_virtual and params and "Condition" in params:
                реальные_params = dict(params)
                реальные_params["Condition"] = гейт.inbound_filter(
                    params["Condition"], entity=описание.name
                )

            spec = build_query(
                описание,
                describe=репозиторий.describe,
                limits=self._config.daemon.limits,
                virtual_timeout_s=base_config.virtual_timeout_s,
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

            клиент = self._client_for(base_config)
            try:
                сырой_ответ = await клиент.get(spec.path, spec.params, timeout=spec.timeout_s)
            except OdataError as ошибка:
                self._уточнить_404(ошибка)
                raise

            записи, всего = items_of(сырой_ответ)
            записи = strip_service(записи, keep_data_version="DataVersion" in _как_список(select))
            маска = гейт.mask(записи, entity=описание.name)
            усечённые, _ = truncate_strings(маска.data, self._config.daemon.limits.string_chars)

            конверт = {
                "entity": описание.name,
                "base": base_config.name,
                "role": base_config.role,
                "gate": гейт.mode,
                **page_info(count=len(записи), total=всего, top=spec.top, skip=spec.skip),
                "items": усечённые,
                "masked_fields": маска.masked_fields,
                "warnings": [*spec.warnings, *маска.warnings],
            }
            return fit_result(конверт, self._config.daemon.limits.result_chars, skip=spec.skip)

        return await self._run(scope, base, тело)

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
        async def тело(base_config, гейт, репозиторий):
            описание = self._resolve_entity(репозиторий, гейт, entity)
            реальный_ключ = гейт.inbound_key(key, entity=описание.name)

            spec = build_get(
                описание,
                реальный_ключ,
                describe=репозиторий.describe,
                limits=self._config.daemon.limits,
                select=select,
                expand=expand,
            )

            клиент = self._client_for(base_config)
            try:
                сырой_ответ = await клиент.get(spec.path, spec.params)
            except OdataError as ошибка:
                self._уточнить_404(ошибка)
                raise

            записи, _ = items_of(сырой_ответ)
            записи = strip_service(записи, keep_data_version="DataVersion" in _как_список(select))
            маска = гейт.mask(записи, entity=описание.name)
            усечённые, _ = truncate_strings(маска.data, self._config.daemon.limits.string_chars)

            конверт = {
                "entity": описание.name,
                "base": base_config.name,
                "role": base_config.role,
                "gate": гейт.mode,
                "item": усечённые[0] if усечённые else None,
                "masked_fields": маска.masked_fields,
                "warnings": маска.warnings,
            }
            return fit_result(конверт, self._config.daemon.limits.result_chars)

        return await self._run(scope, base, тело)

    @staticmethod
    def _уточнить_404(ошибка: OdataError) -> None:
        """SPEC §4.3: ответ 1С 404 на сущность, которая есть в индексе — подсказка про reindex,
        а не универсальная подсказка `map_error` («если сущность точно есть...»), звучащая как
        сомнение в том, что мы сами уже проверили по индексу до обращения к 1С."""
        if ошибка.code == "entity_unknown":
            ошибка.hint = "структура базы могла измениться: вызовите odata1c_reindex"
