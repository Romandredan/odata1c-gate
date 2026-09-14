"""Модели настроек: база, разрешения, гейт, демон (SPEC §3)."""

from __future__ import annotations

import pathlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ИМЯ_БАЗЫ = re.compile(r"^[a-z0-9_]{1,32}$")
ОКОНЧАНИЕ_URL = "/odata/standard.odata/"

# Имя конфигурации для библиотеки рецептов (`recipes/<config>/`, ADR-0011, поправка 2026-09-14):
# буква в начале — та же форма, что у имени рецепта (`recipes/model.py::ИМЯ_РЕЦЕПТА`), только
# короче (32 символа, как у имени базы) — это имя каталога, а не имя файла.
ИМЯ_КОНФИГУРАЦИИ = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

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
    """Гейт в записи базы: только уровень (SPEC §3.1, ADR-0015). Что именно скрывать и что
    открыть — `bases/<база>/policy.yaml`; ключи `names_for` и `scan_free_text` переехали туда."""

    model_config = ConfigDict(extra="forbid")

    mode: GateMode = "identifiers+names"

    @model_validator(mode="before")
    @classmethod
    def _переехавшие_ключи(cls, данные):
        if isinstance(данные, dict):
            лишние = [к for к in ("names_for", "scan_free_text") if к in данные]
            if лишние:
                raise ValueError(
                    f"gate.{лишние[0]} больше не задаётся в bases.yaml — перенесите в "
                    "bases/<база>/policy.yaml (раздел с тем же именем, SPEC §6.9)"
                )
        return данные


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
    # Конфигурация для библиотеки рецептов (recipes/<config>/, ADR-0011, поправка 2026-09-14):
    # какой каталог ~/.claude/odata1c/recipes/ и какой шаблон пакета (templates/recipes/
    # <config>.yaml) видит эта база нижними слоями recipes.model.load_layered. Не путать с
    # `recipes` (путь к собственному файлу рецептов базы, верхний слой) — `config` называет общую
    # библиотеку, `recipes` — файл одной базы.
    config: str | None = None

    # Оба валидатора называют поле и правило, но НЕ повторяют полученное значение (находка 3
    # ревью задачи 6 плана M2b). Текст `ValidationError` доходит до модели: `bases.yaml`
    # перечитывается на ходу (SPEC §3.1, поправка 2026-09-14), и файл, не прошедший проверку,
    # закрывает тулы отказом `config_invalid` с этим текстом внутри. Значение из файла в таком
    # отказе — содержимое файла настроек в ответе MCP; правило же владельцу и объясняет, что
    # чинить, а своё написание он видит в самом файле (и в терминале, если ошибся в `base add`).

    @field_validator("name")
    @classmethod
    def _проверить_имя(cls, value: str) -> str:
        if not ИМЯ_БАЗЫ.match(value):
            raise ValueError(
                "имя базы не подходит: допустимы строчные латинские буквы, "
                "цифры и подчёркивание, до 32 символов"
            )
        return value

    @field_validator("config")
    @classmethod
    def _проверить_конфигурацию(cls, value: str | None) -> str | None:
        if value is not None and not ИМЯ_КОНФИГУРАЦИИ.match(value):
            raise ValueError(
                "config не подходит: строчная латинская буква в начале, дальше строчные "
                "латинские буквы, цифры и подчёркивание, до 32 символов"
            )
        return value

    @field_validator("url")
    @classmethod
    def _проверить_url(cls, value: str) -> str:
        if not value.endswith(ОКОНЧАНИЕ_URL):
            raise ValueError(f"адрес базы должен оканчиваться на {ОКОНЧАНИЕ_URL}")
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
