"""Проверка файла владельца и печать действующей политики (SPEC §6.9, ADR-0015, задача 4 плана
M2b): `policy check` находит опечатки в именах сущностей и полей, неизвестные классы и мёртвые
правила по индексу метаданных; `policy show`/ресурс `odata1c://policy/{base}` печатают файл
владельца вместе с объединённым видом (владелец поверх авторазметки), с пометкой источника
каждой строки — `владелец` или `авто`.

Инвариант 1 (AGENTS.md): здесь нет ни одного значения данных 1С — только имена сущностей, полей,
классов и путей к файлам. `render_effective` для ресурса вызывается с `names_visible=False`:
строки скрытых сущностей исключаются из объединённого вида безусловно (фильтр по данным, не по
представлению), а сами имена скрытых сущностей — из комментариев `# скрыты:` и
`# названия скрываются у:` (Ruling 29: имя скрытой сущности не появляется ни в одном ответе). Для
`policy show` (локальная команда владельца, не MCP) имена показываются как есть — это его
собственный файл."""

from __future__ import annotations

import dataclasses
import difflib
import pathlib
from typing import Literal

from odata1c.gate.field_rules import DEFAULT_NAMES_FOR
from odata1c.gate.policy import Policy, parse_owner_file
from odata1c.gate.tokens import CLASSES
from odata1c.index.repository import IndexRepository

# `CLASSES` (gate/tokens.py) уже включает `keep`; `scan` — класс «только сканировать значение»,
# в грамматике токенов не участвует и в CLASSES не входит. custom:* добавляются по разделу custom
# конкретного файла — собираются в `допустимые_классы` внутри `check_policy`, не здесь.
# Без подчёркивания — конструктор `policy set` (`cli.py`, задача 5 плана M2b) проверяет класс тем
# же набором: два отдельных объявления одного и того же множества разошлись бы однажды, и
# `policy set` начал бы принимать то, что `policy check` отвергает.
БАЗОВЫЕ_КЛАССЫ = CLASSES | {"scan"}

ЗАГОЛОВОК_БЛОКА = "# --- действующая политика (владелец поверх авторазметки) ---"
_ПОМЕТКА_ВСТРОЕННЫЙ_СПИСОК = " (встроенный список)"
_ПОМЕТКА_НЕ_ПОКАЗАНО = "<не показано: у базы есть скрытые сущности>"


@dataclasses.dataclass(slots=True)
class Finding:
    """Одна находка `policy check`: `where` — машиночитаемый адрес правила в файле владельца
    (`entities.Catalog_X`, `fields.Catalog_X.Поле`, `names_for[2]`, `custom.tab_number`),
    `message` — что не так, `hint` — ближайшие известные имена (`suggest_names`,
    `difflib.get_close_matches`) или пусто, если подсказать нечем."""

    level: Literal["error", "warning"]
    where: str
    message: str
    hint: str = ""


def suggest_names(repo: IndexRepository, query: str, limit: int = 3) -> list[str]:
    """Ближайшие имена сущностей по индексу (то же ранжирование, что у `odata1c_find_entity`,
    SPEC §4.4) — подсказка к опечатке в `entities`/`names_for`."""
    return [найдено.name for найдено in repo.find(query, limit=limit)]


def _подсказка_сущности(repo: IndexRepository, имя: str) -> str:
    похожие = suggest_names(repo, имя)
    return f"похожие имена: {', '.join(похожие)}" if похожие else ""


def _подсказка_поля(repo: IndexRepository, сущность: str, поле: str) -> str:
    похожие = difflib.get_close_matches(поле, repo.field_names(сущность), n=3)
    return f"похожие поля: {', '.join(похожие)}" if похожие else ""


def повторяющиеся_разделы(owner_path: pathlib.Path) -> list[tuple[str, int]]:
    """Разделы верхнего уровня, встречающиеся в файле дважды, — пара «имя раздела, номер строки
    повтора» (находка I1 итогового ревью M2b).

    Зачем отдельный проход: два разборщика читают такой файл ПО-РАЗНОМУ. `pyyaml`
    (`parse_owner_file`, `load_policy`) молча оставляет последний раздел, а `ruamel` (реиндекс —
    `strip_auto_section`, конструктор — `policy_edit`) отказывается разбирать файл вовсе. Шаблон
    сам к этому подталкивает: заглушка `entities: {}` и раскомментированный пример `# entities:`
    ниже. В порядке «правило выше заглушки» побеждает пустая заглушка — правило владельца молча не
    действует, и без этой проверки `policy check` о том не говорил ни слова.

    Разбор — `compose` (дерево узлов), а не `load`: он не конструирует значения, поэтому
    повторяющийся ключ его не останавливает, а имена и номера строк берутся из узлов ключей —
    ЗНАЧЕНИЯ не читаются вовсе (инвариант 1). Ошибка синтаксиса здесь проглатывается: до этого
    места её уже подняла `parse_owner_file`."""
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError
    from ruamel.yaml.nodes import MappingNode

    yaml_rt = YAML()
    yaml_rt.allow_duplicate_keys = True
    try:
        with owner_path.open(encoding="utf-8") as f:
            корень = yaml_rt.compose(f)
    except YAMLError:
        return []
    if not isinstance(корень, MappingNode):
        return []
    встречено: set[str] = set()
    повторы: list[tuple[str, int]] = []
    for узел_ключа, _ in корень.value:
        имя = getattr(узел_ключа, "value", None)
        if not isinstance(имя, str):
            continue
        if имя in встречено:
            отметка = getattr(узел_ключа, "start_mark", None)
            повторы.append((имя, отметка.line + 1 if отметка is not None else 0))
        встречено.add(имя)
    return повторы


def _ошибка_формата_ключа(ключ: str) -> Finding | None:
    """Ключ раздела `fields` — всегда `Сущность.Поле` (находка I3 итогового ревью M2b). Проверка
    БЕЗУСЛОВНА, индекса она не требует: `ДопИдентификатор` без точки — мёртвое правило при любом
    состоянии индекса, `Policy.sensitivity_of` ищет ключ вида `Сущность.Поле` и такую запись не
    найдёт никогда. Прежде проверка формата жила внутри ветки `repo is not None`, и на
    непроиндексированной базе `policy check` о мёртвом правиле молчал."""
    сущность, точка, поле = ключ.partition(".")
    if точка and сущность and поле:
        return None
    return Finding(
        level="error",
        where=f"fields.{ключ}",
        message=(
            f"«{ключ}» не похоже на «Сущность.Поле»: правило не сработает ни при какой политике"
        ),
        hint="имя поля пишется через точку, например Catalog_Контрагенты.ИНН",
    )


def check_policy(owner_path: pathlib.Path, repo: IndexRepository | None) -> list[Finding]:
    """Проверить файл владельца: повторяющиеся разделы верхнего уровня, опечатки в именах (по
    индексу, если он есть), ключ `fields` не вида `Сущность.Поле`, неизвестные классы, свой класс
    без `fields`/`regex`, бесполезный `keep` на сущности, закрытой владельцем же
    (`entities.hide`). Битый YAML или раздел неожиданного типа — `PolicyError` наружу
    (`parse_owner_file`), не находка: файл в этом состоянии политикой ещё не стал. Повтор раздела —
    наоборот, находка, а не отказ: `pyyaml` такой файл разбирает (оставляя последний раздел), и
    команда обязана дойти до конца и сказать владельцу, ЧТО именно повторено (находка I1 итогового
    ревью M2b).

    Без индекса (`repo is None`) имена сущностей и полей не проверяются — одна `warning` об этом,
    и только она; проверки, не требующие индекса (повтор раздела, формат ключа `fields`, класс
    поля, свой класс, `keep` на скрытой сущности), выполняются как обычно — задача 1 брифа,
    находка ревью «частичная работа лучше отказа», тот же принцип, что и у
    `_скрытые`/`resource_policy` на неполном индексе."""
    owner_data = parse_owner_file(owner_path)
    находки: list[Finding] = []

    for раздел, строка in повторяющиеся_разделы(owner_path):
        находки.append(
            Finding(
                level="error",
                where=раздел,
                message=(
                    f"раздел встречается дважды: строка {строка}; действует только последний, "
                    f"а конструктор политики и реиндекс такой файл разобрать не могут"
                ),
                hint=f"оставьте один раздел {раздел} в {owner_path}",
            )
        )

    if repo is None:
        находки.append(
            Finding(
                level="warning",
                where="index",
                message="индекса нет: имена сущностей и полей не проверены",
            )
        )

    скрытые_сущности = {
        имя
        for имя, настройки in (owner_data.get("entities") or {}).items()
        if isinstance(настройки, dict) and настройки.get("hide")
    }
    допустимые_классы = БАЗОВЫЕ_КЛАССЫ | {
        f"custom:{имя}" for имя in (owner_data.get("custom") or {})
    }

    if repo is not None:
        for сущность in owner_data.get("entities") or {}:
            if repo.resolve_name(сущность) is None:
                находки.append(
                    Finding(
                        level="error",
                        where=f"entities.{сущность}",
                        message=f"сущность «{сущность}» не найдена в индексе базы",
                        hint=_подсказка_сущности(repo, сущность),
                    )
                )
        for индекс, имя in enumerate(owner_data.get("names_for") or []):
            if not isinstance(имя, str) or repo.resolve_name(имя) is None:
                находки.append(
                    Finding(
                        level="error",
                        where=f"names_for[{индекс}]",
                        message=f"сущность «{имя}» не найдена в индексе базы",
                        hint=_подсказка_сущности(repo, str(имя)),
                    )
                )

    for ключ, класс in (owner_data.get("fields") or {}).items():
        сущность, _, поле = ключ.partition(".")
        ошибка_формата = _ошибка_формата_ключа(ключ)
        if ошибка_формата is not None:
            находки.append(ошибка_формата)

        if ошибка_формата is None and repo is not None:
            каноническое = repo.resolve_name(сущность)
            if каноническое is None:
                находки.append(
                    Finding(
                        level="error",
                        where=f"fields.{ключ}",
                        message=f"сущность «{сущность}» не найдена в индексе базы",
                        hint=_подсказка_сущности(repo, сущность),
                    )
                )
            elif поле not in repo.field_names(каноническое):
                находки.append(
                    Finding(
                        level="error",
                        where=f"fields.{ключ}",
                        message=f"поле «{поле}» не найдено у сущности «{сущность}»",
                        hint=_подсказка_поля(repo, каноническое, поле),
                    )
                )

        if isinstance(класс, str) and класс.startswith("custom:"):
            имя_класса = класс.split(":", 1)[1]
            if имя_класса not in (owner_data.get("custom") or {}):
                находки.append(
                    Finding(
                        level="error",
                        where=f"fields.{ключ}",
                        message=f"класс «{класс}» не объявлен: нет раздела custom.{имя_класса}",
                    )
                )
        elif класс not in допустимые_классы:
            находки.append(
                Finding(
                    level="error",
                    where=f"fields.{ключ}",
                    message=(
                        f"класс «{класс}» неизвестен; допустимые: "
                        f"{', '.join(sorted(БАЗОВЫЕ_КЛАССЫ))}"
                    ),
                )
            )

        if класс == "keep" and сущность in скрытые_сущности:
            находки.append(
                Finding(
                    level="warning",
                    where=f"fields.{ключ}",
                    message="правило не действует: сущность скрыта",
                )
            )

    for имя, описание in (owner_data.get("custom") or {}).items():
        описание = описание or {}
        if not описание.get("fields") and not описание.get("regex"):
            находки.append(
                Finding(
                    level="error",
                    where=f"custom.{имя}",
                    message=f"свой класс custom:{имя} не задаёт ни fields, ни regex",
                )
            )

    return находки


def effective_rows(policy: Policy, owner_data: dict) -> list[tuple[str, str, str]]:
    """Объединённый вид классов полей: правила владельца (`owner_data["fields"]`, включая
    `keep`/`scan`/`custom:*`) поверх авторазметки (`policy.auto_items()`), с источником каждой
    строки. Строки `custom` по имени поля (раздел `custom.<класс>.fields`) не входят — они уже
    перечислены в самом файле владельца, который печатается перед этим блоком (уточнение брифа
    задачи 4)."""
    строки: dict[str, tuple[str, str]] = {}
    for ключ, класс in (owner_data.get("fields") or {}).items():
        строки[ключ] = (класс, "владелец")
    for ключ, класс in policy.auto_items().items():
        строки.setdefault(ключ, (класс, "авто"))
    return sorted((ключ, класс, источник) for ключ, (класс, источник) in строки.items())


def _хвост_сущности(ключ: str) -> str:
    return ключ.split(".", 1)[0]


def render_effective(
    owner_text: str,
    policy: Policy,
    owner_data: dict,
    *,
    hidden: set[str],
    names_visible: bool = True,
) -> str:
    """Текст файла владельца (`owner_text` — как есть, включая комментарии; для ресурса модели
    вызывающий отдаёт сюда уже прогнанный через `redact_policy` текст) плюс блок действующей
    политики: что скрыто, у кого скрываются названия, и объединённый список классов полей с
    источником каждой строки (`# владелец` | `# авто`).

    `hidden` — ПОЛНЫЙ набор скрытых сущностей (корни `policy.hidden_entities()` плюс все их
    дочерние объекты по индексу, если он есть) — параметр ОБЯЗАТЕЛЬНЫЙ, вычисляет его вызывающий
    (находка 1 ревью задачи 4, Important). Прежняя версия принимала необязательный `repo` и
    строила набор сама внутри функции: без индекса под рукой (или просто забытого аргумента)
    получался НЕПОЛНЫЙ набор — только корни, без потомков, — и блок печатал строку авторазметки
    дочернего объекта скрытой сущности (`Document_X_Товары.Поле: org  # авто`), то есть называл и
    сам скрытый документ, и место, где лежат его данные. Ruling 29 держался дисциплиной
    вызывающего, а не контрактом функции. Теперь корректность набора — обязанность вызывающего:
    `resource_policy` передаёт то же самое `self._скрытые(репозиторий, гейт)`, что строкой выше
    уходит в `redact_policy`; `cmd_policy_show` — `policy.hidden_entities() |
    repo.descendants(...)`, если индекс открыт, иначе только корни (см. докстринг
    `cmd_policy_show`). Строки скрытых сущностей исключаются из блока БЕЗУСЛОВНО, независимо от
    `names_visible`, — это фильтр данных, не оформление.

    Блок не пересобирается через `redact_policy` целиком: пересборка YAML стирает комментарии
    `# авто`/`# владелец` при разборе, а именно они и есть смысл задачи 4.

    `names_visible=False` (ресурс `odata1c://policy/{base}`, Ruling 29: имя скрытой сущности не
    появляется ни в одном ответе) — комментарии `# скрыты:`/`# названия скрываются у:` не
    называют сущности, а дают только счётчик/пометку. `names_visible=True` (`policy show`,
    локальная команда) показывает имена как есть — это собственный файл владельца. Счётчик
    дочерних в `# скрыты:` — агрегат (`len(hidden - policy.hidden_entities())`), а не разбивка по
    каждому корню: разбивку строил бы только сам индекс, а функция больше его не принимает."""
    строки = [
        строка
        for строка in effective_rows(policy, owner_data)
        if _хвост_сущности(строка[0]) not in hidden
    ]

    части = [owner_text.rstrip("\n"), "", ЗАГОЛОВОК_БЛОКА]

    корни_скрытых = policy.hidden_entities()
    if корни_скрытых:
        if names_visible:
            перечень = ", ".join(sorted(корни_скрытых))
            всего_дочерних = len(hidden - корни_скрытых)
            if всего_дочерних:
                части.append(f"# скрыты: {перечень} (+ {всего_дочерних} дочерних всего)")
            else:
                части.append(f"# скрыты: {перечень}")
        else:
            части.append(f"# скрыты: {_ПОМЕТКА_НЕ_ПОКАЗАНО}")

    имена_названий = policy.names_for()
    if names_visible:
        if имена_названий is None:
            часть = (
                f"# названия скрываются у: {', '.join(sorted(DEFAULT_NAMES_FOR))}"
                f"{_ПОМЕТКА_ВСТРОЕННЫЙ_СПИСОК}"
            )
        else:
            часть = f"# названия скрываются у: {', '.join(sorted(имена_названий))}"
        части.append(часть)
    else:
        части.append(f"# названия скрываются у: {_ПОМЕТКА_НЕ_ПОКАЗАНО}")

    for ключ, класс, источник in строки:
        части.append(f"{ключ}: {класс}  # {источник}")

    return "\n".join(части) + "\n"
