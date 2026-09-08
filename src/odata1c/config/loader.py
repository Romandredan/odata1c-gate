"""Чтение bases.yaml и daemon.yaml, наложение умолчаний роли (SPEC §3.1–§3.4)."""

from __future__ import annotations

import base64
import copy
import os
import pathlib
import secrets

import pydantic
import yaml

from odata1c.config.home import check_file_permissions
from odata1c.config.models import AppConfig, BaseConfig, DaemonConfig

# Таблица SPEC §3.2 дословно: роль задаёт умолчания, явные поля базы их перекрывают.
УМОЛЧАНИЯ_РОЛЕЙ: dict[str, dict] = {
    "prod": {
        "gate": {"mode": "identifiers+names"},
        "write": False,
        "permissions": {
            "post_documents": True,
            "mark_deletion": True,
            "independent_register_delete": False,
            "register_direct_write": False,
            "commit_limit": 20,
        },
    },
    "test": {
        "gate": {"mode": "identifiers"},
        "write": True,
        "permissions": {
            "post_documents": True,
            "mark_deletion": True,
            "independent_register_delete": False,
            "register_direct_write": False,
            "commit_limit": 50,
        },
    },
    "dev": {
        "gate": {"mode": "off"},
        "write": True,
        "permissions": {
            "post_documents": True,
            "mark_deletion": True,
            "independent_register_delete": True,
            "register_direct_write": True,
            "commit_limit": 0,
        },  # 0 = без лимита
    },
}


class ConfigError(Exception):
    """Ошибка настроек. Код — из перечня SPEC §5.2."""

    def __init__(self, message: str, code: str = "config_invalid", hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


def apply_role(role: str, raw: dict) -> dict:
    """Наложить умолчания роли на запись базы: явные значения выигрывают."""
    if role not in УМОЛЧАНИЯ_РОЛЕЙ:
        raise ConfigError(f"неизвестная роль «{role}»; допустимы prod, test, dev")
    result = copy.deepcopy(УМОЛЧАНИЯ_РОЛЕЙ[role])
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = {**result[key], **value}
        else:
            result[key] = value
    result["role"] = role
    return result


def load_config(home: pathlib.Path) -> AppConfig:
    warnings: list[str] = []
    daemon = _load_daemon(home, warnings)
    default, bases = _load_bases(home, warnings)
    if default is not None and default not in bases:
        raise ConfigError(
            f"база по умолчанию «{default}» не описана в bases.yaml",
            code="base_unknown",
            hint=f"известные базы: {', '.join(sorted(bases)) or 'ни одной'}",
        )
    return AppConfig(home=home, default=default, bases=bases, daemon=daemon, warnings=warnings)


def _load_bases(
    home: pathlib.Path, warnings: list[str]
) -> tuple[str | None, dict[str, BaseConfig]]:
    path = home / "bases.yaml"
    if not path.exists():
        return None, {}
    предупреждение = check_file_permissions(path)
    if предупреждение:
        warnings.append(предупреждение)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_bases = data.get("bases") or {}
    if not isinstance(raw_bases, dict):
        raise ConfigError("в bases.yaml раздел bases должен быть словарём «имя базы: настройки»")

    bases: dict[str, BaseConfig] = {}
    for name, raw in raw_bases.items():
        if raw is None:
            continue
        role = raw.get("role", "prod")
        try:
            bases[name] = BaseConfig(name=name, **apply_role(role, raw))
        except pydantic.ValidationError as exc:
            raise ConfigError(f"база «{name}» описана неверно: {_кратко(exc)}") from exc
        if bases[name].password == "keyring":
            bases[name] = bases[name].model_copy(update={"password": _из_keyring(name)})
    return data.get("default"), bases


def _load_daemon(home: pathlib.Path, warnings: list[str]) -> DaemonConfig:
    path = home / "daemon.yaml"
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        daemon = DaemonConfig(**data)
    except pydantic.ValidationError as exc:
        raise ConfigError(f"daemon.yaml описан неверно: {_кратко(exc)}") from exc
    if not daemon.gate_secret:
        daemon = daemon.model_copy(update={"gate_secret": _создать_секрет(path, data)})
    return daemon


def _создать_секрет(path: pathlib.Path, data: dict) -> str:
    """Секрет HMAC — 32 случайных байта, создаётся при первом запуске (SPEC §6.3)."""
    secret = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    data = {**data, "gate_secret": secret}
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return secret


def _из_keyring(base_name: str) -> str:
    try:
        import keyring
    except ImportError as exc:
        raise ConfigError(
            f"база «{base_name}»: пароль указан как keyring, но пакет keyring не установлен",
            hint="установите odata1c[keyring] или впишите пароль в bases.yaml",
        ) from exc
    password = keyring.get_password("odata1c/base", base_name)
    if password is None:
        raise ConfigError(
            f"база «{base_name}»: пароль не найден в хранилище ОС",
            hint=f"запишите его командой odata1c base secret {base_name}",
        )
    return password


def _кратко(exc: pydantic.ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
