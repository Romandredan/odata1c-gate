"""Команды CLI `recipe check <config>` и `recipe list <база>` (SPEC §8, ADR-0011, поправка
2026-09-14, M3 задача 3): проверка библиотеки рецептов по индексу и перечень рецептов базы с
источником каждого имени (шаблон пакета | библиотека конфигурации | файл базы).

`recipe check` разбирает файлы библиотеки, как их разбирал бы демон в `odata1c_recipe` — то же
`load_recipe_file`, — и сверяет имена по индексу тем же способом, что `policy check`
(`gate/policy_check.py::suggest_names`, `IndexRepository.field_names`): два разных разбора одного
вопроса расходятся (Ruling 20 из `recipes/render.py`), поэтому здесь используются ровно те же
функции, а не отдельная копия."""

import pathlib

from odata1c.cli import main
from odata1c.config.loader import load_config
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository
from odata1c.recipes.model import library_dir

URL_UT = "http://localhost/ut/odata/standard.odata/"
BASES_UT = f"""
default: ut
bases:
  ut:
    label: УТ 11, тестовая
    url: {URL_UT}
    user: u
    password: p
    role: test
    config: ut
"""


def _домашний_с_базой(tmp_path: pathlib.Path) -> pathlib.Path:
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES_UT, encoding="utf-8")
    return home


def _построить_индекс(home: pathlib.Path, edmx_ut_real: bytes) -> None:
    хранилище = IndexRepository(index_path(home, "ut"))
    хранилище.write(parse_edmx(edmx_ut_real))
    хранилище.close()


def _библиотека_в_стиле_ут(home: pathlib.Path) -> None:
    """Библиотека конфигурации «ut», по одному файлу на рецепт — тот же вид, что у поставляемого
    `templates/recipes/ut.yaml` (справочник по фильтру, регистр-остаток по виртуальной таблице),
    но на сущностях и полях, которые действительно есть в урезанном образце `ut-real.edmx`
    (проба P4): у пакетного шаблона имена сверены с ПОЛНЫМ дампом (`probe.full.edmx`,
    `test_шаблон_ут_сверен_с_полным_дампом` в `test_recipes.py`), а он в git не хранится и здесь
    недоступен."""
    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    (каталог / "partners.yaml").write_text(
        "title: Контрагенты по ИНН\n"
        "entity: Catalog_Контрагенты\n"
        "params:\n"
        "  inn: { type: string, required: true, description: ИНН контрагента }\n"
        "filter: ИНН eq {inn}\n"
        "select: [Ref_Key, Description, ИНН]\n"
        "top: 10\n",
        encoding="utf-8",
    )
    (каталог / "plan.yaml").write_text(
        "title: План оплат клиентов\n"
        "entity: AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance\n"
        "params:\n"
        "  period: { type: datetime, required: true, description: момент остатков }\n"
        "  partner_analytics:\n"
        "    type: guid\n"
        "    required: false\n"
        "    description: Ref_Key аналитики учёта по партнёрам\n"
        "virtual:\n"
        '  Period: "{period}"\n'
        "  Condition:\n"
        "    - АналитикаУчетаПоПартнерам_Key eq {partner_analytics}\n"
        "select: [АналитикаУчетаПоПартнерам_Key, Валюта_Key, КОплатеBalance]\n",
        encoding="utf-8",
    )


# --- recipe check -------------------------------------------------------------------------------


def test_recipe_check_чистый_на_шаблоне_ut(tmp_path, capsys, edmx_ut_real):
    home = _домашний_с_базой(tmp_path)
    _построить_индекс(home, edmx_ut_real)
    _библиотека_в_стиле_ут(home)

    код = main(["recipe", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "замечаний нет" in вывод


def test_recipe_check_ошибка_файла_с_путём(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)
    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    путь = каталог / "bad.yaml"
    путь.write_text(
        "entity: Catalog_Контрагенты\nparams:\n  q: { type: string }\nselect: ['{q}']\n",
        encoding="utf-8",
    )

    код = main(["recipe", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert f"error: {путь}" in вывод


def test_recipe_check_без_индекса_warning(tmp_path, capsys):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    (каталог / "partners.yaml").write_text(
        "entity: Catalog_Контрагенты\nselect: [Ref_Key]\n",
        encoding="utf-8",
    )

    код = main(["recipe", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "warning: имена не проверены: индекса базы с config=ut нет" in вывод


def test_recipe_check_неизвестная_сущность_error(tmp_path, capsys, edmx_ut_real):
    home = _домашний_с_базой(tmp_path)
    _построить_индекс(home, edmx_ut_real)
    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    (каталог / "missing.yaml").write_text(
        "entity: Catalog_НетТакой\nselect: [Ref_Key]\n",
        encoding="utf-8",
    )

    код = main(["recipe", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "Catalog_НетТакой" in вывод
    assert "не найдена в индексе" in вывод
    assert "похожие имена" in вывод
    assert "Catalog_Контрагенты" in вывод


# --- recipe list ----------------------------------------------------------------------------


def test_recipe_list_печатает_источник(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)

    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    (каталог / "stock.yaml").write_text(
        "title: Остатки (библиотека)\n"
        "entity: AccumulationRegister_ТоварыНаСкладах_Balance\n"
        "select: [Номенклатура_Key]\n",
        encoding="utf-8",
    )

    путь_базы = home / "bases" / "ut" / "recipes.yaml"
    путь_базы.parent.mkdir(parents=True, exist_ok=True)
    путь_базы.write_text(
        "version: 1\n"
        "recipes:\n"
        "  partners:\n"
        "    title: Контрагенты (база)\n"
        "    entity: Catalog_Контрагенты\n"
        "    select: [Ref_Key]\n",
        encoding="utf-8",
    )

    код = main(["recipe", "list", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out
    assert код == 0

    строки = {
        часть[0]: часть
        for часть in (строка.split("  ") for строка in вывод.strip("\n").splitlines())
    }
    assert строки["stock"][1] == "library"
    assert строки["stock"][2] == "Остатки (библиотека)"
    assert строки["partners"][1] == "base"
    assert строки["partners"][2] == "Контрагенты (база)"
    # рецепт, которого нет ни в библиотеке, ни в файле базы, — из шаблона пакета
    assert строки["debtors"][1] == "template"


# --- base add --recipes ----------------------------------------------------------------------


def test_base_add_recipes_пишет_config(tmp_path, monkeypatch):
    """`base add --recipes ut` пишет `config: ut` (новое, M3 задача 3) И, КАК ПРЕЖДЕ, копирует
    шаблон пакета в `recipes.yaml` базы (design §4b, строка 130 — владелец явно сохранил прежнее
    поведение команды): пропавшая копия — регресс, который эта проверка и ловит."""
    home = tmp_path / "home"
    ответы = iter(["http://localhost/x/odata/standard.odata/", "Подпись", "пользователь"])
    monkeypatch.setattr("builtins.input", lambda *_: next(ответы))
    monkeypatch.setattr("getpass.getpass", lambda *_: "пароль")

    код = main(["base", "add", "ut", "--recipes", "ut", "--home", str(home)])
    assert код == 0

    текст = (home / "bases.yaml").read_text(encoding="utf-8")
    assert "config: ut" in текст
    config = load_config(home)
    assert config.bases["ut"].config == "ut"

    файл_рецептов = home / "bases" / "ut" / "recipes.yaml"
    assert файл_рецептов.exists()
    содержимое = файл_рецептов.read_text(encoding="utf-8")
    assert "debtors" in содержимое  # рецепт пакетного шаблона ut.yaml, скопирован как раньше
