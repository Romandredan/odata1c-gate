"""Хранилище индекса: запись, поиск, описание сущности (SPEC §4.2, §4.4)."""

import json

import pytest

from odata1c.index.edmx import parse_edmx
from odata1c.index.repository import IndexRepository


@pytest.fixture
def индекс(tmp_path, edmx_synthetic):
    repository = IndexRepository(tmp_path / "metadata.sqlite")
    repository.write(parse_edmx(edmx_synthetic))
    yield repository
    repository.close()


def test_записаны_все_сущности(индекс):
    assert len(индекс.entity_names()) == 8
    assert "Catalog_Контрагенты" in индекс.entity_names()


def test_точное_совпадение_первым(индекс):
    найдено = индекс.find("Контрагенты")
    assert найдено[0].name == "Catalog_Контрагенты"
    assert найдено[0].russian_kind == "Справочник"
    assert найдено[0].key_fields == ["Ref_Key"]


def test_поиск_без_учёта_регистра_и_ё(индекс):
    assert индекс.find("контрагенты")[0].name == "Catalog_Контрагенты"


def test_поиск_по_части_слова(индекс):
    имена = [найдено.name for найдено in индекс.find("реализация")]
    assert "Document_РеализацияТоваровУслуг" in имена


def test_поиск_по_основе_слова(индекс):
    имена = [найдено.name for найдено in индекс.find("реализации товаров")]
    assert "Document_РеализацияТоваровУслуг" in имена


def test_фильтр_по_виду(индекс):
    найдено = индекс.find("товары", kind="Document")
    assert найдено
    assert all(результат.name.startswith("Document") for результат in найдено)


def test_ограничение_числа_результатов(индекс):
    assert len(индекс.find("к", limit=3)) <= 3


def test_описание_сущности(индекс):
    описание = индекс.describe("Document_РеализацияТоваровУслуг")
    assert описание.key_fields == ["Ref_Key"]
    assert описание.russian_kind == "Документ"
    имена_полей = {поле["name"] for поле in описание.fields}
    assert "СуммаДокумента" in имена_полей
    assert "Document_РеализацияТоваровУслуг_Товары" in описание.children


def test_описание_показывает_действия(индекс):
    описание = индекс.describe("Document_РеализацияТоваровУслуг")
    имена_действий = {действие["name"] for действие in описание.actions}
    assert "Document_РеализацияТоваровУслуг_Post" in имена_действий


def test_описание_неизвестной_сущности(индекс):
    assert индекс.describe("Catalog_Нет") is None


def test_признак_независимого_регистра_сохранён(индекс):
    assert индекс.describe("InformationRegister_КурсыВалют").is_independent_register is True
    assert индекс.describe("InformationRegister_СостоянияЗаказов").is_independent_register is False


def test_класс_поля_можно_проставить_и_прочитать(индекс):
    индекс.set_field_sensitivity("Catalog_Контрагенты", "ИНН", "inn", source="auto")
    поля = {поле["name"]: поле for поле in индекс.describe("Catalog_Контрагенты").fields}
    assert поля["ИНН"]["sensitivity"] == "inn"
    assert поля["ИНН"]["sensitivity_source"] == "auto"


def test_контрольная_сумма_сохраняется(индекс, edmx_synthetic):
    assert индекс.meta("edmx_sha256") == parse_edmx(edmx_synthetic).edmx_sha256
    assert индекс.meta("entity_count") == "8"


def test_повторная_запись_не_дублирует(индекс, edmx_synthetic):
    индекс.write(parse_edmx(edmx_synthetic))
    assert len(индекс.entity_names()) == 8


def _эдмкс_с_нераспознанным_набором() -> bytes:
    """Минимальный EDMX с набором, ссылающимся на несуществующий EntityType (см. edmx.py,
    поле ParsedMetadata.unresolved_entity_sets) — отдельно от synthetic.edmx, чтобы не менять
    фикстуру, общую с задачами 1 и 3."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
  <edmx:DataServices m:DataServiceVersion="3.0"
                     xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <Schema Namespace="StandardODATA" xmlns="http://schemas.microsoft.com/ado/2009/11/edm">
      <EntityContainer Name="StandardODATA" m:IsDefaultEntityContainer="true">
        <EntitySet Name="Catalog_Пропавший" EntityType="StandardODATA.Catalog_Пропавший"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>""".encode()


def test_нераспознанные_наборы_попадают_в_служебную_таблицу(tmp_path):
    репозиторий = IndexRepository(tmp_path / "metadata.sqlite")
    репозиторий.write(parse_edmx(_эдмкс_с_нераспознанным_набором()))
    assert json.loads(репозиторий.meta("unresolved_entity_sets")) == ["Catalog_Пропавший"]
    репозиторий.close()


def test_отсутствие_нераспознанных_наборов_не_оставляет_ключ(индекс):
    # synthetic.edmx не содержит испорченных ссылок — ключ не должен появляться в meta.
    assert индекс.meta("unresolved_entity_sets") is None
