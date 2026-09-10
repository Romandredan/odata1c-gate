"""Хранилище индекса: запись, поиск, описание сущности (SPEC §4.2, §4.4)."""

import json

import pytest
from conftest import обёртка_эдмкс

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
        is_records=False,
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


def test_основа_слова_опережает_подстроку_а_та_триграммы(tmp_path):
    # Правка по итогам финального ревью M1b (Important, задача 4): в реализации есть четвёртый
    # уровень ранжирования (вхождение сжатого запроса в сжатое имя), которого нет в SPEC.md
    # §4.4 — раздел дополнен поправкой 2026-09-08. Раньше проверялось только «точное совпадение
    # опережает основы слов» (см. тест выше); порядок трёх оставшихся уровней между собой не был
    # закреплён тестом ни разу.
    #
    # Три сущности подобраны так, чтобы каждая проходила ровно через одну ветку _оценить() для
    # общего запроса "Накладная" (подобрано вычислением, см. отчёт):
    #   - "Накладные" — общая основа слова с запросом ("накладн"), ветка основ (50 + n);
    #   - "Ннакладнаяхт" — один регистрозависимый токен без CamelCase-границ: сжатый запрос
    #     "накладная" входит в сжатое имя подстрокой, но как отдельное слово/основа не совпадает
    #     (ветка подстроки, 30 + доля длины);
    #   - "Нкладная" — опечатка (выпала буква "а"): не совпадает ни точно, ни по основе, ни по
    #     подстроке, только по триграммам (доля общих триграмм 0.312 — выше порога 0.2).
    #
    # Убедиться, что тест ловит поломку: поднять оценку ветки подстроки выше ветки основ (или
    # опустить порог триграмм так, чтобы триграммная сущность обогнала подстроку) — тест падает;
    # вернуть как было — тест снова проходит (проверено вручную, см. отчёт).
    основа = _сущность(
        "Document_Накладные",
        "Document",
        "Документ",
        "Накладные",
        [ParsedField(name="Ref_Key", edm_type="Edm.Guid", nullable=False, is_key=True)],
    )
    подстрока = _сущность(
        "Document_Ннакладнаяхт",
        "Document",
        "Документ",
        "Ннакладнаяхт",
        [ParsedField(name="Ref_Key", edm_type="Edm.Guid", nullable=False, is_key=True)],
    )
    триграммы = _сущность(
        "Document_Нкладная",
        "Document",
        "Документ",
        "Нкладная",
        [ParsedField(name="Ref_Key", edm_type="Edm.Guid", nullable=False, is_key=True)],
    )
    репозиторий = IndexRepository(tmp_path / "metadata.sqlite")
    репозиторий.write(
        ParsedMetadata(
            entities=[триграммы, подстрока, основа],  # нарочно не по итоговому порядку
            actions=[],
            edmx_sha256="0" * 64,
        )
    )

    найдено = репозиторий.find("Накладная")
    assert [результат.name for результат in найдено] == [
        "Document_Накладные",
        "Document_Ннакладнаяхт",
        "Document_Нкладная",
    ]
    assert найдено[0].score == 51.0  # основа слова "накладн"
    assert найдено[1].score == 30.45  # подстрока: 30 + 9/20
    assert 0 < найдено[2].score < 30.0  # триграммы, ниже ветки подстроки
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
    assert "Post" in имена_действий


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


def test_класс_поля_возвращает_true_при_попадании(индекс):
    assert индекс.set_field_sensitivity("Catalog_Контрагенты", "ИНН", "inn", source="auto") is True


def test_класс_поля_промах_по_несуществующей_сущности_не_молчит(индекс):
    # Правка по итогам финального ревью M1b (Important, задача 6): раньше промах условия
    # (опечатка в правиле, рассинхронизация имён после обновления индекса) не давал ни ошибки,
    # ни признака — поле в этом случае осталось бы без класса и ушло бы модели в открытом виде,
    # хотя «реальные значения защищаемых классов не выходят никогда» — инвариант, а не тест.
    assert индекс.set_field_sensitivity("Catalog_Нет", "ИНН", "inn", source="auto") is False


def test_класс_поля_промах_по_несуществующему_полю_не_молчит(индекс):
    assert (
        индекс.set_field_sensitivity("Catalog_Контрагенты", "НетТакогоПоля", "inn", source="auto")
        is False
    )


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
    """EDMX с набором, ссылающимся на несуществующий EntityType (см. edmx.py, поле
    ParsedMetadata.unresolved_entity_sets) — отдельно от synthetic.edmx, чтобы не менять
    фикстуру, общую с задачами 1 и 3. Тело контейнера собирает общая обёртка из conftest.py
    (см. правку по итогам финального ревью M1b — та же функция использовалась в двух
    тестовых файлах отдельными копиями)."""
    return обёртка_эдмкс(
        '<EntitySet Name="Catalog_Пропавший" EntityType="StandardODATA.Catalog_Пропавший"/>'
    )


def test_нераспознанные_наборы_попадают_в_служебную_таблицу(tmp_path):
    репозиторий = IndexRepository(tmp_path / "metadata.sqlite")
    репозиторий.write(parse_edmx(_эдмкс_с_нераспознанным_набором()))
    assert json.loads(репозиторий.meta("unresolved_entity_sets")) == ["Catalog_Пропавший"]
    репозиторий.close()


def test_отсутствие_нераспознанных_наборов_не_оставляет_ключ(индекс):
    # synthetic.edmx не содержит испорченных ссылок — ключ не должен появляться в meta.
    assert индекс.meta("unresolved_entity_sets") is None


# Задача 3 плана M1b-fix: набор записей, перечисления, версия разбора — на реальном образце УТ
# (тот же приём, что в test_index_edmx.py для наборов записей и виртуальных таблиц).


@pytest.fixture
def индекс_ut(tmp_path, edmx_ut_real):
    хранилище = IndexRepository(tmp_path / "ut.sqlite")
    хранилище.write(parse_edmx(edmx_ut_real))
    yield хранилище
    хранилище.close()


def test_описание_набора_записей(индекс_ut):
    описание = индекс_ut.describe("InformationRegister_СтоимостьТоваров_RecordType")
    assert описание.is_records is True
    assert описание.parent_entity == "InformationRegister_СтоимостьТоваров"
    assert описание.is_independent_register is False


def test_описание_виртуальной_таблицы_с_параметрами(индекс_ut):
    описание = индекс_ut.describe("AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance")
    assert описание.is_virtual is True
    assert описание.virtual_kind == "Balance"
    assert описание.parent_entity == "AccumulationRegister_РасчетыСКлиентамиПланОплат"
    assert [д["name"] for д in описание.actions] == ["Balance"]
    assert set(описание.actions[0]["params"]) == {"Condition", "Dimensions", "Period"}


def test_виртуальные_таблицы_видны_у_регистра_как_дети(индекс_ut):
    описание = индекс_ut.describe("AccumulationRegister_РасчетыСКлиентамиПланОплат")
    assert "AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance" in описание.children
    assert "AccumulationRegister_РасчетыСКлиентамиПланОплат_RecordType" in описание.children


def test_перечисление_описано_значениями_и_находится_поиском(индекс_ut):
    описание = индекс_ut.describe("Enum_ХозяйственныеОперации")
    assert "ОплатаПоставщику" in описание.members
    assert "Enum_ХозяйственныеОперации" in [
        н.name for н in индекс_ut.find("хозяйственные операции")
    ]


def test_версия_разбора_записана(индекс_ut):
    from odata1c.index.edmx import PARSER_VERSION

    assert индекс_ut.meta("parser_version") == PARSER_VERSION


def test_описание_содержит_навигации(индекс_ut):
    описание = индекс_ut.describe("Document_РеализацияТоваровУслуг")
    assert описание.navigations["Контрагент"] == "Catalog_Контрагенты"
    ключ = next(п for п in описание.fields if п["name"] == "Контрагент_Key")
    assert ключ["ref_targets"] == ["Catalog_Контрагенты"]
