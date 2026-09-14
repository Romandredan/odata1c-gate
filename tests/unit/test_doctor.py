"""`odata1c doctor` — проверка окружения без секретов в выводе (план M3, задача 2).

Каждая проверка изолирована через инъекции `which`/`run`/`connect` (`doctor.run`) — ни один тест
не трогает настоящий `uv`, настоящий `claude` или демон владельца на 7171. CLI-тест ниже — то же
самое через `main(["doctor", …])`, но со своими значениями `which`/`run` по умолчанию
(`shutil.which`/`subprocess.run`): порт демона подменяется через `monkeypatch` на
`doctor.is_listening` (поздно связанное имя, которое `doctor.опросить_демон` смотрит в момент
вызова, а не при определении функции) — иначе `cmd_doctor` без явного `connect` стучался бы
в порт 7171 рабочего дома (AGENTS.md: «демон владельца исполнители не трогают»)."""

from __future__ import annotations

import subprocess

import httpx
import respx

import odata1c
from odata1c import doctor
from odata1c.cli import main
from odata1c.doctor import exit_code, render, run

URL = "https://1c.example.local/ut/odata/standard.odata/"
BASES = f"""
default: ut
bases:
  ut:
    label: УТ 11, тестовая
    url: {URL}
    user: кто-то
    password: p@ss
    role: test
"""


def _статус(проверки, имя):
    for проверка in проверки:
        if проверка.name == имя:
            return проверка.status
    raise AssertionError(f"нет проверки «{имя}» среди {[п.name for п in проверки]}")


def _деталь(проверки, имя):
    for проверка in проверки:
        if проверка.name == имя:
            return проверка.detail
    raise AssertionError(f"нет проверки «{имя}» среди {[п.name for п in проверки]}")


def _which_нет(name: str) -> str | None:
    return None


def _run_нет(*args, **kwargs):
    raise FileNotFoundError("бинарник не найден — этот тест его не вызывает")


def _демон_молчит(port: int) -> str | None:
    return None


# --- Step 1 брифа: восемь проверок run()/render()/exit_code() -------------------------------


def test_нет_дома_fail(tmp_path):
    проверки = run(
        tmp_path / "нет", online=False, which=_which_нет, run=_run_нет, connect=_демон_молчит
    )

    assert _статус(проверки, "дом шлюза") == "FAIL"
    assert exit_code(проверки) == 2
    # остальные строки всё равно построены — «дом шлюза» не обрывает таблицу (докстринг run())
    assert _статус(проверки, "демон") == "WARN"
    assert _статус(проверки, "Claude Code") == "WARN"


def test_шаблонный_дом_без_баз_warn(tmp_path):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])

    проверки = run(home, online=False, which=_which_нет, run=_run_нет, connect=_демон_молчит)

    assert _статус(проверки, "bases.yaml") == "WARN"
    assert "баз нет" in _деталь(проверки, "bases.yaml")
    assert exit_code(проверки) == 1


def test_битый_bases_yaml_fail_без_значений(tmp_path):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(
        "bases:\n  ut:\n    password: 'p@ss\n",  # не закрытая кавычка — синтаксическая ошибка
        encoding="utf-8",
    )

    проверки = run(home, online=False, which=_which_нет, run=_run_нет, connect=_демон_молчит)

    assert _статус(проверки, "bases.yaml") == "FAIL"
    деталь = _деталь(проверки, "bases.yaml")
    assert "p@ss" not in деталь
    assert str(home / "bases.yaml") in деталь
    assert "строка" in деталь
    assert exit_code(проверки) == 2


def test_база_без_индекса_warn(tmp_path):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")

    проверки = run(home, online=False, which=_which_нет, run=_run_нет, connect=_демон_молчит)

    assert _статус(проверки, "база ut") == "WARN"
    assert "индекса нет" in _деталь(проверки, "база ut")


def test_uv_и_claude_найдены(tmp_path):
    def which(name: str) -> str | None:
        return {"uv": "C:/tools/uv.exe", "claude": "C:/tools/claude.exe"}.get(name)

    def run_(cmd, **kwargs):
        текст = "uv 0.9.24\n" if cmd[0] == "C:/tools/uv.exe" else "2.1.267 (Claude Code)\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=текст.encode("utf-8"))

    проверки = run(tmp_path / "нет", online=False, which=which, run=run_, connect=_демон_молчит)

    assert _статус(проверки, "uv") == "OK"
    assert "0.9.24" in _деталь(проверки, "uv")
    assert _статус(проверки, "Claude Code") == "OK"
    assert "2.1.267" in _деталь(проверки, "Claude Code")


def test_демон_не_запущен_warn(tmp_path):
    проверки = run(
        tmp_path / "нет", online=False, which=_which_нет, run=_run_нет, connect=lambda порт: None
    )

    assert _статус(проверки, "демон") == "WARN"
    assert "не запущен" in _деталь(проверки, "демон")


def test_демон_другой_версии_fail(tmp_path):
    assert odata1c.__version__ != "0.0.9"  # предпосылка теста

    проверки = run(
        tmp_path / "нет",
        online=False,
        which=_which_нет,
        run=_run_нет,
        connect=lambda порт: "0.0.9",
    )

    assert _статус(проверки, "демон") == "FAIL"
    assert "daemon stop" in _деталь(проверки, "демон")


def test_render_не_печатает_url_user_password(tmp_path):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")

    проверки = run(home, online=False, which=_which_нет, run=_run_нет, connect=_демон_молчит)
    текст = render(проверки)

    assert "p@ss" not in текст
    assert "кто-то" not in текст
    assert "https://1c.example.local" in текст
    assert "/ut/odata/standard.odata/" not in текст


@respx.mock
def test_online_вызывает_base_test(tmp_path):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    respx.get(f"{URL}$metadata").mock(
        return_value=httpx.Response(
            200, text="<edmx:Edmx/>", headers={"Content-Type": "application/xml"}
        )
    )
    respx.get(URL).mock(return_value=httpx.Response(200, json={"value": []}))

    проверки = run(home, online=True, which=_which_нет, run=_run_нет, connect=_демон_молчит)

    assert _статус(проверки, "соединение с ut") == "OK"


@respx.mock
def test_online_отказ_1с_не_разглашает_пароль(tmp_path):
    """`--online` на отказавшей 1С: текст ошибки OData (`cli.cmd_base_test` берёт его как есть,
    тот же путь здесь) не должен всё равно протащить пароль или имя пользователя базы — Basic Auth
    отправляется в заголовке запроса, а не оказывается в тексте исключения httpx."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    respx.get(f"{URL}$metadata").mock(return_value=httpx.Response(401, text="Unauthorized"))

    проверки = run(home, online=True, which=_which_нет, run=_run_нет, connect=_демон_молчит)
    текст = render(проверки)

    assert _статус(проверки, "соединение с ut") == "FAIL"
    assert "p@ss" not in текст
    assert "кто-то" not in текст


@respx.mock
def test_online_5xx_не_разглашает_полный_адрес(tmp_path):
    """Регресс: страница прокси/веб-сервера перед 1С на 5xx нередко повторяет запрошенный адрес
    целиком (проба P7 — 1С сама эхом повторяет присланное в шести формах запроса из четырнадцати).
    `OdataError._map_error` берёт такое тело как есть (`body.strip()[:500]`) — без явной чистки
    `_check_connection`/`_без_адреса` полный путь базы (не только схема и хост) попал бы в
    render(), нарушая footnote design §8 «адрес базы — только схема и хост»."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    respx.get(f"{URL}$metadata").mock(
        return_value=httpx.Response(502, text=f"Ошибка прокси при обращении к {URL}$metadata")
    )

    проверки = run(home, online=True, which=_which_нет, run=_run_нет, connect=_демон_молчит)
    текст = render(проверки)

    assert _статус(проверки, "соединение с ut") == "FAIL"
    assert "/ut/odata/standard.odata/" not in текст
    assert "https://1c.example.local" in текст


# --- Step 4 брифа: CLI --------------------------------------------------------------------


def test_cli_doctor_печатает_таблицу_и_возвращает_худший_код(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    # doctor.run без явного connect использует doctor.опросить_демон по умолчанию — тот сначала
    # зовёт is_listening(port), поздно связанное имя модуля: подмена здесь не даёт cmd_doctor
    # стучаться в порт 7171 рабочего дома владельца (домашний каталог этого теста — tmp_path,
    # но порт демона в daemon.yaml по умолчанию всё равно 7171).
    monkeypatch.setattr(doctor, "is_listening", lambda *a, **k: False)

    код = main(["doctor", "--home", str(home)])
    вывод = capsys.readouterr().out

    assert код == 1  # шаблонный дом: bases.yaml WARN «баз нет» — худшая строка
    assert "bases.yaml" in вывод
    assert "дом шлюза" in вывод
    assert "демон" in вывод
