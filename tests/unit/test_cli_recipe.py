"""Команды CLI `recipe check <config>` и `recipe list <база>` (SPEC §8, ADR-0011, поправка
2026-09-14, M3 задача 3): проверка библиотеки рецептов по индексу и перечень рецептов базы с
источником каждого имени (шаблон пакета | библиотека конфигурации | файл базы).

`recipe check` разбирает файлы библиотеки, как их разбирал бы демон в `odata1c_recipe` — то же
`load_recipe_file`, — и сверяет имена по индексу тем же способом, что `policy check`
(`gate/policy_check.py::suggest_names`, `IndexRepository.field_names`): два разных разбора одного
вопроса расходятся (Ruling 20 из `recipes/render.py`), поэтому здесь используются ровно те же
функции, а не отдельная копия."""

import pathlib

import pytest

from odata1c.cli import _проверить_рецепт_по_индексу, main
from odata1c.config.loader import load_config
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository
from odata1c.recipes.model import Recipe, RecipeError, library_dir, load_recipe_file

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


def test_recipe_check_ошибка_файла_печатает_сообщение_один_раз(tmp_path, capsys):
    """Minor 1 ревью задачи 3: `load_recipe_file` уже кладёт путь файла в `RecipeError.message` —
    `cmd_recipe_check` не должен приписывать его ещё раз (`error: <путь>: <путь>: …»)."""
    home = _домашний_с_базой(tmp_path)
    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    путь = каталог / "bad.yaml"
    путь.write_text(
        "entity: Catalog_Контрагенты\nparams:\n  q: { type: string }\nselect: ['{q}']\n",
        encoding="utf-8",
    )
    with pytest.raises(RecipeError) as отказ:
        load_recipe_file(путь)
    ожидаемая_строка = f"error: {отказ.value.message}"

    код = main(["recipe", "check", "ut", "--home", str(home)])
    строки = capsys.readouterr().out.strip("\n").splitlines()

    assert код == 1
    assert ожидаемая_строка in строки
    assert строки[строки.index(ожидаемая_строка)].count(str(путь)) == 1


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


# --- Ruling 78 (ревью задачи 3, Major 1): orderby и путь через связь -------------------------


def test_recipe_check_orderby_с_направлением_не_ошибка(tmp_path, capsys, edmx_ut_real):
    """`orderby: Description desc` — рабочий синтаксис (`odata_query.orderby_fields` разбирает
    его так же на исполнении рецепта), а не сырая строка, которая ни с одним полем индекса не
    совпадёт никогда."""
    home = _домашний_с_базой(tmp_path)
    _построить_индекс(home, edmx_ut_real)
    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    (каталог / "partners.yaml").write_text(
        "entity: Catalog_Контрагенты\nselect: [Ref_Key, Description]\norderby: Description desc\n",
        encoding="utf-8",
    )

    код = main(["recipe", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "замечаний нет" in вывод


def test_recipe_check_orderby_список_полей_не_ошибка(tmp_path, capsys, edmx_ut_real):
    """`orderby` списком через запятую — тоже штатный синтаксис."""
    home = _домашний_с_базой(tmp_path)
    _построить_индекс(home, edmx_ut_real)
    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    (каталог / "partners.yaml").write_text(
        "entity: Catalog_Контрагенты\n"
        "select: [Ref_Key, Description, ИНН]\n"
        "orderby: Description, ИНН desc\n",
        encoding="utf-8",
    )

    код = main(["recipe", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "замечаний нет" in вывод


def test_recipe_check_путь_через_связь_предупреждение_не_ошибка(tmp_path, capsys, edmx_ut_real):
    """Поле через связанный объект (`Партнер/Наименование`) не разбирается по навигациям индекса
    здесь — `warning: путь не проверен», а не ложный `error` на рабочем рецепте."""
    home = _домашний_с_базой(tmp_path)
    _построить_индекс(home, edmx_ut_real)
    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    (каталог / "partners.yaml").write_text(
        "entity: Catalog_Контрагенты\n"
        "params:\n"
        "  name: { type: string, required: true }\n"
        "filter: substringof({name}, Партнер/Наименование)\n"
        "select: [Ref_Key]\n",
        encoding="utf-8",
    )

    код = main(["recipe", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "warning" in вывод
    assert "путь не проверен" in вывод
    assert "Партнер/Наименование" in вывод
    assert "error" not in вывод


# --- Ruling 79 (ревью задачи 3, Major 2): изоляция негодных файлов библиотеки -----------------


def test_recipe_check_негодный_файл_не_мешает_проверить_остальные(tmp_path, capsys, edmx_ut_real):
    """Один негодный файл (здесь — неверное имя) не должен останавливать проверку остальных."""
    home = _домашний_с_базой(tmp_path)
    _построить_индекс(home, edmx_ut_real)
    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    (каталог / "Остатки.yaml").write_text(
        "entity: Catalog_Контрагенты\nselect: [Ref_Key]\n", encoding="utf-8"
    )
    (каталог / "partners.yaml").write_text(
        "entity: Catalog_Контрагенты\nselect: [Ref_Key]\n", encoding="utf-8"
    )

    код = main(["recipe", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "Остатки.yaml" in вывод
    assert "не годится как имя рецепта" in вывод
    assert "partners.yaml" not in вывод


def test_recipe_check_yml_расширение_даёт_warning(tmp_path, capsys):
    """`.yml` — расширение, которое демон не читает нигде, но и не должен пропускать молча."""
    home = _домашний_с_базой(tmp_path)
    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    (каталог / "stock.yml").write_text(
        "entity: Catalog_Контрагенты\nselect: [Ref_Key]\n", encoding="utf-8"
    )

    код = main(["recipe", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "warning" in вывод
    assert "расширение .yml не читается" in вывод
    assert "stock.yml" in вывод


def test_recipe_check_пустая_библиотека_warning(tmp_path, capsys):
    """Каталога библиотеки нет вовсе — `warning: библиотека пуста», а не тихое «замечаний нет»
    (в отличие от «замечаний нет» пустая библиотека — это повод проверить путь/config)."""
    home = _домашний_с_базой(tmp_path)

    код = main(["recipe", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "библиотека пуста" in вывод


def test_recipe_check_дубликат_имени_из_за_регистра_расширения(tmp_path, capsys, edmx_ut_real):
    """Два файла на один стем, отличающиеся только регистром расширения (`stock.yaml` /
    `stock.YAML`), — дубликат имени, `error`. На регистронезависимой ФС (Windows, macOS по
    умолчанию) такие два файла физически не сосуществуют — ОС делает их одним файлом, и сценарий
    пропускается, если создать оба не удалось."""
    home = _домашний_с_базой(tmp_path)
    _построить_индекс(home, edmx_ut_real)
    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    (каталог / "stock.yaml").write_text(
        "entity: Catalog_Контрагенты\nselect: [Ref_Key]\n", encoding="utf-8"
    )
    (каталог / "stock.YAML").write_text(
        "entity: Catalog_Контрагенты\nselect: [Ref_Key]\n", encoding="utf-8"
    )
    if len(list(каталог.iterdir())) < 2:
        pytest.skip("файловая система нечувствительна к регистру расширений — сценарий недостижим")

    код = main(["recipe", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "дубликат имени" in вывод
    assert "stock" in вывод


def test_recipe_check_неверное_имя_конфигурации_error(tmp_path, capsys):
    """Аргумент `<config>` проверяется той же маской, что поле `config` (защита в глубину —
    `recipe check ../..` не должен уводить просмотр за пределы домашнего каталога)."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])

    код = main(["recipe", "check", "../etc", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "не подходит как имя конфигурации" in вывод


# --- Ruling 80 (ревью задачи 3): подсказки не называют скрытые сущности -----------------------


def _рецепт_с_опечаткой_в_сущности(home: pathlib.Path) -> None:
    каталог = library_dir(home, "ut")
    каталог.mkdir(parents=True)
    (каталог / "missing.yaml").write_text(
        "entity: Catalog_НетТакой\nselect: [Ref_Key]\n", encoding="utf-8"
    )


def test_recipe_check_подсказка_называет_видимую_сущность(tmp_path, capsys, edmx_ut_real):
    """Контроль к следующему тесту: без правила `hide` подсказка «похожие имена» называет
    Catalog_Контрагенты — иначе следующий тест ничего не доказывает."""
    home = _домашний_с_базой(tmp_path)
    _построить_индекс(home, edmx_ut_real)
    _рецепт_с_опечаткой_в_сущности(home)

    код = main(["recipe", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "Catalog_Контрагенты" in вывод


def test_recipe_check_подсказка_не_называет_скрытую_сущность(tmp_path, capsys, edmx_ut_real):
    """Ruling 80: вывод `recipe check` может попасть в контекст модели (навык запускает его
    через Bash) — подсказка «похожие имена» не должна называть сущность, скрытую владельцем у
    базы, чей индекс используется."""
    home = _домашний_с_базой(tmp_path)
    _построить_индекс(home, edmx_ut_real)
    путь_политики = home / "bases" / "ut" / "policy.yaml"
    путь_политики.parent.mkdir(parents=True, exist_ok=True)
    путь_политики.write_text(
        "version: 2\nscan_free_text: true\nentities:\n  Catalog_Контрагенты: {hide: true}\n",
        encoding="utf-8",
    )
    _рецепт_с_опечаткой_в_сущности(home)

    код = main(["recipe", "check", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "Catalog_Контрагенты" not in вывод


def _индекс_с_сущностью(tmp_path: pathlib.Path, entity: str, fields: set[str]) -> IndexRepository:
    """Минимальный индекс с одной сущностью (ключ `Ref_Key` плюс перечисленные строковые поля) —
    для м-3 итогового ревью M3: подсказка «похожие поля» не должна называть состав сущности,
    скрытой владельцем, а реальный образец `$metadata` здесь не нужен."""
    свойства = "\n".join(
        f'        <Property Name="{поле}" Type="Edm.String" Nullable="true"/>'
        for поле in sorted(fields)
    )
    edmx = f"""<?xml version="1.0" encoding="UTF-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
  <edmx:DataServices m:DataServiceVersion="3.0"
                     xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <Schema Namespace="StandardODATA" xmlns="http://schemas.microsoft.com/ado/2009/11/edm">
      <EntityType Name="{entity}">
        <Key><PropertyRef Name="Ref_Key"/></Key>
        <Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
{свойства}
      </EntityType>
      <EntityContainer Name="StandardODATA" m:IsDefaultEntityContainer="true">
        <EntitySet Name="{entity}" EntityType="StandardODATA.{entity}"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>""".encode()

    репозиторий = IndexRepository(tmp_path / "metadata.sqlite")
    репозиторий.write(parse_edmx(edmx))
    return репозиторий


def test_скрытая_сущность_не_подсказывает_похожие_поля(tmp_path) -> None:
    """м-3 итогового ревью M3, согласованно с Ruling 80. Сущность найдена в индексе, но скрыта
    владельцем: промах по полю печатается (статус проверки не меняется), а «похожие поля» —
    нет, иначе состав скрытой сущности утёк бы через диагностику рецептов.

    Рецепт называет поле «description» (строчными буквами) — опечатка в регистре реального имени
    поля «Description»: `field_names` сравнивает точно, поэтому это промах, а не совпадение, но
    достаточно похожий, чтобы `difflib.get_close_matches` в открытом случае назвал «Description»
    в подсказке."""
    repo = _индекс_с_сущностью(tmp_path, "Catalog_Контрагенты", {"Description", "ИНН", "КПП"})
    try:
        рецепт = Recipe(entity="Catalog_Контрагенты", select=["description"])

        открытая = _проверить_рецепт_по_индексу(repo, tmp_path / "ut.yaml", рецепт)
        скрытая = _проверить_рецепт_по_индексу(
            repo, tmp_path / "ut.yaml", рецепт, hidden=frozenset({"Catalog_Контрагенты"})
        )

        assert "похожие поля: Description" in открытая[0]
        assert len(скрытая) == 1
        assert скрытая[0].startswith("error: ")
        assert "похожие поля" not in скрытая[0]
    finally:
        repo.close()


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
