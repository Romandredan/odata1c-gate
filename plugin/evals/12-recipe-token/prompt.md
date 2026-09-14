---
max_turns: 20
timeout_seconds: 600
runs: 1
allowed_tools:
  - Skill
  - "mcp__plugin_odata1c_gate__odata1c_recipe"
tags: [recipe]
---

Выполни рецепт оборотов продаж за прошлый месяц по всем контрагентам. Затем повтори его же с
отбором по одному контрагенту — ключ партнёра возьми прямо из строки первого ответа.
