---
max_turns: 20
timeout_seconds: 600
runs: 1
allowed_tools:
  - Skill
  - "mcp__plugin_odata1c_gate__odata1c_find_entity"
  - "mcp__plugin_odata1c_gate__odata1c_describe_entity"
  - "mcp__plugin_odata1c_gate__odata1c_query"
tags: [read]
---

Возьми любого контрагента из базы 1С и покажи его пять последних документов «Заказ клиента» —
от самой новой даты к более старым.
