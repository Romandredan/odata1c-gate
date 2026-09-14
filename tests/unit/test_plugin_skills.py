"""Навыки плагина Claude Code (`plugin/skills/*/SKILL.md`), план M3 задача 5.

Проверяется то, без чего навык не подключится или не выполнит своё назначение: frontmatter
(`name` совпадает с именем каталога, `description` не пуст — иначе плагин не загрузит навык или
модель не поймёт, когда его применять), предел длины `odata1c/SKILL.md` (SPEC §11.3: справочники
не дублируются в теле навыка, они отсылают к темам `odata1c_info` — длинный файл значит, что
что-то продублировано) и обязательные упоминания — без них навык неполон по брифу задачи: имена
ключевых тулов и файлов у `odata1c`, признаки протокола сохранения рецепта у `odata1c-recipe`.
Дословное содержание и стиль текста тест не проверяет — это дело ревью.

Раунд правок 1 (`task-5-review.md`) добавляет регрессионные проверки на четыре Major-находки:
M1 — `odata1c-recipe` берёт `config` только из ответа `odata1c_bases` и не призывает читать
`bases.yaml`; M2 — `permission_denied` в `odata1c` различает отказ по разрешениям и отказ
пользователя в диалоге `commit`, повтор `commit` без новой просьбы запрещён; M3 —
`pending_expired`/`pending_stale` требуют нового превью и нового согласия, а не просто повтора
подготовки; M4 — проводник по базе учит передавать `base` в каждом вызове явно."""

from __future__ import annotations

import pathlib

import pytest
import yaml

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
НАВЫКИ = КОРЕНЬ / "plugin" / "skills"


def _файлы_навыков() -> list[pathlib.Path]:
    return sorted(НАВЫКИ.glob("*/SKILL.md"))


def _frontmatter(текст: str) -> dict:
    """Блок YAML между первой и второй строкой `---`. Пустой текст или отсутствие второй границы
    даёт пустой словарь — вызывающий тест провалится на отсутствующих ключах, а не здесь."""
    if not текст.startswith("---\n"):
        return {}
    _, _, остаток = текст.partition("---\n")
    сырой, разделитель, _ = остаток.partition("\n---")
    if not разделитель:
        return {}
    return yaml.safe_load(сырой) or {}


def test_ожидаемые_навыки_существуют() -> None:
    """Задача 5 добавляет `odata1c` и `odata1c-recipe` к уже существующему `odata1c-policy`."""
    имена = {путь.parent.name for путь in _файлы_навыков()}
    assert {"odata1c", "odata1c-policy", "odata1c-recipe"} <= имена


@pytest.mark.parametrize("путь", _файлы_навыков(), ids=lambda p: p.parent.name)
def test_frontmatter_имя_и_описание(путь: pathlib.Path) -> None:
    метаданные = _frontmatter(путь.read_text(encoding="utf-8"))
    assert метаданные.get("name") == путь.parent.name
    assert метаданные.get("description")


def test_odata1c_skill_не_длиннее_250_строк() -> None:
    путь = НАВЫКИ / "odata1c" / "SKILL.md"
    строки = путь.read_text(encoding="utf-8").splitlines()
    assert len(строки) <= 250


@pytest.mark.parametrize(
    "фраза",
    ["odata1c_bases", "odata1c_info", "odata1c_commit", "reveal", "bases.yaml"],
)
def test_odata1c_skill_содержит(фраза: str) -> None:
    текст = (НАВЫКИ / "odata1c" / "SKILL.md").read_text(encoding="utf-8")
    assert фраза in текст


@pytest.mark.parametrize("фраза", ["recipe check", "recipes/", "{параметр}"])
def test_odata1c_recipe_skill_содержит(фраза: str) -> None:
    текст = (НАВЫКИ / "odata1c-recipe" / "SKILL.md").read_text(encoding="utf-8")
    assert фраза in текст


# --- Раунд правок 1: регрессия на находки M1-M4 (task-5-review.md) ----------------------------


@pytest.mark.parametrize(
    "фраза",
    [
        # M4: базу передают параметром в каждом вызове, а не полагаются на умолчание.
        "в КАЖДОМ вызове",
        # M2: два разных permission_denied — флаг разрешений и отказ пользователя в диалоге;
        # повторный commit без новой просьбы запрещён явно.
        "отказался в диалоге",
        "не повторяйте",
        # M3: истёкшая/устаревшая операция требует нового превью и нового согласия, а не просто
        # повторной подготовки.
        "новое превью",
        "новое согласие",
    ],
)
def test_odata1c_skill_содержит_правки_раунда_1(фраза: str) -> None:
    текст = (НАВЫКИ / "odata1c" / "SKILL.md").read_text(encoding="utf-8")
    assert фраза in текст


@pytest.mark.parametrize(
    "фраза",
    [
        # M1: config берётся только из ответа odata1c_bases, bases.yaml модель не читает.
        "из ответа `odata1c_bases`",
        "не читайте и не редактируйте",
    ],
)
def test_odata1c_recipe_skill_содержит_правки_раунда_1(фраза: str) -> None:
    текст = (НАВЫКИ / "odata1c-recipe" / "SKILL.md").read_text(encoding="utf-8")
    assert фраза in текст
