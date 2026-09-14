"""`bases.yaml` действует без перезапуска демона (SPEC §3.1, поправка 2026-09-14, ADR-0015).

Демон живёт дольше одной версии файла настроек: владелец правит `bases.yaml` — уровень гейта,
адрес, учётку, состав баз, — и правка действует с ближайшего вызова тула. Файл, который не
разбирается, закрывает все тулы кодом `config_invalid`, пока владелец его не починит: прежние
настройки не используются, потому что закрыть базу владелец мог как раз этой правкой.

Отметка файла — mtime и размер, а mtime на Windows грубее, чем две правки подряд в одном тесте,
поэтому каждая правка здесь идёт через `переписать()`: та сдвигает mtime явно.
"""

import json
import os
import pathlib

import pytest
from conftest import обеспечить_policy_yaml

from odata1c.cli import main
from odata1c.config.loader import load_config
from odata1c.gate.service import policy_path, refresh_policy
from odata1c.index.edmx import parse_edmx
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository
from odata1c.registry.registry import SessionScope
from odata1c.tools import service as модуль_сервиса
from odata1c.tools.service import ПРЕДУПРЕЖДЕНИЕ_СМЕНЫ_ПОЛИТИКИ, ToolService

URL_UT = "http://localhost/ut/odata/standard.odata/"
URL_DEV = "http://localhost/dev/odata/standard.odata/"
НОВЫЙ_URL_UT = "http://localhost/ut2/odata/standard.odata/"
НОВЫЙ_URL_DEV = "http://localhost/dev2/odata/standard.odata/"
# Пароль 1С лежит в том же файле: ни одно сообщение об ошибке разбора не вправе его повторить.
ПАРОЛЬ = "пароль-1с-из-файла"


def настройки(*, ut: str = "", dev: str | None = "", url_ut: str = URL_UT, ещё: str = "") -> str:
    """Текст `bases.yaml` из баз `ut` (роль `prod`) и `dev` (роль `dev`).

    `ut`/`dev` — дополнительные строки записи базы (с отступом в четыре пробела), `dev=None` —
    записи `dev` в файле нет вовсе, `ещё` — дописывается в конец раздела `bases`.
    """
    текст = (
        "default: ut\n"
        "bases:\n"
        "  ut:\n"
        "    label: УТ, тестовая\n"
        f"    url: {url_ut}\n"
        "    user: u\n"
        f"    password: {ПАРОЛЬ}\n"
        "    role: prod\n"
        f"{ut}"
    )
    if dev is not None:
        текст += (
            "  dev:\n"
            "    label: Песочница\n"
            f"    url: {URL_DEV}\n"
            "    user: u\n"
            f"    password: {ПАРОЛЬ}\n"
            "    role: dev\n"
            f"{dev}"
        )
    return текст + ещё


def переписать(путь: pathlib.Path, текст: str) -> None:
    """Переписать файл и сдвинуть его mtime вперёд: отметка (mtime, размер) обязана отличаться от
    прежней, даже если правка не поменяла размер, а часы файловой системы грубее теста."""
    путь.write_text(текст, encoding="utf-8")
    отметка = путь.stat().st_mtime + 10
    os.utime(путь, (отметка, отметка))


def _дом(tmp_path, edmx_synthetic):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(настройки(), encoding="utf-8")
    config = load_config(home)
    for имя, база in config.bases.items():
        хранилище = IndexRepository(index_path(home, имя))
        хранилище.write(parse_edmx(edmx_synthetic))
        хранилище.close()
        refresh_policy(home, база)
        обеспечить_policy_yaml(home, имя)
    return home


@pytest.fixture
def дом(tmp_path, edmx_synthetic):
    return _дом(tmp_path, edmx_synthetic)


@pytest.fixture
async def сервис(дом):
    служба = ToolService(load_config(дом))
    yield служба
    await служба.aclose()


async def найти(служба: ToolService, *, base: str | None = "ut") -> dict:
    """`find_entity` — тул через `_run`, без сети и без 1С: в его конверте есть и уровень гейта
    базы, и `warnings`, куда попадает предупреждение о смене политики."""
    return json.loads(await служба.find_entity(SessionScope(), base=base, query="Контрагенты"))


# ---------------------------------------------------------------------------------------------
# Уровень гейта и предупреждение о смене политики
# ---------------------------------------------------------------------------------------------


async def test_новый_уровень_гейта_действует_с_ближайшего_вызова(сервис, дом):
    assert (await найти(сервис))["gate"] == "identifiers+names"

    переписать(дом / "bases.yaml", настройки(ut="    gate:\n      mode: identifiers\n"))

    ответ = await найти(сервис)
    assert ответ["gate"] == "identifiers"
    assert ответ["entities"], "сущности индекса по-прежнему видны"


async def test_смена_уровня_предупреждает_ровно_один_раз(сервис, дом):
    assert ПРЕДУПРЕЖДЕНИЕ_СМЕНЫ_ПОЛИТИКИ not in (await найти(сервис))["warnings"]

    переписать(дом / "bases.yaml", настройки(ut="    gate:\n      mode: identifiers\n"))

    первый = await найти(сервис)
    assert первый["warnings"].count(ПРЕДУПРЕЖДЕНИЕ_СМЕНЫ_ПОЛИТИКИ) == 1
    второй = await найти(сервис)
    assert ПРЕДУПРЕЖДЕНИЕ_СМЕНЫ_ПОЛИТИКИ not in второй["warnings"]


async def test_правка_policy_yaml_предупреждает_тем_же_текстом(сервис, дом):
    """Уточнение контролёра к задаче 6 (SPEC §6.9): модели сообщается смена ЛЮБОГО из трёх файлов
    политики — `bases.yaml`, `policy.yaml`, `policy.auto.yaml`. Текст один: модели важно не то,
    какой файл правил владелец, а что состав полей в ответах мог измениться."""
    await найти(сервис)
    путь = policy_path(дом, "ut")
    переписать(путь, путь.read_text(encoding="utf-8") + "fields:\n  Catalog_Контрагенты.Код: inn\n")

    первый = await найти(сервис)
    assert первый["warnings"].count(ПРЕДУПРЕЖДЕНИЕ_СМЕНЫ_ПОЛИТИКИ) == 1
    assert ПРЕДУПРЕЖДЕНИЕ_СМЕНЫ_ПОЛИТИКИ not in (await найти(сервис))["warnings"]


# ---------------------------------------------------------------------------------------------
# Пул соединений базы
# ---------------------------------------------------------------------------------------------


class ПоддельныйКлиент:
    """Клиент 1С без сети: `ToolService` держит его по одному на базу и закрывает, когда запись
    базы в `bases.yaml` изменилась в части соединения."""

    def __init__(self, base) -> None:
        self.base = base
        self.закрыт = False

    async def close(self) -> None:
        self.закрыт = True


async def test_смена_адреса_пересоздаёт_клиента_а_прежний_закрывается(дом):
    служба = ToolService(load_config(дом), client_factory=ПоддельныйКлиент)
    try:
        первый = служба._client_for(служба._registry.get("ut", SessionScope()))

        переписать(дом / "bases.yaml", настройки(url_ut=НОВЫЙ_URL_UT))
        await найти(служба)

        второй = служба._client_for(служба._registry.get("ut", SessionScope()))
        assert второй is not первый
        assert первый.закрыт is True
        assert второй.base.url == НОВЫЙ_URL_UT
    finally:
        await служба.aclose()


async def test_клиент_строится_по_текущей_записи_а_не_по_снимку_вызывающего(дом):
    """Находка 2 ревью задачи 6: вызов, уснувший на `await` между разрешением базы и получением
    клиента (реально — `commit` после диалога подтверждения), держит снимок прежних настроек.
    Клиент кэшируется на ИМЯ базы и общий на все сессии, поэтому строиться он обязан по текущей
    записи, иначе один такой вызов вернул бы шлюз на прежний адрес до следующей правки."""
    служба = ToolService(load_config(дом), client_factory=ПоддельныйКлиент)
    try:
        снимок = служба._registry.get("ut", SessionScope())
        переписать(дом / "bases.yaml", настройки(url_ut=НОВЫЙ_URL_UT))
        await служба.bases(SessionScope())  # чужое перечитывание

        assert служба._client_for(снимок).base.url == НОВЫЙ_URL_UT
    finally:
        await служба.aclose()


async def test_гейт_строится_по_текущему_уровню_а_не_по_снимку_вызывающего(дом):
    """То же и для гейта, только цена ошибки выше: гейт собирает маскировщик на уровне при
    построении, и гейт из устаревшего снимка отдавал бы всем сессиям МЕНЕЕ строгий уровень, чем
    стоит в файле."""
    служба = ToolService(load_config(дом), client_factory=ПоддельныйКлиент)
    try:
        снимок = служба._registry.get("dev", SessionScope())
        assert снимок.gate.mode == "off", "роль dev: гейт выключен"
        переписать(дом / "bases.yaml", настройки(dev="    gate:\n      mode: identifiers+names\n"))
        await служба.bases(SessionScope())

        assert служба._gate_for(снимок).mode == "identifiers+names"
    finally:
        await служба.aclose()


def запись_dev(url: str) -> str:
    """Запись базы `dev` с произвольным адресом — дописывается в конец раздела `bases`."""
    return (
        "  dev:\n"
        "    label: Песочница\n"
        f"    url: {url}\n"
        "    user: u\n"
        f"    password: {ПАРОЛЬ}\n"
        "    role: dev\n"
    )


async def test_клиент_по_снимку_исчезнувшей_базы_не_кэшируется(дом):
    """Находка M9 итогового ревью M2b: база исчезла из файла, а вызов в полёте всё равно
    доводится до конца по своему снимку (`_запись_базы`). Построенный по снимку клиент в общий
    кэш процесса попадать не вправе: база, вернувшаяся под тем же именем с ДРУГИМ адресом,
    обслуживалась бы им и дальше — `_применить_настройки` вытесняет клиента, только сравнивая
    прежнюю запись с новой (`прежняя is None → continue`), а прежней в момент возвращения нет."""
    служба = ToolService(load_config(дом), client_factory=ПоддельныйКлиент)
    try:
        снимок = служба._registry.get("dev", SessionScope())
        переписать(дом / "bases.yaml", настройки(dev=None))
        await служба.bases(SessionScope())  # чужое перечитывание убрало базу из файла

        в_полёте = служба._client_for(снимок)
        assert в_полёте.base.url == URL_DEV, "начатый вызов доводится до конца по своему снимку"
        assert "dev" not in служба._clients, "но в общий кэш снимок исчезнувшей базы не ложится"

        переписать(дом / "bases.yaml", настройки(dev=None) + запись_dev(НОВЫЙ_URL_DEV))
        await служба.bases(SessionScope())

        вернувшийся = служба._client_for(служба._registry.get("dev", SessionScope()))
        assert вернувшийся.base.url == НОВЫЙ_URL_DEV
        assert вернувшийся is not в_полёте
    finally:
        await служба.aclose()


async def test_гейт_по_снимку_исчезнувшей_базы_не_кэшируется(дом):
    """То же для гейта: он собирает маскировщик на уровне при построении, и гейт снимка
    обслуживал бы вернувшуюся базу прежним, более мягким уровнем."""
    служба = ToolService(load_config(дом), client_factory=ПоддельныйКлиент)
    try:
        снимок = служба._registry.get("dev", SessionScope())
        assert снимок.gate.mode == "off", "роль dev: гейт выключен"
        переписать(дом / "bases.yaml", настройки(dev=None))
        await служба.bases(SessionScope())

        assert служба._gate_for(снимок).mode == "off"
        assert "dev" not in служба._gates

        строгая = запись_dev(URL_DEV) + "    gate:\n      mode: identifiers+names\n"
        переписать(дом / "bases.yaml", настройки(dev=None) + строгая)
        await служба.bases(SessionScope())

        assert служба._gate_for(служба._registry.get("dev", SessionScope())).mode == (
            "identifiers+names"
        )
    finally:
        await служба.aclose()


async def test_правка_не_про_соединение_клиента_не_трогает(дом):
    """Смена подписи базы пул соединений не пересоздаёт: перечитывание не должно рвать открытые
    соединения на каждой правке комментария в файле."""
    служба = ToolService(load_config(дом), client_factory=ПоддельныйКлиент)
    try:
        первый = служба._client_for(служба._registry.get("ut", SessionScope()))

        переписать(дом / "bases.yaml", настройки().replace("УТ, тестовая", "УТ, рабочая"))
        await найти(служба)

        assert служба._client_for(служба._registry.get("ut", SessionScope())) is первый
        assert первый.закрыт is False
    finally:
        await служба.aclose()


# ---------------------------------------------------------------------------------------------
# Битый файл: тулы закрыты до починки
# ---------------------------------------------------------------------------------------------


async def test_битый_bases_yaml_закрывает_тулы_и_чинится(сервис, дом):
    путь = дом / "bases.yaml"
    переписать(путь, настройки() + "  сломано: [не закрытая скобка\n")

    отказ = (await найти(сервис))["error"]
    assert отказ["code"] == "config_invalid"
    текст = json.dumps(отказ, ensure_ascii=False)
    assert "bases.yaml" in текст and "строка" in текст
    # Инвариант 1 и решение loader'а: место ошибки — числа, содержимое файла в ответ не идёт.
    assert ПАРОЛЬ not in текст and "сломано" not in текст

    # «Любой тул» — в том числе те, что не идут через `_run`.
    список = json.loads(await сервис.bases(SessionScope()))
    assert список["error"]["code"] == "config_invalid"

    переписать(путь, настройки())
    assert (await найти(сервис))["gate"] == "identifiers+names"


async def test_файл_не_прошедший_проверку_закрывает_тулы_тем_же_кодом(сервис, дом):
    """SPEC §3.1 говорит про файл, который «не разбирается **или не проходит проверку**»: YAML
    целый, а запись базы неверна. Проверка не должна пройти мимо `_run` голым исключением
    pydantic — там в тексте цитируется то, что стояло в файле."""
    переписать(дом / "bases.yaml", настройки(ut="    неизвестный_ключ: 1\n"))

    отказ = (await найти(сервис))["error"]
    assert отказ["code"] == "config_invalid"
    assert ПАРОЛЬ not in json.dumps(отказ, ensure_ascii=False)


async def test_отказ_проверки_называет_файл_и_поле_но_не_значение(сервис, дом):
    """Находка 3 ревью: до этой правки валидатор `url` вклеивал в текст адрес из файла, а
    сообщение не называло файла — и то и другое уходило модели ответом тула."""
    переписать(дом / "bases.yaml", настройки(url_ut="http://секретный-хост/ut/odata/"))

    отказ = (await найти(сервис))["error"]
    текст = json.dumps(отказ, ensure_ascii=False)
    assert отказ["code"] == "config_invalid"
    assert "bases.yaml" in текст and "url" in текст
    assert "секретный-хост" not in текст and ПАРОЛЬ not in текст


async def test_битый_daemon_yaml_не_мешает_правке_bases_yaml(сервис, дом):
    """Находка 1 ревью: перечитывание следит за отметкой `bases.yaml`, а `load_config` падал на
    ошибке `daemon.yaml` — испорченный `daemon.yaml` закрывал бы тулы с первой правкой соседнего
    файла и не отпускал до перезапуска: его починка отметку `bases.yaml` не меняет."""
    переписать(дом / "daemon.yaml", "port: [не закрытая скобка\n")
    переписать(дом / "bases.yaml", настройки(ut="    gate:\n      mode: identifiers\n"))

    ответ = await найти(сервис)
    assert "error" not in ответ, ответ
    assert ответ["gate"] == "identifiers"


async def test_база_по_умолчанию_исчезла_из_файла_это_тоже_config_invalid(сервис, дом):
    """Файл не прочитан целиком, и отвечать `base_unknown` на вызов с явной существующей базой
    значило бы указать модели не на ту причину (SPEC §3.1: код всегда `config_invalid`)."""
    переписать(дом / "bases.yaml", настройки().replace("default: ut\n", "default: нет_такой\n"))

    assert (await найти(сервис))["error"]["code"] == "config_invalid"
    assert json.loads(await сервис.bases(SessionScope()))["error"]["code"] == "config_invalid"


async def test_daemon_yaml_остаётся_стартовым(сервис, дом):
    """SPEC §3.1: без перезапуска действует только `bases.yaml`. `load_config` читает оба файла,
    и правка `daemon.yaml` вступила бы в силу тайком — от того, что владелец тронул соседний
    файл, — да ещё наполовину: лимиты в описании тулов демон взял при старте."""
    было = сервис.config.daemon.limits.top_default
    путь = дом / "daemon.yaml"
    переписать(путь, путь.read_text(encoding="utf-8").replace("top_default: 50", "top_default: 7"))
    переписать(дом / "bases.yaml", настройки(ut="    gate:\n      mode: identifiers\n"))

    await найти(сервис)

    assert сервис.config.daemon.limits.top_default == было
    assert сервис.config.bases["ut"].gate.mode == "identifiers", "bases.yaml при этом перечитан"


async def test_настройки_не_перечитываются_пока_файл_не_менялся(сервис, дом, monkeypatch):
    вызовы = []
    настоящая = модуль_сервиса.reload_bases

    def считать(home, daemon):
        вызовы.append(home)
        return настоящая(home, daemon)

    monkeypatch.setattr(модуль_сервиса, "reload_bases", считать)

    await найти(сервис)
    await найти(сервис)
    await сервис.bases(SessionScope())
    assert вызовы == []

    переписать(дом / "bases.yaml", настройки())
    await найти(сервис)
    await найти(сервис)
    assert len(вызовы) == 1


# ---------------------------------------------------------------------------------------------
# Состав баз
# ---------------------------------------------------------------------------------------------


async def имена_баз(служба: ToolService) -> set[str]:
    список = json.loads(await служба.bases(SessionScope()))
    return {строка["name"] for строка in список["bases"]}


async def test_удалённая_база_неизвестна_а_добавленная_видна(сервис, дом):
    assert await имена_баз(сервис) == {"ut", "dev"}

    переписать(
        дом / "bases.yaml",
        настройки(
            dev=None,
            ещё=(
                "  new:\n"
                "    label: Новая\n"
                "    url: http://localhost/new/odata/standard.odata/\n"
                "    user: u\n"
                f"    password: {ПАРОЛЬ}\n"
                "    role: test\n"
            ),
        ),
    )

    assert await имена_баз(сервис) == {"ut", "new"}
    assert (await найти(сервис, base="dev"))["error"]["code"] == "base_unknown"


async def test_состояние_индексации_переживает_перечитывание(сервис, дом):
    сервис._registry.set_indexed("ut", "2026-09-14T10:00:00", 7224)

    переписать(дом / "bases.yaml", настройки(ut="    gate:\n      mode: identifiers\n"))
    await найти(сервис)

    состояние = {с.name: с for с in сервис._registry.visible(SessionScope())}["ut"]
    assert (состояние.indexed, состояние.entity_count) == (True, 7224)
    assert состояние.gate_mode == "identifiers"
