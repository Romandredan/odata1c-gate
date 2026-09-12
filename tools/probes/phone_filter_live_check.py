"""Отбор по токену телефона в колонке поиска контактной информации (Н-1, Ruling 38) на живой базе.

Только чтение: к 1С уходят одни GET — запросы `odata1c_query` через слой тулов и прямые GET
клиента 1С. Демон не поднимается (слой тулов работает в процессе), дом — ВРЕМЕННЫЙ: запись базы,
индекс и политика — копией из рабочего дома, словарь пустой, секрет свой; рабочий дом владельца,
его словарь и демон на 7171 не трогаются. Учётные данные читаются из `bases.yaml` и никуда не
выводятся.

Что печатается. Реальные значения — никогда: только счётчики и ФОРМЫ значений (цифра → 9,
буква → a).

Что меряется. Берутся строки `Catalog_Контрагенты_КонтактнаяИнформация` типа `Телефон`, токен
каждой — из `Представление` в ответе шлюза. Для каждой строки:

1. прямым чтением — находит ли её `НомерТелефона eq '<цифры представления>'` (так искал шлюз до
   написаний: нормализованной формой, у телефона — цифрами);
2. через шлюз — находит ли её `НомерТелефона eq '<токен>'`.

Для строк, которые не нашёл хотя бы один способ, печатаются формы представления, его цифр и
колонок поиска — видно, почему: колонка пуста или в представлении несколько номеров.

Запуск из корня репозитория:

    uv run python tools/probes/phone_filter_live_check.py            # 30 строк
    uv run python tools/probes/phone_filter_live_check.py 10
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import shutil
import sys
import tempfile

import yaml

from odata1c.cli import main as cli_main
from odata1c.config.loader import load_config
from odata1c.registry.registry import SessionScope
from odata1c.tools.service import ToolService

РАБОЧИЙ_ДОМ = pathlib.Path.home() / ".claude" / "odata1c"
БАЗА = "trade_dev"
КИ = "Catalog_Контрагенты_КонтактнаяИнформация"


def форма(значение) -> str:
    if not isinstance(значение, str):
        return type(значение).__name__
    return re.sub(r"[^\W\d_]", "a", re.sub(r"\d", "9", значение))


def подготовить(дом: pathlib.Path) -> None:
    cli_main(["init", "--home", str(дом)])
    запись = dict(
        yaml.safe_load((РАБОЧИЙ_ДОМ / "bases.yaml").read_text(encoding="utf-8"))["bases"][БАЗА]
    )
    запись["role"] = "prod"
    запись["gate"] = {"mode": "identifiers+names"}
    запись.pop("recipes", None)
    (дом / "bases.yaml").write_text(
        yaml.safe_dump({"default": БАЗА, "bases": {БАЗА: запись}}, allow_unicode=True),
        encoding="utf-8",
    )
    каталог = дом / "bases" / БАЗА
    каталог.mkdir(parents=True, exist_ok=True)
    for имя in ("metadata.sqlite", "metadata.edmx", "policy.yaml"):
        shutil.copy2(РАБОЧИЙ_ДОМ / "bases" / БАЗА / имя, каталог / имя)


async def запрос(сервис: ToolService, **параметры) -> dict:
    return json.loads(await сервис.query(SessionScope(), base=БАЗА, entity=КИ, **параметры))


async def проверить(дом: pathlib.Path, выборка: int) -> None:
    config = load_config(дом)
    сервис = ToolService(config)
    try:
        ответ = await запрос(
            сервис,
            select=["Ref_Key", "LineNumber", "Представление"],
            filter="Тип eq 'Телефон'",
            top=выборка,
        )
        строки = [
            r
            for r in ответ["items"]
            if isinstance(r.get("Представление"), str) and r["Представление"].startswith("[[phone:")
        ]
        клиент = сервис._client_for(config.bases[БАЗА])
        итог = {"строк": len(строки), "по_токену": 0, "нормализованной": 0, "ошибок": 0}
        первые = {"по_токену": 0, "нормализованной": 0}
        промахи: list[str] = []
        for номер, строка in enumerate(строки):
            ключ = f"Ref_Key eq guid'{строка['Ref_Key']}' and LineNumber eq {строка['LineNumber']}"
            сырая = await клиент.get(
                КИ,
                {"$filter": ключ, "$select": "Представление,НомерТелефона,НомерТелефонаБезКодов"},
            )
            исходная = (сырая.get("value") or [{}])[0]
            цифры = re.sub(r"\D", "", исходная.get("Представление") or "")
            по_цифрам = await клиент.get(
                КИ, {"$filter": f"{ключ} and НомерТелефона eq '{цифры}'", "$select": "Ref_Key"}
            )
            нормализованной = bool(по_цифрам.get("value"))
            через_шлюз = await запрос(
                сервис,
                select=["Ref_Key", "LineNumber"],
                filter=f"НомерТелефона eq '{строка['Представление']}'",
                top=50,
            )
            if "error" in через_шлюз:
                итог["ошибок"] += 1
                промахи.append(f"{номер}: ошибка шлюза {через_шлюз['error'].get('code')}")
                continue
            по_токену = any(
                r.get("Ref_Key") == строка["Ref_Key"]
                and r.get("LineNumber") == строка["LineNumber"]
                for r in через_шлюз["items"]
            )
            итог["нормализованной"] += нормализованной
            итог["по_токену"] += по_токену
            if номер < 10:
                первые["нормализованной"] += нормализованной
                первые["по_токену"] += по_токену
            if not (нормализованной and по_токену):
                колонка = исходная.get("НомерТелефона")
                промахи.append(
                    f"{номер}: нормализованной {'да' if нормализованной else 'нет'}, "
                    f"по токену {'да' if по_токену else 'нет'} | представление "
                    f"{форма(исходная.get('Представление'))} | цифры {форма(цифры)} | "
                    f"НомерТелефона {форма(колонка)} | "
                    f"БезКодов {форма(исходная.get('НомерТелефонаБезКодов'))}"
                )
        print("итог:", итог)
        print("из первых 10:", первые)
        for промах in промахи:
            print(промах)
    finally:
        await сервис.aclose()


def main() -> None:
    выборка = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    дом = pathlib.Path(tempfile.mkdtemp(prefix="odata1c-phone-"))
    try:
        подготовить(дом)
        asyncio.run(проверить(дом, выборка))
    finally:
        shutil.rmtree(дом, ignore_errors=True)


if __name__ == "__main__":
    main()
