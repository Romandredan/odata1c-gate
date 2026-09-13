"""Подпись лаунчера (Ruling 59, план M2 задача 9, раунд 2): механизм `claude_code` — только клиенту,
чьи заголовки подписал настоящий лаунчер ключом домашнего каталога.

Находка ревью задачи 9: демон без аутентификации до M4, и любой локальный процесс — `curl` из Bash
модели после prompt-injection в данных 1С — называл себя `claude-code` в `initialize` или заголовком
и получал механизм «подтверждает сам клиент»: `commit` без единого диалога. Подпись не делает такой
обход невозможным для процесса, который прочитает ключ, но поднимает его с одной команды до видимой
цепочки действий.

Здесь — части по отдельности: файл ключа, подпись и её проверка, хук лаунчера, выбор механизма.
Демон целиком через настоящий лаунчер и прямой HTTP-клиент с поддельными подписями —
`tests/integration/test_write_end_to_end.py`.
"""

import base64
import inspect
import os
import types as pytypes

import httpx2
import mcp.types as types
import pytest
import yaml

from odata1c import daemon
from odata1c.cli import cmd_init
from odata1c.config.home import ensure_home
from odata1c.config.writer import (
    LAUNCHER_KEY_FILE,
    ensure_gate_secret,
    ensure_launcher_key,
    read_launcher_key,
)
from odata1c.daemon import (
    CLIENT_ELICITATION_HEADER,
    CLIENT_NAME_HEADER,
    CLIENT_SIG_HEADER,
    CLIENT_VERSION_HEADER,
    SESSION_ID_HEADER,
    ClientIdentity,
    SessionMechanisms,
    client_from_request,
    client_signature,
)
from odata1c.launcher import client_signer

АДРЕС = "http://127.0.0.1:1/mcp"


# ---------------------------------------------------------------------------------------------
# Файл ключа
# ---------------------------------------------------------------------------------------------


def test_ключ_лаунчера_32_байта_создаётся_один_раз(tmp_path):
    ensure_home(tmp_path)
    ключ = ensure_launcher_key(tmp_path)
    assert isinstance(ключ, bytes) and len(ключ) == 32
    assert (tmp_path / LAUNCHER_KEY_FILE).read_bytes() == ключ
    assert ensure_launcher_key(tmp_path) == ключ
    assert read_launcher_key(tmp_path) == ключ
    другой = tmp_path / "другой"
    ensure_home(другой)
    assert ensure_launcher_key(другой) != ключ


def test_ключ_лаунчера_отдельно_от_секрета_гейта(tmp_path):
    ensure_home(tmp_path)
    секрет = ensure_gate_secret(tmp_path / "daemon.yaml")
    ключ = ensure_launcher_key(tmp_path)
    assert base64.b64decode(секрет) != ключ
    текст = (tmp_path / "daemon.yaml").read_text(encoding="utf-8")
    assert set(yaml.safe_load(текст)) == {"gate_secret"}
    assert base64.b64encode(ключ).decode() not in текст and ключ.hex() not in текст


@pytest.mark.parametrize("содержимое", [b"", b"x" * 31, b"x" * 33])
def test_битый_ключ_не_читается_и_заменяется(tmp_path, содержимое):
    ensure_home(tmp_path)
    (tmp_path / LAUNCHER_KEY_FILE).write_bytes(содержимое)
    assert read_launcher_key(tmp_path) is None
    ключ = ensure_launcher_key(tmp_path)
    assert len(ключ) == 32 and read_launcher_key(tmp_path) == ключ


def test_нет_ключа_читается_как_None(tmp_path):
    assert read_launcher_key(tmp_path) is None


@pytest.mark.skipif(os.name == "nt", reason="права файла на Windows — ACL каталога (icacls)")
def test_ключ_лаунчера_закрыт_правами_владельца(tmp_path):
    ensure_home(tmp_path)
    ensure_launcher_key(tmp_path)
    assert (tmp_path / LAUNCHER_KEY_FILE).stat().st_mode & 0o077 == 0


def test_init_создаёт_ключ_лаунчера(tmp_path, capsys):
    дом = tmp_path / "дом"
    assert cmd_init(дом) == 0
    ключ = read_launcher_key(дом)
    assert ключ is not None
    # Ни имя файла ключа, ни ключ в выводе команды не нужны.
    вывод = capsys.readouterr().out
    assert ключ.hex() not in вывод


# ---------------------------------------------------------------------------------------------
# Подпись и её проверка
# ---------------------------------------------------------------------------------------------


def test_подпись_привязана_к_сессии_и_каждому_полю():
    ключ = bytes(range(32))
    поля = ("sid-1", "claude-code", "2.1.267", "1")
    подпись = client_signature(ключ, *поля)
    assert подпись == client_signature(ключ, *поля)
    for номер in range(len(поля)):
        изменённые = list(поля)
        изменённые[номер] += "x"
        assert client_signature(ключ, *изменённые) != подпись, номер
    # Граница полей однозначна: перенос символа из одного поля в соседнее — другая подпись.
    assert client_signature(ключ, "sid-1", "claude-cod", "e2.1.267", "1") != подпись
    assert client_signature(bytes(32), *поля) != подпись
    подпись.encode("ascii")


def _ctx(headers=None, *, params=None, caps=None):
    сессия = pytypes.SimpleNamespace(
        client_params=params, client_capabilities=caps, can_send_request=True
    )
    return pytypes.SimpleNamespace(headers=headers, session=сессия)


def _заголовки(ключ, sid="s1", имя="claude-code", версия="2.1.267", elicitation="1", *, для=None):
    """Заголовки клиента, как их ставит лаунчер; `для` — чьей сессией подписано (по умолчанию
    своей), `ключ=None` — без подписи."""
    заголовки = {
        SESSION_ID_HEADER: sid,
        CLIENT_NAME_HEADER: имя,
        CLIENT_VERSION_HEADER: версия,
        CLIENT_ELICITATION_HEADER: elicitation,
    }
    if ключ is not None:
        заголовки[CLIENT_SIG_HEADER] = client_signature(ключ, для or sid, имя, версия, elicitation)
    return заголовки


def test_верная_подпись_делает_клиента_проверенным():
    ключ = os.urandom(32)
    assert client_from_request(_ctx(_заголовки(ключ)), ключ) == ClientIdentity(
        "claude-code", "2.1.267", True, verified=True
    )


def test_неверная_или_отсутствующая_подпись_клиент_не_проверен():
    ключ = os.urandom(32)
    случаи = {
        "без подписи": _заголовки(None),
        "подпись чужой сессии": _заголовки(ключ, "s2", для="s1"),
        "подпись другим ключом": _заголовки(os.urandom(32)),
        "не-ASCII подпись": {**_заголовки(ключ), CLIENT_SIG_HEADER: "подпись"},
        "пустая подпись": {**_заголовки(ключ), CLIENT_SIG_HEADER: ""},
        "поле изменено после подписи": {
            **_заголовки(ключ, elicitation="0"),
            CLIENT_ELICITATION_HEADER: "1",
        },
    }
    без_сессии = _заголовки(ключ)
    del без_сессии[SESSION_ID_HEADER]
    случаи["нет mcp-session-id"] = без_сессии
    for что, заголовки in случаи.items():
        клиент = client_from_request(_ctx(заголовки), ключ)
        assert клиент.verified is False, что
        # Имя и возможности читаются как прежде — меняется только доверие к ним.
        assert клиент.name == "claude-code", что
    # У демона нет ключа — не проверить никого, и верно подписанного тоже.
    assert client_from_request(_ctx(_заголовки(ключ)), None).verified is False


def test_клиент_из_initialize_не_проверен_никогда():
    """Прямой клиент без заголовков лаунчера называет себя в `initialize` — это не заверено
    ничем, и `claude_code` по такому имени не выдаётся (обратная форма `test_А1` ревьюера)."""
    ключ = os.urandom(32)
    параметры = types.InitializeRequestParams(
        protocol_version="2025-11-25",
        capabilities=types.ClientCapabilities(),
        client_info=types.Implementation(name="claude-code", version="2.1.267"),
    )
    клиент = client_from_request(_ctx({}, params=параметры), ключ)
    assert (клиент.name, клиент.verified) == ("claude-code", False)


def test_подпись_сравнивается_за_постоянное_время():
    """Проверка кода, не поведения: подпись сравнивается `hmac.compare_digest`, а не `==` —
    поведенческий тест разницы во времени не увидит."""
    источник = inspect.getsource(daemon._подпись_клиента_верна)
    assert "hmac.compare_digest(" in источник
    assert " == " not in источник and "!=" not in источник


# ---------------------------------------------------------------------------------------------
# Хук лаунчера
# ---------------------------------------------------------------------------------------------


def _запрос(заголовки: dict) -> httpx2.Request:
    return httpx2.Request("POST", АДРЕС, headers=заголовки)


async def test_лаунчер_подписывает_каждый_запрос_сессии():
    ключ = os.urandom(32)
    подписать = client_signer(ключ)
    заголовки = _заголовки(None)
    запрос = _запрос(заголовки)
    await подписать(запрос)
    assert client_from_request(_ctx(dict(запрос.headers)), ключ).verified is True
    # Другая сессия того же клиента — другая подпись.
    второй = _запрос({**заголовки, SESSION_ID_HEADER: "s2"})
    await подписать(второй)
    assert второй.headers[CLIENT_SIG_HEADER] != запрос.headers[CLIENT_SIG_HEADER]


async def test_лаунчер_не_подписывает_без_сессии_клиента_или_ключа():
    ключ = os.urandom(32)
    # `initialize` — сессии ещё нет.
    без_сессии = _заголовки(None)
    del без_сессии[SESSION_ID_HEADER]
    # Клиент ещё не передан (первый запрос downstream-сессии не пришёл).
    без_клиента = {SESSION_ID_HEADER: "s1"}
    for заголовки in (без_сессии, без_клиента):
        запрос = _запрос({**заголовки, CLIENT_SIG_HEADER: "stale"})
        await client_signer(ключ)(запрос)
        assert CLIENT_SIG_HEADER not in запрос.headers
    # Ключа нет — подписывать нечем.
    запрос = _запрос(_заголовки(None))
    await client_signer(None)(запрос)
    assert CLIENT_SIG_HEADER not in запрос.headers


# ---------------------------------------------------------------------------------------------
# Выбор механизма
# ---------------------------------------------------------------------------------------------


def test_claude_code_только_проверенному_клиенту():
    механизмы = SessionMechanisms("deny")
    assert механизмы.choose("a", ClientIdentity("claude-code", "2.1.267", False)) == "deny"
    assert механизмы.choose("b", ClientIdentity("claude-code", "2.1.267", True)) == "elicitation"
    проверенный = ClientIdentity("claude-code", "2.1.267", False, verified=True)
    assert механизмы.choose("c", проверенный) == "claude_code"
    # Подпись не отменяет прежних правил: похожее имя и старая версия — обычный клиент.
    похожий = ClientIdentity("Claude Code", "2.1.267", False, verified=True)
    assert механизмы.choose("d", похожий) == "deny"
    старый = ClientIdentity("claude-code", "2.1.245", True, verified=True)
    assert механизмы.choose("e", старый) == "elicitation"
    # `trust_client` — явная настройка владельца, от подписи не зависит.
    доверие = SessionMechanisms("trust_client")
    assert доверие.choose("f", ClientIdentity("claude-code", "2.1.267", False)) == "trust"


# ---------------------------------------------------------------------------------------------
# Демон: ключ читается при старте
# ---------------------------------------------------------------------------------------------


def _дом_с_базой(tmp_path, edmx_ut_real):
    import test_write_commit as к

    return к._дом(tmp_path, edmx_ut_real)


async def test_демон_без_ключа_предупреждает_не_называя_ключ(tmp_path, edmx_ut_real, caplog):
    from odata1c.config.loader import load_config
    from odata1c.tools.service import ToolService

    дом = _дом_с_базой(tmp_path, edmx_ut_real)
    # Дом собран `odata1c init` — ключ там есть; демон старого дома, где init был до Ruling 59, его
    # не найдёт.
    (дом / LAUNCHER_KEY_FILE).unlink()
    assert read_launcher_key(дом) is None
    служба = ToolService(load_config(дом))
    try:
        _проверить_чтение_ключа(дом, служба, caplog)
    finally:
        await служба.aclose()


def _проверить_чтение_ключа(дом, служба, caplog) -> None:
    слой = daemon.build_write_layer(служба)
    assert слой.launcher_key is None
    assert "ключ лаунчера" in caplog.text
    assert LAUNCHER_KEY_FILE not in caplog.text

    caplog.clear()
    ключ = ensure_launcher_key(дом)
    слой = daemon.build_write_layer(служба)
    assert слой.launcher_key == ключ
    assert "ключ лаунчера" not in caplog.text
    assert ключ.hex() not in caplog.text
    # Ключ читается при старте, а не с диска на каждый запрос: смена файла — только перезапуском.
    (дом / LAUNCHER_KEY_FILE).write_bytes(os.urandom(32))
    assert слой.launcher_key == ключ
