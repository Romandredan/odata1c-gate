"""Чтение bases.yaml и daemon.yaml, наложение умолчаний роли (SPEC §3.1–§3.4)."""

from __future__ import annotations

import copy
import pathlib

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


def _разобрать_yaml(path: pathlib.Path) -> dict:
    """Прочитать YAML-файл; синтаксическая ошибка — ConfigError с именем файла и местом ошибки.

    Текст исключения PyYAML целиком не используется: библиотека вклеивает в него фрагмент
    исходного файла вокруг места ошибки, и если повреждение пришлось на строку с паролем,
    пароль дословно попадает в сообщение. Берём из исключения только позицию (строка,
    колонка) — это числа, не текст файла, — и строим собственное сообщение.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            место = f"строка {mark.line + 1}, колонка {mark.column + 1}"
        else:
            место = "точное место в файле не определено"
        raise ConfigError(
            f"файл {path.name} повреждён и не разбирается как YAML: {место}",
            hint=f"проверьте синтаксис файла {path}",
        ) from None  # текст ошибки PyYAML вклеивает фрагмент файла — а в нём может быть пароль
    return data or {}


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
    _проверить_дом(home)
    warnings: list[str] = []
    daemon = _load_daemon(home, warnings)
    return _собрать(home, daemon, warnings)


def reload_bases(home: pathlib.Path, daemon: DaemonConfig) -> AppConfig:
    """Перечитать ТОЛЬКО `bases.yaml`, оставив уже прочитанный раздел `daemon` (SPEC §3.1,
    поправка 2026-09-14, ADR-0015): без перезапуска демона действует один этот файл.

    `daemon.yaml` здесь не открывается вовсе — и это не экономия, а правило (находка 1 ревью
    задачи 6). `load_config` читает его первым и падает на его ошибке; демон, который зовёт
    `load_config` по отметке `bases.yaml`, дал бы испорченному `daemon.yaml` право вето: первая же
    правка `bases.yaml` закрыла бы все тулы `config_invalid`, а починка `daemon.yaml` отметку
    `bases.yaml` не меняет — шлюз залип бы до перезапуска. Настройки демона за его жизнь не
    меняются по определению, перечитывать их незачем и вредно.

    `warnings` начинается пустым: предупреждение о правах `daemon.yaml` выдано при старте, а
    предупреждения `bases.yaml` собираются заново — они относятся к тому файлу, что сейчас на
    диске.
    """
    _проверить_дом(home)
    return _собрать(home, daemon, [])


def _проверить_дом(home: pathlib.Path) -> None:
    if not home.is_dir():
        # is_dir(), не exists(): путь может существовать как обычный файл (опечатка в --home),
        # и тогда попытка создать home/daemon.yaml упадёт NotADirectoryError чуть ниже —
        # тем же необработанным трейсбеком, который чинит эта проверка.
        raise ConfigError(
            f"домашний каталог не найден: {home}",
            hint=f"выполните: odata1c init --home {home}",
        )


def _собрать(home: pathlib.Path, daemon: DaemonConfig, warnings: list[str]) -> AppConfig:
    """Общий хвост `load_config` и `reload_bases`: `bases.yaml` поверх готового раздела `daemon`."""
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
    return parse_bases(_разобрать_yaml(path), path, warnings)


def parse_bases(
    data: dict,
    path: pathlib.Path,
    warnings: list[str],
    *,
    resolve_keyring: bool = True,
) -> tuple[str | None, dict[str, BaseConfig]]:
    """Собрать записи баз из уже разобранного содержимого файла баз: умолчания роли, модель
    `BaseConfig`, база по умолчанию. `path` нужен только для текстов ошибок.

    Отдельно от чтения файла — ради `base set` (`config/base_edit.py`): команда проверяет новый
    текст этой же функцией ДО того, как положит его на место исходного файла, и с
    `resolve_keyring=False` — пароль `keyring` остаётся строкой, хранилище ОС не трогается: проверка
    текста не должна зависеть от того, установлен ли пакет keyring и записан ли секрет."""
    raw_bases = data.get("bases") or {}
    if not isinstance(raw_bases, dict):
        raise ConfigError("в bases.yaml раздел bases должен быть словарём «имя базы: настройки»")

    bases: dict[str, BaseConfig] = {}
    for name, raw in raw_bases.items():
        if raw is None:
            warnings.append(f"база «{name}»: запись в bases.yaml пуста, база пропущена")
            continue
        if not isinstance(raw, dict):
            raise ConfigError(
                f"база «{name}»: запись должна быть набором полей (ключ: значение), "
                f"получено {type(raw).__name__}"
            )
        if "name" in raw:
            raise ConfigError(
                f"база «{name}»: ключ name внутри записи базы недопустим — "
                f"имя базы уже задано ключом «{name}» в bases.yaml"
            )
        role = raw.get("role", "prod")
        try:
            resolved = apply_role(role, raw)
        except ConfigError as exc:
            # `from None`: исключение в `__cause__` попало бы в трассировку целиком, а при отказе
            # валидатора оно несёт `input_value` — значение поля, то есть пароль без кавычек.
            raise ConfigError(f"база «{name}»: {exc}", code=exc.code, hint=exc.hint) from None
        try:
            bases[name] = BaseConfig(name=name, **resolved)
        except pydantic.ValidationError as exc:
            текст_ошибки = format_validation_error(exc)
            # Файл называется в сообщении, а путь — в подсказке (находка 3 ревью задачи 6 плана
            # M2b): отказ по не прошедшему проверку файлу доходит до модели тем же кодом
            # `config_invalid`, что и битый YAML, а тот файл и место называет. Без имени файла
            # отказ не отличить от ошибки в `daemon.yaml` или в политике.
            raise ConfigError(
                f"bases.yaml: база «{name}» описана неверно: {текст_ошибки}",
                hint=f"проверьте запись базы в файле {path}",
            ) from None  # `__cause__` с `input_value` (пароль) в трассировку не попадает
        if resolve_keyring and bases[name].password == "keyring":
            bases[name] = bases[name].model_copy(update={"password": _из_keyring(name)})

    default = data.get("default")
    if default is not None and not isinstance(default, str):
        raise ConfigError(
            f"default должен быть строкой с именем базы, получено {type(default).__name__}"
        )
    return default, bases


def _load_daemon(home: pathlib.Path, warnings: list[str]) -> DaemonConfig:
    """Прочитать daemon.yaml. Только чтение: секрет гейта здесь никогда не создаётся и не
    пишется на диск (SPEC §6.3) — иначе несколько одновременных чтений на свежем каталоге
    порождают в памяти разные секреты при одном значении на диске (задокументированный
    дефект ревью M1a). Создание секрета — обязанность команды создания домашнего каталога,
    см. odata1c.config.writer.ensure_gate_secret.
    """
    path = home / "daemon.yaml"
    data = {}
    if path.exists():
        data = _разобрать_yaml(path)
    try:
        daemon = DaemonConfig(**data)
    except pydantic.ValidationError as exc:
        # `from None`: `input_value` в `__cause__` — значение поля, в том числе секрет гейта.
        raise ConfigError(f"daemon.yaml описан неверно: {format_validation_error(exc)}") from None
    if not daemon.gate_secret:
        raise ConfigError(
            "секрет гейта (gate_secret) не найден в daemon.yaml",
            hint=f"выполните: odata1c init --home {home}",
        )
    предупреждение = check_file_permissions(path)
    if предупреждение:
        warnings.append(предупреждение)
    return daemon


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


def format_validation_error(exc: pydantic.ValidationError) -> str:
    """Сжатый текст ошибки pydantic для сообщений ConfigError/OdataError. Публичная —
    используется и здесь, и в odata1c.cli, чтобы не дублировать одну и ту же функцию."""
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
