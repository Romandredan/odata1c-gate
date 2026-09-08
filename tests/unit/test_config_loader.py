"""Разбор bases.yaml и daemon.yaml: проверки значений и сообщения об ошибках."""

import pytest

from odata1c.config.loader import ConfigError, load_config

BASES = """
default: ut
bases:
  ut:
    label: УТ 11, боевая
    url: https://1c.corp.local/ut/odata/standard.odata/
    user: odata_claude
    password: "секрет"
    role: prod
"""


def записать(tmp_path, bases: str = BASES, daemon: str | None = None):
    (tmp_path / "bases.yaml").write_text(bases, encoding="utf-8")
    if daemon is not None:
        (tmp_path / "daemon.yaml").write_text(daemon, encoding="utf-8")
    (tmp_path / "bases").mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    return tmp_path


def test_разбор_минимальной_настройки(tmp_path):
    config = load_config(записать(tmp_path))
    assert set(config.bases) == {"ut"}
    assert config.default == "ut"
    assert config.bases["ut"].label == "УТ 11, боевая"
    assert config.bases["ut"].concurrency == 2
    assert config.bases["ut"].timeout_s == 60


def test_умолчания_демона_без_файла(tmp_path):
    config = load_config(записать(tmp_path))
    assert config.daemon.port == 7171
    assert config.daemon.limits.top_default == 50
    assert config.daemon.limits.top_max == 1000
    assert config.daemon.limits.expand_depth == 2
    assert config.daemon.limits.result_chars == 120_000
    assert config.daemon.limits.string_chars == 2_000
    assert config.daemon.limits.pending_ttl_s == 600
    assert config.daemon.write_confirm_fallback == "deny"
    assert len(config.daemon.gate_secret) >= 40  # 32 байта в base64


def test_секрет_гейта_сохраняется_между_запусками(tmp_path):
    home = записать(tmp_path)
    первый = load_config(home).daemon.gate_secret
    assert load_config(home).daemon.gate_secret == первый


def test_url_без_стандартного_окончания_отклоняется(tmp_path):
    плохой = BASES.replace("/odata/standard.odata/", "/odata/")
    with pytest.raises(ConfigError) as ошибка:
        load_config(записать(tmp_path, плохой))
    assert "odata/standard.odata" in str(ошибка.value)


def test_недопустимое_имя_базы_отклоняется(tmp_path):
    плохой = BASES.replace("  ut:", "  UT-Боевая:")
    with pytest.raises(ConfigError) as ошибка:
        load_config(записать(tmp_path, плохой))
    assert "имя базы" in str(ошибка.value).lower()


def test_база_по_умолчанию_должна_существовать(tmp_path):
    плохой = BASES.replace("default: ut", "default: нет_такой")
    with pytest.raises(ConfigError):
        load_config(записать(tmp_path, плохой))


def test_отсутствие_файла_баз_не_ошибка_а_пустой_список(tmp_path):
    (tmp_path / "bases").mkdir()
    (tmp_path / "logs").mkdir()
    config = load_config(tmp_path)
    assert config.bases == {}
    assert config.default is None
