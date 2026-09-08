"""Реестр баз: видимость по сессии, база по умолчанию, состояние."""

import pytest

from odata1c.config.models import AppConfig, BaseConfig, DaemonConfig
from odata1c.registry.registry import Registry, SessionScope, UnknownBase


def собрать(*names: str) -> AppConfig:
    bases = {
        name: BaseConfig(
            name=name,
            label=f"база {name}",
            url=f"http://localhost/{name}/odata/standard.odata/",
            user="u",
            password="p",
            role="prod",
        )
        for name in names
    }
    return AppConfig(
        home=".",
        default=names[0] if names else None,
        bases=bases,
        daemon=DaemonConfig(gate_secret="x" * 44),
    )


def test_без_сужения_видны_все_базы():
    registry = Registry(собрать("ut", "buh"))
    видимые = registry.visible(SessionScope(bases=None, default=None))
    assert {b.name for b in видимые} == {"ut", "buh"}


def test_сужение_списком_баз():
    registry = Registry(собрать("ut", "buh", "zup"))
    видимые = registry.visible(SessionScope(bases=("ut", "zup"), default=None))
    assert {b.name for b in видимые} == {"ut", "zup"}


def test_база_по_умолчанию_из_сессии_главнее_общей():
    registry = Registry(собрать("ut", "buh"))
    scope = SessionScope(bases=("ut", "buh"), default="buh")
    assert registry.get(None, scope).name == "buh"


def test_база_по_умолчанию_из_настроек_если_сессия_не_задала():
    registry = Registry(собрать("ut", "buh"))
    assert registry.get(None, SessionScope(bases=None, default=None)).name == "ut"


def test_скрытая_от_сессии_база_неизвестна():
    registry = Registry(собрать("ut", "buh"))
    with pytest.raises(UnknownBase) as ошибка:
        registry.get("buh", SessionScope(bases=("ut",), default=None))
    assert ошибка.value.code == "base_unknown"


def test_состояние_обновляется():
    registry = Registry(собрать("ut"))
    registry.set_error("ut", "не отвечает")
    assert registry.visible(SessionScope(None, None))[0].last_error == "не отвечает"
    registry.set_indexed("ut", "2026-09-07T10:00:00", 1200)
    состояние = registry.visible(SessionScope(None, None))[0]
    assert состояние.indexed is True
    assert состояние.entity_count == 1200
    assert состояние.last_error is None
