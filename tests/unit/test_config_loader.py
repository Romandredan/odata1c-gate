"""Разбор bases.yaml и daemon.yaml: проверки значений и сообщения об ошибках."""

import concurrent.futures

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

# Секрет для тестов ниже — просто непустая строка правдоподобной длины (32 байта в base64);
# его создание и запись на диск проверяются отдельно, на уровне odata1c.config.writer
# (ensure_gate_secret) и команды odata1c init (tests/unit/test_cli_base.py) — здесь loader
# только читает то, что уже лежит в файле.
СЕКРЕТ = "s" * 44
DAEMON_С_СЕКРЕТОМ = f'gate_secret: "{СЕКРЕТ}"\n'


def записать(tmp_path, bases: str = BASES, daemon: str | None = None):
    (tmp_path / "bases.yaml").write_text(bases, encoding="utf-8")
    (tmp_path / "daemon.yaml").write_text(
        daemon if daemon is not None else DAEMON_С_СЕКРЕТОМ, encoding="utf-8"
    )
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


def test_умолчания_демона_кроме_секрета(tmp_path):
    """daemon.yaml чтением не создаётся и не дополняется — секрет должен быть в файле
    заранее (как после odata1c init), остальные поля при этом берутся из умолчаний."""
    config = load_config(записать(tmp_path))
    assert config.daemon.port == 7171
    assert config.daemon.limits.top_default == 50
    assert config.daemon.limits.top_max == 1000
    assert config.daemon.limits.expand_depth == 2
    assert config.daemon.limits.result_chars == 120_000
    assert config.daemon.limits.string_chars == 2_000
    assert config.daemon.limits.pending_ttl_s == 600
    assert config.daemon.write_confirm_fallback == "deny"
    assert config.daemon.gate_secret == СЕКРЕТ


def test_чтение_настроек_с_пустым_секретом_даёт_ошибку_и_не_пишет_на_диск(tmp_path):
    """Регресс: раньше пустой gate_secret заставлял чтение настроек сгенерировать секрет и
    записать его на диск. Теперь это ошибка настроек с подсказкой, а файл не трогается —
    ни один байт daemon.yaml не должен измениться."""
    home = записать(tmp_path, daemon="port: 7171\n")
    исходные_байты = (home / "daemon.yaml").read_bytes()

    with pytest.raises(ConfigError) as ошибка:
        load_config(home)

    assert ошибка.value.hint
    assert "init" in ошибка.value.hint
    assert (home / "daemon.yaml").read_bytes() == исходные_байты


def test_комментарии_daemon_yaml_остаются_после_чтения_настроек(tmp_path):
    daemon_с_комментарием = (
        "# комментарий шаблона про gate_secret, не должен пропасть при чтении\n"
        f'gate_secret: "{СЕКРЕТ}"\n'
        "port: 7171\n"
    )
    home = записать(tmp_path, daemon=daemon_с_комментарием)

    load_config(home)

    assert (home / "daemon.yaml").read_text(encoding="utf-8") == daemon_с_комментарием


def test_параллельные_чтения_настроек_не_расходятся_в_секрете_и_не_пишут_на_диск(tmp_path):
    """Регресс, воспроизведённый ревьюером: раньше при пустом gate_secret чтение настроек
    само его генерировало и писало на диск — четыре одновременных чтения на свежем каталоге
    давали четыре РАЗНЫХ секрета в памяти, а на диске оставался только один. Теперь чтение
    ничего не генерирует и не пишет: расходиться им физически неоткуда — все параллельные
    чтения видят один и тот же секрет, уже лежащий на диске, и файл после них не меняется.
    """
    home = записать(tmp_path)
    до = (home / "daemon.yaml").read_bytes()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as пул:
        секреты = list(пул.map(lambda _: load_config(home).daemon.gate_secret, range(4)))

    assert len(set(секреты)) == 1
    assert секреты[0] == СЕКРЕТ
    assert (home / "daemon.yaml").read_bytes() == до


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


def test_default_списком_отклоняется(tmp_path):
    """Регресс: default, заданный списком, роняет load_config необработанным TypeError
    (unhashable type: 'list') на проверке `default not in bases`."""
    плохой = BASES.replace("default: ut", "default: [ut, buh]")
    with pytest.raises(ConfigError) as ошибка:
        load_config(записать(tmp_path, плохой))
    assert "default" in str(ошибка.value).lower()


def test_запись_базы_строкой_отклоняется(tmp_path):
    """Регресс: raw.get(...) на строке роняет AttributeError вместо понятной ошибки."""
    плохой = "bases:\n  ut: просто_строка\n"
    with pytest.raises(ConfigError) as ошибка:
        load_config(записать(tmp_path, плохой))
    assert "ut" in str(ошибка.value)


def test_ключ_name_внутри_записи_базы_отклоняется(tmp_path):
    """Регресс: ключ name внутри записи базы дублирует именованный аргумент name=name
    при построении BaseConfig и роняет TypeError вместо понятной ошибки."""
    плохой = BASES.replace("  ut:\n", "  ut:\n    name: другое_имя\n")
    with pytest.raises(ConfigError) as ошибка:
        load_config(записать(tmp_path, плохой))
    assert "name" in str(ошибка.value)


def test_отсутствие_файла_баз_не_ошибка_а_пустой_список(tmp_path):
    (tmp_path / "daemon.yaml").write_text(DAEMON_С_СЕКРЕТОМ, encoding="utf-8")
    (tmp_path / "bases").mkdir()
    (tmp_path / "logs").mkdir()
    config = load_config(tmp_path)
    assert config.bases == {}
    assert config.default is None


def test_битый_bases_yaml_даёт_понятную_ошибку_с_именем_файла(tmp_path):
    битый = "default: ut\nbases:\n  ut: [не_закрыта_скобка\n"
    with pytest.raises(ConfigError) as ошибка:
        load_config(записать(tmp_path, битый))
    assert "bases.yaml" in str(ошибка.value)


def test_битый_daemon_yaml_даёт_понятную_ошибку_с_именем_файла(tmp_path):
    битый_daemon = "port: [не_закрыта_скобка\n"
    with pytest.raises(ConfigError) as ошибка:
        load_config(записать(tmp_path, BASES, битый_daemon))
    assert "daemon.yaml" in str(ошибка.value)


def test_битый_yaml_на_строке_с_паролем_не_содержит_пароль_в_сообщении(tmp_path):
    """Регресс: PyYAML вклеивает в текст исключения фрагмент файла вокруг места ошибки —
    если повреждение пришлось на строку с паролем, пароль попадал в сообщение дословно."""
    пароль = "супер_секретный_пароль_XYZ"
    битый = f'bases:\n  ut:\n    password: "{пароль}\n    role: prod\n'
    with pytest.raises(ConfigError) as ошибка:
        load_config(записать(tmp_path, bases=битый))
    текст = str(ошибка.value)
    assert пароль not in текст
    assert "строка" in текст


def test_неизвестная_роль_называет_базу_и_роль(tmp_path):
    плохой = BASES.replace("role: prod", "role: qa")
    with pytest.raises(ConfigError) as ошибка:
        load_config(записать(tmp_path, плохой))
    текст = str(ошибка.value).lower()
    assert "ut" in текст
    assert "роль" in текст


def test_пустая_запись_базы_даёт_предупреждение_и_не_падает(tmp_path):
    с_пустой_записью = BASES + "  пустая:\n"
    config = load_config(записать(tmp_path, с_пустой_записью))
    assert set(config.bases) == {"ut"}
    assert any("пустая" in предупреждение for предупреждение in config.warnings)
