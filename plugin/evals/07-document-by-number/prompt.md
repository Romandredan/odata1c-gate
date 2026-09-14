---
max_turns: 20
timeout_seconds: 600
runs: 1
allowed_tools:
  - Skill
  - "mcp__plugin_odata1c_gate__odata1c_find_entity"
  - "mcp__plugin_odata1c_gate__odata1c_query"
tags: [read]
---

Возьми номер любого документа «Заказ клиента» из последних десяти по дате и найди этот же
документ ещё раз — уже отбором по номеру.
