"""Шаблон `bases.yaml`, который создаёт `odata1c init`: пример записи базы — один блок, и каждое
его поле, включая уровень гейта, принадлежит записи базы (замечание владельца 2026-09-24:
закомментированный `gate` над базами читался как общая настройка всех баз)."""

import importlib.resources
import re

import pytest

from odata1c.cli import main
from odata1c.config.loader import load_config
from odata1c.config.writer import (
    КОЛОНКА_КОММЕНТАРИЯ,
    КОММЕНТАРИИ_ПОЛЕЙ,
    append_base,
    render_base,
    строка_записи,
)

# Комментарий справа от поля (в том числе закомментированного) или строка-продолжение такого
# комментария: слева от `# ` — поле со значением либо одни решётки и пробелы.
_КОММЕНТАРИЙ_ПОЛЯ = re.compile(
    r"^(?P<code>#?\s*(?:#\s+)?[\w\-]+:(?:.*?\S)?)\s{2,}# |^(?P<cont>\s*#?(?:\s+#)?)\s{12,}# "
)


def _колонки(текст: str) -> set[int]:
    колонки = set()
    for строка in текст.splitlines():
        м = _КОММЕНТАРИЙ_ПОЛЯ.match(строка)
        if м:
            колонки.add(м.end() - 2)
    return колонки


def _шаблон() -> str:
    return (
        importlib.resources.files("odata1c.templates")
        .joinpath("bases.example.yaml")
        .read_text(encoding="utf-8")
    )


def _снять_внешний_комментарий(текст: str) -> str:
    """То, что делает Ctrl+/ редактора на выделенном блоке примеров: `# ` в первой колонке
    снимается у каждой строки раздела `bases`, внутренние комментарии остаются."""
    голова, _, примеры = текст.partition("bases:\n")
    строки = [с[2:] if с.startswith("# ") else "" if с == "#" else с for с in примеры.splitlines()]
    return голова + "bases:\n" + "\n".join(строки) + "\n"


def _дом_с(tmp_path, текст: str):
    home = tmp_path / "home"
    main(["init", "--home", str(home)])
    (home / "bases.yaml").write_text(текст, encoding="utf-8")
    return home


def test_шаблон_как_есть_баз_не_описывает(tmp_path):
    assert load_config(_дом_с(tmp_path, _шаблон())).bases == {}


def test_раскомментированный_пример_даёт_рабочие_записи_баз(tmp_path):
    config = load_config(_дом_с(tmp_path, _снять_внешний_комментарий(_шаблон())))
    assert set(config.bases) == {"ut", "buh", "ut_test"}
    assert config.bases["ut"].gate.mode == "identifiers+names"  # по роли prod
    assert config.bases["ut_test"].gate.mode == "off"  # по роли dev


def test_поля_внутри_примера_раскомментируются_в_запись_своей_базы(tmp_path):
    """Второй Ctrl+/ по строкам `gate` и `write` примера — и они становятся полями записи `ut`,
    а не отдельной «базой» `gate` рядом с ней."""
    текст = _снять_внешний_комментарий(_шаблон())
    текст = re.sub(r"(?m)^(    )# (gate:|  mode:|write:)", r"\1\2", текст)
    config = load_config(_дом_с(tmp_path, текст))
    assert set(config.bases) == {"ut", "buh", "ut_test"}
    assert "gate" not in config.bases
    # примеры показывают умолчания роли prod: раскомментированная строка поведения не меняет
    assert config.bases["ut"].role == "prod"
    assert config.bases["ut"].gate.mode == "identifiers+names"
    assert config.bases["ut"].write is False


def test_шапка_шаблона_говорит_что_гейт_задаётся_для_каждой_базы():
    шапка = _шаблон().partition("bases:\n")[0]
    assert "для каждой базы" in шапка
    assert "--gate" in шапка


def test_комментарии_шаблона_в_одной_колонке():
    """Замечание владельца: длинные строки (`label` примера, `independent_register_delete`) не
    должны сдвигать колонку комментариев. В шаблоне она на две позиции правее, чем в записи
    `base add`: после снятия `# ` с примера колонки совпадают."""
    assert _колонки(_шаблон()) == {КОЛОНКА_КОММЕНТАРИЯ + 2}


@pytest.mark.parametrize("роль", ["prod", "test", "dev"])
@pytest.mark.parametrize("гейт", [None, "identifiers+names"])
def test_комментарии_записи_base_add_в_одной_колонке(роль, гейт):
    значения = {"label": "УТ", "url": "https://server/ut", "user": "u", "password": "p"}
    значения |= {"role": роль, "config": "ut"} | ({"gate": {"mode": гейт}} if гейт else {})
    assert _колонки(render_base("trade_dev", значения)) == {КОЛОНКА_КОММЕНТАРИЯ}


def test_строка_записи_активная_и_образец_в_колонке_комментария():
    активная = строка_записи(4, "write", "false", активна=True)
    образец = строка_записи(4, "write", "false", активна=False)
    вложенная = строка_записи(4, "gate.mode", "off", активна=True)
    вложенный_образец = строка_записи(4, "gate.mode", "off", активна=False)

    assert активная.startswith("    write: false")
    assert образец.startswith("    # write: false")
    assert вложенная.startswith("      mode: off")
    assert вложенный_образец.startswith("    #   mode: off")
    for строка in (активная, образец, вложенная, вложенный_образец):
        assert (
            строка.index("# ", 6) == КОЛОНКА_КОММЕНТАРИЯ
            or строка.index("# ", 10) == КОЛОНКА_КОММЕНТАРИЯ
        )
    assert активная.endswith("# " + КОММЕНТАРИИ_ПОЛЕЙ["write"])


def test_строка_записи_свой_комментарий_и_без_комментария():
    assert строка_записи(4, "write", "true", активна=True, комментарий="мой").endswith("# мой")
    assert строка_записи(4, "role", "dev", активна=True) == "    role: dev"
    assert строка_записи(4, "label", "'УТ: тест'", активна=True) == "    label: 'УТ: тест'"


def test_render_base_после_выделения_помощников_не_изменился():
    """Снимок трёх строк, которые теперь рисует `строка_записи`: активная `write`, образец
    `independent_register_delete`, образец `mode` — те же пробелы и комментарии, что до правки."""
    текст = render_base("ut", {"url": "https://s/ut", "role": "prod", "write": True})
    assert "    write: true                              # по роли — false\n" in текст
    assert (
        "    #   independent_register_delete: false   "
        "# DELETE записей регистров сведений без регистратора\n" in текст
    )
    assert (
        "    #   mode: identifiers+names              # off | identifiers | identifiers+names\n"
        in текст
    )


def test_длинная_подпись_записывается_одной_строкой(tmp_path):
    """`yaml.safe_dump` по умолчанию переносит строку длиннее 80 знаков; построчная правка записи
    (`base set`) держится на правиле «один ключ — одна строка»."""
    подпись = " ".join(["слово"] * 24)
    assert len(подпись) > 120
    home = _дом_с(tmp_path, _шаблон())

    append_base(
        home / "bases.yaml",
        "ut",
        {"url": "https://s/ut", "user": "u", "password": "p", "label": подпись},
    )

    строки = (home / "bases.yaml").read_text(encoding="utf-8").splitlines()
    [номер] = [i for i, с in enumerate(строки) if с.startswith("    label:")]
    assert строки[номер + 1].startswith("    url:")
    assert load_config(home).bases["ut"].label == подпись
