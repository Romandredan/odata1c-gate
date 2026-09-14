# Evals плагина `odata1c`

Четырнадцать дел для `claude plugin eval` (см. также SPEC §7 и AGENTS.md, раздел «Запуск и
подключение»). Формат — раскладка `prompt.md` + `graders/*.md` на каждое дело; данные — живая база
`trade_dev` через дев-копию плагина (`tools/plugin_dev_copy.py`). Только чтение — кроме дела 14,
которое пишет один файл рецепта в домашний каталог шлюза (см. ниже).

## Перед прогоном

1. `uv sync` в рабочей копии — один раз.
2. Демон владельца должен слушать `127.0.0.1:7171` с настроенной и проиндексированной базой
   `trade_dev` (роль `dev`, гейт `identifiers+names`) — эти evals читают её напрямую, ничего не
   поднимают сами.
3. **Дев-копия плагина** — пересобирать перед КАЖДЫМ прогоном после правки любого файла в
   `plugin/` (включая сами файлы evals — они читаются из `build/plugin-dev/evals/`, а не из
   `plugin/evals/`):

   ```text
   uv run python tools/plugin_dev_copy.py
   ```

4. **`claude plugin eval` — early access функция этой версии Claude Code (2.1.267), гейт по
   организации.** Без включения команда отвечает `plugin eval is currently in early access` и
   завершается кодом 1. Включить для машин вне ролаута (в том числе для CI-раннеров) — переменная
   окружения `CLAUDE_CODE_WALNUT_SPIRE=1`, установленная в shell перед вызовом (НЕ в
   `.claude/settings.json` репозитория — закоммиченное там значение сознательно не учитывается).

   PowerShell:

   ```powershell
   $env:CLAUDE_CODE_WALNUT_SPIRE=1
   ```

   Git Bash:

   ```bash
   export CLAUDE_CODE_WALNUT_SPIRE=1
   ```

   Флага `--trust-plugin` в этой версии CLI нет (несмотря на более раннюю версию команды в плане
   M3) — диалог доверия к каталогу плагина пропускается автоматически, когда Claude Code запущен
   неинтерактивно (без TTY), а `claude plugin eval` всегда запускает свои прогоны именно так.

## Прогон дел 1–13 (чтение и рецепты; дело 14 — отдельно, см. ниже)

```text
$env:PYTHONUTF8=1
$env:CLAUDE_CODE_WALNUT_SPIRE=1
claude plugin eval build/plugin-dev --mocks off --runs 1 --ablation none --no-publish `
  --tag read --tag recipe `
  --allow-tools Skill "mcp__plugin_odata1c_gate__odata1c_bases" `
  "mcp__plugin_odata1c_gate__odata1c_find_entity" "mcp__plugin_odata1c_gate__odata1c_describe_entity" `
  "mcp__plugin_odata1c_gate__odata1c_query" "mcp__plugin_odata1c_gate__odata1c_recipe" `
  "mcp__plugin_odata1c_gate__odata1c_info" `
  --json build/evals.json
```

(Git Bash — та же команда одной строкой, без обратных апострофов-переносов PowerShell.)

- `--no-publish` — обязателен: без него отчёт по умолчанию публикуется на claude.ai, а в
  `evidence` LLM-судьи попадает дословный ответ модели, включая подпись базы из `bases.yaml`
  (локальные метаданные владельца, не данные 1С, но им не место вне машины — M3, глобальные
  ограничения).
- `--runs 1 --ablation none` — один прогон на дело вместо трёх (дефолт `case.runs ?? 3`); каждое
  дело сверх этого дополнительно фиксирует `runs: 1` в своём `prompt.md`, так что флаг не строго
  обязателен, но экономит подтверждение живой базы 3× — оставляйте оба.
- Ожидание: `casesPassed == casesTotal` (13 из 13). Результат конкретного прогона и стоимость —
  в отчёте задачи 7 (`docs/superpowers` контроллера), не здесь — этот файл про то, как запустить,
  не про то, что получилось в конкретный день.
- Результаты (`build/plugin-dev/evals/results/…` и файл `--json`) — не в репозитории
  (`plugin/evals/results/` в `.gitignore`, `build/` — тоже).

## Дело 14 (`14-save-recipe`) — отдельно, с подготовкой

Дело пишет файл `recipes/ut/eval_last_orders.yaml` в домашний каталог шлюза (`~/.claude/odata1c/`
или `$ODATA1C_HOME`) и проверяет его `odata1c recipe check ut`. Нужны два условия, которых на
момент написания этих evals ещё нет в ветке:

1. У базы `trade_dev` в `bases.yaml` должно быть поле `config: ut` (навык `odata1c-recipe`
   отказывается работать без него и останавливается, не написав файл).
2. Команда `odata1c recipe check` (задача 3 плана M3, библиотека рецептов) должна быть в ветке —
   сейчас (`odata1c --help`) её нет.

Прогон дела 14 отдельно от 1–13:

```text
claude plugin eval build/plugin-dev --mocks off --runs 1 --ablation none --no-publish `
  --tag save `
  --allow-tools Skill "mcp__plugin_odata1c_gate__odata1c_find_entity" `
  "mcp__plugin_odata1c_gate__odata1c_describe_entity" "mcp__plugin_odata1c_gate__odata1c_query" `
  Write "Bash(odata1c recipe check *)" "Bash(uv run odata1c recipe check *)" `
  --json build/evals-14.json
```

После прогона удалите файл, который дело записало в домашний каталог владельца:

```text
rm "$ODATA1C_HOME/recipes/ut/eval_last_orders.yaml"   # или ~/.claude/odata1c/recipes/ut/…
```

(каталог `recipes/ut/`, если он стал пустым и не использовался ни для чего другого, можно удалить
тоже — но это на усмотрение владельца, не автоматически).

## Полный прогон (все 14, когда предпосылки дела 14 выполнены)

Тот же вызов, что для 1–13, но без `--tag` (или с `--tag read --tag recipe --tag save`) и с
объединённым списком `--allow-tools` (все MCP-тулы выше + `Write` + оба паттерна `Bash`).

## Схема файлов (проверено на установленной версии, расходится с ранними набросками SPEC)

- `prompt.md` — frontmatter `max_turns`, `timeout_seconds`, `runs`, `allowed_tools` (полные имена
  MCP-тулов вида `mcp__plugin_odata1c_gate__odata1c_<тул>`, либо встроенные `Skill`/`Write`/`Bash`),
  `tags`; тело — текст промпта как есть.
- `graders/*.md` — frontmatter `type` + поля по типу, тело используется только для `type: llm`
  (становится `criteria` автоматически — отдельное поле `criteria` во frontmatter не нужно) и
  `type: baseline`.
  - `regex`: `pattern`, `match` (`contains` | `not_contains` | `count:N`), `target`
    (`last_message` по умолчанию | `trace` | `files` | `{source: file, path: ...}`), `flags`.
  - `tool_used`: `tool` (одно точное имя — ИЛИ не поддерживается), `input_match` (regex по
    сериализованным аргументам вызова), `min`, `max`.
  - `llm`: тело = критерии PASS/FAIL; `focus` — НЕ произвольный текст, а тот же выбор цели, что
    `target` у `regex` (по умолчанию `last_message` — этого достаточно почти всегда, поле можно не
    указывать).
  - `file_exists`: `path` (glob), `exists` (default `true`) — не годится для файлов, которые
    прогон пишет вне рабочего каталога плагина (как рецепт дела 14 — он уходит в домашний каталог
    шлюза); там вместо этого используется `tool_used: Write` с `input_match` по пути.
  - `arm: with-only | both` имеет смысл только под `--ablation with-without`; под `--ablation none`
    (как в этой команде) он ни на что не влияет, все graderы обычные и входят в score.
