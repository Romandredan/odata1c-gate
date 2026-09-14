"""Библиотека рецептов по конфигурации (SPEC §8, ADR-0011, поправка 2026-09-14, M3 задача 3):
файл-рецепт (`recipes/<config>/<имя>.yaml`, один рецепт на файл), каталог библиотеки и три слоя
`load_layered` — шаблон пакета, библиотека конфигурации, собственный файл базы.

Главное, что здесь проверяется, — рецепт остаётся тем же самым, что и раньше (`Recipe`,
`recipes/model.py`): файл-рецепт не ослабляет проверок `Recipe._проверить`, а только меняет,
откуда рецепт берётся и как его имя связано с файлом.
"""

import pytest

from odata1c.config.models import BaseConfig
from odata1c.recipes.model import (
    RecipeError,
    library_dir,
    load_layered,
    load_library,
    load_recipe_file,
    recipes_path,
    scan_library,
)

URL_UT = "http://localhost/ut/odata/standard.odata/"


def _база(config: str | None = None) -> BaseConfig:
    return BaseConfig(name="ut", label="УТ", url=URL_UT, user="u", config=config)


def test_файл_рецепта_читается_по_имени_файла(tmp_path):
    каталог = library_dir(tmp_path, "ut")
    каталог.mkdir(parents=True)
    (каталог / "stock_by_warehouse.yaml").write_text(
        "title: Остатки по складу\n"
        "entity: AccumulationRegister_ТоварыНаСкладах_Balance\n"
        "select: [Номенклатура_Key]\n",
        encoding="utf-8",
    )

    рецепт = load_recipe_file(каталог / "stock_by_warehouse.yaml")
    assert рецепт.entity == "AccumulationRegister_ТоварыНаСкладах_Balance"
    assert рецепт.title == "Остатки по складу"

    библиотека = load_library(tmp_path, "ut")
    assert set(библиотека) == {"stock_by_warehouse"}
    assert библиотека["stock_by_warehouse"].entity == рецепт.entity


def test_файл_с_ключом_recipes_отклоняется(tmp_path):
    путь = tmp_path / "debtors.yaml"
    путь.write_text(
        "version: 1\nrecipes:\n  debtors:\n    entity: Catalog_Контрагенты\n",
        encoding="utf-8",
    )

    with pytest.raises(RecipeError) as отказ:
        load_recipe_file(путь)

    assert отказ.value.code == "config_invalid"
    assert "один рецепт на файл" in отказ.value.message
    assert str(путь) in отказ.value.message


def test_неверное_имя_файла_отклоняется(tmp_path):
    путь = tmp_path / "Остатки.yaml"
    путь.write_text(
        "entity: Catalog_Контрагенты\nselect: [Ref_Key]\n",
        encoding="utf-8",
    )

    with pytest.raises(RecipeError) as отказ:
        load_recipe_file(путь)

    assert отказ.value.code == "config_invalid"


def test_слои_перекрываются_по_имени(tmp_path):
    """Шаблон пакета УТ (`stock`, `debtors` среди прочих) ← библиотека конфигурации (`stock`) ←
    файл базы (`debtors`): при совпадении имени сильный слой побеждает целиком, имена, которых
    нет ни в библиотеке, ни в файле базы, остаются `template`."""
    home = tmp_path / "home"
    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    (каталог / "stock.yaml").write_text(
        "title: Остатки (библиотека)\n"
        "entity: AccumulationRegister_ТоварыНаСкладах_Balance\n"
        "select: [Номенклатура_Key]\n",
        encoding="utf-8",
    )

    база = _база(config="ut")
    путь_базы = recipes_path(home, база)
    путь_базы.parent.mkdir(parents=True, exist_ok=True)
    путь_базы.write_text(
        "version: 1\n"
        "recipes:\n"
        "  debtors:\n"
        "    title: Долг (база)\n"
        "    entity: AccumulationRegister_РасчетыСКлиентамиПоДокументам_Balance\n"
        "    select: [АналитикаУчетаПоПартнерам_Key]\n",
        encoding="utf-8",
    )

    слои = load_layered(home, база)
    assert слои is not None
    книга, источники = слои

    assert источники["stock"] == "library"
    assert источники["debtors"] == "base"
    остальные = set(книга.recipes) - {"stock", "debtors"}
    assert остальные, "шаблон УТ должен содержать хотя бы один рецепт, кроме stock и debtors"
    for имя in остальные:
        assert источники[имя] == "template"

    assert книга.recipes["stock"].title == "Остатки (библиотека)"
    assert книга.recipes["debtors"].title == "Долг (база)"


def test_база_без_config_видит_только_свой_файл(tmp_path):
    home = tmp_path / "home"
    база = _база(config=None)
    путь_базы = recipes_path(home, база)
    путь_базы.parent.mkdir(parents=True, exist_ok=True)
    путь_базы.write_text(
        "version: 1\n"
        "recipes:\n"
        "  partners:\n"
        "    entity: Catalog_Контрагенты\n"
        "    select: [Ref_Key]\n",
        encoding="utf-8",
    )

    слои = load_layered(home, база)
    assert слои is not None
    книга, источники = слои
    assert set(книга.recipes) == {"partners"}
    assert источники == {"partners": "base"}


def test_ни_файла_ни_config_даёт_None(tmp_path):
    home = tmp_path / "home"
    база = _база(config=None)
    assert load_layered(home, база) is None


def test_библиотека_перечитывается_без_кэша(tmp_path):
    home = tmp_path / "home"
    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    (каталог / "stock.yaml").write_text(
        "entity: AccumulationRegister_ТоварыНаСкладах_Balance\nselect: [Номенклатура_Key]\n",
        encoding="utf-8",
    )

    первый = load_library(home, "ut")
    assert set(первый) == {"stock"}

    (каталог / "debtors.yaml").write_text(
        "entity: AccumulationRegister_РасчетыСКлиентамиПоДокументам_Balance\n"
        "select: [АналитикаУчетаПоПартнерам_Key]\n",
        encoding="utf-8",
    )

    второй = load_library(home, "ut")
    assert set(второй) == {"stock", "debtors"}


def test_load_library_пропускает_негодный_файл_рядом_с_исправным(tmp_path, caplog):
    """Ruling 79 (ревью задачи 3, Major 2): до этой правки первый же негодный файл (здесь —
    имя файла не по маске) уходил `RecipeError`-ом из включения словаря наверх и через
    `load_layered` закрывал `odata1c_recipe` у ВСЕХ баз этой конфигурации разом. Негодный файл
    пропускается молча для вызывающего (`load_library` не бросает исключение) со строкой в
    журнале демона уровня WARNING — путь без содержимого файла."""
    каталог = library_dir(tmp_path, "ut")
    каталог.mkdir(parents=True)
    (каталог / "Остатки.yaml").write_text(
        "entity: Catalog_Контрагенты\nselect: [Ref_Key]\n", encoding="utf-8"
    )
    (каталог / "partners.yaml").write_text(
        "entity: Catalog_Контрагенты\nselect: [Ref_Key]\n", encoding="utf-8"
    )

    with caplog.at_level("WARNING"):
        библиотека = load_library(tmp_path, "ut")

    assert set(библиотека) == {"partners"}
    assert any("Остатки.yaml" in запись.message for запись in caplog.records)


def test_scan_library_различает_кандидатов_и_yml(tmp_path):
    каталог = library_dir(tmp_path, "ut")
    каталог.mkdir(parents=True)
    (каталог / "partners.yaml").write_text("entity: Catalog_Контрагенты\n", encoding="utf-8")
    (каталог / "stock.yml").write_text("entity: Catalog_Контрагенты\n", encoding="utf-8")

    кандидаты, дубликаты, yml_файлы = scan_library(каталог)

    assert set(кандидаты) == {"partners"}
    assert дубликаты == {}
    assert [ф.name for ф in yml_файлы] == ["stock.yml"]


def test_scan_library_каталога_нет_даёт_пустые_результаты(tmp_path):
    кандидаты, дубликаты, yml_файлы = scan_library(library_dir(tmp_path, "ut"))
    assert кандидаты == {}
    assert дубликаты == {}
    assert yml_файлы == []
