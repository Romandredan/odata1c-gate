"""Проба: база, добавленная при работающем шлюзе, индексируется инструментом модели.

Вопрос владельца (2026-09-21): хватит ли пользователю одной команды `odata1c base add`, а
остальное — «построй индекс» в чате? Проверяется цепочка целиком на живой базе:

1. временный дом с ПУСТЫМ перечнем баз, свой демон (порт 7191) поднимает лаунчер;
2. `odata1c_bases` — баз нет;
3. запись базы дописывается в настройки ПРИ РАБОТАЮЩЕМ демоне (копия записи `trade_dev` под
   новым именем; индекса, политики и рецептов у неё нет вовсе);
4. `odata1c_bases` — база видна без перезапуска, индекса нет;
5. `odata1c_find_entity` до индекса — что отвечает шлюз;
6. `odata1c_reindex` — строит индекс; 7. `odata1c_find_entity` — находит сущности.

Рабочий дом владельца не трогается; пароль читается из его настроек этим скриптом и никуда не
печатается; печатаются только коды, счётчики и длительности.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import secrets
import shutil
import sys
import tempfile
import time

import yaml
from contact_info_live_check import БАЗА, ПОРТ, остановить_демон, сессия

from odata1c.config.home import resolve_home

НОВАЯ = "fresh_check"
ТАЙМАУТ_РЕИНДЕКСА = 1500.0


async def вызвать(сеанс, имя: str, аргументы: dict, таймаут: float = 240.0) -> dict:
    ответ = await сеанс.call_tool(имя, аргументы, read_timeout_seconds=таймаут)
    текст = "\n".join(ч.text for ч in ответ.content if getattr(ч, "text", None))
    try:
        return json.loads(текст)
    except ValueError:
        return {"error": {"code": "not_json"}}


def кратко(ответ: dict) -> str:
    if "error" in ответ:
        ошибка = ответ["error"]
        return f"отказ {ошибка.get('code')}"
    return "ок"


async def main() -> int:
    рабочий = resolve_home(None)
    настройки = yaml.safe_load((рабочий / "bases.yaml").read_text(encoding="utf-8"))
    запись = dict(настройки["bases"][БАЗА])
    запись["label"] = "проба свежей базы"
    дом = pathlib.Path(tempfile.mkdtemp(prefix="odata1c-fresh-"))
    (дом / "logs").mkdir()
    (дом / "bases.yaml").write_text("bases: {}\n", encoding="utf-8")
    (дом / "daemon.yaml").write_text(
        f'port: {ПОРТ}\nreindex_check_hours: 0\ngate_secret: "{secrets.token_hex(32)}"\n',
        encoding="utf-8",
    )
    итог = 0
    try:
        async with сессия(дом) as сеанс:
            базы = await вызвать(сеанс, "odata1c_bases", {})
            print(f"1. баз до добавления: {len(базы.get('bases', []))} ({кратко(базы)})")

            await asyncio.sleep(1.2)  # отметка времени файла обязана измениться
            (дом / "bases.yaml").write_text(
                yaml.safe_dump({"default": НОВАЯ, "bases": {НОВАЯ: запись}}, allow_unicode=True),
                encoding="utf-8",
            )
            базы = await вызвать(сеанс, "odata1c_bases", {})
            строка = next((б for б in базы.get("bases", []) if б.get("name") == НОВАЯ), None)
            print(
                f"2. после добавления при работающем демоне: видна={строка is not None}, "
                f"indexed={None if строка is None else строка.get('indexed')} ({кратко(базы)})"
            )
            if строка is None:
                return 1

            до = await вызвать(сеанс, "odata1c_find_entity", {"query": "контрагенты"})
            print(
                f"3. find_entity до индекса: {кратко(до)}; подсказка есть={'hint' in до.get('error', {})}"
            )

            начало = time.monotonic()
            реиндекс = await вызвать(сеанс, "odata1c_reindex", {"base": НОВАЯ}, ТАЙМАУТ_РЕИНДЕКСА)
            длилось = time.monotonic() - начало
            print(
                f"4. odata1c_reindex: {кратко(реиндекс)}, {длилось:.0f} с, "
                f"ключи ответа: {sorted(k for k in реиндекс if k != 'error')}"
            )

            после = await вызвать(сеанс, "odata1c_find_entity", {"query": "контрагенты"})
            print(
                f"5. find_entity после индекса: {кратко(после)}, найдено {len(после.get('entities', []))}"
            )
            базы = await вызвать(сеанс, "odata1c_bases", {})
            строка = next((б for б in базы.get("bases", []) if б.get("name") == НОВАЯ), {})
            print(
                f"6. bases: indexed={строка.get('indexed')}, сущностей={строка.get('entity_count')}"
            )
            созданы = sorted(п.name for п in (дом / "bases" / НОВАЯ).glob("*"))
            print(f"7. файлы базы во временном доме: {созданы}")
            if "error" in реиндекс or not после.get("entities"):
                итог = 1
    finally:
        остановить_демон(дом)
        shutil.rmtree(дом, ignore_errors=True)
        print(f"временный дом удалён: {not дом.exists()}")
    return итог


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
