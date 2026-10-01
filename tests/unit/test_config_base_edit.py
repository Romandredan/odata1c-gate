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
from odata1c.config.writer import append_base

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
    assert строка.endswith("# подпись для модели") or "#" not in строка.split("'УТ #2'")[-1][:1]


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


def test_сбой_проверки_оставляет_файл_нетронутым(tmp_path, monkeypatch):
    путь = _файл(tmp_path, ("ut", {"role": "prod"}))
    байты = путь.read_bytes()

    def отказ(*args, **kwargs):
        raise ConfigError("bases.yaml: база «ut» описана неверно: write: тест")

    monkeypatch.setattr(base_edit, "parse_bases", отказ)

    with pytest.raises(ConfigError, match="описана неверно"):
        set_base_fields(путь, "ut", {"write": True})

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
