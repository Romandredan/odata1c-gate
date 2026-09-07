# P2 — `_meta` тула через SDK `mcp`

Дата: 2026-09-08 · план: [M0](../plans/2026-09-07-m0-probes.md), задача 2 · статус: **закрыта
программно**, ручная проверка в Claude Code не выполнена

## Вопрос

SPEC §15, пункт 1: «SDK `mcp`: выставление `_meta` тула через FastMCP; иначе низкоуровневый
`Server`». От ответа зависит, на чём пишется слой `tools`: тулу `odata1c_commit` нужен
`_meta["anthropic/requiresUserInteraction"]=true` (SPEC §5), иначе подтверждение записи перестаёт
быть подтверждением — инвариант 2 в `AGENTS.md`.

## Как проверял

`tools/probes/p2_meta_probe.py`: объявление тула с `meta=`, чтение `tools/list` у самого сервера,
затем сквозная проверка настоящим транспортом — сервер поднимается на Streamable HTTP
(`127.0.0.1:7179`), клиент SDK подключается, вызывает `initialize` и `tools/list`.

Окружение: Windows 10.0.26200, CPython 3.12.12, `mcp` 2.2.0, `uvicorn` 0.52.4.

Команда: `python tools/probes/p2_meta_probe.py`

## Что получилось

**Класс FastMCP в SDK 2.x переименован.** Импорт `mcp.server.fastmcp` выбрасывает
`ModuleNotFoundError` с текстом: «This is mcp 2.x, where FastMCP was renamed to MCPServer
(from mcp.server.mcpserver import MCPServer) and other APIs changed; see the migration guide at
<https://py.sdk.modelcontextprotocol.io/v2/migration/> or pin 'mcp<2' to keep running v1 code».

Фактическая сигнатура объявления тула:

```python
MCPServer.tool(self, name=None, title=None, description=None, annotations=None,
               icons=None, meta: dict[str, Any] | None = None, structured_output=None)
```

Поля `types.Tool`: `annotations`, `description`, `execution`, `icons`, `input_schema`, `meta`,
`name`, `output_schema`, `title`. Псевдоним поля `meta` — `_meta`, то есть в протокол оно уходит
под нужным именем.

Клиент по Streamable HTTP получил в `tools/list`:

```json
{
  "name": "odata1c_commit",
  "description": "Выполнить подготовленную операцию записи.",
  "inputSchema": { "...": "..." },
  "outputSchema": { "...": "..." },
  "_meta": {
    "anthropic/requiresUserInteraction": true,
    "anthropic/maxResultSizeChars": 120000
  }
}
```

Итоговая строка пробы: `requiresUserInteraction у клиента = True`.

Попутно выяснены два расхождения с API 1.x, на которых проба падала:

| Было в 1.x | Стало в 2.x |
|---|---|
| `from mcp.server.fastmcp import FastMCP` | `from mcp.server.mcpserver import MCPServer` |
| `streamablehttp_client(url)` → три значения | `streamable_http_client(url)` → два значения (`read`, `write`) |

## Ответ

**Да, `_meta` выставляется штатным публичным API** — аргументом `meta=` декоратора
`MCPServer.tool` — и доходит до клиента сквозь Streamable HTTP без обращения к приватным полям.
Переходить на низкоуровневый `Server` не нужно. Условие «доходит только правкой приватного
`_tool_manager`», при котором ответ считался бы отрицательным, не наступило.

## Следствия

1. **Поправка к [ADR-0008](../adr/0008-python-fastmcp-stack.md) и правки SPEC §2.2, §11.1:**
   вместо FastMCP используется `MCPServer` из `mcp.server.mcpserver`, нижняя граница `mcp>=2.2`.
   Выбор SDK не меняется — меняется имя класса и путь импорта.
2. **План M1d пишется на `MCPServer`.** Способ объявления тула с `_meta` зафиксирован дословно
   выше и переносится в план без изменений.
3. **Клиентские вызовы в лаунчере** используют `streamable_http_client` с распаковкой в два потока.
   Учесть при написании пробы P3 и задачи «лаунчер» плана M1d.
4. **Осталась ручная проверка:** появляется ли в Claude Code диалог разрешения при вызове тула с
   этим `_meta`. Программно подтверждено только то, что клиент видит поле. Требует подключения
   пробного сервера командой `claude mcp add` и вызова тула в живой сессии; версию Claude Code
   записать в отчёт — SPEC §15 отмечает, что поведение исправлено в 2.1.246.
