"""Приёмка правил гейта для БП (ADR-0016, Ruling 109–112) на живой базе 1С — только чтение.

Скрипт разговаривает со шлюзом так, как Claude Code: настоящий лаунчер `odata1c mcp` (stdio MCP)
поднимает свой демон. Демон и всё его состояние живут во ВРЕМЕННОМ домашнем каталоге на порту
7197: рабочий дом владельца (его `bases.yaml`, словарь, политика, демон на 7171) не трогается.
Во временный дом копируются запись базы, индекс метаданных и `policy.yaml`; авторазметка
(`policy.auto.yaml`) пересобирается кодом этой рабочей копии — так, как её соберёт реиндекс
владельца после обновления. Словарь пустой, секрет свой.

Что меряется — по ответу тула, то есть по тому, что увидела бы модель:

* оракул — реальные значения защищаемых полей, взятые из 1С напрямую тем же запросом, не
  встречаются в ответе шлюза нигде (значение от четырёх символов, подстрокой);
* открытые формы — в защищаемых полях вне токенов не осталось двух и более цифр (номера) или
  букв (ФИО, названия, адреса);
* инвариант 6 — незащищаемые поля (даты, номера документов, суммы) совпадают с сырым ответом.

Проверяются сущности, названные в проекте, и все сущности БП, в которых правка поменяла класс
хотя бы одного поля на защищаемый (сверка с эталоном `tools/probes/class_diff.py`).

Что печатается: счётчики, имена сущностей и полей, коды ошибок. Реальных значений — нет; пароль
читается из рабочего `bases.yaml` и в вывод не попадает.

    uv run python tools/probes/bp_live_check.py --out build/bp-live.json
    uv run python tools/probes/bp_live_check.py --classes-before build/classes-before.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

import httpx
import yaml
from mcp import StdioServerParameters, stdio_client
from mcp.client.session import ClientSession

БАЗА = "bp_test"
ПОРТ = 7197
ТАЙМАУТ = 240.0
ВСЕГО = 60
КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
ДАМП = КОРЕНЬ / "tests/fixtures/edmx/bp.full.edmx"

ТОКЕН = re.compile(r"\[\[[a-z][a-z0-9_:]*:[0-9A-Z]{1,16}\]\]")
ЦИФРЫ = re.compile(r"\d.*\d")
БУКВЫ = re.compile(r"[A-Za-zА-Яа-яЁё]{2,}")
ЦИФРОВЫЕ = {"inn", "kpp", "ogrn", "acc", "corr", "bic", "snils", "sfr", "card", "phone", "doc"}

# Сущности и поля, названные в проекте и отчёте разведки: (сущность, защищаемые, открытые).
ПРОВЕРКИ: list[tuple[str, list[str], list[str]]] = [
    (
        "InformationRegister_ДокументыФизическихЛиц",
        [
            "Серия",
            "Номер",
            "КемВыдан",
            "КодПодразделения",
            "Представление",
            "ФамилияЛатиницей",
            "ИмяЛатиницей",
        ],
        ["Period", "ДатаВыдачи", "СрокДействия"],
    ),
    (
        "Catalog_ФизическиеЛица",
        [
            "Description",
            "Фамилия",
            "Имя",
            "Отчество",
            "ИНН",
            "СтраховойНомерПФР",
            "МестоРождения",
            "МестоРожденияПредставление",
        ],
        # `ДатаРождения` — класс dob, защищаемый: в «открытые» (инвариант 6) не входит.
        ["Code"],
    ),
    ("Catalog_Сотрудники", ["Description"], ["Code"]),
    (
        "Catalog_РодственникиФизическихЛиц",
        ["Description", "Фамилия", "Имя", "Отчество", "СНИЛС"],
        ["Code"],
    ),
    (
        "Catalog_Организации",
        [
            "РегистрационныйНомерПФР",
            "РегистрационныйНомерФСС",
            "РегистрационныйНомерСФР",
            "ИНН",
            "КПП",
            "ОГРН",
        ],
        ["Code"],
    ),
    (
        "Document_СведенияОЗастрахованныхЛицахСЗВ_М_Сотрудники",
        ["Фамилия", "Имя", "Отчество", "ИНН", "СтраховойНомерПФР"],
        ["LineNumber"],
    ),
    (
        "InformationRegister_ЛицевыеСчетаСотрудниковПоЗарплатнымПроектам",
        ["НомерЛицевогоСчета"],
        ["Period"],
    ),
]


def форма(текст: str) -> str:
    return re.sub(r"[A-Za-zА-Яа-яЁё]", "a", re.sub(r"\d", "9", текст))


def без_токенов(текст: str) -> str:
    return ТОКЕН.sub(" ", текст)


def поля_дампа() -> dict[str, dict[str, str]]:
    from odata1c.index.edmx import parse_edmx

    return {
        с.name: {п.name: п.edm_type for п in с.fields}
        for с in parse_edmx(ДАМП.read_bytes()).entities
        if not с.is_virtual
    }


def изменённые(было_путь: pathlib.Path) -> list[tuple[str, list[str], list[str]]]:
    """Сущности БП, где правка дала полю защищаемый класс: `(сущность, поля, [])`."""
    from odata1c.gate.field_rules import classify_field

    было = json.loads(было_путь.read_text(encoding="utf-8"))["bp"]
    по_сущности: dict[str, list[str]] = {}
    for сущность, поля in поля_дампа().items():
        for поле, тип in поля.items():
            стало = classify_field(сущность, поле, тип)
            if стало is None:
                continue
            if было.get(f"{сущность}.{поле}") != стало[0]:
                по_сущности.setdefault(сущность, []).append(поле)
    return [(с, sorted(п), []) for с, п in sorted(по_сущности.items())]


def собрать_дом(рабочий: pathlib.Path) -> pathlib.Path:
    дом = pathlib.Path(tempfile.mkdtemp(prefix="odata1c-bp-"))
    for имя in ("bases", "logs"):
        (дом / имя).mkdir()
    настройки = yaml.safe_load((рабочий / "bases.yaml").read_text(encoding="utf-8"))
    запись = dict(настройки["bases"][БАЗА])
    запись["write"] = False
    (дом / "bases.yaml").write_text(
        yaml.safe_dump({"default": БАЗА, "bases": {БАЗА: запись}}, allow_unicode=True),
        encoding="utf-8",
    )
    (дом / "daemon.yaml").write_text(
        f'port: {ПОРТ}\nreindex_check_hours: 0\ngate_secret: "{secrets.token_hex(32)}"\n',
        encoding="utf-8",
    )
    исходный, каталог = рабочий / "bases" / БАЗА, дом / "bases" / БАЗА
    каталог.mkdir()
    for имя in ("metadata.sqlite", "policy.yaml"):
        if (исходный / имя).exists():
            shutil.copy2(исходный / имя, каталог / имя)
    from odata1c.config.loader import load_config
    from odata1c.gate.service import refresh_policy

    refresh_policy(дом, load_config(дом).bases[БАЗА])
    return дом


def остановить_демон(дом: pathlib.Path) -> None:
    subprocess.run(  # noqa: S603 — фиксированная команда собственного пакета
        [sys.executable, "-m", "odata1c", "daemon", "stop", "--home", str(дом)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        check=False,
    )
    time.sleep(1.5)
    if sys.platform == "win32":
        # Лаунчер на Windows поднимает демон планировщиком заданий, а если тот не ответил за
        # отведённое время — ещё и CreateProcess; опоздавший первый остаётся жить с удалённым
        # временным домом и держит порт следующему прогону. Добиваются процессы, в командной
        # строке которых стоит путь этого временного дома, — и только они.
        шаблон = str(дом).replace("'", "''")
        subprocess.run(  # noqa: S603 — фиксированная команда, путь временного дома
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
                f"'*{шаблон}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}",
            ],
            capture_output=True,
            check=False,
        )


@contextlib.asynccontextmanager
async def сессия(дом: pathlib.Path):
    параметры = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odata1c", "mcp", "--home", str(дом)],
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    async with stdio_client(параметры) as (чтение, запись), ClientSession(чтение, запись) as сеанс:
        await сеанс.initialize()
        yield сеанс


async def тул(сеанс: ClientSession, имя: str, аргументы: dict) -> tuple[dict, str]:
    ответ = await сеанс.call_tool(имя, аргументы, read_timeout_seconds=ТАЙМАУТ)
    текст = "\n".join(ч.text for ч in ответ.content if getattr(ч, "text", None))
    try:
        return json.loads(текст), текст
    except ValueError:
        return {"error": {"code": "not_json"}}, текст


def сырые(клиент: httpx.Client, адрес: str, сущность: str, поля: list[str]) -> list[dict] | None:
    ответ = клиент.get(
        адрес + сущность,
        params={"$format": "json", "$select": ",".join(поля), "$top": str(ВСЕГО)},
    )
    if ответ.status_code != 200:
        return None
    return ответ.json().get("value", [])


def встречается(значение: str, текст: str) -> bool:
    """Реальное значение от четырёх символов в тексте ответа тула — как есть или в записи JSON
    (кавычки и обратная косая черта в ответе экранированы: `ООО "Ромашка"` там записано как
    `ООО \\"Ромашка\\"`, и поиск как есть такое название пропустил бы)."""
    значение = значение.strip()
    if len(значение) < 4:
        return False
    в_json = json.dumps(значение, ensure_ascii=False)[1:-1]
    return значение in текст or в_json in текст


def значения(записи: list[dict], поле: str) -> list[str]:
    return [з[поле] for з in записи if isinstance(з.get(поле), str) and з[поле].strip()]


async def проверить(сеанс, клиент, адрес, сущность, защищаемые, открытые, поля) -> dict:
    есть = поля.get(сущность, {})
    защищаемые = [п for п in защищаемые if п in есть]
    открытые = [п for п in открытые if п in есть]
    выборка = защищаемые + открытые
    итог: dict = {"сущность": сущность, "поля": защищаемые}
    if not защищаемые:
        итог["пропуск"] = "полей нет в сущности"
        return итог
    сырой = сырые(клиент, адрес, сущность, выборка)
    if сырой is None:
        итог["пропуск"] = "1С не отдала сущность напрямую"
        return итог
    итог["строк_1с"] = len(сырой)
    if not сырой:
        return итог
    # Шлюз отвечает страницами (`has_more`, `next_skip`): читаются все, пока не наберётся
    # столько же строк, сколько отдала 1С. Текст ответов склеивается — оракул ищет в нём.
    записи: list[dict] = []
    тексты: list[str] = []
    замаскированные: set[str] = set()
    пропуск = 0
    while len(записи) < len(сырой):
        аргументы = {"entity": сущность, "select": выборка, "top": len(сырой) - len(записи)}
        if пропуск:
            аргументы["skip"] = пропуск
        ответ, кусок = await тул(сеанс, "odata1c_query", аргументы)
        if "error" in ответ:
            итог["ошибка_шлюза"] = ответ["error"].get("code")
            return итог
        записи += ответ.get("items", [])
        тексты.append(кусок)
        замаскированные |= set(ответ.get("masked_fields") or [])
        if not ответ.get("has_more"):
            break
        пропуск = ответ.get("next_skip") or len(записи)
    текст = "\n".join(тексты)
    итог["строк_шлюза"] = len(записи)
    итог["masked_fields"] = sorted(замаскированные)
    утечки: dict[str, int] = {}
    открытые_формы: dict[str, list[str]] = {}
    непустых: dict[str, int] = {}
    for поле in защищаемые:
        реальные = значения(сырой, поле)
        непустых[поле] = len(реальные)
        найдено = sum(1 for в in set(реальные) if встречается(в, текст))
        if найдено:
            утечки[поле] = найдено
        формы = []
        for в in значения(записи, поле):
            остаток = без_токенов(в)
            if ЦИФРЫ.search(остаток) or БУКВЫ.search(остаток):
                формы.append(форма(остаток.strip())[:24])
        if формы:
            открытые_формы[поле] = sorted(set(формы))[:5]
    итог["непустых_в_1с"] = непустых
    итог["утечки_оракула"] = утечки
    итог["открытые_формы"] = открытые_формы
    совпало = {}
    for поле in открытые:
        совпало[поле] = sorted(map(str, (з.get(поле) for з in сырой))) == sorted(
            map(str, (з.get(поле) for з in записи))
        )
    итог["инвариант_6"] = совпало
    return итог


async def main() -> int:
    разбор = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    разбор.add_argument("--home", default=str(pathlib.Path.home() / ".claude" / "odata1c"))
    разбор.add_argument("--out", type=pathlib.Path)
    разбор.add_argument(
        "--classes-before",
        type=pathlib.Path,
        help="эталон class_diff.py до правок — добавить все сущности с новым защищаемым классом",
    )
    разбор.add_argument(
        "--checks-from",
        type=pathlib.Path,
        help="итог прежнего прогона (--out): проверить те же сущности и поля — положительный "
        "контроль на коде без правок, который сам изменённых сущностей не найдёт",
    )
    аргументы = разбор.parse_args()

    рабочий = pathlib.Path(аргументы.home)
    настройки = yaml.safe_load((рабочий / "bases.yaml").read_text(encoding="utf-8"))["bases"][БАЗА]
    адрес = настройки["url"].rstrip("/") + "/"
    клиент = httpx.Client(auth=(настройки["user"], настройки["password"]), timeout=120)
    поля = поля_дампа()
    проверки = list(ПРОВЕРКИ)
    if аргументы.classes_before:
        названные = {с for с, _, _ in проверки}
        проверки += [п for п in изменённые(аргументы.classes_before) if п[0] not in названные]
    if аргументы.checks_from:
        прежний = json.loads(аргументы.checks_from.read_text(encoding="utf-8"))["проверки"]
        открытые_по = {с: о for с, _, о in ПРОВЕРКИ}
        проверки = [(и["сущность"], и["поля"], открытые_по.get(и["сущность"], [])) for и in прежний]

    дом = собрать_дом(рабочий)
    print(f"временный дом создан, порт демона {ПОРТ}, проверок {len(проверки)}")
    итоги: list[dict] = []
    try:
        async with сессия(дом) as сеанс:
            for сущность, защищаемые, открытые in проверки:
                итог = await проверить(сеанс, клиент, адрес, сущность, защищаемые, открытые, поля)
                итоги.append(итог)
                if итог.get("строк_1с"):
                    print(
                        f"{сущность}: строк {итог.get('строк_шлюза', 0)}, утечек "
                        f"{sum(итог.get('утечки_оракула', {}).values())}, открытых форм "
                        f"{len(итог.get('открытые_формы', {}))}"
                        + (f", ошибка {итог['ошибка_шлюза']}" if "ошибка_шлюза" in итог else "")
                    )
    finally:
        остановить_демон(дом)
        клиент.close()
        shutil.rmtree(дом, ignore_errors=True)
        print(f"временный дом удалён: {not дом.exists()}")

    с_данными = [и for и in итоги if и.get("строк_шлюза")]
    свод = {
        "проверок": len(итоги),
        "с_данными": len(с_данными),
        "строк": sum(и["строк_шлюза"] for и in с_данными),
        "утечек_оракула": sum(sum(и.get("утечки_оракула", {}).values()) for и in с_данными),
        "полей_с_открытыми_формами": sum(len(и.get("открытые_формы", {})) for и in с_данными),
        "инвариант_6_расхождений": sum(
            1 for и in с_данными for ок in и.get("инвариант_6", {}).values() if not ок
        ),
        "ошибок_шлюза": sum(1 for и in итоги if "ошибка_шлюза" in и),
    }
    print(json.dumps(свод, ensure_ascii=False))
    if аргументы.out:
        аргументы.out.write_text(
            json.dumps({"свод": свод, "проверки": итоги}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
