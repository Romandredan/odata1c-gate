"""Перенос баз из env-файла прежнего сервера и дописывание записей в bases.yaml."""

import yaml

from odata1c.config.importer import parse_env
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
