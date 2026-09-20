"""Конструктор `policy hide | open | set` — точечная запись правил в файл владельца без потери
комментариев (SPEC §6.9, ADR-0015, задача 5 плана M2b).

`hide_entity`/`set_field_class` пишут через `ruamel.yaml` round-trip во временный файл и
`os.replace` — тот же приём, что и `strip_auto_section` (`gate/policy.py`), но здесь дописывается
одно правило в раздел `entities`/`fields`, а не убирается целый раздел `auto`. Проверяется на
копии настоящего шаблона (`ensure_policy_template`), а не на самодельном обрезке YAML: только
так тест видит те же заглушки `entities: {}`/`fields: {}`/`custom: {}` и ту же шапку с перечнем
классов, что получит владелец на живой базе."""

import pathlib

import pytest

from odata1c.config.writer import ensure_policy_template
from odata1c.gate.policy import load_policy, parse_owner_file
from odata1c.gate.policy_edit import hide_entity, set_field_class
from odata1c.gate.service import policy_path


def _шаблон(tmp_path) -> pathlib.Path:
    ensure_policy_template(tmp_path, "ut")
    return policy_path(tmp_path, "ut")


def test_hide_entity_дописывает_правило_и_бережёт_шапку_и_комментарий_scan_free_text(tmp_path):
    путь = _шаблон(tmp_path)
    исходный_текст = путь.read_text(encoding="utf-8")
    шапка = исходный_текст.splitlines()[0]  # "# odata1c: политика гейта базы ut. ..."
    assert "# Классы." in исходный_текст  # перечень классов — есть в шаблоне до правки

    изменено = hide_entity(путь, "Catalog_Контрагенты")

    assert изменено is True
    текст = путь.read_text(encoding="utf-8")
    assert текст.splitlines()[0] == шапка
    assert "# Классы." in текст  # перечень классов — дословно
    assert "scan_free_text: true" in текст
    assert "# искать реквизиты и известные названия в любом тексте ответа:" in текст

    политика = load_policy(путь)
    assert политика.is_hidden("Catalog_Контрагенты")


def test_hide_entity_второй_раз_возвращает_false_и_не_дублирует_правило(tmp_path):
    путь = _шаблон(tmp_path)
    assert hide_entity(путь, "Catalog_Контрагенты") is True

    изменено = hide_entity(путь, "Catalog_Контрагенты")

    assert изменено is False
    # Раздел entities — ровно одна запись: второй вызов не дописал дубликат и не переписал файл
    # заново (шаблон упоминает «Catalog_Контрагенты» ещё и в закомментированных примерах —
    # проверка по значению раздела, а не по числу подстрок во всём файле).
    assert parse_owner_file(путь)["entities"] == {"Catalog_Контрагенты": {"hide": True}}


def test_set_field_class_пишет_в_fields_и_второй_вызов_возвращает_прежний_класс(tmp_path):
    путь = _шаблон(tmp_path)

    прежнее_1 = set_field_class(путь, "Catalog_A.B", "keep")
    assert прежнее_1 is None
    политика = load_policy(путь)
    assert политика.sensitivity_of("Catalog_A", "B") == "keep"

    прежнее_2 = set_field_class(путь, "Catalog_A.B", "inn")
    assert прежнее_2 == "keep"
    политика = load_policy(путь)
    assert политика.sensitivity_of("Catalog_A", "B") == "inn"


def test_hide_entity_при_ошибке_записи_не_меняет_исходный_файл(tmp_path, monkeypatch):
    путь = _шаблон(tmp_path)
    исходные_байты = путь.read_bytes()

    def падающий_replace(*args, **kwargs):
        raise OSError("диск недоступен (имитация сбоя)")

    monkeypatch.setattr("odata1c.gate.policy_edit.os.replace", падающий_replace)

    with pytest.raises(OSError):
        hide_entity(путь, "Catalog_Контрагенты")

    assert путь.read_bytes() == исходные_байты


def test_set_field_class_при_ошибке_записи_не_меняет_исходный_файл(tmp_path, monkeypatch):
    путь = _шаблон(tmp_path)
    исходные_байты = путь.read_bytes()

    def падающий_replace(*args, **kwargs):
        raise OSError("диск недоступен (имитация сбоя)")

    monkeypatch.setattr("odata1c.gate.policy_edit.os.replace", падающий_replace)

    with pytest.raises(OSError):
        set_field_class(путь, "Catalog_A.B", "keep")

    assert путь.read_bytes() == исходные_байты
