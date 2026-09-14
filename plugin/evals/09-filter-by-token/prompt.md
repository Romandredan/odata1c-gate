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

Найди в базе любого контрагента, возьми токен его ИНН из ответа и найди этого же контрагента ещё
раз — уже отбором по этому токену.
