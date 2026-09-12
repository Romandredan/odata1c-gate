"""Проба P8 (план M2, задача 1): формы записи OData 1С на живой базе.

Отвечает фактами на вопросы, от которых зависят задачи 5–7 плана M2: какой справочник и какой
документ принимают создание с минимумом полей, точная форма URL проведения, частичный PATCH,
`DataVersion` и оптимистическая блокировка, пометка удаления, ответ POST и эхо переданного
значения в ошибках записи.

Запуск из корня репозитория (консоль Windows — cp1251, поэтому `PYTHONUTF8=1`):

    PYTHONUTF8=1 uv run python tools/probes/p8_write_probe.py run       # вопросы 1–7 и уборка
    PYTHONUTF8=1 uv run python tools/probes/p8_write_probe.py cleanup   # только уборка по журналу

Правила живой базы (бриф задачи 1):
- адрес и учётные данные — из рабочего `bases.yaml` (`~/.claude/odata1c/bases.yaml`, запись
  `trade_dev`); в вывод они не попадают: всё печатаемое проходит через `Скрыватель`;
- проба создаёт и меняет только свои объекты: у каждого в `Description` (у документа — в
  `Комментарий`) маркер `odata1c-приёмка P8 <дата>`; существующие объекты только читаются —
  ради ссылок для обязательных реквизитов (организация, склад, номенклатура);
- каждый созданный объект сразу же, до следующего запроса, дописывается в журнал созданного
  (JSON-файл во временном каталоге ОС): оборванный прогон не теряет список, и `cleanup`
  доубирает по нему;
- уборка: проведённые документы `Unpost`, всем созданным `DeletionMark=true`; физического
  удаления нет (инвариант 3 проекта);
- реальные значения защищаемых классов (ИНН, КПП, названия организаций, ФИО, телефоны,
  адреса) не печатаются: `Скрыватель` вырезает известные значения, прочитанные напрямую, и
  серии цифр формы ИНН/КПП/счёта. GUID, номера, даты, суммы не защищаются и печатаются.

Кодирование — как в `Client1C`: путь через `_экранировать_путь`, строка запроса через
`_собрать_запрос` (пробел — `%20`, не `+`), тело ответа — UTF-8 из байтов, а не по заголовку.
Эхо проверяется меткой, как в пробе P7: в запрос подставляется заведомо чужое базе значение
`ZZP8…`, в ответе ищется сама метка.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import pathlib
import re
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx
import yaml

from odata1c.client1c.client import _собрать_запрос, _экранировать_путь

БАЗА = "trade_dev"
СЕГОДНЯ = datetime.date.today().isoformat()
МАРКЕР = f"odata1c-приёмка P8 {СЕГОДНЯ}"
ЖУРНАЛ_СОЗДАННОГО = pathlib.Path(tempfile.gettempdir()) / "odata1c-p8-created.json"

# Первый запрос после простоя публикации идёт 10–15 с, проведение документа — ещё дольше.
ТАЙМАУТ_С = 180.0

# Серии цифр формы ИНН (10/12), КПП (9), счёта (20) и телефона (10–11) — вырезаются из вывода
# независимо от того, знает ли их скрипт. Границы не пускают внутрь GUID и номеров документов:
# там цифры соседствуют с буквами и дефисами.
СЕРИЯ_ЦИФР = re.compile(r"(?<![\w-])\d{9,20}(?![\w-])")


class Скрыватель:
    """Вырезает учётные данные и известные реальные значения защищаемых классов из вывода."""

    def __init__(self) -> None:
        self._секреты: set[str] = set()

    def добавить(self, значения: Iterable[Any]) -> None:
        for значение in значения:
            if isinstance(значение, str) and len(значение.strip()) >= 3:
                self._секреты.add(значение.strip())

    def __call__(self, текст: str) -> str:
        # Сначала длинные: адрес целиком должен исчезнуть раньше, чем имя хоста внутри него.
        for секрет in sorted(self._секреты, key=len, reverse=True):
            текст = текст.replace(секрет, "<скрыто>")
        return СЕРИЯ_ЦИФР.sub("<цифры>", текст)


СКРЫТЬ = Скрыватель()


def вывод(*части: Any) -> None:
    print(СКРЫТЬ(" ".join(str(ч) for ч in части)), flush=True)


@dataclass
class Ответ:
    метод: str
    цель: str
    статус: int
    текст: str
    секунды: float
    заголовки: dict[str, str] = field(default_factory=dict)

    @property
    def данные(self) -> Any:
        try:
            return json.loads(self.текст) if self.текст else None
        except ValueError:
            return None

    @property
    def ошибка(self) -> str:
        """Текст ошибки 1С из тела `odata.error`, иначе начало тела."""
        данные = self.данные
        if isinstance(данные, dict) and "odata.error" in данные:
            сообщение = данные["odata.error"].get("message", {})
            код = данные["odata.error"].get("code", "")
            return f"[код {код}] {сообщение.get('value', сообщение)}"
        return self.текст[:500]

    def кратко(self) -> str:
        тело = self.ошибка if self.статус >= 400 else self.текст[:300]
        return f"{self.метод} {self.цель} → HTTP {self.статус} за {self.секунды:.1f} с; {тело}"


class Публикация:
    """Прямые запросы к OData 1С тем же кодированием, что у `Client1C`."""

    def __init__(self, запись: dict) -> None:
        # Учётные данные — только в заголовок; httpx кодирует базовую аутентификацию в UTF-8,
        # как того требует публикация 1С за IIS (кириллица в имени пользователя).
        СКРЫТЬ.добавить([запись["url"], запись["user"], запись["password"]])
        хост = httpx.URL(запись["url"]).host
        СКРЫТЬ.добавить([хост])
        self._http = httpx.Client(
            base_url=запись["url"],
            auth=(запись["user"], запись["password"]),
            timeout=ТАЙМАУТ_С,
            headers={"Accept": "application/json"},
        )
        self._сеанс_открыт = False

    def close(self) -> None:
        if self._сеанс_открыт:
            with contextlib.suppress(httpx.HTTPError):
                self._http.get("", headers={"IBSession": "finish"})
        self._http.close()

    def запрос(
        self,
        метод: str,
        путь: str,
        *,
        params: dict | None = None,
        тело: Any = None,
        сырое_тело: bytes | None = None,
        заголовки: dict | None = None,
        формат: bool = True,
    ) -> Ответ:
        params = dict(params or {})
        if формат:
            params.setdefault("$format", "json")
        строка = _собрать_запрос(params)
        цель = _экранировать_путь(путь) + (f"?{строка}" if строка else "")
        заголовки = dict(заголовки or {})
        if not self._сеанс_открыт:
            заголовки["IBSession"] = "start"
            self._сеанс_открыт = True
        аргументы: dict[str, Any] = {}
        if сырое_тело is not None:
            аргументы["content"] = сырое_тело
            заголовки.setdefault("Content-Type", "application/json")
        elif тело is not None:
            аргументы["json"] = тело
        начало = time.monotonic()
        ответ = self._http.request(метод, цель, headers=заголовки, **аргументы)
        return Ответ(
            метод=метод,
            цель=urllib_unquote(цель),
            статус=ответ.status_code,
            текст=ответ.content.decode("utf-8", errors="replace"),
            секунды=time.monotonic() - начало,
            заголовки={
                k: v
                for k, v in ответ.headers.items()
                if k.lower() in ("etag", "location", "content-type", "dataserviceversion")
            },
        )

    # Короткие формы -------------------------------------------------------------------------

    def get(self, путь: str, **params: str) -> Ответ:
        return self.запрос("GET", путь, params=params)

    def post(self, путь: str, тело: Any, **kw: Any) -> Ответ:
        return self.запрос("POST", путь, тело=тело, **kw)

    def patch(self, путь: str, тело: Any, **kw: Any) -> Ответ:
        return self.запрос("PATCH", путь, тело=тело, **kw)


def urllib_unquote(цель: str) -> str:
    """Цель запроса читаемой — для вывода и отчёта; в сеть уходит закодированная."""
    from urllib.parse import unquote

    return unquote(цель)


def ключ(набор: str, ref: str) -> str:
    return f"{набор}(guid'{ref}')"


# Журнал созданного --------------------------------------------------------------------------


def прочитать_журнал() -> list[dict]:
    if ЖУРНАЛ_СОЗДАННОГО.exists():
        return json.loads(ЖУРНАЛ_СОЗДАННОГО.read_text(encoding="utf-8"))
    return []


def записать_в_журнал(набор: str, ref: str, что: str) -> None:
    """Дописать созданный объект в журнал немедленно — до следующего запроса к 1С."""
    журнал = прочитать_журнал()
    if not any(з["ref"] == ref for з in журнал):
        журнал.append({"набор": набор, "ref": ref, "что": что, "создан": time.strftime("%H:%M:%S")})
        ЖУРНАЛ_СОЗДАННОГО.write_text(
            json.dumps(журнал, ensure_ascii=False, indent=1), encoding="utf-8"
        )


def создать(п: Публикация, набор: str, тело: dict, что: str) -> Ответ:
    """POST с записью в журнал: всё, что 1С приняла, попадает в список уборки."""
    ответ = п.post(набор, тело)
    данные = ответ.данные
    if ответ.статус < 300:
        if isinstance(данные, dict) and данные.get("Ref_Key"):
            записать_в_журнал(набор, данные["Ref_Key"], что)
        else:
            # 1С приняла запись, а ссылки в ответе нет: объект есть, но в журнал не попал.
            # Его подберёт поиск по маркеру в уборке; здесь — громко, чтобы это было видно.
            вывод(
                f"!! {набор}: HTTP {ответ.статус} без Ref_Key в ответе — ищите по маркеру;",
                ответ.заголовки,
                ответ.текст[:300],
            )
    return ответ


def открыть_публикацию() -> Публикация:
    """Публикация с уже открытым сеансом 1С.

    Сеанс открывается пустым чтением: заголовок `IBSession: start` не должен ехать на запросе
    записи — иначе сбой открытия сеанса и сбой записи в выводе не различить.
    """
    путь = pathlib.Path.home() / ".claude" / "odata1c" / "bases.yaml"
    настройки = yaml.safe_load(путь.read_text(encoding="utf-8"))
    п = Публикация(настройки["bases"][БАЗА])
    первый = п.get("Catalog_Организации", **{"$top": "1", "$select": "Ref_Key"})
    вывод(f"сеанс 1С: первый запрос HTTP {первый.статус} за {первый.секунды:.1f} с")
    return п


# Уборка -------------------------------------------------------------------------------------


# Наборы, в которых проба создаёт объекты, и поле, где лежит маркер: у справочника —
# наименование, у документа — комментарий. По ним уборка ищет «потерянные» объекты.
НАБОРЫ_С_МАРКЕРОМ = {
    "Catalog_Номенклатура": "Description",
    "Catalog_ВидыКонтактнойИнформации": "Description",
    "Document_ПересчетТоваров": "Комментарий",
    "Document_ОприходованиеИзлишковТоваров": "Комментарий",
}


def поиск_по_маркеру(п: Публикация) -> None:
    """Дописать в журнал всё, что несёт маркер пробы, но в журнал не попало.

    Страховка от двух случаев: 1С приняла POST, а ссылки в ответе не оказалось; прогон оборвался
    между ответом 1С и записью журнала. Ищется общий префикс маркера без даты — так подбираются
    и остатки прогонов прошлых дней.
    """
    for набор, поле in НАБОРЫ_С_МАРКЕРОМ.items():
        найдено = п.get(
            набор,
            **{"$filter": f"substringof('odata1c-приёмка P8', {поле})", "$select": "Ref_Key"},
        )
        if найдено.статус != 200:
            вывод(f"  поиск по маркеру в {набор}: {найдено.кратко()[:300]}")
            continue
        for строка in найдено.данные.get("value", []):
            записать_в_журнал(набор, строка["Ref_Key"], "найден по маркеру")


def уборка(п: Публикация) -> list[dict]:
    """Проведённые — `Unpost`, всем — `DeletionMark=true`, затем проверка чтением."""
    поиск_по_маркеру(п)
    итог = []
    for запись in прочитать_журнал():
        набор, ref = запись["набор"], запись["ref"]
        путь = ключ(набор, ref)
        до = п.get(путь)
        состояние = до.данные if до.статус == 200 else {}
        if состояние.get("Posted"):
            вывод("  уборка Unpost:", п.запрос("POST", f"{путь}/Unpost").кратко())
        if not состояние.get("DeletionMark"):
            вывод("  уборка пометка:", п.patch(путь, {"DeletionMark": True}).кратко()[:200])
        после = п.get(путь).данные or {}
        строка = {
            **запись,
            "Posted": после.get("Posted"),
            "DeletionMark": после.get("DeletionMark"),
        }
        итог.append(строка)
        вывод(f"  {набор} {ref}: Posted={строка['Posted']} DeletionMark={строка['DeletionMark']}")
    return итог


def main() -> None:
    разбор = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    разбор.add_argument("фаза", choices=["run", "cleanup"])
    аргументы = разбор.parse_args()
    п = открыть_публикацию()
    try:
        if аргументы.фаза == "run":
            прогон(п)
        вывод("== уборка по журналу", ЖУРНАЛ_СОЗДАННОГО)
        итог = уборка(п)
        не_убрано = [з for з in итог if not з["DeletionMark"] or з["Posted"]]
        вывод(f"== создано {len(итог)}, не убрано {len(не_убрано)}")
        if не_убрано:
            sys.exit(2)
    finally:
        п.close()


def прогон(п: Публикация) -> None:  # заполняется ниже по вопросам
    raise NotImplementedError


if __name__ == "__main__":
    main()
