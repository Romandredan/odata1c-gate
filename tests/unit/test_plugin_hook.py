"""Хук `PreToolUse` плагина (`plugin/hooks/pretooluse.py`, M3 задача 4, design-doc §5).

Скрипт — один файл на стандартной библиотеке Python без зависимостей проекта: тесты запускают
его настоящим подпроцессом (`sys.executable`, не импорт) — это то же, чем его запустит Claude
Code, и заодно доказывает отсутствие импортов пакета `odata1c`. Ввод — JSON на stdin, как отдаёт
Claude Code событию `PreToolUse`; вывод — JSON `hookSpecificOutput` или пустая строка, код
возврата всегда 0 (хук никогда не должен падать кодом 2 — это остановило бы тул сильнее, чем
осознанный `deny`).

Каждый тест собирает окружение подпроцесса сам (`_окружение`): по умолчанию `ODATA1C_HOME`
указывает на заведомо посторонний временный каталог, чтобы «домашний» путь по умолчанию
(`~/.claude/odata1c` настоящего пользователя) никогда не участвовал в сравнении — единственное
исключение оговорено явно в тесте на `~`."""

import json
import os
import pathlib
import subprocess
import sys

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
СКРИПТ = КОРЕНЬ / "plugin" / "hooks" / "pretooluse.py"


# Скрипт сам переводит свои stdin/stdout в UTF-8 (`main`, `.reconfigure`) — не должен зависеть от
# того, как была запущена сама тестовая сессия. Эти переменные убираются из окружения подпроцесса
# намеренно: если бы `.reconfigure` не сработал, тесты с кириллицей в путях (ниже, домашний
# каталог "чужой_дом") ловили бы молчание вместо ожидаемого решения даже на Windows с не-UTF-8
# кодовой страницей консоли — по умолчанию, без специальной настройки хоста.
_ПЕРЕМЕННЫЕ_КОДИРОВКИ = ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO")


def _окружение(tmp_path: pathlib.Path, **переопределения: str) -> dict[str, str]:
    """Окружение подпроцесса: копия текущего (нужны `PATH`/`SystemRoot` для запуска интерпретатора
    на Windows) без переменных, которые вручную форсируют UTF-8 в дочернем процессе, с
    `ODATA1C_HOME`, указанным на посторонний временный каталог по умолчанию, и любыми точечными
    переопределениями теста."""
    окружение = dict(os.environ)
    for имя in _ПЕРЕМЕННЫЕ_КОДИРОВКИ:
        окружение.pop(имя, None)
    окружение["ODATA1C_HOME"] = str(tmp_path / "чужой_дом" / "odata1c")
    окружение.update(переопределения)
    return окружение


def _запустить(событие: dict, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(СКРИПТ)],
        input=json.dumps(событие, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=10,
    )


def _запустить_текст(текст: str, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(СКРИПТ)],
        input=текст,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=10,
    )


def _решение(результат: subprocess.CompletedProcess[str]) -> dict | None:
    assert результат.returncode == 0, результат.stderr
    вывод = результат.stdout.strip()
    if not вывод:
        return None
    return json.loads(вывод)["hookSpecificOutput"]


# --- Правило 1: odata1c_commit → ask -----------------------------------------------------


@pytest.mark.parametrize(
    "тул",
    ["mcp__plugin_odata1c_gate__odata1c_commit", "mcp__odata1c__odata1c_commit"],
)
def test_commit_ask_с_pending_id(тул, tmp_path):
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"pending_id": "abc-1"}},
        env=_окружение(tmp_path),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "ask"
    assert "pending abc-1" in решение["permissionDecisionReason"]


def test_commit_другого_сервера_молчит(tmp_path):
    результат = _запустить(
        {"tool_name": "mcp__other__odata1c_commit", "tool_input": {"pending_id": "abc-1"}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


# --- Правило 2: файлы домашнего каталога шлюза → deny -------------------------------------

_ИМЕНА_ФАЙЛОВ_ДОМА = [
    "bases.yaml",
    "daemon.yaml",
    "launcher.key",
    "gate.sqlite",
    "gate.sqlite-wal",
    "gate.sqlite-shm",  # находка M-1 (Ruling 68): -shm — часть той же базы SQLite в режиме WAL.
    "journal.sqlite",
    "journal.sqlite-wal",
    "journal.sqlite-shm",  # находка M-1.
]

_ТУЛЫ_И_ПОЛЯ = [
    ("Read", "file_path"),
    ("Edit", "file_path"),
    ("Write", "file_path"),
    ("NotebookEdit", "notebook_path"),
    ("Grep", "path"),
    ("Glob", "path"),
]


@pytest.mark.parametrize("имя", _ИМЕНА_ФАЙЛОВ_ДОМА)
@pytest.mark.parametrize("тул,поле", _ТУЛЫ_И_ПОЛЯ)
def test_файлы_дома_deny(тул, поле, имя, tmp_path):
    дом = tmp_path / "odata1c"
    путь = дом / имя

    результат = _запустить(
        {"tool_name": тул, "tool_input": {поле: str(путь)}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


def test_multiedit_дома_deny(tmp_path):
    """`MultiEdit` не входит в параметризацию выше (там один универсальный список полей на все
    случаи), но матчер `hooks.json` его перечисляет отдельно от `Edit` — правило 2 обязано
    сработать и на нём, с тем же полем `file_path`, что и у `Edit`."""
    дом = tmp_path / "odata1c"
    путь = дом / "bases.yaml"

    результат = _запустить(
        {"tool_name": "MultiEdit", "tool_input": {"file_path": str(путь)}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "написание",
    [
        "BASES.YAML",  # регистр (находка M-2, Ruling 68: сравнение без учёта регистра).
        "bases.yaml.",  # хвостовая точка — NTFS открывает тот же файл, что и без неё.
        "bases.yaml ",  # хвостовой пробел — то же самое.
        "bases.yaml::$DATA",  # неименованный поток NTFS (Alternate Data Stream) того же файла.
    ],
)
def test_иное_написание_имени_файла_deny(написание, tmp_path):
    """Находка M-2: все четыре написания на NTFS открывают тот же файл `bases.yaml` (проверено
    ревью экспериментально), а сравнение по точному совпадению строки их пропускало."""
    дом = tmp_path / "odata1c"
    путь = дом / написание

    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": str(путь)}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "написание",
    [
        "bases.yaml.bak-adr15",
        "daemon.yaml.bak-before-ttl-1800",
        "gate.sqlite.bak-20260913-final.sqlite",
        "journal.sqlite-wal",
        "bases.yaml.bak",
        "bases.yaml.orig",
        "launcher.key.bak",
    ],
)
def test_резервная_копия_защищённого_файла_deny(написание, tmp_path):
    """Ruling 85 (раунд 3, повторное ревью, находка M2-1): защищённое имя сравнивается как
    ПРЕФИКС, без учёта регистра — резервные копии, которые владелец и скрипты этапов M2/ADR-0015
    реально оставляют рядом с рабочим файлом (`bases.yaml.bak-adr15` в доме владельца — ревьюер
    проверил не гипотетически, файл там лежит), больше не проходят молчанием. Явные `-wal`/`-shm`
    в списке защищённых имён стали не нужны — тот же префикс их уже покрывает без отдельной
    записи (`journal.sqlite-wal` в списке выше — тому доказательство)."""
    дом = tmp_path / "odata1c"
    путь = дом / написание

    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": str(путь)}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


def test_резервная_копия_через_grep_deny(tmp_path):
    """Ruling 85: та же префиксная проверка действует в общей ветке правила 2 — не только у
    `Read`, но и у `Grep`/`Glob` через поле `path`."""
    дом = tmp_path / "odata1c"
    путь = дом / "daemon.yaml.bak-before-ttl-1800"

    результат = _запустить(
        {"tool_name": "Grep", "tool_input": {"path": str(путь)}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


def test_bases_example_yaml_не_защищённое_имя_разрешён(tmp_path):
    """Ruling 85, регрессия: префиксная проверка не должна расшириться на `bases.example.yaml`
    (документированный шаблон поставки, не рабочий файл владельца) — имя расходится с
    `bases.yaml` уже на седьмом символе (`e` вместо `y`), а не продолжает его точкой/дефисом."""
    дом = tmp_path / "odata1c"
    путь = дом / "bases.example.yaml"

    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": str(путь)}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


@pytest.mark.parametrize("префикс", ["\\\\?\\", "\\\\.\\"])
def test_unc_префикс_снимается_deny(префикс, tmp_path):
    """Minor (раунд 3, повторное ревью, находка м2-1): докстрока `_нормализовать_путь` с раунда 1
    обещала независимость от UNC-формы (`\\\\?\\...`), но фактически ничего с ней не делала —
    `os.path.realpath` такую строку не разбирает как букву диска, и сравнение с домом не совпадало
    ни при каком `ODATA1C_HOME`. Правило 3 (Bash) ту же строку ловит по имени файла — асимметрия,
    как и в M2-1."""
    дом = tmp_path / "odata1c"
    путь = дом / "bases.yaml"

    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": префикс + str(путь)}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


def test_unc_расширенный_сетевой_путь_разворачивается_deny(tmp_path):
    """Minor: `\\\\?\\UNC\\сервер\\ресурс\\...` — расширенная форма сетевого пути, должна
    развернуться в обычные `\\\\сервер\\ресурс\\...`, а не остаться нераспознанной строкой,
    которая никогда не совпадёт с домом.

    Сервер — `localhost` с заведомо несуществующим ресурсом: хук канонизирует путь через
    `os.path.realpath`, и на Windows сетевой путь к вымышленному серверу (`\\\\server\\...`)
    заставлял систему искать этот сервер в сети — около 11 с при тайм-ауте запуска хука 10 с, тест
    падал по времени, а не по логике. Имя `localhost` разрешается локально, отказ приходит сразу."""
    дом_unc = "\\\\?\\UNC\\localhost\\odata1c-no-such-share\\odata1c"
    обычный_дом = "\\\\localhost\\odata1c-no-such-share\\odata1c"

    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": дом_unc + "\\bases.yaml"}},
        env=_окружение(tmp_path, ODATA1C_HOME=обычный_дом),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


def test_путь_с_тильдой_и_обратными_косыми_deny(tmp_path):
    """`ODATA1C_HOME` не задан вовсе — дом берётся по умолчанию (`~/.claude/odata1c`), а путь из
    события написан в стиле Windows-команды (`~\\...`), а не как отдаёт его сам Claude Code
    (обычно POSIX-стиль `/`): normalisation должна понимать оба написания `~`."""
    окружение = dict(os.environ)
    for имя in _ПЕРЕМЕННЫЕ_КОДИРОВКИ:
        окружение.pop(имя, None)
    окружение.pop("ODATA1C_HOME", None)
    окружение["HOME"] = str(tmp_path)
    окружение["USERPROFILE"] = str(tmp_path)

    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": "~\\.claude\\odata1c\\bases.yaml"}},
        env=окружение,
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


def test_bases_yaml_вне_дома_разрешён(tmp_path):
    путь = tmp_path / "проект" / "bases.yaml"

    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": str(путь)}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_policy_и_recipes_разрешены(tmp_path):
    дом = tmp_path / "odata1c"
    env = _окружение(tmp_path, ODATA1C_HOME=str(дом))

    for путь in (дом / "bases" / "x" / "policy.yaml", дом / "recipes" / "ut" / "a.yaml"):
        результат = _запустить(
            {"tool_name": "Read", "tool_input": {"file_path": str(путь)}},
            env=env,
        )
        assert результат.returncode == 0
        assert результат.stdout.strip() == "", путь


def test_grep_по_дому_с_pattern_и_output_mode_deny(tmp_path):
    """Находка M-3, точная форма входа из текста ревью: `Grep` с `pattern="password"`,
    `output_mode="content"` и `path` на весь дом — чтение содержимого `bases.yaml` с паролем
    одним вызовом."""
    дом = tmp_path / "odata1c"

    результат = _запустить(
        {
            "tool_name": "Grep",
            "tool_input": {"pattern": "password", "path": str(дом), "output_mode": "content"},
        },
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


def test_glob_по_дому_с_pattern_deny(tmp_path):
    """Находка M-3, точная форма из ревью: `Glob` с `path` на весь дом и `pattern="**/*"` —
    перечисление состава дома целиком, включая `launcher.key`. Остальные тесты этого раздела не
    передают `pattern` вовсе — этот проверяет, что `deny` не зависит от его отсутствия."""
    дом = tmp_path / "odata1c"

    результат = _запустить(
        {"tool_name": "Glob", "tool_input": {"path": str(дом), "pattern": "**/*"}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


@pytest.mark.parametrize("тул", ["Grep", "Glob"])
def test_grep_glob_по_дому_целиком_deny(тул, tmp_path):
    """Находка M-3 (Ruling 72): один вызов `Grep`/`Glob` с путём на весь дом рекурсивно вычитывает
    все защищённые файлы разом — дешевле, чем допущенный спецификацией `python -c`. `path` = сам
    дом (без указания конкретного файла — базовое имя `odata1c` не входит в список защищённых
    файлов, старое правило 2 такой путь пропускало)."""
    дом = tmp_path / "odata1c"

    результат = _запустить(
        {"tool_name": тул, "tool_input": {"path": str(дом)}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


@pytest.mark.parametrize("тул", ["Grep", "Glob"])
@pytest.mark.parametrize("предок", ["сам_дом", ".claude", "домашний_каталог_пользователя"])
def test_grep_glob_по_предку_дома_deny(тул, предок, tmp_path):
    """Находка M-3 (Ruling 72): предки дома (`~`, `~/.claude`, сам дом) — тоже `deny`, поскольку
    поиск по ним рекурсивно заходит и в сам дом. Дом здесь не задан через `ODATA1C_HOME` — берётся
    дефолт `~/.claude/odata1c` от управляемых `HOME`/`USERPROFILE`, чтобы получить все три уровня
    предков одним и тем же способом."""
    окружение = dict(os.environ)
    for имя in _ПЕРЕМЕННЫЕ_КОДИРОВКИ:
        окружение.pop(имя, None)
    окружение.pop("ODATA1C_HOME", None)
    окружение["HOME"] = str(tmp_path)
    окружение["USERPROFILE"] = str(tmp_path)

    путь_по_уровню = {
        "сам_дом": tmp_path / ".claude" / "odata1c",
        ".claude": tmp_path / ".claude",
        "домашний_каталог_пользователя": tmp_path,
    }[предок]

    результат = _запустить(
        {"tool_name": тул, "tool_input": {"path": str(путь_по_уровню)}},
        env=окружение,
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


@pytest.mark.parametrize("тул", ["Grep", "Glob"])
@pytest.mark.parametrize("подкаталог", ["bases", "recipes", "logs"])
def test_grep_glob_по_разрешённому_подкаталогу_разрешён(тул, подкаталог, tmp_path):
    """Находка M-3, обратная сторона (Ruling 72): подкаталоги `bases/`, `recipes/`, `logs/`
    разрешены явно — искать и обходить их можно, деньга не по всему дому, а по документированному
    списку безопасных подкаталогов."""
    дом = tmp_path / "odata1c"

    результат = _запустить(
        {"tool_name": тул, "tool_input": {"path": str(дом / подкаталог)}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_read_по_дому_целиком_не_денится_новым_правилом(tmp_path):
    """Ruling 72 ограничивает новую директорийную проверку `Grep`/`Glob` — `Read` в матчере тоже
    есть, но принимает путь к ОДНОМУ файлу, а не к каталогу; путь на весь дом ему не запрещён этим
    новым правилом (базовое имя `odata1c` не входит в список защищённых файлов — и не должно,
    иначе `Read` пришлось бы разбирать как каталог, а не как файл)."""
    дом = tmp_path / "odata1c"

    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": str(дом)}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


# --- Ruling 86 (раунд 3, находка M2-2): Glob — поле pattern наравне с path --------------------


def test_glob_pattern_абсолютный_на_дом_deny(tmp_path):
    """Ruling 86: `pattern` — обязательный аргумент `Glob`, `path` — нет; абсолютный шаблон,
    указывающий в дом целиком, обходил правило 2/Ruling 72 полностью, потому что проверялся
    только `path` (проверено настоящим инструментом `Glob` в повторном ревью — 11 путей, включая
    `bases.yaml.bak-adr15`)."""
    дом = tmp_path / "odata1c"

    результат = _запустить(
        {"tool_name": "Glob", "tool_input": {"pattern": str(дом).replace("\\", "/") + "/*"}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


@pytest.mark.parametrize("хвост", ["**/*", "*.yaml*"])
def test_glob_pattern_абсолютный_рекурсивный_deny(хвост, tmp_path):
    """Ruling 86: та же проверка для `**/*` (рекурсивный обход всего дома) и `*.yaml*` (маска, с
    которой повторное ревью проверило настоящий инструмент)."""
    дом = tmp_path / "odata1c"

    результат = _запустить(
        {"tool_name": "Glob", "tool_input": {"pattern": str(дом).replace("\\", "/") + "/" + хвост}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


def test_glob_pattern_защищённое_имя_deny(tmp_path):
    """Ruling 86: шаблон, называющий защищённое имя напрямую (`<дом>/bases.yaml`, без единого
    символа шаблона) — тоже `deny`, тем же правилом, что и у Bash/PowerShell."""
    дом = tmp_path / "odata1c"

    результат = _запустить(
        {"tool_name": "Glob", "tool_input": {"pattern": str(дом / "bases.yaml")}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


def test_glob_pattern_абсолютный_разрешённый_подкаталог_разрешён(tmp_path):
    """Ruling 86, обратная сторона: абсолютный шаблон в разрешённый подкаталог (`recipes/`) без
    защищённого имени — каталог шаблона не совпадает ни с домом, ни с его предком."""
    дом = tmp_path / "odata1c"

    результат = _запустить(
        {
            "tool_name": "Glob",
            "tool_input": {"pattern": str(дом / "recipes").replace("\\", "/") + "/*.yaml"},
        },
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_glob_pattern_относительный_вне_дома_разрешён(tmp_path):
    """Ruling 86, буквальный текст решения контроллера: «относительный шаблон при `path` вне
    дома — разрешён»."""
    результат = _запустить(
        {
            "tool_name": "Glob",
            "tool_input": {"pattern": "*.yaml", "path": str(tmp_path / "проект")},
        },
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_glob_pattern_относительный_с_выходом_из_подкаталога_известное_ограничение(tmp_path):
    """Известное ограничение (раздел «Обходы...» докстроки скрипта и отчёт задачи, раунд 3):
    относительный шаблон (`../*.yaml`) при `path` = разрешённый подкаталог (`recipes`) заходит
    обратно в дом через `..`, не называя защищённое имя явно и не будучи абсолютным сам по себе —
    буквальный текст Ruling 86 эту комбинацию не покрывает (находка M2-2 повторного ревью,
    «попроще форма»). Тест фиксирует границу как утверждение о поведении, а не как случайно
    прошедшее молчание."""
    дом = tmp_path / "odata1c"

    результат = _запустить(
        {
            "tool_name": "Glob",
            "tool_input": {"pattern": "../*.yaml", "path": str(дом / "recipes")},
        },
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def _как_git_bash(путь: pathlib.Path) -> str:
    """Тот же путь, что и `путь`, но записанный так, как его отдаёт Git Bash на Windows —
    `/c/Users/...`, без буквы диска: `C:\\foo\\bar` → `/c/foo/bar`."""
    текст = str(путь)
    if len(текст) >= 2 and текст[1] == ":":
        буква = текст[0].lower()
        остаток = текст[2:].replace("\\", "/")
        return f"/{буква}{остаток}"
    return текст.replace("\\", "/")


def test_путь_posix_без_буквы_диска_совпадает_с_домом_deny(tmp_path):
    """Находка м-2 (ревью раунда 0 признано ненадёжным): прежняя защита этого случая держалась на
    признаке «`odata1c` и `.claude` среди частей пути» — эвристике, которую ревью раунда 1
    забраковало (она же и пропускала обходы, и ложно срабатывала). Замена — переписывание
    Git Bash-пути в путь с буквой диска (`_переписать_posix_диск`) ещё до сравнения с домом:
    здесь домашний каталог задан явно (`ODATA1C_HOME` = `дом`), а путь к тому же файлу написан в
    POSIX-стиле Git Bash, без буквы диска — deny обязан сработать через переписывание, а не через
    эвристику по частям пути, которой в коде раунда 1 больше нет."""
    дом = tmp_path / "odata1c"
    путь_windows = дом / "bases.yaml"
    путь_posix = _как_git_bash(путь_windows)

    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": путь_posix}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


def test_дефолтный_дом_защищён_даже_при_другом_odata1c_home_deny(tmp_path):
    """Находка м-2, защита «в оба конца» (Ruling: явный перечень домов = `ODATA1C_HOME` этой сессии
    И, отдельно, документированный дефолт `~/.claude/odata1c`): сессия настроена на посторонний
    `ODATA1C_HOME` (например, временный дом тестового прогона), но путь к НАСТОЯЩЕМУ дому владельца
    по умолчанию обязан остаться закрытым независимо от того, на какую базу сконфигурирована именно
    эта сессия хука."""
    окружение = dict(os.environ)
    for имя in _ПЕРЕМЕННЫЕ_КОДИРОВКИ:
        окружение.pop(имя, None)
    окружение["HOME"] = str(tmp_path)
    окружение["USERPROFILE"] = str(tmp_path)
    окружение["ODATA1C_HOME"] = str(tmp_path / "другой_настроенный_дом" / "odata1c")

    путь_к_дефолтному_дому = tmp_path / ".claude" / "odata1c" / "bases.yaml"

    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": str(путь_к_дефолтному_дому)}},
        env=окружение,
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


def test_путь_репозитория_src_odata1c_больше_не_ложное_срабатывание(tmp_path):
    """Находка м-2 (ложное срабатывание раунда 0): в этом же репозитории worktree-каталоги живут
    в `.claude/worktrees/...`, а пакет — в `src/odata1c/`, поэтому старый признак «`odata1c` и
    `.claude` среди частей пути» запрещал такой путь (`.../.claude/worktrees/<агент>/src/odata1c/
    bases.yaml`), хотя к дому шлюза это отношения не имеет. Ни настроенный `ODATA1C_HOME`, ни
    дефолт под этим путём не лежат — раунд 1 обязан отвечать молчанием, а не `deny`."""
    окружение = dict(os.environ)
    for имя in _ПЕРЕМЕННЫЕ_КОДИРОВКИ:
        окружение.pop(имя, None)
    окружение["HOME"] = str(tmp_path / "чужой_дефолтный_дом")
    окружение["USERPROFILE"] = str(tmp_path / "чужой_дефолтный_дом")
    окружение["ODATA1C_HOME"] = str(tmp_path / "рабочий_дом" / "odata1c")

    путь_репозитория = (
        tmp_path
        / "репозиторий"
        / ".claude"
        / "worktrees"
        / "агент"
        / "src"
        / "odata1c"
        / "bases.yaml"
    )

    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": str(путь_репозитория)}},
        env=окружение,
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_временный_дом_приёмки_вне_claude_не_защищён_известное_ограничение(tmp_path):
    """Находка м-2, принятый остаточный пробел (задокументирован в design-doc §5): скрипты приёмки
    (`m2_live_check.py`, `contact_info_live_check.py`) поднимают временный дом шлюза вне `.claude`
    (например, `C:\\Temp\\...\\odata1c\\`), на который сессия хука не настроена через
    `ODATA1C_HOME` — хуку неоткуда узнать об этом доме. Тест фиксирует текущее (ограниченное)
    поведение, а не требует его починки: молчание здесь ожидаемо и осознанно, не регрессия."""
    окружение = dict(os.environ)
    for имя in _ПЕРЕМЕННЫЕ_КОДИРОВКИ:
        окружение.pop(имя, None)
    окружение["HOME"] = str(tmp_path / "рабочий_дефолтный_дом")
    окружение["USERPROFILE"] = str(tmp_path / "рабочий_дефолтный_дом")
    окружение["ODATA1C_HOME"] = str(tmp_path / "рабочий_дом" / "odata1c")

    путь_временного_дома_приёмки = tmp_path / "temp_приёмки" / "odata1c" / "bases.yaml"

    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": str(путь_временного_дома_приёмки)}},
        env=окружение,
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


# --- Правило 3: Bash / PowerShell ---------------------------------------------------------

_КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ = ["Bash", "PowerShell"]  # находка м-3 (Ruling 68): оба — поле command.


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
@pytest.mark.parametrize(
    "cmd",
    [
        "cat ~/.claude/odata1c/bases.yaml",
        "odata1c reveal '[[inn:1]]'",
        "uv run odata1c reveal x",
        "sqlite3 /home/u/.claude/odata1c/gate.sqlite .dump",
        "type C:\\Users\\u\\.claude\\odata1c\\daemon.yaml",
        # Раунд 1 больше не требует подстроки odata1c рядом с именем файла (Ruling 73) — имя
        # переменной окружения регистром или её наличием вообще ни на что не влияет.
        "cat $ODATA1C_HOME/bases.yaml",
    ],
)
def test_bash_deny(тул, cmd, tmp_path):
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": cmd}},
        env=_окружение(tmp_path),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
@pytest.mark.parametrize(
    "cmd",
    [
        "cat ~/.claude/odata1c/BASES.YAML",  # находка M-4: регистр имени файла (Ruling 73).
        'odata1c.exe reveal "[[inn:1]]"',  # находка M-4: суффикс .exe (Windows).
        "ODATA1C reveal x",  # находка M-4: регистр слова odata1c рядом с reveal.
        "python -m odata1c reveal x",  # находка M-4: запуск модулем, не консольным скриптом.
        # находка м-1: относительный путь после смены рабочего каталога.
        "cd ~/.claude/odata1c && cat bases.yaml",
        "cat gate.sqlite",  # находка м-1: имя без пути и без слова odata1c рядом вообще.
        "cat launcher.key",  # находка м-1, то же для launcher.key.
        # Ruling 73: принятое ложное срабатывание — grep ищет строку "bases.yaml" в чужом каталоге,
        # но deny всё равно срабатывает, поскольку правило больше не проверяет соседство с odata1c.
        "grep bases.yaml docs/",
    ],
)
def test_bash_deny_раунд_1(тул, cmd, tmp_path):
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": cmd}},
        env=_окружение(tmp_path),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
def test_bash_sqlite3_многострочный_deny(тул, tmp_path):
    """Находка м-4: `sqlite3` и имя файла на разных строках одной команды — `.` в исходном
    регулярном выражении не проходил через перевод строки без `re.DOTALL`."""
    cmd = "sqlite3 \\\n  gate.sqlite .dump"

    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": cmd}},
        env=_окружение(tmp_path),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
@pytest.mark.parametrize(
    "cmd",
    [
        "cat ~/.claude/odata1c/bases.yaml.bak-adr15",
        "type daemon.yaml.bak-before-ttl-1800",
        "cp gate.sqlite.bak-20260913-final.sqlite /tmp/x",
    ],
)
def test_bash_резервная_копия_deny(тул, cmd, tmp_path):
    """Ruling 85 (раунд 3): правило 3 уже ловило резервные копии защищённых файлов до этого
    раунда — граница слова (`\\b`) сразу после базового имени срабатывает на точке или дефисе
    хвоста (находка M2-1 повторного ревью проверила это экспериментально: «правило 3 эти же имена
    ловит»). Тест фиксирует поведение явно, отдельной проверкой, а не оставляет его непроверенным
    побочным эффектом общего списка."""
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": cmd}},
        env=_окружение(tmp_path),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
@pytest.mark.parametrize(
    "cmd",
    [
        "odata1c policy show trade_dev",
        "odata1c recipe check ut",
        "cat README.md",
        "grep gate_secret docs/",
    ],
)
def test_bash_разрешён(тул, cmd, tmp_path):
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": cmd}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


# --- Ruling 77 (раунд 2): шаблон оболочки в каталоге дома -------------------------------------


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
@pytest.mark.parametrize(
    "cmd",
    [
        "cat ~/.claude/odata1c/bases.y*",
        "cat ~/.claude/odata1c/*",
        "powershell -c 'gc $env:ODATA1C_HOME\\*'",
        "Get-Content $env:ODATA1C_HOME\\*",
        "type %ODATA1C_HOME%\\*.yaml",
        "cat ~\\.claude\\odata1c\\*",  # .claude\odata1c — обратные косые, не только прямые.
        "cat $ODATA1C_HOME/*",
        "cat ${ODATA1C_HOME}/*",
    ],
)
def test_bash_шаблон_в_доме_deny(тул, cmd, tmp_path):
    """Ruling 77 (раунд 2): было принятым ограничением (Ruling 73, «Не покрыто намеренно») —
    теперь закрыто отдельным условием правила 3. Путь дома шлюза (буквально `.claude/odata1c` в
    любом написании разделителя или любая форма подстановки `ODATA1C_HOME`) вместе с символом
    шаблона (`*` или `?`) в одной команде — `deny` с отдельной причиной «шаблон в каталоге шлюза
    может задеть файлы с паролями», а не с общей причиной остальных условий правила 3."""
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": cmd}},
        env=_окружение(tmp_path),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"
    assert решение["permissionDecisionReason"] == (
        "шаблон в каталоге шлюза может задеть файлы с паролями"
    )


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
def test_bash_шаблон_реальное_значение_переменной_deny(тул, tmp_path):
    """Ruling 77, третий случай упоминания дома: `ODATA1C_HOME` настроен на каталог, который не
    содержит буквально `.claude/odata1c` (например, временный дом приёмки) — команда, называющая
    этот путь напрямую (без символической ссылки на переменную) вместе с шаблоном, всё равно
    обязана денистить: хук знает фактическое значение `ODATA1C_HOME` этой сессии."""
    чужой_дом = tmp_path / "нестандартный_дом" / "odata1c"

    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": f"type {чужой_дом}\\*.yaml"}},
        env=_окружение(tmp_path, ODATA1C_HOME=str(чужой_дом)),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
def test_bash_путь_дома_без_шаблона_разрешён(тул, tmp_path):
    """Ruling 77: путь дома без символа шаблона и без запрещённого имени файла остаётся
    разрешённым — правило требует ОБА условия сразу (путь дома И шаблон), не одно из двух."""
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": "ls ~/.claude/odata1c/recipes/ut"}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
def test_bash_шаблон_без_упоминания_дома_разрешён(тул, tmp_path):
    """Ruling 77, обратная сторона: символ шаблона без всякого упоминания дома шлюза (ни
    `.claude/odata1c`, ни `ODATA1C_HOME`, ни фактического значения этой сессии) — правило не
    срабатывает, само по себе наличие `*`/`?` в команде ничего не значит."""
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": "ls /some/other/dir/*"}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


# --- Ruling 87 (раунд 3, находка M2-3): фигурные/квадратные скобки как символ шаблона ---------


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
def test_bash_раскрытие_фигурных_скобок_в_доме_deny(тул, tmp_path):
    """Ruling 87: `{bases,daemon}.yaml` — раскрытие скобок оболочки, тот же приём обхода, что и
    `bases.y*`, но Ruling 77 (раунд 2) проверяла только `*`/`?` и его не ловила."""
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": "cat ~/.claude/odata1c/{bases,daemon}.yaml"}},
        env=_окружение(tmp_path),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"
    assert решение["permissionDecisionReason"] == (
        "шаблон в каталоге шлюза может задеть файлы с паролями"
    )


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
def test_bash_квадратный_набор_символов_в_доме_deny(тул, tmp_path):
    """Ruling 87: `[bd]ases.yaml` — набор символов оболочки/глоба, тот же приём обхода."""
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": "cat ~/.claude/odata1c/[bd]ases.yaml"}},
        env=_окружение(tmp_path),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
def test_bash_фигурные_скобки_подстановки_переменной_разрешён(тул, tmp_path):
    """Ruling 87, ловушка наивной правки — находка M2-3, которую нашло повторное ревью:
    `${ODATA1C_HOME}` содержит `{`/`}` из-за самой подстановки переменной, без запятой внутри —
    ЭТО НЕ раскрытие скобок и должно остаться разрешено. Проверяет, что признак — запятая внутри
    скобок, а не одно присутствие `{`/`}`."""
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": "cat ${ODATA1C_HOME}/recipes/ut.yaml"}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


# --- Ruling 88 (раунд 3, находка M2-4): массовое чтение дома без символа шаблона ---------------


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
@pytest.mark.parametrize(
    "cmd",
    [
        "Get-ChildItem $env:ODATA1C_HOME | Get-Content",
        "Get-ChildItem ~\\.claude\\odata1c -Recurse | Get-Content",
        "cat ~/.claude/odata1c",
        "ls ~/.claude/odata1c",
        "ls ~/.claude/odata1c/",
        "ls $ODATA1C_HOME",
        "tar czf x.tgz ~/.claude/odata1c",
        "cp -r ~/.claude/odata1c /tmp/x",
    ],
)
def test_bash_массовое_чтение_дома_deny(тул, cmd, tmp_path):
    """Ruling 88 (раунд 3, находка M2-4): путь дома шлюза упомянут КАК КОРЕНЬ (не разрешённый
    подкаталог) вместе с глаголом чтения/перечисления/копирования/архивации — дешевле даже
    символа шаблона (Ruling 77/87). Часть форм здесь (`ls ~/.claude/odata1c/` без хвоста, `cp -r`,
    `tar czf`) раньше была принятым молчанием (её же З-1 повторного ревью прямо называет
    задокументированной границей, а не находкой) — Ruling 88 намеренно закрывает и её, не только
    PowerShell-конвейер из исходного текста находки M2-4: список глаголов, который дал контроллер,
    прямо включает копирование и архивацию."""
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": cmd}},
        env=_окружение(tmp_path),
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"
    assert решение["permissionDecisionReason"] == (
        "чтение каталога шлюза целиком может задеть файлы с паролями"
    )


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
@pytest.mark.parametrize(
    "cmd",
    [
        "ls ~/.claude/odata1c/recipes/ut",
        "cat ${ODATA1C_HOME}/recipes/ut.yaml",
        "Get-ChildItem $env:ODATA1C_HOME/bases",
        "cat ~/.claude/odata1c/logs/daemon.log",
    ],
)
def test_bash_массовое_чтение_разрешённого_подкаталога_разрешён(тул, cmd, tmp_path):
    """Ruling 88, обратная сторона: разрешённые подкаталоги (`recipes`, `bases`, `logs`) остаются
    разрешены даже с глаголом чтения/перечисления — правило денит только КОРЕНЬ дома, не сами эти
    подкаталоги."""
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": cmd}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
def test_bash_массовое_чтение_дефолтного_дома_deny_и_подкаталог_разрешён(тул, tmp_path):
    """Ruling 88, дефолтный дом (`ODATA1C_HOME` не задан вовсе): все остальные тесты этого раздела
    задают `ODATA1C_HOME` явно (через `_окружение`), поэтому `_домашние_формы` всегда добавляет
    ФАКТИЧЕСКОЕ значение переменной как отдельную альтернативу регулярного выражения — с
    дефолтным домом это не проверено ни разу, а `str(home)` в этом случае — путь через `~`,
    другая ветка сборки альтернатив. Проверяет обе стороны разом: подкаталог `recipes` остаётся
    разрешён, бare-каталог — `deny`."""
    окружение = dict(os.environ)
    for имя in _ПЕРЕМЕННЫЕ_КОДИРОВКИ:
        окружение.pop(имя, None)
    окружение.pop("ODATA1C_HOME", None)
    окружение["HOME"] = str(tmp_path)
    окружение["USERPROFILE"] = str(tmp_path)

    разрешено = _запустить(
        {"tool_name": тул, "tool_input": {"command": "ls ~/.claude/odata1c/recipes/ut"}},
        env=окружение,
    )
    assert разрешено.returncode == 0
    assert разрешено.stdout.strip() == ""

    denied = _запустить(
        {"tool_name": тул, "tool_input": {"command": "ls ~/.claude/odata1c/"}},
        env=окружение,
    )
    решение = _решение(denied)
    assert решение is not None
    assert решение["permissionDecision"] == "deny"


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
def test_bash_odata1c_doctor_разрешён(тул, tmp_path):
    """Ruling 88, пример из текста решения контроллера: `odata1c doctor` не упоминает путь дома
    ни в одной форме (это имя CLI-команды, а не `.claude/odata1c`/`ODATA1C_HOME`) — остаётся
    разрешён."""
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": "odata1c doctor"}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


@pytest.mark.parametrize("тул", _КОМАНДНЫЕ_ТУЛЫ_ДЛЯ_ТЕСТОВ)
def test_bash_echo_дома_без_глагола_разрешён(тул, tmp_path):
    """Ruling 88: `echo $ODATA1C_HOME` упоминает дом как корень, но `echo` не входит в список
    глаголов чтения/перечисления/копирования/архивации — печать самого пути, не его содержимого,
    остаётся разрешена."""
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": "echo $ODATA1C_HOME"}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


# --- Правило 4 и устойчивость ко входу -----------------------------------------------------


def test_битый_json_молчит_код_0(tmp_path):
    результат = _запустить_текст("это не json {{{", env=_окружение(tmp_path))

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_пустой_stdin_молчит_код_0(tmp_path):
    результат = _запустить_текст("", env=_окружение(tmp_path))

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_json_не_объект_молчит_код_0(tmp_path):
    результат = _запустить_текст("[1, 2, 3]", env=_окружение(tmp_path))

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_нет_полей_молчит(tmp_path):
    результат = _запустить({}, env=_окружение(tmp_path))

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_путь_с_null_байтом_не_роняет_хук(tmp_path):
    """`os.path.abspath` на строке со встроенным нулевым байтом бросает `ValueError` (это валидный
    JSON-вход: `\\u0000` — обычный код в строке JSON). Базовое имя ("bases.yaml") до нулевого байта
    не задето — проверка членства в списке защищённых файлов проходит, и нормализация пути
    действительно вызывается. Интерфейс задачи требует код 0 всегда: сбой внутри `decide` не
    должен превращаться в трассировку и код 1, только в «решения нет».

    Путь собирается через `chr(0)`, а не литеральным экранированием в исходнике: escape-код `\\0`
    в самом тексте файла — слишком лёгкая мишень для случайной порчи файла настоящим нулевым
    байтом при последующем редактировании (ровно так один раз и случилось при подготовке этого
    теста — `ast.parse` теста уже не проходил бы, файл содержал бы бинарный мусор)."""
    путь_с_null = "some" + chr(0) + "dir/bases.yaml"
    результат = _запустить(
        {"tool_name": "Read", "tool_input": {"file_path": путь_с_null}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_неизвестный_тул_молчит(tmp_path):
    результат = _запустить(
        {"tool_name": "odata1c_query", "tool_input": {"base": "trade_dev"}},
        env=_окружение(tmp_path),
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


# --- Скрипт — только стандартная библиотека --------------------------------------------------


def test_скрипт_только_стандартная_библиотека():
    import ast

    дерево = ast.parse(СКРИПТ.read_text(encoding="utf-8"), filename=str(СКРИПТ))
    имена_модулей: set[str] = set()
    for узел in ast.walk(дерево):
        if isinstance(узел, ast.Import):
            имена_модулей.update(псевдоним.name.split(".")[0] for псевдоним in узел.names)
        elif isinstance(узел, ast.ImportFrom) and узел.module:
            имена_модулей.add(узел.module.split(".")[0])

    assert имена_модулей, "скрипт должен хоть что-то импортировать"
    непозволенные = имена_модулей - set(sys.stdlib_module_names)
    assert непозволенные == set(), непозволенные


def test_скрипт_компилируется_без_syntaxwarning():
    """Раунд 2: неэкранированный `\\*`/`\\?` в докстроке (не raw-строке) — валидный Python, но
    `SyntaxWarning: invalid escape sequence` на стандартный поток ошибок; ни один из выбранных
    правил ruff (E/F/I/UP/B/SIM, см. `pyproject.toml`) это не ловит — W605 в набор не входит.
    Раунд 2 словил это только запуском скрипта напрямую (см. отчёт): предупреждение уходило в
    stderr не в UTF-8 на этой консоли Windows и валило `UnicodeDecodeError` у ЛЮБОГО подпроцессного
    теста этого файла, включая не связанные с раундом 2. Этот тест — механическая защита от
    повторения: компилирует файл с предупреждениями как ошибками."""
    import py_compile
    import tempfile
    import warnings

    # компиляция ниже — сама проверка; чтение файла нужно только чтобы тест не был пустым
    исходник = СКРИПТ.read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with tempfile.TemporaryDirectory() as врем:
            байткод = pathlib.Path(врем) / "pretooluse.pyc"
            py_compile.compile(str(СКРИПТ), cfile=str(байткод), doraise=True)
    assert исходник


# --- Правило 4: команды владельца, снижающие защиту → ask (ADR-0017) ------------------------

_СНИЖАЮЩИЕ = [
    "odata1c base set ut --gate off",
    "odata1c base set ut --gate identifiers",
    "odata1c base set ut --gate=default",
    "odata1c base set ut --write on",
    "odata1c base set ut --write default",
    "odata1c base set ut --role dev",
    "odata1c base set ut --role test",
    "odata1c base set ut --post-documents on",
    "odata1c base set ut --mark-deletion default",
    "odata1c base set ut --register-delete on",
    "odata1c base set ut --commit-limit 0",
    "odata1c base set ut --commit-limit 100",
    "odata1c base set ut --label 'Боевая' --write on",
    "uvx --from odata1c-gate odata1c base set ut --gate off",
    "uv run odata1c base set --home C:/дом ut --gate off",
    "odata1c.exe base set ut --gate off",
    'odata1c base set ut --label "незакрытая кавычка --write off',
]

_НЕ_СНИЖАЮЩИЕ = [
    "odata1c base set ut --gate identifiers+names",
    "odata1c base set ut --write off",
    "odata1c base set ut --role prod",
    "odata1c base set ut --post-documents off --mark-deletion off --register-delete off",
    "odata1c base set ut --label 'УТ 11, боевая'",
    "odata1c base set ut --label 'Включить --write on не надо'",
    "odata1c base list",
    "odata1c base test ut",
    "odata1c policy hide ut Catalog_Пользователи --yes",
    "odata1c policy set ut Catalog_Контрагенты.ИНН inn",
    "odata1c policy show ut",
    "odata1c policy check ut",
]


@pytest.mark.parametrize("тул", ["Bash", "PowerShell"])
@pytest.mark.parametrize("команда", _СНИЖАЮЩИЕ)
def test_base_set_снижающий_защиту_даёт_ask(тул, команда, tmp_path):
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": команда}}, env=_окружение(tmp_path)
    )

    решение = _решение(результат)
    assert решение is not None
    assert решение["permissionDecision"] == "ask"
    assert "снижает защиту базы" in решение["permissionDecisionReason"]
    assert "подтвердите" in решение["permissionDecisionReason"]


@pytest.mark.parametrize("тул", ["Bash", "PowerShell"])
@pytest.mark.parametrize("команда", _НЕ_СНИЖАЮЩИЕ)
def test_команды_владельца_без_снижения_молчат(тул, команда, tmp_path):
    результат = _запустить(
        {"tool_name": тул, "tool_input": {"command": команда}}, env=_окружение(tmp_path)
    )

    assert результат.returncode == 0
    assert результат.stdout.strip() == ""


def test_base_set_причина_называет_базу_и_ключи(tmp_path):
    результат = _запустить(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "odata1c base set trade_dev --gate off --write on"},
        },
        env=_окружение(tmp_path),
    )

    причина = _решение(результат)["permissionDecisionReason"]
    assert "базы trade_dev" in причина
    assert "--gate off" in причина and "--write on" in причина


def test_base_set_в_цепочке_после_другой_команды(tmp_path):
    """Review Focus 4: снижающая команда не первая в цепочке."""
    команда = "odata1c base list && odata1c base set ut --gate off; echo готово"

    результат = _запустить(
        {"tool_name": "Bash", "tool_input": {"command": команда}}, env=_окружение(tmp_path)
    )

    assert _решение(результат)["permissionDecision"] == "ask"


def test_base_set_в_цепочке_с_повышающей_командой_молчит(tmp_path):
    команда = "odata1c base set ut --write off && odata1c base set bp --gate identifiers+names"

    результат = _запустить(
        {"tool_name": "Bash", "tool_input": {"command": команда}}, env=_окружение(tmp_path)
    )

    assert результат.stdout.strip() == ""


def test_policy_open_даёт_ask(tmp_path):
    результат = _запустить(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": "uvx --from odata1c-gate odata1c policy open ut Catalog_Контрагенты.ИНН"
            },
        },
        env=_окружение(tmp_path),
    )

    решение = _решение(результат)
    assert решение["permissionDecision"] == "ask"
    assert "открывает поле" in решение["permissionDecisionReason"]


def test_reveal_старше_правила_4(tmp_path):
    """Правило 3 (`deny`) по-прежнему первое: `reveal` в одной команде с `base set` — deny."""
    результат = _запустить(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "odata1c reveal x && odata1c base set ut --gate off"},
        },
        env=_окружение(tmp_path),
    )

    assert _решение(результат)["permissionDecision"] == "deny"
