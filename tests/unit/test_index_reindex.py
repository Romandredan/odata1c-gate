"""Реиндекс: пропуск при неизменном $metadata, разница, атомарная замена (SPEC §4.3)."""

import pathlib

import httpx
import respx

from odata1c.client1c.client import Client1C
from odata1c.config.models import BaseConfig
from odata1c.index.reindex import index_path, reindex

URL = "http://localhost/ut/odata/standard.odata/"


def база() -> BaseConfig:
    return BaseConfig(name="ut", label="УТ", url=URL, user="u", password="p", role="test")


def _замокать_завершение_сеанса() -> None:
    # client.close() при ib_session=True (по умолчанию) шлёт завершение сеанса на тот же
    # адрес без хвоста пути (см. приём в tests/unit/test_client1c.py, test_cli_base.py).
    respx.get(URL).mock(return_value=httpx.Response(200, json={"value": []}))


@respx.mock
async def test_первый_реиндекс_строит_индекс(tmp_path, edmx_synthetic):
    respx.get(f"{URL}$metadata").mock(return_value=httpx.Response(200, content=edmx_synthetic))
    _замокать_завершение_сеанса()
    client = Client1C(база())
    результат = await reindex(база(), client, tmp_path)
    await client.close()

    assert результат.changed is True
    assert результат.entity_count == 8
    assert index_path(tmp_path, "ut").exists()
    assert "Catalog_Контрагенты" in результат.added_entities


@respx.mock
async def test_повторный_реиндекс_без_изменений(tmp_path, edmx_synthetic):
    respx.get(f"{URL}$metadata").mock(return_value=httpx.Response(200, content=edmx_synthetic))
    _замокать_завершение_сеанса()
    client = Client1C(база())
    await reindex(база(), client, tmp_path)
    результат = await reindex(база(), client, tmp_path)
    await client.close()

    assert результат.changed is False
    assert "без изменений" in результат.message


@respx.mock
async def test_принудительный_реиндекс_перестраивает(tmp_path, edmx_synthetic):
    respx.get(f"{URL}$metadata").mock(return_value=httpx.Response(200, content=edmx_synthetic))
    _замокать_завершение_сеанса()
    client = Client1C(база())
    await reindex(база(), client, tmp_path)
    результат = await reindex(база(), client, tmp_path, force=True)
    await client.close()

    assert результат.changed is True
    assert результат.entity_count == 8


@respx.mock
async def test_разница_показывает_добавленное_и_удалённое(tmp_path, edmx_synthetic):
    урезанный = edmx_synthetic.replace(
        b'<EntitySet Name="Catalog_\xd0\x91\xd0\xb0\xd0\xbd\xd0\xba\xd0\xbe\xd0\xb2\xd1\x81\xd0\xba'
        b'\xd0\xb8\xd0\xb5\xd0\xa1\xd1\x87\xd0\xb5\xd1\x82\xd0\xb0" '
        b'EntityType="StandardODATA.Catalog_\xd0\x91\xd0\xb0\xd0\xbd\xd0\xba\xd0\xbe\xd0\xb2\xd1'
        b'\x81\xd0\xba\xd0\xb8\xd0\xb5\xd0\xa1\xd1\x87\xd0\xb5\xd1\x82\xd0\xb0"/>',
        b"",
    )
    маршрут = respx.get(f"{URL}$metadata")
    маршрут.mock(
        side_effect=[
            httpx.Response(200, content=edmx_synthetic),
            httpx.Response(200, content=урезанный),
        ]
    )
    _замокать_завершение_сеанса()
    client = Client1C(база())
    await reindex(база(), client, tmp_path)
    результат = await reindex(база(), client, tmp_path)
    await client.close()

    assert результат.changed is True
    assert "Catalog_БанковскиеСчета" in результат.removed_entities


@respx.mock
async def test_классификатор_проставляет_классы_полей(tmp_path, edmx_synthetic):
    respx.get(f"{URL}$metadata").mock(return_value=httpx.Response(200, content=edmx_synthetic))

    def классификатор(сущность, поле, тип):
        return ("inn", "auto") if поле == "ИНН" else None

    _замокать_завершение_сеанса()
    client = Client1C(база())
    результат = await reindex(база(), client, tmp_path, classifier=классификатор)
    await client.close()

    новые = {
        (поле["entity"], поле["field"], поле["sensitivity"])
        for поле in результат.new_sensitive_fields
    }
    assert ("Catalog_Контрагенты", "ИНН", "inn") in новые


@respx.mock
async def test_повреждённый_метаданные_не_ломают_старый_индекс(tmp_path, edmx_synthetic):
    маршрут = respx.get(f"{URL}$metadata")
    маршрут.mock(
        side_effect=[
            httpx.Response(200, content=edmx_synthetic),
            httpx.Response(200, content="<edmx:Edmx><не закрыт>".encode()),
        ]
    )
    _замокать_завершение_сеанса()
    client = Client1C(база())
    await reindex(база(), client, tmp_path)
    import pytest

    from odata1c.index.edmx import EdmxError

    with pytest.raises(EdmxError):
        await reindex(база(), client, tmp_path)
    await client.close()

    from odata1c.index.repository import IndexRepository

    хранилище = IndexRepository(index_path(tmp_path, "ut"))
    assert len(хранилище.entity_names()) == 8  # старый индекс уцелел
    хранилище.close()


def _эдмкс_с_нераспознанным_набором() -> bytes:
    """Минимальный EDMX с набором, ссылающимся на несуществующий EntityType — тот же приём,
    что в tests/unit/test_index_repository.py, отдельно от synthetic.edmx, чтобы не менять
    фикстуру, общую с задачами 1 и 3."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
  <edmx:DataServices m:DataServiceVersion="3.0"
                     xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <Schema Namespace="StandardODATA" xmlns="http://schemas.microsoft.com/ado/2009/11/edm">
      <EntityContainer Name="StandardODATA" m:IsDefaultEntityContainer="true">
        <EntitySet Name="Catalog_Пропавший" EntityType="StandardODATA.Catalog_Пропавший"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>""".encode()


@respx.mock
async def test_нераспознанные_наборы_попадают_в_результат(tmp_path):
    # Требование задачи 5 («на что обратить внимание»): набор с испорченной ссылкой на тип —
    # это признак повреждённого $metadata, а не удалённых объектов, и его нужно показать
    # пользователю, а не пропустить молча.
    испорченный = _эдмкс_с_нераспознанным_набором()
    respx.get(f"{URL}$metadata").mock(
        side_effect=[
            httpx.Response(200, content=испорченный),
            httpx.Response(200, content=испорченный),
        ]
    )
    _замокать_завершение_сеанса()
    client = Client1C(база())
    первый = await reindex(база(), client, tmp_path)
    второй = await reindex(база(), client, tmp_path)  # sha256 тот же — ветка «без изменений»
    await client.close()

    assert первый.changed is True
    assert первый.unresolved_entity_sets == ["Catalog_Пропавший"]
    assert второй.changed is False
    assert второй.unresolved_entity_sets == ["Catalog_Пропавший"]


@respx.mock
async def test_сбой_записи_не_портит_прежний_индекс_и_не_оставляет_мусора(
    tmp_path, edmx_synthetic, monkeypatch
):
    """Правки по итогам ревью задачи 5 (Critical + Important): единственный тест, который
    реально ловит поломку атомарной подмены — а не только ветку, где до подмены дело не
    доходит (сбой разбора EDMX, см. test_повреждённый_метаданные_не_ломают_старый_индекс —
    там временный файл вообще не успевает появиться, потому что parse_edmx падает раньше
    записи). Здесь второй проход падает НА ЭТАПЕ ЗАПИСИ временного индекса: сначала реально
    отрабатывает write() (временный файл получает настоящие данные на диск — ровно то
    состояние, при котором прямая запись поверх прежнего файла, без временного файла и
    os.replace, уже необратимо испортила бы прежний индекс), и только после этого — сбой.

    Проверяются все три вещи, которые просил ревьюер: прежний файл индекса не изменился
    побайтово, он по-прежнему читается и содержит прежнее число сущностей, временный файл
    и его журналы (-wal, -shm) не остались на диске.
    """
    from odata1c.index.repository import IndexRepository

    respx.get(f"{URL}$metadata").mock(return_value=httpx.Response(200, content=edmx_synthetic))
    _замокать_завершение_сеанса()
    client = Client1C(база())

    # Первый реиндекс — обычный, без патча: строит прежний, заведомо исправный индекс.
    первый = await reindex(база(), client, tmp_path)
    assert первый.changed is True

    путь = index_path(tmp_path, "ut")
    содержимое_до = путь.read_bytes()
    размер_до = путь.stat().st_size

    исходный_write = IndexRepository.write

    def падающий_write(self, разобрано):
        исходный_write(self, разобрано)  # временный файл реально получает данные на диск
        raise RuntimeError("смоделированный сбой на этапе записи временного индекса")

    monkeypatch.setattr(IndexRepository, "write", падающий_write)

    import pytest

    with pytest.raises(RuntimeError):
        # force=True: sha256 не изменился (тот же edmx_synthetic), без force сработала бы
        # ветка «без изменений» и до записи дело бы не дошло.
        await reindex(база(), client, tmp_path, force=True)
    await client.close()

    временный = путь.with_suffix(".sqlite.new")
    assert not временный.exists(), "временный файл .sqlite.new остался после сбоя"
    for суффикс in ("-wal", "-shm"):
        # Тот же способ собрать путь к журналу, что в reindex.py::_удалить_с_журналами.
        журнал = pathlib.Path(str(временный) + суффикс)
        assert not журнал.exists(), f"журнал {журнал.name} остался после сбоя"

    assert путь.read_bytes() == содержимое_до, "прежний индекс изменился побайтово"
    assert путь.stat().st_size == размер_до

    хранилище = IndexRepository(путь)
    assert len(хранилище.entity_names()) == 8  # прежний индекс по-прежнему читается
    хранилище.close()
