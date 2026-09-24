"""Шаблон `bases.yaml`, который создаёт `odata1c init`: пример записи базы — один блок, и каждое
его поле, включая уровень гейта, принадлежит записи базы (замечание владельца 2026-09-24:
закомментированный `gate` над базами читался как общая настройка всех баз)."""

import importlib.resources
import re

from odata1c.cli import main
from odata1c.config.loader import load_config


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
