"""Рецепты (SPEC §8, план M1d задача 8): чтение `recipes.yaml`, рендеринг в аргументы выборки
и тул `odata1c_recipe`.

Главное, что здесь проверяется, — рецепт остаётся ДАННЫМИ: значение параметра попадает в запрос
только литералом, условие через него не внедряется, а раскрытое из токена значение не выходит
наружу даже в тексте ошибки.
"""

import json
import pathlib
import urllib.parse

import httpx
import pytest
import respx
from conftest import ЗНАЧЕНИЯ_КЛАССОВ, без_навигаций, эхо_отбора

from odata1c.cli import main
from odata1c.config.loader import load_config
from odata1c.config.models import BaseConfig
from odata1c.gate.service import refresh_policy
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository
from odata1c.recipes.model import RecipeError, load_recipes, recipes_path
from odata1c.recipes.render import render
from odata1c.registry.registry import SessionScope
from odata1c.tools.odata_query import orderby_fields
from odata1c.tools.service import ToolService

ПОЛНЫЙ_ДАМП = pathlib.Path(__file__).parent.parent / "fixtures" / "edmx" / "probe.full.edmx"
ШАБЛОН_УТ = (
    pathlib.Path(__file__).parent.parent.parent
    / "src"
    / "odata1c"
    / "templates"
    / "recipes"
    / "ut.yaml"
)

URL_UT = "http://localhost/ut/odata/standard.odata/"
ИНН = "7707083893"

# Класс гейта → реальное значение. Перебор ПО ВСЕМ классам, а не по одному ИНН: прошлый сторож
# утечки был построен на ИНН — единственном классе, у которого есть и детектор, и контрольная
# сумма, и цифровая серия в страже, — и дыру в `addr`/`dob` не заметил (C1 ревью 2026-09-11).
# Набор общий на все параметризованные проверки — он переехал в `conftest.py`, когда тот же
# перебор по классам понадобился стражу раскрытых значений (задача N1 M1d).

BASES_YAML = f"""
default: ut
bases:
  ut:
    label: УТ 11, тестовая
    url: {URL_UT}
    user: u
    password: p
    role: prod
"""

РЕЦЕПТЫ_ТЕСТА = """
version: 1
recipes:
  partners:
    title: Контрагенты по ИНН
    description: Поиск контрагента по идентификатору
    entity: Catalog_Контрагенты
    params:
      inn: { type: string, required: true, description: ИНН контрагента }
    filter: ИНН eq {inn}
    select: [Ref_Key, Description, ИНН]
    top: 10
  plan:
    title: План оплат клиентов
    entity: AccumulationRegister_РасчетыСКлиентамиПланОплат_Balance
    params:
      period: { type: datetime, required: true, description: момент остатков }
      currency: { type: guid, required: false, description: Ref_Key валюты }
    virtual:
      Period: "{period}"
      Condition:
        - Валюта_Key eq {currency}
    select: [ОбъектРасчетов_Key, Валюта_Key, КОплатеBalance]
  missing:
    title: Рецепт для чужой конфигурации
    entity: AccumulationRegister_ЭтогоРегистраЗдесьНет_Balance
    params:
      period: { type: datetime, required: true }
    virtual:
      Period: "{period}"
"""


def книга(tmp_path: pathlib.Path, текст: str):
    путь = tmp_path / "recipes.yaml"
    путь.write_text(текст, encoding="utf-8")
    return load_recipes(путь)


def рецепт(tmp_path: pathlib.Path, тело: str, имя: str = "проба"):
    """Одна запись рецепта из куска YAML — чтобы каждый тест читал ровно то, что проверяет."""
    return книга(tmp_path, f"version: 1\nrecipes:\n  {имя}:\n{тело}").recipes[имя]


# ---------------------------------------------------------------------------------------------
# Рендеринг: литералы по типам, необязательные параметры, экранирование
# ---------------------------------------------------------------------------------------------


def test_литерал_по_типу_для_каждого_типа_параметра(tmp_path):
    р = рецепт(
        tmp_path,
        """    entity: Catalog_Проба
    params:
      д: { type: datetime }
      дата: { type: date }
      г: { type: guid }
      с: { type: string }
      ц: { type: int }
      дроб: { type: decimal }
      б: { type: bool }
    filter:
      - Момент eq {д}
      - День eq {дата}
      - Ссылка_Key eq {г}
      - Наименование eq {с}
      - Количество eq {ц}
      - Сумма eq {дроб}
      - Проведен eq {б}
""",
    )
    аргументы = render(
        р,
        {
            "д": "2026-01-01T12:30:00",
            "дата": "2026-01-01",
            "г": "a103cb54-42ee-11ec-a7a0-f10ab59a067e",
            "с": "Ромашка",
            "ц": 5,
            "дроб": "10.5",
            "б": True,
        },
    )
    assert аргументы.filter == (
        "Момент eq datetime'2026-01-01T12:30:00' and День eq datetime'2026-01-01T00:00:00' "
        "and Ссылка_Key eq guid'a103cb54-42ee-11ec-a7a0-f10ab59a067e' "
        "and Наименование eq 'Ромашка' and Количество eq 5 and Сумма eq 10.5 "
        "and Проведен eq true"
    )


def test_незаданный_необязательный_параметр_убирает_условие(tmp_path):
    р = рецепт(
        tmp_path,
        """    entity: Catalog_Проба
    params:
      инн: { type: string }
      город: { type: string }
    filter:
      - ИНН eq {инн}
      - Город eq {город}
""",
    )
    assert render(р, {"инн": "7707083893"}).filter == "ИНН eq '7707083893'"
    assert render(р, {}).filter is None


def test_обязательный_параметр_без_значения_отклоняется(tmp_path):
    р = рецепт(
        tmp_path,
        """    entity: Catalog_Проба
    params:
      период: { type: datetime, required: true, description: момент остатков }
    filter: Момент eq {период}
""",
    )
    with pytest.raises(RecipeError) as отказ:
        render(р, {})
    assert отказ.value.code == "recipe_param"
    assert "период" in отказ.value.message
    assert "момент остатков" in отказ.value.hint


def test_параметр_со_значением_none_считается_незаданным(tmp_path):
    р = рецепт(
        tmp_path,
        """    entity: Catalog_Проба
    params:
      город: { type: string }
    filter: Город eq {город}
""",
    )
    assert render(р, {"город": None}).filter is None
    # Пустая строка — осознанно переданное значение, а не отсутствие параметра.
    assert render(р, {"город": ""}).filter == "Город eq ''"


def test_неизвестный_параметр_отклоняется(tmp_path):
    р = рецепт(
        tmp_path,
        """    entity: Catalog_Проба
    params:
      город: { type: string }
    filter: Город eq {город}
""",
    )
    with pytest.raises(RecipeError) as отказ:
        render(р, {"регион": "Москва"})
    assert отказ.value.code == "recipe_param"
    assert "регион" in отказ.value.message


def test_значение_негодного_формата_отклоняется(tmp_path):
    р = рецепт(
        tmp_path,
        """    entity: Catalog_Проба
    params:
      ссылка: { type: guid, required: true }
    filter: Ссылка_Key eq {ссылка}
""",
    )
    with pytest.raises(RecipeError) as отказ:
        render(р, {"ссылка": "не-guid"})
    assert отказ.value.code == "recipe_param"


def test_строка_не_внедряет_условие_в_отбор(tmp_path):
    """Классическая попытка внедрения: значение, которое «закрывает» литерал и дописывает своё
    условие. Значение подставляется уже литералом с удвоением внутренних кавычек, поэтому оно
    остаётся ОДНОЙ строкой, а не вторым условием."""
    р = рецепт(
        tmp_path,
        """    entity: Catalog_Проба
    params:
      имя: { type: string, required: true }
    filter: Наименование eq {имя}
""",
    )
    аргументы = render(р, {"имя": "' or 1 eq 1 or Наименование eq '"})
    assert аргументы.filter == ("Наименование eq ''' or 1 eq 1 or Наименование eq '''")
    # Условие ровно одно: внедрённого «or» на верхнем уровне выражения нет.
    вне_литерала = "".join(аргументы.filter.split("'")[::2])
    assert " or " not in вне_литерала
    assert вне_литерала.count(" eq ") == 1


def test_подстановка_идёт_одним_проходом(tmp_path):
    """Значение одного параметра, похожее на подстановку другого, второй раз не разбирается:
    иначе текст из ответа 1С или из промпта мог бы дотянуться до чужого значения."""
    р = рецепт(
        tmp_path,
        """    entity: Catalog_Проба
    params:
      первый: { type: string, required: true }
      второй: { type: string, required: true }
    filter:
      - Наименование eq {первый}
      - Комментарий eq {второй}
""",
    )
    аргументы = render(р, {"первый": "{второй}", "второй": "секрет"})
    assert аргументы.filter == "Наименование eq '{второй}' and Комментарий eq 'секрет'"


def test_параметры_виртуальной_таблицы_передаются_сырыми(tmp_path):
    """`Period` и подобные оформляет литералом сам построитель запроса по типу из `$metadata` —
    рецепт отдаёт их значением, иначе литерал обернулся бы дважды. `Condition` — наоборот,
    готовое выражение."""
    р = рецепт(
        tmp_path,
        """    entity: AccumulationRegister_Проба_Balance
    params:
      период: { type: datetime, required: true }
      склад: { type: guid }
    virtual:
      Period: "{период}"
      Condition:
        - Склад_Key eq {склад}
""",
    )
    полные = render(р, {"период": "2026-01-01", "склад": "a103cb54-42ee-11ec-a7a0-f10ab59a067e"})
    assert полные.params == {
        "Period": "2026-01-01",
        "Condition": "Склад_Key eq guid'a103cb54-42ee-11ec-a7a0-f10ab59a067e'",
    }
    только_период = render(р, {"период": "2026-01-01"})
    assert только_период.params == {"Period": "2026-01-01"}


def test_незаданный_параметр_виртуальной_таблицы_не_передаётся(tmp_path):
    р = рецепт(
        tmp_path,
        """    entity: AccumulationRegister_Проба_Turnovers
    params:
      начало: { type: datetime, required: true }
      разрез: { type: string }
    virtual:
      StartPeriod: "{начало}"
      Dimensions: "{разрез}"
""",
    )
    assert render(р, {"начало": "2026-01-01"}).params == {"StartPeriod": "2026-01-01"}


# ---------------------------------------------------------------------------------------------
# Чтение файла: что рецепту делать нельзя
# ---------------------------------------------------------------------------------------------


def test_подстановка_внутри_литерала_отклоняется(tmp_path):
    """Форма из примера SPEC §8 (`guid'{organization}'`) не принимается: значение подставляется
    уже литералом, а разрешение писать его внутрь кавычек вернуло бы строковую интерполяцию."""
    with pytest.raises(RecipeError) as отказ:
        рецепт(
            tmp_path,
            """    entity: Catalog_Проба
    params:
      орг: { type: guid }
    filter: "Организация_Key eq guid'{орг}'"
""",
        )
    assert отказ.value.code == "config_invalid"
    assert "литерал" in отказ.value.message


@pytest.mark.parametrize(
    "тело",
    [
        """    entity: "Catalog_{имя}"
    params:
      имя: { type: string }
    filter: Наименование eq {имя}
""",
        """    entity: Catalog_Проба
    params:
      имя: { type: string }
    select: ["{имя}"]
    filter: Наименование eq {имя}
""",
        """    entity: Catalog_Проба
    params:
      имя: { type: string }
    orderby: "{имя}"
    filter: Наименование eq {имя}
""",
    ],
    ids=["entity", "select", "orderby"],
)
def test_подстановка_в_имени_отклоняется(tmp_path, тело):
    """Параметризовать можно значение, но не имя: сущность, поле и порядок сортировки идут в
    запрос текстом, мимо `odata_literal`."""
    with pytest.raises(RecipeError) as отказ:
        рецепт(tmp_path, тело)
    assert отказ.value.code == "config_invalid"


def test_подстановка_необъявленного_параметра_отклоняется(tmp_path):
    with pytest.raises(RecipeError) as отказ:
        рецепт(
            tmp_path,
            """    entity: Catalog_Проба
    filter: Наименование eq {имя}
""",
        )
    assert отказ.value.code == "config_invalid"
    assert "имя" in отказ.value.message


def test_параметр_с_двумя_разными_полями_отклоняется(tmp_path):
    """У поля свой класс защиты, и от него зависят проверки обратной подмены: один параметр,
    сравниваемый то с ИНН, то с наименованием, получил бы проверки чужого поля."""
    with pytest.raises(RecipeError) as отказ:
        рецепт(
            tmp_path,
            """    entity: Catalog_Проба
    params:
      значение: { type: string }
    filter:
      - ИНН eq {значение}
      - Наименование eq {значение}
""",
        )
    assert отказ.value.code == "config_invalid"
    assert "сравнивается по-разному" in отказ.value.message


def test_условие_без_видимого_поля_отклоняется(tmp_path):
    with pytest.raises(RecipeError) as отказ:
        рецепт(
            tmp_path,
            """    entity: Catalog_Проба
    params:
      порог: { type: int }
    filter: "{порог} eq {порог}"
""",
        )
    assert отказ.value.code == "config_invalid"


def test_поле_и_вид_сравнения_параметра_опознаются(tmp_path):
    р = рецепт(
        tmp_path,
        """    entity: Catalog_Проба
    params:
      инн: { type: string }
      кусок: { type: string }
      начало: { type: string }
      с_даты: { type: datetime }
    filter:
      - ИНН eq {инн}
      - substringof({кусок}, НаименованиеПолное)
      - startswith(Комментарий, {начало})
      - Дата ge {с_даты}
""",
    )
    assert (р.param_field("инн"), р.param_comparison("инн")) == ("ИНН", "eq")
    assert (р.param_field("кусок"), р.param_comparison("кусок")) == (
        "НаименованиеПолное",
        "substring",
    )
    assert (р.param_field("начало"), р.param_comparison("начало")) == ("Комментарий", "substring")
    assert (р.param_field("с_даты"), р.param_comparison("с_даты")) == ("Дата", "ordered")


def test_параметр_с_разным_видом_сравнения_отклоняется(tmp_path):
    with pytest.raises(RecipeError) as отказ:
        рецепт(
            tmp_path,
            """    entity: Catalog_Проба
    params:
      значение: { type: string }
    filter:
      - ИНН eq {значение}
      - substringof({значение}, ИНН)
""",
        )
    assert отказ.value.code == "config_invalid"


def test_битый_файл_рецептов_config_invalid(tmp_path):
    путь = tmp_path / "recipes.yaml"
    путь.write_text("version: 1\nrecipes:\n  - не словарь\n   плохой отступ\n", encoding="utf-8")
    with pytest.raises(RecipeError) as отказ:
        load_recipes(путь)
    assert отказ.value.code == "config_invalid"


def test_отсутствующий_файл_рецептов_config_invalid(tmp_path):
    with pytest.raises(RecipeError) as отказ:
        load_recipes(tmp_path / "нет.yaml")
    assert отказ.value.code == "config_invalid"


def test_путь_рецептов_по_умолчанию_и_по_настройке(tmp_path):
    база = BaseConfig(name="ut", label="УТ", url=URL_UT, user="u")
    assert recipes_path(tmp_path, база) == tmp_path / "bases" / "ut" / "recipes.yaml"
    своя = база.model_copy(update={"recipes": "рецепты/ут.yaml"})
    assert recipes_path(tmp_path, своя) == tmp_path / "рецепты" / "ут.yaml"
    абсолютная = база.model_copy(update={"recipes": str(tmp_path / "где-то" / "r.yaml")})
    assert recipes_path(tmp_path, абсолютная) == tmp_path / "где-то" / "r.yaml"


# ---------------------------------------------------------------------------------------------
# Тул odata1c_recipe: список, выполнение, гейт
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def дом(tmp_path, edmx_ut_real):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES_YAML, encoding="utf-8")
    config = load_config(home)
    хранилище = IndexRepository(index_path(home, "ut"))
    хранилище.write(parse_edmx(edmx_ut_real))
    хранилище.close()
    refresh_policy(home, config.bases["ut"])
    (home / "bases" / "ut" / "recipes.yaml").write_text(РЕЦЕПТЫ_ТЕСТА, encoding="utf-8")
    return home


@pytest.fixture
async def сервис(дом):
    служба = ToolService(load_config(дом))
    yield служба
    await служба.aclose()


@pytest.fixture
def respx_ut():
    with respx.mock(base_url=URL_UT, assert_all_called=False) as router:
        router.get(URL_UT).mock(return_value=httpx.Response(200, json={"value": []}))
        yield router


async def токен_инн(служба: ToolService, инн: str) -> str:
    гейт = служба._gate_for(служба._config.bases["ut"])
    return гейт.mask({"ИНН": инн}, entity="Catalog_Контрагенты", resolve=без_навигаций).data["ИНН"]


async def _токен_класса(служба: ToolService, класс: str) -> tuple[str, str]:
    """Токен произвольного класса — такой же, какой модель получила бы в ответе от поля этого
    класса. Выдаётся словарём того же гейта, поэтому обратная подмена и страж знают о нём ровно
    то же, что и о токене из настоящего ответа."""
    гейт = служба._gate_for(служба._config.bases["ut"])
    значение = ЗНАЧЕНИЯ_КЛАССОВ[класс]
    токен = гейт._dictionary.token_for(
        класс, значение, base="ut", entity="Catalog_ФизическиеЛица", field=f"Поле{класс}"
    )
    return токен, значение


def _адрес(router) -> str:
    """Адрес последнего запроса к 1С в читаемом виде: кириллица и кавычки в нём закодированы
    процентами (это делает httpx), а проверять удобнее исходный текст."""
    return urllib.parse.unquote(str(router.calls.last.request.url))


async def test_список_рецептов_помечает_неприменимый(сервис):
    ответ = json.loads(await сервис.recipe(SessionScope()))
    по_имени = {строка["name"]: строка for строка in ответ["recipes"]}
    assert по_имени["partners"]["applicable"] is True
    assert по_имени["partners"]["params"] == [
        {"name": "inn", "type": "string", "required": True, "description": "ИНН контрагента"}
    ]
    assert по_имени["missing"]["applicable"] is False
    assert по_имени["missing"]["hint"]


async def test_список_на_непроиндексированной_базе_не_отказывает(сервис, дом):
    """Список рецептов лежит в файле, а не в индексе: на свежей базе он должен читаться, просто
    без пометки применимости."""
    index_path(дом, "ut").unlink()
    ответ = json.loads(await сервис.recipe(SessionScope()))
    assert {строка["name"] for строка in ответ["recipes"]} == {"partners", "plan", "missing"}
    assert all(строка["applicable"] is None for строка in ответ["recipes"])
    assert "не проиндексирована" in ответ["hint"]


async def test_рецепт_с_параметрами_на_обычной_сущности_неприменим(сервис, дом):
    """`virtual` у невиртуальной сущности — рецепт, который не выполнится никогда: список обязан
    сказать об этом сразу, а не отдать отказ построителя запроса при вызове."""
    (дом / "bases" / "ut" / "recipes.yaml").write_text(
        """
version: 1
recipes:
  кривой:
    title: Параметры таблицы у справочника
    entity: Catalog_Контрагенты
    params:
      период: { type: datetime, required: true }
    virtual:
      Period: "{период}"
""",
        encoding="utf-8",
    )
    ответ = json.loads(await сервис.recipe(SessionScope()))
    assert ответ["recipes"][0]["applicable"] is False
    assert "не виртуальная таблица" in ответ["recipes"][0]["hint"]


async def test_список_без_файла_рецептов_даёт_подсказку(сервис, дом):
    (дом / "bases" / "ut" / "recipes.yaml").unlink()
    ответ = json.loads(await сервис.recipe(SessionScope()))
    assert ответ["recipes"] == []
    assert "recipes.yaml" in ответ["hint"]


async def test_неизвестный_рецепт_отвечает_recipe_unknown(сервис):
    ответ = json.loads(await сервис.recipe(SessionScope(), name="нет-такого"))
    assert ответ["error"]["code"] == "recipe_unknown"
    assert "partners" in ответ["error"]["hint"]


async def test_рецепт_выполняется_и_маскирует_ответ(сервис, respx_ut):
    respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "Ref_Key": "a103cb54-42ee-11ec-a7a0-f10ab59a067e",
                        "Description": "ООО Ромашка",
                        "ИНН": ИНН,
                    }
                ]
            },
        )
    )
    ответ = json.loads(await сервис.recipe(SessionScope(), name="partners", params={"inn": ИНН}))
    assert ответ["recipe"] == "partners"
    assert ответ["title"] == "Контрагенты по ИНН"
    assert ответ["entity"] == "Catalog_Контрагенты"
    assert ИНН not in json.dumps(ответ, ensure_ascii=False)
    assert ответ["items"][0]["ИНН"].startswith("[[inn:")
    assert f"$filter=ИНН eq '{ИНН}'" in _адрес(respx_ut)


async def test_токен_в_параметре_подставляется_реальным_значением(сервис, respx_ut):
    токен = await токен_инн(сервис, ИНН)
    respx_ut.get("Catalog_Контрагенты").mock(return_value=httpx.Response(200, json={"value": []}))
    ответ = json.loads(await сервис.recipe(SessionScope(), name="partners", params={"inn": токен}))
    assert "error" not in ответ
    # В 1С ушло реальное значение…
    assert f"ИНН eq '{ИНН}'" in _адрес(respx_ut)
    # …а наружу оно не вернулось ни в каком виде.
    assert ИНН not in json.dumps(ответ, ensure_ascii=False)


РЕЦЕПТ_АДРЕСА = """
version: 1
recipes:
  адреса:
    title: Физлица по адресу регистрации
    entity: Catalog_ФизическиеЛица
    params:
      адрес: { type: string, required: true, description: адрес регистрации }
    filter: АдресРегистрации eq {адрес}
    select: [Ref_Key, Description]
"""


async def test_эхо_отбора_в_ошибке_1С_не_выносит_раскрытое_рецептом(сервис, дом, respx_ut):
    """Задача N1 M1d: параметр рецепта — такой же вход обратной подмены, как `$filter` у
    `query` (`_значения_рецепта` → `inbound_value`), и раскрытое им значение обязано дойти до
    стража тем же набором. Класс `addr` — тот, у которого другого рубежа нет вовсе."""
    (дом / "bases" / "ut" / "recipes.yaml").write_text(РЕЦЕПТ_АДРЕСА, encoding="utf-8")
    значение = ЗНАЧЕНИЯ_КЛАССОВ["addr"]
    гейт = сервис._gate_for(сервис._config.bases["ut"])
    токен = гейт._dictionary.token_for(
        "addr",
        значение,
        base="ut",
        entity="Catalog_ФизическиеЛица",
        field="АдресРегистрации",
    )
    маршрут = respx_ut.get("Catalog_ФизическиеЛица").mock(side_effect=эхо_отбора)

    ответ = json.loads(await сервис.recipe(SessionScope(), name="адреса", params={"адрес": токен}))

    assert значение in маршрут.calls.last.request.url.params["$filter"]
    assert ответ["error"]["code"] == "odata_error"
    assert значение not in json.dumps(ответ, ensure_ascii=False)
    assert токен in ответ["error"]["message"]


@pytest.mark.parametrize("класс", sorted(ЗНАЧЕНИЯ_КЛАССОВ))
@pytest.mark.parametrize("параметр", ["period", "currency"])
async def test_рецепт_не_работает_дешифратором_токена(сервис, respx_ut, класс, параметр):
    """C1 ревью 2026-09-11: рецепт раскрывал токен ЛЮБОГО класса и возвращал реальное значение
    в тексте ошибки построителя литерала — один вызов, без обращения к 1С.

    Два входа: параметр, который ни с чем не сравнивается (`period` — так устроены все шесть
    поставляемых рецептов УТ), и параметр, сравниваемый со ссылочным ключом (`currency` ↔
    `Валюта_Key`), у которого класса поля нет по инварианту 6. Прошлый сторож брал только ИНН —
    класс с тремя независимыми страховками; `addr` и `dob` он бы не поймал."""
    маршрут = respx_ut.route(method="GET").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    токен, значение = await _токен_класса(сервис, класс)
    вызов = {"period": "2026-01-01", "currency": None} | {параметр: токен}

    ответ = json.loads(
        await сервис.recipe(
            SessionScope(),
            name="plan",
            params={имя: з for имя, з in вызов.items() if з is not None},
        )
    )

    assert ответ["error"]["code"] == "token_type_mismatch"
    assert значение not in json.dumps(ответ, ensure_ascii=False)
    assert not маршрут.called


async def test_текст_ошибки_литерала_не_повторяет_значение(сервис):
    """Ruling 20, пункт 2: текст отказа строится из имени параметра и типа, а не из значения —
    иначе инвариант 1 держится только на том, умеет ли страж собрать конкретный класс обратно."""
    ответ = json.loads(
        await сервис.recipe(SessionScope(), name="plan", params={"period": "не-дата-вовсе-2026"})
    )
    assert ответ["error"]["code"] == "recipe_param"
    assert "не-дата-вовсе-2026" not in json.dumps(ответ, ensure_ascii=False)
    assert "period" in ответ["error"]["message"]


async def test_рецепт_виртуальной_таблицы_строит_путь_и_условие(сервис, respx_ut):
    respx_ut.route(method="GET").mock(return_value=httpx.Response(200, json={"value": []}))
    ответ = json.loads(
        await сервис.recipe(
            SessionScope(),
            name="plan",
            params={"period": "2026-01-01", "currency": "a103cb54-42ee-11ec-a7a0-f10ab59a067e"},
        )
    )
    assert "error" not in ответ
    адрес = _адрес(respx_ut)
    assert "AccumulationRegister_РасчетыСКлиентамиПланОплат/Balance(" in адрес
    assert "Period=datetime'2026-01-01T00:00:00'" in адрес
    # Condition — аргумент вызова виртуальной таблицы, то есть строковый литерал: кавычки внутри
    # него удвоены построителем запроса, ровно как у обычного query с params={"Condition": …}.
    assert "Condition='Валюта_Key eq guid''a103cb54-42ee-11ec-a7a0-f10ab59a067e'''" in адрес


async def test_сортировка_по_защищаемому_полю_в_рецепте_запрещена(сервис, дом):
    (дом / "bases" / "ut" / "recipes.yaml").write_text(
        """
version: 1
recipes:
  bad:
    title: Контрагенты по ИНН, по порядку
    entity: Catalog_Контрагенты
    orderby: ИНН desc
    select: [Ref_Key, ИНН]
""",
        encoding="utf-8",
    )
    ответ = json.loads(await сервис.recipe(SessionScope(), name="bad"))
    assert ответ["error"]["code"] == "params_invalid"


async def test_битый_файл_рецептов_не_роняет_тул(сервис, дом):
    (дом / "bases" / "ut" / "recipes.yaml").write_text("recipes: [не словарь]\n", encoding="utf-8")
    ответ = json.loads(await сервис.recipe(SessionScope(), name="partners"))
    assert ответ["error"]["code"] == "config_invalid"


async def _рецепт_на_условии(
    дом, условие: str, тип: str = "string", сущность: str = "Catalog_Контрагенты"
):
    (дом / "bases" / "ut" / "recipes.yaml").write_text(
        f"""
version: 1
recipes:
  проба:
    title: Проба
    entity: {сущность}
    params:
      значение: {{ type: {тип} }}
    filter: {условие}
    select: [Ref_Key]
""",
        encoding="utf-8",
    )


async def test_упорядоченное_сравнение_с_защищаемым_полем_запрещено(сервис, дом):
    """Правило `unmasking._обработать_сравнение` действует и в рецепте: по непрозрачному значению
    нельзя сравнивать «больше-меньше», а перебором границ оно раскрывается. Запрет не зависит от
    того, токен передан или открытое значение."""
    await _рецепт_на_условии(дом, "ИНН gt {значение}")
    токен = await токен_инн(сервис, ИНН)
    ответ = json.loads(
        await сервис.recipe(SessionScope(), name="проба", params={"значение": токен})
    )
    assert ответ["error"]["code"] == "filter_syntax"
    assert "упорядоченное" in ответ["error"]["message"]


async def test_поиск_по_вхождению_на_защищаемом_классе_запрещён(сервис, дом, respx_ut):
    """Открытый образец на поле класса inn — посимвольный подбор значения (правило F1 M1c).
    Через рецепт он не должен проходить так же, как не проходит через filter."""
    await _рецепт_на_условии(дом, "substringof({значение}, ИНН)")
    ответ = json.loads(
        await сервис.recipe(SessionScope(), name="проба", params={"значение": "7707"})
    )
    assert ответ["error"]["code"] == "filter_syntax"
    assert "вхождению" in ответ["error"]["message"]


async def test_поиск_по_вхождению_названия_разрешён(сервис, дом, respx_ut):
    """…а по названию организации — разрешён: `org` и `person` из правила исключены (поиск по
    части названия — рабочий сценарий), и рецепт повторяет ровно это поведение."""
    await _рецепт_на_условии(дом, "substringof({значение}, Description)")
    respx_ut.get("Catalog_Контрагенты").mock(return_value=httpx.Response(200, json={"value": []}))
    ответ = json.loads(
        await сервис.recipe(SessionScope(), name="проба", params={"значение": "Ромаш"})
    )
    assert "error" not in ответ
    assert "substringof('Ромаш', Description)" in _адрес(respx_ut)


async def test_открытая_дата_рождения_запрещена(сервис, дом):
    await _рецепт_на_условии(
        дом, "ДатаРождения eq {значение}", тип="datetime", сущность="Catalog_ФизическиеЛица"
    )
    ответ = json.loads(
        await сервис.recipe(SessionScope(), name="проба", params={"значение": "1980-05-01"})
    )
    assert ответ["error"]["code"] == "filter_syntax"
    assert "dob" in ответ["error"]["message"]


# ---------------------------------------------------------------------------------------------
# Ruling 20, пункт 3: анти-оракульные правила — один список, применяемый из одного места
#
# Правила `$filter` жили в двух реализациях: разбор выражения (`Unmasker.filter`) и отдельный
# список в `BaseGate.inbound_param`. Списки разошлись — из пяти правил в рецепте работали три, и
# те не срабатывали на поле через связанный объект. Теперь условия рецепта прогоняются через тот
# же разбор `$filter`, поэтому одинаковое условие получает одинаковый вердикт обоими путями.
# ---------------------------------------------------------------------------------------------

УСЛОВИЯ_ОРАКУЛА = [
    # Посимвольный подбор через поиск вхождения на поле класса addr, добытом через связанный
    # объект: `query` отклонял, рецепт выполнял (I1 ревью 2026-09-11).
    "substringof('Тверск', Контрагент/АдресРегистрации)",
    # Двоичный поиск по границе — то же поле, упорядоченное сравнение (I1).
    "Контрагент/АдресРегистрации gt 'М'",
    # Защищаемое поле не целиком: обёртка функцией. В рецепте правило не воспроизводилось вовсе,
    # потому что условие без подстановки не проверялось (M3 ревью 2026-09-11).
    "year(Контрагент/ДатаРождения) eq 1980",
]


@pytest.mark.parametrize("условие", УСЛОВИЯ_ОРАКУЛА)
async def test_анти_оракульное_правило_в_рецепте_и_в_query_одинаково(
    сервис, дом, respx_ut, условие
):
    """Одно и то же условие двумя путями обязано получить один вердикт. Условие здесь записано в
    рецепте целиком, без подстановки, — то есть проверка `inbound_param` его не видит в принципе,
    и закрыть его может только общий разбор выражения."""
    маршрут = respx_ut.get(url__regex=r".*atalog_.*").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    (дом / "bases" / "ut" / "recipes.yaml").write_text(
        f"""
version: 1
recipes:
  проба:
    title: Проба
    entity: Catalog_Контрагенты
    filter: {условие}
    select: [Ref_Key]
""",
        encoding="utf-8",
    )

    через_рецепт = json.loads(await сервис.recipe(SessionScope(), name="проба"))
    через_query = json.loads(
        await сервис.query(SessionScope(), entity="Catalog_Контрагенты", filter=условие)
    )

    assert через_рецепт["error"]["code"] == "filter_syntax"
    assert через_query["error"]["code"] == "filter_syntax"
    assert not маршрут.called


async def test_токен_смешанный_с_текстом_в_параметре_отклонён(сервис, дом, respx_ut):
    """Четвёртое правило (`unmasking._проверить_литерал_текстом`): токен, обрамлённый текстом, в
    `$filter` отклонялся, а в рецепте подставлялся внутрь строки — и раскрытое значение уезжало
    в 1С обрамлённым, то есть тем же приёмом выносилось наружу (I2 ревью 2026-09-11)."""
    маршрут = respx_ut.get("Catalog_Контрагенты").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    await _рецепт_на_условии(дом, "ИНН eq {значение}")
    токен = await токен_инн(сервис, ИНН)

    ответ = json.loads(
        await сервис.recipe(SessionScope(), name="проба", params={"значение": f"пре{токен}пост"})
    )

    assert ответ["error"]["code"] == "token_partial"
    assert ИНН not in json.dumps(ответ, ensure_ascii=False)
    assert not маршрут.called


async def test_ресурс_рецептов_отдаёт_читаемый_список(сервис):
    текст = await сервис.resource_recipes(SessionScope(), "ut")
    assert "# Рецепты базы ut" in текст
    assert "## partners — Контрагенты по ИНН" in текст
    assert "| inn | string | да |" in текст
    assert "Неприменим к этой базе" in текст


# ---------------------------------------------------------------------------------------------
# Шаблон УТ: имена только проверенные по полному дампу
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def индекс_полного_дампа(tmp_path_factory):
    if not ПОЛНЫЙ_ДАМП.exists():
        pytest.skip(f"нет полного дампа {ПОЛНЫЙ_ДАМП.name} (в git не хранится)")
    путь = tmp_path_factory.mktemp("probe") / "metadata.sqlite"
    хранилище = IndexRepository(путь)
    хранилище.write(parse_edmx(ПОЛНЫЙ_ДАМП.read_bytes()))
    yield хранилище
    хранилище.close()


def test_шаблон_ут_сверен_с_полным_дампом(индекс_полного_дампа):
    """Каждый рецепт шаблона УТ существует в реальной базе ЦЕЛИКОМ: и сущность, и каждое поле
    `select`, и поле сортировки, и каждое поле, с которым сравнивается параметр. Проверки одной
    сущности мало — именно несуществующие ПОЛЯ нашла проба P4."""
    книга_ут = load_recipes(ШАБЛОН_УТ)
    assert 4 <= len(книга_ут.recipes) <= 6
    for имя, р in книга_ут.recipes.items():
        описание = индекс_полного_дампа.describe(р.entity)
        assert описание is not None, f"{имя}: сущности {р.entity} нет в полном дампе"
        поля = {поле["name"] for поле in описание.fields}
        assert set(р.select) <= поля, f"{имя}: нет полей {sorted(set(р.select) - поля)}"
        for параметр in р.params:
            поле = р.param_field(параметр)
            assert not поле or поле in поля, f"{имя}: поля условия {поле} нет в {р.entity}"
        if р.orderby:
            assert set(orderby_fields(р.orderby)) <= поля, f"{имя}: сортировка по чужому полю"
        if описание.is_virtual:
            схема = описание.actions[0]["params"]
            assert set(р.virtual) <= set(схема), f"{имя}: у {р.virtual_kind} нет таких параметров"
