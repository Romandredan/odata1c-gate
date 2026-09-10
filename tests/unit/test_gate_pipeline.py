"""Конвейер гейта на одну базу: BaseGate — фасад над маскировщиком, обратной подменой, стражем
и политикой (SPEC §6.1-6.9). Слой тулов (задача 4 M1d) вызывает его на каждый запрос к 1С."""

import json
import os
import time

import pytest

from odata1c.config.models import BaseConfig, GateSettings
from odata1c.gate.dictionary import Dictionary
from odata1c.gate.guard import Guard
from odata1c.gate.pipeline import BaseGate, guard_only

СЕКРЕТ = "секрет-для-тестов-ровно-32-байта".encode()

# auto (не fields): test_is_protected_учитывает_уровень описывает именно эту разметку, а
# test_refresh_подхватывает_новую_политику дописывает НОВЫЙ раздел fields — раздела fields в
# политике по умолчанию ещё нет, дублирования ключа не возникает.
ПОЛИТИКА_ТЕКСТ = """
version: 2
scan_free_text: true
auto:
  Catalog_Контрагенты.Description: org
  Catalog_Контрагенты.ИНН: inn
"""


def _база(name: str, mode: str) -> BaseConfig:
    return BaseConfig(
        name=name,
        label=name,
        url="http://host/base/odata/standard.odata/",
        user="agent",
        gate=GateSettings(mode=mode),
    )


@pytest.fixture
def путь_политики(tmp_path):
    путь = tmp_path / "policy.yaml"
    путь.write_text(ПОЛИТИКА_ТЕКСТ, encoding="utf-8")
    return путь


@pytest.fixture
def словарь(tmp_path):
    словарь = Dictionary(tmp_path / "gate.sqlite", СЕКРЕТ)
    yield словарь
    словарь.close()


@pytest.fixture
def guard(словарь):
    return Guard(словарь)


@pytest.fixture
def врата_prod(путь_политики, словарь, guard):
    return BaseGate(
        base=_база("ut", "identifiers+names"),
        dictionary=словарь,
        guard=guard,
        policy_path=путь_политики,
    )


@pytest.fixture
def врата_identifiers(путь_политики, словарь, guard):
    return BaseGate(
        base=_база("ut", "identifiers"),
        dictionary=словарь,
        guard=guard,
        policy_path=путь_политики,
    )


@pytest.fixture
def врата_dev(путь_политики, словарь, guard):
    return BaseGate(
        base=_база("dev", "off"),
        dictionary=словарь,
        guard=guard,
        policy_path=путь_политики,
    )


def test_finish_прогоняет_стража_и_помечает_замену(врата_prod, словарь):
    инн = "7707083893"
    словарь.token_for("inn", инн, base="ut", entity="Catalog_Контрагенты", field="ИНН")
    текст = врата_prod.finish({"items": [{"Комментарий": f"ИНН {инн}"}], "warnings": []})
    assert инн not in текст
    данные = json.loads(текст)
    assert any(п.startswith("guard_replaced") for п in данные["warnings"])


def test_finish_сериализует_без_экранирования(врата_prod):
    текст = врата_prod.finish({"items": [{"Description": "Склад"}], "warnings": []})
    assert "Склад" in текст and "\\u" not in текст


def test_finish_на_уровне_off_ничего_не_меняет(врата_dev, словарь):
    инн = "7707083893"
    словарь.token_for("inn", инн, base="dev", entity="E", field="ИНН")
    текст = врата_dev.finish({"items": [{"ИНН": инн}], "warnings": []})
    assert json.loads(текст)["items"][0]["ИНН"] == инн


def test_finish_не_экранирует_кириллицу_на_уровне_off(врата_dev):
    """Уровень off — единственный путь, где страж возвращает сериализованный текст как есть, не
    переписывая JSON-литералы (`guard.check` при `mode == "off"` — тождество): здесь видна сама
    сериализация `finish`, а не побочная нормализация `\\u`-экранирования стражем на JSON-пути
    (guard.py, докстринг модуля, раунд 2 C1 — страж сам снимает экранирование у изменённых или
    записанных через `\\u` литералов на любом другом уровне)."""
    текст = врата_dev.finish({"items": [{"Description": "Склад"}], "warnings": []})
    assert "Склад" in текст and "\\u" not in текст


def test_error_маскирует_сообщение_1С(врата_prod, словарь):
    инн = "7707083893"
    словарь.token_for("inn", инн, base="ut", entity="E", field="ИНН")
    текст = врата_prod.error("odata_error", f"Не найден контрагент с ИНН {инн}", "проверьте отбор")
    assert инн not in текст
    assert json.loads(текст)["error"]["code"] == "odata_error"


def test_is_protected_учитывает_уровень(врата_prod, врата_identifiers):
    # политика: auto Catalog_Контрагенты.Description: org, Catalog_Контрагенты.ИНН: inn
    assert врата_prod.is_protected("Catalog_Контрагенты", "Description") is True
    assert врата_identifiers.is_protected("Catalog_Контрагенты", "Description") is False
    assert врата_identifiers.is_protected("Catalog_Контрагенты", "ИНН") is True
    assert врата_prod.is_protected("Catalog_Контрагенты", "Code") is False


def test_inbound_filter_в_режиме_identifiers_пропускает_название(врата_identifiers):
    выражение = "Description eq 'ООО Ромашка'"
    assert врата_identifiers.inbound_filter(выражение, entity="Catalog_Контрагенты") == выражение


def test_refresh_подхватывает_новую_политику(врата_prod, путь_политики):
    assert врата_prod.is_protected("Catalog_Контрагенты", "Code") is False
    путь_политики.write_text(
        путь_политики.read_text(encoding="utf-8") + "fields:\n  Catalog_Контрагенты.Code: inn\n",
        encoding="utf-8",
    )
    os.utime(путь_политики, (time.time() + 5, time.time() + 5))
    врата_prod.refresh()
    assert врата_prod.is_protected("Catalog_Контрагенты", "Code") is True


def test_finish_text_прогоняет_стража(врата_prod, словарь):
    инн = "7707083893"
    словарь.token_for("inn", инн, base="ut", entity="E", field="ИНН")
    assert инн not in врата_prod.finish_text(f"| ИНН | {инн} |")


def test_guard_only_на_строжайшем_уровне(guard, словарь):
    инн = "7707083893"
    словарь.token_for("inn", инн, base="ut", entity="E", field="ИНН")
    assert инн not in guard_only(guard, {"error": {"message": f"x {инн}"}})
