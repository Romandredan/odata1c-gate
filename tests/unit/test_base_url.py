"""Адрес базы в bases.yaml: корень публикации 1С, хвост `/odata/standard.odata/` шлюз достраивает
сам (SPEC §3.1, Ruling 106)."""

import pytest

from odata1c.config.models import BaseConfig, адрес_odata, корень_публикации


@pytest.mark.parametrize(
    ("введено", "корень"),
    [
        ("https://server/base", "https://server/base"),
        ("https://server/base/", "https://server/base"),
        ("  https://server/base  ", "https://server/base"),
        ("https://server/base/odata/standard.odata/", "https://server/base"),
        ("https://server/base/odata/standard.odata", "https://server/base"),
        ("https://server/base/OData/Standard.odata/", "https://server/base"),
        # адресная строка веб-клиента: язык интерфейса и всё, что после него
        ("https://server/base/ru_RU/", "https://server/base"),
        ("https://server/base/en_US", "https://server/base"),
        ("https://server/base/ru_RU/#e1cib/list/Справочник.Номенклатура", "https://server/base"),
        ("https://server/base/ru_RU/e1cib/list/Справочник.Номенклатура", "https://server/base"),
        # публикация в корне сайта (псевдоним в DNS) и многоуровневый путь
        ("https://ut.corp.ru", "https://ut.corp.ru"),
        ("https://ut.corp.ru/", "https://ut.corp.ru"),
        ("https://ut.corp.ru/odata/standard.odata/", "https://ut.corp.ru"),
        ("https://1c.corp.ru/prod/ut", "https://1c.corp.ru/prod/ut"),
        ("HTTP://server:8080/base", "http://server:8080/base"),
        ("http://localhost/УТ_тест/", "http://localhost/УТ_тест"),
        # учётные данные в адресе — сценарий импорта из env-файла, адрес сохраняется как есть
        ("https://имя:пароль@1c.corp.local/ut/", "https://имя:пароль@1c.corp.local/ut"),
        # имя публикации из двух букв — не язык интерфейса
        ("https://server/ut", "https://server/ut"),
    ],
)
def test_корень_публикации(введено, корень):
    assert корень_публикации(введено) == корень
    assert адрес_odata(введено) == корень + "/odata/standard.odata/"


@pytest.mark.parametrize(
    ("введено", "в_тексте"),
    [
        ("server/base", "http"),
        ("localhost:8080/base", "http"),
        ("ftp://server/base", "http"),
        ("https:///base", "http"),
        ("https://server/base?N=user", "?"),
        ("https://server/base/odata/", "odata/standard.odata"),
        ("https://server/base/odata/standard.odata/Catalog_Номенклатура", "odata/standard.odata"),
        ("https://server/base/hs/api/v1", "hs"),
        ("https://server/base/ws/exchange", "ws"),
        ("https://server/base/e1cib/list/Справочник.Номенклатура", "e1cib"),
    ],
)
def test_не_корень_публикации_отклоняется_без_повтора_значения(введено, в_тексте):
    with pytest.raises(ValueError) as ошибка:
        корень_публикации(введено)
    текст = str(ошибка.value)
    assert в_тексте in текст
    assert "server/base" not in текст and "localhost" not in текст


def test_запись_базы_хранит_полный_адрес_odata():
    база = BaseConfig(name="ut", label="УТ", url="https://server/base/ru_RU/", user="u")
    assert база.url == "https://server/base/odata/standard.odata/"


def test_полный_адрес_в_записи_базы_не_меняется():
    полный = "https://1c.corp.local/ut/odata/standard.odata/"
    база = BaseConfig(name="ut", label="УТ", url=полный, user="u")
    assert база.url == полный
