"""Реестр баз: что видит сессия, какая база по умолчанию, состояние индекса (SPEC §2.2)."""

from __future__ import annotations

import dataclasses

from odata1c.config.loader import ConfigError
from odata1c.config.models import AppConfig, BaseConfig


class UnknownBase(ConfigError):
    def __init__(self, name: str | None, известные: list[str]) -> None:
        super().__init__(
            f"база «{name}» неизвестна" if name else "база не указана и нет базы по умолчанию",
            code="base_unknown",
            hint=f"доступные базы: {', '.join(известные) or 'ни одной, опишите их в bases.yaml'}",
        )


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
        self._state: dict[str, BaseState] = {
            name: BaseState(
                name=name,
                label=base.label,
                role=base.role,
                gate_mode=base.gate.mode,
                write=base.write,
            )
            for name, base in config.bases.items()
        }

    def visible(self, session: SessionScope) -> list[BaseState]:
        names = self._visible_names(session)
        return [self._state[name] for name in names]

    def get(self, name: str | None, session: SessionScope) -> BaseConfig:
        names = self._visible_names(session)
        if name is None:
            name = session.default or self._config.default
            if name is None or name not in names:
                raise UnknownBase(None, names)
        if name not in names:
            raise UnknownBase(name, names)
        return self._config.bases[name]

    def set_error(self, name: str, message: str) -> None:
        self._state[name].last_error = message

    def set_indexed(self, name: str, indexed_at: str, entity_count: int) -> None:
        state = self._state[name]
        state.indexed, state.indexed_at, state.entity_count = True, indexed_at, entity_count
        state.last_error = None

    def _visible_names(self, session: SessionScope) -> list[str]:
        if session.bases is None:
            return sorted(self._config.bases)
        return sorted(name for name in session.bases if name in self._config.bases)
