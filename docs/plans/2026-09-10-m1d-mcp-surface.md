# M1d — поверхность MCP: демон, лаунчер, тулы чтения

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** шлюз подключается к Claude Code (`claude mcp add … -- uvx odata1c mcp`) и отвечает на
вопросы о данных базы 1С через тулы чтения; всё, что уходит модели, проходит гейт.

**Architecture:** демон (`odata1c daemon`) — `MCPServer` SDK `mcp` 2.2 на Streamable HTTP
`127.0.0.1:<port>`; тулы — тонкие обёртки над `ToolService` (`src/odata1c/tools/service.py`),
который ничего не знает об MCP и тестируется без сети. Конвейер ответа: 1С → очистка служебных
полей → маскировщик → усечение строк → подгонка под `result_chars` (целыми записями) →
сериализация → страж. Лаунчер (`odata1c mcp`) — stdio-прокси на низкоуровневом `Server`,
поднимает демон и передаёт область видимости баз заголовками HTTP. Порядок задач таков, что после
задачи 6 шлюз уже работает в Claude Code; задачи 7–9 — хвост (реиндекс из тула, справочник,
`raw_get`, ресурсы, рецепты, приёмка).

**Tech Stack:** Python 3.12, `mcp` 2.2 (`MCPServer`, lowlevel `Server`, `streamable_http_client`,
`InMemoryTransport`), `httpx2` (клиент лаунчера — его требует `streamable_http_client`), `httpx`
(клиент 1С), `uvicorn`, `pytest`, `respx`.

**Spec:** `SPEC.md` §2 (архитектура), §3.3 (`daemon.yaml`), §4.4, §5 (тулы, формат ответов,
ошибки), §6 (гейт), §9 (клиент 1С, поправка 2026-09-10), §10 (лимиты); факты: пробы P2, P3, P4
(`docs/probes/`), шпаргалка SDK — раздел «Шпаргалка по SDK» ниже (проверена исполнением
2026-09-10).

**Зависит от:** M1b-fix (индекс: действия, виртуальные таблицы, навигации, перечисления,
`EntityDescription` с полями `parent_entity`, `is_records`, `is_virtual`, `virtual_kind`,
`members`, `navigations`), M1c (гейт с правками F1/F2).

## Global Constraints

- Язык комментариев, docstring, сообщений коммитов, текстов ошибок и подсказок — русский;
  идентификаторы — как в коде. Байтовый литерал с кириллицей невозможен — `"…".encode()`.
- Инвариант 1: реальные значения защищаемых классов не выходят через MCP никогда — включая
  ошибки 1С, описания, подсказки. Страж — последний проход по сериализованному ответу **любого**
  тула (кроме уровня `off`), в том числе ответа об ошибке.
- Инвариант 6: суммы, количества, даты, GUID, коды и номера документов не защищаются.
- Сериализация ответа — `json.dumps(..., ensure_ascii=False)`: страж не видит кириллицу в
  `\uXXXX`-экранировании.
- Порядок конвейера ответа неизменен: очистка → маска → усечение строк (не разрезая токен) →
  подгонка под `result_chars` выбрасыванием записей целиком → сериализация → страж.
- Ошибка тула — обычный текст с JSON `{"error": {"code", "message", "hint"}}` (SPEC §5.2), не
  исключение: голое исключение теряет текст у клиента, `ToolError` добавляет свою обёртку.
- Тулы объявляются с `structured_output=False`: иначе строковый результат дублируется в
  `structuredContent`.
- Демон слушает только `127.0.0.1`. Файлы индекса открываются на время вызова, а не держатся
  открытыми: на Windows `os.replace` при реиндексе не заменит открытый файл.
- Команды: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .` — чисто перед
  каждым коммитом; коммит заканчивается строкой `Co-Authored-By:` исполнителя.

## Шпаргалка по SDK `mcp` 2.2 (проверено исполнением)

```python
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations              # тот же модуль, что mcp.types
server = MCPServer("odata1c", instructions="…")
@server.tool(name="odata1c_bases", annotations=ToolAnnotations(read_only_hint=True),
             meta={"anthropic/maxResultSizeChars": 120000}, structured_output=False)
async def bases(ctx: Context) -> str: ...
ctx.headers            # dict заголовков HTTP-запроса или None (stdio, в памяти)
ctx.headers["mcp-session-id"]   # устойчивый ключ сессии; id(ctx.session) — НЕ устойчив
@server.resource("odata1c://cheatsheet")          # статический ресурс
@server.resource("odata1c://policy/{base}")       # шаблон — по {параметру} в URI
@server.prompt()                                  # аргументы функции = аргументы промпта
app = server.streamable_http_app()                # Starlette, путь /mcp, stateful
config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
http = uvicorn.Server(config); task = asyncio.create_task(http.serve())
http.should_exit = True; await task               # остановка

from mcp.client._memory import InMemoryTransport  # тесты без сети (ctx.headers is None)
async with InMemoryTransport(server) as (read, write):
    async with ClientSession(read, write) as session: ...

import httpx2
from mcp.client.streamable_http import streamable_http_client  # headers= НЕ принимает
async with httpx2.AsyncClient(headers={...}, timeout=httpx2.Timeout(10, read=None)) as http_client:
    async with streamable_http_client(url, http_client=http_client) as (read, write): ...

from mcp.server.lowlevel import Server           # лаунчер: обработчики в конструкторе
Server(name, on_list_tools=, on_call_tool=, on_list_resources=, on_list_resource_templates=,
       on_read_resource=, on_list_prompts=, on_get_prompt=)   # каждый: async (ctx, params)
from mcp.server.stdio import stdio_server        # сам оборачивает stdin/stdout в UTF-8
async with stdio_server() as (r, w): await proxy.run(r, w, proxy.create_initialization_options())
ServerSession.elicit_form(message, requested_schema)   # пересылка elicitation вниз
```

Образцы: `tools/probes/p2_meta_probe.py`, `p3_elicit_server.py`, `p3_launcher.py`, `p3_client.py`.

## Решения, принятые планом (и поправки SPEC, которые вносит исполнитель)

1. **Область видимости сессии — заголовки HTTP**, а не `_meta` запроса `initialize`: SDK не
   сохраняет `_meta` инициализации, а заголовки `X-Odata1c-Bases: ut,buh` и
   `X-Odata1c-Default: ut` приходят с каждым запросом и читаются `ctx.headers`. Без заголовков —
   все базы. Поправка SPEC §2.1 — задача 5.
2. **Новый код ошибки `params_invalid`** — неверные аргументы тула: неизвестный или
   недостающий параметр виртуальной таблицы, неполный составной ключ, слишком глубокий `$expand`,
   неизвестная навигация, сортировка по защищаемому полю. Поправка SPEC §5.2 — задача 2.
3. **Сортировка по защищаемому полю запрещена** (`params_invalid`): порядок строк — оракул
   сравнения, тот же, что закрыт в `$filter`.
4. **Таймаут виртуальных таблиц** — `virtual_timeout_s` базы (по умолчанию 180, SPEC §10);
   первый запрос после простоя публикации может идти дольше 30 с (проба P4).
5. **Автоматический `$select` при `$expand`** — только когда модель задала `select`: для каждого
   раскрываемого пути добавляются `Путь/Ref_Key` и представление цели (`Description`, `Code`,
   `Number`, `Date` — что есть у цели). Без `select` 1С раскрывает связь целиком. Без
   навигационного поля в `$select` 1С молча не раскрывает связь (проба P4) — автодобавление это
   закрывает.

---

### Задача 1: конвейер гейта для ответа тула

**Files:**
- Create: `src/odata1c/gate/pipeline.py`
- Modify: `src/odata1c/gate/masking.py` (вынести эффективный класс поля в публичную функцию)
- Test: `tests/unit/test_gate_pipeline.py`

**Interfaces:**
- Consumes: `Masker`, `MaskResult` (`masking.py`), `Unmasker`, `GateError` (`unmasking.py`),
  `Guard`, `GuardResult` (`guard.py`), `Dictionary`, `load_policy`, `Policy`
  (`sensitivity_of`, `custom_fields`, `is_hidden`), `classify_field`, `BaseConfig`.
- Produces:
  - `masking.effective_field_class(policy: Policy, entity: str, field: str, *, mode: str) -> str | None`
    — ровно логика нынешних `Masker._базовый_класс_поля` + `_класс_поля` (понижение классов
    названий ниже `identifiers+names`); `Masker` использует её же.
  - `class BaseGate` в `pipeline.py`:
    ```python
    class BaseGate:
        def __init__(self, *, base: BaseConfig, dictionary: Dictionary, guard: Guard,
                     policy_path: pathlib.Path) -> None
        mode: str                                   # base.gate.mode
        def refresh(self) -> None                   # перечитать политику, если сменилось mtime
        def is_hidden(self, entity: str) -> bool
        def is_protected(self, entity: str, field: str) -> bool   # класс не None/keep/scan
        def inbound_filter(self, expression: str, *, entity: str) -> str
        def inbound_value(self, text: str, *, entity: str, field: str) -> str
        def inbound_key(self, key, *, entity: str)
        def mask(self, data, *, entity: str) -> MaskResult
        def finish(self, envelope: dict) -> str     # сериализация + страж
        def finish_text(self, text: str) -> str     # страж по готовому тексту (markdown describe)
        def error(self, code: str, message: str, hint: str = "") -> str
    ```
  - `def guard_only(guard: Guard, envelope: dict) -> str` — сериализация и страж на строжайшем
    уровне (`identifiers+names`) для ответов, у которых база не определена (неизвестная база).

- [ ] **Шаг 1: падающие тесты**

`tests/unit/test_gate_pipeline.py` (фикстуры — по образцу `tests/unit/test_gate_masking.py`:
временный каталог, `Dictionary(путь, "секрет-для-тестов-ровно-32-байта".encode())`, политика в
`policy.yaml`):

```python
def test_finish_прогоняет_стража_и_помечает_замену(врата_prod, словарь):
    инн = "7707083893"
    словарь.token_for("inn", инн, base="ut", entity="Catalog_Контрагенты", field="ИНН")
    текст = врата_prod.finish({"items": [{"Комментарий": f"ИНН {инн}"}], "warnings": []})
    assert инн not in текст
    данные = json.loads(текст)
    assert any(п.startswith("guard_replaced") for п in данные["warnings"])


def test_finish_сериализует_без_экранирования(врата_prod):
    текст = врата_prod.finish({"items": [{"Description": "Склад"}], "warnings": []})
    assert "Склад" in текст and "\\u" not in текст


def test_finish_на_уровне_off_ничего_не_меняет(врата_dev, словарь):
    инн = "7707083893"
    словарь.token_for("inn", инн, base="dev", entity="E", field="ИНН")
    текст = врата_dev.finish({"items": [{"ИНН": инн}], "warnings": []})
    assert json.loads(текст)["items"][0]["ИНН"] == инн


def test_error_маскирует_сообщение_1С(врата_prod, словарь):
    инн = "7707083893"
    словарь.token_for("inn", инн, base="ut", entity="E", field="ИНН")
    текст = врата_prod.error("odata_error", f"Не найден контрагент с ИНН {инн}", "проверьте отбор")
    assert инн not in текст
    assert json.loads(текст)["error"]["code"] == "odata_error"


def test_is_protected_учитывает_уровень(врата_prod, врата_identifiers):
    # политика: auto Catalog_Контрагенты.Description: org, Catalog_Контрагенты.ИНН: inn
    assert врата_prod.is_protected("Catalog_Контрагенты", "Description") is True
    assert врата_identifiers.is_protected("Catalog_Контрагенты", "Description") is False
    assert врата_identifiers.is_protected("Catalog_Контрагенты", "ИНН") is True
    assert врата_prod.is_protected("Catalog_Контрагенты", "Code") is False


def test_inbound_filter_в_режиме_identifiers_пропускает_название(врата_identifiers):
    выражение = "Description eq 'ООО Ромашка'"
    assert врата_identifiers.inbound_filter(выражение, entity="Catalog_Контрагенты") == выражение


def test_refresh_подхватывает_новую_политику(врата_prod, путь_политики):
    assert врата_prod.is_protected("Catalog_Контрагенты", "Code") is False
    путь_политики.write_text(путь_политики.read_text(encoding="utf-8")
                             + "fields:\n  Catalog_Контрагенты.Code: inn\n", encoding="utf-8")
    os.utime(путь_политики, (time.time() + 5, time.time() + 5))
    врата_prod.refresh()
    assert врата_prod.is_protected("Catalog_Контрагенты", "Code") is True


def test_finish_text_прогоняет_стража(врата_prod, словарь):
    инн = "7707083893"
    словарь.token_for("inn", инн, base="ut", entity="E", field="ИНН")
    assert инн not in врата_prod.finish_text(f"| ИНН | {инн} |")


def test_guard_only_на_строжайшем_уровне(guard, словарь):
    инн = "7707083893"
    словарь.token_for("inn", инн, base="ut", entity="E", field="ИНН")
    assert инн not in guard_only(guard, {"error": {"message": f"x {инн}"}})
```

(Если в политике по умолчанию раздел `fields:` уже есть — дописывай в него, а не вторым ключом.
Фикстуры `врата_prod`, `врата_identifiers`, `врата_dev` — `BaseGate` для баз с `gate.mode`
`identifiers+names`, `identifiers`, `off`; `словарь` и `guard` — общие.)

- [ ] **Шаг 2: RED** — `uv run pytest tests/unit/test_gate_pipeline.py -q` падает на импорте.

- [ ] **Шаг 3: реализация**

`masking.py`: вынести тело `_базовый_класс_поля` и понижение `_класс_поля` в
`effective_field_class(policy, entity, field, *, mode)`; методы `Masker` вызывают её (поведение
маскировщика не меняется — существующие тесты `test_gate_masking.py` остаются зелёными без правок).

`pipeline.py`:

```python
class BaseGate:
    def __init__(self, *, base, dictionary, guard, policy_path):
        self._base = base
        self._dictionary = dictionary
        self._guard = guard
        self._policy_path = policy_path
        self.mode = base.gate.mode
        self._mtime = None
        self.refresh()

    def refresh(self) -> None:
        """Политику перезаписывает реиндекс (раздел auto) и пользователь (fields) — демон живёт
        дольше одной версии файла. Сверка mtime дешевле чтения YAML на каждом вызове."""
        mtime = self._policy_path.stat().st_mtime if self._policy_path.exists() else None
        if mtime == self._mtime and hasattr(self, "_masker"):
            return
        policy = load_policy(self._policy_path)   # отсутствующий файл → пустая политика
        self._policy, self._mtime = policy, mtime
        self._masker = Masker(self._dictionary, policy, mode=self.mode, base=self._base.name)
        self._unmasker = Unmasker(
            self._dictionary, base=self._base.name,
            field_class=lambda entity, field: effective_field_class(policy, entity, field, mode=self.mode),
        )

    def finish(self, envelope: dict) -> str:
        текст = json.dumps(envelope, ensure_ascii=False)
        проверено = self._guard.check(текст, mode=self.mode)
        if not проверено.replacements:
            return проверено.text
        данные = json.loads(проверено.text)   # страж сохраняет валидность JSON (правка F2 M1c)
        данные.setdefault("warnings", []).append(
            f"guard_replaced: страж заменил {len(проверено.replacements)} значений, "
            "не распознанных маскировщиком"
        )
        return json.dumps(данные, ensure_ascii=False)
```

`load_policy` при отсутствии файла — проверь фактическое поведение; если он бросает, `refresh`
создаёт пустую `Policy` тем способом, которым это делают тесты `test_gate_policy.py`.
`is_protected` — `effective_field_class(...) not in (None, "keep", "scan")`. `error` — `mask_text`
по `message` и `hint` (поле `"error"`, сущность `""`), затем `finish({"error": {...}})`.
`inbound_*` — делегирование `Unmasker` (`GateError` пробрасывается наверх: слой тулов превращает
его в ответ-ошибку). На уровне `off` `inbound_*` возвращают вход без изменений (токенов нет).

- [ ] **Шаг 4: GREEN, мутации, коммит**

Мутации (по одной, с возвратом): `finish` без стража → падают тесты стража; `ensure_ascii=True` →
падает тест экранирования; `refresh` без сверки mtime, но и без перечитывания → падает тест
`refresh`. Затем полный прогон, ruff, коммит
`feat: конвейер гейта для ответа тула`.

---

### Задача 2: построение запроса к 1С

**Files:**
- Create: `src/odata1c/tools/__init__.py` (есть), `src/odata1c/tools/odata_query.py`
- Modify: `src/odata1c/client1c/client.py` (`get(..., timeout=)`), `src/odata1c/config/models.py`
  (`BaseConfig.virtual_timeout_s: int = 180`)
- Modify: `SPEC.md` §5.2 (код `params_invalid`), §10 (строка таймаута — где задаётся)
- Test: `tests/unit/test_tools_query.py`, `tests/unit/test_client1c.py`

**Interfaces:**
- Consumes: `EntityDescription` (M1b-fix задача 3: `name`, `key_fields`, `fields` — список dict с
  `name`, `edm_type`, …; `navigations: dict[str, str]`, `is_virtual`, `virtual_kind`,
  `parent_entity`, `actions` — список dict с `name`, `params`), `Limits`.
- Produces:
  ```python
  class QueryError(Exception):
      def __init__(self, code: str, message: str, hint: str = "") -> None  # .code .message .hint

  @dataclasses.dataclass(slots=True)
  class QuerySpec:
      path: str                  # относительно standard.odata/, без $format
      params: dict[str, str]     # $filter, $select, $expand, $orderby, $top, $skip, …
      timeout_s: int | None      # None — таймаут базы
      top: int                   # фактический $top (для has_more)
      skip: int
      warnings: list[str]

  Describe = Callable[[str], EntityDescription | None]

  def build_query(desc, *, describe: Describe, limits: Limits, virtual_timeout_s: int,
                  filter: str | None = None, select: list[str] | None = None,
                  expand: list[str] | None = None, orderby: str | None = None,
                  top: int | None = None, skip: int | None = None, inlinecount: bool = False,
                  params: dict | None = None, allowed_only: bool = False) -> QuerySpec
  def build_get(desc, key, *, describe: Describe, limits: Limits,
                select: list[str] | None = None, expand: list[str] | None = None) -> QuerySpec
  def odata_literal(edm_type: str, value) -> str
  def orderby_fields(orderby: str) -> list[str]     # имена полей из $orderby (для проверки гейтом)
  ```
- `Client1C.get(path, params=None, *, timeout: float | None = None)` — таймаут одного запроса
  (httpx принимает `timeout=` на запрос); сообщение `timeout` называет фактический таймаут.

Правила (SPEC §9 с поправкой 2026-09-10, решения плана 2, 4, 5):
- `$top`: по умолчанию `limits.top_default`; больше `limits.top_max` → `top_max` и предупреждение
  `"top уменьшен до {top_max}"`; отрицательный `top`/`skip` → `params_invalid`.
- `select`/`expand` — списки; строка с запятыми тоже принимается (разбивается). Пустые элементы
  отбрасываются.
- `expand`: глубина пути (число сегментов) ≤ `limits.expand_depth`, иначе `params_invalid`;
  каждый сегмент — навигация текущей сущности (`desc.navigations`), цель — следующая; неизвестный
  сегмент → `params_invalid` с перечнем навигаций. Если задан `select` — к нему добавляются
  `Путь/Ref_Key` и те из `Description`, `Code`, `Number`, `Date`, что есть у цели.
- `inlinecount=True` → `$inlinecount=allpages`; `allowed_only=True` → `allowedOnly=true`.
- Виртуальная таблица (`desc.is_virtual`): путь `f"{desc.parent_entity}/{desc.virtual_kind}({аргументы})"`;
  допустимые параметры — `desc.actions[0]["params"]` (имя → тип); неизвестный параметр →
  `params_invalid` с перечнем; обязательные: `Balance` — `Period`; `Turnovers`,
  `BalanceAndTurnovers` — `StartPeriod` и `EndPeriod`; прочие — без обязательных. Аргументы в
  порядке имён: `Имя=литерал` через запятую; `Condition` и `Dimensions` — строковые литералы.
  `timeout_s = virtual_timeout_s`. Для невиртуальной сущности непустой `params` → `params_invalid`.
- `odata_literal`: `Edm.Guid` → `guid'…'` (проверка формата GUID, иначе `params_invalid`);
  `Edm.DateTime` → `datetime'YYYY-MM-DDTHH:MM:SS'` (дата без времени дополняется `T00:00:00`;
  иное — `params_invalid`); `Edm.String` → `'…'` с удвоением `'`; `Edm.Boolean` → `true`/`false`;
  `Edm.Int16/32/64`, `Edm.Decimal`, `Edm.Double` → число как есть (проверка `int`/`float`).
- `build_get`: `key` — строка GUID для ключа `Ref_Key` или dict для составного ключа; все поля
  `desc.key_fields` обязательны (`params_invalid` с перечнем недостающих), лишние — тоже
  `params_invalid`; одиночный `Ref_Key` → `(guid'…')`, составной → `(Имя=литерал,…)` в порядке
  `key_fields`, тип — из `desc.fields`.
- `orderby_fields("Дата desc, Номер")` → `["Дата", "Номер"]` (путь `Контрагент/Description` →
  `"Контрагент/Description"` целиком).

- [ ] **Шаг 1: падающие тесты** — на реальном образце через `IndexRepository` (фикстура
  `индекс_ut` из `tests/unit/test_index_repository.py` — вынеси её в `tests/unit/conftest.py`, если
  она ещё там не лежит; `describe = индекс_ut.describe`):

```python
def test_простая_выборка(индекс_ut, лимиты):
    спец = build_query(индекс_ut.describe("Catalog_Контрагенты"), describe=индекс_ut.describe,
                       limits=лимиты, virtual_timeout_s=180, select=["Ref_Key", "Description"],
                       filter="DeletionMark eq false", top=10)
    assert спец.path == "Catalog_Контрагенты"
    assert спец.params == {"$select": "Ref_Key,Description", "$filter": "DeletionMark eq false",
                           "$top": "10"}
    assert спец.timeout_s is None


def test_top_ограничен_максимумом(индекс_ut, лимиты):
    спец = build_query(индекс_ut.describe("Catalog_Валюты"), describe=индекс_ut.describe,
                       limits=лимиты, virtual_timeout_s=180, top=5000)
    assert спец.params["$top"] == str(лимиты.top_max)
    assert спец.warnings


def test_expand_добавляет_путь_в_select(индекс_ut, лимиты):
    спец = build_query(индекс_ut.describe("Document_РеализацияТоваровУслуг"),
                       describe=индекс_ut.describe, limits=лимиты, virtual_timeout_s=180,
                       select=["Number"], expand=["Контрагент"])
    поля = спец.params["$select"].split(",")
    assert "Контрагент/Ref_Key" in поля and "Контрагент/Description" in поля
    assert спец.params["$expand"] == "Контрагент"


def test_expand_глубже_лимита(индекс_ut, лимиты):
    with pytest.raises(QueryError) as ошибка:
        build_query(индекс_ut.describe("Document_РеализацияТоваровУслуг"),
                    describe=индекс_ut.describe, limits=лимиты, virtual_timeout_s=180,
                    expand=["Контрагент/ГоловнойКонтрагент/ГоловнойКонтрагент"])
    assert ошибка.value.code == "params_invalid"


def test_неизвестная_навигация(индекс_ut, лимиты):
    with pytest.raises(QueryError) as ошибка:
        build_query(индекс_ut.describe("Document_РеализацияТоваровУслуг"),
                    describe=индекс_ut.describe, limits=лимиты, virtual_timeout_s=180,
                    expand=["НетТакой"])
    assert "Контрагент" in ошибка.value.hint


def test_остатки_требуют_период(индекс_ut, лимиты):
    остатки = индекс_ut.describe("AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance")
    with pytest.raises(QueryError) as ошибка:
        build_query(остатки, describe=индекс_ut.describe, limits=лимиты, virtual_timeout_s=180)
    assert ошибка.value.code == "params_invalid" and "Period" in ошибка.value.message


def test_остатки_на_дату_с_условием(индекс_ut, лимиты):
    остатки = индекс_ut.describe("AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance")
    спец = build_query(остатки, describe=индекс_ut.describe,
                       limits=лимиты, virtual_timeout_s=180,
                       params={"Period": "2026-09-01", "Condition": "Валюта_Key eq guid'00000000-0000-0000-0000-000000000000'"})
    assert спец.path == ("AccumulationRegister_РасчетыСКлиентамиПланОплат/Balance("
                         "Condition='Валюта_Key eq guid''00000000-0000-0000-0000-000000000000''',"
                         "Period=datetime'2026-09-01T00:00:00')")
    assert спец.timeout_s == 180


def test_неизвестный_параметр_виртуальной_таблицы(индекс_ut, лимиты):
    срез = индекс_ut.describe("InformationRegister_КурсыВалют_SliceLast")
    with pytest.raises(QueryError) as ошибка:
        build_query(срез, describe=индекс_ut.describe, limits=лимиты, virtual_timeout_s=180,
                    params={"Периодичность": "Месяц"})
    assert "Period" in ошибка.value.hint


def test_get_по_guid(индекс_ut, лимиты):
    спец = build_get(индекс_ut.describe("Catalog_Контрагенты"),
                     "a103cb54-42ee-11ec-a7a0-f10ab59a067e", describe=индекс_ut.describe, limits=лимиты)
    assert спец.path == "Catalog_Контрагенты(guid'a103cb54-42ee-11ec-a7a0-f10ab59a067e')"


def test_get_составной_ключ_неполный(индекс_ut, лимиты):
    with pytest.raises(QueryError) as ошибка:
        build_get(индекс_ut.describe("InformationRegister_КурсыВалют"), {"Период": "2026-01-01"},
                  describe=индекс_ut.describe, limits=лимиты)
    assert ошибка.value.code == "params_invalid"


def test_литералы():
    assert odata_literal("Edm.String", "О'Брайен") == "'О''Брайен'"
    assert odata_literal("Edm.DateTime", "2026-09-01") == "datetime'2026-09-01T00:00:00'"
    assert odata_literal("Edm.Boolean", True) == "true"
    with pytest.raises(QueryError):
        odata_literal("Edm.Guid", "не-guid")


def test_orderby_fields():
    assert orderby_fields("Дата desc, Контрагент/Description asc") == ["Дата", "Контрагент/Description"]
```

(Ключевые поля `InformationRegister_КурсыВалют` возьми из образца — тест проверяет неполный
ключ, конкретные имена подставь фактические.)

`test_client1c.py`: `get(..., timeout=0.01)` против `respx`-маршрута с задержкой → `OdataError`
с кодом `timeout`, в сообщении фактический таймаут.

- [ ] **Шаг 2: RED; Шаг 3: реализация; Шаг 4: GREEN**

`SPEC.md` §5.2 — поправка 2026-09-10: код `params_invalid` (решение плана 2, перечень случаев);
§10 — строка таймаута: «60 с (`timeout_s`), виртуальные таблицы 180 с (`virtual_timeout_s`)».

- [ ] **Шаг 5: мутации и коммит** — убрать автодобавление пути в `$select` → падает тест
  `expand`; убрать удвоение кавычек → падает тест литералов; обязательность `Period` → падает
  тест остатков. Коммит `feat: построение запросов к 1С для тулов чтения`.

---

### Задача 3: формирование ответа

**Files:**
- Create: `src/odata1c/tools/response.py`
- Test: `tests/unit/test_tools_response.py`

**Interfaces:**
- Produces:
  ```python
  def strip_service(obj, *, keep_data_version: bool = False)
      # рекурсивно убирает ключи "odata.metadata", "odata.type", "odata.count",
      # "odata.nextLink", все "*@navigationLinkUrl" и "*@odata.*", "DataVersion" (если не
      # keep_data_version), "*_Base64Data" и "*ХранилищеЗначения*"
  def items_of(raw: dict) -> tuple[list, int | None]
      # список из "value" (или [raw] для объекта), total = int(raw["odata.count"]) при наличии
  def truncate_strings(obj, limit: int) -> tuple[object, int]
      # строки длиннее limit обрезаются до limit, но не посреди токена [[…]]: если граница
      # попадает внутрь токена, срез переносится на его начало; к обрезанной строке
      # добавляется "…[обрезано: N симв.]"; второй результат — число обрезанных строк
  def page_info(*, count: int, total: int | None, top: int, skip: int) -> dict
      # {"count", "total", "has_more", "next_skip"}: has_more = skip+count < total при
      # известном total, иначе count == top; next_skip = skip+count при has_more, иначе None
  def fit_result(envelope: dict, limit_chars: int) -> dict
      # выбрасывает записи envelope["items"] с конца, пока json.dumps(..., ensure_ascii=False)
      # длиннее limit_chars; при выбрасывании has_more=True, next_skip пересчитан от
      # оставшихся, в warnings — "результат усечён до N записей по лимиту result_chars"
  ```

- [ ] **Шаг 1: падающие тесты**

```python
def test_strip_убирает_служебное():
    сырой = {"Ref_Key": "g", "DataVersion": "AAA", "Контрагент@navigationLinkUrl": "x",
             "odata.type": "t", "Фото_Base64Data": "…", "Вложенный": {"odata.type": "t", "Code": "1"}}
    assert strip_service(сырой) == {"Ref_Key": "g", "Вложенный": {"Code": "1"}}


def test_strip_оставляет_dataversion_по_запросу():
    assert strip_service({"DataVersion": "A"}, keep_data_version=True) == {"DataVersion": "A"}


def test_count_строкой_становится_числом():
    записи, всего = items_of({"odata.count": "1103", "value": [{"a": 1}]})
    assert записи == [{"a": 1}] and всего == 1103


def test_усечение_не_режет_токен():
    строка = "а" * 8 + "[[org:17]]" + "б" * 20
    обрезано, число = truncate_strings({"x": строка}, 12)
    assert обрезано["x"].startswith("а" * 8 + "…")
    assert "[[org:1" not in обрезано["x"].replace("[[org:17]]", "")
    assert число == 1


def test_page_info_по_total_и_без_него():
    assert page_info(count=25, total=1340, top=25, skip=0) == {
        "count": 25, "total": 1340, "has_more": True, "next_skip": 25}
    assert page_info(count=3, total=None, top=25, skip=0)["has_more"] is False


def test_fit_result_выбрасывает_записи_целиком():
    конверт = {"items": [{"t": "x" * 100} for _ in range(50)], "has_more": False,
               "next_skip": None, "count": 50, "warnings": []}
    подогнано = fit_result(конверт, 2000)
    assert len(json.dumps(подогнано, ensure_ascii=False)) <= 2000
    assert all(запись == {"t": "x" * 100} for запись in подогнано["items"])
    assert подогнано["has_more"] is True and подогнано["warnings"]
```

- [ ] **Шаги 2–4:** RED → реализация → GREEN; мутация «срез без переноса на начало токена» →
  падает тест усечения. Коммит `feat: формирование ответа тулов чтения`.

---

### Задача 4: сервис тулов — bases, find_entity, describe_entity, query, get

**Files:**
- Create: `src/odata1c/tools/service.py`, `src/odata1c/tools/describe.py` (markdown описания)
- Test: `tests/unit/test_tools_service.py`

**Interfaces:**
- Consumes: задачи 1–3; `AppConfig`, `Registry`, `SessionScope`, `UnknownBase`, `ConfigError`,
  `Client1C`, `OdataError`, `IndexRepository`, `IndexCorruptError`, `index_path`,
  `open_dictionary`, `policy_path`, `Guard`.
- Produces:
  ```python
  class ToolService:
      def __init__(self, config: AppConfig, *, client_factory=Client1C) -> None
      async def bases(self, scope: SessionScope) -> str
      async def find_entity(self, scope, *, base=None, query: str, kind=None, limit: int = 10) -> str
      async def describe_entity(self, scope, *, base=None, entity: str,
                                response_format: str = "markdown") -> str
      async def query(self, scope, *, base=None, entity: str, filter=None, select=None,
                      expand=None, orderby=None, top=None, skip=None, inlinecount=False,
                      params=None, allowed_only=False) -> str
      async def get(self, scope, *, base=None, entity: str, key, select=None, expand=None) -> str
      async def aclose(self) -> None
  ```
  Все методы возвращают готовый текст (JSON или markdown) после гейта и никогда не бросают:
  ошибка — текст `{"error": {...}}`.

Устройство:
- Словарь (`open_dictionary(home, secret)`) и `Guard` — по одному на сервис. `Client1C` и
  `BaseGate` — кэш по имени базы (клиент держит пул соединений и семафор базы). Индекс —
  `IndexRepository(index_path(home, base))` на время вызова (Global Constraints: Windows).
- Перед каждым вызовом с базой — `gate.refresh()`.
- Нет файла индекса → ошибка `entity_unknown` с подсказкой «база не проиндексирована: вызовите
  odata1c_reindex(base) или odata1c reindex <база>».
- `entity_unknown` — кандидаты из `IndexRepository.find(entity, limit=5)` в `hint`; для
  виртуальной таблицы, которой нет у регистра (например `…_Balance` у регистра оборотов), —
  перечень детей-виртуальных таблиц регистра, если он найден по префиксу имени.
- Скрытая сущность (`gate.is_hidden`) → `entity_hidden`; из `find_entity` скрытые убираются.
- `query`/`get`: `filter` → `gate.inbound_filter(entity=desc.name)`; для виртуальной таблицы
  `params["Condition"]` → `gate.inbound_filter(entity=<сущность результата>)`; ключ →
  `gate.inbound_key`; `orderby_fields` с `gate.is_protected` → `params_invalid`
  («сортировка по защищаемому полю недоступна: порядок раскрывает значения»). Затем
  `build_query`/`build_get` → `client.get(spec.path, spec.params, timeout=spec.timeout_s)` →
  `items_of` → `strip_service(keep_data_version="DataVersion" in (select or []))` →
  `gate.mask(items, entity=desc.name)` → `truncate_strings(limits.string_chars)` →
  конверт SPEC §5.1 (`entity`, `base`, `role`, `gate`, `count`, `total`, `has_more`,
  `next_skip`, `items`, `masked_fields`, `warnings`) → `fit_result(limits.result_chars)` →
  `gate.finish`. Для `get` — `item` вместо `items` и без полей страницы.
- Ответ 1С `404` на сущность, которая есть в индексе, — подсказка «структура базы могла
  измениться: вызовите odata1c_reindex» (SPEC §4.3).
- Ошибки: `UnknownBase`/`ConfigError` → `guard_only` (база не определена); `OdataError`,
  `GateError`, `QueryError`, `IndexCorruptError`, `PolicyError` → `gate.error(code, message, hint)`;
  прочие исключения → код `internal`, сообщение «внутренняя ошибка шлюза, подробности в журнале
  демона», исключение пишется в `logging` (текст исключения модели не отдаётся — в нём могут
  быть данные). `internal` — поправка SPEC §5.2 в этой задаче.
- `bases`: `registry.visible(scope)` + статус индекса из файла (`indexed_at`, `entity_count`
  через `IndexRepository.meta`), `default` сессии; пусто → `{"bases": [], "hint": "опишите базы в
  <путь>/bases.yaml или перенесите из прежнего сервера: odata1c base import <путь к env>"}`.
- `find_entity`: `IndexRepository.find(query, kind, limit)` → JSON-список (`name`, `kind`,
  `key_fields`, краткий состав полей — как отдаёт `find`).
- `describe_entity` (`describe.py`): markdown по `EntityDescription` — вид, ключ, признаки
  (табличная часть, набор записей, виртуальная таблица и её вызов, независимый регистр), таблица
  полей (имя, тип, ссылка → цель из `navigations`, составное, класс гейта из
  `effective_field_class`), навигации, дети (табличные части, наборы записей, виртуальные таблицы
  с параметрами), действия (`Post`, `Unpost`, …), для `Enum_*` — значения. `response_format="json"`
  — тот же набор словарём через `gate.finish`; markdown — через `gate.finish_text` (страж по
  не-JSON тексту целиком).

- [ ] **Шаг 1: падающие тесты** — `respx` подменяет 1С; дом с `bases.yaml` (базы `ut` роль
  `prod`, `dev` роль `dev`), индекс построен из `ut-real.edmx` (`IndexRepository.write` +
  `refresh_policy`), политика с `Catalog_Контрагенты.ИНН: inn`:

```python
async def test_query_маскирует_и_отдаёт_конверт(сервис, respx_ut):
    respx_ut.get("Catalog_Контрагенты").respond(json={
        "odata.metadata": "…", "odata.count": "2",
        "value": [{"Ref_Key": GUID1, "Description": "ООО Ромашка", "ИНН": "7707083893",
                   "Контрагент@navigationLinkUrl": "x"}]})
    текст = await сервис.query(SessionScope(), base="ut", entity="Catalog_Контрагенты",
                               select=["Ref_Key", "Description", "ИНН"], inlinecount=True)
    данные = json.loads(текст)
    assert "7707083893" not in текст and "Ромашка" not in текст
    assert данные["total"] == 2 and данные["base"] == "ut" and данные["role"] == "prod"
    assert данные["items"][0]["Ref_Key"] == GUID1
    assert "ИНН" in данные["masked_fields"]
    assert "@navigationLinkUrl" not in текст


async def test_фильтр_с_токеном_уходит_в_1С_реальным_значением(сервис, respx_ut):
    токен = await токен_инн(сервис, "7707083893")        # через маскировку ответа, как модель
    маршрут = respx_ut.get("Catalog_Контрагенты").respond(json={"value": []})
    await сервис.query(SessionScope(), base="ut", entity="Catalog_Контрагенты",
                       filter=f"ИНН eq '{токен}'")
    assert "7707083893" in маршрут.calls.last.request.url.params["$filter"]


async def test_ошибка_1С_проходит_гейт(сервис, respx_ut):
    await токен_инн(сервис, "7707083893")
    respx_ut.get("Catalog_Контрагенты").respond(400, json={"odata.error": {"code": "6",
        "message": {"lang": "ru", "value": "Сегмент пути 7707083893 не найден!"}}})
    текст = await сервис.query(SessionScope(), base="ut", entity="Catalog_Контрагенты")
    assert "7707083893" not in текст and json.loads(текст)["error"]["code"] == "odata_error"


async def test_сортировка_по_защищаемому_полю_запрещена(сервис):
    текст = await сервис.query(SessionScope(), base="ut", entity="Catalog_Контрагенты", orderby="ИНН")
    assert json.loads(текст)["error"]["code"] == "params_invalid"


async def test_неизвестная_база_и_видимость(сервис):
    текст = await сервис.query(SessionScope(bases=("dev",)), base="ut", entity="Catalog_Валюты")
    assert json.loads(текст)["error"]["code"] == "base_unknown"
    assert [б["name"] for б in json.loads(await сервис.bases(SessionScope(bases=("dev",))))["bases"]] == ["dev"]


async def test_неизвестная_сущность_с_кандидатами(сервис):
    текст = await сервис.query(SessionScope(), base="ut", entity="Catalog_Контрагент")
    ошибка = json.loads(текст)["error"]
    assert ошибка["code"] == "entity_unknown" and "Catalog_Контрагенты" in ошибка["hint"]


async def test_describe_показывает_навигации_и_классы(сервис):
    текст = await сервис.describe_entity(SessionScope(), base="ut", entity="Catalog_Контрагенты")
    assert "ИНН" in текст and "inn" in текст and "ГоловнойКонтрагент" in текст


async def test_внутренняя_ошибка_не_отдаёт_текст_исключения(сервис, monkeypatch):
    def взрыв(*a, **k):
        raise RuntimeError("секрет 7707083893")
    monkeypatch.setattr("odata1c.tools.service.build_query", взрыв)
    текст = await сервис.query(SessionScope(), base="ut", entity="Catalog_Валюты")
    assert "7707083893" not in текст and json.loads(текст)["error"]["code"] == "internal"


async def test_виртуальная_таблица_с_таймаутом(сервис, respx_ut):
    маршрут = respx_ut.get(url__regex=r".*/Balance\(.*").respond(json={"value": []})
    await сервис.query(SessionScope(), base="ut",
                       entity="AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance",
                       params={"Period": "2026-09-01"})
    assert маршрут.called
```

(Имена фикстур и способ получить токен — на усмотрение, по образцу соседних тестов; каждое
утверждение выше обязательно. `токен_инн` — маскировкой ответа 1С с этим ИНН через сам сервис.)

- [ ] **Шаги 2–4:** RED → реализация → GREEN. Поправка SPEC §5.2: код `internal`.
- [ ] **Шаг 5:** мутации — `finish` без стража в `query` (обход `gate.finish`) → падает тест
  ошибки 1С или маскировки; сортировка без проверки → падает тест сортировки. Коммит
  `feat: сервис тулов чтения`.

---

### Задача 5: демон

**Files:**
- Create: `src/odata1c/daemon.py`, `src/odata1c/__main__.py` (`python -m odata1c` = CLI)
- Modify: `src/odata1c/cli.py` (подкоманда `daemon [--foreground]`, `daemon stop`)
- Modify: `SPEC.md` §2.1 (область видимости — заголовки), §3.5 (если меняется синтаксис)
- Test: `tests/unit/test_daemon.py`, `tests/integration/test_daemon_http.py`

**Interfaces:**
- Consumes: `ToolService` (задача 4), `load_config`, `ensure_gate_secret`, `ensure_home`,
  `check_file_permissions`.
- Produces:
  ```python
  SCOPE_BASES_HEADER = "x-odata1c-bases"      # ctx.headers — ключи в нижнем регистре
  SCOPE_DEFAULT_HEADER = "x-odata1c-default"
  INSTRUCTIONS: str                            # ≤ 2048 символов, текст SPEC §5 «Server instructions»
  def scope_from_headers(headers: Mapping[str, str] | None) -> SessionScope
  def build_server(service: ToolService, limits: Limits) -> MCPServer
  async def serve(home: pathlib.Path, *, port: int | None = None,
                  ready: asyncio.Event | None = None) -> None
  def daemon_url(port: int) -> str             # f"http://127.0.0.1:{port}/mcp"
  def is_listening(port: int, timeout: float = 0.5) -> bool
  def spawn_detached(home: pathlib.Path) -> None   # запуск `python -m odata1c daemon --foreground --home …`
  def stop(home: pathlib.Path) -> bool         # по daemon.pid
  ```

Тулы задачи (остальные — задача 7): `odata1c_bases`, `odata1c_find_entity`,
`odata1c_describe_entity`, `odata1c_query`, `odata1c_get` — все `read_only_hint=True`,
`meta={"anthropic/maxResultSizeChars": limits.result_chars}`, `structured_output=False`,
`ctx: Context` для заголовков. Описания тулов — русские, одна-две фразы, с подсказкой порядка
(«сначала find_entity и describe_entity, потом query с select»). Параметры — как в SPEC §5
(`select`/`expand` — список строк; `key` — строка или объект; `params` — объект).

`serve`: `ensure_home`, `ensure_gate_secret`, `load_config` (предупреждения конфигурации и
`check_file_permissions(bases.yaml)` — в журнал), порт занят → выход с понятной ошибкой;
`daemon.pid` пишется после старта и удаляется при выходе; журнал — `home/logs/daemon.log`
(`logging`, уровень INFO; текст исключений 1С туда можно — это локальный файл владельца).
`--foreground` — работать в текущем процессе; без него `odata1c daemon` вызывает
`spawn_detached` и ждёт готовности порта до 15 с. `spawn_detached`: Windows —
`creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`, иначе `start_new_session=True`;
stdout/stderr — в `home/logs/daemon.log`. `stop`: `os.kill(pid, signal.SIGTERM)`, удалить pid-файл.

- [ ] **Шаг 1: падающие тесты**

`tests/unit/test_daemon.py` (в памяти, `InMemoryTransport`, сервис на поддельной 1С из задачи 4):

```python
async def test_тулы_объявлены_с_аннотациями_и_meta(сервер):
    async with InMemoryTransport(сервер) as (r, w), ClientSession(r, w) as сессия:
        await сессия.initialize()
        тулы = {т.name: т for т in (await сессия.list_tools()).tools}
    assert set(тулы) >= {"odata1c_bases", "odata1c_find_entity", "odata1c_describe_entity",
                         "odata1c_query", "odata1c_get"}
    for т in тулы.values():
        assert т.annotations.read_only_hint is True
        assert т.meta["anthropic/maxResultSizeChars"] == 120000
        assert т.output_schema is None


async def test_результат_только_текстом(сервер):
    async with InMemoryTransport(сервер) as (r, w), ClientSession(r, w) as сессия:
        await сессия.initialize()
        результат = await сессия.call_tool("odata1c_bases", {})
    assert результат.is_error is False
    assert результат.structured_content is None
    assert len(результат.content) == 1
    assert "bases" in json.loads(результат.content[0].text)


def test_scope_from_headers():
    assert scope_from_headers(None) == SessionScope()
    assert scope_from_headers({"x-odata1c-bases": "ut, buh", "x-odata1c-default": "ut"}) == \
        SessionScope(bases=("ut", "buh"), default="ut")


def test_instructions_не_длиннее_2_кб():
    assert len(INSTRUCTIONS) <= 2048
```

`tests/integration/test_daemon_http.py` — настоящий Streamable HTTP: `serve()` в задаче на
свободном порту (`socket.bind(("127.0.0.1", 0))`), два клиента `httpx2.AsyncClient` с разными
`X-Odata1c-Bases` (`ut` и `dev`) → `odata1c_bases` у каждого возвращает свою базу; остановка
`should_exit` через событие/отмену; pid-файл создан и удалён.

- [ ] **Шаги 2–4:** RED → реализация → GREEN. Поправка SPEC §2.1: «Необязательные аргументы
  `--bases` и `--default` лаунчер передаёт демону заголовками `X-Odata1c-Bases` и
  `X-Odata1c-Default` на каждом запросе (поправка 2026-09-10: SDK не сохраняет `_meta`
  инициализации); прямые HTTP-клиенты могут прислать те же заголовки».
- [ ] **Шаг 5:** коммит `feat: демон MCP с тулами чтения`.

---

### Задача 6: лаунчер и сквозная проверка

**Files:**
- Create: `src/odata1c/launcher.py`, `tests/integration/fake_1c.py`,
  `tests/integration/test_end_to_end.py`
- Modify: `src/odata1c/cli.py` (подкоманда `mcp [--bases a,b] [--default a] [--url URL]`),
  `pyproject.toml` (`httpx2>=2.12` в зависимостях — клиент лаунчера)
- Test: `tests/unit/test_launcher.py`

**Interfaces:**
- Consumes: `daemon.is_listening`, `daemon.spawn_detached`, `daemon.daemon_url`, `ensure_home`,
  `load_config` (порт).
- Produces: `async def run_launcher(home, *, bases: list[str] | None, default: str | None,
  url: str | None) -> None`; `def build_proxy(upstream: ClientSession, holder) -> Server`.

Устройство: `ensure_home`; адрес — `--url` или `daemon_url(port из daemon.yaml)`; если порт не
слушается — `spawn_detached(home)` и ожидание до 15 с (иначе сообщение в stderr и выход с кодом 1);
`httpx2.AsyncClient(headers=заголовки области, timeout=httpx2.Timeout(10, read=None))` →
`streamable_http_client(url, http_client=…)` → `ClientSession(read, write,
elicitation_callback=переслать)`; `initialize`; `build_proxy` — семь обработчиков, каждый
вызывает соответствующий метод апстрим-сессии и возвращает его результат; `переслать` —
`holder.session.elicit_form(params.message, params.requested_schema)`, где `holder.session` —
downstream-сессия, запомненная первым обработчиком (`ctx.session`); если её ещё нет — отказ
`ElicitResult(action="decline")`. `stdio_server()` → `proxy.run(...)`.

- [ ] **Шаг 1: тест прокси в памяти** (`tests/unit/test_launcher.py`): демон-заглушка `MCPServer`
  с тулом, ресурсом, шаблоном ресурса, промптом и тулом с `ctx.elicit`; два перехода
  `InMemoryTransport` (клиент → прокси → апстрим → заглушка); проверить `list_tools`, `call_tool`,
  `list_resources`, `list_resource_templates`, `read_resource`, `list_prompts`, `get_prompt` и
  прохождение elicitation (клиент отвечает `accept`, тул получает ответ).

- [ ] **Шаг 2: поддельная 1С** (`tests/integration/fake_1c.py`): Starlette-приложение на
  свободном порту: `GET …/standard.odata/$metadata` → `ut-real.edmx`; `GET
  …/Catalog_Контрагенты` → JSON с ИНН `7707083893` и названием `ООО Ромашка`; прочее → `404`
  в формате `odata.error`. Basic-аутентификация не проверяется.

- [ ] **Шаг 3: сквозной тест** (`tests/integration/test_end_to_end.py`): временный дом —
  `daemon.yaml` со свободным портом, `bases.yaml` с базой `ut` (роль `prod`) на адрес поддельной
  1С; `odata1c reindex ut` через `cli.main([...])`; лаунчер — дочерний процесс через
  `mcp.client.stdio.stdio_client(StdioServerParameters(command=sys.executable, args=["-m",
  "odata1c", "--home", дом, "mcp"]))`; лаунчер сам поднимает демон. Проверить: `odata1c_bases`
  видит `ut`; `odata1c_query` по `Catalog_Контрагенты` отдаёт токены, в тексте ответа нет
  `7707083893` и `Ромашка`; `odata1c_describe_entity` отвечает. В `finally` —
  `daemon.stop(дом)` и ожидание освобождения порта. Тест помечен `@pytest.mark.timeout`,
  если плагин есть, иначе ограничь ожидания явно (не дольше 60 с суммарно).

- [ ] **Шаг 4:** GREEN, полный прогон, ruff; коммит `feat: лаунчер odata1c mcp и сквозная проверка`.

После задачи 6 оркестратор проверяет лаунчер на живой базе `trade_dev` (скрипт через
`stdio_client`, вопросы из задачи 9) и сообщает владельцу команду подключения:
`claude mcp add odata1c -- uv run --directory <репозиторий> odata1c mcp`.

---

### Задача 7: реиндекс из тула, справочник, `raw_get`, ресурсы, промпт

**Files:**
- Create: `src/odata1c/tools/info.py`
- Modify: `src/odata1c/tools/service.py`, `src/odata1c/daemon.py`
- Test: `tests/unit/test_tools_service.py`, `tests/unit/test_daemon.py`

**Interfaces:**
- Produces: `ToolService.reindex(scope, *, base=None, force=False) -> str`,
  `ToolService.info(topic: str) -> str`, `ToolService.raw_get(scope, *, base=None, path: str,
  query: dict | None = None) -> str`, `ToolService.resource_policy(base) -> str`,
  `resource_index(base) -> str`; тулы `odata1c_reindex` (`idempotent_hint=True`),
  `odata1c_info`, `odata1c_raw_get` (`read_only_hint=True`); ресурсы `odata1c://cheatsheet`,
  `odata1c://policy/{base}`, `odata1c://index/{base}`; промпт `explore(base)`; фоновая проверка
  `$metadata` раз в `reindex_check_hours`.

Правила:
- `reindex`: `index.reindex(base, client, home, force, classifier=classifier_for(base))`, при
  `changed` — `refresh_policy`, `guard.rebuild()`, `registry.set_indexed`; ответ — разница
  (`added_entities`, `removed_entities`, `new_sensitive_fields` — первые 50 каждого, с числом
  всего), `warnings` разбора; через `gate.finish`.
- `info(topic)`: темы `naming`, `standard_fields`, `registers`, `keys`, `filter`, `tokens`,
  `recipes`, `all` (SPEC §5; `write_protocol` — одна строка «запись появится на этапе M2»).
  Тексты — в `info.py` константами, по фактам проб (виртуальные таблицы — действия регистра,
  `$expand` требует навигацию в `$select`, `odata.count`, `Condition`, составные поля, токены
  непрозрачны). Неизвестная тема → `params_invalid` с перечнем.
- `raw_get`: `path` — относительный путь внутри `standard.odata/`: без схемы, без `..`, без
  ведущего `/`, не `$metadata` (отказ `params_invalid`); `query` — словарь; `$filter` проходит
  `gate.inbound_filter` (сущность — первый сегмент пути до `(` или `/`), значения прочих
  параметров с `[[` → `params_invalid`; ответ — как у `query`/`get` по форме (список или объект),
  маска по сущности первого сегмента.
- Ресурсы: `cheatsheet` = `info("all")`; `policy/{base}` — текст `policy.yaml` базы (в нём
  имена полей и классы, значений нет) через стража; `index/{base}` — сводка индекса (число
  сущностей по видам, `indexed_at`).
- Промпт `explore(base)` — «Покажи состав базы {base}: вызови odata1c_bases, затем
  odata1c_find_entity по основным справочникам и документам, опиши, что нашёл; помни правила
  работы с токенами».
- Фоновая проверка: задача asyncio в `serve`, раз в `reindex_check_hours` для каждой базы с
  индексом — `reindex(force=False)` (сама решит «без изменений»); ошибки — в журнал и
  `registry.set_error`.

Тесты: `reindex` на поддельной 1С меняет `indexed_at` и возвращает разницу; `info("all")` ≤
разумного объёма и упоминает `[[`; `raw_get` отказывает на `../x`, `$metadata`, токене вне
`$filter`; маскирует ответ; ресурсы и промпт видны в `list_resources`/`list_resource_templates`/
`list_prompts` (в памяти). Коммит `feat: реиндекс, справочник, raw_get, ресурсы и промпт`.

---

### Задача 8: рецепты

**Files:**
- Create: `src/odata1c/recipes/model.py`, `src/odata1c/recipes/render.py`
- Modify: `src/odata1c/templates/recipes/ut.yaml`, `bp.yaml`, `zup.yaml`,
  `src/odata1c/tools/service.py`, `src/odata1c/daemon.py`
- Test: `tests/unit/test_recipes.py`

**Interfaces:**
- Produces: `load_recipes(path) -> RecipeBook` (pydantic: `version`, `recipes: dict[str, Recipe]`;
  `Recipe`: `title`, `description`, `entity`, `params: dict[str, Param]` (`type`, `required`,
  `description`), `virtual: dict[str, str | list[str]]`, `select`, `filter: str | list[str]`,
  `orderby`, `top`); `render(recipe, values: dict) -> QueryArgs` (аргументы для
  `ToolService.query`); `RecipeError(code, message, hint)` с кодами `recipe_unknown`,
  `recipe_param`; тул `odata1c_recipe(base, name?, params?)`; ресурс `odata1c://recipes/{base}`.

Правила SPEC §8: типы `datetime`, `date`, `guid`, `string`, `int`, `decimal`, `bool` → литерал
через `odata_literal` (задача 2); подстановка `{имя}` только в позиции литерала — значение
вставляется уже литералом, строковая интерполяция в текст условия невозможна; условия `filter` и
`virtual.Condition` — список, склеивается `and`; необязательный параметр без значения удаляет
условия, где он встречается; значения параметров проходят `gate.inbound_value` (токен →
значение); `entity` рецепта проверяется по индексу — без `name` тул помечает неприменимые рецепты
(`applicable: false` и подсказка `find_entity`).

Шаблон `ut.yaml`: 4–6 рецептов для УТ 11 (остатки товаров на складах, дебиторская задолженность
клиентов, кредиторская задолженность поставщикам, продажи за период, денежные средства), имена
регистров и полей — **только проверенные** по `tests/fixtures/edmx/probe.full.edmx` (локальный
полный дамп; если его нет — по `ut-real.edmx` и сообщить) и прогоном на живой базе оркестратором
после задачи. `bp.yaml`, `zup.yaml` — пустой `recipes: {}` с комментарием «нет базы для проверки
имён регистров; заполняется, когда появится база».

Тесты: рендер литералов всех типов; удаление условия с пустым необязательным параметром;
обязательный без значения → `recipe_param`; токен в параметре превращается в значение; подстановка
не даёт внедрить `' or 1 eq 1`; рецепт с неизвестной сущностью помечен неприменимым; каждый
рецепт `ut.yaml` загружается и его `entity` есть в полном дампе (тест пропускается, если дампа
нет). Коммит `feat: рецепты и тул odata1c_recipe`.

---

### Задача 9: приёмка на живой базе и документация

**Files:**
- Create: `tools/probes/m1d_live_check.py`, `docs/probes/M1d-live-check.md`
- Modify: `AGENTS.md` (статус, команды подключения), `README.md` (как подключить)

Скрипт — `stdio_client` к `odata1c mcp --home <рабочий дом>` (база `trade_dev`), 10 вопросов
чтения (SPEC §12 evals, без модели — вызовы тулов и проверки ответа):
1. `bases` — видна `trade_dev`, роль `dev`;
2. `find_entity("контрагенты")` — `Catalog_Контрагенты` первым;
3. `describe_entity("Document_РеализацияТоваровУслуг")` — навигации и `Post`;
4. `query Catalog_Валюты` с `select`, `inlinecount` — `total` число;
5. `query Document_РеализацияТоваровУслуг` `top=3`, `select=[Number, Date]`, `expand=[Контрагент]` —
   у записей есть `Контрагент.Description` (молчаливое нераскрытие — провал);
6. `get` документа по `Ref_Key` из п. 5;
7. остатки `AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance` на дату — ответ за время
   не больше `virtual_timeout_s`;
8. обороты `…_Turnovers` без периода — `params_invalid`, не запрос в 1С;
9. `Balance` у регистра оборотов — `entity_unknown` со списком доступных виртуальных таблиц;
10. рецепт `ut` из шаблона — ответ без ошибок.

Для проверки гейта на живых данных — второй прогон с временной базой-копией `trade_dev` в
`bases.yaml` рабочего дома с ролью `prod` (уровень `identifiers+names`) под другим именем
(`trade_dev_prod`, удаляется после прогона): в ответах `query Catalog_Контрагенты` с `select` ИНН
и `Description` нет ни одного 10/12-значного ИНН и ни одного значения из `Description`
незамаскированным (сверка — повторным запросом той же выборки на базе `trade_dev` уровня `off`).

Результат — отчёт с фактами (время ответов, число записей, найденные расхождения). `AGENTS.md` —
статус «M1d готов: тулы чтения», раздел команд: `odata1c daemon`, `odata1c mcp`, подключение к
Claude Code. Коммит `docs: приёмка M1d на живой базе`.

---

## Вне плана

- Запись (M2): `create`/`update`/`mark_for_deletion`/`delete_record`/`action`/`commit`/`undo`/
  `journal`, pending-операции по `mcp-session-id`, elicitation подтверждения.
- Служба Windows, `doctor`, плагин Claude Code — M3.
- Лишний отказ `ИНН eq '<токен>' and year(Date) eq 2026` (итоговое ревью M1c, отложено): отбор по
  защищённому полю токеном не сочетается с функцией над другим полем; обход — диапазон дат.
- Восстановление сессии после смерти демона (Ruling 15 раунда правок 3 задачи 6): сейчас сторожок
  живости обнаруживает обрыв и **необратимо** замыкает сессию — клиенту остаётся переподключить
  шлюз. Нужно вместо замка поднимать демон заново и переустанавливать апстрим-сессию. Требуется
  независимо от способа запуска демона: он может упасть сам или быть остановлен из другой сессии.
- Проксирование лаунчером уведомлений, прогресса, `params.meta` и отмены (Ruling 12 раунда правок
  задачи 6). Отложено сознательно: виртуальные таблицы идут до 180 с, и всё это время клиент не
  видит признаков жизни — работа нужная, но со своим тестом, а не довеском к фикс-раунду. Сюда же
  объявление возможностей elicitation апстриму по возможностям реального клиента: сейчас лаунчер
  заявляет поддержку всегда, независимо от того, умеет ли downstream-клиент спрашивать
  пользователя.

### Блокеры M2 — обязательны до первого пишущего тула

Этап M1d закрыт итоговым ревью атакующим (Approved, 2026-09-12). Ниже — то, что в режиме чтения
стоит неполным или неточным ответом, а на этапе записи превращается в запись не в тот объект:
модель готовит изменение по токенам, пользователь подтверждает превью на тех же токенах и не может
отличить одно реальное значение от другого.

- **Несколько написаний одного токена** (ревью 7): телефоны в разном формате, ИНН с пробелами и без,
  названия в «ёлочках» и прямых кавычках получают один токен, а в 1С уходит одно написание — вторая
  запись по токену не находится. Предложение исполнителя: раскрывать `eq` в `or` по всем написаниям,
  известным словарю; оракулом это не становится, если каждое написание проходит `revealed.add`.
  В M1 модель предупреждена справочником (`odata1c_info`, тема `tokens`).
- **M-1**: строка 1С, дословно равная известному токену, показывается модели этим токеном — два разных
  значения под одним токеном; на записи модель может записать чужой ИНН.
- **M-6**: реальное значение лежит в атрибуте `ScrubbedText.original` до маскировщика. Сейчас его
  читает только маскировщик, но `Client1C.post/patch` разбирают ответ без раннего прохода — на M2 это
  дыра. Укрепление: хранить исходное в наборе раскрытого на время вызова и проверять, что в `finish`
  не приходит ни одного `ScrubbedText`.
- **I-2**: два известных названия пересекаются в тексте — хвост второго виден модели («Юг Альфа
  Север» → `[[org:2]] Север`), с отбором и без. Лечение уже есть: правило вложенности из Ruling 32
  применить к самому слою названий с поиском пересекающихся совпадений.
- **M-3**: признак «мусорного» токена у цифровых классов не отличает цифры значения от цифр вписанного
  рядом токена.

**Закрыты раундом блокеров M2 (2026-09-12)**, отчёт — `.superpowers/sdd/2026-09-10-m1d-mcp-surface/
task-m2-blockers-report.md`, раздел «Интерфейс для M2» там же — контракт пишущих тулов:

- несколько написаний — Б-1: отбор `eq`/`ne` по токену раскрывается в группу всех написаний поля
  (живьём на `trade_dev` — 37 групп из 37 найдены целиком, одно написание находило не все в 23),
  одно значение и запись без однозначного написания — `token_ambiguous`; для записи —
  `BaseGate.inbound_write` (написание из текущего значения поля, иначе единственное, иначе отказ);
- M-1 — Б-2: строка 1С с токеном шлюза выдаётся токеном `lit`, раскрывается только в своём поле;
- M-6 — Б-3: исходное значение в наборе вызова, ранний проход у `post`/`patch`/`delete`, сторож
  `ScrubbedText` в `finish`;
- I-2 — Б-4: поиск всех совпадений и правило вложенности в слое названий маскировщика, стража и в
  раннем проходе (`gate/overlaps.py`); закрыт и остаток «Бета» Ruling 32;
- M-3 — Б-5: у цифровых классов след двойной маскировки — только если вне токенов нет цифр.

### Хвост M1d — не блокирует M2

- **M-5**: свободный текст маскируется раньше, чем поле с классом того же ответа выдаёт токен длинному
  названию — в первом ответе название видно рядом со своим токеном (живьём у банковских счетов).
  Лечение: маскировщик в два прохода — сначала поля с классом, потом свободный текст.
- **M-7**: отбор по токену короткого КПП с `inlinecount` превращает счётчик `"odata.count": "1"` в
  токен, `int()` падает, вызов отвечает `internal`. Следствие Ruling 27, не утечка.
- `stop()` на осиротевшем `daemon.pid` после перезагрузки может снять чужой процесс с тем же номером —
  сверять время создания процесса с временем изменения pid-файла.
- Гонка двух одновременных реиндексов одной базы — окно в миллисекунды, закрывается в слое `index/`.
- Рецепты отдают организацию, счёт и прочее ключами без названий — модели нужен второй запрос;
  стоит добавить `expand` в поставляемые шаблоны.
- Эвристика ФИО по подстроке ложно помечает `ГрафикРаботыСотрудников`, `ДоговорЗаймаСотруднику`,
  `ДолжностьРуководителя`, `РешениеРуководителя` — на живой базе пустые, вреда нет; расширять
  исключение только полным именем (рядом `ФИОРуководителяКонтрагента`).

### Хвост контактной информации (ревью 8–9, не блокирует M2)

- **Подсказка «тел» слишком широкая** (обратная сторона I-2): слово-подсказка ловится с начала
  слова, поэтому `телевизор`, `телега`, `Телеком`, `ТелеграфноеАгентство` рядом с 11-значным кодом
  дают `[[phone:…]]` в свободном тексте. Лишняя маскировка, не утечка. Сузить до `тел[.:]|телефон`.
- **Сторож класса пути в обходчике `$orderby`**: явная проверка снимается без красного теста —
  обходчик отказывает сам на звене табличной части. Добавить прямой юнит на `path_class(...) ==
  contact`, чтобы рефакторинг обхода не превратил отказ в разрешение.
- **`Тип` добавляется по суффиксу имени** и при уровне `off`: сущность `…_КонтактнаяИнформация` без
  поля `Тип` получит от 1С 400. Во всех восьми табличных частях БСП поле есть — теоретично.
- Одно похожее на ФИО значение в `КонтактныеЛицаПартнеров.ДолжностьПоВизитке` (свободный текст).
- `ОтветственныеЛицаОрганизаций.Description` вида «ФИО, доверенность № … от …» уходит одним токеном
  вместе с номером и датой доверенности.
- Отбор `Представление ne ''` отклоняется (пустая строка — тоже открытый литерал).
- Поля-URL получают класс `addr` по префиксу «адрес».
- Слитный городской и казахский мобильный номер без слова-подсказки в свободном тексте не ловятся.
- Строки контактной информации с пустым `Тип` классифицируются по содержимому — на другой базе
  возможны разные классы в разных выборках.
- **Для M2**: входные методы гейта требуют `shape` и `path_class` — пишущие тулы обязаны их передавать.

### СНИЛС и номера фонда — отложено до подключения БП (решение владельца 2026-09-12)

Проверено на `trade_dev` от данных: СНИЛС наружу не уходит нигде. В УТ 11 у `Catalog_ФизическиеЛица`
поля СНИЛС нет вовсе; 12 полей со СНИЛС (доверенности, МЧД, подключения МПЭПД) классифицированы
`snils`; дополнительного реквизита «СНИЛС» нет; в 1000 строк дополнительных реквизитов и 1669 записях
свободного текста — ни одного значения с верной контрольной суммой СНИЛС.

Владелец решил заняться этим **на примере БП** — там СНИЛС у физлиц есть, и поверхность шире
(кадровые справочники, регистры сведений физлиц, отчётность в фонды). Что сделать тогда:

- **Детектор СНИЛС по содержимому** (`gate/detectors.py`, `ШАБЛОНЫ["snils"]`) знает две формы:
  11 цифр слитно и каноническую `112-233-445 95`. Пропускает `112-233-445-95` (все дефисы) и
  `112 233 445 95` (все пробелы) — формы, которые вводят руками. Расширение безопасно: контрольная
  сумма СНИЛС пропускает около одного случайного числа из ста. Обратный тест инварианта 6 обязателен
  (суммы с разбивкой разрядов, номера документов, коды).
- **`Catalog_Организации.ИПРегистрационныйНомерПФР`** — регистрационный номер ИП как страхователя.
  Не СНИЛС и не номер человека, но ИНН ИП маскируется (`inn`), и для последовательности этот номер
  стоит закрыть. Номера фонда организаций (`РегистрационныйНомерПФР`, `КодОрганаПФР`) — сведения
  о юрлице, открыты.
- **Дополнительные реквизиты** — аналог контактной информации: строка «свойство → значение», класс
  значения задаётся свойством. Если в базе заведён реквизит «СНИЛС», его значение сейчас защищает
  только детектор по содержимому. Решить, нужен ли класс по названию свойства (как `Тип` в КИ).
- **Приёмка на БП — от класса данных к полям**: найти каждое значение с верной контрольной суммой
  СНИЛС во всех строковых полях реального ответа, включая дополнительные реквизиты, JSON и XML.

### Документы, удостоверяющие личность — туда же, на примере БП (находка ревью задачи 5 M2, 2026-09-12)

У `InformationRegister_ДокументыФизическихЛиц` (и его срезов `_SliceFirst`/`_SliceLast`) политика
`auto` дала класс `doc` полям `Серия`, `КемВыдан`, `КодПодразделения`, но **не** `Номер` — сам номер
паспорта, — и не `Представление` (строка вида «Паспорт гражданина РФ, серия …, № …, выдан …»), и не
`ИмяЛатиницей` / `ФамилияЛатиницей` (ФИО латиницей, класс `person`). На `trade_dev` у регистра 0
записей — живой утечки нет; в БП и ЗУП регистр заполнен у каждого сотрудника. Причина вероятна:
`Номер` исключён из классификации как номер документа (инвариант 6), а исключение не различает номер
объекта 1С и номер удостоверения в регистре физлица. Что сделать вместе со СНИЛС:

- правило политики: `Номер` сущности, у которой есть `Серия` класса `doc` (или сущность из списка
  документов физлиц), — `doc`; `Представление` такой сущности — `doc` целиком; `…Латиницей` у
  регистра физлица — `person`;
- детектор серии и номера паспорта в свободном тексте (`45 06 123456`, `4506 123456`) — проверить,
  есть ли, и обратный тест инварианта 6 (номера документов 1С, суммы);
- приёмка на БП — от класса данных: все значения регистра документов физлиц ищутся во всём ответе.

### Бэклог ревью раунда 3 блокеров M2 (2026-09-13, Minor)

- **Р3-3.** Адрес площадки или производственного объекта организации-ИП выходит открытым: в регистре СУЗ
  и документах ИС МП нет `ЮрФизЛицо`, пункт 2 Ruling 34 (адреса ИП закрываются) не срабатывает. На
  `trade_dev` все организации — юрлица.
- **Р3-4.** У близнецов `…Строкой` нет класса в политике: `АвторСтрокой` (ФИО пользователя) и адресные
  близнецы ИС МП. Текстовое представление структуры должно получать класс структуры.
