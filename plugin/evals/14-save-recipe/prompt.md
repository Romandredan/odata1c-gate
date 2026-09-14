---
max_turns: 20
timeout_seconds: 600
runs: 1
allowed_tools:
  - Skill
  - "mcp__plugin_odata1c_gate__odata1c_find_entity"
  - "mcp__plugin_odata1c_gate__odata1c_describe_entity"
  - "mcp__plugin_odata1c_gate__odata1c_query"
  - Write
  - Bash
tags: [save]
---

Выполни запрос последних пяти заказов клиента любого контрагента и сохрани этот запрос как
именованный рецепт `eval_last_orders`.
