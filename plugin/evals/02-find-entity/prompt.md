---
max_turns: 20
timeout_seconds: 600
runs: 1
allowed_tools:
  - Skill
  - "mcp__plugin_odata1c_gate__odata1c_find_entity"
tags: [read]
---

Найди в базе 1С сущность справочника контрагентов. Точное имя сущности в OData тебе неизвестно —
ищи по части названия.
