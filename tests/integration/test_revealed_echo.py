"""Раскрытое гейтом значение не возвращается моделью через эхо выражения отбора (задача N1 M1d).

Отличие от юнит-проверок того же инварианта (`tests/unit/test_gate_revealed.py`,
`test_tools_service.py`): здесь настоящий HTTP и настоящий формат ошибки платформы — поддельная
1С (`fake_1c.py`) отвечает `odata.error`, который повторяет выражение отбора вместе с литералом.
Через транспорт проходит весь конвейер: клиент 1С, разбор ошибки (`client1c/errors.map_error`),
маскировка текста сообщения и страж.

Перебор по классам, а не по одному ИНН: у `addr`, `dob` и свободнотекстового `doc` нет ни
детектора, ни цифровой серии в словаре стража, ни автомата названий — собрать значение обратно
может только набор раскрытого в этом вызове.
"""

from __future__ import annotations

import json

from fake_1c import EDMX_ФИКСТУРА, запущенная

from odata1c.cli import main
from odata1c.config.loader import load_config
from odata1c.gate.service import refresh_policy
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository
from odata1c.registry.registry import SessionScope
from odata1c.tools.service import ToolService

ЛИЦА = "Catalog_ФизическиеЛица"

# Класс → поле, в котором гейт законно раскрывает токен этого класса, и реальное значение.
КЛАССЫ: dict[str, tuple[str, str]] = {
    "addr": ("АдресРегистрации", "г. Москва, ул. Тверская, д. 7, кв. 43"),
    "dob": ("ДатаРождения", "1980-05-01"),
    "doc": ("КемВыдан", "ОУФМС России по гор. Москве"),
    "phone": ("Телефон", "+7 916 123-45-67"),
    "inn": ("ИНН", "7707083893"),
}


def _дом(tmp_path, порт_1с: int):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(
        "default: ut\n"
        "bases:\n"
        "  ut:\n"
        "    label: УТ, поддельная 1С (эхо отбора)\n"
        f"    url: http://127.0.0.1:{порт_1с}/odata/standard.odata/\n"
        "    user: u\n"
        "    password: p\n"
        "    role: prod\n",
        encoding="utf-8",
    )
    config = load_config(home)
    хранилище = IndexRepository(index_path(home, "ut"))
    хранилище.write(parse_edmx(EDMX_ФИКСТУРА.read_bytes()))
    хранилище.close()
    refresh_policy(home, config.bases["ut"])
    return config


async def test_эхо_отбора_настоящей_1С_не_выносит_раскрытое(tmp_path):
    async with запущенная() as порт:
        config = _дом(tmp_path, порт)
        служба = ToolService(config)
        try:
            for класс, (поле, значение) in КЛАССЫ.items():
                токен = служба._gate_for(config.bases["ut"])._dictionary.token_for(
                    класс, значение, base="ut", entity=ЛИЦА, field=поле
                )

                текст = await служба.query(
                    SessionScope(), base="ut", entity=ЛИЦА, filter=f"{поле} eq '{токен}'"
                )
                ошибка = json.loads(текст)["error"]

                # Эхо действительно состоялось: 1С ответила отказом разбора и повторила поле —
                # значит на её стороне побывало раскрытое значение, а не только имя поля.
                assert ошибка["code"] == "odata_error", класс
                assert f"{поле} eq " in ошибка["message"], класс
                # …и вместо литерала в ответе стоит токен, а не значение.
                assert значение not in текст, класс
                assert токен in ошибка["message"], класс
        finally:
            await служба.aclose()
