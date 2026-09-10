"""Общие фикстуры юнит-тестов."""

import pathlib

import pytest

from odata1c.config.models import Limits
from odata1c.index.edmx import parse_edmx
from odata1c.index.repository import IndexRepository

ОБРАЗЦЫ = pathlib.Path(__file__).parent.parent / "fixtures" / "edmx"


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
