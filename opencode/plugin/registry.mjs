/**
 * Разбор поставляемых плагином навыков и агента в записи V2 OpenCode.
 *
 * Файлы — те же, что читает Claude Code (`plugin/skills/<имя>/SKILL.md`,
 * `plugin/agents/<имя>.md`); второй копии под OpenCode нет намеренно: копии
 * расходятся при первой правке одной из них, и расходятся молча.
 *
 * Пакет, установленный OpenCode из репозитория, лежит в кэше, а каталоги
 * `.opencode/skills/…` и `.opencode/agents/…` сканируются только в проекте и
 * в конфигурационном каталоге пользователя — до кэша сканер не достаёт,
 * поэтому регистрация идёт программно (`ctx.skill.transform`,
 * `ctx.agent.transform`).
 *
 * Разбор frontmatter намеренно узкий: ключ верхнего уровня, свёрнутый блок
 * `>-`, строка в кавычках и вложенная карта в один уровень. Всё, что в эти
 * формы не укладывается, возвращает `null`, и запись не регистрируется вовсе.
 * Тест `tests/opencode-odata1c.test.mjs` проверяет, что все поставляемые файлы
 * разбираются, — новая конструкция во frontmatter покраснеет там, а не молча
 * выключит навык у пользователя.
 */

import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { basename, join } from 'node:path';

/** `true` / `false` — булево, всё остальное — строка как есть. */
function scalar(raw) {
  const v = raw.trim();
  if (v === 'true') return true;
  if (v === 'false') return false;
  return v;
}

/**
 * После scalar: у строк в двойных кавычках снимаются кавычки и раскрываются
 * YAML-эскейпы `\\` и `\"` (плюс `\n` и `\t` — в файле агента они не встречаются,
 * но зеркало семантику YAML для «новая конструкция покраснеет в тестах»).
 * Оставшееся `…\n…` в тексте остаётся как есть, ровно как в Claude Code,
 * который разбирает frontmatter тем же YAML.
 */
function unescapeQuoted(raw) {
  const value = raw.replace(/\\(\\|\"|n|t)/g, (_, эскейп) =>
    эскейп === '\\' ? '\\' : эскейп === 'n' ? '\n' : эскейп === 't' ? '\t' : '"'
  );
  return value;
}

/**
 * Строка значения: в кавычках — с распаковкой, без — scalar().
 */
function значение(rest) {
  const v = rest.trim();
  if (v.length >= 2 && v.startsWith('"') && v.endsWith('"')) {
    return {
      кавычками: true,
      text: unescapeQuoted(v.slice(1, -1)),
    };
  }
  return { кавычками: false, text: scalar(v) };
}

/**
 * Frontmatter между парой строк `---` в начале файла.
 *
 * Возвращает `{ data, body }` либо `null`, если блока нет или встретилась
 * конструкция за пределами признаваемой формы.
 */
export function parseFrontmatter(text) {
  const lines = String(text).split(/\r?\n/);
  if (lines[0] !== '---') return null;
  const end = lines.indexOf('---', 1);
  if (end < 0) return null;

  const data = {};
  let i = 1;
  while (i < end) {
    const line = lines[i];
    if (!line.trim() || line.trimStart().startsWith('#')) {
      i += 1;
      continue;
    }
    const m = /^([A-Za-z_][\w-]*):(.*)$/.exec(line);
    // Строка вне формы «ключ: значение» на верхнем уровне: разбор не признаётся.
    if (!m) return null;

    const key = m[1];
    const rest = m[2].trim();

    if (rest === '>-' || rest === '>' || rest === '|' || rest === '|-') {
      // Свёрнутый или буквальный блок: все последующие строки с отступом.
      const block = [];
      i += 1;
      while (i < end && (lines[i].startsWith('  ') || !lines[i].trim())) {
        block.push(lines[i].replace(/^ {2}/, ''));
        i += 1;
      }
      data[key] = block.join('\n').trim();
      continue;
    }

    if (rest === '') {
      // Вложенная карта в один уровень: `ключ:` и строки с отступом под ним.
      const nested = {};
      i += 1;
      while (i < end && lines[i].startsWith('  ')) {
        const sub = /^ {2}([A-Za-z_][\w-]*):(.*)$/.exec(lines[i]);
        if (!sub) return null;
        nested[sub[1]] = scalar(sub[2]);
        i += 1;
      }
      data[key] = nested;
      continue;
    }

    const разобранное = значение(rest);
    data[key] = разобранное.text;
    i += 1;
  }

  return { data, body: lines.slice(end + 1).join('\n').trim() };
}

/** Читает `*.md` каталога и отдаёт пары `[имя, разбор]`; неразобранные — пропускает. */
function entries(dir) {
  if (!dir || !existsSync(dir)) return [];
  const out = [];
  for (const file of readdirSync(dir)) {
    if (!file.endsWith('.md')) continue;
    let parsed = null;
    try {
      parsed = parseFrontmatter(readFileSync(join(dir, file), 'utf8'));
    } catch {
      parsed = null;
    }
    if (parsed) out.push([basename(file, '.md'), parsed]);
  }
  return out;
}

/**
 * Навыки пакета в форме `Skill.Info` V2 (`ctx.skill.transform` → `editor.add`).
 *
 * `path` обязан быть абсолютным: навык лежит в кэше пакета, и справочники из
 * `references/` разрешаются относительно SKILL.md — относительный путь оборвал бы
 * их загрузку. `content` — тело файла целиком, включая frontmatter: так делает
 * сам загрузчик OpenCode при файловом обнаружении.
 */
export function skillsFrom(dir) {
  if (!dir || !existsSync(dir)) return [];
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const file = join(dir, entry.name, 'SKILL.md');
    if (!existsSync(file)) continue;
    let content;
    try {
      content = readFileSync(file, 'utf8');
    } catch {
      continue;
    }
    // Без description навык не рекламируется модели — а весь смысл поставки в том,
    // чтобы модель находила навык сама. Неразобранный frontmatter пропускает навык
    // целиком: зарегистрировать его без описания хуже, чем не зарегистрировать —
    // тест состава покраснеет, а не сработает тихая деградация.
    const parsed = parseFrontmatter(content);
    if (!parsed || typeof parsed.data.description !== 'string' || !parsed.data.description) continue;
    out.push({
      id: entry.name,
      name: typeof parsed.data.name === 'string' ? parsed.data.name : entry.name,
      description: parsed.data.description,
      path: file,
      content,
    });
  }
  return out;
}

// --- Субагент V2 ---------------------------------------------------------------------------

/**
 * Канонический состав MCP-тулов шлюза для permissions агента-следователя.
 * Читательские — его единственный доступ к базам; всё остальное у него отобрано:
 * тулы записи, `reindex` (индекс — работа владельца), `journal` (журнал коммитов)
 * и даже `reindex`-подобные.
 */
const MCP_READ_TOOLS = ['bases', 'find_entity', 'describe_entity', 'query', 'get', 'info', 'recipe', 'raw_get'];
const MCP_WRITE_TOOLS = [
  'create',
  'update',
  'mark_for_deletion',
  'action',
  'undo',
  'commit',
  'journal',
  'delete_record',
  'reindex',
];

/**
 * Имена серверов, под которыми шлюз может быть подключён: имя плагинной
 * регистрации (`odata1c`) и запасные (ручная регистрация, как в matcher'е
 * Python-хука). Действие permission в V2 — `<сервер>_<тул>`.
 */
const MCP_SERVERS = ['odata1c', 'gate', 'plugin_odata1c_gate'];

/**
 * Permissions агента-следователя (V2-массив правил, last-match-wins).
 *
 * Разрешающая база `*` идёт ПЕРВОЙ — неупомянутое (читательские тулы MCP,
 * чтение файлов, поиск) остаётся разрешённым, как это фиксировало `tools:`
 * конца 1 закрытым списком у Claude Code (строка `tools:` frontmatter —
 * Claude-специфичная и в V2 не переносится: ограничение решают permissions).
 */
function permissionsСледователя() {
  const rules = [
    { action: '*', resource: '*', effect: 'allow' },
    // Правка (у V2 одно действие на write/edit/patch) и оболочка закрыты: файлы
    // шлюза недоступны, право записи у следователя нет.
    { action: 'edit', resource: '*', effect: 'deny' },
    { action: 'shell', resource: '*', effect: 'deny' },
    { action: 'subagent', resource: '*', effect: 'deny' },
    { action: 'webfetch', resource: '*', effect: 'deny' },
    { action: 'websearch', resource: '*', effect: 'deny' },
  ];
  for (const тул of MCP_WRITE_TOOLS) {
    for (const сервер of MCP_SERVERS) {
      rules.push({ action: `${сервер}_odata1c_${тул}`, resource: '*', effect: 'deny' });
    }
  }
  return rules;
}

/**
 * Субагенты пакета в форме для `ctx.agent.transform` V2: тело файла становится
 * `system`, разрешения — упорядоченный массив `permissions`. Из frontmatter
 * переносятся только `name` и `description`; строка `tools:` и поле `model: haiku`
 * Claude-специфичные (модель для OpenCode задаётся владельцем в конфиге ключом
 * `agents`) — решением задачи 3 остаются без переноса, намеренно.
 */
export function agentsV2From(dir) {
  return entries(dir)
    .filter(([, p]) => p.body)
    .map(([имяФайла, p]) => {
      const имя = typeof p.data.name === 'string' ? p.data.name : имяФайла;
      const def = {
        mode: 'subagent',
        system: p.body,
        permissions: permissionsСледователя(),
      };
      if (typeof p.data.description === 'string' && p.data.description) def.description = p.data.description;
      return [имя, def];
    });
}
