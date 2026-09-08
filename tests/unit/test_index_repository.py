"""Хранилище индекса: запись, поиск, описание сущности (SPEC §4.2, §4.4)."""

import json

import pytest

from odata1c.index.edmx import ParsedEntity, ParsedField, ParsedMetadata, parse_edmx
from odata1c.index.repository import IndexCorruptError, IndexRepository


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


def _сущность(name, kind, russian_kind, base_name, fields) -> ParsedEntity:
    """Минимальная ParsedEntity для тестов ранжирования — без похода через parse_edmx, чтобы
    сосредоточиться на IndexRepository.find(), а не на разборе EDMX (тот проверен отдельно)."""
    return ParsedEntity(
        name=name,
        kind=kind,
        russian_kind=russian_kind,
        base_name=base_name,
        parent_entity=None,
        is_tabular_part=False,
        is_virtual=False,
        virtual_kind=None,
        key_fields=[],
        description_field=None,
        has_posted=False,
        has_recorder=False,
        is_independent_register=False,
        fields=fields,
    )


def test_точное_совпадение_опережает_совпадение_по_основам(tmp_path):
    # Конфликтный случай, которого нет в synthetic.edmx: обе сущности делят основу слова
    # "контрагент", но только одна совпадает с запросом точно. По алфавиту
    # "AccumulationRegister_..." идёт раньше "Catalog_...", поэтому без отдельной ветки точного
    # совпадения (SPEC §4.4: точное совпадение → основы слов → триграммы) сортировка по (-score,
    # name) поставила бы её первой — обе получили бы одинаковую оценку через ветку основ слов.
    контрагенты = _сущность(
        "Catalog_Контрагенты",
        "Catalog",
        "Справочник",
        "Контрагенты",
        [ParsedField(name="Ref_Key", edm_type="Edm.Guid", nullable=False, is_key=True)],
    )
    долги = _сущность(
        "AccumulationRegister_ДолгиКонтрагентов",
        "AccumulationRegister",
        "РегистрНакопления",
        "ДолгиКонтрагентов",
        [ParsedField(name="Period", edm_type="Edm.DateTime", nullable=False)],
    )
    репозиторий = IndexRepository(tmp_path / "metadata.sqlite")
    репозиторий.write(
        ParsedMetadata(entities=[контрагенты, долги], actions=[], edmx_sha256="0" * 64)
    )

    найдено = репозиторий.find("Контрагенты")
    assert [результат.name for результат in найдено] == [
        "Catalog_Контрагенты",
        "AccumulationRegister_ДолгиКонтрагентов",
    ]
    assert найдено[0].score == 100.0  # точное совпадение
    assert найдено[1].score == 51.0  # только основа слова совпала
    репозиторий.close()


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


def test_повторная_запись_не_копит_мусор_в_полнотекстовой_таблице(индекс, edmx_synthetic):
    # entity_names() (см. тест выше) смотрит только на таблицу entities — полнотекстовая
    # таблица entities_fts заполняется отдельной командой в write() и требует отдельной
    # проверки: без очистки перед повторной вставкой строки в ней копятся (после трёх записей
    # синтетического образца — 24 строки вместо 8, обнаружено ревью). Публичного метода для
    # чтения entities_fts нет, поэтому проверка идёт через внутреннее соединение — тот же приём,
    # что и в test_index_edmx.py для проверки потоковой очистки разбора.
    индекс.write(parse_edmx(edmx_synthetic))
    индекс.write(parse_edmx(edmx_synthetic))
    строк_в_fts = индекс._connection.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0]
    assert строк_в_fts == len(индекс.entity_names()) == 8


def test_порог_отсекает_совпадение_только_по_общему_префиксу_вида(индекс):
    # Запрос точным именем объекта раньше вторым кандидатом возвращал другую сущность того же
    # вида (Catalog) — триграммы совпадали только за счёт общего префикса "catalog" в сжатом
    # имени, доля общих триграмм 0.161 (обнаружено ревью). Порог ПОРОГ_ТРИГРАММ = 0.2 это
    # отсекает: единственный результат — сама запрошенная сущность.
    найдено = индекс.find("Catalog_Контрагенты")
    assert [результат.name for результат in найдено] == ["Catalog_Контрагенты"]


def test_несуществующее_слово_не_даёт_случайных_кандидатов(индекс):
    # Бессвязное слово случайно даёт долю общих триграмм 0.024 с одной из сущностей образца
    # (InformationRegister_СостоянияЗаказов) — ниже порога, результат пуст.
    assert индекс.find("плаваниясрок") == []


def test_опечатка_в_одну_букву_всё_ещё_находит_сущность(индекс):
    # Буква "а" выпала из середины слова — ни точного совпадения, ни общей основы, ни
    # вхождения подстроки уже нет, остаётся триграммное сходство (0.333 — выше порога 0.2).
    найдено = индекс.find("Контргенты")
    assert найдено
    assert найдено[0].name == "Catalog_Контрагенты"


def test_однобуквенный_запрос_не_возвращает_всё_подряд(индекс):
    # До ограничения минимальной длины ветка вхождения подстроки принимала любой непустой
    # запрос: один символ "к" находился в сжатых именах 6 из 8 сущностей образца.
    assert индекс.find("к") == []


def test_повреждённый_файл_индекса_даёт_понятную_ошибку(tmp_path):
    путь = tmp_path / "metadata.sqlite"
    путь.write_bytes(b"not a real sqlite database, just random junk bytes 12345")
    with pytest.raises(IndexCorruptError) as ошибка:
        IndexRepository(путь)
    assert str(путь) in str(ошибка.value)
    assert ошибка.value.code == "index_corrupt"
    assert "reindex" in ошибка.value.hint


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
