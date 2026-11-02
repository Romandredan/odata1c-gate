#!/usr/bin/env node
/**
 * Тесты плагина OpenCode 2.x (ишью Romandredan/odata1c-gate#2): ядро защиты
 * файлов каталога шлюза (opencode/plugin/guard-core.mjs — порт правил 2–3 хука
 * `plugin/hooks/pretooluse.py`), правило 1 в форме permission-хука, состав
 * плагина (навыки, агент, MCP-сервер, хуки) через setup() с мок-ctx.
 *
 * Источник истины сценариев — `tests/unit/test_plugin_hook.py`; каждая его
 * группа перенесена сюда под имена тулов и полей V2 (`read`/`filePath` вместо
 * `Read`/`file_path`, `shell` вместо пары `Bash`/`PowerShell` — в OpenCode
 * оболочка один тул). Отличия в поведении зафиксированы комментариями.
 *
 * Запуск: node tests/opencode-odata1c.test.mjs
 * Код возврата 0 — всё зелёное, 1 — есть упавшие проверки.
 */

import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const ОКОНЦЕ = import.meta.dirname;
const плагинПуть = path.join(ОКОНЦЕ, '..', 'opencode', 'plugin');
const { default: плагин } = await import(pathToFileURL(path.join(плагинПуть, 'odata1c.js')).href);
const ядро = await import(pathToFileURL(path.join(плагинПуть, 'guard-core.mjs')).href);
const реестр = await import(pathToFileURL(path.join(плагинПуть, 'registry.mjs')).href);

let прошло = 0;
let упало = 0;

function пусть(имя, условие) {
  if (условие) {
    прошло += 1;
    console.log(`ok — ${имя}`);
  } else {
    упало += 1;
    console.error(`FAIL — ${имя}`);
  }
}

function пустьРавно(имя, значение, ожидание) {
  if (значение === ожидание) {
    прошло += 1;
    console.log(`ok — ${имя}`);
  } else {
    упало += 1;
    console.error(`FAIL — ${имя}: ${JSON.stringify(значение)} !== ${JSON.stringify(ожидание)}`);
  }
}

function ожидаемЗапрет(имя, решение, причина) {
  const есть = Boolean(решение);
  const текст = решение?.message || '';
  пусть(имя, есть && (причина ? текст === причина : true));
}

function ожидаемМолчание(имя, решение) {
  пусть(имя, решение === null);
}

// --- Сессионные окружения (аналог `_окружение` Python-тестов) --------------------------------

const ПЕРЕМЕННЫЕ_ОКРУЖЕНИЯ = ['HOME', 'USERPROFILE', 'ODATA1C_HOME'];

let врем = mkdtempSync(path.join(tmpdir(), 'odata1c-opencode-test-'));
process.on('exit', () => rmSync(врем, { recursive: true, force: true }));

/**
 * Настроить окружение процесса на время проверки и вернуть функцию восстановления.
 * По умолчанию дом сессии — посторонний временный каталог (как `_окружение`):
 * «домашний» путь по умолчанию настоящего пользователя не участвует в сравнении.
 */
function вОкружении({ домПоУмолчанию = false, домСессии = true } = {}) {
  const сохранённые = ПЕРЕМЕННЫЕ_ОКРУЖЕНИЯ.map((имя) => [имя, process.env[имя]]);
  if (домСессии) {
    process.env.ODATA1C_HOME = path.join(врем, 'чужой_дом', 'odata1c');
  } else {
    delete process.env.ODATA1C_HOME;
  }
  if (домПоУмолчанию) {
    // Дефолтный дом строится от HOME/USERPROFILE тестового корня.
    process.env.HOME = врем;
    process.env.USERPROFILE = врем;
  }
  return () => {
    for (const [имя, значение] of сохранённые) {
      if (значение === undefined) delete process.env[имя];
      else process.env[имя] = значение;
    }
  };
}

/** Событие execute.before V2, как его отдаёт OpenCode: `tool` + `input`. */
function событие(тул, ввод) {
  return { tool: тул, input: ввод };
}

/**
 * Решение ядра на событие; `дом` — дом этой сессии (строка; undefined — берётся
 * из ODATA1C_HOME процесса), cwd — тестовый корень.
 */
function решить(тул, ввод, { дом } = {}) {
  return ядро.решение(событие(тул, ввод), дом ?? process.env.ODATA1C_HOME, врем);
}

// --- Правило 1: odata1c_commit → ask (permission-хук) ----------------------------------------

{
  // Отличие от Python-теста: в V2 у события нет pending_id (resources MCP-тула —
  // `*`), поэтому причина — фиксированный текст, а не «pending abc-1…».
  for (const действие of [
    'odata1c_odata1c_commit', // каноническое действие V2: <сервер>_<тул>
    'mcp__odata1c__odata1c_commit', // запасная форма, если ядро отдаёт имя с префиксом
    'mcp__plugin_odata1c_gate__odata1c_commit',
    'gate_odata1c_commit', // запасное имя сервера
  ]) {
    const событие = { action: действие, effect: 'allow', resources: ['*'] };
    ядро.правило1(событие);
    пустьРавно(`правило1 ${действие} — ask`, событие.effect, 'ask');
    пустьРавно(`правило1 ${действие} — причина`, событие.message, ядро.ПРИЧИНА_ЗАПИСЬ);
  }

  const чужой = { action: 'mcp__other__odata1c_commit', effect: 'allow' };
  ядро.правило1(чужой);
  пустьРавно('правило1 чужой сервер — молчит', чужой.effect, 'allow');

  const конфигDeny = { action: 'odata1c_odata1c_commit', effect: 'deny' };
  ядро.правило1(конфигDeny);
  пустьРавно('правило1 configured deny — финален', конфигDeny.effect, 'deny');

  const прочийТул = { action: 'read', effect: 'allow' };
  ядро.правило1(прочийТул);
  пустьРавно('правило1 прочий тул — молчит', прочийТул.effect, 'allow');
}

// --- Правило 2: файлы домашнего каталога шлюза → deny ----------------------------------------

const ИМЕНА_ФАЙЛОВ_ДОМА = [
  'bases.yaml',
  'daemon.yaml',
  'launcher.key',
  'gate.sqlite',
  'gate.sqlite-wal',
  'gate.sqlite-shm', // находка M-1 (Ruling 68): часть той же базы SQLite в режиме WAL
  'journal.sqlite',
  'journal.sqlite-wal',
  'journal.sqlite-shm',
];

// V2-имена тулов и полей. Третьи члены — алиасные написания полей (`file_path`),
// которые допускают кандидаты `ПУТЬ_ПОЛЯ`; проверяются на одном имени.
const ТУЛЫ_И_ПОЛЯ = [
  ['read', 'filePath', 'file_path'],
  ['edit', 'filePath', 'file_path'],
  ['write', 'filePath', 'file_path'],
  ['notebookedit', 'notebookPath', 'notebook_path'],
  ['grep', 'path', 'filePath'],
  ['glob', 'path', 'filePath'],
];

{
  for (const [тул, поле, алиас] of ТУЛЫ_И_ПОЛЯ) {
    const дом = path.join(врем, 'odata1c');
    mkdirSync(дом, { recursive: true });
    for (const имя of ИМЕНА_ФАЙЛОВ_ДОМА) {
      const запрет = решить(тул, { [поле]: path.join(дом, имя) }, { дом });
      ожидаемЗапрет(`правило2 ${тул} ${имя} — deny`, запрет, ядро.ПРИЧИНА_ФАЙЛ);
    }
    if (алиас !== поле) {
      const запрет = решить(тул, { [алиас]: path.join(дом, 'bases.yaml') }, { дом });
      ожидаемЗапрет(`правило2 ${тул} алиас ${алиас} — deny`, запрет, ядро.ПРИЧИНА_ФАЙЛ);
    }
  }
}

{
  // `MultiEdit` в Python-тестах проверялся отдельно от общего списка. У V2-мапы
  // он есть; тест подтверждает, что правило не потерялось при переносе.
  const дом = path.join(врем, 'odata1c');
  mkdirSync(дом, { recursive: true });
  const запрет = решить('multiedit', { filePath: path.join(дом, 'bases.yaml') }, { дом });
  ожидаемЗапрет('правило2 multiedit bases.yaml — deny', запрет, ядро.ПРИЧИНА_ФАЙЛ);
}

{
  // Находка M-2: все четыре написания на NTFS открывают тот же файл bases.yaml.
  const дом = path.join(врем, 'odata1c');
  mkdirSync(дом, { recursive: true });
  for (const написание of ['BASES.YAML', 'bases.yaml.', 'bases.yaml ', 'bases.yaml::$DATA']) {
    const запрет = решить('read', { filePath: path.join(дом, написание) }, { дом });
    ожидаемЗапрет(`правило2 написание ${JSON.stringify(написание)} — deny`, запрет, ядро.ПРИЧИНА_ФАЙЛ);
  }
}

{
  // Ruling 85: защищённое имя сравнивается как ПРЕФИКС — резервные копии, которые
  // владелец и скрипты реально оставляют рядом с рабочим файлом, не проходят.
  const дом = path.join(врем, 'odata1c');
  mkdirSync(дом, { recursive: true });
  for (const написание of [
    'bases.yaml.bak-adr15',
    'daemon.yaml.bak-before-ttl-1800',
    'gate.sqlite.bak-20260913-final.sqlite',
    'journal.sqlite-wal',
    'bases.yaml.bak',
    'bases.yaml.orig',
    'launcher.key.bak',
  ]) {
    const запрет = решить('read', { filePath: path.join(дом, написание) }, { дом });
    ожидаемЗапрет(`правило2 резервная копия ${написание} — deny`, запрет, ядро.ПРИЧИНА_ФАЙЛ);
  }

  // Ruling 85: та же префиксная проверка действует в общей ветке правила 2.
  const черезGrep = решить(
    'grep',
    { path: path.join(дом, 'daemon.yaml.bak-before-ttl-1800') },
    { дом },
  );
  ожидаемЗапрет('правило2 резервная копия через grep — deny', черезGrep, ядро.ПРИЧИНА_ФАЙЛ);

  // Ruling 85, регрессия: префиксная проверка не расширяется на bases.example.yaml
  // (документированный шаблон поставки).
  const шаблонПоставки = решить('read', { filePath: path.join(дом, 'bases.example.yaml') }, { дом });
  ожидаемМолчание('правило2 bases.example.yaml — разрешён', шаблонПоставки);
}

{
  // Minor: расширенные UNC-формы снимаются, сравнение с домом не теряется.
  const дом = path.join(врем, 'odata1c');
  mkdirSync(дом, { recursive: true });
  for (const префикс of ['\\\\?\\', '\\\\.\\']) {
    const запрет = решить('read', { filePath: префикс + path.join(дом, 'bases.yaml') }, { дом });
    ожидаемЗапрет(`правило2 UNC-префикс ${JSON.stringify(префикс)} — deny`, запрет, ядро.ПРИЧИНА_ФАЙЛ);
  }
}

{
  // Minor: `\\\\?\\UNC\\сервер\\ресурс\\...` разворачивается в `\\\\сервер\\ресурс\\...`.
  // Сервер localhost с заведомо несуществующим ресурсом: отказ приходит сразу.
  const домUnc = '\\\\?\\UNC\\localhost\\odata1c-no-such-share\\odata1c';
  const обычныйДом = '\\\\localhost\\odata1c-no-such-share\\odata1c';
  const запрет = ядро.решение(
    событие('read', { filePath: `${домUnc}\\bases.yaml` }),
    обычныйДом,
    врем,
  );
  ожидаемЗапрет('правило2 UNC расширенный сетевой путь — deny', запрет, ядро.ПРИЧИНА_ФАЙЛ);
}

{
  // `ODATA1C_HOME` не задан вовсе — дом по умолчанию; путь-событие написан
  // Windows-стилем `~\\...`. Оба написания `~` должны пониматься.
  const восстановить = вОкружении({ домПоУмолчанию: true });
  try {
    const запрет = ядро.решение(
      событие('read', { filePath: '~\\.claude\\odata1c\\bases.yaml' }),
      undefined,
      врем,
    );
    ожидаемЗапрет('правило2 тильда и обратные косые — deny', запрет, ядро.ПРИЧИНА_ФАЙЛ);
  } finally {
    восстановить();
  }
}

{
  // bases.yaml вне дома шлюза — разрешён.
  const наружу = решить('read', { filePath: path.join(врем, 'проект', 'bases.yaml') });
  ожидаемМолчание('правило2 bases.yaml вне дома — разрешён', наружу);

  // policy.yaml и recipes — открытые владельцу файлы, разрешены и в доме.
  const дом = path.join(врем, 'odata1c');
  mkdirSync(path.join(дом, 'bases', 'x'), { recursive: true });
  mkdirSync(path.join(дом, 'recipes', 'ut'), { recursive: true });
  for (const п of [path.join(дом, 'bases', 'x', 'policy.yaml'), path.join(дом, 'recipes', 'ut', 'a.yaml')]) {
    const решение = решить('read', { filePath: п }, { дом });
    ожидаемМолчание(`правило2 текст редактируемого общего файла ${path.basename(п)} — разрешён`, решение);
  }
}

{
  // Находка M-3: точные формы входа из текста ревью.
  const дом = path.join(врем, 'odata1c');
  mkdirSync(дом, { recursive: true });
  const grep = решить('grep', { pattern: 'password', path: дом, output_mode: 'content' }, { дом });
  ожидаемЗапрет('правило2 grep по дому с pattern и output_mode — deny', grep, ядро.ПРИЧИНА_ФАЙЛ);
  // `Glob` с path на весь дом и pattern `**/*` — перечисление состава дома целиком;
  // deny не зависит от присутствия pattern.
  const глоб = решить('glob', { path: дом, pattern: '**/*' }, { дом });
  ожидаемЗапрет('правило2 glob по дому с pattern — deny', глоб, ядро.ПРИЧИНА_ФАЙЛ);
}

{
  // Ruling 72: один вызов grep/glob с путём на весь дом рекурсивно вычитывает все
  // защищённые файлы разом.
  const дом = path.join(врем, 'odata1c');
  mkdirSync(дом, { recursive: true });
  for (const тул of ['grep', 'glob']) {
    const запрет = решить(тул, { path: дом }, { дом });
    ожидаемЗапрет(`правило2 ${тул} по дому целиком — deny`, запрет, ядро.ПРИЧИНА_ФАЙЛ);
  }

  // Ruling 72: предки дома тоже deny — поиск по ним рекурсивно заходит в дом.
  // Дом здесь дефолтный (`~/.claude/odata1c`), чтобы получить все три уровня
  // предков, управляя HOME/USERPROFILE.
  const восстановить = вОкружении({ домПоУмолчанию: true });
  try {
    const уровни = {
      'сам дом': path.join(врем, '.claude', 'odata1c'),
      '.claude': path.join(врем, '.claude'),
      'домашний каталог пользователя': врем,
    };
    for (const [уровень, п] of Object.entries(уровни)) {
      for (const тул of ['grep', 'glob']) {
        const запрет = ядро.решение(событие(тул, { path: п }), undefined, врем);
        ожидаемЗапрет(`правило2 ${тул} по ${уровень} — deny`, запрет, ядро.ПРИЧИНА_ФАЙЛ);
      }
    }
    // M-3, обратная сторона: документированные подкаталоги остаются разрешёнными.
    for (const подкаталог of ['bases', 'recipes', 'logs']) {
      for (const тул of ['grep', 'glob']) {
        const решение = решить(тул, { path: path.join(дом, подкаталог) }, { дом });
        ожидаемМолчание(`правило2 ${тул} в ${подкаталог} — разрешён`, решение);
      }
    }
    // Ruling 72 ограничивает каталоговую проверку grep/glob — Read принимает путь
    // к одному файлу; путь на весь дом ему не запрещён этим правилом.
    const читается = ядро.решение(событие('read', { filePath: дом }), undefined, врем);
    ожидаемМолчание('правило2 read по дому целиком — не денится', читается);
  } finally {
    восстановить();
  }
}

{
  // Ruling 86: Glob — поле pattern наравне с path.
  const дом = path.join(врем, 'odata1c');
  mkdirSync(дом, { recursive: true });
  const кореньШаблона = дом.split(path.sep).join('/');
  for (const маска of ['/*', '/**/*', '/*.yaml*']) {
    const запрет = решить('glob', { pattern: кореньШаблона + маска }, { дом });
    ожидаемЗапрет(`правило2 glob pattern ${маска} — deny`, запрет, ядро.ПРИЧИНА_ФАЙЛ);
  }
  // Ruling 86: шаблон без единого символа шаблона, называющий защищённое имя.
  const прямоИмя = решить('glob', { pattern: path.join(дом, 'bases.yaml') }, { дом });
  ожидаемЗапрет('правило2 glob pattern защищённое имя — deny', прямоИмя, ядро.ПРИЧИНА_ФАЙЛ);
  // Ruling 86, обратная сторона: абсолютный шаблон в разрешённый подкаталог.
  mkdirSync(path.join(дом, 'recipes'), { recursive: true });
  const вРецепты = решить(
    'glob',
    { pattern: path.join(дом, 'recipes').split(path.sep).join('/') + '/*.yaml' },
    { дом },
  );
  ожидаемМолчание('правило2 glob pattern в recipes — разрешён', вРецепты);
  // Ruling 86: относительный шаблон при path вне дома — разрешён.
  const относительный = решить('glob', { pattern: '*.yaml', path: path.join(врем, 'проект') });
  ожидаемМолчание('правимо2 glob pattern относительный вне дома — разрешён', относительный);
  // Известное ограничение: относительный шаблон с `..` из разрешённого подкаталога
  // — фиксируем текущее поведение (молчание), а не случайный пропуск.
  const выходИзПодкаталога = решить(
    'glob',
    { pattern: '../*.yaml', path: path.join(дом, 'recipes') },
    { дом },
  );
  ожидаемМолчание('правило2 glob pattern с `..` — известное ограничение, разрешён', выходИзПодкаталога);
}

{
  // Находка м-2: путь в POSIX-стиле Git Bash (`/c/...`) без буквы диска обязан
  // совпадать с домом через переписывание, а не через эвристику по частям пути.
  const дом = path.join(врем, 'odata1c');
  mkdirSync(дом, { recursive: true });
  const путьWindows = path.join(дом, 'bases.yaml');
  let путьPosix = путьWindows;
  if (путьWindows.length >= 2 && путьWindows[1] === ':') {
    путьPosix = `/${путьWindows[0].toLowerCase()}${путьWindows.slice(2).split(path.sep).join('/')}`;
  } else {
    путьPosix = путьWindows.split(path.sep).join('/');
  }
  const запрет = ядро.решение(событие('read', { filePath: путьPosix }), дом, врем);
  ожидаемЗапрет('правило2 POSIX без буквы диска — deny', запрет, ядро.ПРИЧИНА_ФАЙЛ);
}

{
  // Защита «в оба конца» (явный перечень домов = ODATA1C_HOME этой сессии И,
  // отдельно, документированный дефолт): сессия настроена на посторонний дом,
  // но путь к дефолтному дому владельца остаётся закрытым.
  const восстановить = вОкружении({ домСессии: false });
  try {
    process.env.ODATA1C_HOME = path.join(врем, 'другой_настроенный_дом', 'odata1c');
    process.env.HOME = врем;
    process.env.USERPROFILE = врем;
    const запрет = ядро.решение(
      событие('read', { filePath: path.join(врем, '.claude', 'odata1c', 'bases.yaml') }),
      undefined,
      врем,
    );
    ожидаемЗапрет('правило2 дефолтный дом при другом ODATA1C_HOME — deny', запрет, ядро.ПРИЧИНА_ФАЙЛ);
  } finally {
    восстановить();
  }
}

{
  // Ложное срабатывание раунда 0 устранено: путь репозитория, где рядом живут
  // `.claude/worktrees/...` и `src/odata1c/`, к дому шлюза отношения не имеет.
  const восстановить = вОкружении({ домСессии: false });
  try {
    process.env.HOME = path.join(врем, 'чужой_дефолтный_дом');
    process.env.USERPROFILE = process.env.HOME;
    process.env.ODATA1C_HOME = path.join(врем, 'рабочий_дом', 'odata1c');
    const путьРепозитория = path.join(
      врем, 'репозиторий', '.claude', 'worktrees', 'агент', 'src', 'odata1c', 'bases.yaml',
    );
    const решение = ядро.решение(событие('read', { filePath: путьРепозитория }), undefined, врем);
    ожидаемМолчание('правило2 путь src/odata1c репозитория — не ложное срабатывание', решение);
  } finally {
    восстановить();
  }
}

{
  // Принятый остаточный пробел: временный дом приёмки вне `.claude`, на который
  // сессия не настроена, — молчание ожидаемо и осознанно.
  const восстановить = вОкружении({ домСессии: false });
  try {
    process.env.HOME = path.join(врем, 'рабочий_дефолтный_дом');
    process.env.USERPROFILE = process.env.HOME;
    process.env.ODATA1C_HOME = path.join(врем, 'рабочий_дом', 'odata1c');
    const решение = ядро.решение(
      событие('read', { filePath: path.join(врем, 'temp_приёмки', 'odata1c', 'bases.yaml') }),
      undefined,
      врем,
    );
    ожидаемМолчание('правило2 временный дом приёмки вне .claude — известное ограничение', решение);
  } finally {
    восстановить();
  }
}

// --- Правило 3: shell -------------------------------------------------------------------------

// В OpenCode оболочка — один тул `shell`; параметризация Bash/PowerShell
// Python-тестов на V2 вырождается. Глаголы и написания команд остаются теми же.
{
  const восстановить = вОкружении();
  try {
    const запрещены = [
      'cat ~/.claude/odata1c/bases.yaml',
      'odata1c reveal \'[[inn:1]]\'',
      'uv run odata1c reveal x',
      'sqlite3 /home/u/.claude/odata1c/gate.sqlite .dump',
      'type C:\\Users\\u\\.claude\\odata1c\\daemon.yaml',
      // Ruling 73: имя переменной окружения или её наличие вообще ни на что не влияет.
      'cat $ODATA1C_HOME/bases.yaml',
    ];
    for (const команда of запрещены) {
      const запрет = решить('shell', { command: команда });
      ожидаемЗапрет(`правило3 ${команда} — deny`, запрет, ядро.ПРИЧИНА_ФАЙЛ);
    }

    const раунд1 = [
      'cat ~/.claude/odata1c/BASES.YAML', // находка M-4: регистр имени файла (Ruling 73)
      'odata1c.exe reveal "[[inn:1]]"', // находка M-4: суффикс .exe
      'ODATA1C reveal x', // находка M-4: регистр слова odata1c рядом с reveal
      'python -m odata1c reveal x', // находка M-4: запуск модулем
      'cd ~/.claude/odata1c && cat bases.yaml', // находка м-1: относительный путь после cd
      'cat gate.sqlite', // находка м-1: имя без пути и без слова odata1c рядом
      'cat launcher.key', // находка м-1: то же для launcher.key
      // Ruling 73: принятое ложное срабатывание — deny всё равно срабатывает.
      'grep bases.yaml docs/',
    ];
    for (const команда of раунд1) {
      const запрет = решить('shell', { command: команда });
      ожидаемЗапрет(`правило3 раунд-1 ${команда} — deny`, запрет, ядро.ПРИЧИНА_ФАЙЛ);
    }

    // Находка м-4: sqlite3 и имя файла на разных строках одной команды.
    const многострочный = решить('shell', { command: 'sqlite3 \\\n  gate.sqlite .dump' });
    ожидаемЗапрет('правило3 sqlite3 многострочный — deny', многострочный, ядро.ПРИЧИНА_ФАЙЛ);

    // Ruling 85: правило 3 ловит и резервные копии защищённых файлов.
    for (const команда of [
      'cat ~/.claude/odata1c/bases.yaml.bak-adr15',
      'type daemon.yaml.bak-before-ttl-1800',
      'cp gate.sqlite.bak-20260913-final.sqlite /tmp/x',
    ]) {
      const запрет = решить('shell', { command: команда });
      ожидаемЗапрет(`правило3 резервная копия ${команда} — deny`, запрет, ядро.ПРИЧИНА_ФАЙЛ);
    }

    for (const команда of [
      'odata1c policy show trade_dev',
      'odata1c recipe check ut',
      'odata1c doctor',
      'cat README.md',
      'grep gate_secret docs/',
    ]) {
      const решение = решить('shell', { command: команда });
      ожидаемМолчание(`правило3 ${команда} — разрешён`, решение);
    }
  } finally {
    восстановить();
  }
}

{
  // Ruling 77: путь дома шлюза вместе с символом шаблона в одной команде —
  // deny с отдельной причиной.
  const восстановить = вОкружении();
  try {
    for (const команда of [
      'cat ~/.claude/odata1c/bases.y*',
      'cat ~/.claude/odata1c/*',
      "powershell -c 'gc $env:ODATA1C_HOME\\*'",
      'Get-Content $env:ODATA1C_HOME\\*',
      'type %ODATA1C_HOME%\\*.yaml',
      'cat ~\\.claude\\odata1c\\*', // обратные косые, не только прямые
      'cat $ODATA1C_HOME/*',
      'cat ${ODATA1C_HOME}/*',
    ]) {
      const запрет = решить('shell', { command: команда });
      ожидаемЗапрет(`правило3 шаблон ${команда} — deny`, запрет, ядро.ПРИЧИНА_ШАБЛОН);
    }

    // Ruling 77, третий случай: ODATA1C_HOME настроен на каталог без `.claude`,
    // команда называет фактическое значение напрямую вместе с шаблоном — deny.
    const чужойДом = path.join(врем, 'нестандартный_дом', 'odata1c');
    const запрет = решить('shell', { command: `type ${чужойДом}\\*.yaml` }, { дом: чужойДом });
    ожидаемЗапрет('правило3 шаблон фактического значения ODATA1C_HOME — deny', запрет, ядро.ПРИЧИНА_ШАБЛОН);

    // Обратные стороны правила: путь дома без шаблона и шаблон без дома разрешены.
    const безШаблона = решить('shell', { command: 'ls ~/.claude/odata1c/recipes/ut' });
    ожидаемМолчание('правило3 путь дома без шаблона — разрешён', безШаблона);
    const безДома = решить('shell', { command: 'ls /some/other/dir/*' });
    ожидаемМолчание('правило3 шаблон без дома — разрешён', безДома);

    // Ruling 87: фигурное и квадратное раскрытие — те же обходы.
    const фигурные = решить('shell', { command: 'cat ~/.claude/odata1c/{bases,daemon}.yaml' });
    ожидаемЗапрет('правило3 фигурные скобки в доме — deny', фигурные, ядро.ПРИЧИНА_ШАБЛОН);
    const квадратные = решить('shell', { command: 'cat ~/.claude/odata1c/[bd]ases.yaml' });
    ожидаемЗапрет('правило3 квадратные скобки в доме — deny', квадратные, ядро.ПРИЧИНА_ШАБЛОН);
    // Ловушка наивной правки: `${ODATA1C_HOME}` содержит `{}` без запятой — это
    // НЕ раскрытие скобок.
    const подстановка = решить('shell', { command: 'cat ${ODATA1C_HOME}/recipes/ut.yaml' });
    ожидаемМолчание('правило3 фигурные скобки подстановки переменной — разрешён', подстановка);
  } finally {
    восстановить();
  }
}

{
  // Ruling 88: путь дома как КОРЕНЬ вместе с глаголом чтения/перечисления/
  // копирования/архивации — deny с отдельной причиной.
  const восстановить = вОкружении();
  try {
    for (const команда of [
      'Get-ChildItem $env:ODATA1C_HOME | Get-Content',
      'Get-ChildItem ~\\.claude\\odata1c -Recurse | Get-Content',
      'cat ~/.claude/odata1c',
      'ls ~/.claude/odata1c',
      'ls ~/.claude/odata1c/',
      'ls $ODATA1C_HOME',
      'tar czf x.tgz ~/.claude/odata1c',
      'cp -r ~/.claude/odata1c /tmp/x',
    ]) {
      const запрет = решить('shell', { command: команда });
      ожидаемЗапрет(`правило3 массовое чтение ${команда} — deny`, запрет, ядро.ПРИЧИНА_МАССОВОЕ_ЧТЕНИЕ);
    }

    for (const команда of [
      'ls ~/.claude/odata1c/recipes/ut',
      'cat ${ODATA1C_HOME}/recipes/ut.yaml',
      'Get-ChildItem $env:ODATA1C_HOME/bases',
      'cat ~/.claude/odata1c/logs/daemon.log',
    ]) {
      const решение = решить('shell', { command: команда });
      ожидаемМолчание(`правило3 подкаталог ${команда} — разрешён`, решение);
    }

    // Дефолтный дом (`ODATA1C_HOME` не задан): обе стороны разом.
    const восстановитьДефолт = вОкружении({ домСессии: false });
    try {
      const разрешено = ядро.решение(
        событие('shell', { command: 'ls ~/.claude/odata1c/recipes/ut' }),
        undefined,
        врем,
      );
      ожидаемМолчание('правило3 дефолтный дом recipes — разрешён', разрешено);
      const запрет = ядро.решение(
        событие('shell', { command: 'ls ~/.claude/odata1c/' }),
        undefined,
        врем,
      );
      ожидаемЗапрет('правило3 дефолтный дом корень — deny', запрет, ядро.ПРИЧИНА_МАССОВОЕ_ЧТЕНИЕ);
    } finally {
      восстановитьДефолт();
    }

    // echo не входит в список глаголов — печать пути, не его содержимого.
    const эхо = решить('shell', { command: 'echo $ODATA1C_HOME' });
    ожидаемМолчание('правило3 echo дома без глагола — разрешён', эхо);
  } finally {
    восстановить();
  }
}

// --- Устойчивость к неожиданному входу ---------------------------------------------------------

{
  const восстановить = вОкружении();
  try {
    // Python-тесты «битый json / пустой stdin / json не объект» в V2 соответствуют
    // входу, который нельзя прочитать как событие — пусто, массив, null, без полей.
    ожидаемМолчание('вход пустой объект — молчит', ядро.решение({}, undefined, врем));
    ожидаемМолчание('вход массив — молчит', ядро.решение([], undefined, врем));
    ожидаемМолчание('вход null — молчит', ядро.решение(null, undefined, врем));
    ожидаемМолчание('событие без полей — молчит', ядро.решение({ tool: undefined }, undefined, врем));
    // os.path.abspath на NUL-байте бросал ValueError; в V2 — сбой внутри решения,
    // гашеный до null, а не исключение через плагин.
    const путьСNul = 'some\u0000dir/bases.yaml';
    ожидаемМолчание('путь с NUL-байтом — не роняет', ядро.решение(
      событие('read', { filePath: путьСNul }), undefined, врем,
    ));
    // Неизвестный тул (MCP-тул шлюза) — молчит: правило 2 на него не смотрит.
    ожидаемМолчание('неизвестный тул — молчит', ядро.решение(
      событие('odata1c_query', { base: 'trade_dev' }), undefined, врем,
    ));
  } finally {
    восстановить();
  }
}

// --- Состав плагина: frontmatter, навыки, агент, permissions ----------------------------------

{
  // Настоящий файл агента разбирается: описание с YAML-эскейпами, name, тело.
  const текст = readFileSync(path.join(ОКОНЦЕ, '..', 'plugin', 'agents', 'odata1c-investigator.md'), 'utf8');
  const разбор = реестр.parseFrontmatter(текст);
  пусть('агент: frontmatter разбирается', разбор !== null);
  пустьРавно('агент: name из frontmatter', разбор?.data?.name, 'odata1c-investigator');
  пусть('агент: непустое описание', typeof разбор?.data?.description === 'string'
    && разбор.data.description.length > 50);
  // Реальные байты описания: `\\n` в файле — YAML-эскейп, в значении — текст `\n`
  // (как и видит Claude Code тем же YAML).
  пусть('агент: YAML-эскейп в значении', разбор?.data?.description?.includes('\\n'));
  пусть('агент: непустое тело', typeof разбор?.body === 'string' && разбор.body.length > 50);

  const агенты = реестр.agentsV2From(path.join(ОКОНЦЕ, '..', 'plugin', 'agents'));
  пустьРавно('плагин: агент один', агенты.length, 1);
  const [имяАгента, агентDef] = агенты[0];
  пустьРавно('плагин: имя агента — odata1c-investigator', имяАгента, 'odata1c-investigator');
  пустьРавно('плагин: режим агента — subagent', агентDef.mode, 'subagent');
  пусть('плагин: тело в system', typeof агентDef.system === 'string' && агентDef.system.length > 50);

  // Permissions агента-следователя: разрешающая база `*` первой (last-match-wins),
  // затем deny-исключения. Читательский набор шлюза остаётся разрешён.
  const правила = агентDef.permissions;
  пусть('агент: permissions — непустой массив', Array.isArray(правила) && правила.length > 0);
  пусть('агент: первая база allow *', правила[0].action === '*' && правила[0].effect === 'allow');
  for (const действие of ['edit', 'shell', 'subagent', 'webfetch', 'websearch']) {
    пусть(`агент: ${действие} deny`,
      правила.some((п) => п.action === действие && п.effect === 'deny'));
  }
  const записные = [
    'create', 'update', 'mark_for_deletion', 'action', 'undo', 'commit', 'journal',
    'delete_record', 'reindex',
  ];
  const серверы = ['odata1c', 'gate', 'plugin_odata1c_gate'];
  for (const тул of записные) {
    for (const сервер of серверы) {
      const действие = `${сервер}_odata1c_${тул}`;
      пусть(`агент: ${действие} deny`,
        правила.some((п) => п.action === действие && п.effect === 'deny'));
    }
  }
  const всегоDeny = правила.filter((п) => п.effect === 'deny').length;
  пустьРавно('агент: число deny-правил', всегоDeny, 5 + записные.length * серверы.length);
  for (const тул of ['query', 'get', 'info', 'recipe', 'raw_get', 'find_entity', 'describe_entity']) {
    пусть(`агент: читательский ${тул} не деnится`,
      !правила.some((п) => п.action.endsWith(`_odata1c_${тул}`) && п.effect === 'deny'));
  }
}

{
  // Четыре навыка поставки разбираются с непустым описанием и абсолютным путём.
  const список = реестр.skillsFrom(path.join(ОКОНЦЕ, '..', 'plugin', 'skills'));
  const имена = список.map((с) => с.id).sort();
  пустьРавно('плагин: навыки поставки', JSON.stringify(имена),
    JSON.stringify(['odata1c', 'odata1c-policy', 'odata1c-recipe', 'odata1c-setup']));
  for (const навык of список) {
    пусть(`навык ${навык.id}: описание`,
      typeof навык.description === 'string' && навык.description.length > 20);
    пусть(`навык ${навык.id}: путь абсолютный`, path.isAbsolute(навык.path));
    пусть(`навык ${навык.id}: content с frontmatter`,
      typeof навык.content === 'string' && навык.content.startsWith('---'));
  }
}

// --- Состав плагина: setup() через мок-ctx ------------------------------------------------------

{
  const ctxКаталог = mkdtempSync(path.join(tmpdir(), 'odata1c-plugin-ctx-'));
  const сохранённые = ПЕРЕМЕННЫЕ_ОКРУЖЕНИЯ.map((имя) => [имя, process.env[имя]]);
  const обработчики = { toolBefore: [], permissionEvaluate: [] };

  function сделатьCtx() {
    const skills = new Map();
    const agents = new Map();
    const mcp = new Map();
    let disposeCount = 0;
    const регистрация = { dispose: async () => { disposeCount += 1; } };
    const ctx = {
      location: { directory: ctxКаталог },
      skill: {
        transform: async (cb) => {
          cb({
            get: (id) => skills.get(id),
            add: (запись) => skills.set(запись.id, запись),
          });
          return регистрация;
        },
      },
      agent: {
        transform: async (cb) => {
          cb({
            get: (id) => agents.get(id),
            update: (имя, fn) => {
              const текущий = agents.get(имя) || {};
              fn(текущий);
              agents.set(имя, текущий);
            },
          });
          return регистрация;
        },
      },
      mcp: {
        transform: async (cb) => {
          cb({
            get: (id) => mcp.get(id),
            set: (id, значение) => mcp.set(id, значение),
          });
          return регистрация;
        },
      },
      tool: {
        hook: (имяСобытия, cb) => {
          if (имяСобытия === 'execute.before') обработчики.toolBefore.push(cb);
          return регистрация;
        },
      },
      permission: {
        hook: (имяСобытия, cb) => {
          if (имяСобытия === 'evaluate') обработчики.permissionEvaluate.push(cb);
          return регистрация;
        },
      },
      внутренние: { skills, agents, mcp, dispose: () => disposeCount },
    };
    return ctx;
  }

  // Окружение хука: дом сессии — временный каталог, дефолтный дом владельца
  // не задет; решение() закрывает оба, здесь важна только повторяемость.
  const дом = path.join(ctxКаталог, 'odata1c');
  mkdirSync(дом, { recursive: true });
  process.env.ODATA1C_HOME = дом;
  process.env.HOME = ctxКаталог;
  process.env.USERPROFILE = ctxКаталог;

  try {
    const ctx = сделатьCtx();
    const очистка = await плагин.setup(ctx);

    const навыки = [...ctx.внутренние.skills.keys()].sort();
    пустьРавно('setup: четыре навыка зарегистрированы', JSON.stringify(навыки),
      JSON.stringify(['odata1c', 'odata1c-policy', 'odata1c-recipe', 'odata1c-setup']));

    пусть('setup: агент зарегистрирован', ctx.внутренние.agents.has('odata1c-investigator'));
    const агент = ctx.внутренние.agents.get('odata1c-investigator');
    пусть('setup: агент с permissions', Array.isArray(агент.permissions) && агент.permissions.length > 0);
    пусть('setup: агент с system', typeof агент.system === 'string' && агент.system.length > 50);
    пусть('setup: агент с description', typeof агент.description === 'string' && агент.description.length > 50);

    const mcpCfg = ctx.внутренние.mcp.get('odata1c');
    пусть('setup: MCP-сервер odata1c зарегистрирован', Boolean(mcpCfg));
    пустьРавно('setup: MCP type local', mcpCfg?.type, 'local');
    пустьРавно('setup: MCP включён', mcpCfg?.enabled, true);
    const команда = Array.isArray(mcpCfg?.command) ? mcpCfg.command : [];
    пустьРавно('setup: MCP запускается uvx', команда[0], 'uvx');
    const версия = readFileSync(
      path.join(ОКОНЦЕ, '..', 'src', 'odata1c', '__about__.py'), 'utf8',
    ).match(/"([^"]+)"/)[1];
    пусть('setup: MCP закрепляет версию пакета', команда.join(' ').includes(`odata1c-gate==${версия}`));

    пустьРавно('setup: хук execute.before один', обработчики.toolBefore.length, 1);
    пустьРавно('setup: хук permission evaluate один', обработчики.permissionEvaluate.length, 1);

    // Хук execute.before бросает запрет guard-ошибкой с фиксированной причиной.
    let брошено = null;
    try {
      обработчики.toolBefore[0]({ tool: 'read', input: { filePath: path.join(дом, 'bases.yaml') } });
    } catch (ошибка) {
      брошено = ошибка;
    }
    пусть('setup: хук deny — guard-ошибка', ядро.guardDenial(брошено));
    пустьРавно('setup: deny причина фиксированная', брошено?.message, ядро.ПРИЧИНА_ФАЙЛ);

    // Чтение вне дома и неизвестный тул не блокируются.
    let незаблокировано = false;
    try {
      обработчики.toolBefore[0]({
        tool: 'read',
        input: { filePath: path.join(ctxКаталог, 'проект', 'README.md') },
      });
      обработчики.toolBefore[0]({ tool: 'odata1c_query', input: { base: 'trade_dev' } });
      незаблокировано = true;
    } catch {
      незаблокировано = false;
    }
    пусть('setup: чтение вне дома и MCP-тул не блокируются', незаблокировано);

    // Permission-хук превращает commit в ask, прочие тула не трогает.
    const коммит = { action: 'odata1c_odata1c_commit', effect: 'allow', resources: ['*'] };
    обработчики.permissionEvaluate[0](коммит);
    пустьРавно('setup: permission-хук commit → ask', коммит.effect, 'ask');
    const прочий = { action: 'read', effect: 'allow' };
    обработчики.permissionEvaluate[0](прочий);
    пустьРавно('setup: permission-хук прочий тул — молчит', прочий.effect, 'allow');

    // Cleanup вызывает dispose всех регистраций.
    if (typeof очистка === 'function') await очистка();
    пусть('setup: cleanup вызвал dispose регистраций', ctx.внутренние.dispose() > 0);

    // Повторное setup не дублирует записи.
    const ctx2 = сделатьCtx();
    await плагин.setup(ctx2);
    пустьРавно('setup: повторный setup — те же навыки без дублей', JSON.stringify(
      [...ctx2.внутренние.skills.keys()].sort(),
    ), JSON.stringify(навыки));
  } finally {
    for (const [имя, значение] of сохранённые) {
      if (значение === undefined) delete process.env[имя];
      else process.env[имя] = значение;
    }
    rmSync(ctxКаталог, { recursive: true, force: true });
  }
}

{
  // Аналог Python-теста «скрипт — только стандартная библиотека»: модули плагина
  // импортируют либо node:-модули, либо относительные файлы поставки.
  for (const файл of ['odata1c.js', 'guard-core.mjs', 'registry.mjs']) {
    const текст = readFileSync(path.join(плагинПуть, файл), 'utf8');
    const спецификации = [...текст.matchAll(/^\s*import[^;\n]*from\s+['"]([^'"]+)['"]/gm)]
      .map((совпадение) => совпадение[1]);
    пусть(`импорты ${файл}: есть import`, спецификации.length > 0);
    const внешние = спецификации.filter((с) => !с.startsWith('node:') && !с.startsWith('.'));
    пустьРавно(`импорты ${файл}: только node:/относительные`, внешние.length, 0);
  }
}

console.log('');
if (упало) {
  console.error(`упало проверок: ${упало} (прошло ${прошло})`);
  process.exitCode = 1;
} else {
  console.log(`все проверки прошли: ${прошло}`);
}
