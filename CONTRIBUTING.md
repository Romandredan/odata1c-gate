# Разработка odata1c-gate

Этот документ — для тех, кто правит сам шлюз: собирает его из рабочей копии, готовит выпуск или
меняет архитектуру. Обычному пользователю он не нужен — установка описана в
[README.md](README.md) и [docs/install.md](docs/install.md).

## Проектные документы

- `SPEC.md` — полная спецификация: архитектура, конфигурация, индекс метаданных, MCP-тулы, гейт,
  протокол записи, рецепты, клиент 1С OData, лимиты, поставка, тестирование, этапы.
- `CONTEXT.md` — глоссарий: термины проекта и запрещённые синонимы (`_Avoid_`).
- `docs/adr/` — архитектурные решения (ADR) с обоснованиями; формат — frontmatter
  (`status: accepted|superseded|amended`) плюс разделы `Considered Options` и `Consequences`.
  Проверяйте статус во frontmatter: решение могло быть отменено или дополнено позже.
- `docs/plans/` — планы реализации по задачам.
- `docs/probes/` — отчёты технических проверок и приёмок на живой базе 1С.
- `AGENTS.md` — общая картина проекта для AI-агентов: архитектура, стек, инварианты, процесс,
  команды разработки. Самый быстрый способ сориентироваться в коде.

Порядок чтения для новой задачи: сперва `CONTEXT.md` — без словаря термины в остальных документах
не разберутся однозначно; затем `AGENTS.md` — общая картина; затем нужный раздел `SPEC.md` (он
большой — ищите заголовок и читайте точечно); `docs/adr/` — только когда меняется само решение.

## Структура репозитория

```text
src/odata1c/          пакет PyPI odata1c-gate: демон, лаунчер, CLI
  config/             bases.yaml и daemon.yaml, роли, валидация, права файлов
  registry/           реестр баз, статус индекса, видимость по сессии
  client1c/            httpx-пул на базу, IBSession, семафор, маппинг ошибок 1С
  index/               парсер EDMX → metadata.sqlite, реиндекс, нечёткий поиск
  gate/                детекторы реквизитов, словарь, подмена в обе стороны, страж
  write/               разрешения, pending-операции, commit, журнал, undo
  recipes/             загрузка рецептов и рендеринг параметров в OData-литералы
  tools/               регистрация тулов, ресурсов, промптов, server instructions
  templates/           файлы-шаблоны в поставке: конфигурация и рецепты УТ/БП/ЗУП
  cli.py               команды odata1c: init, base, reindex, policy, recipe, doctor, reveal, daemon, mcp
  daemon.py            демон: тулы, ресурсы, промпт, MCP Streamable HTTP на 127.0.0.1
  launcher.py          лаунчер: stdio-прокси демону, подъём демона, проброс elicitation
plugin/                плагин Claude Code: манифест, .mcp.json, навыки, хук, агент, evals
.claude-plugin/        маркетплейс плагина для claude plugin marketplace add
tests/                 unit / property / integration + образцы EDMX
tools/                 bump_version.py, plugin_dev_copy.py, probes/ — скрипты проверок и приёмки
docs/adr/              архитектурные решения
docs/plans/            планы реализации по этапам
docs/probes/           отчёты технических проверок и приёмок на живой базе
```

## Что не попадает в репозиторий

`bases.yaml` хранит пароли 1С открытым текстом, поэтому в git не коммитятся конфигурация рабочей
машины, env-файлы, базы SQLite (словарь, индекс, журнал), выгрузки `$metadata` и логи — см.
[.gitignore](.gitignore). В поставке живут только файлы-шаблоны в `src/odata1c/templates/`, в
тестах — урезанные образцы `$metadata`, не полные дампы.

## Сборка, тесты, команды

Python ≥ 3.12, менеджер окружения `uv`:

```text
uv sync                                # зависимости, включая группу dev
uv run pytest -q                       # все тесты; tests/unit -q — только юнит
uv run ruff check .                    # линт
uv run ruff format --check .           # формат
uv run odata1c <команда>               # CLI из рабочей копии
claude plugin validate plugin/         # манифест плагина, навыки, хук, агент
```

## Выпуск версии

Единственный источник версии — `src/odata1c/__about__.py`; в `plugin/.claude-plugin/plugin.json`,
`plugin/.mcp.json` и `.claude-plugin/marketplace.json` она повторяется, и юнит-тест
`tests/unit/test_versions_agree.py` требует равенства всех четырёх. Переписать версию во всех
четырёх местах разом:

```text
uv run python tools/bump_version.py <версия>
```

**`main` двигается только выпусками.** Маркетплейс отдаёт плагин из ветки по умолчанию, а
`plugin/.mcp.json` закрепляет версию дистрибутива на PyPI: любая правка `main` немедленно
доходит до всех, кто поставил плагин. Поэтому в `main` попадает только коммит выпуска с тегом
`v<версия>`, а рабочая ветка между выпусками живёт с версией `X.Y.Z.devN` во всех четырёх
местах (`odata1c --version` тогда честно говорит, что это не выпуск). `claude plugin validate`
версию с суффиксом `.devN` принимает.

```text
uv run python tools/bump_version.py 0.1.0
uv run pytest -q
git commit -m "release: 0.1.0"
git tag v0.1.0
git push origin main --tags
```

Перед тегом — запись в [CHANGELOG.md](CHANGELOG.md) с датой выпуска. Тег `v<версия>` запускает
`.github/workflows/publish.yml`: `uv build`, публикация на PyPI через trusted publishing и
приложение колеса и sdist к GitHub Release. Токенов PyPI в репозитории нет.

## Trusted publishing на pypi.org

Настраивается один раз, до первого выпуска, в разделе Publishing проекта на pypi.org (для первого
выпуска — в Pending publishers, до того как проект появился):

| Поле | Значение |
|---|---|
| PyPI Project Name | `odata1c-gate` |
| Owner | `Romandredan` |
| Repository name | `odata1c-gate` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

Environment `pypi` должен существовать и в настройках репозитория GitHub, а у публикующей задачи —
разрешение `id-token: write`: без него обмен токена не состоится.

## Evals плагина

Дела `claude plugin eval` лежат в `plugin/evals/`: десять вопросов чтения, три сценария через
рецепты и одно дело на сохранение рецепта. Они ходят в живую базу 1С, поэтому в непрерывную
проверку не входят — прогон делается в приёмке и перед выпуском.

Точная команда прогона — в `plugin/evals/README.md` (единственный источник: флаги меняются вместе
с делами). Два флага обязательны всегда: `--no-publish` (иначе отчёт с ответами модели, включая
подписи баз из `bases.yaml`, публикуется на claude.ai) и `--mocks off` с перечнем разрешённых тулов
чтения в `--allow-tools`; команда доступна за флагом окружения `CLAUDE_CODE_WALNUT_SPIRE=1`.

`tools/plugin_dev_copy.py` делает копию плагина в `build/plugin-dev`, указывающую на рабочую
копию репозитория (`uv run --directory …`), — так evals проверяют ещё не опубликованную версию.
Порог: пройдены все дела и ни одного реального значения защищаемых классов в ответах. Дело
сохранения рецепта пишет один файл в библиотеку рецептов домашнего каталога — после прогона его
удаляют.

## Разработка из рабочей копии

```text
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run odata1c <команда>
```

Подключить к Claude Code рабочую копию вместо пакета, проверить плагин
(`claude plugin validate plugin/`) и прогнать приёмки на живой базе — «Запуск и подключение к
Claude Code» в [AGENTS.md](AGENTS.md).
