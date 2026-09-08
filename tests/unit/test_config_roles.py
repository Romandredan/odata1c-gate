"""Роль задаёт умолчания, явные поля базы их перекрывают (SPEC §3.2)."""

import pytest

from odata1c.config.loader import apply_role
from odata1c.config.models import BaseConfig

МИНИМУМ = {
    "label": "УТ 11",
    "url": "https://1c.corp.local/ut/odata/standard.odata/",
    "user": "odata_claude",
    "password": "секрет",
}


@pytest.mark.parametrize(
    ("role", "gate_mode", "write", "independent_delete", "register_write", "commit_limit"),
    [
        ("prod", "identifiers+names", False, False, False, 20),
        ("test", "identifiers", True, False, False, 50),
        ("dev", "off", True, True, True, 0),
    ],
)
def test_умолчания_ролей(role, gate_mode, write, independent_delete, register_write, commit_limit):
    config = BaseConfig(name="ut", **apply_role(role, {**МИНИМУМ, "role": role}))
    assert config.gate.mode == gate_mode
    assert config.write is write
    assert config.permissions.independent_register_delete is independent_delete
    assert config.permissions.register_direct_write is register_write
    assert config.permissions.commit_limit == commit_limit
    assert config.permissions.post_documents is True
    assert config.permissions.mark_deletion is True


def test_явное_поле_перекрывает_роль():
    raw = {**МИНИМУМ, "role": "prod", "write": True, "gate": {"mode": "identifiers"}}
    config = BaseConfig(name="ut", **apply_role("prod", raw))
    assert config.write is True
    assert config.gate.mode == "identifiers"


def test_явное_разрешение_перекрывает_роль():
    raw = {**МИНИМУМ, "role": "prod", "permissions": {"independent_register_delete": True}}
    config = BaseConfig(name="ut", **apply_role("prod", raw))
    assert config.permissions.independent_register_delete is True
    assert config.permissions.commit_limit == 20  # остальное осталось от роли


def test_явное_поле_гейта_перекрывает_роль_остальное_сохраняется():
    raw = {**МИНИМУМ, "role": "prod", "gate": {"names_for": ["Контрагент"]}}
    config = BaseConfig(name="ut", **apply_role("prod", raw))
    assert config.gate.names_for == ["Контрагент"]
    assert config.gate.mode == "identifiers+names"  # уровень защиты остался от роли
