"""Связка гейта с индексом и настройками: политика базы, словарь, классификатор.

SPEC §4.3, §6.9.
"""

from __future__ import annotations

import base64
import pathlib

import yaml

from odata1c.config.home import base_dir
from odata1c.config.models import BaseConfig
from odata1c.gate.dictionary import Dictionary
from odata1c.gate.field_rules import classify_field
from odata1c.gate.policy import generate_policy, load_policy, merge_auto
from odata1c.index.reindex import index_path
from odata1c.index.repository import IndexRepository

КЛАССЫ_НАЗВАНИЙ = ("org", "person")


def policy_path(home: pathlib.Path, base_name: str) -> pathlib.Path:
    return base_dir(home, base_name) / "policy.yaml"


def gate_db_path(home: pathlib.Path) -> pathlib.Path:
    return home / "gate.sqlite"


def open_dictionary(home: pathlib.Path, secret: str) -> Dictionary:
    return Dictionary(gate_db_path(home), base64.b64decode(secret))


def owner_names_for(home: pathlib.Path, base_name: str) -> set[str] | None:
    """Список `names_for` из файла владельца; нет файла или раздела — `None` (встроенный список,
    `field_rules.DEFAULT_NAMES_FOR`, ADR-0015: список — ручной раздел `bases/<база>/policy.yaml`,
    не поле `bases.yaml`)."""
    return load_policy(policy_path(home, base_name)).names_for()


def classifier_for(home: pathlib.Path, base: BaseConfig):
    """Функция-классификатор для reindex: подставляет список названий этой базы (SPEC §6.5)."""
    список = owner_names_for(home, base.name)

    def классификатор(entity: str, field: str, edm_type: str):
        return classify_field(entity, field, edm_type, names_for=список)

    return классификатор


def refresh_policy(home: pathlib.Path, base: BaseConfig) -> list[dict]:
    """Пересобрать секцию auto по индексу, сохранив ручные разделы (SPEC §4.3 п. 3).

    Правка ревью задачи 9 (Important): «на проверку» возвращает только поля классов org/person,
    у которых класс в СТАРОЙ секции auto (та, что лежала в файле до этого вызова) отличался от
    нового — не весь текущий auto целиком. Без сравнения список на боевой базе (сотни полей
    классов org/person) печатался бы заново при каждом реиндексе, меняющем состав сущностей, хотя
    для подавляющего большинства полей ничего не изменилось; сигнал о действительно новых полях
    тонет в шуме. Тот же приём, что уже применён в `index/reindex.py::_классифицировать` —
    сравнение с состоянием ДО перестройки, снятым заранее.

    Вызывать можно на каждом реиндексе, в том числе «без изменений» (находка П1): раздел `auto`
    зависит не только от индекса, но и от классификатора, а он меняется с версией шлюза. Файл при
    этом переписывается, только если меняется его содержимое (см. ниже); сборка раздела на
    индексе УТ из 7224 сущностей — около половины секунды."""
    путь = policy_path(home, base.name)
    путь.parent.mkdir(parents=True, exist_ok=True)

    хранилище = IndexRepository(index_path(home, base.name))
    try:
        список = owner_names_for(home, base.name)
        собранное = generate_policy(хранилище, names_for=список)
    finally:
        хранилище.close()

    существующая = {}
    if путь.exists():
        существующая = yaml.safe_load(путь.read_text(encoding="utf-8")) or {}
    прежний_auto = (существующая or {}).get("auto") or {}
    итог = merge_auto(существующая or собранное, собранное["auto"])
    итог.setdefault("version", 2)
    итог.setdefault("scan_free_text", True)
    итог.setdefault("defaults", собранное["defaults"])
    # Файл переписывается, только если его СОДЕРЖИМОЕ меняется (находка П1 приёмки через
    # настоящие инструменты, 2026-09-12). Политику теперь пересобирает каждый реиндекс, а не
    # только перестроивший индекс (см. `ToolService.reindex`), и фоновая проверка `$metadata`
    # зовёт его раз в сутки. Политику правит и владелец — комментарии, порядок, ручные разделы;
    # запись через `yaml.safe_dump` их разметку теряет, и терять её без смысловой перемены нельзя.
    if not путь.exists() or итог != существующая:
        путь.write_text(_в_yaml(итог), encoding="utf-8")

    return [
        {"entity": ключ.rsplit(".", 1)[0], "field": ключ.rsplit(".", 1)[1], "sensitivity": класс}
        for ключ, класс in собранное["auto"].items()
        if класс in КЛАССЫ_НАЗВАНИЙ and прежний_auto.get(ключ) != класс
    ]


def _в_yaml(данные: dict) -> str:
    шапка = (
        "# Политика гейта для базы. Приоритет разделов: fields > entities > defaults > auto.\n"
        "# Раздел auto перезаписывается при каждом реиндексе — правки вносите в fields.\n"
        "# Класс keep оставляет значение открытым; hide: true скрывает сущность от модели.\n"
    )
    return шапка + yaml.safe_dump(данные, allow_unicode=True, sort_keys=False)
