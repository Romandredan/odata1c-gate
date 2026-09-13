"""Политика базы после реиндекса и локальное раскрытие токена (SPEC §4.3, §3.5, §14.5)."""

import textwrap

import pytest
import yaml

from odata1c.config.models import BaseConfig
from odata1c.gate.service import classifier_for, owner_names_for, policy_path, refresh_policy
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository


def база(**kwargs) -> BaseConfig:
    return BaseConfig(
        name="ut",
        label="УТ",
        url="http://localhost/ut/odata/standard.odata/",
        user="u",
        password="p",
        role="test",
        **kwargs,
    )


@pytest.fixture
def дом_с_индексом(tmp_path, edmx_synthetic):
    хранилище = IndexRepository(index_path(tmp_path, "ut"))
    хранилище.write(parse_edmx(edmx_synthetic))
    хранилище.close()
    return tmp_path


def test_политика_создаётся_с_секцией_auto(дом_с_индексом):
    refresh_policy(дом_с_индексом, база())
    данные = yaml.safe_load(policy_path(дом_с_индексом, "ut").read_text(encoding="utf-8"))

    assert данные["auto"]["Catalog_Контрагенты.ИНН"] == "inn"
    assert данные["auto"]["Catalog_Контрагенты.КПП"] == "kpp"
    assert данные["auto"]["Catalog_БанковскиеСчета.НомерСчета"] == "acc"


def test_описание_контрагентов_попадает_в_класс_названий(дом_с_индексом):
    refresh_policy(дом_с_индексом, база())
    данные = yaml.safe_load(policy_path(дом_с_индексом, "ut").read_text(encoding="utf-8"))
    assert данные["auto"]["Catalog_Контрагенты.Description"] == "org"


def test_новые_поля_названий_возвращаются_на_проверку(дом_с_индексом):
    новые = refresh_policy(дом_с_индексом, база())
    поля = {(поле["entity"], поле["field"]) for поле in новые}
    assert ("Catalog_Контрагенты", "Description") in поля


def test_повторный_вызов_без_изменений_не_возвращает_старые_поля(дом_с_индексом):
    """Правка ревью задачи 9: «на проверку» — только НОВЫЕ поля, не весь текущий auto заново
    (иначе на боевой базе одни и те же сотни строк печатались бы при каждом реиндексе)."""
    первый_вызов = refresh_policy(дом_с_индексом, база())
    assert первый_вызов  # индекс собран впервые — есть что показать

    второй_вызов = refresh_policy(дом_с_индексом, база())
    assert второй_вызов == []


def test_ручные_разделы_не_затираются(дом_с_индексом):
    путь = policy_path(дом_с_индексом, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text(
        textwrap.dedent("""
        version: 2
        scan_free_text: true
        fields:
          Catalog_Контрагенты.ИНН: keep
        entities:
          Catalog_БанковскиеСчета: { hide: true }
        auto:
          Catalog_Устаревший.Поле: inn
    """),
        encoding="utf-8",
    )

    refresh_policy(дом_с_индексом, база())
    данные = yaml.safe_load(путь.read_text(encoding="utf-8"))

    assert данные["fields"] == {"Catalog_Контрагенты.ИНН": "keep"}
    assert данные["entities"]["Catalog_БанковскиеСчета"]["hide"] is True
    assert "Catalog_Устаревший.Поле" not in данные["auto"]  # старое auto заменено целиком


def test_список_названий_базы_учитывается(дом_с_индексом):
    """Список `names_for` — ручной раздел `policy.yaml` (ADR-0015), не поле `bases.yaml`:
    `refresh_policy` подставляет его классификатору, не трогая сам раздел при перезаписи auto."""
    путь = policy_path(дом_с_индексом, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text("version: 2\nnames_for: [Catalog_БанковскиеСчета]\n", encoding="utf-8")

    refresh_policy(дом_с_индексом, база())
    данные = yaml.safe_load(путь.read_text(encoding="utf-8"))

    assert данные["auto"]["Catalog_БанковскиеСчета.Description"] == "org"
    assert "Catalog_Контрагенты.Description" not in данные["auto"]


def test_классификатор_совместим_с_реиндексом(tmp_path):
    классификатор = classifier_for(tmp_path, база())
    assert классификатор("Catalog_Контрагенты", "ИНН", "Edm.String") == ("inn", "auto")
    assert классификатор("Catalog_Контрагенты", "Code", "Edm.String") is None


def test_owner_names_for_без_файла_политики_возвращает_none(tmp_path):
    assert owner_names_for(tmp_path, "ut") is None


def test_owner_names_for_читает_список_из_policy_yaml(tmp_path):
    путь = policy_path(tmp_path, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text("version: 2\nnames_for: [Catalog_Контрагенты]\n", encoding="utf-8")

    assert owner_names_for(tmp_path, "ut") == {"Catalog_Контрагенты"}
