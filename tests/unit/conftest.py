"""Общие фикстуры юнит-тестов."""

import importlib.resources
import pathlib

import httpx
import pytest

from odata1c.config.models import Limits
from odata1c.gate.service import policy_path
from odata1c.index.edmx import parse_edmx
from odata1c.index.repository import IndexRepository

ОБРАЗЦЫ = pathlib.Path(__file__).parent.parent / "fixtures" / "edmx"


def обеспечить_policy_yaml(home: pathlib.Path, base_name: str) -> pathlib.Path:
    """Создать пустой файл владельца `policy.yaml`, если его ещё нет.

    ADR-0015: `refresh_policy` больше не создаёт файл владельца — она пишет только
    `policy.auto.yaml` (см. `odata1c.gate.service.refresh_policy`). Файл владельца создаёт
    `base add` из шаблона (задача плана, ещё не реализована), а в тестах, что настраивают базу
    напрямую — минуя `base add`, — эту роль берёт на себя эта функция: без неё тесты, дописывающие
    правила в `policy.yaml` (`entities.hide` и подобные), падали бы с `FileNotFoundError`.
    Минимальное содержимое — как у будущего шаблона `base add` (SPEC §6.9): версия и
    `scan_free_text` включён по умолчанию."""
    путь = policy_path(home, base_name)
    if not путь.exists():
        путь.parent.mkdir(parents=True, exist_ok=True)
        путь.write_text("version: 2\nscan_free_text: true\n", encoding="utf-8")
    return путь


def политика_из_шаблона_с_дублем_entities(base_name: str = "trade_dev") -> str:
    """Текст `policy.yaml` ровно в том виде, какой получает владелец, раскомментировавший пример
    `# entities:` и не убравший заглушку `entities: {}` строкой выше (находка I1 итогового ревью
    M2b: шаблон к этому подталкивает сам — заглушка и пример стоят рядом).

    Берётся НАСТОЯЩИЙ файл поставки (`odata1c/templates/policy.example.yaml`), а не его копия в
    тесте: доказывается именно то, что шаблон провоцирует повтор раздела, а не то, что повтор
    вообще ловится. Раскомментирование — механическое: строка `# entities:` и следующие за ней
    строки с префиксом `#   ` теряют первые два символа."""
    текст = (
        importlib.resources.files("odata1c.templates")
        .joinpath("policy.example.yaml")
        .read_text(encoding="utf-8")
        .replace("{{base}}", base_name)
    )
    строки: list[str] = []
    внутри = False
    for строка in текст.splitlines():
        if строка.startswith("# entities:"):
            внутри = True
            строки.append(строка[2:])
            continue
        if внутри and строка.startswith("#   "):
            строки.append(строка[2:])
            continue
        внутри = False
        строки.append(строка)
    return "\n".join(строки) + "\n"


# По одному правдоподобному значению на каждый класс гейта (`tokens.CLASSES`, кроме `keep`).
# Общий набор для всех параметризованных проверок «по всем классам сразу»: урок ревью
# 2026-09-11 — сторож, построенный на одном ИНН, не доказывает ничего, потому что ИНН
# единственный класс с тремя независимыми страховками (детектор, контрольная сумма, цифровая
# серия в словаре стража), а у `addr`, `dob` и свободнотекстового `doc` нет ни одной.
ЗНАЧЕНИЯ_КЛАССОВ: dict[str, str] = {
    "inn": "7707083893",
    "kpp": "770701001",
    "ogrn": "1027700132195",
    "acc": "40702810900000012345",
    "corr": "30101810400000000225",
    "bic": "044525225",
    "iban": "DE89370400440532013000",
    "card": "4111111111111111",
    "snils": "112-233-445 95",
    "doc": "45 03 123456",
    "phone": "+7 916 123-45-67",
    "email": "ivan@example.com",
    "dob": "1980-05-01",
    "addr": "г. Москва, ул. Тверская, д. 7, кв. 43",
    "org": "ООО Ромашка",
    "person": "Иванов Иван Иванович",
}


def без_навигаций(entity: str, ключ: str) -> None:
    """Резолвер-заглушка для вспомогательных вызовов `BaseGate.mask` в тестах, где запись плоская
    (выдать токен значению, чтобы дальше подставить его в отбор). Переключать политику не на чем,
    но аргумент у `BaseGate.mask` обязателен намеренно — см. его докстринг."""
    return None


def строение_неизвестно(entity: str) -> None:
    """Строение-заглушка для тех же вспомогательных вызовов `BaseGate.mask`: запись плоская, и
    табличной части контактной информации в ней нет (Ruling 33). Аргумент `shape` обязателен
    намеренно — см. докстринг `BaseGate.mask`."""
    return None


def без_класса_пути(entity: str, path: str) -> None:
    """Класс пути-заглушка для наборов, которые собирают `Unmasker` напрямую и путей к
    контактной информации не проверяют. Аргумент `path_class` обязателен намеренно (Ruling 37):
    `Unmasker` без него снова открыл бы оракул через путь — заглушка здесь видна явно."""
    return None


def ничего_не_скрыто(entity: str) -> bool:
    """Предикат-заглушка «сущность закрыта владельцем» для тех же вспомогательных вызовов
    `BaseGate.mask`: запись плоская, скрывать нечего. Аргумент `hidden` обязателен намеренно —
    запрет наследуется на дочерние объекты (Ruling 30), и полный набор знает только `ToolService`
    с индексом на руках; молчаливое умолчание вернуло бы неполный запрет."""
    return False


def эхо_отбора(request: httpx.Request) -> httpx.Response:
    """Поддельная 1С, повторяющая выражение отбора вместе с литералом в тексте ошибки (форма из
    находки ревью 2026-09-11, задача N1 M1d).

    Спорить о том, повторяет ли конкретная публикация литералы, эта проверка не должна: формы
    ошибок у платформы разные, версии разные, а инвариант 1 сформулирован как «никогда» —
    защита строится так, чтобы не зависеть от того, проговорится платформа или нет.
    """
    отбор = request.url.params.get("$filter", "")
    return httpx.Response(
        400,
        json={
            "odata.error": {
                "code": "6",
                "message": {
                    "lang": "ru",
                    "value": f"Ошибка при разборе выражения отбора: {отбор}",
                },
            }
        },
    )


@pytest.fixture
def edmx_synthetic() -> bytes:
    """Синтетический $metadata: по одному представителю каждого разбираемого случая."""
    return (ОБРАЗЦЫ / "synthetic.edmx").read_bytes()


@pytest.fixture
def edmx_ut_real() -> bytes:
    """Урезанный реальный $metadata УТ (проба P4): структура, которую синтетика не воспроизводит."""
    return (ОБРАЗЦЫ / "ut-real.edmx").read_bytes()


@pytest.fixture
def индекс_ut(tmp_path, edmx_ut_real):
    """Индекс, построенный на урезанном реальном образце УТ (проба P4).

    Перенесена сюда из `test_index_repository.py` (план M1d, задача 2): построение запросов
    (`odata_query.py`) проверяется на тех же реальных именах и структурах, что и хранилище
    индекса, — дублировать фикстуру в каждом файле не нужно."""
    хранилище = IndexRepository(tmp_path / "ut.sqlite")
    хранилище.write(parse_edmx(edmx_ut_real))
    yield хранилище
    хранилище.close()


@pytest.fixture
def лимиты() -> Limits:
    """Лимиты по умолчанию (SPEC §10) — без переопределений, если тесту не нужны другие значения."""
    return Limits()


def обёртка_эдмкс(тело_контейнера: str) -> bytes:
    """Минимальный валидный EDMX с произвольным содержимым EntityContainer — для сценариев,
    которые не должны затрагивать общую фикстуру synthetic.edmx.

    Правка по итогам финального ревью M1b («мелочь напоследок»): раньше эта функция была
    скопирована один в один в test_index_repository.py и test_index_reindex.py под именем
    `_эдмкс_с_нераспознанным_набором` (обе копии строили один и тот же документ — набор,
    ссылающийся на несуществующий EntityType), при том что в test_index_edmx.py уже была
    обобщённая версия `_обёртка_эдмкс`, принимающая произвольное тело контейнера. Общая версия
    перенесена сюда и используется везде, где нужен нестандартный EDMX-документ."""
    return обёртка_эдмкс_с_типами("", тело_контейнера)


def обёртка_эдмкс_с_типами(типы_xml: str, тело_контейнера: str) -> bytes:
    """Как `обёртка_эдмкс`, но с произвольными `EntityType` перед `EntityContainer` — для
    сценариев с неразрешающейся или частично резолвящейся ссылкой на тип (раунд правок 1,
    задача 1 плана M1b-fix: осиротевший набор записей регистра и родитель без резолвящегося
    типа — инвариант 3)."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
  <edmx:DataServices m:DataServiceVersion="3.0"
                     xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <Schema Namespace="StandardODATA" xmlns="http://schemas.microsoft.com/ado/2009/11/edm">
      {типы_xml}
      <EntityContainer Name="StandardODATA" m:IsDefaultEntityContainer="true">
        {тело_контейнера}
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>""".encode()
