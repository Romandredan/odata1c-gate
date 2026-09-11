"""Конвейер гейта на одну базу (SPEC §6.1-6.9): фасад над прямой подменой (`masking.Masker`),
обратной подменой (`unmasking.Unmasker`), стражем утечек (`guard.Guard`) и политикой
(`policy.Policy`). Слой тулов (M1d, задача 4) вызывает `BaseGate` на каждый запрос к 1С —
входящий текст/значение/ключ через `inbound_*` перед отправкой, ответ через `mask` и `finish`
перед возвратом модели.
"""

from __future__ import annotations

import json
import logging
import pathlib
from collections.abc import Callable

from odata1c.config.models import BaseConfig
from odata1c.gate.dictionary import Dictionary
from odata1c.gate.guard import Guard
from odata1c.gate.masking import Masker, MaskResult, Resolve, effective_field_class
from odata1c.gate.policy import load_policy
from odata1c.gate.revealed import RevealedValues
from odata1c.gate.unmasking import Unmasker

# Уровень для ответов, у которых база не определена (неизвестная база в запросе): классов полей
# по политике конкретной базы нет, поэтому страж проверяет по максимально строгому уровню —
# известные словарю значения (числа, названия) всё равно не должны выйти наружу (инвариант 1).
СТРОЖАЙШИЙ_УРОВЕНЬ = "identifiers+names"

_log = logging.getLogger(__name__)


class BaseGate:
    """Фасад гейта на одну базу. Держит собранные на актуальной политике `Masker`/`Unmasker`;
    `Guard` и `Dictionary` общие на процесс — база отличает их только параметром `base` вызовов."""

    def __init__(
        self,
        *,
        base: BaseConfig,
        dictionary: Dictionary,
        guard: Guard,
        policy_path: pathlib.Path,
    ) -> None:
        self._base = base
        self._dictionary = dictionary
        self._guard = guard
        self._policy_path = pathlib.Path(policy_path)
        self.mode = base.gate.mode
        self._mtime: float | None = None
        self._masker: Masker | None = None
        self._unmasker: Unmasker | None = None
        self.refresh()

    def refresh(self, *, force: bool = False) -> None:
        """Перечитать политику, если файл изменился с прошлого раза. Политику перезаписывает и
        реиндекс (раздел auto), и пользователь (fields) — демон живёт дольше одной версии файла.
        Сверка mtime дешевле разбора YAML на каждый вызов. Отсутствующий файл политики —
        не ошибка и не повод падать (`load_policy` отдаёт пустую `Policy`): демон поднимается
        и без политики, с пустыми классами.

        `force=True` — перечитать безусловно (план M1d, задача 7): тот, кто ТОЛЬКО ЧТО сам
        переписал политику (`ToolService.reindex` → `refresh_policy`), не может опираться на
        mtime. Прежнее значение снято этим же гейтом секундой раньше, и если файловая система
        отдала обеим отметкам одно значение (разрешение mtime на Windows — не наносекунды),
        обычный `refresh()` счёл бы новую политику прежней и оставил бы маскировщик на старых
        классах полей — то есть новое защищаемое поле ушло бы модели открытым."""
        mtime = self._policy_path.stat().st_mtime if self._policy_path.exists() else None
        if not force and mtime == self._mtime and self._masker is not None:
            return
        policy = load_policy(self._policy_path)
        self._policy, self._mtime = policy, mtime
        self._masker = Masker(self._dictionary, policy, mode=self.mode, base=self._base.name)
        self._unmasker = Unmasker(
            self._dictionary,
            base=self._base.name,
            field_class=lambda entity, field, *, strict=False: effective_field_class(
                policy, entity, field, mode=self.mode, strict=strict
            ),
        )

    def is_hidden(self, entity: str) -> bool:
        """Выписано ли правило `entities.hide` прямо на эту сущность. Наследование запрета на
        дочерние объекты (Ruling 30) здесь не учитывается и учтено быть не может: родство знает
        индекс метаданных, а гейт его не читает — полный набор строит `ToolService._скрытые`."""
        return self._policy.is_hidden(entity)

    def hidden_entities(self) -> set[str]:
        """Корни запрета — имена с `entities.hide: true`; см. `policy.Policy.hidden_entities`."""
        return self._policy.hidden_entities()

    def has_hidden_entities(self) -> bool:
        """Есть ли у базы хоть одно правило `entities.hide` — см. `policy.Policy.has_hidden`."""
        return self._policy.has_hidden()

    def field_class(self, entity: str, field: str, *, strict: bool = False) -> str | None:
        """Эффективный класс поля по текущей политике и уровню гейта. Публичный доступ к тому,
        что до сих пор брали через приватную `_policy` (долг, отмеченный в `tools/service.py`
        при задаче 4): слою тулов класс нужен не только ответом «защищено или нет» — от самого
        класса зависят анти-оракульные правила (`org`/`person` ищутся по подстроке, `dob` не
        сравнивается с открытым литералом).

        `strict` — сущность не подтверждена индексом (см. `masking.effective_field_class`): тот
        же строгий взгляд, что и у маскировки (Ruling 18)."""
        return effective_field_class(self._policy, entity, field, mode=self.mode, strict=strict)

    def is_protected(self, entity: str, field: str, *, strict: bool = False) -> bool:
        """Класс поля — что-то, кроме «не защищён» (`None`), «оставить как есть» (`keep`) или
        «только сканировать значение» (`scan`, значение целиком не заменяется по классу поля).

        `strict` пробрасывается в `field_class`: иначе запрет на оракул порядка (`$orderby` по
        защищаемому полю) снимался бы ровно там, где снимается маска, — на сущности вне индекса
        (форма (г) ревью 2026-09-11)."""
        return self.field_class(entity, field, strict=strict) not in (None, "keep", "scan")

    def inbound_filter(
        self, expression: str, *, entity: str, revealed: RevealedValues, strict: bool = False
    ) -> str:
        """Выражение отбора от модели: обратная подмена токенов и анти-оракульные правила (SPEC
        §6.7). Единственная точка, где эти правила реализованы, — рецепты прогоняют через неё
        свои условия (Ruling 20, пункт 3), а не повторяют список своими силами.

        `revealed` — набор вызова, в который записывается КАЖДОЕ раскрытое значение (задача N1
        M1d). Аргумент обязателен, а не с умолчанием `None`: раскрытие без набора и есть тот
        дефект, который здесь чинится — значение уходит в 1С, а страж на обратном пути его не
        узнаёт. Отказ типа делает пропуск невозможным, в том числе у тулов, которых ещё нет.

        `strict` — путь не разрешён по индексу целиком (Ruling 18, пункт 7 дополнения): «любые
        другие параметры, которые на разрешённом пути отклоняются как оракул, на неразрешённом
        отклоняются тем более». Без проброса `Description ge 'М'` на сущности вне индекса уходил
        в 1С — двоичный поиск по названию, — хотя на известной сущности отклонялся."""
        if self.mode == "off":
            return expression
        return self._unmasker.filter(expression, entity=entity, strict=strict, revealed=revealed)

    def inbound_value(
        self,
        text: str,
        *,
        entity: str,
        field: str,
        revealed: RevealedValues,
        strict: bool = False,
    ) -> str:
        """Одно значение от модели (параметр рецепта, элемент ключа). Раскрытие токена здесь
        возможно только при известном поле и совпадении класса — см. `unmasking._раскрыть`.
        `revealed` — набор вызова, см. `inbound_filter`."""
        if self.mode == "off":
            return text
        return self._unmasker.value(
            text, entity=entity, field=field, strict=strict, revealed=revealed
        )

    def inbound_key(self, key, *, entity: str, revealed: RevealedValues, strict: bool = False):
        """Ключ записи от модели. `revealed` — набор вызова, см. `inbound_filter`."""
        if self.mode == "off":
            return key
        return self._unmasker.key(key, entity=entity, strict=strict, revealed=revealed)

    def mask(
        self,
        data,
        *,
        entity: str,
        resolve: Resolve,
        hidden: Callable[[str], bool],
        revealed: RevealedValues | None,
        strict: bool = False,
    ) -> MaskResult:
        """`resolve` — резолвер «сущность и ключ ответа → сущность вложенного объекта» (итоговое
        ревью M1d, C1). Аргумент обязателен, а не с умолчанием `None`: маскировка раскрытого
        через `$expand` объекта по политике чужой сущности и есть тот дефект, который здесь
        чинится, — отказ типа делает пропуск невозможным, в том числе у тулов, которых ещё нет.
        Тот же приём, что с `revealed` у `inbound_*` (задача N1 M1d).

        `hidden` — скрыта ли сущность запретом владельца. Вложенный объект скрытой сущности
        изымается из ответа — второй рубеж к обрезке `$expand` в запросе, для того что 1С отдаёт
        без спроса (табличные части). Аргумент обязателен по той же причине, что `resolve` и
        `revealed`: запрет наследуется на дочерние объекты (Ruling 30), а знает об этом только
        `ToolService`, у которого есть индекс, — умолчание `self.is_hidden` молча вернуло бы
        неполный запрет, и ошибку никто бы не заметил.

        `revealed` — набор раскрытого в этом вызове (находка П2 приёмки через настоящие
        инструменты, 2026-09-12): ранний проход (`scrubber`) уже подменил раскрытые значения в
        сыром теле токенами, и без набора маскировщик выдаёт полю с классом токен по чужому
        токену — одна запись с отбором и без него приходит разными токенами. Аргумент обязателен
        по той же причине, что `resolve` и `hidden`: пропуск не падает, а тихо ломает инвариант 5.
        `None` — только у вызова, который заведомо ничего не раскрывал."""
        return self._masker.mask(
            data, entity=entity, resolve=resolve, hidden=hidden, strict=strict, revealed=revealed
        )

    def scrub_revealed(self, text: str, revealed: RevealedValues | None) -> str:
        """Обратная замена раскрытого по СЫРОМУ тексту от 1С — до всех преобразований
        (Ruling 25, раунд правок 1).

        Слой раскрытого держится на точном вхождении, а до стража текст успевают переписать:
        `mask_text` заменяет телефон или название ВНУТРИ раскрытого адреса, `truncate_strings` и
        `map_error` режут значение посередине, — и вхождение перестаёт совпадать. Поэтому проход
        делается первым в конвейере ответа, а не первым внутри стража: на сыром теле ответа 1С
        (и успешном, и ошибочном) заменять нечему мешать. Дальше конвейер работает как работал,
        а `finish` со стражем остаётся последним рубежом.

        Пустой набор (вызов ничего не раскрывал — обычный случай) стоит одну проверку: автомат
        не собирается, текст возвращается тем же объектом."""
        if self.mode == "off" or not revealed:
            return text
        return self._guard.scrub_revealed(text, revealed)

    def scrubber(self, revealed: RevealedValues | None):
        """Функция обратной замены раскрытого для клиента 1С (`Client1C.get(scrub=…)`):
        единственное, что смотрит на сырой ответ базы до разбора, маскировки и усечения."""
        return lambda текст: self.scrub_revealed(текст, revealed)

    def finish(self, envelope: dict, revealed: RevealedValues | None = None) -> str:
        """Сериализация ответа тула (`ensure_ascii=False` — страж должен видеть кириллицу как
        есть, не в `\\uXXXX`-экранировании) и страж утечек как последний проход по готовому
        тексту (SPEC §6.8, инвариант 1). Заменивший что-то страж помечает ответ `guard_replaced`
        в `warnings` — маскировщик пропустил значение, страж поймал его отдельно.

        Штатно `guard.py` сохраняет валидность JSON у изменённого текста (F2 M1c) — `json.loads`
        ниже на это опирается. `try/except` вокруг него — страховка последнего рубежа (Important,
        ревью 2026-09-10): finish — часть инварианта 1, и голое исключение здесь потеряло бы
        текст ответа у клиента. Если страж когда-нибудь вернёт невалидный JSON, отдаём его текст
        как есть (он уже прошёл страж — утечки в нём нет, только `warnings` не допишутся) и
        логируем сам факт, без текста ответа (в нём могут быть данные).

        `revealed` — набор раскрытого в этом вызове (задача N1 M1d): страж ищет в ответе и его.
        Умолчание `None` здесь допустимо, в отличие от `inbound_*`: у вызова, который ничего не
        раскрывал, набора и нет, а тот, кто раскрывал, получает набор и на входе, и на выходе из
        одного места — `ToolService._run`."""
        текст = json.dumps(envelope, ensure_ascii=False)
        проверено = self._guard.check(текст, mode=self.mode, revealed=revealed)
        # Замены, сделанные проходом по сырому ответу (`scrub_revealed`), считаются наравне с
        # заменами этого прохода: к моменту `finish` заменять уже нечего, но ответ всё равно
        # содержал защищённое значение, и получатель обязан это видеть (Ruling 25).
        всего = len(проверено.replacements) + (revealed.replacements if revealed else 0)
        if not всего:
            return проверено.text
        try:
            данные = json.loads(проверено.text)
        except json.JSONDecodeError:
            _log.error("страж вернул невалидный JSON при непустых replacements")
            return проверено.text
        данные.setdefault("warnings", []).append(
            f"guard_replaced: страж заменил {всего} значений, не распознанных маскировщиком"
        )
        return json.dumps(данные, ensure_ascii=False)

    def finish_text(self, text: str, revealed: RevealedValues | None = None) -> str:
        """Страж по готовому тексту целиком, без обёртки в JSON-конверт — для markdown-ответов
        (`describe` и подобные), а не JSON-тулов. `revealed` — как у `finish`."""
        return self._guard.check(text, mode=self.mode, revealed=revealed).text

    def error(
        self, code: str, message: str, hint: str = "", revealed: RevealedValues | None = None
    ) -> str:
        """Ошибка тула — обычный текст с JSON `{"error": {...}}` (SPEC §5.2), не исключение.
        Сообщение 1С и подсказка могут содержать реальное значение (инвариант 1: ошибки 1С —
        такой же путь утечки, как обычный ответ) — маскируются `mask_text` тем же маскировщиком,
        что и ответ, следом идёт обычный `finish` со стражем.

        Набор раскрытого нужен здесь не меньше, чем на успешном пути, а по сути — больше: это
        главный путь эха (1С повторяет выражение отбора в сообщении об ошибке), и `mask_text`
        на нём бессилен, потому что у `addr`/`dob`/свободнотекстового `doc` детектора нет
        (задача N1 M1d).

        Набор проходит по тексту ДО `mask_text` (Ruling 25, раунд правок 1): иначе детектор
        заменит реквизит ВНУТРИ раскрытого значения — телефон, вписанный в адрес, обычное
        содержимое свободных полей 1С, — и точное вхождение адреса больше не совпадёт, а адресная
        часть выйдет открытой. Соглашение проекта «маскировка, затем страж» при этом не меняется:
        проход добавляется перед, а не переставляется существующий."""
        message = self.scrub_revealed(message, revealed)
        hint = self.scrub_revealed(hint, revealed) if hint else hint
        сообщение = self._masker.mask_text(message, entity="", field="error")
        подсказка = self._masker.mask_text(hint, entity="", field="error") if hint else hint
        return self.finish(
            {"error": {"code": code, "message": сообщение, "hint": подсказка}}, revealed
        )


def guard_only(guard: Guard, envelope: dict) -> str:
    """Сериализация и страж на строжайшем уровне — для ответов, у которых база не определена
    (запрос на неизвестную/недоступную базу): политики и классов полей ещё нет, но известные
    словарю значения всё равно не должны выйти наружу (инвариант 1).

    Набора раскрытого здесь нет и быть не может, и это не упущение: пока база не разрешена (или
    гейт этой базы не построился), обратная подмена не выполнялась ни разу — раскрывать токены
    некому и нечем (задача N1 M1d)."""
    текст = json.dumps(envelope, ensure_ascii=False)
    return guard.check(текст, mode=СТРОЖАЙШИЙ_УРОВЕНЬ).text
