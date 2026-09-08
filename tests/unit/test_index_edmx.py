"""Разбор EDMX: сущности, ключи, поля, признаки регистров, действия (SPEC §4.1, §4.2)."""

import io

from odata1c.index.edmx import parse_edmx


def test_разобраны_все_наборы_сущностей(edmx_synthetic):
    разобрано = parse_edmx(edmx_synthetic)
    имена = {сущность.name for сущность in разобрано.entities}
    assert имена == {
        "Catalog_Контрагенты",
        "Document_РеализацияТоваровУслуг",
        "Document_РеализацияТоваровУслуг_Товары",
        "InformationRegister_КурсыВалют",
        "InformationRegister_КурсыВалют_SliceLast",
        "InformationRegister_СостоянияЗаказов",
        "AccumulationRegister_ТоварыНаСкладах_Balance",
        "Catalog_БанковскиеСчета",
    }


def найти(разобрано, имя):
    return next(сущность for сущность in разобрано.entities if сущность.name == имя)


def test_ключи_и_поля_справочника(edmx_synthetic):
    контрагенты = найти(parse_edmx(edmx_synthetic), "Catalog_Контрагенты")
    assert контрагенты.key_fields == ["Ref_Key"]
    assert контрагенты.description_field == "Description"
    assert контрагенты.russian_kind == "Справочник"
    имена_полей = {поле.name for поле in контрагенты.fields}
    assert {"ИНН", "КПП", "DeletionMark", "ГоловнойКонтрагент_Key"} <= имена_полей


def test_ссылочное_поле_опознано(edmx_synthetic):
    контрагенты = найти(parse_edmx(edmx_synthetic), "Catalog_Контрагенты")
    ссылка = next(поле for поле in контрагенты.fields if поле.name == "ГоловнойКонтрагент_Key")
    assert ссылка.is_ref is True


def test_составное_поле_опознано(edmx_synthetic):
    счета = найти(parse_edmx(edmx_synthetic), "Catalog_БанковскиеСчета")
    владелец = next(поле for поле in счета.fields if поле.name == "Владелец_Key")
    assert владелец.is_composite is True  # рядом есть Владелец_Type


def test_ссылочное_поле_без_парного_типа_не_составное(edmx_synthetic):
    контрагенты = найти(parse_edmx(edmx_synthetic), "Catalog_Контрагенты")
    ссылка = next(поле for поле in контрагенты.fields if поле.name == "ГоловнойКонтрагент_Key")
    assert ссылка.is_composite is False  # парного ГоловнойКонтрагент_Type нет


def test_регистратор_с_несколькими_типами_опознан_как_составная_ссылка(edmx_synthetic):
    состояния = найти(parse_edmx(edmx_synthetic), "InformationRegister_СостоянияЗаказов")
    регистратор = next(поле for поле in состояния.fields if поле.name == "Recorder")
    assert регистратор.is_ref is True
    assert регистратор.is_composite is True  # рядом есть Recorder_Type


def test_документ_имеет_признак_проведения(edmx_synthetic):
    документ = найти(parse_edmx(edmx_synthetic), "Document_РеализацияТоваровУслуг")
    assert документ.has_posted is True


def test_табличная_часть_привязана_к_родителю(edmx_synthetic):
    товары = найти(parse_edmx(edmx_synthetic), "Document_РеализацияТоваровУслуг_Товары")
    assert товары.is_tabular_part is True
    assert товары.parent_entity == "Document_РеализацияТоваровУслуг"
    assert товары.key_fields == ["Ref_Key", "LineNumber"]


def test_независимый_регистр_сведений(edmx_synthetic):
    курсы = найти(parse_edmx(edmx_synthetic), "InformationRegister_КурсыВалют")
    assert курсы.is_independent_register is True
    assert курсы.has_recorder is False
    assert курсы.key_fields == ["Period", "Валюта_Key"]


def test_регистр_с_регистратором_не_независимый(edmx_synthetic):
    состояния = найти(parse_edmx(edmx_synthetic), "InformationRegister_СостоянияЗаказов")
    assert состояния.has_recorder is True
    assert состояния.is_independent_register is False


def test_виртуальная_таблица_привязана_к_родителю(edmx_synthetic):
    остатки = найти(parse_edmx(edmx_synthetic), "AccumulationRegister_ТоварыНаСкладах_Balance")
    assert остатки.is_virtual is True
    assert остатки.virtual_kind == "Balance"
    assert остатки.parent_entity == "AccumulationRegister_ТоварыНаСкладах"


def test_виртуальная_таблица_не_независимый_регистр(edmx_synthetic):
    # Без исключения виртуальных таблиц из признака независимого регистра сведений
    # физическое удаление записи оказалось бы разрешено там, где разрешена только
    # пометка удаления, — см. предупреждение в брифе задачи.
    срез = найти(parse_edmx(edmx_synthetic), "InformationRegister_КурсыВалют_SliceLast")
    assert срез.is_virtual is True
    assert срез.has_recorder is False
    assert срез.is_independent_register is False


def test_действия_разобраны_с_параметрами(edmx_synthetic):
    разобрано = parse_edmx(edmx_synthetic)
    действия = {действие.name: действие for действие in разобрано.actions}
    assert "Document_РеализацияТоваровУслуг_Post" in действия
    post = действия["Document_РеализацияТоваровУслуг_Post"]
    assert post.entity == "Document_РеализацияТоваровУслуг"
    assert post.params == {"PostingModeOperational": "Edm.Boolean"}
    assert post.http_method == "POST"


def test_контрольная_сумма_устойчива(edmx_synthetic):
    первый = parse_edmx(edmx_synthetic).edmx_sha256
    второй = parse_edmx(edmx_synthetic).edmx_sha256
    assert первый == второй
    assert len(первый) == 64


def test_битый_xml_даёт_понятную_ошибку():
    import pytest

    from odata1c.index.edmx import EdmxError

    with pytest.raises(EdmxError) as ошибка:
        parse_edmx("<edmx:Edmx><не закрыт>".encode())
    assert "metadata" in str(ошибка.value).lower() or "разобрать" in str(ошибка.value).lower()


def _обёртка_эдмкс(тело_контейнера: str) -> bytes:
    """Минимальный валидный EDMX с произвольным содержимым EntityContainer — для сценариев,
    которые не должны затрагивать общую фикстуру synthetic.edmx."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
  <edmx:DataServices m:DataServiceVersion="3.0"
                     xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <Schema Namespace="StandardODATA" xmlns="http://schemas.microsoft.com/ado/2009/11/edm">
      <EntityContainer Name="StandardODATA" m:IsDefaultEntityContainer="true">
        {тело_контейнера}
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>""".encode()


def test_набор_без_существующего_типа_попадает_в_нераспознанные():
    # Набор ссылается на EntityType, которого в документе нет (испорченная ссылка в описании).
    # Обновление индекса не должно прочитать это как удаление сущности — see SPEC brief задачи.
    edmx = _обёртка_эдмкс(
        '<EntitySet Name="Catalog_Пропавший" EntityType="StandardODATA.Catalog_Пропавший"/>'
    )
    разобрано = parse_edmx(edmx)
    assert разобрано.entities == []
    assert разобрано.unresolved_entity_sets == ["Catalog_Пропавший"]


class _МаленькимиПорциями:
    """Обёртка над BytesIO, отдающая байты малыми кусками.

    lxml.etree.iterparse честно освобождает память только тогда, когда парсер действительно
    читает источник по частям: если весь тестовый документ умещается в один internal-read
    (обычный BytesIO так и делает для документов в десятки килобайт), библиотека успевает
    построить дерево целиком ДО первого события — и очистка внутри цикла ничего не покажет,
    хотя код давно всё чистит правильно. Порционное чтение форсирует настоящее чередование
    "разобрали кусок — отдали событие — вызывающий код освободил узел", как на настоящем
    файле в десятки мегабайт.
    """

    # Настоящий io.BytesIO — сохранён до подмены edmx_модуль.io.BytesIO ниже в тесте,
    # иначе конструктор рекурсивно вызывал бы сам себя через подменённый атрибут модуля.
    _настоящий_bytesio = io.BytesIO

    def __init__(self, data: bytes, размер_порции: int = 256) -> None:
        self._поток = self._настоящий_bytesio(data)
        self._размер_порции = размер_порции

    def read(self, size: int = -1) -> bytes:  # noqa: ARG002 — сигнатура нужна libxml2
        return self._поток.read(self._размер_порции)


def test_разбор_контейнера_не_копит_дерево_целиком(monkeypatch):
    """Регрессия: второй проход (_разобрать_контейнер) обязан освобождать разобранные узлы так
    же, как первый (_разобрать_типы) — иначе пиковая память растёт пропорционально числу
    наборов данных, а не остаётся постоянной. Проверяем не память процесса (не переносимо на
    Windows), а число живых детей EntityContainer в момент, когда очередной EntitySet
    закрывается: при потоковой очистке оно ограничено горсткой независимо от размера
    документа, без неё — растёт пропорционально числу наборов. Проверяется именно вторая
    функция разбора отдельно от первой: у первой то же свойство уже обеспечено и не является
    предметом этой правки.
    """
    import odata1c.index.edmx as edmx_модуль

    число_наборов = 3000
    наборы_xml = "".join(
        f'<EntitySet Name="Catalog_Т{i}" EntityType="StandardODATA.Catalog_Т{i}"/>'
        for i in range(число_наборов)
    )
    edmx = _обёртка_эдмкс(наборы_xml)

    настоящий_iterparse = edmx_модуль.etree.iterparse
    наибольшее_число_детей = 0

    def подглядывающий_iterparse(*args, **kwargs):
        nonlocal наибольшее_число_детей
        for событие, элемент in настоящий_iterparse(*args, **kwargs):
            родитель = элемент.getparent()
            if родитель is not None:
                наибольшее_число_детей = max(наибольшее_число_детей, len(родитель))
            yield событие, элемент

    monkeypatch.setattr(edmx_модуль.etree, "iterparse", подглядывающий_iterparse)
    monkeypatch.setattr(edmx_модуль.io, "BytesIO", _МаленькимиПорциями)
    наборы, _действия, _подсказка = edmx_модуль._разобрать_контейнер(edmx)

    assert len(наборы) == число_наборов
    # При потоковой очистке у EntityContainer в любой момент разбора держится лишь горстка уже
    # обработанных, но ещё не удалённых соседей — далеко меньше, чем число_наборов. До правки
    # (без очистки во втором проходе) это число росло почти до число_наборов — проверено вручную
    # тем же способом с временно опустошённым набором освобождаемых тегов.
    assert наибольшее_число_детей < 50
