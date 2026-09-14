"""`odata1c doctor` — проверка окружения без секретов в выводе (план M3, задача 2).

Каждая проверка изолирована через инъекции `which`/`run`/`connect` (`doctor.run`) — ни один тест
не трогает настоящий `uv`, настоящий `claude` или демон владельца на 7171. CLI-тест ниже — то же
самое через `main(["doctor", …])`, но со своими значениями `which`/`run` по умолчанию
(`shutil.which`/`subprocess.run`): порт демона подменяется через `monkeypatch` на
`doctor.is_listening` (поздно связанное имя, которое `doctor.опросить_демон` смотрит в момент
вызова, а не при определении функции) — иначе `cmd_doctor` без явного `connect` стучался бы
в порт 7171 рабочего дома (AGENTS.md: «демон владельца исполнители не трогают»)."""

from __future__ import annotations

import json
import subprocess

import httpx
import pytest
import respx

import odata1c
from odata1c import doctor
from odata1c.cli import main
from odata1c.doctor import exit_code, render, run
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository

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


# --- Ruling 74 (ревью раунда 1): «соединение с <имя>» — только класс исхода, без текста 1С -----


@respx.mock
def test_online_401_с_именем_пользователя_1с_не_разглашает_имя(tmp_path):
    """Находка M-1, форма 1 итогового ревью: 401 с разобранным телом платформы, где 1С называет
    имя пользователя в тексте отказа («Пользователю ivanov отказано…»). Ruling 74 — строка не
    печатает текст 1С вовсе, только класс «отказ аутентификации (<401|403>)»."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    тело = json.dumps(
        {
            "odata.error": {
                "code": "",
                "message": {
                    "value": "Доступ запрещён. Пользователю ivanov отказано в праве "
                    "использовать WEB-сервис."
                },
            }
        }
    )
    respx.get(f"{URL}$metadata").mock(return_value=httpx.Response(401, text=тело))

    проверки = run(home, online=True, which=_which_нет, run=_run_нет, connect=_демон_молчит)
    текст = render(проверки)

    assert _статус(проверки, "соединение с ut") == "FAIL"
    assert _деталь(проверки, "соединение с ut") == "отказ аутентификации (401)"
    assert "ivanov" not in текст
    assert "p@ss" not in текст
    assert "кто-то" not in текст


@respx.mock
def test_online_5xx_не_разглашает_путь_публикации(tmp_path):
    """Находка M-1, форма 2: ошибку отдал не 1С, а веб-сервер/посредник перед ней
    (`platform_error=False`) — страница называет путь публикации без схемы и хоста, поэтому
    прежний `_без_адреса` (вырезал только `base.url` целиком) его не ловил. Ruling 74 — деталь
    строки теперь вообще не содержит тело ответа, только «HTTP <код>»."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    respx.get(f"{URL}$metadata").mock(
        return_value=httpx.Response(
            502, text="The requested URL /ut/odata/standard.odata/$metadata was not found"
        )
    )

    проверки = run(home, online=True, which=_which_нет, run=_run_нет, connect=_демон_молчит)
    текст = render(проверки)

    assert _статус(проверки, "соединение с ut") == "FAIL"
    assert _деталь(проверки, "соединение с ut") == "HTTP 502"
    assert "/ut/odata/standard.odata/" not in текст
    # схема и хост остаются видны — но только из соседней строки «база ut», не из этой
    assert "https://1c.example.local" in текст


@respx.mock
def test_online_сеть_недоступна_класс_сеть_таймаут(tmp_path):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    respx.get(f"{URL}$metadata").mock(side_effect=httpx.ConnectError("connection refused"))

    проверки = run(home, online=True, which=_which_нет, run=_run_нет, connect=_демон_молчит)

    assert _статус(проверки, "соединение с ut") == "FAIL"
    assert _деталь(проверки, "соединение с ut") == "сеть/таймаут"


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_online_сертификат_не_найден_класс_tls(tmp_path):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    сертификат = (tmp_path / "нет_такого.pem").as_posix()
    (home / "bases.yaml").write_text(
        "bases:\n"
        "  ut:\n"
        "    label: УТ\n"
        f"    url: {URL}\n"
        "    user: u\n"
        "    password: p\n"
        "    role: test\n"
        f'    verify_tls: "{сертификат}"\n',
        encoding="utf-8",
    )

    проверки = run(home, online=True, which=_which_нет, run=_run_нет, connect=_демон_молчит)

    assert _статус(проверки, "соединение с ut") == "FAIL"
    assert _деталь(проверки, "соединение с ut") == "TLS"


# --- Ruling 75 (ревью раунда 1): run() только читает, ни одной записи в файлы -----------------


def test_run_не_пишет_в_файл_индекса(tmp_path):
    """Находка M-2: `IndexRepository` без `read_only=True` заводит `PRAGMA journal_mode=WAL`
    (файлы `-wal`/`-shm` рядом) и дописывает недостающие таблицы/колонки схемы — на индексе
    ПРЕЖНЕЙ версии разбора (тот самый случай, ради которого строка «база <имя>» вообще открывает
    файл) это молча меняло файл владельца. Проверяем побайтово: ни размер файла, ни его содержимое
    не меняются, и не появляется ни `-wal`, ни `-shm`."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")

    путь = index_path(home, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    репозиторий = IndexRepository(путь)  # старая версия разбора: parser_version не проставлен
    репозиторий.close()
    до = путь.read_bytes()
    wal = путь.with_name(путь.name + "-wal")
    shm = путь.with_name(путь.name + "-shm")
    assert not wal.exists() and not shm.exists()

    run(home, online=False, which=_which_нет, run=_run_нет, connect=_демон_молчит)

    assert путь.read_bytes() == до
    assert not wal.exists()
    assert not shm.exists()


# --- Ruling 76 (ревью раунда 1): общий перехват, run() не бросает никогда ---------------------


def test_повреждённый_индекс_не_роняет_run(tmp_path):
    """Находка M-3, п. 1: конструктор `IndexRepository` был снаружи `try` в `_check_base` — файл,
    который вообще не открывается как SQLite, ронял `run()` целиком (`IndexCorruptError` наружу),
    а не давал одну строку WARN. Файл — реальный мусор, не заготовленный edmx/sqlite."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    путь = index_path(home, "ut")
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_bytes(b"\x00\x01\x02 not a sqlite file at all " * 20)

    проверки = run(home, online=False, which=_which_нет, run=_run_нет, connect=_демон_молчит)

    # Ревью назвал исход точно: «строка станет WARN «индекс повреждён»» — не крах, не FAIL
    # (реиндекс чинит это без потери данных, в отличие от ошибки самой политики).
    assert _статус(проверки, "база ut") == "WARN"
    assert "индекс повреждён" in _деталь(проверки, "база ut")
    assert exit_code(проверки) == 1
    # остальные строки таблицы всё равно построены — крах одной базы не обрывает run()
    assert _статус(проверки, "демон") == "WARN"


def test_нечисловой_порт_в_адресе_не_роняет_run(tmp_path):
    """Находка M-3, п. 2: `urlsplit(...).port` бросает `ValueError` на нечисловом порте в адресе
    (`BaseConfig._проверить_url` проверяет только окончание строки — до `.port` дело не доходит).
    `_хост` теперь ловит это сама, а `_безопасно` — запасная линия, если где-то ещё не поймали."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(
        "bases:\n"
        "  ut:\n"
        "    label: УТ\n"
        "    url: https://1c.example.local:непорт/odata/standard.odata/\n"
        "    user: u\n"
        "    password: p\n"
        "    role: test\n",
        encoding="utf-8",
    )

    проверки = run(home, online=False, which=_which_нет, run=_run_нет, connect=_демон_молчит)

    # Не крах: строка «база ut» строится с диагностикой адреса и WARN (индекса и политики у этой
    # базы тоже нет — тот же худший статус, что и у обычного «индекса нет»), а не пятая аварийная
    # FAIL-строка `_безопасно` с одним только именем класса исключения.
    assert _статус(проверки, "база ut") == "WARN"
    assert "порт не число" in _деталь(проверки, "база ut")
    assert exit_code(проверки) == 1


# --- Minor находки ревью раунда 1 --------------------------------------------------------------


def test_bases_yaml_права_дают_warn(tmp_path, monkeypatch):
    """m-1: шаг 3 брифа требует `check_file_permissions` и на дом, и на сам `bases.yaml` — был
    реализован только первый. Права на файл с паролями 1С открытым текстом важнее прав на пустой
    каталог вокруг него. Настоящий `icacls` не трогаем — подменяем `check_file_permissions`
    так, чтобы отвечать только на путь `bases.yaml`, как это бывает в реальности (два разных
    файла могут иметь разные права)."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(BASES, encoding="utf-8")
    путь_bases = home / "bases.yaml"

    def права(путь):
        if путь == путь_bases:
            return f"{путь} доступен другим учётным записям: TESTS\\Все:(R)"
        return None

    monkeypatch.setattr(doctor, "check_file_permissions", права)

    проверки = run(home, online=False, which=_which_нет, run=_run_нет, connect=_демон_молчит)

    assert _статус(проверки, "bases.yaml") == "WARN"
    assert "доступен другим учётным записям" in _деталь(проверки, "bases.yaml")


def test_демон_порт_из_daemon_yaml_даже_при_битом_bases_yaml(tmp_path):
    """m-2: `config` остаётся `None`, если `bases.yaml` не разобрался, а порт демона раньше в
    этом случае всегда брался как умолчание 7171 — даже когда `daemon.yaml` исправен и называет
    другой порт. `daemon` должен опрашиваться по НАСТОЯЩЕМУ порту независимо от судьбы соседнего
    файла."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "daemon.yaml").write_text('gate_secret: "секрет"\nport: 7999\n', encoding="utf-8")
    (home / "bases.yaml").write_text(
        "bases:\n  ut:\n    password: 'не закрытая кавычка\n", encoding="utf-8"
    )

    увиденные_порты = []

    def connect(port):
        увиденные_порты.append(port)
        return None

    run(home, online=False, which=_which_нет, run=_run_нет, connect=connect)

    assert увиденные_порты == [7999]


def test_launcher_key_виден_при_битом_bases_yaml(tmp_path):
    """m-3: строка `launcher.key` не зависит от того, разобрался ли `bases.yaml` — она читает
    свой файл напрямую (`read_launcher_key`). Была спрятана под `if config is not None`, и
    владелец, чинящий `bases.yaml`, заодно терял диагностику ключа лаунчера."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text("bases: [не, словарь]\n", encoding="utf-8")

    проверки = run(home, online=False, which=_which_нет, run=_run_нет, connect=_демон_молчит)

    assert _статус(проверки, "bases.yaml") == "FAIL"
    assert _статус(проверки, "launcher.key") == "OK"  # odata1c init уже создал ключ


def test_неизвестная_роль_даёт_строку_настройки_fail_без_значений(tmp_path):
    """m-4: строка `настройки` (запасной путь — `load_config` отказал глубже, чем ловят
    отдельные разборы `bases.yaml`/`daemon.yaml`) была объявлена в докстринге модуля, но не
    исполнялась ни одним тестом. `role: prodd` — оба файла по отдельности разбираются, но
    `apply_role` внутри `load_config` не знает такой роли."""
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(
        f"bases:\n  ut:\n    label: УТ\n    url: {URL}\n    user: u\n    password: p@ss\n"
        "    role: prodd\n",
        encoding="utf-8",
    )

    проверки = run(home, online=False, which=_which_нет, run=_run_нет, connect=_демон_молчит)
    текст = render(проверки)

    assert _статус(проверки, "настройки") == "FAIL"
    assert "p@ss" not in текст
    assert exit_code(проверки) == 2


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
