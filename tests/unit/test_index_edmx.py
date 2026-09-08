"""Разбор EDMX: сущности, ключи, поля, признаки регистров, действия (SPEC §4.1, §4.2)."""

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
