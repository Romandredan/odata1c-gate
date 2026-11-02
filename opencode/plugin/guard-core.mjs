/**
 * Ядро защиты файлов каталога шлюза odata1c для плагина OpenCode (V2).
 *
 * Порт правил 2–3 хука `plugin/hooks/pretooluse.py` (Claude Code) на JavaScript.
 * Правило 1 (`odata1c_commit` → ask) переехало в нативный permission-хук плагина
 * (`ctx.permission.hook("evaluate")`) — это не порт, а замена механизма, поэтому
 * здесь его нет. Правило 2 (файлы домашнего каталога) и правило 3 (команды
 * оболочки) — тот же алгоритм, тот же порядок проверок и те же фиксированные
 * русские причины; имена тулов и поля ввода переведены на V2-имена OpenCode.
 *
 * Источник истины сценариев — `tests/unit/test_plugin_hook.py`; каждая его группа
 * перенесена в `tests/opencode-odata1c.test.mjs`. Ссылки на Ruling — те же, что в
 * докстроке Python-хука; здесь не дублируются подробности, только различия.
 *
 * Единая точка входа — `решение(event, home, cwdRoot)`: по событию
 * execute.before V2 (`tool`, `input`) возвращает `Error` с маркером `guard`
 * (запрет — плагин бросает его, тул не выполняется, сообщение ошибки = причина)
 * или `null` (решения нет — тул выполняется как обычно). Свои сбои на
 * неожиданном входе гасятся до `null`: защита не должна ломать тул сильнее,
 * чем осознанный deny.
 */

import { existsSync, realpathSync } from 'node:fs';
import { homedir } from 'node:os';
import * as path from 'node:path';

// Причины решений — фиксированный русский текст, как в Python-хуке. Никаких значений
// из события (пути, команды) в текст не подставляется. Причина записи (правило 1,
// permission-хук) держится здесь же, чтобы все причины были в одном месте.
export const ПРИЧИНА_ФАЙЛ =
  'файл или каталог шлюза odata1c: пароли или реальные значения; читать его модели нельзя, спросите владельца';
export const ПРИЧИНА_ШАБЛОН = 'шаблон в каталоге шлюза может задеть файлы с паролями';
export const ПРИЧИНА_МАССОВОЕ_ЧТЕНИЕ = 'чтение каталога шлюза целиком может задеть файлы с паролями';
export const ПРИЧИНА_ЗАПИСЬ =
  'запись в 1С через шлюз odata1c: подтвердите, что превью операции показано пользователю и он согласился';
export const ДЕФОЛТНЫЙ_ДОМ = '~/.claude/odata1c';

/** Запрет переносится через throw — OpenCode прерывает вызов tuла сообщением ошибки. */
export function denyError(причина) {
  const ошибка = new Error(причина);
  ошибка.odata1cGuard = true; // маркер «запрет защитного хука», отличает от случайного сбоя
  return ошибка;
}

export function guardDenial(ошибка) {
  return Boolean(ошибка && ошибка.odata1cGuard);
}

// --- Правило 1: permission-аска на odata1c_commit ------------------------------------------

/**
 * В V2 действие MCP-тула в permission-системе — `<сервер>_<тул>`, неподдержанные
 * символы становятся `_`. Запасные формы — имена с префиксом `mcp` (на случай
 * другого написания) и серверного имени `gate`. `mcp__other__testdata1c_commit`
 * чужого сервера не матчится.
 */
export const COMMIT_ACTION_RE = /^(?:mcp_+)?(?:plugin_odata1c_gate|odata1c|gate)_*odata1c_commit$/i;

/**
 * Правило 1 в V2-форме: мягкий перевод эффекта решения в `ask` с фиксированным
 * сообщением. Явный configured `deny` финален и хук не вызывает — не нарушаем.
 * `pending_id` в событии нет (resources MCP-тула — `*`), поэтому без него, в
 * отличие от формулировки Python-хука; зафиксировано в docs/OPENCODE.md.
 */
export function правило1(event) {
  if (!COMMIT_ACTION_RE.test(String(event?.action || ''))) return;
  // Явный configured deny финален: хук не ослабляет его до ask.
  if (event.effect === 'deny') return;
  event.effect = 'ask';
  event.message = ПРИЧИНА_ЗАПИСЬ;
}

// --- Правило 2/3: имена тулов и поля ввода V2 ----------------------------------------------

/**
 * Путь лежит в одном из полей списка — V2-имена тулов и кандидаты поля.
 * Кандидаты покрывают оба написания (`filePath` у ядра OpenCode, `file_path` у
 * перехватчиков): проверяется первое непустое строковое значение.
 */
export const ПУТЬ_ПОЛЯ = {
  read: ['filePath', 'file_path', 'path'],
  edit: ['filePath', 'file_path', 'path'],
  write: ['filePath', 'file_path', 'path'],
  multiedit: ['filePath', 'file_path', 'path'],
  notebookedit: ['notebookPath', 'notebook_path', 'path'],
  grep: ['path', 'filePath', 'file_path'],
  glob: ['path', 'filePath', 'file_path'],
};

// Ruling 72: для этих двух запрещён ещё и путь на весь дом или его предок.
export const КАТАЛОЖНЫЕ_ТУЛЫ = new Set(['grep', 'glob']);

export const КОМАНДНЫЕ_ТУЛЫ = new Set(['shell']);

// --- Базовые защищённые имена (эталон — Python `_БАЗОВЫЕ_ИМЕНА`) ---------------------------

const БАЗОВЫЕ_ИМЕНА = 'bases\\.yaml|daemon\\.yaml|launcher\\.key|gate\\.sqlite|journal\\.sqlite';

/** Базовое имя файла как префикс (Ruling 85): «gate.sqlite» матчит и «-wal», и «.bak». */
export const ИМЯ_ПРЕФИКС_RE = new RegExp(`^(?:${БАЗОВЫЕ_ИМЕНА})(?![a-z0-9])`, 'i');

// Правило 3: имя файла как отдельное слово в команде (тот же префикс Ruling 85).
export const BASH_ФАЙЛЫ_RE = new RegExp(`(?<![\\w.-])(?:${БАЗОВЫЕ_ИМЕНА})(?![a-z0-9])`, 'i');

// Правило 3: reveal рядом с именем скрипта (пробелы, кавычки, .exe, регистр).
export const BASH_REVEAL_RE = /\bodata1c(?:\.exe)?\b[\s'"]*reveal\b/i;

// Правило 3: sqlite3 вместе с gate/journal.sqlite в любом месте, включая многострочные.
export const BASH_SQLITE_RE = /\bsqlite3\b.*(?:gate|journal)\.sqlite/is;

// Ruling 87: раскрытие фигурных скобок (по запятой внутри) и квадратный набор символов.
export const ФИГУРНЫЕ_СКОБКИ_RE = /\{[^{}]*,[^{}]*\}/;
export const КВАДРАТНЫЕ_СКОБКИ_RE = /\[[^\[\]]+\]/;

// Ruling 88: глаголы чтения/перечисления/копирования/архивации из фиксированного списка.
export const ГЛАГОЛЫ_ЧТЕНИЯ_RE =
  /\b(?:cat|type|more|less|head|tail|Get-Content|gc|Get-ChildItem|gci|ls|dir|find|grep|Select-String|rg|sed|awk|cp|copy|xcopy|robocopy|Copy-Item|tar|zip|7z|Compress-Archive|python|node)\b/i;

// Ruling 88: подкаталоги, для которых упоминание дома НЕ считается «корнем».
const ДОПУЩЕННЫЙ_ПОДКАТАЛОГ_RE = /^[/\\](?:recipes|bases|logs)\b/i;

// Git Bash/MSYS: /c/Users/... → C:/Users/... — только на Windows (Python `_POSIX_ДИСК_RE`).
const POSIX_ДИСК_RE = /^\/([A-Za-z])(\/.*)?$/;

// Метасимволы глоба Python, где режем адрес каталога шаблона (Ruling 86).
const ШАБЛОН_ГРАНИЦА_RE = /[*?\[]/;

function escapeRe(значение) {
  return String(значение).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// --- Нормализация пути ---------------------------------------------------------------------

function снятьUncПрефикс(значение) {
  // `\\?\…`, `\\.\…` — расширенные формы Windows; `\\?\UNC\…` разворачивается в `\\`.
  const голова = значение.slice(0, 4);
  if (голова !== '\\\\?\\' && голова !== '\\\\.\\') return значение;
  const остаток = значение.slice(4);
  if (остаток.slice(0, 4).toUpperCase() === 'UNC\\') return '\\\\' + остаток.slice(4);
  return остаток;
}

function переписатьPosixДиск(значение) {
  if (process.platform !== 'win32') return значение;
  const совпадение = POSIX_ДИСК_RE.exec(значение);
  if (!совпадение) return значение;
  return `${совпадение[1]}:${совпадение[2] || '/'}`;
}

function раскрытьUser(значение) {
  if (значение === '~') return homedir();
  if (значение.startsWith('~/')) {
    const дом = homedir();
    const сепа = дом.endsWith('/') || дом.endsWith('\\') ? '' : '/';
    return дом + сепа + значение.slice(2);
  }
  return значение;
}

/**
 * Ближайший аналог `os.path.realpath`: полный реалпуть для существующего пути;
 * для несуществующего — реалпуть самого глубокого существующего префикса плюс
 * лексический остаток (так же работает Python realpath по несуществующему файлу).
 * Второе — не отказ от реалпути: реалпуть целиком на UNC-каталоге, которого
 * нет, мог бы висеть секунда на сетевом стата, а нормализация вызывается на
 * каждом событии хука (находка м2-1 Python-версии).
 */
function канонизация(раскрытый) {
  if (existsSync(раскрытый)) {
    try {
      return realpathSync(раскрытый);
    } catch {
      /* ниже — лексический разбор */
    }
  }
  const части = раскрытый.split(/[\\/]+/);
  for (let длина = части.length - 1; длина >= 2; длина -= 1) {
    const префикс = части.slice(0, длина).join('/');
    if (!префикс) continue;
    if (existsSync(префикс)) {
      try {
        const реальный = realpathSync(префикс);
        const хвост = части.slice(длина).join('/');
        if (!хвост) return реальный;
        const сепа = /[/\\]$/.test(реальный) ? '' : path.sep;
        return `${реальный}${сепа}${хвост}`;
      } catch {
        break;
      }
    }
  }
  return раскрытый;
}

/**
 * Каноническая форма для сравнения с домом шлюза: UNC-префиксы сняты, разделители
 * единые, POSIX-диск Git Bash переписан (Windows), `~` раскрыта, реалпуть по
 * существу, `..` разрешены, регистр снят на Windows (Python normcase).
 */
export function нормализоватьПуть(значение, cwdRoot) {
  if (typeof значение !== 'string' || !значение) return null;
  let единообразный = переписатьPosixДиск(снятьUncПрефикс(значение).replaceAll('\\', '/'));
  единообразный = раскрытьUser(единообразный);
  if (cwdRoot && !path.isAbsolute(единообразный)) единообразный = path.resolve(cwdRoot, единообразный);
  const канонический = канонизация(единообразный);
  const норм = path.normalize(канонический);
  return process.platform === 'win32' ? норм.toLowerCase() : норм;
}

// --- Сравнение с домами --------------------------------------------------------------------

/** Дом этой сессии: `ODATA1C_HOME`, если задан, иначе документированный дефолт. */
export function домашнийКаталог() {
  const задан = process.env.ODATA1C_HOME;
  return задан ? задан : ДЕФОЛТНЫЙ_ДОМ;
}

export function известныеДома(home) {
  const кандидаты = [нормализоватьПуть(home), нормализоватьПуть(ДЕФОЛТНЫЙ_ДОМ)];
  const уникальные = [];
  for (const дом of кандидаты) {
    if (дом && !уникальные.includes(дом)) уникальные.push(дом);
  }
  return уникальные;
}

function родители(путь) {
  const список = [];
  let текущий = путь;
  while (true) {
    const родитель = path.dirname(текущий);
    if (родитель === текущий) break;
    список.push(родитель);
    текущий = родитель;
  }
  return список;
}

export function внутриКакогоТоДома(нормПуть, дома) {
  if (!нормПуть) return false;
  return дома.some((дом) => нормПуть === дом || родители(нормПуть).includes(дом));
}

export function путьЭтоДомИлиПредок(нормПуть, дома) {
  if (!нормПуть) return false;
  return дома.some((дом) => нормПуть === дом || родители(дом).includes(нормПуть));
}

// --- Базовое имя для сравнения -------------------------------------------------------------

/**
 * Базовое имя в виде для сравнения с защищёнными именами: без потока NTFS
 * (`::$DATA`), без хвостовых точек и пробелов, без учёта регистра.
 */
export function базовоеИмяДляСравнения(значение) {
  let имя = String(значение).replaceAll('\\', '/');
  const грань = имя.lastIndexOf('/');
  if (грань >= 0) имя = имя.slice(грань + 1);
  имя = имя.split(':', 1)[0];
  имя = имя.replace(/[ .]+$/, '');
  return имя.toLowerCase();
}

// --- Правило 3: упоминание дома в команде --------------------------------------------------

function домашниеформы(home) {
  const формы = [
    '\\.claude[/\\\\]odata1c',
    '%odata1c_home%',
    '\\$\\{odata1c_home\\}',
    '\\$(?:env:)?odata1c_home\\b',
  ];
  const значение = String(home ?? '');
  формы.push(escapeRe(значение));
  const перевёрнутое = значение.includes('\\')
    ? значение.replaceAll('\\', '/')
    : значение.replaceAll('/', '\\');
  if (перевёрнутое !== значение) формы.push(escapeRe(перевёрнутое));
  return формы;
}

function домашнийRegex(home) {
  return new RegExp(`(?:${домашниеформы(home).join('|')})`, 'gi');
}

export function командаУпоминаетДомШлюза(команда, home) {
  return домашнийRegex(home).test(команда);
}

export function естьШаблонОболочки(команда) {
  if (команда.includes('*') || команда.includes('?')) return true;
  if (ФИГУРНЫЕ_СКОБКИ_RE.test(команда)) return true;
  return КВАДРАТНЫЕ_СКОБКИ_RE.test(команда);
}

export function массовоеЧтениеДома(команда, home) {
  if (!ГЛАГОЛЫ_ЧТЕНИЯ_RE.test(команда)) return false;
  const поиск = домашнийRegex(home);
  let совпадение;
  while ((совпадение = поиск.exec(команда)) !== null) {
    const остаток = команда.slice(совпадение.index + совпадение[0].length);
    if (ДОПУЩЕННЫЙ_ПОДКАТАЛОГ_RE.test(остаток)) continue;
    return true;
  }
  return false;
}

// --- Правило 2: Glob-шаблон ----------------------------------------------------------------

function шаблонАбсолютный(шаблон) {
  if (шаблон.startsWith('~')) return true;
  if (шаблон.replaceAll('\\', '/').startsWith('/')) return true;
  return /^[A-Za-z]:[/\\]/.test(шаблон);
}

function каталогШаблона(шаблон) {
  const граница = ШАБЛОН_ГРАНИЦА_RE.exec(шаблон);
  const голова = граница ? шаблон.slice(0, граница.index) : шаблон;
  const единообразная = голова.replaceAll('\\', '/');
  if (единообразная.includes('/')) return единообразная.split('/').slice(0, -1).join('/') || '/';
  return единообразная;
}

export function проверитьGlobШаблон(pattern, дома, cwdRoot) {
  if (typeof pattern !== 'string' || !pattern) return null;
  if (BASH_ФАЙЛЫ_RE.test(pattern)) return denyError(ПРИЧИНА_ФАЙЛ);
  if (шаблонАбсолютный(pattern)) {
    const нормКаталог = нормализоватьПуть(каталогШаблона(pattern), cwdRoot);
    if (нормКаталог && путьЭтоДомИлиПредок(нормКаталог, дома)) return denyError(ПРИЧИНА_ФАЙЛ);
  }
  return null;
}

// --- Правило 2: путь-тулы ------------------------------------------------------------------

export function решитьПутевой(tool, input, дома, cwdRoot) {
  const кандидаты = ПУТЬ_ПОЛЯ[tool] || [];
  for (const поле of кандидаты) {
    const значение = input?.[поле];
    if (typeof значение !== 'string' || !значение) continue;
    const нормПуть = нормализоватьПуть(значение, cwdRoot);
    if (КАТАЛОЖНЫЕ_ТУЛЫ.has(tool) && путьЭтоДомИлиПредок(нормПуть, дома)) {
      return denyError(ПРИЧИНА_ФАЙЛ);
    }
    const имя = базовоеИмяДляСравнения(значение);
    if (ИМЯ_ПРЕФИКС_RE.test(имя) && внутриКакогоТоДома(нормПуть, дома)) {
      return denyError(ПРИЧИНА_ФАЙЛ);
    }
  }
  if (tool === 'glob') {
    const решение = проверитьGlobШаблон(input?.pattern, дома, cwdRoot);
    if (решение) return решение;
  }
  return null;
}

// --- Правило 3: команды оболочки -----------------------------------------------------------

export function решитьКомандную(input, home) {
  const команда = input?.command;
  if (typeof команда !== 'string' || !команда) return null;
  if (BASH_ФАЙЛЫ_RE.test(команда) || BASH_REVEAL_RE.test(команда) || BASH_SQLITE_RE.test(команда)) {
    return denyError(ПРИЧИНА_ФАЙЛ);
  }
  if (естьШаблонОболочки(команда) && командаУпоминаетДомШлюза(команда, home)) {
    return denyError(ПРИЧИНА_ШАБЛОН);
  }
  if (массовоеЧтениеДома(команда, home)) {
    return denyError(ПРИЧИНА_МАССОВОЕ_ЧТЕНИЕ);
  }
  return null;
}

// --- Единая точка входа --------------------------------------------------------------------

/**
 * Решение по событию execute.before V2. `home` — дом этой сессии (строка, в том
 * числе `~…`), плагин читает окружение заново на каждое событие; `cwdRoot` —
 * каталог проекта (для относительных путей; без него относительный путь никогда
 * не совпадёт с домом — Python-хук разрешал относительно CWD хук-процесса).
 */
export function решение(event, home, cwdRoot) {
  try {
    const tool = String(event?.tool || '').toLowerCase();
    const input = event?.input;
    if (!input || typeof input !== 'object') return null;
    if (ПУТЬ_ПОЛЯ[tool]) {
      return решитьПутевой(tool, input, известныеДома(home), cwdRoot);
    }
    if (КОМАНДНЫЕ_ТУЛЫ.has(tool)) {
      return решитьКомандную(input, home || ДЕФОЛТНЫЙ_ДОМ);
    }
    return null;
  } catch {
    return null; // сбой на неожиданном входе не блокирует тул
  }
}
