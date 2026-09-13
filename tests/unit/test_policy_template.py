"""Шаблон `policy.yaml` владельца (ADR-0015, план 2026-09-13 §3, задача 3): содержимое как есть
после подстановки имени базы должно приниматься `load_policy` без ошибок, а после раскомментирования
примеров — давать ожидаемые классы полей, `names_for`, скрытую сущность и адрес доставки.
"""

from __future__ import annotations

import importlib.resources
import re

import yaml

from odata1c.gate.policy import load_policy
from odata1c.gate.tokens import CLASSES

ИМЯ_БАЗЫ = "trade_dev"


def _текст_шаблона() -> str:
    """Сырой текст `policy.example.yaml` из пакета — тот же файл, что читает
    `ensure_policy_template` (задача не дублирует копию содержимого в тесте)."""
    return (
        importlib.resources.files("odata1c.templates")
        .joinpath("policy.example.yaml")
        .read_text(encoding="utf-8")
    )


def _подставить_имя_базы(текст: str) -> str:
    return текст.replace("{{base}}", ИМЯ_БАЗЫ)


# Автомат раскомментирования примеров: строка `# <раздел>:` открывает область, строки с префиксом
# `#   ` внутри неё раскомментируются (тот же отступ, что и у открывающей строки, плюс три
# пробела), заглушка `<раздел>: {}` перед примером убирается, чтобы её не осталось рядом с
# раскомментированным разделом.
ОТКРЫВАЮЩИЕ = re.compile(r"^(?P<отступ>\s*)# (?P<ключ>names_for|entities|fields|custom|addr):")


def _раскомментировать_примеры(текст: str) -> str:
    итог: list[str] = []
    внутри: str | None = None
    for строка in текст.splitlines():
        m = ОТКРЫВАЮЩИЕ.match(строка)
        if m:
            ключ, отступ = m.group("ключ"), m.group("отступ")
            while итог and re.match(rf"^{re.escape(отступ)}{ключ}: \{{\}}", итог[-1]):
                итог.pop()
            итог.append(f"{отступ}{ключ}:")
            внутри = f"{отступ}#"
            continue
        if внутри is not None and строка.startswith(внутри + "   "):
            итог.append(строка.replace(внутри, внутри[:-1] + " ", 1))
            continue
        внутри = None
        итог.append(строка)
    return "\n".join(итог)


def test_шаблон_как_есть_разбирается_и_принимается_load_policy(tmp_path):
    текст = _подставить_имя_базы(_текст_шаблона())
    yaml.safe_load(текст)  # не бросает — синтаксис действующего (не примерного) содержимого верен

    путь = tmp_path / "policy.yaml"
    путь.write_text(текст, encoding="utf-8")
    политика = load_policy(путь)

    assert политика.scan_free_text is True
    assert политика.names_for() is None
    assert политика.sensitivity_of("Catalog_Контрагенты", "ИНН") is None


def test_шаблон_с_раскомментированными_примерами(tmp_path):
    текст = _раскомментировать_примеры(_подставить_имя_базы(_текст_шаблона()))
    yaml.safe_load(текст)  # раскомментированные примеры сами по себе — валидный YAML

    путь = tmp_path / "policy.yaml"
    путь.write_text(текст, encoding="utf-8")
    политика = load_policy(путь)

    assert политика.sensitivity_of("Catalog_Контрагенты", "КодПоОКПО") == "keep"
    assert политика.sensitivity_of("Document_ПлатежноеПоручение", "НазначениеПлатежа") == "scan"
    assert политика.sensitivity_of("Catalog_Контрагенты", "ДопИдентификатор") == "inn"
    assert политика.sensitivity_of("Catalog_Сотрудники", "ТабельныйНомер") == "custom:tab_number"
    assert политика.names_for() == {"Catalog_Контрагенты", "Catalog_Организации"}
    assert политика.is_hidden("Catalog_ФизическиеЛица")
    assert политика._defaults["addr"]["mask_for"] == ["Catalog_Партнеры"]


def test_шапка_называет_все_классы_гейта_и_keep_scan():
    """Проверяется именно таблица классов в шапке (между `# Классы` и `scan_free_text:`), а не
    файл целиком: часть классов (`keep`, `scan`, `inn`, `custom:tab_number`) встречается и в
    закомментированных примерах ниже — без сужения область теста осталась бы истинной, даже
    если саму таблицу классов из шапки убрать."""
    текст = _текст_шаблона()
    начало = текст.index("# Классы")
    конец = текст.index("scan_free_text:")
    шапка = текст[начало:конец]
    for класс in CLASSES:
        assert re.search(rf"\b{re.escape(класс)}\b", шапка), f"класс {класс!r} не упомянут в шапке"
    assert re.search(r"\bscan\b", шапка), "класс scan не упомянут в шапке"
