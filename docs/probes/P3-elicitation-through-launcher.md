# P3 — elicitation сквозь лаунчер

Дата: 2026-09-08 · план: [M0](../plans/2026-09-07-m0-probes.md), задача 3 · статус: **закрыта
программно**

## Вопрос

SPEC §15, пункт 2 (и §2.1, §7.2). Доходит ли запрос сервера к пользователю
(`elicitation/create`) через тонкий stdio-лаунчер до конечного клиента, и возвращается ли ответ
обратно? Цепочка: stdio-клиент → лаунчер (stdio снаружи, Streamable HTTP внутрь) → демон, и ответ
назад. От ответа зависит, работает ли подтверждение записи через elicitation (инвариант 2 в
`AGENTS.md`, `write_confirm_fallback` в SPEC §3.3) или схема «тонкий лаунчер + один демон»
(ADR-0013) требует пересмотра.

Отдельный вопрос: по какому каналу Streamable HTTP приходит запрос демона — по потоку ответа на
вызов тула или по отдельному GET-потоку. От этого зависит, обязан ли лаунчер держать отдельный
GET-поток к демону открытым.

## Как проверял

Три процесса, схема SPEC §2.1:

- `tools/probes/p3_elicit_server.py` — демон-заглушка на Streamable HTTP (`127.0.0.1:7179`),
  один тул `odata1c_probe_commit`, внутри — `ctx.elicit(...)` с pydantic-схемой `Confirm`;
- `tools/probes/p3_launcher.py` — лаунчер: stdio-сервер (низкоуровневый `Server` из
  `mcp.server.lowlevel`) снаружи, `ClientSession` по Streamable HTTP внутрь; вызов тула
  пересылается напрямую апстриму, `elicitation_callback` апстрим-сессии пересылает запрос демона
  downstream-сессии через `ServerSession.elicit_form(...)`;
- `tools/probes/p3_client.py` — stdio-клиент, отвечающий на elicitation `accept` без участия
  человека (роль пользователя).

Окружение: Windows 10.0.26200, CPython 3.12.12, `mcp` 2.2.0 (то же, что в P1/P2).

Запуск: сервер — `python tools/probes/p3_elicit_server.py` в одном процессе, клиент —
`python tools/probes/p3_client.py` во втором (сам поднимает лаунчер как дочерний stdio-процесс).

Для проверки канала отдельно собрана диагностическая копия лаунчера
(`p3_launcher_no_get.py`, во временный каталог пробы, в репозиторий не входит), в которой
`mcp.client.streamable_http.StreamableHTTPTransport.handle_get_stream` подменён на no-op —
GET-поток к демону физически не открывается. Цепочка прогнана повторно с этим лаунчером.

## Что получилось

**Код брифа писался под mcp 1.x, у 2.2.0 другой API — на этом расхождения не заканчиваются
одними именами импортов** (кроме уже известных по P2 `FastMCP → MCPServer`,
`streamablehttp_client → streamable_http_client`):

| Было в брифе (1.x) | Стало в 2.x (проверено интроспекцией) |
|---|---|
| `from mcp.server.lowlevel import Server`, затем `@proxy.list_tools()` / `@proxy.call_tool()` — декораторы | `Server(name, on_list_tools=..., on_call_tool=...)` — обработчики передаются в конструктор, декораторов `list_tools()`/`call_tool()` у `Server` в 2.x нет |
| `proxy.request_context.session.send_request(...)` — доступ к сессии текущего запроса через context-var на объекте `Server` | `ctx.session` — `ServerRequestContext` (первый аргумент каждого `on_*`-обработчика) несёт готовую `ServerSession`; атрибута `request_context` у `Server` в 2.x нет |
| Ручная сборка `types.ServerRequest(types.ElicitRequest(...))` + `send_request(..., types.ElicitResult)` | `ServerSession.elicit_form(message, requested_schema, related_request_id=...)` — готовый метод, оборачивающий то же самое |

Обе стороны elicitation воспроизведены дословно по факту API:
- сервер: `ctx: Context` (из `mcp.server.mcpserver`) и `await ctx.elicit(message=..., schema=Confirm)`
  — тот же способ, что подтверждён в P2;
- клиент верхнего уровня (`ClientSession(..., elicitation_callback=on_elicit)`) — без изменений
  относительно брифа, `on_elicit(context, params) -> types.ElicitResult`.

Прогон `p3_client.py` (после апстрима и лаунчера) дал ожидаемые три строки клиента подряд —
без единого расхождения с ожиданием из брифа:

```
[клиент] тулы: ['odata1c_probe_commit']
[лаунчер] получен elicitation/create от демона: 'Выполнить операцию p-1?'
[клиент] пришёл запрос подтверждения: Выполнить операцию p-1?
[лаунчер] ответ клиента переслан демону: action=accept
[клиент] результат тула: [TextContent(type='text', text='action=accept data=approve=True', annotations=None, meta=None)]
```

Полный цикл подтверждён: запрос сервера дошёл через лаунчер до stdio-клиента, ответ клиента
дошёл обратно до демона, тул получил и вернул этот ответ.

### Канал доставки

Источник (`mcp/server/session.py`, `ServerSession.send_request`):

```python
channel = self._request_outbound if related is not None else self._connection.outbound
```

`MCPServer`-овский `Context.elicit()` всегда передаёт `related_request_id=self.request_id`
(`mcp/server/mcpserver/context.py`), то есть elicitation внутри вызова тула по коду SDK уходит
через канал, привязанный к текущему запросу (`_request_outbound`), а не через
`_connection.outbound` — общий канал, который обслуживает отдельный GET-поток
(`StreamableHTTPTransport.handle_get_stream` на стороне клиента).

Эмпирическая проверка подтвердила это: клиентский `streamable_http_client` в норме сам открывает
GET-поток сразу после `initialize()` (см. `post_writer`: `start_get_stream()` вызывается при
отправке `notifications/initialized`), поэтому в обычном прогоне оба канала работают одновременно
и по одному успешному прогону нельзя было отличить, какой из них донёс elicitation. Чтобы
исключить GET-поток физически, метод `handle_get_stream` был подменён на no-op в лаунчере
(`p3_launcher_no_get.py`) — при повторном прогоне лаунчер напечатал
«GET-поток отключён монкипатчем — не открываем», и цепочка тем не менее отработала полностью:
elicitation дошла до клиента, ответ вернулся демону, тул завершился успешно.

**Вывод: запрос сервера приходит по потоку ответа на вызов тула (POST `tools/call`), отдельный
GET-поток для этого не требуется.**

## Ответ

Да, запрос сервера (`elicitation/create`) доходит через тонкий stdio-лаунчер до конечного
stdio-клиента, и ответ клиента возвращается обратно до демона — цепочка stdio-клиент → лаунчер →
Streamable HTTP → демон → обратно работает целиком на публичном API SDK `mcp` 2.2.0, без обращения
к приватным полям. Схема лаунчера SPEC §2.1 подтверждена: лаунчеру достаточно пересылать
`elicitation_callback` апстрим-сессии в `elicit_form` downstream-сессии — собственной логики
подтверждения в нём нет.

Канал — поток ответа на вызов тула, не отдельный GET-поток. Практическое следствие: лаунчеру
**не обязательно** держать отдельный долгоживущий GET-поток к демону ради elicitation, но в 2.x
клиентская сессия открывает его сама после `initialize()` для других серверных сообщений,
не привязанных к запросу (уведомления об изменении списков и т. п.) — отключать его не нужно
и незачем, экономии на нём нет.

## Следствия

1. **Схема лаунчера SPEC §2.1 не меняется**, код пробы `tools/probes/p3_launcher.py` — рабочий
   образец для задачи «лаунчер» плана M1d: конструктор `Server(on_list_tools=..., on_call_tool=...)`
   вместо декораторов `@proxy.list_tools()`/`@proxy.call_tool()` из брифа, `ctx.session` вместо
   `proxy.request_context.session`, `ServerSession.elicit_form(...)` вместо ручной сборки
   `ServerRequest(ElicitRequest(...))`.
2. **Поправка к плану M1d и к SPEC §11.1** (там, где описывается устройство лаунчера): пример кода
   лаунчера в задаче должен ссылаться на актуальный API `Server` из `mcp.server.lowlevel` —
   конструктор с `on_*`-обработчиками, а не декораторы 1.x.
3. **ADR-0013 пересмотра не требует** — базовая посылка «тонкий лаунчер + один демон» с
   пробросом elicitation подтверждена работающей реализацией на фактическом SDK.
4. **Для нескольких одновременных pending-операций в одном лаунчере** (несколько сессий агентов к
   одному демону, SPEC §2.1) понадобится не module-level переменная под одну downstream-сессию
   (как в пробе — для одного stdio-клиента на процесс лаунчера этого достаточно), а сопоставление
   апстрим-запроса конкретному downstream-запросу — это относится к задаче «лаунчер» плана M1d,
   в проверяемый минимум P3 не входило.
5. **Осталась ручная проверка**, как и по итогам P2: ведёт ли реальный диалог разрешения Claude
   Code (elicitation UI) себя так же, как приведённый здесь клиент-заглушка — требует подключения
   пробного демона через `claude mcp add` с лаунчером в реальной сессии.
