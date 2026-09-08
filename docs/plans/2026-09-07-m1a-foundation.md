# M1a — основание: план исполнения

> **Для исполнителя-агента:** ОБЯЗАТЕЛЬНЫЙ СКИЛЛ: `superpowers:subagent-driven-development`
> (рекомендуется) либо `superpowers:executing-plans`. Шаги отмечаются флажками (`- [ ]`).

**Цель:** довести репозиторий от пустого каркаса до состояния, в котором команда
`odata1c base test <имя>` открывает соединение с базой 1С и докладывает результат.

**Подход:** сначала манифест пакета и запускаемые тесты, затем три модуля снизу вверх — `config`
(разбор `bases.yaml` и `daemon.yaml`, применение ролей), `registry` (реестр баз и их состояние),
`client1c` (httpx-пул, сеанс 1С, семафор, отображение ошибок), затем команды CLI поверх них.
Каждая задача заканчивается работающим и проверенным куском; тесты пишутся до кода.

**Стек:** Python 3.12, `pydantic` (проверка настроек), `pyyaml`, `httpx`, `typer`-подобный разбор
аргументов средствами `argparse` из стандартной библиотеки, `pytest` + `pytest-asyncio` +
`respx` (подмена HTTP в тестах).

**Спецификация:** [SPEC.md](../../SPEC.md) §2.2, §2.3, §3 (вся), §9, §10; термины —
[CONTEXT.md](../../CONTEXT.md); решения — [ADR-0007](../adr/0007-config-layering-and-secrets-in-keyring.md),
[ADR-0013](../adr/0013-launcher-and-direct-http-no-project-context.md),
[ADR-0014](../adr/0014-single-bases-yaml-with-passwords.md).

## Общие ограничения

- Python ≥ 3.12; целевая ОС — Windows; поставка — пакет PyPI, запуск через `uvx odata1c …`
  (SPEC §11.1). Зависимости только из списка SPEC §11.1: `mcp`, `httpx`, `pydantic`, `pyyaml`,
  `lxml`, `snowballstemmer`, `ahocorasick_rs`, `uvicorn`, необязательно `keyring`.
- Домашний каталог: `--home`, затем `ODATA1C_HOME`, затем `~/.claude/odata1c/` — именно в этом
  порядке (SPEC §2.1). Создаётся с правами только текущего пользователя: `icacls` на Windows,
  `chmod 700` в остальных ОС (SPEC §2.3).
- Имя базы: `[a-z0-9_]{1,32}` (SPEC §3.1). URL базы обязан оканчиваться на `/odata/standard.odata/`
  (SPEC §3.1, §9).
- Умолчания ролей `prod` / `test` / `dev` — таблица SPEC §3.2, значения переносятся дословно.
- Лимиты по умолчанию — таблица SPEC §10: `$top` 50 (максимум 1000), глубина `$expand` 2, размер
  результата 120 000 символов, длина строки 2000, TTL pending 600 с, семафор на базу 2, таймаут
  запроса 60 с и 180 с для виртуальных таблиц.
- Пароли: открытым текстом в `bases.yaml` либо значение `keyring` (SPEC §3.4). Реальные `bases.yaml`
  не коммитятся — `.gitignore` их закрывает; в тестах только временные каталоги.
- Язык кода: имена сущностей 1С и ключи настроек — как в спецификации; комментарии, сообщения об
  ошибках и подсказки — на русском, терминология по `CONTEXT.md` (база, роль, демон, лаунчер, гейт,
  разрешения, индекс, сущность). Запрещённые синонимы (`_Avoid_`) не использовать.
- Коды ошибок — только из перечня SPEC §5.2; новых не изобретать.

---

### Задача 1: манифест пакета и запускаемые тесты

Без манифеста ни один шаг «прогнать тест» в этом и следующих планах не выполним. Задача
заканчивается зелёным прогоном `pytest` на одном осмысленном тесте.

**Файлы:**
- Создать: `pyproject.toml`
- Создать: `src/odata1c/__about__.py`
- Изменить: `src/odata1c/__init__.py`
- Создать: `tests/unit/test_package.py`
- Изменить: `AGENTS.md` (раздел «Сборка, тесты, команды»)

**Интерфейсы:**
- Потребляет: ничего.
- Отдаёт: `odata1c.__version__: str`; команды `pytest`, `ruff check .`, `ruff format --check .`;
  консольную команду `odata1c`, указывающую на `odata1c.cli:main` (сама функция появляется в задаче 6).

- [ ] **Шаг 1: Написать тест на пакет**

Создать `tests/unit/test_package.py`:

```python
"""Проверка, что пакет собран и импортируется."""
import odata1c


def test_версия_пакета_объявлена():
    assert isinstance(odata1c.__version__, str)
    assert odata1c.__version__.count(".") >= 2
```

- [ ] **Шаг 2: Прогнать тест и убедиться, что он падает**

Выполнить: `uv run pytest tests/unit/test_package.py -v`
Ожидается: падение — нет `pyproject.toml`, `uv run` не может собрать окружение.

- [ ] **Шаг 3: Написать манифест**

Создать `pyproject.toml`:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "odata1c"
dynamic = ["version"]
description = "Локальный MCP-шлюз к OData 1С:Предприятие с гейтом псевдонимизации"
readme = "README.md"
requires-python = ">=3.12"
license = { text = "MIT" }
authors = [{ name = "Roman Danilov" }]
dependencies = [
    # Нижняя граница по факту пробы P1: SDK вышел в мажорной версии 2 (docs/probes/P1-wheels-windows.md).
    "mcp>=2.2",
    "httpx>=0.27",
    "pydantic>=2.7",
    "pyyaml>=6.0",
    "lxml>=5.2",
    "snowballstemmer>=2.2",
    "ahocorasick-rs>=0.22",
    "uvicorn>=0.30",
]

[project.optional-dependencies]
keyring = ["keyring>=25.0"]

[project.scripts]
odata1c = "odata1c.cli:main"

[dependency-groups]
dev = [
    "pytest>=8.2",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
    "hypothesis>=6.100",
    "ruff>=0.5",
]

[tool.hatch.version]
path = "src/odata1c/__about__.py"

[tool.hatch.build.targets.wheel]
packages = ["src/odata1c"]

[tool.hatch.build.targets.sdist]
exclude = ["tools/probes", "docs/probes", "tests/fixtures/edmx/*.full.edmx"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
filterwarnings = ["error"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
```

Создать `src/odata1c/__about__.py`:

```python
__version__ = "0.1.0.dev0"
```

Заменить содержимое `src/odata1c/__init__.py`:

```python
"""odata1c-gate: локальный MCP-шлюз к OData 1С с гейтом псевдонимизации."""

from odata1c.__about__ import __version__

__all__ = ["__version__"]
```

- [ ] **Шаг 4: Прогнать тест и убедиться, что он проходит**

Выполнить: `uv run pytest tests/unit/test_package.py -v`
Ожидается: `1 passed`.

Выполнить: `uv run ruff check . && uv run ruff format --check .`
Ожидается: без замечаний.

- [ ] **Шаг 5: Дописать команды в `AGENTS.md`**

В разделе «Сборка, тесты, команды» заменить строку «Кода пока нет — команды ниже из спецификации и
станут актуальны с реализацией» на фактические команды:

```markdown
Команды разработки (Python 3.12, менеджер окружения `uv`):

- `uv sync` — поставить зависимости, включая группу `dev`;
- `uv run pytest` — все тесты; `uv run pytest tests/unit -q` — только юнит;
- `uv run ruff check .` и `uv run ruff format --check .` — линт и формат;
- `uv run odata1c <команда>` — CLI из рабочей копии.
```

- [ ] **Шаг 6: Зафиксировать изменения**

```bash
git add pyproject.toml src/odata1c/__about__.py src/odata1c/__init__.py tests/unit/test_package.py AGENTS.md
git commit -m "feat: манифест пакета, запускаемые тесты и линт"
```

---

### Задача 2: домашний каталог и его права

Домашний каталог создаёт лаунчер до всего остального (SPEC §2.1, §2.3), поэтому он и появляется
первым. Права закрываются сразу: в каталоге лежат пароли и журнал с реальными значениями.

**Файлы:**
- Создать: `src/odata1c/config/home.py`
- Создать: `tests/unit/test_home.py`

**Интерфейсы:**
- Потребляет: `odata1c.__version__` (задача 1).
- Отдаёт:
  - `resolve_home(explicit: str | None = None) -> pathlib.Path` — порядок `--home` → `ODATA1C_HOME`
    → `~/.claude/odata1c/`;
  - `ensure_home(path: pathlib.Path) -> HomeStatus` — создаёт каталог и подкаталоги `bases/`,
    `logs/`, закрывает права, возвращает состояние;
  - `HomeStatus` — датакласс с полями `path: pathlib.Path`, `created: bool`,
    `permissions_narrowed: bool`, `warning: str | None`;
  - `check_file_permissions(path: pathlib.Path) -> str | None` — предупреждение, если файл доступен
    другим учётным записям, иначе `None`.

- [ ] **Шаг 1: Написать падающие тесты**

Создать `tests/unit/test_home.py`:

```python
"""Домашний каталог: порядок разрешения пути, создание, права."""
import pathlib

import pytest

from odata1c.config.home import ensure_home, resolve_home


def test_явный_путь_главнее_переменной_окружения(tmp_path, monkeypatch):
    monkeypatch.setenv("ODATA1C_HOME", str(tmp_path / "из-окружения"))
    assert resolve_home(str(tmp_path / "явный")) == tmp_path / "явный"


def test_переменная_окружения_главнее_умолчания(tmp_path, monkeypatch):
    monkeypatch.setenv("ODATA1C_HOME", str(tmp_path / "из-окружения"))
    assert resolve_home(None) == tmp_path / "из-окружения"


def test_умолчание_внутри_домашнего_каталога_пользователя(monkeypatch):
    monkeypatch.delenv("ODATA1C_HOME", raising=False)
    assert resolve_home(None) == pathlib.Path.home() / ".claude" / "odata1c"


def test_создание_каталога_и_подкаталогов(tmp_path):
    status = ensure_home(tmp_path / "home")
    assert status.created is True
    assert (tmp_path / "home" / "bases").is_dir()
    assert (tmp_path / "home" / "logs").is_dir()


def test_повторный_вызов_не_считается_созданием(tmp_path):
    ensure_home(tmp_path / "home")
    assert ensure_home(tmp_path / "home").created is False


@pytest.mark.skipif(sys.platform == "win32", reason="проверка режима доступа только для POSIX")
def test_права_каталога_закрыты(tmp_path):
    status = ensure_home(tmp_path / "home")
    assert status.permissions_narrowed is True


@pytest.mark.skipif(sys.platform != "win32", reason="разбор вывода icacls только для Windows")
def test_свежий_каталог_предупреждения_не_даёт(tmp_path):
    status = ensure_home(tmp_path / "home")
    assert check_file_permissions(status.path) is None


@pytest.mark.skipif(sys.platform != "win32", reason="разбор вывода icacls только для Windows")
def test_широкий_доступ_первой_записью_замечен(tmp_path):
    """Первая запись списка доступа печатается icacls в одной строке с путём: её нельзя терять."""
    status = ensure_home(tmp_path / "home")
    # S-1-1-0 — идентификатор группы «Все», одинаков на любой локали Windows.
    subprocess.run(["icacls", str(status.path), "/grant", "*S-1-1-0:(F)"],
                   check=True, capture_output=True)
    предупреждение = check_file_permissions(status.path)
    assert предупреждение is not None
    assert str(status.path) in предупреждение


@pytest.mark.skipif(sys.platform != "win32", reason="разбор вывода icacls только для Windows")
def test_несуществующий_путь_не_ломает_проверку(tmp_path):
    assert check_file_permissions(tmp_path / "нет-такого") is None
```

Импорты теста: `import pathlib`, `import subprocess`, `import sys`, `import pytest`, и из модуля —
`check_file_permissions`, `ensure_home`, `resolve_home`.

- [ ] **Шаг 2: Прогнать и убедиться в падении**

Выполнить: `uv run pytest tests/unit/test_home.py -v`
Ожидается: `ModuleNotFoundError: No module named 'odata1c.config.home'`.

- [ ] **Шаг 3: Написать модуль**

Создать `src/odata1c/config/home.py`:

```python
"""Домашний каталог шлюза: разрешение пути, создание, ограничение прав.

Порядок разрешения задан SPEC §2.1: аргумент --home, затем ODATA1C_HOME, затем ~/.claude/odata1c/.
В каталоге лежат пароли баз и журнал с реальными значениями, поэтому права закрываются сразу
(SPEC §2.3).
"""
from __future__ import annotations

import dataclasses
import getpass
import os
import pathlib
import stat
import subprocess
import sys

SUBDIRS = ("bases", "logs")


@dataclasses.dataclass(slots=True)
class HomeStatus:
    path: pathlib.Path
    created: bool
    permissions_narrowed: bool
    warning: str | None = None


def resolve_home(explicit: str | None = None) -> pathlib.Path:
    if explicit:
        return pathlib.Path(explicit).expanduser()
    from_env = os.environ.get("ODATA1C_HOME")
    if from_env:
        return pathlib.Path(from_env).expanduser()
    return pathlib.Path.home() / ".claude" / "odata1c"


def ensure_home(path: pathlib.Path) -> HomeStatus:
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    for name in SUBDIRS:
        (path / name).mkdir(exist_ok=True)
    narrowed, warning = _narrow_permissions(path)
    return HomeStatus(path=path, created=created, permissions_narrowed=narrowed, warning=warning)


def _narrow_permissions(path: pathlib.Path) -> tuple[bool, str | None]:
    if sys.platform == "win32":
        user = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}".lstrip("\\")
        try:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                check=True, capture_output=True, text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            return False, f"не удалось закрыть права на {path}: {exc}"
        return True, None
    path.chmod(stat.S_IRWXU)
    return True, None


def check_file_permissions(path: pathlib.Path) -> str | None:
    """Предупреждение, если файл виден другим учётным записям (SPEC §2.3)."""
    if not path.exists():
        return None
    if sys.platform == "win32":
        try:
            output = subprocess.run(["icacls", str(path)], check=True,
                                    capture_output=True, text=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return None
        me = getpass.getuser().lower()
        others = [line.strip() for line in output.splitlines()[1:]
                  if ":" in line and me not in line.lower()
                  and "NT AUTHORITY\\SYSTEM" not in line
                  and "BUILTIN\\Администраторы" not in line
                  and "BUILTIN\\Administrators" not in line]
        if others:
            return f"{path} доступен другим учётным записям: {'; '.join(others)}"
        return None
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"{path} доступен другим учётным записям (режим {oct(mode & 0o777)})"
    return None
```

- [ ] **Шаг 4: Прогнать тесты**

Выполнить: `uv run pytest tests/unit/test_home.py -v`
Ожидается: все тесты проходят (тест прав пропускается на Windows).

- [ ] **Шаг 5: Зафиксировать изменения**

```bash
git add src/odata1c/config/home.py tests/unit/test_home.py
git commit -m "feat: домашний каталог с ограничением прав"
```

---

### Задача 3: разбор `bases.yaml` и `daemon.yaml`, применение ролей

Ядро настроек. Роль даёт умолчания, явные поля записи базы их перекрывают (SPEC §3.2) — это правило
проверяется тестами по каждой строке таблицы ролей.

**Файлы:**
- Создать: `src/odata1c/config/models.py`
- Создать: `src/odata1c/config/loader.py`
- Создать: `tests/unit/test_config_roles.py`
- Создать: `tests/unit/test_config_loader.py`

**Интерфейсы:**
- Потребляет: `resolve_home`, `check_file_permissions` (задача 2).
- Отдаёт:
  - `Permissions` — модель с полями `post_documents: bool`, `mark_deletion: bool`,
    `independent_register_delete: bool`, `register_direct_write: bool`, `allow_entities: list[str]`,
    `deny_entities: list[str]`, `deny_fields: list[str]`, `commit_limit: int`;
  - `GateSettings` — `mode: Literal["off", "identifiers", "identifiers+names"]`,
    `names_for: list[str] | None`, `scan_free_text: bool`;
  - `BaseConfig` — `name: str`, `label: str`, `url: str`, `user: str`, `password: str`,
    `role: Literal["prod","test","dev"]`, `verify_tls: bool | str`, `timeout_s: int`,
    `concurrency: int`, `ib_session: bool`, `write: bool`, `permissions: Permissions`,
    `gate: GateSettings`, `recipes: str | None`;
  - `Limits` — поля из SPEC §3.3 (`top_default`, `top_max`, `expand_depth`, `result_chars`,
    `string_chars`, `pending_ttl_s`);
  - `DaemonConfig` — `port: int`, `gate_secret: str`, `limits: Limits`,
    `write_confirm_fallback: Literal["deny","trust_client"]`, `reindex_check_hours: int`;
  - `AppConfig` — `home: pathlib.Path`, `default: str | None`, `bases: dict[str, BaseConfig]`,
    `daemon: DaemonConfig`, `warnings: list[str]`;
  - `load_config(home: pathlib.Path) -> AppConfig`;
  - `apply_role(role: str, raw: dict) -> dict` — наложение умолчаний роли;
  - `ConfigError(Exception)` с полем `code: str` из перечня SPEC §5.2.

- [ ] **Шаг 1: Написать тесты на роли**

Создать `tests/unit/test_config_roles.py`:

```python
"""Роль задаёт умолчания, явные поля базы их перекрывают (SPEC §3.2)."""
import pytest

from odata1c.config.models import BaseConfig
from odata1c.config.loader import apply_role

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
```

- [ ] **Шаг 2: Написать тесты на разбор файла**

Создать `tests/unit/test_config_loader.py`:

```python
"""Разбор bases.yaml и daemon.yaml: проверки значений и сообщения об ошибках."""
import pytest

from odata1c.config.loader import ConfigError, load_config

BASES = """
default: ut
bases:
  ut:
    label: УТ 11, боевая
    url: https://1c.corp.local/ut/odata/standard.odata/
    user: odata_claude
    password: "секрет"
    role: prod
"""


def записать(tmp_path, bases: str = BASES, daemon: str | None = None):
    (tmp_path / "bases.yaml").write_text(bases, encoding="utf-8")
    if daemon is not None:
        (tmp_path / "daemon.yaml").write_text(daemon, encoding="utf-8")
    (tmp_path / "bases").mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    return tmp_path


def test_разбор_минимальной_настройки(tmp_path):
    config = load_config(записать(tmp_path))
    assert set(config.bases) == {"ut"}
    assert config.default == "ut"
    assert config.bases["ut"].label == "УТ 11, боевая"
    assert config.bases["ut"].concurrency == 2
    assert config.bases["ut"].timeout_s == 60


def test_умолчания_демона_без_файла(tmp_path):
    config = load_config(записать(tmp_path))
    assert config.daemon.port == 7171
    assert config.daemon.limits.top_default == 50
    assert config.daemon.limits.top_max == 1000
    assert config.daemon.limits.expand_depth == 2
    assert config.daemon.limits.result_chars == 120_000
    assert config.daemon.limits.string_chars == 2_000
    assert config.daemon.limits.pending_ttl_s == 600
    assert config.daemon.write_confirm_fallback == "deny"
    assert len(config.daemon.gate_secret) >= 40  # 32 байта в base64


def test_секрет_гейта_сохраняется_между_запусками(tmp_path):
    home = записать(tmp_path)
    первый = load_config(home).daemon.gate_secret
    assert load_config(home).daemon.gate_secret == первый


def test_url_без_стандартного_окончания_отклоняется(tmp_path):
    плохой = BASES.replace("/odata/standard.odata/", "/odata/")
    with pytest.raises(ConfigError) as ошибка:
        load_config(записать(tmp_path, плохой))
    assert "odata/standard.odata" in str(ошибка.value)


def test_недопустимое_имя_базы_отклоняется(tmp_path):
    плохой = BASES.replace("  ut:", "  UT-Боевая:")
    with pytest.raises(ConfigError) as ошибка:
        load_config(записать(tmp_path, плохой))
    assert "имя базы" in str(ошибка.value).lower()


def test_база_по_умолчанию_должна_существовать(tmp_path):
    плохой = BASES.replace("default: ut", "default: нет_такой")
    with pytest.raises(ConfigError):
        load_config(записать(tmp_path, плохой))


def test_отсутствие_файла_баз_не_ошибка_а_пустой_список(tmp_path):
    (tmp_path / "bases").mkdir()
    (tmp_path / "logs").mkdir()
    config = load_config(tmp_path)
    assert config.bases == {}
    assert config.default is None
```

- [ ] **Шаг 3: Прогнать и убедиться в падении**

Выполнить: `uv run pytest tests/unit/test_config_roles.py tests/unit/test_config_loader.py -v`
Ожидается: `ModuleNotFoundError` для `odata1c.config.models`.

- [ ] **Шаг 4: Написать модели**

Создать `src/odata1c/config/models.py`:

```python
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
            raise ValueError(f"адрес базы должен оканчиваться на {ОКОНЧАНИЕ_URL}, получено «{value}»")
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


class AppConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    home: pathlib.Path
    default: str | None
    bases: dict[str, BaseConfig]
    daemon: DaemonConfig
    warnings: list[str] = Field(default_factory=list)
```

- [ ] **Шаг 5: Написать загрузчик**

Создать `src/odata1c/config/loader.py`:

```python
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
        "permissions": {"post_documents": True, "mark_deletion": True,
                        "independent_register_delete": False, "register_direct_write": False,
                        "commit_limit": 20},
    },
    "test": {
        "gate": {"mode": "identifiers"},
        "write": True,
        "permissions": {"post_documents": True, "mark_deletion": True,
                        "independent_register_delete": False, "register_direct_write": False,
                        "commit_limit": 50},
    },
    "dev": {
        "gate": {"mode": "off"},
        "write": True,
        "permissions": {"post_documents": True, "mark_deletion": True,
                        "independent_register_delete": True, "register_direct_write": True,
                        "commit_limit": 0},  # 0 = без лимита
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


def _load_bases(home: pathlib.Path, warnings: list[str]) -> tuple[str | None, dict[str, BaseConfig]]:
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
```

- [ ] **Шаг 6: Прогнать тесты**

Выполнить: `uv run pytest tests/unit/test_config_roles.py tests/unit/test_config_loader.py -v`
Ожидается: все проходят.

- [ ] **Шаг 7: Зафиксировать изменения**

```bash
git add src/odata1c/config/models.py src/odata1c/config/loader.py tests/unit/test_config_roles.py tests/unit/test_config_loader.py
git commit -m "feat: разбор настроек баз и демона с умолчаниями ролей"
```

---

### Задача 4: реестр баз

Реестр отвечает на вопрос «какие базы видны этой сессии и в каком они состоянии» (SPEC §2.2).
Сужение видимости аргументами лаунчера `--bases` и `--default` — часть ADR-0013, поэтому проверяется
здесь, а не в слое тулов.

**Файлы:**
- Создать: `src/odata1c/registry/registry.py`
- Создать: `tests/unit/test_registry.py`

**Интерфейсы:**
- Потребляет: `AppConfig`, `BaseConfig` (задача 3).
- Отдаёт:
  - `BaseState` — датакласс: `name`, `label`, `role`, `gate_mode`, `write`, `indexed: bool`,
    `indexed_at: str | None`, `entity_count: int | None`, `last_error: str | None`;
  - `Registry` с методами `__init__(config: AppConfig)`, `visible(session: SessionScope) -> list[BaseState]`,
    `get(name: str | None, session: SessionScope) -> BaseConfig` (при `None` — база по умолчанию
    сессии), `set_error(name: str, message: str) -> None`, `set_indexed(name: str, indexed_at: str,
    entity_count: int) -> None`;
  - `SessionScope` — датакласс: `bases: tuple[str, ...] | None`, `default: str | None`;
  - `UnknownBase(ConfigError)` с кодом `base_unknown`.

- [ ] **Шаг 1: Написать тесты**

Создать `tests/unit/test_registry.py`:

```python
"""Реестр баз: видимость по сессии, база по умолчанию, состояние."""
import pytest

from odata1c.config.models import AppConfig, BaseConfig, DaemonConfig
from odata1c.registry.registry import Registry, SessionScope, UnknownBase


def собрать(*names: str) -> AppConfig:
    bases = {
        name: BaseConfig(
            name=name, label=f"база {name}",
            url=f"http://localhost/{name}/odata/standard.odata/",
            user="u", password="p", role="prod",
        )
        for name in names
    }
    return AppConfig(home=".", default=names[0] if names else None, bases=bases,
                     daemon=DaemonConfig(gate_secret="x" * 44))


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
```

- [ ] **Шаг 2: Прогнать и убедиться в падении**

Выполнить: `uv run pytest tests/unit/test_registry.py -v`
Ожидается: `ModuleNotFoundError: No module named 'odata1c.registry.registry'`.

- [ ] **Шаг 3: Написать модуль**

Создать `src/odata1c/registry/registry.py`:

```python
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
            name: BaseState(name=name, label=base.label, role=base.role,
                            gate_mode=base.gate.mode, write=base.write)
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
```

- [ ] **Шаг 4: Прогнать тесты**

Выполнить: `uv run pytest tests/unit/test_registry.py -v`
Ожидается: все проходят.

- [ ] **Шаг 5: Зафиксировать изменения**

```bash
git add src/odata1c/registry/registry.py tests/unit/test_registry.py
git commit -m "feat: реестр баз с сужением видимости по сессии"
```

---

### Задача 5: клиент 1С OData

Единственное место, которое ходит в 1С. Здесь же семафор на базу, сеанс 1С и перевод ответов
платформы в коды ошибок SPEC §5.2. Гейт этот слой не трогает: он работает выше, над готовым ответом.

Граница задачи: `Client1C` — только транспорт. Построение запросов из SPEC §9 — умолчание и предел
`$top`, глубина `$expand` с автоматическим `$select` на раскрытых сущностях, `allowedOnly`, формы
URL составных ключей и виртуальных таблиц — относится к слою тулов и пишется планом M1d. Здесь
`path` и `params` приходят готовыми.

**Файлы:**
- Создать: `src/odata1c/client1c/client.py`
- Создать: `src/odata1c/client1c/errors.py`
- Создать: `tests/unit/test_client1c.py`

**Интерфейсы:**
- Потребляет: `BaseConfig` (задача 3).
- Отдаёт:
  - `OdataError(Exception)` с полями `code: str`, `message: str`, `hint: str`;
  - `Client1C` с методами `__init__(base: BaseConfig)`, `async get(path: str, params: dict | None = None) -> dict`,
    `async get_raw(path: str, params: dict | None = None, accept: str = "application/json",
    add_format: bool = True) -> bytes` (для `$metadata` вызывается с `add_format=False`:
    параметр `$format=json` там неуместен),
    `async post(path: str, json: dict) -> dict`, `async patch(path: str, json: dict) -> dict`,
    `async delete(path: str) -> None`, `async close() -> None`;
  - `map_error(status: int, body: str) -> OdataError` — перевод ответа 1С в код SPEC §5.2.

- [ ] **Шаг 1: Написать тесты**

Создать `tests/unit/test_client1c.py`:

```python
"""Клиент 1С: формирование запроса, сеанс, семафор, перевод ошибок."""
import asyncio

import httpx
import pytest
import respx

from odata1c.client1c.client import Client1C
from odata1c.client1c.errors import OdataError
from odata1c.config.models import BaseConfig

URL = "http://localhost/ut/odata/standard.odata/"


def база(**kwargs) -> BaseConfig:
    return BaseConfig(name="ut", label="УТ", url=URL, user="u", password="p", **kwargs)


@respx.mock
async def test_запрос_идёт_с_форматом_json_и_basic_аутентификацией():
    route = respx.get(f"{URL}Catalog_Валюты").mock(
        return_value=httpx.Response(200, json={"value": [{"Code": "643"}]})
    )
    client = Client1C(база())
    результат = await client.get("Catalog_Валюты", {"$top": 1})
    await client.close()

    assert результат == {"value": [{"Code": "643"}]}
    запрос = route.calls.last.request
    assert запрос.url.params["$format"] == "json"
    assert запрос.url.params["$top"] == "1"
    assert запрос.headers["Authorization"].startswith("Basic ")
    assert запрос.headers["Accept"] == "application/json"


@respx.mock
async def test_сеанс_запрашивается_один_раз():
    respx.get(f"{URL}Catalog_Валюты").mock(return_value=httpx.Response(200, json={"value": []}))
    client = Client1C(база(ib_session=True))
    await client.get("Catalog_Валюты")
    await client.get("Catalog_Валюты")
    await client.close()

    заголовки = [call.request.headers.get("IBSession") for call in respx.calls]
    assert заголовки[0] == "start"
    assert заголовки[1] is None


@respx.mock
async def test_семафор_ограничивает_одновременные_запросы():
    одновременно, пик = 0, 0

    async def медленный(request):
        nonlocal одновременно, пик
        одновременно += 1
        пик = max(пик, одновременно)
        await asyncio.sleep(0.05)
        одновременно -= 1
        return httpx.Response(200, json={"value": []})

    respx.get(f"{URL}Catalog_Валюты").mock(side_effect=медленный)
    client = Client1C(база(concurrency=2))
    await asyncio.gather(*(client.get("Catalog_Валюты") for _ in range(6)))
    await client.close()

    assert пик <= 2


@respx.mock
async def test_ошибка_аутентификации():
    respx.get(f"{URL}Catalog_Валюты").mock(return_value=httpx.Response(401, text="Unauthorized"))
    client = Client1C(база())
    with pytest.raises(OdataError) as ошибка:
        await client.get("Catalog_Валюты")
    await client.close()
    assert ошибка.value.code == "auth_failed"


@respx.mock
async def test_ошибка_платформы_передаётся_текстом():
    тело = {"odata.error": {"message": {"value": "Поле объекта не обнаружено (ИНН)"}}}
    respx.get(f"{URL}Catalog_Контрагенты").mock(return_value=httpx.Response(400, json=тело))
    client = Client1C(база())
    with pytest.raises(OdataError) as ошибка:
        await client.get("Catalog_Контрагенты")
    await client.close()
    assert ошибка.value.code == "odata_error"
    assert "Поле объекта не обнаружено" in ошибка.value.message


@respx.mock
async def test_неизвестная_сущность_отдаёт_свой_код():
    respx.get(f"{URL}Catalog_Нет").mock(return_value=httpx.Response(404, text="Not found"))
    client = Client1C(база())
    with pytest.raises(OdataError) as ошибка:
        await client.get("Catalog_Нет")
    await client.close()
    assert ошибка.value.code == "entity_unknown"
    assert "reindex" in ошибка.value.hint


@respx.mock
async def test_повтор_только_для_503():
    route = respx.get(f"{URL}Catalog_Валюты").mock(
        side_effect=[httpx.Response(503, text="busy"), httpx.Response(200, json={"value": []})]
    )
    client = Client1C(база())
    assert await client.get("Catalog_Валюты") == {"value": []}
    await client.close()
    assert route.call_count == 2


@respx.mock
async def test_запись_не_повторяется():
    route = respx.post(f"{URL}Catalog_Валюты").mock(return_value=httpx.Response(503, text="busy"))
    client = Client1C(база())
    with pytest.raises(OdataError):
        await client.post("Catalog_Валюты", {"Code": "643"})
    await client.close()
    assert route.call_count == 1
```

- [ ] **Шаг 2: Прогнать и убедиться в падении**

Выполнить: `uv run pytest tests/unit/test_client1c.py -v`
Ожидается: `ModuleNotFoundError: No module named 'odata1c.client1c.client'`.

- [ ] **Шаг 3: Написать перевод ошибок**

Создать `src/odata1c/client1c/errors.py`:

```python
"""Перевод ответов 1С в коды ошибок SPEC §5.2."""
from __future__ import annotations

import json


class OdataError(Exception):
    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def map_error(status: int, body: str) -> OdataError:
    текст = _текст_ошибки_платформы(body) or body.strip()[:500]
    if status in (401, 403):
        return OdataError("auth_failed", текст or "1С отклонила учётные данные",
                          "проверьте user и password базы командой odata1c base test")
    if status == 404:
        return OdataError("entity_unknown", текст or "1С не нашла указанный путь",
                          "если сущность точно есть, обновите индекс: odata1c reindex <база>")
    if status == 408 or status == 504:
        return OdataError("timeout", текст or "1С не ответила вовремя",
                          "сузьте выборку через $select и $top или увеличьте timeout_s базы")
    return OdataError("odata_error", текст or f"1С вернула HTTP {status}",
                      "текст выше — сообщение платформы")


def _текст_ошибки_платформы(body: str) -> str:
    """Из тела odata.error достать человекочитаемое сообщение."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return ""
    ошибка = data.get("odata.error") or data.get("error") or {}
    сообщение = ошибка.get("message")
    if isinstance(сообщение, dict):
        return str(сообщение.get("value", ""))
    return str(сообщение or "")
```

- [ ] **Шаг 4: Написать клиент**

Создать `src/odata1c/client1c/client.py`:

```python
"""Единственная точка обращения к OData-интерфейсу 1С (SPEC §9).

Семафор на базу общий для всех сессий, сеанс 1С (IBSession) переиспользуется, повторы — только
для сетевых ошибок и HTTP 503 и только для чтения.
"""
from __future__ import annotations

import asyncio

import httpx

from odata1c.client1c.errors import OdataError, map_error
from odata1c.config.models import BaseConfig

ПОВТОРЫ = 2
ПАУЗА_ПЕРЕД_ПОВТОРОМ_С = 0.5


class Client1C:
    def __init__(self, base: BaseConfig) -> None:
        self._base = base
        self._semaphore = asyncio.Semaphore(base.concurrency)
        self._session_started = False
        self._client = httpx.AsyncClient(
            base_url=base.url,
            auth=(base.user, base.password),
            verify=base.verify_tls,
            timeout=base.timeout_s,
            headers={"Accept": "application/json"},
        )

    async def get(self, path: str, params: dict | None = None) -> dict:
        response = await self._request("GET", path, params=params, retry=True)
        return response.json()

    async def get_raw(self, path: str, params: dict | None = None,
                      accept: str = "application/json", add_format: bool = True) -> bytes:
        """Сырой ответ. Для $metadata вызывается с add_format=False: $format=json там неуместен."""
        response = await self._request("GET", path, params=params, retry=True,
                                       headers={"Accept": accept}, add_format=add_format)
        return response.content

    async def post(self, path: str, json: dict) -> dict:
        response = await self._request("POST", path, json=json, retry=False)
        return response.json() if response.content else {}

    async def patch(self, path: str, json: dict) -> dict:
        response = await self._request("PATCH", path, json=json, retry=False)
        return response.json() if response.content else {}

    async def delete(self, path: str) -> None:
        await self._request("DELETE", path, retry=False)

    async def close(self) -> None:
        if self._session_started and self._base.ib_session:
            try:
                await self._client.get("", headers={"IBSession": "finish"})
            except httpx.HTTPError:
                pass  # завершение сеанса — вежливость, а не обязанность
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       json: dict | None = None, retry: bool, headers: dict | None = None,
                       add_format: bool = True) -> httpx.Response:
        params = dict(params or {})
        if add_format:
            params.setdefault("$format", "json")
        headers = dict(headers or {})
        if json is not None:
            headers["Content-Type"] = "application/json"

        async with self._semaphore:
            if self._base.ib_session and not self._session_started:
                headers["IBSession"] = "start"
            попытки = ПОВТОРЫ if retry else 1
            последняя: Exception | None = None
            for попытка in range(попытки):
                try:
                    response = await self._client.request(method, path, params=params,
                                                          json=json, headers=headers)
                except httpx.TimeoutException as exc:
                    последняя = OdataError("timeout", f"1С не ответила за {self._base.timeout_s} с",
                                           "увеличьте timeout_s базы или сузьте выборку")
                    if попытка + 1 == попытки:
                        raise последняя from exc
                except httpx.HTTPError as exc:
                    последняя = OdataError("odata_error", f"не удалось обратиться к 1С: {exc}",
                                           "проверьте адрес базы и доступность сервера")
                    if попытка + 1 == попытки:
                        raise последняя from exc
                else:
                    if response.status_code == 503 and попытка + 1 < попытки:
                        await asyncio.sleep(ПАУЗА_ПЕРЕД_ПОВТОРОМ_С)
                        continue
                    if response.status_code >= 400:
                        raise map_error(response.status_code, response.text)
                    self._session_started = self._session_started or self._base.ib_session
                    return response
                await asyncio.sleep(ПАУЗА_ПЕРЕД_ПОВТОРОМ_С)
            raise последняя or OdataError("odata_error", "запрос к 1С не удался")
```

- [ ] **Шаг 5: Прогнать тесты**

Выполнить: `uv run pytest tests/unit/test_client1c.py -v`
Ожидается: все проходят.

- [ ] **Шаг 6: Зафиксировать изменения**

```bash
git add src/odata1c/client1c/client.py src/odata1c/client1c/errors.py tests/unit/test_client1c.py
git commit -m "feat: клиент 1С OData с семафором, сеансом и переводом ошибок"
```

---

### Задача 6: команды CLI `init`, `base list`, `base test`

Первая команда, которую пользователь набирает руками. `base test` замыкает всю цепочку задач 2–5:
домашний каталог → настройки → реестр → живой запрос к 1С.

**Файлы:**
- Создать: `src/odata1c/cli.py`
- Создать: `src/odata1c/templates/bases.example.yaml`
- Создать: `src/odata1c/templates/daemon.example.yaml`
- Создать: `tests/unit/test_cli_base.py`

**Интерфейсы:**
- Потребляет: `resolve_home`, `ensure_home` (задача 2), `load_config` (задача 3), `Registry`,
  `SessionScope` (задача 4), `Client1C` (задача 5).
- Отдаёт:
  - `main(argv: list[str] | None = None) -> int` — точка входа консольной команды;
  - `cmd_init(home: pathlib.Path) -> int`, `cmd_base_list(home) -> int`,
    `cmd_base_test(home, name: str) -> int`;
  - файлы-шаблоны в пакете, копируемые в домашний каталог при первом запуске.

- [ ] **Шаг 1: Написать тесты**

Создать `tests/unit/test_cli_base.py`:

```python
"""Команды CLI: init, base list, base test."""
import httpx
import respx

from odata1c.cli import main

URL = "http://localhost/ut/odata/standard.odata/"
BASES = f"""
default: ut
bases:
  ut:
    label: УТ 11, тестовая
    url: {URL}
    user: u
    password: p
    role: test
"""


def test_init_создаёт_каталог_и_шаблоны(tmp_path, capsys):
    код = main(["init", "--home", str(tmp_path / "home")])
    вывод = capsys.readouterr().out

    assert код == 0
    assert (tmp_path / "home" / "bases.yaml").exists()
    assert (tmp_path / "home" / "daemon.yaml").exists()
    assert (tmp_path / "home" / "bases").is_dir()
    assert "bases.yaml" in вывод


def test_init_не_затирает_существующие_настройки(tmp_path):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    main(["init", "--home", str(home)])
    assert "УТ 11, тестовая" in (home / "bases.yaml").read_text(encoding="utf-8")


def test_base_list_показывает_роль_и_уровень_гейта(tmp_path, capsys):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")

    код = main(["base", "list", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "ut" in вывод
    assert "test" in вывод
    assert "identifiers" in вывод


def test_base_list_без_баз_подсказывает_куда_писать(tmp_path, capsys):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text("bases: {}\n", encoding="utf-8")

    main(["base", "list", "--home", str(home)])
    вывод = capsys.readouterr().out
    assert "bases.yaml" in вывод
    assert "base import" in вывод


@respx.mock
def test_base_test_докладывает_успех(tmp_path, capsys):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    respx.get(f"{URL}$metadata").mock(
        return_value=httpx.Response(200, text="<edmx:Edmx/>",
                                    headers={"Content-Type": "application/xml"})
    )

    код = main(["base", "test", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "соединение установлено" in вывод.lower()


@respx.mock
def test_base_test_докладывает_отказ_аутентификации(tmp_path, capsys):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    respx.get(f"{URL}$metadata").mock(return_value=httpx.Response(401, text="Unauthorized"))

    код = main(["base", "test", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "auth_failed" in вывод


def test_base_test_неизвестной_базы(tmp_path, capsys):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")

    код = main(["base", "test", "нет_такой", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "base_unknown" in вывод
    assert "ut" in вывод  # подсказка со списком доступных
```

- [ ] **Шаг 2: Прогнать и убедиться в падении**

Выполнить: `uv run pytest tests/unit/test_cli_base.py -v`
Ожидается: `ModuleNotFoundError: No module named 'odata1c.cli'`.

- [ ] **Шаг 3: Написать шаблоны настроек**

Создать `src/odata1c/templates/bases.example.yaml` — шаблон из SPEC §3.1 дословно, включая
комментарии к каждому параметру и закомментированные примеры баз `buh` и `ut_test`. Правило шаблона
(SPEC §3.1): у каждого параметра комментарий с назначением и допустимыми значениями; параметры,
совпадающие с умолчанием роли, закомментированы, но видны как возможность.

Создать `src/odata1c/templates/daemon.example.yaml`:

```yaml
# odata1c: настройки демона. Файл необязателен, создаётся с умолчаниями при первом запуске.
port: 7171                     # демон слушает только 127.0.0.1
# gate_secret заполняется автоматически при первом запуске: 32 случайных байта в base64.
# Потеря секрета не ломает работу, но токены реквизитов из старых чатов перестанут совпадать.
limits:
  top_default: 50              # $top по умолчанию
  top_max: 1000                # предел $top
  expand_depth: 2              # предельная глубина $expand
  result_chars: 120000         # предельный размер результата тула в символах
  string_chars: 2000           # предельная длина одной строки в ответе
  pending_ttl_s: 600           # сколько живёт подготовленная операция записи
write_confirm_fallback: deny   # deny | trust_client — как быть с клиентом без elicitation
reindex_check_hours: 24        # как часто фоново сверять хэш $metadata
```

- [ ] **Шаг 4: Написать CLI**

Создать `src/odata1c/cli.py`:

```python
"""Командная строка odata1c (SPEC §3.5).

В этой задаче реализованы init, base list и base test; остальные команды добавляются
следующими задачами и планами.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.resources
import pathlib
import sys

from odata1c.client1c.client import Client1C
from odata1c.client1c.errors import OdataError
from odata1c.config.home import ensure_home, resolve_home
from odata1c.config.loader import ConfigError, load_config
from odata1c.registry.registry import Registry, SessionScope

ШАБЛОНЫ = {"bases.yaml": "bases.example.yaml", "daemon.yaml": "daemon.example.yaml"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="odata1c", description="Шлюз к OData 1С с гейтом")
    parser.add_argument("--home", help="домашний каталог шлюза (иначе ODATA1C_HOME или ~/.claude/odata1c)")
    команды = parser.add_subparsers(dest="команда", required=True)

    команды.add_parser("init", help="создать домашний каталог и шаблоны настроек")

    base = команды.add_parser("base", help="работа с базами")
    подкоманды = base.add_subparsers(dest="подкоманда", required=True)
    подкоманды.add_parser("list", help="список описанных баз")
    test = подкоманды.add_parser("test", help="проверить соединение с базой")
    test.add_argument("name", help="имя базы из bases.yaml")

    args = parser.parse_args(argv)
    home = resolve_home(args.home)

    try:
        if args.команда == "init":
            return cmd_init(home)
        if args.команда == "base" and args.подкоманда == "list":
            return cmd_base_list(home)
        if args.команда == "base" and args.подкоманда == "test":
            return cmd_base_test(home, args.name)
    except ConfigError as ошибка:
        print(f"[{ошибка.code}] {ошибка}")
        if ошибка.hint:
            print(f"подсказка: {ошибка.hint}")
        return 1
    return 2


def cmd_init(home: pathlib.Path) -> int:
    status = ensure_home(home)
    for имя_файла, имя_шаблона in ШАБЛОНЫ.items():
        назначение = home / имя_файла
        if назначение.exists():
            continue
        шаблон = importlib.resources.files("odata1c.templates").joinpath(имя_шаблона)
        назначение.write_text(шаблон.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"домашний каталог: {home}")
    print(f"опишите базы в {home / 'bases.yaml'}")
    print("перенести базы из прежнего сервера: odata1c base import <путь к 1c-odata.env>")
    if status.warning:
        print(f"предупреждение: {status.warning}")
    return 0


def cmd_base_list(home: pathlib.Path) -> int:
    config = load_config(home)
    for предупреждение in config.warnings:
        print(f"предупреждение: {предупреждение}")
    registry = Registry(config)
    состояния = registry.visible(SessionScope())
    if not состояния:
        print(f"баз не описано; опишите их в {home / 'bases.yaml'}")
        print("или перенесите из прежнего сервера: odata1c base import <путь к 1c-odata.env>")
        return 0
    print(f"{'база':<16}{'роль':<8}{'гейт':<20}{'запись':<8}подпись")
    for состояние in состояния:
        запись = "да" if состояние.write else "нет"
        по_умолчанию = " (по умолчанию)" if состояние.name == config.default else ""
        print(f"{состояние.name:<16}{состояние.role:<8}{состояние.gate_mode:<20}"
              f"{запись:<8}{состояние.label}{по_умолчанию}")
    return 0


def cmd_base_test(home: pathlib.Path, name: str) -> int:
    config = load_config(home)
    registry = Registry(config)
    base = registry.get(name, SessionScope())
    return asyncio.run(_проверить_соединение(base))


async def _проверить_соединение(base) -> int:
    client = Client1C(base)
    try:
        данные = await client.get_raw("$metadata", accept="application/xml", add_format=False)
    except OdataError as ошибка:
        print(f"[{ошибка.code}] {ошибка.message}")
        if ошибка.hint:
            print(f"подсказка: {ошибка.hint}")
        return 1
    finally:
        await client.close()
    print(f"база {base.name}: соединение установлено, роль {base.role}, "
          f"уровень гейта {base.gate.mode}")
    print(f"$metadata получен: {len(данные) / 1024:.1f} КБ; "
          f"следующий шаг — odata1c reindex {base.name}")
    return 0
```

- [ ] **Шаг 5: Прогнать тесты**

Выполнить: `uv run pytest tests/unit/test_cli_base.py -v`
Ожидается: все проходят.

Проверить руками:
```bash
uv run odata1c init --home .tmp/home
uv run odata1c base list --home .tmp/home
```

- [ ] **Шаг 6: Зафиксировать изменения**

```bash
git add src/odata1c/cli.py src/odata1c/templates/ tests/unit/test_cli_base.py
git commit -m "feat: команды CLI init, base list, base test"
```

---

### Задача 7: `base add` и `base import`

`base import` переносит базы из env-файла прежнего сервера `1c-odata-mcp` (SPEC §3.5) — это путь
установки для того, у кого он уже настроен. `base add` дописывает запись в том же стиле, что и
шаблон: с комментариями и закомментированными умолчаниями.

**Файлы:**
- Изменить: `src/odata1c/cli.py`
- Создать: `src/odata1c/config/writer.py`
- Создать: `src/odata1c/config/importer.py`
- Создать: `tests/unit/test_config_import.py`

**Интерфейсы:**
- Потребляет: `BaseConfig` (задача 3), `cmd_init` (задача 6).
- Отдаёт:
  - `parse_env(text: str) -> tuple[str | None, list[dict]]` — база по умолчанию и записи баз из
    env-файла;
  - `render_base(name: str, values: dict) -> str` — запись базы в стиле шаблона;
  - `append_base(path: pathlib.Path, name: str, values: dict) -> None` — дописать запись, не тронув
    остальной файл и комментарии;
  - команды `odata1c base add <name> [--role prod] [--recipes ut|bp|zup]` и
    `odata1c base import <path>`.

- [ ] **Шаг 1: Написать тесты**

Создать `tests/unit/test_config_import.py`:

```python
"""Перенос баз из env-файла прежнего сервера и дописывание записей в bases.yaml."""
import yaml

from odata1c.config.importer import parse_env
from odata1c.config.writer import append_base

ENV = """
ODATA_DEFAULT_DB=ut
READ_ONLY=false

ODATA_DB_UT_BASE_URL=https://1c.corp.local/ut/odata/standard.odata/
ODATA_DB_UT_USERNAME=odata_claude
ODATA_DB_UT_PASSWORD=секрет
ODATA_DB_UT_LABEL=УТ 11, боевая
ODATA_DB_UT_WRITABLE=true

ODATA_DB_BUH_BASE_URL=https://1c.corp.local/buh/odata/standard.odata/
ODATA_DB_BUH_USERNAME=odata_claude
ODATA_DB_BUH_PASSWORD=секрет2
ODATA_DB_BUH_LABEL=БП 3.0
"""


def test_разбор_env_файла():
    по_умолчанию, базы = parse_env(ENV)
    assert по_умолчанию == "ut"
    имена = {b["name"] for b in базы}
    assert имена == {"ut", "buh"}
    ut = next(b for b in базы if b["name"] == "ut")
    assert ut["url"] == "https://1c.corp.local/ut/odata/standard.odata/"
    assert ut["user"] == "odata_claude"
    assert ut["label"] == "УТ 11, боевая"
    assert ut["write"] is True
    assert ut["role"] == "prod"


def test_база_без_writable_не_пишущая():
    _, базы = parse_env(ENV)
    buh = next(b for b in базы if b["name"] == "buh")
    assert buh["write"] is False


def test_имя_базы_приводится_к_допустимому():
    env = "ODATA_DB_UT-ROZNICA_BASE_URL=http://x/odata/standard.odata/\n"
    _, базы = parse_env(env)
    assert базы[0]["name"] == "ut_roznica"


def test_дописывание_не_ломает_существующий_файл(tmp_path):
    path = tmp_path / "bases.yaml"
    path.write_text(
        "# комментарий шаблона\ndefault: ut\nbases:\n  ut:\n    label: УТ\n"
        "    url: http://x/odata/standard.odata/\n    user: u\n    password: p\n    role: prod\n",
        encoding="utf-8",
    )
    append_base(path, "buh", {"label": "БП", "url": "http://y/odata/standard.odata/",
                              "user": "u2", "password": "p2", "role": "prod"})

    текст = path.read_text(encoding="utf-8")
    assert "# комментарий шаблона" in текст
    данные = yaml.safe_load(текст)
    assert set(данные["bases"]) == {"ut", "buh"}
    assert данные["bases"]["buh"]["label"] == "БП"


def test_запись_добавляется_с_комментариями(tmp_path):
    path = tmp_path / "bases.yaml"
    path.write_text("bases:\n", encoding="utf-8")
    append_base(path, "ut", {"label": "УТ", "url": "http://x/odata/standard.odata/",
                             "user": "u", "password": "p", "role": "prod"})
    текст = path.read_text(encoding="utf-8")
    assert "# --- соединение" in текст
    assert "# concurrency:" in текст
```

- [ ] **Шаг 2: Прогнать и убедиться в падении**

Выполнить: `uv run pytest tests/unit/test_config_import.py -v`
Ожидается: `ModuleNotFoundError: No module named 'odata1c.config.importer'`.

- [ ] **Шаг 3: Написать разбор env-файла**

Создать `src/odata1c/config/importer.py`:

```python
"""Перенос баз из env-файла прежнего сервера 1c-odata-mcp (SPEC §3.5).

Читаются ключи ODATA_DB_<NAME>_BASE_URL | _USERNAME | _PASSWORD | _LABEL | _WRITABLE,
а также ODATA_DEFAULT_DB и READ_ONLY. _WRITABLE=true даёт write: true, роль по умолчанию prod.
"""
from __future__ import annotations

import re

КЛЮЧ = re.compile(r"^ODATA_DB_(?P<база>[A-Z0-9_\-]+)_(?P<поле>BASE_URL|USERNAME|PASSWORD|LABEL|WRITABLE)$")
ПОЛЯ = {"BASE_URL": "url", "USERNAME": "user", "PASSWORD": "password", "LABEL": "label"}


def normalize_name(raw: str) -> str:
    """Имя базы в env написано заглавными и может содержать дефис: приводим к [a-z0-9_]."""
    имя = re.sub(r"[^a-z0-9_]", "_", raw.lower())
    return имя[:32] or "base"


def parse_env(text: str) -> tuple[str | None, list[dict]]:
    значения: dict[str, dict] = {}
    по_умолчанию: str | None = None
    только_чтение = False

    for строка in text.splitlines():
        строка = строка.strip()
        if not строка or строка.startswith("#") or "=" not in строка:
            continue
        ключ, _, значение = строка.partition("=")
        ключ, значение = ключ.strip(), значение.strip().strip('"').strip("'")

        if ключ == "ODATA_DEFAULT_DB":
            по_умолчанию = normalize_name(значение)
            continue
        if ключ == "READ_ONLY":
            только_чтение = значение.lower() in ("1", "true", "yes")
            continue

        совпадение = КЛЮЧ.match(ключ)
        if not совпадение:
            continue
        имя = normalize_name(совпадение["база"])
        запись = значения.setdefault(имя, {"name": имя, "role": "prod", "write": False})
        поле = совпадение["поле"]
        if поле == "WRITABLE":
            запись["write"] = значение.lower() in ("1", "true", "yes")
        else:
            запись[ПОЛЯ[поле]] = значение

    базы = [запись for запись in значения.values() if "url" in запись]
    for запись in базы:
        запись.setdefault("label", запись["name"])
        запись.setdefault("user", "")
        запись.setdefault("password", "")
        if только_чтение:
            запись["write"] = False
    return по_умолчанию, базы
```

- [ ] **Шаг 4: Написать дописывание записи**

Создать `src/odata1c/config/writer.py`:

```python
"""Дописывание записи базы в bases.yaml в стиле шаблона: с комментариями, не трогая остальное."""
from __future__ import annotations

import pathlib

ШАБЛОН_ЗАПИСИ = """\
  {name}:
    label: {label}
    url: {url}
    user: {user}
    password: "{password}"
    role: {role}

    # --- соединение (умолчания показаны, раскомментируйте для изменения) ---
    # verify_tls: true               # true | false | путь к CA-сертификату (PEM)
    # timeout_s: 60                  # таймаут обычного запроса; виртуальные таблицы — 180
    # concurrency: 2                 # одновременных запросов к этой базе от всех сессий
    # ib_session: true               # держать сеанс 1С (IBSession) между запросами

    # --- запись (умолчание роли {role}) ---
{write_line}
    # permissions:
    #   post_documents: true
    #   mark_deletion: true
    #   independent_register_delete: false
    #   register_direct_write: false
    #   deny_entities: []
    #   deny_fields: []

    # --- гейт (умолчание роли {role}) ---
    # gate:
    #   mode: identifiers+names      # off | identifiers | identifiers+names
"""


def render_base(name: str, values: dict) -> str:
    write = values.get("write")
    write_line = ("    write: true                    # разрешить пишущие тулы"
                  if write else "    # write: false                 # разрешить пишущие тулы")
    return ШАБЛОН_ЗАПИСИ.format(
        name=name,
        label=values.get("label", name),
        url=values["url"],
        user=values.get("user", ""),
        password=values.get("password", ""),
        role=values.get("role", "prod"),
        write_line=write_line,
    )


def append_base(path: pathlib.Path, name: str, values: dict) -> None:
    """Дописать базу в конец раздела bases, сохранив комментарии остального файла."""
    текст = path.read_text(encoding="utf-8") if path.exists() else ""
    if "bases:" not in текст:
        текст = (текст + "\n" if текст and not текст.endswith("\n") else текст) + "bases:\n"
    if not текст.endswith("\n"):
        текст += "\n"
    path.write_text(текст + "\n" + render_base(name, values), encoding="utf-8")
```

- [ ] **Шаг 5: Подключить команды к CLI**

В `src/odata1c/cli.py` добавить в разбор аргументов, рядом с существующими подкомандами `base`:

```python
    add = подкоманды.add_parser("add", help="добавить базу")
    add.add_argument("name", help="имя базы: строчные латинские буквы, цифры, подчёркивание")
    add.add_argument("--role", choices=("prod", "test", "dev"), default="prod")
    add.add_argument("--recipes", choices=("ut", "bp", "zup"),
                     help="скопировать шаблон рецептов для типовой конфигурации")
    импорт = подкоманды.add_parser("import", help="перенести базы из env-файла прежнего сервера")
    импорт.add_argument("path", help="путь к 1c-odata.env")
```

И обработку в `main`, рядом с существующими ветками:

```python
        if args.команда == "base" and args.подкоманда == "add":
            return cmd_base_add(home, args.name, args.role, args.recipes)
        if args.команда == "base" and args.подкоманда == "import":
            return cmd_base_import(home, pathlib.Path(args.path))
```

И сами команды:

```python
def cmd_base_add(home: pathlib.Path, name: str, role: str, recipes: str | None) -> int:
    cmd_init(home)
    print(f"добавляю базу «{name}» с ролью {role}")
    url = input("адрес (оканчивается на /odata/standard.odata/): ").strip()
    values = {
        "label": input("подпись для модели: ").strip() or name,
        "url": url,
        "user": input("пользователь 1С: ").strip(),
        "password": getpass.getpass("пароль 1С (не отображается): "),
        "role": role,
    }
    BaseConfig(name=name, **values)  # проверка имени и адреса до записи в файл
    append_base(home / "bases.yaml", name, values)
    print(f"база «{name}» дописана в {home / 'bases.yaml'}")
    if recipes:
        _скопировать_рецепты(home, name, recipes)
    print(f"проверить соединение: odata1c base test {name}")
    return 0


def cmd_base_import(home: pathlib.Path, path: pathlib.Path) -> int:
    if not path.exists():
        print(f"файл не найден: {path}")
        return 1
    cmd_init(home)
    по_умолчанию, базы = parse_env(path.read_text(encoding="utf-8"))
    if not базы:
        print(f"в {path} не нашлось ключей ODATA_DB_<ИМЯ>_BASE_URL")
        return 1
    существующие = set((load_config(home)).bases)
    добавлено = 0
    for запись in базы:
        имя = запись.pop("name")
        if имя in существующие:
            print(f"база «{имя}» уже описана, пропускаю")
            continue
        append_base(home / "bases.yaml", имя, запись)
        добавлено += 1
        print(f"перенесена база «{имя}»: {запись['url']}")
    if по_умолчанию and добавлено:
        _записать_базу_по_умолчанию(home / "bases.yaml", по_умолчанию)
    print(f"перенесено баз: {добавлено}; проверьте: odata1c base list")
    return 0


def _записать_базу_по_умолчанию(path: pathlib.Path, name: str) -> None:
    текст = path.read_text(encoding="utf-8")
    if текст.lstrip().startswith("default:") or "\ndefault:" in текст:
        return
    path.write_text(f"default: {name}\n{текст}", encoding="utf-8")


def _скопировать_рецепты(home: pathlib.Path, name: str, шаблон: str) -> None:
    источник = importlib.resources.files("odata1c.templates.recipes").joinpath(f"{шаблон}.yaml")
    назначение = home / "bases" / name / "recipes.yaml"
    назначение.parent.mkdir(parents=True, exist_ok=True)
    if назначение.exists():
        print(f"рецепты уже есть: {назначение}, не трогаю")
        return
    назначение.write_text(источник.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"скопированы рецепты {шаблон}: {назначение}")
```

Дописать импорты в начало `cli.py`:

```python
import getpass

from odata1c.config.importer import parse_env
from odata1c.config.models import BaseConfig
from odata1c.config.writer import append_base
```

Примечание: шаблоны рецептов (`src/odata1c/templates/recipes/ut.yaml`, `bp.yaml`, `zup.yaml`)
наполняются планом M1d вместе с тулом `odata1c_recipe`; до этого `--recipes` копирует пустой
шаблон с заголовком `version: 1` и пустым разделом `recipes:`. Создать эти три файла с таким
содержимым в этой задаче, чтобы команда не падала.

- [ ] **Шаг 6: Прогнать тесты**

Выполнить: `uv run pytest tests/unit -v`
Ожидается: все проходят, включая тесты предыдущих задач.

- [ ] **Шаг 7: Зафиксировать изменения**

```bash
git add src/odata1c/cli.py src/odata1c/config/writer.py src/odata1c/config/importer.py src/odata1c/templates/recipes/ tests/unit/test_config_import.py
git commit -m "feat: команды base add и base import"
```

---

## Критерий готовности M1a

- `uv run pytest` — зелёный прогон всех юнит-тестов;
- `uv run odata1c init` создаёт домашний каталог с закрытыми правами и двумя файлами-шаблонами;
- `uv run odata1c base import <путь к 1c-odata.env>` переносит базы прежнего сервера;
- `uv run odata1c base list` показывает базы с ролью, уровнем гейта и признаком записи;
- `uv run odata1c base test <имя>` устанавливает соединение с базой 1С и получает `$metadata`,
  а при неверных учётных данных докладывает `auth_failed` с подсказкой.

Последний пункт проверяется на живой тестовой базе. Пока её нет, критерий закрывается тестами с
подменой HTTP (`respx`), и в отчёте о задаче 6 это отмечается явно.
