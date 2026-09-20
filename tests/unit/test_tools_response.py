"""Формирование ответа тулов чтения: очистка, страница, усечение, подгонка (SPEC §5.1, §10)."""

import json

import pytest

from odata1c.tools.response import (
    fit_result,
    items_of,
    page_info,
    strip_service,
    truncate_strings,
)

# --- Тесты брифа (дословно) ------------------------------------------------------------------


def test_strip_убирает_служебное():
    сырой = {
        "Ref_Key": "g",
        "DataVersion": "AAA",
        "Контрагент@navigationLinkUrl": "x",
        "odata.type": "t",
        "Фото_Base64Data": "…",
        "Вложенный": {"odata.type": "t", "Code": "1"},
    }
    assert strip_service(сырой) == {"Ref_Key": "g", "Вложенный": {"Code": "1"}}


def test_strip_оставляет_dataversion_по_запросу():
    assert strip_service({"DataVersion": "A"}, keep_data_version=True) == {"DataVersion": "A"}


def test_count_строкой_становится_числом():
    записи, всего = items_of({"odata.count": "1103", "value": [{"a": 1}]})
    assert записи == [{"a": 1}] and всего == 1103


@pytest.mark.parametrize("счётчик", ["", "   ", "неизвестно", None, "²", "١٢٣"])
def test_нечисловой_count_читается_как_неизвестный(счётчик):
    """Минор ревью задачи 3: `int("")` ронял разбор ответа целиком. Публикация 1С счётчик отдаёт
    строкой (проба P4), и пустая строка на месте числа — не повод отвечать `internal`: «сколько
    всего» просто неизвестно, ровно как без `$inlinecount`. Тот же ответ и у `raw_get`, где тело
    приходит от произвольного пути."""
    записи, всего = items_of({"odata.count": счётчик, "value": [{"a": 1}]})
    assert записи == [{"a": 1}]
    assert всего is None


def test_усечение_не_режет_токен():
    строка = "а" * 8 + "[[org:17]]" + "б" * 20
    обрезано, число = truncate_strings({"x": строка}, 12)
    assert обрезано["x"].startswith("а" * 8 + "…")
    assert "[[org:1" not in обрезано["x"].replace("[[org:17]]", "")
    assert число == 1


def test_page_info_по_total_и_без_него():
    assert page_info(count=25, total=1340, top=25, skip=0) == {
        "count": 25,
        "total": 1340,
        "has_more": True,
        "next_skip": 25,
    }
    assert page_info(count=3, total=None, top=25, skip=0)["has_more"] is False


def test_fit_result_выбрасывает_записи_целиком():
    конверт = {
        "items": [{"t": "x" * 100} for _ in range(50)],
        "has_more": False,
        "next_skip": None,
        "count": 50,
        "warnings": [],
    }
    подогнано = fit_result(конверт, 2000)
    assert len(json.dumps(подогнано, ensure_ascii=False)) <= 2000
    assert all(запись == {"t": "x" * 100} for запись in подогнано["items"])
    assert подогнано["has_more"] is True and подогнано["warnings"]


# --- Дополнительные границы ------------------------------------------------------------------


def test_strip_убирает_все_формы_служебных_ключей_и_идёт_в_списки():
    сырой = {
        "odata.metadata": "m",
        "odata.count": "3",
        "odata.nextLink": "n",
        "Файл@odata.mediaReadLink": "u",
        "ДанныеХранилищеЗначения": "b",
        "Товары": [
            {"odata.type": "t", "Номенклатура@navigationLinkUrl": "x", "Количество": 2},
            {"DataVersion": "v", "Цена": 10},
        ],
    }
    assert strip_service(сырой) == {"Товары": [{"Количество": 2}, {"Цена": 10}]}


def test_strip_двоичные_поля_по_суффиксу_а_не_по_вхождению():
    # Раунд правок 1: «ЕстьХранилищеЗначенияКартинки» — законный булев реквизит, вырезать его
    # нельзя; двоичное поле узнаётся по суффиксу, как в gate/masking.py::ДВОИЧНЫЕ_СУФФИКСЫ.
    сырой = {
        "ЕстьХранилищеЗначенияКартинки": True,
        "КартинкаХранилищеЗначения": "b",
        "Фото_Base64Data": "…",
        "Base64DataОписание": "текст",
    }
    assert strip_service(сырой) == {
        "ЕстьХранилищеЗначенияКартинки": True,
        "Base64DataОписание": "текст",
    }


def test_strip_не_трогает_скаляры_и_обычные_ключи():
    assert strip_service("строка") == "строка"
    assert strip_service({"Код": None, "Число": 1.5, "Список": [1, "a"]}) == {
        "Код": None,
        "Число": 1.5,
        "Список": [1, "a"],
    }


def test_items_of_объекта_и_без_count():
    записи, всего = items_of({"Ref_Key": "g", "Code": "1"})
    assert записи == [{"Ref_Key": "g", "Code": "1"}] and всего is None
    записи, всего = items_of({"value": [], "odata.count": 0})
    assert записи == [] and всего == 0


def test_усечение_добавляет_пометку_и_считает_отрезанное():
    обрезано, число = truncate_strings({"a": "x" * 30, "b": "короткая"}, 10)
    assert обрезано["a"] == "x" * 10 + "…[обрезано: 20 симв.]"
    assert обрезано["b"] == "короткая"
    assert число == 1


def test_усечение_идёт_в_списки_и_вложенные_словари_и_не_трогает_ключи():
    данные = {"ключ_длиннее_лимита": [{"v": "y" * 12}, "z" * 12, 7]}
    обрезано, число = truncate_strings(данные, 5)
    assert list(обрезано) == ["ключ_длиннее_лимита"]
    assert обрезано["ключ_длиннее_лимита"][0]["v"].startswith("y" * 5 + "…")
    assert обрезано["ключ_длиннее_лимита"][1].startswith("z" * 5 + "…")
    assert обрезано["ключ_длиннее_лимита"][2] == 7
    assert число == 2


def test_строка_с_токеном_в_пределах_лимита_не_трогается():
    строка = "ИНН [[inn:7K3QX2MZ9P]]"
    обрезано, число = truncate_strings({"x": строка}, len(строка))
    assert обрезано["x"] == строка and число == 0


def test_граница_на_конце_токена_сохраняет_токен_целиком():
    строка = "ab[[org:17]]cd"
    обрезано, _ = truncate_strings({"x": строка}, 12)
    assert обрезано["x"].startswith("ab[[org:17]]…")


def test_граница_внутри_токена_в_начале_строки_даёт_пустой_префикс():
    обрезано, число = truncate_strings({"x": "[[custom:vip:AB12]]xyz"}, 3)
    assert обрезано["x"] == "…[обрезано: 22 симв.]" and число == 1


def test_исходный_объект_усечением_не_меняется():
    данные = {"x": "q" * 20}
    truncate_strings(данные, 5)
    assert данные == {"x": "q" * 20}


def test_page_info_последняя_страница_и_без_total_полная():
    assert page_info(count=15, total=40, top=25, skip=25) == {
        "count": 15,
        "total": 40,
        "has_more": False,
        "next_skip": None,
    }
    assert page_info(count=25, total=None, top=25, skip=50) == {
        "count": 25,
        "total": None,
        "has_more": True,
        "next_skip": 75,
    }


def test_fit_result_пересчитывает_next_skip_и_count_от_оставшихся():
    конверт = {
        "items": [{"t": "x" * 100} for _ in range(25)],
        "has_more": True,
        "next_skip": 75,
        "count": 25,
        "warnings": [],
    }
    подогнано = fit_result(конверт, 1500)
    оставшихся = len(подогнано["items"])
    assert 0 < оставшихся < 25
    assert подогнано["count"] == оставшихся
    assert подогнано["next_skip"] == 50 + оставшихся
    assert подогнано["warnings"] == [
        f"результат усечён до {оставшихся} записей по лимиту result_chars"
    ]


def test_fit_result_берёт_skip_из_аргумента_когда_страница_была_последней():
    конверт = {
        "items": [{"t": "x" * 100} for _ in range(10)],
        "has_more": False,
        "next_skip": None,
        "count": 10,
        "warnings": [],
    }
    подогнано = fit_result(конверт, 600, skip=30)
    assert подогнано["has_more"] is True
    assert подогнано["next_skip"] == 30 + len(подогнано["items"])


def test_fit_result_пропускает_запись_которая_не_помещается_даже_одна():
    # Раунд правок 1: иначе items=[] при next_skip == skip — клиент, листающий по next_skip,
    # зацикливается на той же записи.
    конверт = {
        "items": [{"t": "x" * 500}, {"t": "y"}],
        "has_more": False,
        "next_skip": None,
        "count": 2,
        "warnings": ["прежнее"],
    }
    подогнано = fit_result(конверт, 300, skip=100)
    assert подогнано["items"] == [] and подогнано["count"] == 0
    assert подогнано["has_more"] is True
    assert подогнано["next_skip"] == 101 and подогнано["next_skip"] > 100
    assert подогнано["warnings"] == [
        "прежнее",
        "запись 100 не помещается в лимит result_chars (300 симв.) и пропущена — сузьте select",
    ]
    assert len(json.dumps(подогнано, ensure_ascii=False)) <= 300


def test_fit_result_пропуск_записи_без_skip_считает_смещение_нулём():
    конверт = {
        "items": [{"t": "x" * 500}],
        "has_more": False,
        "next_skip": None,
        "count": 1,
        "warnings": [],
    }
    подогнано = fit_result(конверт, 300)
    assert подогнано["items"] == [] and подогнано["has_more"] is True
    assert подогнано["next_skip"] == 1 and подогнано["next_skip"] > 0
    assert "запись 0 не помещается" in подогнано["warnings"][0]


def test_fit_result_в_пределах_лимита_возвращает_конверт_как_есть():
    конверт = {
        "items": [{"a": 1}],
        "has_more": False,
        "next_skip": None,
        "count": 1,
        "warnings": [],
    }
    подогнано = fit_result(конверт, 10_000)
    assert подогнано == конверт
    assert подогнано is not конверт and подогнано["items"] is not конверт["items"]


def test_fit_result_без_items_не_падает():
    конверт = {"item": {"a": "x" * 100}, "warnings": []}
    assert fit_result(конверт, 50) == конверт


def test_fit_result_считает_длину_без_ascii_экранирования():
    # В ensure_ascii=True каждая кириллическая буква занимает 6 символов — конверт не влез бы.
    конверт = {
        "items": [{"t": "я" * 50}],
        "has_more": False,
        "next_skip": None,
        "count": 1,
        "warnings": [],
    }
    лимит = len(json.dumps(конверт, ensure_ascii=False))
    assert len(json.dumps(конверт)) > лимит  # с экранированием запись бы не влезла
    подогнано = fit_result(конверт, лимит)
    assert подогнано["items"] == [{"t": "я" * 50}] and подогнано["has_more"] is False
