/**
 * Плагин odata1c для OpenCode 2.x — точка входа по ишью Romandredan/odata1c-gate#2.
 *
 * V2-only (решение в ишью, 2026-11-03): OpenCode 1.x снят во всех окружениях,
 * двойной интерфейс V1+V2 не делается. Экспорт — голый объект `{ id, setup }`,
 * как и у 1c-quality-gate: пакет без зависимостей, `Plugin.define` из
 * `@opencode/plugin` в рантайме не нужен (это функция-тождество для типизации).
 *
 * Что делает setup:
 *   1. Регистрирует четыре навыка (`odata1c`, `odata1c-policy`, `odata1c-recipe`,
 *      `odata1c-setup`) и агента `odata1c-investigator` из файлов плагина
 *      (opencode/plugin/registry.mjs) — до кэша пакета сканер OpenCode не
 *      достаёт, поэтому программно.
 *   2. Регистрирует MCP-сервер гейта с закреплённой версией пакета — команду
 *      берёт из `plugin/.mcp.json` (её обновляет tools/bump_version.py, у
 *      OpenCode-стороны отдельной копии версии нет).
 *   3. Правило защиты 2–3 (файлы каталога шлюза, команды оболочки) — хуком
 *      `ctx.tool.hook("execute.before")` (opencode/plugin/guard-core.mjs).
 *   4. Правило защиты 1 — нативный permission-хук `ctx.permission.hook("evaluate")`
 *      (механизм замены, а не порт: в event нет pending_id).
 *
 * Домашний каталог шлюза и дефолтны homes — как в Python-хуке: `ODATA1C_HOME`
 * этой сессии и документированный дефолт `~/.claude/odata1c` закрываются оба.
 */

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { домашнийКаталог, правило1, решение } from './guard-core.mjs';
import { agentsV2From, skillsFrom } from './registry.mjs';

const кореньПакета = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

const MCP_SERVER_NAME = 'odata1c';

function mcpКонфиг() {
  // Команда и закреплённая версия — единственный источник plugin/.mcp.json:
  // bump_version.py правит её в числе четырёх мест поставки, OpenCode-сторона
  // не добавляет пятого.
  const mcp = JSON.parse(readFileSync(resolve(кореньПакета, 'plugin', '.mcp.json'), 'utf8'));
  const gate = mcp?.gate;
  if (!gate || gate.type !== 'stdio' || typeof gate.command !== 'string') return null;
  const args = Array.isArray(gate.args) ? gate.args.map(String) : [];
  return [gate.command, ...args];
}

async function setup(ctx) {
  const disposers = [];
  const register = async (источник) => {
    try {
      const registration = await источник();
      if (registration && typeof registration.dispose === 'function') disposers.push(registration);
    } catch {
      /* одна упавшая регистрация не отменяет остальные и не ломает загрузку */
    }
  };

  // ── Навыки ──
  await register(async () => {
    if (typeof ctx.skill?.transform !== 'function') return undefined;
    const список = skillsFrom(resolve(кореньПакета, 'plugin', 'skills'));
    return ctx.skill.transform((editor) => {
      for (const навык of список) {
        if (editor.get?.(навык.id) !== undefined) continue; // своё у пользователя сильнее
        editor.add(навык);
      }
    });
  });

  // ── Агент-следователь ──
  // У редактора агентов V2 нет add: запись создаётся через update(name, fn).
  await register(async () => {
    if (typeof ctx.agent?.transform !== 'function') return undefined;
    const defs = agentsV2From(resolve(кореньПакета, 'plugin', 'agents'));
    if (!defs.length) return undefined;
    return ctx.agent.transform((editor) => {
      for (const [name, def] of defs) {
        if (editor.get?.(name) !== undefined) continue;
        editor.update(name, (agent) => {
          agent.id = name;
          agent.name = name;
          agent.mode = def.mode;
          agent.hidden = false;
          if (def.description !== undefined) agent.description = def.description;
          agent.system = def.system;
          agent.permissions = def.permissions.map((правило) => ({ ...правило }));
        });
      }
    });
  });

  // ── MCP-сервер гейта ──
  await register(async () => {
    if (typeof ctx.mcp?.transform !== 'function') return undefined;
    const команда = mcpКонфиг();
    if (!команда) return undefined;
    return ctx.mcp.transform((editor) => {
      if (editor.get?.(MCP_SERVER_NAME) !== undefined) return; // своё подключение сильнее
      editor.set(MCP_SERVER_NAME, {
        type: 'local',
        command: команда,
        enabled: true,
      });
    });
  });

  // ── Правила 2–3: хук тулов ──
  await register(async () => {
    if (typeof ctx.tool?.hook !== 'function') return undefined;
    return ctx.tool.hook('execute.before', (event) => {
      // решение() свои сбои глушит до null (докстрока guard-core), поэтому
      // выброс отсюда — только осознанный запрет с фиксированной причиной.
      const запрещение = решение(event, домашнийКаталог(), ctx.location?.directory);
      if (запрещение) throw запрещение;
    });
  });

  // ── Правило 1: permission-аска на odata1c_commit ──
  await register(async () => {
    if (typeof ctx.permission?.hook !== 'function') return undefined;
    return ctx.permission.hook('evaluate', (event) => {
      try {
        правило1(event);
      } catch {
        /* сбой правил не разрешает и не запрещает запись — эффект остаётся */
      }
    });
  });

  return async () => {
    for (const registration of disposers.reverse()) {
      try {
        await registration.dispose();
      } catch {
        /* демократично глушим: выгрузка не должна падать */
      }
    }
  };
}

export default { id: 'odata1c', setup };
