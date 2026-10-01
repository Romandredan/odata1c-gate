"""`odata1c base set`: правка записи базы текстом по разметке разбора (`config/base_edit.py`,
проект `docs/superpowers/specs/2026-10-01-base-set-design.md` §3.2).

Записи для тестов рисует настоящий `append_base` — тот же текст, что получает владелец после
`base add`; «рукописные» записи — буквальные строки в тестах."""

import pathlib

import pytest
import yaml

import odata1c.config.base_edit as base_edit
from odata1c.config.base_edit import (  # noqa: F401 — для задач 4–6
    ПО_РОЛИ,
    SetResult,
    set_base_fields,
)
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
    return путь.read_bytes().decode("utf-8").split("\n")


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
