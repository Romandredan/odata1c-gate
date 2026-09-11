"""Команда CLI reindex: код и подсказка на всех путях ошибок (SPEC §4.3, §5.2).

Правки по итогам ревью задачи 5 (Important): невалидное описание $metadata раньше роняло
команду необработанным traceback'ом — EdmxError не перехватывалась в main(). Здесь же
проверены и остальные три ручных случая, названных в ревью: несуществующая база, недоступный
адрес, база без индекса (обычный первый реиндекс).
"""

import httpx
import respx

from odata1c.cli import main

URL = "http://localhost/ut/odata/standard.odata/"
BASES = f"""
default: ut
bases:
  ut:
    label: УТ 11, тестовая
    url: {URL}
    user: u
    password: p
    role: test
"""


def _домашний_с_базой(tmp_path):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    return home


def _замокать_завершение_сеанса() -> None:
    # client.close() при ib_session=True (по умолчанию) шлёт завершение сеанса на тот же
    # адрес без хвоста пути — тот же приём, что в test_cli_base.py и test_index_reindex.py.
    respx.get(URL).mock(return_value=httpx.Response(200, json={"value": []}))


@respx.mock
def test_reindex_строит_индекс_на_базе_без_индекса(tmp_path, capsys, edmx_synthetic):
    home = _домашний_с_базой(tmp_path)
    respx.get(f"{URL}$metadata").mock(return_value=httpx.Response(200, content=edmx_synthetic))
    _замокать_завершение_сеанса()

    код = main(["reindex", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "индекс обновлён" in вывод
    assert (home / "bases" / "ut" / "metadata.sqlite").exists()


@respx.mock
def test_reindex_без_изменений_пересобирает_устаревший_auto(tmp_path, capsys, edmx_ut_real):
    """Находка П1, доставка правки классификатора через командную строку: `$metadata` тот же,
    а раздел `auto` устарел (так его записал прежний классификатор) — обычный `reindex` без
    `--force` обязан его пересобрать и сказать об этом; повторный вызов — промолчать."""
    import yaml

    home = _домашний_с_базой(tmp_path)
    respx.get(f"{URL}$metadata").mock(return_value=httpx.Response(200, content=edmx_ut_real))
    _замокать_завершение_сеанса()
    assert main(["reindex", "ut", "--home", str(home)]) == 0
    путь = home / "bases" / "ut" / "policy.yaml"
    политика = yaml.safe_load(путь.read_text(encoding="utf-8"))
    политика["auto"]["Catalog_Контрагенты.ЮрФизЛицо"] = "person"
    путь.write_text(yaml.safe_dump(политика, allow_unicode=True), encoding="utf-8")
    capsys.readouterr()

    assert main(["reindex", "ut", "--home", str(home)]) == 0
    вывод = capsys.readouterr().out

    assert "без изменений" in вывод
    assert "политика обновлена" in вывод
    политика = yaml.safe_load(путь.read_text(encoding="utf-8"))
    assert "Catalog_Контрагенты.ЮрФизЛицо" not in политика["auto"]

    assert main(["reindex", "ut", "--home", str(home)]) == 0
    assert "политика обновлена" not in capsys.readouterr().out


def test_reindex_неизвестной_базы(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)

    код = main(["reindex", "нет_такой", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "base_unknown" in вывод
    assert "traceback" not in вывод.lower()


@respx.mock
def test_reindex_недоступный_адрес(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)
    respx.get(f"{URL}$metadata").mock(side_effect=httpx.ConnectError("не удалось соединиться"))

    код = main(["reindex", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "odata_error" in вывод
    assert "подсказка" in вывод.lower()  # приведено к общему виду с остальными командами


@respx.mock
def test_reindex_невалидные_метаданные_дают_понятную_ошибку(tmp_path, capsys):
    home = _домашний_с_базой(tmp_path)
    respx.get(f"{URL}$metadata").mock(
        return_value=httpx.Response(200, content=b"<edmx:Edmx><ne-zakryt>")
    )
    _замокать_завершение_сеанса()

    код = main(["reindex", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "odata_error" in вывод
    assert "подсказка" in вывод.lower()
    assert "traceback" not in вывод.lower()


@respx.mock
def test_reindex_повреждённый_индекс_даёт_понятную_ошибку_а_не_трейсбек(
    tmp_path, capsys, edmx_synthetic
):
    # Правка по итогам финального ревью M1b (Important): reindex открывает прежний индекс перед
    # перестройкой (_прежнее_состояние), поэтому повреждённый файл индекса раньше выходил
    # необработанным traceback'ом — IndexCorruptError не была в перечне перехвата main().
    home = _домашний_с_базой(tmp_path)
    индекс = home / "bases" / "ut" / "metadata.sqlite"
    индекс.parent.mkdir(parents=True, exist_ok=True)
    индекс.write_bytes(b"not a real sqlite database, just random junk bytes")
    respx.get(f"{URL}$metadata").mock(return_value=httpx.Response(200, content=edmx_synthetic))
    _замокать_завершение_сеанса()

    код = main(["reindex", "ut", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1
    assert "index_corrupt" in вывод
    assert "подсказка" in вывод.lower()
    assert "traceback" not in вывод.lower()
