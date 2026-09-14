# M2b — политика владельца и конструктор (ADR-0015): план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Две точки управления владельца: `bases.yaml` («кто база, как с ней работать, скрывать ли») и `bases/<база>/policy.yaml` («что скрывать и что открыть», только владельцу, из шаблона со всеми классами); авторазметка реиндекса в `policy.auto.yaml`; `bases.yaml` действует без перезапуска демона; правила в политику добавляет конструктор `odata1c policy check | hide | open | set` и навык плагина.

**Architecture:** Файл владельца `policy.yaml` машина не переписывает; реиндекс пишет `policy.auto.yaml`; `load_policy(path, auto_path)` собирает действующую политику (правила владельца поверх авторазметки, приоритет `fields` > `custom` > `defaults` > авторазметка). `ToolService` перечитывает `bases.yaml` по отметке файла перед каждым вызовом тула; непригодный файл закрывает все тулы кодом `config_invalid`; `commit` перепроверяет разрешения по текущему файлу. Запись в `policy.yaml` только через `ruamel.yaml` (обратимый разбор, комментарии сохраняются) и только из CLI: тула MCP для изменения политики нет.

**Tech Stack:** Python 3.12, `uv`, `pydantic` 2, `pyyaml` (чтение), `ruamel.yaml` ≥ 0.18 (запись в файл владельца), `pytest`, `ruff` (line-length 100).

**Spec:** `docs/adr/0015-owner-policy-file-per-base-auto-split-hot-reload.md`, `docs/plans/2026-09-13-bases-yaml-single-control-point.md` (целевые шаблоны, сценарии), SPEC §2.3, §3.1, §3.5, §4.3, §6.5, §6.9, §7.1, §11.2.

## Global Constraints

- Язык кода, комментариев, тестов, сообщений — русский; идентификаторы в коде могут быть русскими, как в существующих модулях. Терминология по `CONTEXT.md`: политика, авторазметка, конструктор политики, гейт, уровень, класс, разрешения. Запрещённые синонимы (`_Avoid_`) не использовать.
- Инвариант 1: ни одно сообщение, предупреждение, текст ошибки или строка журнала не содержит значений данных 1С; в отказах и подсказках — только имена сущностей, полей, классов и путей.
- Инвариант 6: ссылки, коды, номера, даты, суммы не классифицируются; `classify_field` не менять.
- `filterwarnings = ["error"]` в pytest: любое предупреждение роняет тест.
- Тесты: `uv run pytest tests/unit -q` (быстрые), полный прогон `uv run pytest -q` перед закрытием задачи; линт `uv run ruff check . && uv run ruff format --check .`.
- Коммиты небольшие, сообщения в стиле репозитория (`feat:`, `fix:`, `docs:`, `test:`), с завершающей строкой `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Ветка — текущая `m0-probes` (так ведутся все этапы).
- Ничего не писать в рабочий домашний каталог владельца `~/.claude/odata1c/` из тестов: только `tmp_path`.
- Пути внутри домашнего каталога собирать функциями `base_dir`, `policy_path`, `auto_policy_path`, `index_path`, а не строками.

---

### Task 1: `gate` в `bases.yaml` только `mode`; `names_for` читается из политики

**Files:**
- Modify: `src/odata1c/config/models.py:31-36` (`GateSettings`)
- Modify: `src/odata1c/config/loader.py:15-49` (`УМОЛЧАНИЯ_РОЛЕЙ`, если в `gate` есть что-то кроме `mode`)
- Modify: `src/odata1c/templates/bases.example.yaml` (блок `# --- гейт`)
- Modify: `src/odata1c/gate/service.py:36-44` (`classifier_for`), `:66-67` (`refresh_policy`, чтение `names_for`)
- Modify: `src/odata1c/cli.py:289`, `src/odata1c/tools/service.py:1796` (вызовы `classifier_for`)
- Test: `tests/unit/test_config_loader.py`, `tests/unit/test_gate_service.py`; правки тестов, где строится `GateSettings(names_for=…)` или `scan_free_text=…`: `tests/unit/test_config_import.py`, `test_gate_masking.py`, `test_gate_pipeline.py`, `test_gate_contact_info.py`, `test_gate_literal_tokens.py`, `test_gate_field_rules.py`, `test_tools_service.py`, `test_cli_policy.py`, `tests/property/test_gate_properties.py`, `tests/property/test_gate_differential.py`

**Interfaces:**
- Produces: `GateSettings(mode=…)` — единственное поле; `classifier_for(home: pathlib.Path, base: BaseConfig)`; `owner_names_for(home: pathlib.Path, base_name: str) -> set[str] | None` в `gate/service.py`.

- [ ] **Step 1: Тест на отклонение переехавших ключей**

В `tests/unit/test_config_loader.py` добавить:

```python
def test_gate_names_for_в_bases_отклоняется_с_подсказкой(tmp_path):
    (tmp_path / "daemon.yaml").write_text("port: 7171\ngate_secret: 'AAAA'\n", encoding="utf-8")
    (tmp_path / "bases.yaml").write_text(
        "bases:\n  ut:\n    label: t\n    url: http://x/odata/standard.odata/\n"
        "    user: u\n    role: dev\n    gate:\n      names_for: [Catalog_Контрагенты]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as ошибка:
        load_config(tmp_path)
    assert ошибка.value.code == "config_invalid"
    assert "policy.yaml" in str(ошибка.value)
    assert "names_for" in str(ошибка.value)
```

То же для `scan_free_text` (второй тест, копия с другим ключом).

- [ ] **Step 2: Запустить, убедиться, что падает** — `uv run pytest tests/unit/test_config_loader.py -q -k переехав` → FAIL (сейчас ключ принимается).

- [ ] **Step 3: Реализация модели**

```python
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
```

Проверить, что `format_validation_error` в `loader.py:212` включает текст `ValueError` в сообщение `ConfigError` (иначе дописать). В `УМОЛЧАНИЯ_РОЛЕЙ` оставить в `gate` только `mode`.

- [ ] **Step 4: `names_for` из политики**

В `gate/service.py`:

```python
def owner_names_for(home: pathlib.Path, base_name: str) -> set[str] | None:
    """Список `names_for` из файла владельца; нет файла или раздела — `None` (встроенный список,
    `field_rules.DEFAULT_NAMES_FOR`)."""
    return load_policy(policy_path(home, base_name)).names_for()


def classifier_for(home: pathlib.Path, base: BaseConfig):
    список = owner_names_for(home, base.name)

    def классификатор(entity: str, field: str, edm_type: str):
        return classify_field(entity, field, edm_type, names_for=список)

    return классификатор
```

В `refresh_policy` заменить `список = set(base.gate.names_for) …` на `список = owner_names_for(home, base.name)`. Вызовы: `cli.py:289` → `classifier_for(home, base)`, `tools/service.py:1796` → `classifier_for(self._config.home, base_config)`.

- [ ] **Step 5: Шаблон `bases.example.yaml`** — блок гейта заменить на:

```yaml
    # --- гейт: скрывать ли (умолчание роли prod: identifiers+names) ---
    # gate:
    #   mode: identifiers+names      # off | identifiers | identifiers+names, см. SPEC §6.2
    #                                # что именно скрывать и что открыть: bases/ut/policy.yaml
```

- [ ] **Step 6: Починить тесты, строившие `GateSettings(names_for=…)`** — в каждом файле из списка выше заменить конструкцию: где тест проверял влияние `names_for`, писать `names_for` в `policy.yaml` временного дома (`tmp_path / "bases" / имя / "policy.yaml"` с `names_for: [...]`) и передавать `policy_path`; где просто конструировался объект — убрать ключ. Прогнать `uv run pytest tests/unit -q` и `uv run pytest tests/property -q` до зелёного.

- [ ] **Step 7: Коммит** — `git add -A src tests && git commit -m "feat(config): gate в bases.yaml только mode; names_for и scan_free_text переехали в policy.yaml (ADR-0015, задача 1)"`.

---

### Task 2: Два файла политики: `policy.yaml` владельца и `policy.auto.yaml` реиндекса

**Files:**
- Modify: `pyproject.toml:13-27` (добавить `"ruamel.yaml>=0.18"`)
- Modify: `src/odata1c/gate/policy.py` (`load_policy`, новые `read_auto`, `dump_auto`, `strip_auto_section`, `merge_defaults`)
- Modify: `src/odata1c/gate/service.py` (`auto_policy_path`, `refresh_policy`)
- Modify: `src/odata1c/gate/pipeline.py:83-125` (`BaseGate`: два файла)
- Modify: `src/odata1c/tools/service.py:471-481` (`_gate_for`: передать `auto_path`), `:1758-1900` (`reindex`: пути, текст предупреждения без изменений)
- Modify: `src/odata1c/cli.py:295-305` (сообщение «политика обновлена» → путь авторазметки)
- Test: `tests/unit/test_gate_policy.py`, `tests/unit/test_gate_service.py`, `tests/unit/test_gate_pipeline.py`

**Interfaces:**
- Produces: `auto_policy_path(home, base_name) -> Path` (`bases/<name>/policy.auto.yaml`); `load_policy(path, auto_path=None) -> Policy`; `read_auto(path) -> dict` (ключи `defaults`, `auto`; нет файла → `{}`); `dump_auto(path, *, defaults: dict, auto: dict) -> None`; `strip_auto_section(path) -> bool`; `BaseGate(base=…, dictionary=…, guard=…, policy_path=…, auto_path=…)`.
- Consumes: `owner_names_for` из задачи 1.

- [ ] **Step 1: Тесты сборки политики из двух файлов** (`tests/unit/test_gate_policy.py`):

```python
def test_политика_из_двух_файлов_приоритет_владельца(tmp_path):
    владелец = tmp_path / "policy.yaml"
    авто = tmp_path / "policy.auto.yaml"
    владелец.write_text(
        "version: 2\nfields:\n  Catalog_Контрагенты.КодПоОКПО: keep\n"
        "defaults:\n  addr:\n    mask_for: [Catalog_Партнеры]\n",
        encoding="utf-8",
    )
    авто.write_text(
        "version: 2\ndefaults:\n  corr: keep\n  bic: keep\n  addr:\n    mask_for: [Catalog_ФизическиеЛица]\n"
        "auto:\n  Catalog_Контрагенты.КодПоОКПО: org\n  Catalog_Контрагенты.ИНН: inn\n",
        encoding="utf-8",
    )
    политика = load_policy(владелец, авто)
    assert политика.sensitivity_of("Catalog_Контрагенты", "КодПоОКПО") == "keep"
    assert политика.sensitivity_of("Catalog_Контрагенты", "ИНН") == "inn"
    assert политика.addr_masked("Catalog_Партнеры") and политика.addr_masked("Catalog_ФизическиеЛица")


def test_без_файла_авторазметки_политика_владельца_работает(tmp_path):
    владелец = tmp_path / "policy.yaml"
    владелец.write_text("version: 2\nentities:\n  Catalog_X: {hide: true}\n", encoding="utf-8")
    политика = load_policy(владелец, tmp_path / "policy.auto.yaml")
    assert политика.is_hidden("Catalog_X")


def test_strip_auto_section_сохраняет_комментарии(tmp_path):
    файл = tmp_path / "policy.yaml"
    файл.write_text(
        "# шапка владельца\nversion: 2\nfields: {}   # мои правила\nauto:\n  Catalog_A.B: inn\n",
        encoding="utf-8",
    )
    assert strip_auto_section(файл) is True
    текст = файл.read_text(encoding="utf-8")
    assert "# шапка владельца" in текст and "# мои правила" in текст
    assert "auto:" not in текст and "Catalog_A.B" not in текст
    assert strip_auto_section(файл) is False
```

- [ ] **Step 2: Запустить** — `uv run pytest tests/unit/test_gate_policy.py -q -k "двух_файлов or без_файла or strip_auto"` → FAIL (`load_policy` не принимает второй аргумент).

- [ ] **Step 3: Реализация в `gate/policy.py`**

```python
АВТОРАЗМЕТКА_ШАПКА = (
    "# odata1c: авторазметка гейта — классы полей, вычисленные реиндексом по индексу (SPEC §4.3).\n"
    "# НЕ РЕДАКТИРОВАТЬ: файл пересобирается каждым реиндексом. Правила владельца — в policy.yaml рядом.\n"
)


def read_auto(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    данные = _разобрать_yaml(path)
    _проверить_тип_раздела(данные, "auto", path)
    _проверить_тип_раздела(данные, "defaults", path)
    return {"defaults": данные.get("defaults") or {}, "auto": данные.get("auto") or {}}


def dump_auto(path: pathlib.Path, *, defaults: dict, auto: dict) -> None:
    тело = yaml.safe_dump(
        {"version": 2, "defaults": defaults, "auto": auto}, allow_unicode=True, sort_keys=True
    )
    временный = path.with_suffix(".yaml.new")
    временный.write_text(АВТОРАЗМЕТКА_ШАПКА + тело, encoding="utf-8")
    os.replace(временный, path)


def merge_defaults(авто: dict, владелец: dict) -> dict:
    """Умолчания классов: владелец поверх авторазметки; `addr.mask_for` — объединение списков."""
    итог = copy.deepcopy(авто)
    for ключ, значение in владелец.items():
        if ключ == "addr" and isinstance(значение, dict):
            адрес = итог.setdefault("addr", {})
            список = list(dict.fromkeys([*(адрес.get("mask_for") or []), *(значение.get("mask_for") or [])]))
            адрес.update({к: в for к, в in значение.items() if к != "mask_for"})
            адрес["mask_for"] = список
        else:
            итог[ключ] = значение
    return итог


def strip_auto_section(path: pathlib.Path) -> bool:
    """Унести раздел `auto` из файла владельца (первый реиндекс новой версии, SPEC §4.3):
    обратимый разбор ruamel сохраняет комментарии. `True` — раздел был и удалён."""
    from ruamel.yaml import YAML

    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    with path.open(encoding="utf-8") as f:
        данные = yaml_rt.load(f)
    if not isinstance(данные, dict) or "auto" not in данные:
        return False
    del данные["auto"]
    временный = path.with_suffix(".yaml.new")
    with временный.open("w", encoding="utf-8") as f:
        yaml_rt.dump(данные, f)
    os.replace(временный, path)
    return True
```

`load_policy(path, auto_path=None)`: как сейчас читает файл владельца; если `auto_path` задан — `авто = read_auto(auto_path)`, затем `_auto = {**данные.get("auto", {}), **авто["auto"]}` (раздел `auto` в файле владельца ещё возможен до первого реиндекса новой версии и уступает файлу авторазметки) и `_defaults = merge_defaults(авто["defaults"], данные.get("defaults") or {})`. Без `auto_path` поведение прежнее (тесты старого формата продолжают проходить).

- [ ] **Step 4: `refresh_policy` в `gate/service.py`**

```python
def auto_policy_path(home: pathlib.Path, base_name: str) -> pathlib.Path:
    return base_dir(home, base_name) / "policy.auto.yaml"


def refresh_policy(home: pathlib.Path, base: BaseConfig) -> list[dict]:
    путь_владельца = policy_path(home, base.name)
    путь_авто = auto_policy_path(home, base.name)
    путь_авто.parent.mkdir(parents=True, exist_ok=True)
    хранилище = IndexRepository(index_path(home, base.name))
    try:
        собранное = generate_policy(хранилище, names_for=owner_names_for(home, base.name))
    finally:
        хранилище.close()
    прежнее = read_auto(путь_авто)
    прежний_auto = прежнее.get("auto") or {}
    if not прежний_auto and путь_владельца.exists():
        # первый реиндекс новой версии: раздел auto ещё лежит в файле владельца
        прежний_auto = (_разобрать_yaml(путь_владельца).get("auto") or {})
    новое = {"defaults": собранное["defaults"], "auto": собранное["auto"]}
    if прежнее != новое:
        dump_auto(путь_авто, defaults=новое["defaults"], auto=новое["auto"])
    if путь_владельца.exists():
        strip_auto_section(путь_владельца)
    return _на_проверку(прежний_auto, собранное["auto"])   # тот же расчёт, что был (org/person, класс изменился)
```

Список «на проверку» вынести в `_на_проверку(старое: dict, новое: dict) -> list[dict]` из нынешних строк 87–91. `merge_auto` больше не используется — удалить вместе с его тестами, если они есть только на него.

- [ ] **Step 5: `BaseGate` смотрит на оба файла** (`gate/pipeline.py`):

```python
def __init__(self, *, base, dictionary, guard, policy_path, auto_path=None) -> None:
    ...
    self._policy_path = pathlib.Path(policy_path)
    self._auto_path = pathlib.Path(auto_path) if auto_path else None
    self._stamp: tuple | None = None

def _отметка(self) -> tuple:
    def одна(п):
        return (п.stat().st_mtime, п.stat().st_size) if п and п.exists() else None
    return (одна(self._policy_path), одна(self._auto_path))

def refresh(self, *, force: bool = False) -> None:
    отметка = self._отметка()
    if not force and отметка == self._stamp and self._masker is not None:
        return
    policy = load_policy(self._policy_path, self._auto_path)
    self._policy, self._stamp = policy, отметка
    self._masker = Masker(self._dictionary, policy, mode=self.mode, base=self._base.name)
```

`_gate_for` в `tools/service.py` передаёт `auto_path=auto_policy_path(self._config.home, base.name)`. В `cli.py:301` печать «политика обновлена» → путь авторазметки. `cmd_policy_show` пока не трогать (задача 4).

- [ ] **Step 6: Тест реиндекса** (`tests/unit/test_gate_service.py`): на `индекс_ut` (фикстура) и временном доме: `refresh_policy` создаёт `policy.auto.yaml` с шапкой `НЕ РЕДАКТИРОВАТЬ` и разделом `auto`; файл владельца с разделом `auto` и комментарием после вызова теряет `auto`, но не комментарий; второй вызов файл авторазметки не переписывает (сравнить mtime/содержимое).

- [ ] **Step 7: Зелёный прогон и коммит** — `uv sync`, `uv run pytest tests/unit -q`, `uv run ruff check .`; `git commit -m "feat(gate): авторазметка в policy.auto.yaml, policy.yaml только владельцу (ADR-0015, задача 2)"`.

---

### Task 3: Шаблон `policy.yaml` владельца; создание при `base add` и реиндексе

**Files:**
- Create: `src/odata1c/templates/policy.example.yaml` — содержимое блока «Целевой вид `bases/<база>/policy.yaml`» из `docs/plans/2026-09-13-bases-yaml-single-control-point.md` §3, где имя базы `ut` заменено на `{{base}}` (три места в шапке)
- Modify: `src/odata1c/config/writer.py` (новая `ensure_policy_template`)
- Modify: `src/odata1c/cli.py:513-545` (`cmd_base_add`), `:280-305` (`cmd_reindex`)
- Modify: `src/odata1c/tools/service.py:1758-1840` (`reindex`: перед `refresh_policy`)
- Test: `tests/unit/test_policy_template.py` (новый), `tests/unit/test_cli_base.py`

**Interfaces:**
- Produces: `ensure_policy_template(home: pathlib.Path, base_name: str) -> bool` (`True` — файл создан; существующий не трогается).

- [ ] **Step 1: Тест шаблона** (`tests/unit/test_policy_template.py`) — перенести логику проверочного скрипта: (а) шаблон как есть после подстановки имени разбирается `yaml.safe_load` и принимается `load_policy`: `scan_free_text is True`, `names_for() is None`, `sensitivity_of("Catalog_Контрагенты", "ИНН") is None`; (б) после раскомментирования примеров (автомат: строка `# <раздел>:` открывает область, строки с префиксом `#   ` внутри раскомментируются, заглушка `<раздел>: {}` перед примером убирается) `load_policy` даёт `keep` для `Catalog_Контрагенты.КодПоОКПО`, `scan` для `Document_ПлатежноеПоручение.НазначениеПлатежа`, `inn` для `Catalog_Контрагенты.ДопИдентификатор`, `custom:tab_number` для `Catalog_Сотрудники.ТабельныйНомер`, `names_for() == {"Catalog_Контрагенты", "Catalog_Организации"}`, `is_hidden("Catalog_ФизическиеЛица")`, `defaults.addr.mask_for == ["Catalog_Партнеры"]`; (в) в шапке перечислены все классы из `gate.detectors.CLASSES` (или где определён перечень) плюс `keep` и `scan`.

- [ ] **Step 2: Запустить** → FAIL (шаблона нет).

- [ ] **Step 3: Шаблон и `ensure_policy_template`**

```python
def ensure_policy_template(home: pathlib.Path, base_name: str) -> bool:
    """Файл владельца `bases/<база>/policy.yaml` из шаблона; существующий не трогается никогда
    (ADR-0015). `True` — создан сейчас."""
    назначение = base_dir(home, base_name) / "policy.yaml"
    if назначение.exists():
        return False
    назначение.parent.mkdir(parents=True, exist_ok=True)
    шаблон = importlib.resources.files("odata1c.templates").joinpath("policy.example.yaml")
    назначение.write_text(
        шаблон.read_text(encoding="utf-8").replace("{{base}}", base_name), encoding="utf-8"
    )
    return True
```

Вызовы: в `cmd_base_add` после `append_base(...)`; в `cmd_reindex` и `ToolService.reindex` перед `refresh_policy` (печатать/добавлять в `warnings` строку «создан файл политики владельца: <путь>», без имён полей).

- [ ] **Step 4: Тест `base add`** (`tests/unit/test_cli_base.py`): после добавления базы существует `bases/<имя>/policy.yaml` и в нём строка с именем базы; повторный `ensure_policy_template` возвращает `False` и файл не меняется.

- [ ] **Step 5: Коммит** — `git commit -m "feat(policy): самодокументированный шаблон policy.yaml владельца, создаётся при base add и реиндексе (ADR-0015, задача 3)"`.

---

### Task 4: `policy show` с источником строк, `policy check`, ресурс `odata1c://policy/{base}`

**Files:**
- Create: `src/odata1c/gate/policy_check.py`
- Modify: `src/odata1c/cli.py:111-115` (парсер), `:168` (диспетчер), `:337-350` (`cmd_policy_show`), новая `cmd_policy_check`
- Modify: `src/odata1c/tools/service.py:2059-2115` (`resource_policy`)
- Test: `tests/unit/test_policy_check.py` (новый), `tests/unit/test_cli_policy.py`, `tests/unit/test_tools_service.py` (ресурс)

**Interfaces:**
- Produces:

```python
@dataclasses.dataclass(slots=True)
class Finding:
    level: Literal["error", "warning"]
    where: str          # "entities.Catalog_X" | "fields.Catalog_X.Поле" | "names_for[2]" | "custom.tab_number"
    message: str
    hint: str = ""

def check_policy(owner_path: pathlib.Path, repo: IndexRepository | None) -> list[Finding]
def effective_rows(policy: Policy, owner_data: dict) -> list[tuple[str, str, str]]   # (Сущность.Поле, класс, "владелец" | "авто")
def render_effective(owner_text: str, policy: Policy, owner_data: dict) -> str
def suggest_names(repo: IndexRepository, query: str, limit: int = 3) -> list[str]   # repo.find(query, limit=limit)
```

- [ ] **Step 1: Тесты `check_policy`** (`tests/unit/test_policy_check.py`, фикстура `индекс_ut`):
  - неизвестная сущность в `entities` → `Finding(level="error", where="entities.Catalog_Нет")` с подсказкой из `suggest_names`;
  - неизвестное поле в `fields` (`Catalog_Контрагенты.НетТакого`) → error, подсказка с ближайшими именами полей той же сущности (`difflib.get_close_matches` по `repo.field_names`);
  - неизвестный класс (`Catalog_Контрагенты.ИНН: secret`) → error «класс неизвестен; допустимые: …»;
  - `custom:tab_number` в `fields` без раздела `custom.tab_number` → error;
  - свой класс без `fields` и без `regex` → error;
  - поле `keep` у сущности из `entities.hide` → warning «правило не действует: сущность скрыта»;
  - `repo is None` → одна warning «индекса нет: имена сущностей и полей не проверены», остальные проверки (классы, regex, custom) выполняются;
  - битый YAML → `PolicyError` наружу (не Finding).

- [ ] **Step 2: Запустить** → FAIL (модуля нет).

- [ ] **Step 3: Реализация `policy_check.py`** — читать файл владельца через `policy._разобрать_yaml` + `policy._проверить_разделы` (сделать их публичными: `parse_owner_file(path) -> dict`), перечень классов брать из существующей константы классов гейта (найти в `gate/detectors.py` или `gate/masking.py`: имя `CLASSES`, используется в `masking.py:909`) плюс `keep`, `scan`, `custom:<имя из custom>`. Имена сущностей проверять через `repo.resolve_name(entity)`, поля — `repo.field_names(entity)`. `effective_rows`: строки владельца из `owner_data["fields"]` с источником `владелец`, затем `policy._auto` (сделать доступ через новый метод `Policy.auto_items() -> dict`) минус перекрытые владельцем с источником `авто`; сортировка по имени. `render_effective`: текст файла владельца как есть, затем блок:

```text
# --- действующая политика (владелец поверх авторазметки) ---
# скрыты: Catalog_ФизическиеЛица (+ 3 дочерних)      ← только если repo есть; иначе без счётчика
# названия скрываются у: Catalog_Контрагенты, …       ← names_for() или встроенный список
Catalog_Контрагенты.ИНН: inn            # авто
Catalog_Контрагенты.КодПоОКПО: keep     # владелец
```

- [ ] **Step 4: CLI** — `policy show <name>`: печатает `render_effective` (индекс открывать, если файл индекса есть; иначе без счётчика дочерних). `policy check <name>`: печатает находки строками `<level>: <where>: <message> (<hint>)`, код возврата 1 при наличии `error`, 0 иначе; при `PolicyError` — общий перехват `main()`. Тесты в `test_cli_policy.py` через `capsys`.

- [ ] **Step 5: Ресурс** — `resource_policy` вместо `путь.read_text()` собирает `render_effective(...)` и, как прежде, пропускает через `redact_policy` при правилах `hide`; строки авторазметки скрытых сущностей вычёркиваются той же функцией (она построчная, ключ строки — `Сущность.Поле`). Тест в `test_tools_service.py`: ресурс содержит строку авторазметки с пометкой `# авто` и не содержит имени скрытой сущности.

- [ ] **Step 6: Коммит** — `git commit -m "feat(policy): policy show с источником строк, policy check по индексу, ресурс policy отдаёт действующую политику (ADR-0015, задача 4)"`.

---

### Task 5: Конструктор: `policy hide | open | set`

**Files:**
- Create: `src/odata1c/gate/policy_edit.py`
- Modify: `src/odata1c/cli.py` (парсер `policy`, диспетчер, `cmd_policy_hide`, `cmd_policy_open`, `cmd_policy_set`)
- Test: `tests/unit/test_policy_edit.py` (новый), `tests/unit/test_cli_policy.py`

**Interfaces:**
- Produces: `hide_entity(path: pathlib.Path, entity: str) -> bool` (`False` — уже было), `set_field_class(path: pathlib.Path, field: str, cls: str) -> str | None` (возвращает прежний класс владельца или `None`). Обе пишут через `ruamel.yaml` round-trip во временный файл и `os.replace`; раздел, стоящий заглушкой `{}` или отсутствующий, заменяется отображением; комментарии файла сохраняются.
- Consumes: `check_policy`, `suggest_names`, `render_effective` из задачи 4; `IndexRepository.descendants`.

- [ ] **Step 1: Тесты `policy_edit`** — на копии шаблона (через `ensure_policy_template` во временный дом): `hide_entity` добавляет `entities: {Catalog_X: {hide: true}}`, шапка с перечнем классов и комментарий у `scan_free_text` остаются дословно; `set_field_class(..., "Catalog_A.B", "keep")` пишет в `fields`, второй вызов с `inn` возвращает `"keep"` и заменяет; `load_policy` после каждой правки читает новое правило; при исключении на записи исходный файл не изменён (подменить `os.replace`, чтобы бросал).

- [ ] **Step 2: Запустить** → FAIL.

- [ ] **Step 3: Реализация**

```python
def _открыть(path):
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap
    yaml_rt = YAML(); yaml_rt.preserve_quotes = True
    with path.open(encoding="utf-8") as f:
        данные = yaml_rt.load(f)
    if not isinstance(данные, CommentedMap):
        raise PolicyError(f"policy.yaml: ожидался словарь разделов, файл {path}", hint="см. шаблон policy.example.yaml")
    return yaml_rt, данные

def _раздел(данные, имя):
    from ruamel.yaml.comments import CommentedMap
    if not isinstance(данные.get(имя), CommentedMap) or not данные.get(имя):
        новый = CommentedMap()
        # сохранить комментарий, стоявший у заглушки `имя: {}`
        if имя in данные and данные.ca.items.get(имя):
            новый_ca = данные.ca.items[имя]
            данные[имя] = новый
            данные.ca.items[имя] = новый_ca
        else:
            данные[имя] = новый
    return данные[имя]

def _сохранить(yaml_rt, данные, path):
    временный = path.with_suffix(".yaml.new")
    with временный.open("w", encoding="utf-8") as f:
        yaml_rt.dump(данные, f)
    os.replace(временный, path)
```

`hide_entity`: `сущности = _раздел(данные, "entities")`; если уже `hide: true` → `False`; иначе `сущности[entity] = CommentedMap({"hide": True})`; сохранить. `set_field_class`: `поля = _раздел(данные, "fields")`; прежнее = `поля.get(field)`; `поля[field] = cls`; сохранить; вернуть прежнее.

- [ ] **Step 4: CLI** — общий порядок у всех трёх команд: `load_config` → база → индекс (если есть) → проверка имени (`repo.resolve_name`, для `open`/`set` ещё `field in repo.field_names`), при неудаче — отказ с `suggest_names` и код 1, файл не трогать → для `hide` напечатать `repo.descendants({entity})` (число и первые 10 имён) и, если не передан `--yes`, спросить подтверждение через `input()` (в тестах `--yes`) → запись → `check_policy` → печать находок и строки итога: `скрыто: <сущность> и N дочерних`, `открыто: <поле>` / `класс поля <поле>: <класс> (было: <прежний>|авторазметка)`. `policy set` принимает класс из перечня, `keep`, `scan`, `custom:<имя>` (имя должно быть в `custom` файла, иначе отказ до записи). Класс без индекса (`repo is None`): имена не проверяются, печатается предупреждение из `check_policy`.

- [ ] **Step 5: Тесты CLI** (`test_cli_policy.py`, фикстура `индекс_ut` скопирована в `bases/<имя>/metadata.sqlite` временного дома): `policy hide ut Catalog_Контрагенты --yes` → файл содержит правило, вывод содержит «дочерних»; `policy open ut Catalog_Контрагенты.ИНН` → `fields`; `policy set ut Catalog_Контрагенты.Нет inn` → код 1, файл не изменён, вывод с подсказкой; `policy set ut Catalog_Контрагенты.ИНН custom:x` без раздела `custom.x` → код 1.

- [ ] **Step 6: Коммит** — `git commit -m "feat(policy): конструктор policy hide|open|set на ruamel.yaml с проверкой по индексу (ADR-0015, задача 5)"`.

---

### Task 6: `bases.yaml` без перезапуска демона; перепроверка разрешений при `commit`

**Files:**
- Modify: `src/odata1c/registry/registry.py:62-75` (`replace_config`)
- Modify: `src/odata1c/tools/service.py:454-481` (`__init__`, `_gate_for`, `_client_for`), `:563-600` (`_run`: перечитывание и `config_invalid`), новые `_обновить_настройки`, `_применить_настройки`, свойство `config`
- Modify: `src/odata1c/daemon.py:1115-1125` (`_цикл_проверки_метаданных`: `service.config`), `:1491-1495`
- Modify: `src/odata1c/write/service.py:951` (после `_resolve_entity`: `check_write` по текущей базе)
- Test: `tests/unit/test_tools_config_reload.py` (новый), `tests/unit/test_write_commit_recheck.py` (новый), `tests/unit/test_registry.py` (если есть; иначе в новый)

**Interfaces:**
- Produces: `Registry.replace_config(config: AppConfig) -> None` (состояние индексации существующих баз сохраняется, новые добавляются, удалённые убираются, `label/role/gate_mode/write` обновляются); `ToolService.config` (свойство, текущий `AppConfig`); `ToolService._обновить_настройки() -> None`.
- Consumes: `check_write` (`write/permissions.py:77`), `WriteError`.

- [ ] **Step 1: Тесты перечитывания** (`tests/unit/test_tools_config_reload.py`; сервис строится, как в `test_tools_service.py`, на временном доме с `bases.yaml`, `daemon.yaml`, поддельным клиентом):
  - смена `gate.mode` базы в файле → следующий вызов любого тула отвечает по новому уровню (например, `odata1c_bases` показывает новый `gate_mode`), и в ответе есть предупреждение «политика гейта базы изменилась» ровно один раз;
  - смена `url` → `_client_for` возвращает новый объект клиента (старый закрыт: `client.close` вызван);
  - битый `bases.yaml` → любой тул отвечает кодом `config_invalid` с путём и строкой, без содержимого файла; после починки файла тулы работают;
  - файл не менялся → `load_config` не вызывается повторно (подменить и посчитать вызовы);
  - удалённая база → `base_unknown`; добавленная → видна в `odata1c_bases`.

- [ ] **Step 2: Запустить** → FAIL.

- [ ] **Step 3: Реализация в `ToolService`**

```python
СВЯЗЬ = ("url", "user", "password", "verify_tls", "timeout_s", "virtual_timeout_s", "concurrency", "ib_session")

def _отметка_файла(path: pathlib.Path) -> tuple[float, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)

# __init__:
self._путь_настроек = config.home / "bases.yaml"
self._отметка_настроек = _отметка_файла(self._путь_настроек)
self._ошибка_настроек: ConfigError | None = None
self._смена_уровня: set[str] = set()
self._закрыть: list[Client1C] = []

@property
def config(self) -> AppConfig:
    return self._config

def _обновить_настройки(self) -> None:
    отметка = _отметка_файла(self._путь_настроек)
    if отметка == self._отметка_настроек:
        return
    self._отметка_настроек = отметка
    try:
        новая = load_config(self._config.home)
    except ConfigError as ошибка:
        self._ошибка_настроек = ошибка
        return
    self._ошибка_настроек = None
    self._применить_настройки(новая)

def _применить_настройки(self, новая: AppConfig) -> None:
    старая = self._config
    for имя in set(старая.bases) - set(новая.bases):
        self._gates.pop(имя, None)
        if (клиент := self._clients.pop(имя, None)) is not None:
            self._закрыть.append(клиент)
    for имя, база in новая.bases.items():
        прежняя = старая.bases.get(имя)
        if прежняя is None:
            continue
        if any(getattr(прежняя, п) != getattr(база, п) for п in СВЯЗЬ):
            if (клиент := self._clients.pop(имя, None)) is not None:
                self._закрыть.append(клиент)
        if прежняя.gate.mode != база.gate.mode:
            self._gates.pop(имя, None)
            self._смена_уровня.add(имя)
    self._config = новая
    self._registry.replace_config(новая)
```

В `_run` первой строкой после `раскрытое = RevealedValues()`: `self._обновить_настройки()`; затем `if self._ошибка_настроек is not None: return self._guard_error(ошибка.code, str(ошибка), ошибка.hint)`; закрытие клиентов из `self._закрыть` — `await клиент.close()` там же (список очистить). Перед `итог(гейт, результат, раскрытое)`: если `isinstance(результат, dict)` и `base_config.name in self._смена_уровня` — `результат.setdefault("warnings", []).append("политика гейта базы изменилась: уровень или правила владельца; токены и состав полей в ответах могут отличаться от прежних")`, убрать имя из множества. `_gate_for`, `_client_for` и остальные читают `self._config` — уже так. `resource_*`, `info`, `bases` тоже идут через `_run` или вызывают `_обновить_настройки()` первой строкой (проверить `bases()` — если он не через `_run`, добавить вызов).

`Registry.replace_config(config)`: пересобрать `self._state`, для имён, что были, сохранить `indexed`, `indexed_at`, `entity_count`, `last_error`.

`daemon.py`: `_цикл_проверки_метаданных(service, config)` → в теле `await check_metadata_once(service, service.config)`; период по-прежнему из стартового `config.daemon` (daemon.yaml читается при старте).

- [ ] **Step 4: Перепроверка при `commit`** — в `write/service.py` сразу после `описание = tools._resolve_entity(репозиторий, гейт, операция.entity)` (строка 951) вызвать `check_write(...)` с теми же аргументами, что при подготовке той же операции (см. строки 348, 543, 648: сущность, `операция.op`, имена полей тела, `action`, `hidden=…`); `WriteError` оттуда обрабатывается тем же путём, что `entity_hidden` из `_resolve_entity` (операция получает отказ, в 1С ничего не уходит). Тест `tests/unit/test_write_commit_recheck.py`: подготовить `update` при `write: true`, переписать `bases.yaml` на `write: false`, `commit` → код `base_read_only`, поддельная 1С не получила PATCH.

- [ ] **Step 5: Коммит** — `git commit -m "feat(daemon): bases.yaml действует без перезапуска, битый файл закрывает тулы, commit перепроверяет разрешения (ADR-0015, задача 6)"`.

---

### Task 7: Тема `info("policy")`, навык `odata1c-policy`, документация

**Files:**
- Modify: `src/odata1c/tools/info.py:242-260` (константа `POLICY`, ключ `policy` в `_ТЕМЫ`)
- Create: `plugin/skills/odata1c-policy/SKILL.md`
- Modify: `AGENTS.md` (абзац «ADR-0015 принят… реализация впереди» → «реализован», пункты 3 и 7 раздела «Запуск и подключение»), `docs/plans/README.md` (состояние M2b → исполнен)
- Test: `tests/unit/test_tools_info.py` (если есть; иначе добавить в `test_tools_service.py`)

- [ ] **Step 1: Тест** — `render("policy")` содержит слова «policy.yaml», «policy.auto.yaml», «keep», «scan», «odata1c policy check» и не содержит значений; `"policy" in TOPICS`.

- [ ] **Step 2: Текст темы** — теми же словами, что шапка шаблона: два файла и кто их пишет; перечень классов с пояснением; разделы (`names_for`, `scan_free_text`, `defaults`, `entities`, `fields`, `custom`) и приоритет; что модель делает, когда пользователь просит открыть или скрыть поле: найти сущность и поле (`odata1c_find_entity`, `odata1c_describe_entity`), объяснить, что изменится, и дать одну команду `odata1c policy …` для терминала владельца; тула для правки политики нет, и почему.

- [ ] **Step 3: SKILL.md** — frontmatter `name: odata1c-policy`, `description` с триггерами («скрой поле», «почему это поле токеном», «открой название», «добавь свой класс», «настрой политику гейта»); тело — пять шагов из записки §5 с точными именами тулов и команд, правило «команду выполняет владелец в терминале, модель не пишет в файлы дома шлюза», типовые отказы `policy check` и что с ними делать.

- [ ] **Step 4: Документация** — `AGENTS.md`: статусный абзац ADR-0015 переписать как «реализован <дата>, план …», пункт 3 (команды `policy`), пункт 7 (`bases.yaml` перечитывается; перезапуск только для `daemon.yaml`), абзац про `defaults.addr.mask_for`; `docs/plans/README.md` — состояние строки M2b.

- [ ] **Step 5: Коммит** — `git commit -m "feat(plugin): тема info policy и навык odata1c-policy; docs: ADR-0015 реализован"`.

---

### Task 8: Проверка на рабочем доме владельца

Без кода. Выполняется владельцем или с его согласия, потому что трогает `~/.claude/odata1c/`.

- [x] **Step 1:** копия `bases/trade_dev/policy.yaml` и `bases.yaml` (`*.bak-adr15`).
- [x] **Step 2:** `uv run odata1c daemon stop` (демон старой версии) → `uv run odata1c reindex trade_dev` → появился `policy.auto.yaml` с шапкой, в `policy.yaml` раздела `auto` нет, ручные разделы на месте; `uv run odata1c policy check trade_dev` → без `error`.
- [x] **Step 3:** `uv run odata1c policy show trade_dev | head -60` — файл владельца, затем действующая политика с источниками.
- [x] **Step 4:** `uv run odata1c policy open trade_dev Catalog_Контрагенты.КодПоОКПО` → вывод «открыто…», `check` чистый; вернуть: `uv run odata1c policy set trade_dev Catalog_Контрагенты.КодПоОКПО keep` (то же), затем удалить строку руками или оставить — решение владельца.
- [x] **Step 5:** в Claude Code `/mcp` → переподключить `odata1c`; в `bases.yaml` сменить `gate.mode` у `trade_dev` на `identifiers` без перезапуска демона → `odata1c_bases` показывает новый уровень, в ответе предупреждение; вернуть `off`.
- [x] **Step 6:** записать результат в `docs/probes/M2b-owner-check.md` (счётчики и имена полей, без значений).
