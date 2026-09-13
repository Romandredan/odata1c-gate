"""Модели настроек: база, разрешения, гейт, демон (SPEC §3)."""

from __future__ import annotations

import pathlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ИМЯ_БАЗЫ = re.compile(r"^[a-z0-9_]{1,32}$")
ОКОНЧАНИЕ_URL = "/odata/standard.odata/"

GateMode = Literal["off", "identifiers", "identifiers+names"]
Role = Literal["prod", "test", "dev"]


class Permissions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    post_documents: bool = True
    mark_deletion: bool = True
    independent_register_delete: bool = False
    register_direct_write: bool = False
    allow_entities: list[str] = Field(default_factory=list)
    deny_entities: list[str] = Field(default_factory=list)
    deny_fields: list[str] = Field(default_factory=list)
    commit_limit: int = 20


class GateSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: GateMode = "identifiers+names"
    names_for: list[str] | None = None
    scan_free_text: bool = True


class BaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    label: str
    url: str
    user: str
    password: str = ""
    role: Role = "prod"
    verify_tls: bool | str = True
    timeout_s: int = 60
    virtual_timeout_s: int = 180
    concurrency: int = 2
    ib_session: bool = True
    write: bool = False
    permissions: Permissions = Field(default_factory=Permissions)
    gate: GateSettings = Field(default_factory=GateSettings)
    recipes: str | None = None

    @field_validator("name")
    @classmethod
    def _проверить_имя(cls, value: str) -> str:
        if not ИМЯ_БАЗЫ.match(value):
            raise ValueError(
                f"имя базы «{value}» не подходит: допустимы строчные латинские буквы, "
                "цифры и подчёркивание, до 32 символов"
            )
        return value

    @field_validator("url")
    @classmethod
    def _проверить_url(cls, value: str) -> str:
        if not value.endswith(ОКОНЧАНИЕ_URL):
            raise ValueError(
                f"адрес базы должен оканчиваться на {ОКОНЧАНИЕ_URL}, получено «{value}»"
            )
        return value


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_default: int = 50
    top_max: int = 1000
    expand_depth: int = 2
    result_chars: int = 120_000
    string_chars: int = 2_000
    pending_ttl_s: int = 600


class DaemonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: int = 7171
    gate_secret: str = ""
    limits: Limits = Field(default_factory=Limits)
    write_confirm_fallback: Literal["deny", "trust_client"] = "deny"
    reindex_check_hours: int = 24
    # Имена образов Claude Code — предков лаунчера, при которых имя клиента `claude-code`
    # заверяется (Ruling 61). Дополняет встроенный перечень `launch_parent.ИМЕНА_CLAUDE_CODE`;
    # читается лаунчером при старте. Пусто — только встроенный перечень.
    claude_code_parents: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    home: pathlib.Path
    default: str | None
    bases: dict[str, BaseConfig]
    daemon: DaemonConfig
    warnings: list[str] = Field(default_factory=list)
