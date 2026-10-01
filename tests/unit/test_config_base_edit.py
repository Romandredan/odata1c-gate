"""`odata1c base set`: правка записи базы текстом по разметке разбора (`config/base_edit.py`,
проект `docs/superpowers/specs/2026-10-01-base-set-design.md` §3.2).

Записи для тестов рисует настоящий `append_base` — тот же текст, что получает владелец после
`base add`; «рукописные» записи — буквальные строки в тестах."""

import pathlib

import pytest
import yaml

import odata1c.config.base_edit as base_edit
from odata1c.config.base_edit import ПО_РОЛИ, set_base_fields
from odata1c.config.loader import ConfigError
from odata1c.config.writer import _с_комментарием, append_base

URL = "https://server/ut"


def _файл(tmp_path: pathlib.Path, *записи: tuple[str, dict]) -> pathlib.Path:
    """Файл баз с записями `base add`: (имя, значения) по порядку."""
    путь = tmp_path / "bases.yaml"
    for имя, значения in записи:
        append_base(путь, имя, {"url": URL, "user": "u", "password": "p", **значения})
    return путь


def _строки(путь: pathlib.Path) -> list[str]:
    return путь.read_bytes().decode("utf-8").replace("\r\n", "\n").split("\n")


def _данные(путь: pathlib.Path, имя: str) -> dict:
    return yaml.safe_load(путь.read_text(encoding="utf-8"))["bases"][имя]


# --- Задача 3: поиск записи и отказы -------------------------------------------------------


def test_запись_находится_по_разметке_разбора(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod", "write": True}), ("bp", {"role": "dev"}))
    текст = путь.read_text(encoding="utf-8")

    з = base_edit._найти_запись(текст, "ut", текст.count("\n") + 1)

    строки = текст.split("\n")
    assert строки[з.начало] == "  ut:"
    assert строки[з.конец] == "  bp:"
    assert з.отступ == 4
    assert строки[з.активные["write"]].startswith("    write: true")
    assert "gate.mode" not in з.активные  # уровень по роли — только образец
    assert з.родители == {}


def test_последняя_запись_кончается_концом_файла(tmp_path):
    путь = _файл(
        tmp_path, ("ut", {"role": "prod"}), ("bp", {"role": "dev", "gate": {"mode": "off"}})
    )
    текст = путь.read_text(encoding="utf-8")

    з = base_edit._найти_запись(текст, "bp", текст.count("\n") + 1)

    assert з.конец == текст.count("\n") + 1
    assert "gate" in з.родители
    assert текст.split("\n")[з.активные["gate.mode"]].startswith("      mode: off")


def test_ключ_верхнего_уровня_после_bases_ограничивает_запись(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    путь.write_text(путь.read_text(encoding="utf-8") + "\ndefault: ut\n", encoding="utf-8")
    текст = путь.read_text(encoding="utf-8")

    з = base_edit._найти_запись(текст, "ut", текст.count("\n") + 1)

    assert текст.split("\n")[з.конец] == "default: ut"


@pytest.mark.parametrize(
    "текст, ожидание",
    [
        ("bases:\n  ut: {url: https://s/ut, user: u, password: p}\n", "нестандартной форме"),
        ("bases:\n  ut:\n    url: https://s/ut\n    gate: {mode: off}\n", "нестандартной форме"),
        ("bases:\n  ut:\n    url: https://s/ut\n    url: https://s/ut2\n", "нестандартной форме"),
        ("bases:\n  ut:\n    <<: *общее\n    url: https://s/ut\n", "повреждён"),
        ("bases:\n  ut:\n", "нестандартной форме"),
        ("bases: []\n", "раздел bases"),
        # ключ слияния внутри раздела и составной ключ
        (
            "bases:\n  ut:\n    url: https://s/ut\n    gate:\n      <<: {mode: off}\n",
            "нестандартной форме",
        ),
        (
            "bases:\n  ut:\n    url: https://s/ut\n    gate:\n      ? [a, b]\n      : off\n",
            "нестандартной форме",
        ),
        ("bases:\n  ut:\n    ? [a, b]\n    : https://s/ut\n", "нестандартной форме"),
        # ключ слияния с объявленным якорем: якорь в файле — отказ
        (
            "common: &common\n  role: dev\nbases:\n  ut:\n    <<: *common\n    url: https://s/ut\n",
            "нестандартной форме",
        ),
    ],
)
def test_нестандартные_записи_отклоняются(текст, ожидание):
    with pytest.raises(ConfigError) as ошибка:
        base_edit._найти_запись(текст, "ut", текст.count("\n") + 1)

    assert ожидание in str(ошибка.value)
    assert "https://s/ut" not in str(ошибка.value)


def test_неизвестная_база_называет_известные(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}), ("bp", {"role": "dev"}))
    текст = путь.read_text(encoding="utf-8")

    with pytest.raises(ConfigError) as ошибка:
        base_edit._найти_запись(текст, "zup", текст.count("\n") + 1)

    assert ошибка.value.code == "base_unknown"
    assert ошибка.value.hint == "известные базы: bp, ut"


def test_база_описанная_дважды_отклоняется():
    текст = "bases:\n  ut:\n    url: https://s/ut\n  ut:\n    url: https://s/ut2\n"

    with pytest.raises(ConfigError, match="дважды"):
        base_edit._найти_запись(текст, "ut", текст.count("\n") + 1)


@pytest.mark.parametrize(
    "текст",
    [
        # запись-ссылка: разметка `bp` указала бы на строки записи `ut`
        "bases:\n  ut: &b\n    url: https://s/ut\n    write: false\n  bp: *b\n",
        # раздел-ссылка во второй записи
        "bases:\n  ut:\n    url: https://s/ut\n    gate: &g\n      mode: off\n"
        "  bp:\n    url: https://s/ut\n    gate: *g\n",
        # якорь вне записей: файл с якорями правке не подлежит целиком
        "x: &a 1\nbases:\n  bp:\n    url: https://s/ut\n",
    ],
)
def test_якоря_и_ссылки_отклоняются_для_любой_записи(текст):
    with pytest.raises(ConfigError) as ошибка:
        base_edit._найти_запись(текст, "bp", текст.count("\n") + 1)

    assert "нестандартной форме" in str(ошибка.value)
    assert "YAML" in ошибка.value.hint
    assert "https://s/ut" not in str(ошибка.value) + ошибка.value.hint


def test_сдвиг_строк_на_разделителе_unicode_отклоняется(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod", "write": True, "label": "a b"}))
    текст = путь.read_text(encoding="utf-8")

    with pytest.raises(ConfigError, match="не совпала"):
        base_edit._найти_запись(текст, "ut", текст.count("\n") + 1)


def test_обычная_подпись_разметку_не_ломает(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod", "write": True, "label": "Основная торговля"}))
    текст = путь.read_text(encoding="utf-8")

    з = base_edit._найти_запись(текст, "ut", текст.count("\n") + 1)

    assert текст.split("\n")[з.активные["write"]].startswith("    write: true")


@pytest.mark.parametrize("текст", ["bases:\n", "bases: ~\n", "default: ut\n", ""])
def test_пустой_или_отсутствующий_раздел_bases_значит_нет_баз(текст):
    with pytest.raises(ConfigError) as ошибка:
        base_edit._найти_запись(текст, "ut", текст.count("\n") + 1)

    assert ошибка.value.code == "base_unknown"
    assert ошибка.value.hint == "известные базы: ни одной"


def test_default_выше_bases_последнюю_запись_не_ограничивает(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    текст = "default: ut\n" + путь.read_text(encoding="utf-8")

    з = base_edit._найти_запись(текст, "ut", текст.count("\n") + 1)

    assert з.конец == текст.count("\n") + 1


def test_разделы_gate_и_permissions_первыми_ключами_записи():
    текст = (
        "bases:\n"
        "  ut:\n"
        "    gate:\n"
        "      mode: off\n"
        "    permissions:\n"
        "      post_documents: true\n"
        "      commit_limit: 5\n"
        "    url: https://s/ut\n"
        "  bp:\n"
        "    url: https://s/bp\n"
    )

    з = base_edit._найти_запись(текст, "ut", текст.count("\n") + 1)

    assert з.начало == 1
    assert з.конец == 8
    assert з.отступ == 4
    assert з.родители == {"gate": 2, "permissions": 4}
    assert з.активные == {
        "gate": 2,
        "gate.mode": 3,
        "permissions": 4,
        "permissions.post_documents": 5,
        "permissions.commit_limit": 6,
        "url": 7,
    }


# --- Задача 4: установка полей и запись файла -----------------------------------------------


def test_write_off_на_prod_пишет_явно_в_строку_образца(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    до = _строки(путь)

    результат = set_base_fields(путь, "ut", {"write": False})

    после = _строки(путь)
    assert результат.изменено
    [и] = результат.изменения
    assert (и.было, и.было_источник, и.стало, и.стало_источник) == (
        False,
        "по роли prod",
        False,
        "явно",
    )
    assert _данные(путь, "ut")["write"] is False
    assert len(до) == len(после)
    различия = [(a, b) for a, b in zip(до, после, strict=True) if a != b]
    assert len(различия) == 1
    assert различия[0][0].startswith("    # write: false")
    assert различия[0][1].startswith("    write: false")
    assert различия[0][1].endswith("# разрешить пишущие тулы")


def test_активная_строка_переписывается_с_комментарием_владельца(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod", "write": True}))
    текст = путь.read_text(encoding="utf-8").replace("# по роли — false", "# мой комментарий")
    путь.write_text(текст, encoding="utf-8")

    set_base_fields(путь, "ut", {"write": False})

    строка = next(с for с in _строки(путь) if с.startswith("    write:"))
    assert строка.startswith("    write: false")
    assert строка.endswith("# мой комментарий")
    assert _данные(путь, "ut")["write"] is False


def test_повтор_того_же_значения_не_меняет_файл(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    set_base_fields(путь, "ut", {"write": False})
    байты = путь.read_bytes()

    результат = set_base_fields(путь, "ut", {"write": False})

    assert not результат.изменено
    assert путь.read_bytes() == байты
    assert not путь.with_suffix(".yaml.new").exists()


def test_write_совпадающий_с_ролью_всё_равно_пишется_явно(tmp_path):
    """Как `base add --gate`: явное значение не зависит от последующей смены роли; «изменений
    нет» — только когда совпали и значение, и источник."""
    путь = _файл(tmp_path, ("ut", {"role": "dev"}))

    результат = set_base_fields(путь, "ut", {"write": True})

    assert результат.изменено
    assert _данные(путь, "ut")["write"] is True
    [и] = результат.изменения
    assert (и.было_источник, и.стало_источник) == ("по роли dev", "явно")


def test_label_с_решёткой_и_кавычкой(tmp_path):
    """Review Focus 2: подпись с символами разметки остаётся разбираемой и правится повторно."""
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))

    set_base_fields(путь, "ut", {"label": "УТ #1: 'боевая'"})
    assert _данные(путь, "ut")["label"] == "УТ #1: 'боевая'"

    результат = set_base_fields(путь, "ut", {"label": "УТ #2"})
    assert _данные(путь, "ut")["label"] == "УТ #2"
    assert результат.label == "УТ #2"
    строка = next(с for с in _строки(путь) if с.startswith("    label:"))
    assert строка == "    label: 'УТ #2'"


def test_gate_mode_активирует_родителя_и_оставляет_прочие_образцы(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))

    результат = set_base_fields(путь, "ut", {"gate.mode": "off"})

    строки = _строки(путь)
    assert "    gate:" in строки
    mode = next(с for с in строки if с.startswith("      mode:"))
    assert mode.startswith("      mode: off")
    assert "off | identifiers | identifiers+names" in mode
    assert any(с.startswith("    #") and "что именно скрывать" in с for с in строки)
    assert _данные(путь, "ut")["gate"] == {
        "mode": False
    }  # YAML 1.1: off → false; модель примет как off
    [и] = результат.изменения
    assert (и.было, и.стало, и.стало_источник) == ("identifiers+names", "off", "явно")
    assert результат.действует["gate"] == "off"
    assert результат.явные_поля == ["gate.mode"]


def test_permissions_флаг_активирует_раздел_и_оставляет_остальное_образцами(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))

    результат = set_base_fields(путь, "ut", {"permissions.commit_limit": 5})

    строки = _строки(путь)
    assert "    permissions:" in строки
    assert any(с.startswith("      commit_limit: 5") for с in строки)
    assert any(с.startswith("    #   post_documents: true") for с in строки)
    assert _данные(путь, "ut")["permissions"] == {"commit_limit": 5}
    assert результат.действует["permissions"]["commit_limit"] == 5
    assert результат.действует["permissions"]["post_documents"] is True


def test_второй_флаг_раздела_активируется_внутри_активного_раздела(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    set_base_fields(путь, "ut", {"permissions.commit_limit": 5})

    set_base_fields(путь, "ut", {"permissions.post_documents": False})

    assert _данные(путь, "ut")["permissions"] == {"post_documents": False, "commit_limit": 5}
    assert _строки(путь).count("    permissions:") == 1


def test_вложенное_поле_вставляется_под_активного_родителя(tmp_path):
    """Review Focus 3: владелец сам дописал `permissions:` с одним флагом, образцов нет."""
    текст = (
        "bases:\n  ut:\n    label: УТ\n    url: https://server/ut\n    user: u\n"
        "    password: p\n    role: prod\n    permissions:\n      mark_deletion: false\n"
    )
    путь = tmp_path / "bases.yaml"
    путь.write_text(текст, encoding="utf-8")

    set_base_fields(путь, "ut", {"permissions.commit_limit": 3})

    строки = _строки(путь)
    assert строки[7] == "    permissions:"
    assert строки[8].startswith("      commit_limit: 3")
    assert строки[9] == "      mark_deletion: false"
    assert _данные(путь, "ut")["permissions"] == {"mark_deletion": False, "commit_limit": 3}


def test_рукописная_запись_без_образцов_вставка_после_role(tmp_path):
    текст = (
        "bases:\n  ut:\n    label: УТ\n    url: https://server/ut\n    user: u\n"
        "    password: p\n    role: prod\n"
    )
    путь = tmp_path / "bases.yaml"
    путь.write_text(текст, encoding="utf-8")

    set_base_fields(путь, "ut", {"write": True, "gate.mode": "identifiers"})

    строки = _строки(путь)
    assert строки[6] == "    role: prod"
    assert строки[7] == "    gate:"
    assert строки[8].startswith("      mode: identifiers")
    assert строки[9].startswith("    write: true")
    assert _данные(путь, "ut")["gate"] == {"mode": "identifiers"}


def test_запись_из_шаблона_поставки_правится(tmp_path):
    """Шаблон `bases.example.yaml`, раскомментированный целиком (Ctrl+/ редактора): колонка
    комментариев на две позиции правее, но образцы и структура те же."""
    import importlib.resources

    шаблон = (
        importlib.resources.files("odata1c.templates")
        .joinpath("bases.example.yaml")
        .read_text(encoding="utf-8")
    )
    строки, в_разделе = [], False
    for с in шаблон.split("\n"):
        if с.startswith("bases:"):
            в_разделе = True
        elif в_разделе and с.startswith("# "):
            с = с[2:]
        строки.append(с)
    путь = tmp_path / "bases.yaml"
    путь.write_text("\n".join(строки), encoding="utf-8")

    set_base_fields(путь, "ut", {"write": True, "gate.mode": "identifiers"})

    данные = _данные(путь, "ut")
    assert данные["write"] is True
    assert данные["gate"] == {"mode": "identifiers"}
    assert _данные(путь, "buh")["role"] == "prod"


def test_другие_записи_и_строки_вне_правки_не_меняются(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}), ("bp", {"role": "dev"}))
    до = _строки(путь)
    ut_до = _данные(путь, "ut")

    set_base_fields(путь, "bp", {"write": False, "gate.mode": "identifiers+names"})

    после = _строки(путь)
    assert len(после) == len(до)
    различия = [i for i, (a, b) in enumerate(zip(до, после, strict=True)) if a != b]
    assert len(различия) == 3  # write, `gate:`, `mode`
    assert all(до[i].startswith("    #") for i in различия)
    assert _данные(путь, "ut") == ut_до


def _считающая_проверка(monkeypatch, *, отказ_на_вызове: int) -> list[int]:
    """Подменить `parse_bases` счётчиком: настоящая проверка работает, а вызов с номером
    `отказ_на_вызове` отказывает. Возвращает список вызовов (по одному элементу на вызов)."""
    вызовы: list[int] = []
    настоящая = base_edit.parse_bases

    def проверка(*args, **kwargs):
        вызовы.append(1)
        if len(вызовы) == отказ_на_вызове:
            raise ConfigError("bases.yaml: база «ut» описана неверно: write: тест")
        return настоящая(*args, **kwargs)

    monkeypatch.setattr(base_edit, "parse_bases", проверка)
    return вызовы


def test_сбой_проверки_нового_текста_оставляет_файл_нетронутым(tmp_path, monkeypatch):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    байты = путь.read_bytes()
    вызовы = _считающая_проверка(monkeypatch, отказ_на_вызове=2)

    with pytest.raises(ConfigError, match="описана неверно"):
        set_base_fields(путь, "ut", {"write": True})

    assert len(вызовы) == 2  # исходный текст прошёл, новый отклонён
    assert путь.read_bytes() == байты
    assert not путь.with_suffix(".yaml.new").exists()


def test_сбой_проверки_исходного_текста_до_любой_правки(tmp_path, monkeypatch):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    байты = путь.read_bytes()
    вызовы = _считающая_проверка(monkeypatch, отказ_на_вызове=1)

    with pytest.raises(ConfigError, match="описана неверно"):
        set_base_fields(путь, "ut", {"write": True})

    assert len(вызовы) == 1
    assert путь.read_bytes() == байты


def test_проверка_нового_текста_идёт_и_когда_изменений_нет(tmp_path, monkeypatch):
    путь = _файл(tmp_path, ("ut", {"role": "prod", "write": True}))
    байты = путь.read_bytes()
    вызовы = _считающая_проверка(monkeypatch, отказ_на_вызове=0)

    результат = set_base_fields(путь, "ut", {"write": True})

    assert not результат.изменено
    assert len(вызовы) == 2
    assert путь.read_bytes() == байты


def test_роль_с_опечаткой_в_исходном_файле_даёт_конфигурационную_ошибку(tmp_path):
    """Без `monkeypatch`: настоящий отказ `parse_bases`, а не `KeyError` из таблицы ролей."""
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    путь.write_text(
        путь.read_text(encoding="utf-8").replace("    role: prod", "    role: staging"),
        encoding="utf-8",
    )
    байты = путь.read_bytes()

    with pytest.raises(ConfigError, match="роль"):
        set_base_fields(путь, "ut", {"write": False})

    assert путь.read_bytes() == байты


# Латинский тег проходит `compose` и падает на `safe_load` (ConstructorError), кириллический
# падает уже в `compose` (ScannerError, «expected URI»): секрет не должен попасть в ошибку ни там,
# ни там — ни текстом, ни цепочкой исключений, которую печатает трассировка.
@pytest.mark.parametrize("тег", ["!secret_value", "!секретное_значение"])
def test_тег_yaml_в_пароле_не_попадает_ни_в_ошибку_ни_в_цепочку(tmp_path, тег):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    путь.write_text(
        путь.read_text(encoding="utf-8").replace("    password: p", f"    password: {тег}"),
        encoding="utf-8",
    )
    байты = путь.read_bytes()

    with pytest.raises(ConfigError) as ошибка:
        set_base_fields(путь, "ut", {"write": False})

    assert тег[1:] not in str(ошибка.value)
    assert тег[1:] not in (ошибка.value.hint or "")
    assert ошибка.value.__cause__ is None
    assert ошибка.value.__suppress_context__
    assert путь.read_bytes() == байты


def test_правка_дала_неразбираемый_файл_без_значений_в_ошибке(tmp_path, monkeypatch):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    байты = путь.read_bytes()

    def портит(строки, з, *, поле, текст):
        строки.insert(з.начало + 1, "    заметка: !секретное_значение")

    monkeypatch.setattr(base_edit, "_установить", портит)

    with pytest.raises(ConfigError, match="неразбираемый") as ошибка:
        set_base_fields(путь, "ut", {"write": True})

    assert "секретное_значение" not in str(ошибка.value)
    assert ошибка.value.__cause__ is None
    assert путь.read_bytes() == байты
    assert not путь.with_suffix(".yaml.new").exists()


def test_crlf_сохраняется(tmp_path):
    """Review Focus 1: на Windows `pathlib.write_text` пишет `\\r\\n`, и файл владельца такой."""
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    путь.write_bytes(путь.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))

    set_base_fields(путь, "ut", {"write": False})

    байты = путь.read_bytes()
    assert b"\r\n" in байты
    assert b"\n" not in байты.replace(b"\r\n", b"")
    assert _данные(путь, "ut")["write"] is False


def test_неизвестное_поле_и_default_у_label_отклоняются_до_чтения_файла(tmp_path):
    путь = tmp_path / "нет_такого.yaml"
    with pytest.raises(ValueError):
        set_base_fields(путь, "ut", {"url": "x"})
    with pytest.raises(ValueError):
        set_base_fields(путь, "ut", {"label": ПО_РОЛИ})
    with pytest.raises(ConfigError, match="не найден"):
        set_base_fields(путь, "ut", {"write": True})


# --- Ревью задачи 4: подпись в одну строку, раскладка образцов, запись файла ----------------

_ГОЛОВА = "bases:\n  ut:\n    label: УТ\n    url: https://server/ut\n    user: u\n    password: p\n"


def _рукописный(tmp_path: pathlib.Path, текст: str) -> pathlib.Path:
    путь = tmp_path / "bases.yaml"
    путь.write_bytes(текст.encode("utf-8"))
    return путь


def test_двухстрочная_подпись_отклоняется_без_значения(tmp_path):
    """Файл, записанный прежней версией `base add`: длинная подпись перенесена на вторую строку."""
    путь = _рукописный(
        tmp_path,
        "bases:\n  ut:\n    label: 'очень секретная длинная\n      подпись'\n"
        "    url: https://server/ut\n    user: u\n    password: p\n    role: prod\n",
    )
    байты = путь.read_bytes()

    with pytest.raises(ConfigError, match="несколько строк") as ошибка:
        set_base_fields(путь, "ut", {"label": "Короткая"})

    assert "секретная" not in str(ошибка.value)
    assert путь.read_bytes() == байты


def test_длинная_подпись_после_правки_остаётся_в_одной_строке(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    подпись = " ".join(["слово"] * 30)

    set_base_fields(путь, "ut", {"label": подпись})
    set_base_fields(путь, "ut", {"label": "Короткая"})

    assert _данные(путь, "ut")["label"] == "Короткая"
    assert sum(1 for с in _строки(путь) if с.startswith("    label:")) == 1
    assert "слово" not in путь.read_text(encoding="utf-8")


def test_образец_с_тремя_пробелами_после_решётки_встаёт_на_колонку_записи(tmp_path):
    путь = _рукописный(tmp_path, _ГОЛОВА + "    role: prod\n    #   write: false\n")

    set_base_fields(путь, "ut", {"write": True})

    assert _строки(путь)[7].startswith("    write: true")
    assert _данные(путь, "ut")["write"] is True


def test_флаг_встаёт_под_активный_раздел_а_не_на_образец_выше_него(tmp_path):
    """Раздел `permissions:` дописан в конец записи `base add`; образцы флагов остались выше."""
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    путь.write_bytes(
        путь.read_bytes().replace(b"\r\n", b"\n").rstrip(b"\n")
        + b"\n    permissions:\n      mark_deletion: false\n"
    )

    set_base_fields(путь, "ut", {"permissions.post_documents": False})

    строки = _строки(путь)
    i = строки.index("    permissions:")
    assert строки[i + 1].startswith("      post_documents: false")
    assert any(с.startswith("    #   post_documents: true") for с in строки)
    assert _данные(путь, "ut")["permissions"] == {"post_documents": False, "mark_deletion": False}


def test_образец_внутри_блока_активного_раздела_активируется_на_колонку_детей(tmp_path):
    путь = _рукописный(
        tmp_path,
        _ГОЛОВА
        + "    role: prod\n    permissions:\n      mark_deletion: false\n"
        + "    #   commit_limit: 20\n",
    )

    set_base_fields(путь, "ut", {"permissions.commit_limit": 3})

    строки = _строки(путь)
    assert строки[8] == "      mark_deletion: false"
    assert строки[9].startswith("      commit_limit: 3")
    assert _данные(путь, "ut")["permissions"] == {"mark_deletion": False, "commit_limit": 3}


def test_ребёнок_активного_раздела_встаёт_вровень_с_прежними_детьми(tmp_path):
    путь = _рукописный(
        tmp_path,
        _ГОЛОВА + "    role: prod\n    permissions:\n        mark_deletion: false\n",
    )

    set_base_fields(путь, "ut", {"permissions.commit_limit": 3})

    assert _строки(путь)[8].startswith("        commit_limit: 3")
    assert _данные(путь, "ut")["permissions"] == {"mark_deletion": False, "commit_limit": 3}


def test_образец_ребёнка_есть_образца_родителя_нет(tmp_path):
    путь = _рукописный(tmp_path, _ГОЛОВА + "    role: prod\n    #   mode: off\n")

    set_base_fields(путь, "ut", {"gate.mode": "identifiers"})

    строки = _строки(путь)
    assert строки[6] == "    role: prod"
    assert строки[7] == "    gate:"
    assert строки[8].startswith("      mode: identifiers")
    assert _данные(путь, "ut")["gate"] == {"mode": "identifiers"}


def test_разделить_двойные_кавычки_с_экранированной_косой_чертой():
    assert base_edit._разделить(' "a\\\\"   # к') == ('"a\\\\"', "к")
    assert base_edit._разделить(' "a\\"b"   # к') == ('"a\\"b"', "к")


def test_подпись_с_косой_чертой_в_конце_правится_с_комментарием(tmp_path):
    путь = _рукописный(
        tmp_path,
        """bases:\n  ut:\n    label: "a\\\\"   # к\n    url: https://server/ut\n"""
        "    user: u\n    password: p\n    role: prod\n",
    )
    assert _данные(путь, "ut")["label"] == "a\\"

    set_base_fields(путь, "ut", {"label": "Новая"})

    строка = next(с for с in _строки(путь) if с.startswith("    label:"))
    assert строка.startswith("    label: Новая")
    assert строка.endswith("# к")


def test_окончания_строк_берутся_по_первой_строке(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    lf = путь.read_bytes().replace(b"\r\n", b"\n")
    первая, _, остальное = lf.partition(b"\n")

    путь.write_bytes(первая + b"\n" + остальное.replace(b"\n", b"\r\n"))
    set_base_fields(путь, "ut", {"write": False})
    assert b"\r\n" not in путь.read_bytes()

    путь.write_bytes(первая + b"\r\n" + остальное)
    set_base_fields(путь, "ut", {"write": False})
    байты = путь.read_bytes()
    assert b"\r\n" in байты
    assert b"\n" not in байты.replace(b"\r\n", b"")


def test_поле_без_изменения_не_переписывается_в_вызове_с_несколькими_полями(tmp_path):
    путь = _рукописный(tmp_path, _ГОЛОВА + "    role: prod\n    write: true   # мой\n")

    результат = set_base_fields(путь, "ut", {"write": True, "label": "Новая"})

    assert "    write: true   # мой" in _строки(путь)
    assert _данные(путь, "ut")["label"] == "Новая"
    assert результат.изменено


def test_снять_поле_которого_нет_явно_ничего_не_делает(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    байты = путь.read_bytes()

    результат = set_base_fields(путь, "ut", {"write": ПО_РОЛИ})

    assert not результат.изменено
    assert путь.read_bytes() == байты


def test_вставка_без_role_идёт_после_последней_из_label_url_user_password(tmp_path):
    путь = _рукописный(
        tmp_path,
        "bases:\n  ut:\n    password: p\n    user: u\n    url: https://server/ut\n    label: УТ\n",
    )

    set_base_fields(путь, "ut", {"write": True})

    строки = _строки(путь)
    assert строки[5] == "    label: УТ"
    assert строки[6].startswith("    write: true")


def test_замена_файла_занята_другим_процессом(tmp_path, monkeypatch):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    байты = путь.read_bytes()
    вызовы: list[int] = []

    def занято(*args, **kwargs):
        вызовы.append(1)
        raise PermissionError(13, "занят")

    monkeypatch.setattr(base_edit.os, "replace", занято)
    monkeypatch.setattr(base_edit.time, "sleep", lambda секунды: None)

    with pytest.raises(ConfigError, match="занят другим процессом") as ошибка:
        set_base_fields(путь, "ut", {"write": False})

    assert len(вызовы) > 1  # были повторы
    assert ошибка.value.hint
    assert путь.read_bytes() == байты
    assert not путь.with_suffix(".yaml.new").exists()


def test_замена_файла_удаётся_со_второй_попытки(tmp_path, monkeypatch):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    настоящая = base_edit.os.replace
    вызовы: list[int] = []

    def занято_один_раз(*args, **kwargs):
        вызовы.append(1)
        if len(вызовы) == 1:
            raise PermissionError(13, "занят")
        return настоящая(*args, **kwargs)

    monkeypatch.setattr(base_edit.os, "replace", занято_один_раз)
    monkeypatch.setattr(base_edit.time, "sleep", lambda секунды: None)

    set_base_fields(путь, "ut", {"write": False})

    assert len(вызовы) == 2
    assert _данные(путь, "ut")["write"] is False
    assert not путь.with_suffix(".yaml.new").exists()


def test_многострочная_подпись_без_кавычек_отклоняется_контрольным_чтением(tmp_path):
    """Обычный скаляр с продолжением на следующей строке: правка первой строки оставила бы хвост,
    YAML склеил бы строки в «Короткая подпись», а загрузчик такую запись принимает."""
    путь = _рукописный(
        tmp_path,
        "bases:\n  ut:\n    label: очень длинная\n      подпись\n    url: https://server/ut\n"
        "    user: u\n    password: p\n    role: prod\n",
    )
    assert _данные(путь, "ut")["label"] == "очень длинная подпись"
    байты = путь.read_bytes()

    with pytest.raises(ConfigError, match="читается не так") as ошибка:
        set_base_fields(путь, "ut", {"label": "Короткая"})

    assert "подпись" not in str(ошибка.value)
    assert "label" in str(ошибка.value)
    assert путь.read_bytes() == байты
    assert not путь.with_suffix(".yaml.new").exists()


def test_пустой_раздел_gate_отклоняется_как_нестандартная_форма(tmp_path):
    путь = _рукописный(tmp_path, _ГОЛОВА + "    role: prod\n    gate:\n")
    байты = путь.read_bytes()

    with pytest.raises(ConfigError, match="нестандартной форме"):
        set_base_fields(путь, "ut", {"write": True})

    assert путь.read_bytes() == байты


def test_контрольное_чтение_пропускает_off_и_числа(tmp_path):
    """YAML 1.1 читает `off` как `false`; `_значение` возвращает его как `off`."""
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))

    set_base_fields(путь, "ut", {"gate.mode": "off", "permissions.commit_limit": 0, "write": False})

    данные = _данные(путь, "ut")
    assert данные["gate"] == {"mode": False}
    assert данные["permissions"] == {"commit_limit": 0}


# --- Задача 5: default ----------------------------------------------------------------------


def test_default_возвращает_образец_и_комментирует_пустого_родителя(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    исходные = _строки(путь)
    set_base_fields(путь, "ut", {"gate.mode": "off"})

    результат = set_base_fields(путь, "ut", {"gate.mode": ПО_РОЛИ})

    assert _строки(путь) == исходные  # образец с умолчанием prod и стандартным комментарием
    assert "gate" not in _данные(путь, "ut")
    [и] = результат.изменения
    assert (и.было, и.было_источник, и.стало, и.стало_источник) == (
        "off",
        "явно",
        "identifiers+names",
        "по роли prod",
    )


def test_default_у_поля_по_роли_ничего_не_меняет(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    байты = путь.read_bytes()

    результат = set_base_fields(путь, "ut", {"write": ПО_РОЛИ})

    assert not результат.изменено
    assert путь.read_bytes() == байты


def test_default_одного_из_двух_флагов_оставляет_раздел_активным(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "test"}))
    set_base_fields(
        путь, "ut", {"permissions.commit_limit": 5, "permissions.post_documents": False}
    )

    set_base_fields(путь, "ut", {"permissions.commit_limit": ПО_РОЛИ})

    строки = _строки(путь)
    assert "    permissions:" in строки
    assert any(с.startswith("    #   commit_limit: 50") for с in строки)  # умолчание роли test
    assert _данные(путь, "ut")["permissions"] == {"post_documents": False}


def test_default_последнего_флага_комментирует_раздел(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "test"}))
    set_base_fields(путь, "ut", {"permissions.post_documents": False})

    set_base_fields(путь, "ut", {"permissions.post_documents": ПО_РОЛИ})

    строки = _строки(путь)
    assert "    permissions:" not in строки
    assert "    # permissions:" in строки
    assert "permissions" not in _данные(путь, "ut")


def test_default_у_write_с_комментарием_владельца_ставит_стандартный(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod", "write": True}))

    set_base_fields(путь, "ut", {"write": ПО_РОЛИ})

    строка = next(с for с in _строки(путь) if с.startswith("    # write:"))
    assert строка.startswith("    # write: false")
    assert строка.endswith("# разрешить пишущие тулы")
    assert "write" not in _данные(путь, "ut")


def test_контрольное_чтение_проверяет_и_default(tmp_path, monkeypatch):
    """Снятие ничего не изменило, поле осталось явным: команда не должна отчитаться «по роли»."""
    путь = _файл(tmp_path, ("ut", {"role": "prod", "write": True}))
    байты = путь.read_bytes()
    monkeypatch.setattr(base_edit, "_снять_поле", lambda *args, **kwargs: None)

    with pytest.raises(ConfigError, match="читается не так") as ошибка:
        set_base_fields(путь, "ut", {"write": ПО_РОЛИ})

    assert "write" in str(ошибка.value)
    assert путь.read_bytes() == байты
    assert not путь.with_suffix(".yaml.new").exists()


# --- Задача 6: смена роли -------------------------------------------------------------------


def test_смена_роли_перерисовывает_образцы_и_не_трогает_явные(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod", "gate": {"mode": "identifiers+names"}}))

    результат = set_base_fields(путь, "ut", {"role": "dev"})

    строки = _строки(путь)
    assert "    role: dev" in строки
    assert any(с.startswith("    # write: true") for с in строки)
    assert any(с.startswith("    #   independent_register_delete: true") for с in строки)
    assert any(с.startswith("    #   commit_limit: 0") for с in строки)
    assert any(с.startswith("      mode: identifiers+names") for с in строки)  # явный, остался
    assert any("умолчание роли dev)" in с for с in строки)
    assert any("умолчание роли dev: off)" in с for с in строки)
    assert not any("умолчание роли prod" in с for с in строки)
    [и] = результат.изменения
    assert (и.было, и.стало, и.стало_источник) == ("prod", "dev", "явно")
    assert результат.явные_поля == ["gate.mode"]
    assert результат.действует == {
        "role": "dev",
        "gate": "identifiers+names",
        "write": True,
        "permissions": {
            "post_documents": True,
            "mark_deletion": True,
            "independent_register_delete": True,
            "commit_limit": 0,
        },
    }


def test_смена_роли_обратно_возвращает_образцы_prod(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    исходные = путь.read_bytes()
    set_base_fields(путь, "ut", {"role": "dev"})

    set_base_fields(путь, "ut", {"role": "prod"})

    assert путь.read_bytes() == исходные


def test_смена_роли_без_ключа_role_вставляет_его_после_пароля(tmp_path):
    текст = (
        "bases:\n  ut:\n    label: УТ\n    url: https://server/ut\n    user: u\n    password: p\n"
    )
    путь = tmp_path / "bases.yaml"
    путь.write_text(текст, encoding="utf-8")

    результат = set_base_fields(путь, "ut", {"role": "test"})

    assert _строки(путь)[6] == "    role: test"
    [и] = результат.изменения
    assert (и.было, и.было_источник, и.стало, и.стало_источник) == (
        "prod",
        "по умолчанию",
        "test",
        "явно",
    )


def test_смена_роли_и_поле_в_одном_вызове(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))

    результат = set_base_fields(путь, "ut", {"role": "dev", "write": False})

    данные = _данные(путь, "ut")
    assert данные["role"] == "dev" and данные["write"] is False
    assert результат.действует["write"] is False
    assert результат.явные_поля == ["write"]
    assert [и.поле for и in результат.изменения] == ["role", "write"]


def test_неизвестная_роль_отклоняется_до_правки(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    байты = путь.read_bytes()

    with pytest.raises(ConfigError, match="неизвестная роль"):
        set_base_fields(путь, "ut", {"role": "admin"})

    assert путь.read_bytes() == байты


def test_неизвестная_роль_не_первой_в_вызове_отклоняется_до_любой_правки(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    байты = путь.read_bytes()

    with pytest.raises(ConfigError, match="неизвестная роль"):
        set_base_fields(путь, "ut", {"write": True, "role": "admin"})
    with pytest.raises(ConfigError, match="неизвестная роль"):
        set_base_fields(путь, "ut", {"role": ["dev"]})

    assert путь.read_bytes() == байты


def test_та_же_явная_роль_файл_не_меняет(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "test"}))
    байты = путь.read_bytes()

    результат = set_base_fields(путь, "ut", {"role": "test"})

    assert not результат.изменено
    assert путь.read_bytes() == байты


def test_роль_prod_без_ключа_role_пишется_явно(tmp_path):
    путь = _рукописный(tmp_path, _ГОЛОВА)

    результат = set_base_fields(путь, "ut", {"role": "prod"})

    assert _данные(путь, "ut")["role"] == "prod"
    assert результат.изменено
    [и] = результат.изменения
    assert (и.было_источник, и.стало_источник) == ("по умолчанию", "явно")


def test_контрольное_чтение_проверяет_и_роль(tmp_path, monkeypatch):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    байты = путь.read_bytes()
    monkeypatch.setattr(base_edit, "_применить_роль", lambda текст, name, новая: текст)

    with pytest.raises(ConfigError, match="читается не так") as ошибка:
        set_base_fields(путь, "ut", {"role": "dev"})

    assert "role" in str(ошибка.value)
    assert путь.read_bytes() == байты


def test_заголовок_роли_правится_только_в_строках_комментариев(tmp_path):
    """Подпись, похожая на заголовок, остаётся как есть: правятся только строки-комментарии."""
    путь = _файл(tmp_path, ("ut", {"role": "prod", "label": "умолчание роли prod #1"}))

    set_base_fields(путь, "ut", {"role": "dev"})

    assert _данные(путь, "ut")["label"] == "умолчание роли prod #1"


@pytest.mark.parametrize(
    ("прежняя", "новая"),
    [("prod", "test"), ("test", "dev"), ("dev", "prod"), ("test", "prod"), ("dev", "test")],
)
def test_смена_роли_туда_и_обратно_байт_в_байт(tmp_path, прежняя, новая):
    путь = _файл(tmp_path, ("ut", {"role": прежняя}))
    исходные = путь.read_bytes()

    set_base_fields(путь, "ut", {"role": новая})
    assert путь.read_bytes() != исходные
    set_base_fields(путь, "ut", {"role": прежняя})

    assert путь.read_bytes() == исходные


def test_смена_роли_перерисовывает_все_образцы_ключа_в_записи(tmp_path):
    """Рукописная раскладка: образец `write` есть и в шаблонной секции, и вписан ниже вручную."""
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    текст = путь.read_text(encoding="utf-8").rstrip("\n")
    путь.write_text(текст + "\n    # write: false\n", encoding="utf-8")

    set_base_fields(путь, "ut", {"role": "dev"})

    образцы = [с for с in _строки(путь) if с.lstrip().startswith("# write:")]
    assert len(образцы) == 2
    assert all(с.lstrip().startswith("# write: true") for с in образцы)


# --- Ревью задачи 5 ---------------------------------------------------------------------------


def test_установка_флага_раздела_выбирает_ближайший_образец_родителя(tmp_path):
    """Владелец вписал `permissions:` сразу после `role`; после `default` у его ребёнка в записи
    два образца `# permissions:`. Активировать первый нельзя: между ним и образцом флага стоит
    активный ключ записи."""
    путь = _файл(tmp_path, ("ut", {"role": "prod", "write": True}))
    исходные = _строки(путь)
    n = исходные.index("    role: prod") + 1
    вписано = исходные[:n] + ["    permissions:", "      commit_limit: 5"] + исходные[n:]
    путь.write_text("\n".join(вписано), encoding="utf-8")
    set_base_fields(путь, "ut", {"permissions.commit_limit": ПО_РОЛИ})

    set_base_fields(путь, "ut", {"permissions.post_documents": False})

    assert _данные(путь, "ut")["permissions"] == {"post_documents": False}
    assert _данные(путь, "ut")["write"] is True


def test_разделить_пустое_значение_с_комментарием():
    assert base_edit._разделить("   # решено") == ("", "решено")
    assert base_edit._разделить("") == ("", None)
    assert base_edit._разделить("  off  # так") == ("off", "так")


def test_родитель_с_комментарием_сохраняет_его_после_default_и_установки(tmp_path):
    путь = _рукописный(
        tmp_path,
        _ГОЛОВА + "    role: prod\n    gate:   # решено 01.10\n      mode: off\n",
    )
    set_base_fields(путь, "ut", {"gate.mode": ПО_РОЛИ})
    assert "    # gate:   # решено 01.10" in _строки(путь)

    set_base_fields(путь, "ut", {"gate.mode": "identifiers"})

    assert _с_комментарием("    gate:", "решено 01.10") in _строки(путь)
    assert _данные(путь, "ut")["gate"] == {"mode": "identifiers"}


# --- Круговой путь установки и default по байтам -------------------------------------------

_ПОЛЯ_КРУГА = (
    "write",
    "gate.mode",
    "permissions.post_documents",
    "permissions.mark_deletion",
    "permissions.independent_register_delete",
    "permissions.commit_limit",
)


def _иное_значение(роль: str, поле: str) -> object:
    """Значение поля, отличное от умолчания роли."""
    умолчание = base_edit._умолчание_роли(роль, поле)
    if поле == "gate.mode":
        return next(у for у in ("off", "identifiers", "identifiers+names") if у != умолчание)
    if поле == "permissions.commit_limit":
        return умолчание + 7
    return not умолчание


@pytest.mark.parametrize("роль", ["prod", "test", "dev"])
@pytest.mark.parametrize("поле", _ПОЛЯ_КРУГА)
def test_установка_и_default_возвращают_файл_байт_в_байт(tmp_path, роль, поле):
    путь = _файл(tmp_path, ("ut", {"role": роль}))
    исходные = путь.read_bytes()

    set_base_fields(путь, "ut", {поле: _иное_значение(роль, поле)})
    assert путь.read_bytes() != исходные
    set_base_fields(путь, "ut", {поле: ПО_РОЛИ})

    assert путь.read_bytes() == исходные


def test_два_default_у_детей_одного_родителя_в_одном_вызове(tmp_path):
    путь = _файл(tmp_path, ("ut", {"role": "test"}))
    исходные = путь.read_bytes()
    set_base_fields(
        путь, "ut", {"permissions.commit_limit": 5, "permissions.post_documents": False}
    )

    set_base_fields(
        путь, "ut", {"permissions.post_documents": ПО_РОЛИ, "permissions.commit_limit": ПО_РОЛИ}
    )

    assert путь.read_bytes() == исходные


@pytest.mark.parametrize("порядок", ["default_первым", "установка_первой"])
def test_default_одного_ребёнка_и_установка_другого_в_одном_вызове(tmp_path, порядок):
    (tmp_path / "эталон").mkdir()
    эталон = _файл(tmp_path / "эталон", ("ut", {"role": "test"}))
    set_base_fields(эталон, "ut", {"permissions.post_documents": False})
    путь = _файл(tmp_path, ("ut", {"role": "test"}))
    set_base_fields(путь, "ut", {"permissions.commit_limit": 5})
    изменения = {"permissions.commit_limit": ПО_РОЛИ, "permissions.post_documents": False}
    if порядок == "установка_первой":
        изменения = dict(reversed(list(изменения.items())))

    set_base_fields(путь, "ut", изменения)

    assert _данные(путь, "ut")["permissions"] == {"post_documents": False}
    assert путь.read_bytes() == эталон.read_bytes()
