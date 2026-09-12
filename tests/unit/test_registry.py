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
    assert "buh" not in ошибка.value.hint


def test_состояние_обновляется():
    registry = Registry(собрать("ut"))
    registry.set_error("ut", "не отвечает")
    assert registry.visible(SessionScope(None, None))[0].last_error == "не отвечает"
    registry.set_indexed("ut", "2026-09-07T10:00:00", 1200)
    состояние = registry.visible(SessionScope(None, None))[0]
    assert состояние.indexed is True
    assert состояние.entity_count == 1200
    assert состояние.last_error is None


def test_пустой_список_видимых_баз():
    registry = Registry(собрать("ut", "buh"))
    видимые = registry.visible(SessionScope(bases=("unknown",), default=None))
    assert видимые == []


def test_список_видимости_отбрасывает_несуществующие_имена():
    registry = Registry(собрать("ut", "buh"))
    видимые = registry.visible(SessionScope(bases=("ut", "unknown", "buh"), default=None))
    assert {b.name for b in видимые} == {"ut", "buh"}


def test_сужение_видимости_до_несуществующей_базы():
    registry = Registry(собрать("ut", "buh"))
    with pytest.raises(UnknownBase) as ошибка:
        registry.get("unknown", SessionScope(bases=("unknown",), default=None))
    assert ошибка.value.code == "base_unknown"
    # Ruling 53: имя, которое прислала модель, в тексте не повторяется.
    assert "unknown" not in str(ошибка.value) and "неизвестна" in str(ошибка.value)


def test_обращение_с_пустой_строкой_вместо_имени():
    registry = Registry(собрать("ut", "buh"))
    with pytest.raises(UnknownBase) as ошибка:
        registry.get("", SessionScope(bases=("ut", "buh"), default=None))
    assert ошибка.value.code == "base_unknown"
    assert "пусто" in str(ошибка.value)


def test_база_по_умолчанию_скрытая_от_сессии():
    registry = Registry(собрать("ut", "buh"))
    with pytest.raises(UnknownBase) as ошибка:
        registry.get(None, SessionScope(bases=("buh",), default=None))
    assert ошибка.value.code == "base_unknown"
    assert "по умолчанию" in str(ошибка.value)


def test_обновление_состояния_для_неизвестного_имени():
    registry = Registry(собрать("ut"))
    with pytest.raises(UnknownBase) as ошибка:
        registry.set_error("unknown", "сообщение")
    assert ошибка.value.code == "base_unknown"

    with pytest.raises(UnknownBase) as ошибка:
        registry.set_indexed("unknown", "2026-09-07T10:00:00", 100)
    assert ошибка.value.code == "base_unknown"


def test_visible_возвращает_копии_а_не_живые_объекты_состояния():
    """Регресс: visible() отдавал наружу живые объекты BaseState, общие для всех сессий,
    читающих реестр. Правка снаружи полученного объекта не должна менять состояние в реестре."""
    registry = Registry(собрать("ut"))
    состояние = registry.visible(SessionScope(None, None))[0]

    состояние.last_error = "испорчено снаружи"

    assert registry.visible(SessionScope(None, None))[0].last_error is None


def test_состояние_скрытой_базы_не_видно_другой_сессии():
    registry = Registry(собрать("ut", "buh"))
    registry.set_error("buh", "ошибка")

    видимые = registry.visible(SessionScope(bases=("ut",), default=None))
    имена = {b.name for b in видимые}
    assert имена == {"ut"}
    assert "buh" not in имена


def test_изоляция_скрытая_база_неразличима_от_несуществующей():
    """Отказы при обращении к скрытой базе и заведомо несуществующей базе должны быть
    неразличимы по форме сообщения и подсказки. Это защищает инвариант изоляции:
    сессия не должна узнать из сообщения об ошибке, существует ли скрытая база в настройках
    или её просто никогда не было. Подсказка обязана перечислять только видимые базы.
    """
    registry = Registry(собрать("ut", "buh"))
    scope = SessionScope(bases=("ut",), default=None)

    # Обращение к скрытой базе buh
    with pytest.raises(UnknownBase) as скрытая:
        registry.get("buh", scope)

    # Обращение к заведомо несуществующей базе
    with pytest.raises(UnknownBase) as несуществующая:
        registry.get("нет_такой", scope)

    # Подсказки обязаны совпадать дословно (обе перечисляют только видимые базы)
    assert скрытая.value.hint == несуществующая.value.hint

    # Основной текст неразличим: если подставить имя одного в текст другого, он остаётся верным
    # (оба формируются по шаблону "база «{name}» неизвестна")
    скрытая_текст = str(скрытая.value.args[0])
    несуществующая_текст = str(несуществующая.value.args[0])
    скрытая_с_другим_именем = скрытая_текст.replace("buh", "нет_такой")
    assert скрытая_с_другим_именем == несуществующая_текст
