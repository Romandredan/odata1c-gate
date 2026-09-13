"""Политика базы после реиндекса и локальное раскрытие токена (SPEC §4.3, §3.5, §14.5).

ADR-0015: реиндекс (`refresh_policy`) пишет только `policy.auto.yaml`; файл владельца
`policy.yaml` не трогает — кроме единственного случая, первого вызова новой версии на базе, где
раздел `auto` ещё лежит в файле владельца (миграция, `strip_auto_section`)."""

import textwrap

import pytest
import yaml

from odata1c.config.models import BaseConfig
from odata1c.gate.service import (
    auto_policy_path,
    classifier_for,
    owner_names_for,
    policy_path,
    refresh_policy,
)
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
    данные = yaml.safe_load(auto_policy_path(дом_с_индексом, "ut").read_text(encoding="utf-8"))

    assert данные["auto"]["Catalog_Контрагенты.ИНН"] == "inn"
    assert данные["auto"]["Catalog_Контрагенты.КПП"] == "kpp"
    assert данные["auto"]["Catalog_БанковскиеСчета.НомерСчета"] == "acc"


def test_описание_контрагентов_попадает_в_класс_названий(дом_с_индексом):
    refresh_policy(дом_с_индексом, база())
    данные = yaml.safe_load(auto_policy_path(дом_с_индексом, "ut").read_text(encoding="utf-8"))
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


def test_реиндекс_пишет_только_авторазметку_и_не_переписывает_её_повторно(дом_с_индексом):
    """Шаг 6 плана: `policy.auto.yaml` получает предупреждающую шапку и раздел `auto`; файл
    владельца с разделом `auto` и комментарием после вызова теряет `auto`, но не комментарий;
    повторный вызов без перемен файл авторазметки не переписывает (ADR-0015)."""
    путь_владельца = policy_path(дом_с_индексом, "ut")
    путь_владельца.parent.mkdir(parents=True, exist_ok=True)
    путь_владельца.write_text(
        "# мой комментарий владельца\nversion: 2\nfields: {}\n"
        "auto:\n  Catalog_Устаревший.Поле: inn\n",
        encoding="utf-8",
    )

    refresh_policy(дом_с_индексом, база())

    путь_авто = auto_policy_path(дом_с_индексом, "ut")
    текст_авто = путь_авто.read_text(encoding="utf-8")
    assert "НЕ РЕДАКТИРОВАТЬ" in текст_авто
    авто = yaml.safe_load(текст_авто)
    assert авто["auto"]["Catalog_Контрагенты.ИНН"] == "inn"

    текст_владельца = путь_владельца.read_text(encoding="utf-8")
    assert "# мой комментарий владельца" in текст_владельца
    assert "auto:" not in текст_владельца

    отметка_до = путь_авто.stat().st_mtime_ns
    содержимое_до = путь_авто.read_bytes()
    refresh_policy(дом_с_индексом, база())
    assert путь_авто.stat().st_mtime_ns == отметка_до  # файл не переписан повторно
    assert путь_авто.read_bytes() == содержимое_до


def test_ручные_разделы_не_затираются(дом_с_индексом):
    """Раздел `auto`, ещё лежащий в файле владельца (база со старым `policy.yaml`), первый вызов
    новой версии уносит в `policy.auto.yaml` целиком (`strip_auto_section`) — ручные разделы
    (`fields`, `entities`) остаются на месте, старое значение `auto` не переживает перенос."""
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
    авто = yaml.safe_load(auto_policy_path(дом_с_индексом, "ut").read_text(encoding="utf-8"))

    assert данные["fields"] == {"Catalog_Контрагенты.ИНН": "keep"}
    assert данные["entities"]["Catalog_БанковскиеСчета"]["hide"] is True
    assert "auto" not in данные  # унесено в policy.auto.yaml
    assert "Catalog_Устаревший.Поле" not in авто["auto"]  # старое auto заменено целиком


def test_список_названий_базы_учитывается(дом_с_индексом):
    """Список `names_for` — ручной раздел `policy.yaml` (ADR-0015), не поле `bases.yaml`:
    `refresh_policy` подставляет его классификатору, не трогая сам раздел при перезаписи auto."""
    путь = policy_path(дом_с_индексом, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text("version: 2\nnames_for: [Catalog_БанковскиеСчета]\n", encoding="utf-8")

    refresh_policy(дом_с_индексом, база())
    авто = yaml.safe_load(auto_policy_path(дом_с_индексом, "ut").read_text(encoding="utf-8"))

    assert авто["auto"]["Catalog_БанковскиеСчета.Description"] == "org"
    assert "Catalog_Контрагенты.Description" not in авто["auto"]
    # Раздел names_for — ручной, реиндекс его не трогает.
    владелец = yaml.safe_load(путь.read_text(encoding="utf-8"))
    assert владелец["names_for"] == ["Catalog_БанковскиеСчета"]


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


def test_owner_names_for_пустой_список_это_не_отсутствующий_раздел(tmp_path):
    """`names_for: []`, явно прописанный владельцем, — не то же самое, что отсутствующий раздел:
    отсутствие даёт `None` (встроенный список `DEFAULT_NAMES_FOR`), а пустой список — осознанное
    решение выключить слой 1 целиком и приходит как пустое множество. Разница принципиальна:
    `classify_field(..., names_for=set())` не защищает ни `Description`, ни ФИО ни у одной сущности
    (предостережение — `tests/unit/test_tools_service.py`, комментарий к находкам Р61/атаке на
    `_цель_пути`). Тест фиксирует это поведение явно, а не оставляет его неочевидным следствием
    устройства `Policy.names_for()`."""
    путь = policy_path(tmp_path, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text("version: 2\nnames_for: []\n", encoding="utf-8")

    assert owner_names_for(tmp_path, "ut") == set()
