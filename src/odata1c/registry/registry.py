"""Реестр баз: что видит сессия, какая база по умолчанию, состояние индекса (SPEC §2.2)."""

from __future__ import annotations

import dataclasses

from odata1c.config.loader import ConfigError
from odata1c.config.models import AppConfig, BaseConfig


class UnknownBase(ConfigError):
    def __init__(
        self,
        name: str | None,
        известные: list[str],
        is_default: bool = False,
        has_any_bases: bool = True,
    ) -> None:
        if is_default:
            message = "база по умолчанию недоступна в текущей сессии"
        elif name is None:
            message = "база не указана и нет базы по умолчанию"
        elif name == "":
            message = "база указана, но имя пусто"
        else:
            # Ruling 53: имя от модели не повторяется — отказ идёт через страж (`guard_only`),
            # а тот заменил бы известное значение в имени токеном; доступные — в подсказке.
            message = "база с таким именем неизвестна"

        if not известные:
            if has_any_bases:
                hint = "видимость ограничена аргументами запуска, нет доступных баз"
            else:
                hint = "ни одной базы, опишите их в bases.yaml"
        else:
            hint = f"доступные базы: {', '.join(известные)}"

        super().__init__(message, code="base_unknown", hint=hint)


@dataclasses.dataclass(slots=True)
class SessionScope:
    """Сужение видимости для сессии: аргументы --bases и --default лаунчера (SPEC §2.1)."""

    bases: tuple[str, ...] | None = None
    default: str | None = None


@dataclasses.dataclass(slots=True)
class BaseState:
    name: str
    label: str
    role: str
    gate_mode: str
    write: bool
    indexed: bool = False
    indexed_at: str | None = None
    entity_count: int | None = None
    last_error: str | None = None


class Registry:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._state: dict[str, BaseState] = {}
        self._пересобрать_состояние()

    def replace_config(self, config: AppConfig) -> None:
        """Принять перечитанный `bases.yaml` (SPEC §3.1, поправка 2026-09-14, ADR-0015): состав
        баз и их настройки — из нового файла, состояние индексации баз, оставшихся под тем же
        именем, — прежнее.

        Состояние (`indexed`, `indexed_at`, `entity_count`, `last_error`) в `bases.yaml` не
        описано: его наполняют реиндекс и фоновая проверка `$metadata`, и правка файла настроек —
        не повод объявить проиндексированную базу непроиндексированной. У базы, которой в новом
        файле нет, состояние уходит вместе с ней: вернись она позже, прежние отметки относились бы
        к записи, которой в файле уже не было, — в том числе к другому адресу.
        """
        self._config = config
        self._пересобрать_состояние()

    def _пересобрать_состояние(self) -> None:
        прежние = self._state
        self._state = {}
        for name, base in self._config.bases.items():
            было = прежние.get(name)
            self._state[name] = BaseState(
                name=name,
                label=base.label,
                role=base.role,
                gate_mode=base.gate.mode,
                write=base.write,
                indexed=было.indexed if было else False,
                indexed_at=было.indexed_at if было else None,
                entity_count=было.entity_count if было else None,
                last_error=было.last_error if было else None,
            )

    def visible(self, session: SessionScope) -> list[BaseState]:
        # Копии, а не живые объекты: BaseState общий для всех сессий, читающих реестр.
        # Пока в нём только диагностика (последняя ошибка, статус индекса), правка снаружи
        # безобидна, но со следующим этапом сюда добавится политика замены — общая ссылка
        # тогда станет источником трудноуловимых ошибок между сессиями.
        names = self._visible_names(session)
        return [dataclasses.replace(self._state[name]) for name in names]

    def get(self, name: str | None, session: SessionScope) -> BaseConfig:
        names = self._visible_names(session)
        is_default_used = False
        if name is None:
            session_default = session.default or self._config.default
            if session_default is None:
                raise UnknownBase(
                    None,
                    names,
                    is_default=False,
                    has_any_bases=bool(self._config.bases),
                )
            name = session_default
            is_default_used = True

        if not name or name not in names:
            raise UnknownBase(
                name,
                names,
                is_default=is_default_used,
                has_any_bases=bool(self._config.bases),
            )
        return self._config.bases[name]

    def set_error(self, name: str, message: str) -> None:
        try:
            self._state[name].last_error = message
        except KeyError as err:
            raise UnknownBase(
                name,
                sorted(self._config.bases),
                is_default=False,
                has_any_bases=bool(self._config.bases),
            ) from err

    def set_indexed(self, name: str, indexed_at: str, entity_count: int) -> None:
        try:
            state = self._state[name]
        except KeyError as err:
            raise UnknownBase(
                name,
                sorted(self._config.bases),
                is_default=False,
                has_any_bases=bool(self._config.bases),
            ) from err
        state.indexed, state.indexed_at, state.entity_count = True, indexed_at, entity_count
        state.last_error = None

    def _visible_names(self, session: SessionScope) -> list[str]:
        if session.bases is None:
            return sorted(self._config.bases)
        return sorted(name for name in session.bases if name in self._config.bases)
