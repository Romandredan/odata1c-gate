"""Перенос баз из env-файла прежнего сервера и дописывание записей в bases.yaml."""

import yaml

from odata1c.config.importer import parse_env
from odata1c.config.loader import load_config
from odata1c.config.writer import append_base

ENV = """
ODATA_DEFAULT_DB=ut
READ_ONLY=false

ODATA_DB_UT_BASE_URL=https://1c.corp.local/ut/odata/standard.odata/
ODATA_DB_UT_USERNAME=odata_claude
ODATA_DB_UT_PASSWORD=секрет
ODATA_DB_UT_LABEL=УТ 11, боевая
ODATA_DB_UT_WRITABLE=true

ODATA_DB_BUH_BASE_URL=https://1c.corp.local/buh/odata/standard.odata/
ODATA_DB_BUH_USERNAME=odata_claude
ODATA_DB_BUH_PASSWORD=секрет2
ODATA_DB_BUH_LABEL=БП 3.0
"""


def test_разбор_env_файла():
    по_умолчанию, базы = parse_env(ENV)
    assert по_умолчанию == "ut"
    имена = {b["name"] for b in базы}
    assert имена == {"ut", "buh"}
    ut = next(b for b in базы if b["name"] == "ut")
    assert ut["url"] == "https://1c.corp.local/ut/odata/standard.odata/"
    assert ut["user"] == "odata_claude"
    assert ut["label"] == "УТ 11, боевая"
    assert ut["write"] is True
    assert ut["role"] == "prod"


def test_база_без_writable_не_пишущая():
    _, базы = parse_env(ENV)
    buh = next(b for b in базы if b["name"] == "buh")
    assert buh["write"] is False


def test_имя_базы_приводится_к_допустимому():
    env = "ODATA_DB_UT-ROZNICA_BASE_URL=http://x/odata/standard.odata/\n"
    _, базы = parse_env(env)
    assert базы[0]["name"] == "ut_roznica"


def test_дописывание_не_ломает_существующий_файл(tmp_path):
    path = tmp_path / "bases.yaml"
    path.write_text(
        "# комментарий шаблона\ndefault: ut\nbases:\n  ut:\n    label: УТ\n"
        "    url: http://x/odata/standard.odata/\n    user: u\n    password: p\n    role: prod\n",
        encoding="utf-8",
    )
    append_base(
        path,
        "buh",
        {
            "label": "БП",
            "url": "http://y/odata/standard.odata/",
            "user": "u2",
            "password": "p2",
            "role": "prod",
        },
    )

    текст = path.read_text(encoding="utf-8")
    assert "# комментарий шаблона" in текст
    данные = yaml.safe_load(текст)
    assert set(данные["bases"]) == {"ut", "buh"}
    assert данные["bases"]["buh"]["label"] == "БП"


def test_запись_добавляется_с_комментариями(tmp_path):
    path = tmp_path / "bases.yaml"
    path.write_text("bases:\n", encoding="utf-8")
    append_base(
        path,
        "ut",
        {
            "label": "УТ",
            "url": "http://x/odata/standard.odata/",
            "user": "u",
            "password": "p",
            "role": "prod",
        },
    )
    текст = path.read_text(encoding="utf-8")
    assert "# --- соединение" in текст
    assert "# concurrency:" in текст


def test_пароль_со_спецсимволами_переживает_запись_и_чтение(tmp_path):
    """Регресс: подстановка значения в строку-шаблон без YAML-экранирования ломает разметку
    файла на кавычке в пароле (`password: "p@ss"word"` — синтаксическая ошибка), а следующее
    чтение настроек падает с текстом самого пароля внутри сообщения об ошибке YAML."""
    home = tmp_path / "home"
    home.mkdir()
    path = home / "bases.yaml"
    path.write_text("bases:\n", encoding="utf-8")
    пароль = "p@ss\"word'with:colon#hash"
    append_base(
        path,
        "ut",
        {
            "label": "УТ",
            "url": "http://x/odata/standard.odata/",
            "user": "u",
            "password": пароль,
            "role": "prod",
        },
    )
    config = load_config(home)
    assert config.bases["ut"].password == пароль


def test_подпись_с_двоеточием_и_решёткой_переживает_запись_и_чтение(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    path = home / "bases.yaml"
    path.write_text("bases:\n", encoding="utf-8")
    подпись = "УТ: боевая # приоритет"
    append_base(
        path,
        "ut",
        {
            "label": подпись,
            "url": "http://x/odata/standard.odata/",
            "user": "u",
            "password": "p",
            "role": "prod",
        },
    )
    config = load_config(home)
    assert config.bases["ut"].label == подпись


def test_обрезка_длинного_имени_не_склеивает_разные_базы():
    """Регресс: normalize_name обрезает имя до 32 символов; два разных исходных идентификатора
    базы, различающиеся только хвостом за 32-м символом, раньше давали один и тот же ключ — записи
    молча перемешивались под одним именем."""
    длинное_1 = "A" * 32 + "_ONE"
    длинное_2 = "A" * 32 + "_TWO"
    env = (
        f"ODATA_DB_{длинное_1}_BASE_URL=http://one/odata/standard.odata/\n"
        f"ODATA_DB_{длинное_2}_BASE_URL=http://two/odata/standard.odata/\n"
    )
    _, базы = parse_env(env)

    имена = [b["name"] for b in базы]
    assert len(имена) == len(set(имена)) == 2
    по_url = {b["url"]: b["name"] for b in базы}
    имя_1 = по_url["http://one/odata/standard.odata/"]
    имя_2 = по_url["http://two/odata/standard.odata/"]
    assert имя_1 != имя_2
    assert all(len(n) <= 32 for n in имена)
