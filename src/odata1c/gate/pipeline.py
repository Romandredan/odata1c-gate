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
from odata1c.gate.contact_info import Shape
from odata1c.gate.dictionary import Dictionary
from odata1c.gate.guard import Guard
from odata1c.gate.masking import (
    Masker,
    MaskResult,
    Resolve,
    inbound_field_class,
    inbound_path_class,
)
from odata1c.gate.policy import load_policy
from odata1c.gate.revealed import RevealedValues, ScrubbedText
from odata1c.gate.tokens import find_tokens, is_partial_token, parse_token
from odata1c.gate.unmasking import (
    GateError,
    Unmasker,
    open_literal_refusal,
    token_fragment_refusal,
)

# Уровень для ответов, у которых база не определена (неизвестная база в запросе): классов полей
# по политике конкретной базы нет, поэтому страж проверяет по максимально строгому уровню —
# известные словарю значения (числа, названия) всё равно не должны выйти наружу (инвариант 1).
СТРОЖАЙШИЙ_УРОВЕНЬ = "identifiers+names"

# Пустая дата 1С: так платформа отдаёт и принимает незаполненную дату.
ПУСТАЯ_ДАТА_1С = "0001-01-01T00:00:00"

_log = logging.getLogger(__name__)


def _несёт_токен(значение) -> bool:
    """Строка входа (на любой глубине словаря или списка) с токеном шлюза — целым, внутри текста
    или обрезком (`is_partial_token`: оборванный, испорченный, неверный регистр класса)."""
    if isinstance(значение, str):
        return bool(find_tokens(значение)) or is_partial_token(значение)
    if isinstance(значение, dict):
        return any(_несёт_токен(ключ) or _несёт_токен(поле) for ключ, поле in значение.items())
    if isinstance(значение, list | tuple):
        return any(_несёт_токен(элемент) for элемент in значение)
    return False


def _отказ_токена_без_гейта(значение) -> None:
    """И-7 итогового ревью M2: на уровне `off` гейт ничего не раскрывает, и токен уходил в 1С
    текстом `[[inn:…]]` — в тело записи (порча данных: роль `dev` по умолчанию с гейтом `off` и
    включённой записью), в отбор (пустой ответ вместо записи), в ключ и параметр. Модель берёт
    токен из ответа базы с гейтом (словарь общий) и по инструкции подставляет «как есть». Отказ
    одинаков на всех входах; ввод модели в текст не идёт (Ruling 51, 54).

    Признак — тот же `is_partial_token`, что у баз с гейтом: под него попадает и строка с двойными
    квадратными скобками, токеном не являющаяся («Склад [[основной]]», `substringof(']]', …)`).
    Сузить признак до грамматики токена нельзя — она не ловит ровно те формы, ради которых
    `is_partial_token` и заведён: `[[INN:…]]` (неверный регистр класса), `[[inn: …]]` (пробел),
    `[[inn:…]` (одна закрывающая скобка); в поле без контрольной суммы они уходили бы в 1С
    дословно. Поэтому сужен не признак, а текст: он говорит про двойные скобки, а не только про
    токены (М-1 ревью итоговых правок M2)."""
    if _несёт_токен(значение):
        raise GateError(
            "params_invalid",
            "гейт этой базы выключен: токены шлюза здесь не раскрываются, а строка с двойными "
            "квадратными скобками от токена или его обрезка неотличима — передайте значение",
            "на базе с уровнем гейта off токен из ответа другой базы ушёл бы в 1С текстом "
            "[[…]]; передайте значение, продиктованное пользователем, без «[[» и «]]»",
        )


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
        auto_path: pathlib.Path | None = None,
    ) -> None:
        self._base = base
        self._dictionary = dictionary
        self._guard = guard
        self._policy_path = pathlib.Path(policy_path)
        self._auto_path = pathlib.Path(auto_path) if auto_path else None
        self.mode = base.gate.mode
        self._stamp: tuple | None = None
        self._masker: Masker | None = None
        self.refresh()

    def _отметка(self) -> tuple:
        """Отпечаток обоих файлов политики (ADR-0015): владелец правит `policy.yaml`, реиндекс —
        `policy.auto.yaml`, и перемена любого из них требует пересборки `Masker`."""

        def одна(путь: pathlib.Path | None):
            return (путь.stat().st_mtime, путь.stat().st_size) if путь and путь.exists() else None

        return (одна(self._policy_path), одна(self._auto_path))

    def refresh(self, *, force: bool = False) -> None:
        """Перечитать политику, если один из файлов изменился с прошлого раза. Политику
        перезаписывает и реиндекс (`policy.auto.yaml` целиком), и владелец (`policy.yaml`) —
        демон живёт дольше одной версии файла. Сверка отпечатка (mtime, size) дешевле разбора YAML
        на каждый вызов; размер в паре с mtime — та же страховка, что и раньше (mtime на Windows
        не наносекунды), только на оба файла разом. Отсутствующий файл политики — не ошибка и не
        повод падать (`load_policy` отдаёт пустую `Policy`): демон поднимается и без политики,
        с пустыми классами.

        `force=True` — перечитать безусловно (план M1d, задача 7): тот, кто ТОЛЬКО ЧТО сам
        переписал политику (`ToolService.reindex` → `refresh_policy`), не может опираться на
        отпечаток. Прежнее значение снято этим же гейтом секундой раньше, и если файловая система
        отдала обеим отметкам одно значение, обычный `refresh()` счёл бы новую политику прежней и
        оставил бы маскировщик на старых классах полей — то есть новое защищаемое поле ушло бы
        модели открытым."""
        отметка = self._отметка()
        if not force and отметка == self._stamp and self._masker is not None:
            return
        policy = load_policy(self._policy_path, self._auto_path)
        self._policy, self._stamp = policy, отметка
        self._masker = Masker(self._dictionary, policy, mode=self.mode, base=self._base.name)

    def _обратная_подмена(self, shape: Shape) -> Unmasker:
        """`Unmasker` на один вызов: класс поля на входе зависит от строения сущностей по индексу
        (Ruling 35 — поле значения контактной информации), а строение знает только слой тулов и
        передаёт его в каждый вызов, как в `mask`. Сборка дешёвая — словарь и политика общие, — а
        держать `Unmasker` со строением прошлого вызова нельзя: индекс перестраивается реиндексом
        посреди жизни демона."""
        policy, mode = self._policy, self.mode
        return Unmasker(
            self._dictionary,
            base=self._base.name,
            field_class=lambda entity, field, *, strict=False: inbound_field_class(
                policy, entity, field, mode=mode, shape=shape, strict=strict
            ),
            path_class=lambda entity, path: inbound_path_class(policy, entity, path, shape=shape),
            shape=shape,
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

    def field_class(
        self, entity: str, field: str, *, shape: Shape, strict: bool = False
    ) -> str | None:
        """Эффективный класс поля по текущей политике и уровню гейта. Публичный доступ к тому,
        что до сих пор брали через приватную `_policy` (долг, отмеченный в `tools/service.py`
        при задаче 4): слою тулов класс нужен не только ответом «защищено или нет» — от самого
        класса зависят анти-оракульные правила (`org`/`person` ищутся по подстроке, `dob` не
        сравнивается с открытым литералом).

        `strict` — сущность не подтверждена индексом (см. `masking.effective_field_class`): тот
        же строгий взгляд, что и у маскировки (Ruling 18).

        Это класс ВХОДНОГО пути (`masking.inbound_field_class`): поле значения контактной
        информации получает `contact` (Ruling 35). `shape` — строение сущностей по индексу,
        обязателен по той же причине, что у `mask`: без него поле контактной информации тихо
        получило бы класс из политики, где его нет, и оракул отбора открылся бы снова."""
        return inbound_field_class(
            self._policy, entity, field, mode=self.mode, shape=shape, strict=strict
        )

    def path_class(self, entity: str, path: str, *, shape: Shape) -> str | None:
        """Класс пути по сущности, в которую путь приходит (Ruling 37,
        `masking.inbound_path_class`): `contact` для поля значения табличной части контактной
        информации, `keep` по ручному правилу владельца, `None` — путь правилу не подлежит.
        Нужен слою тулов для `$orderby`: сортировка по такому пути — тот же оракул порядка."""
        return inbound_path_class(self._policy, entity, path, shape=shape)

    def is_protected(self, entity: str, field: str, *, shape: Shape, strict: bool = False) -> bool:
        """Класс поля — что-то, кроме «не защищён» (`None`), «оставить как есть» (`keep`) или
        «только сканировать значение» (`scan`, значение целиком не заменяется по классу поля).

        `strict` пробрасывается в `field_class`: иначе запрет на оракул порядка (`$orderby` по
        защищаемому полю) снимался бы ровно там, где снимается маска, — на сущности вне индекса
        (форма (г) ревью 2026-09-11)."""
        return self.field_class(entity, field, shape=shape, strict=strict) not in (
            None,
            "keep",
            "scan",
        )

    def inbound_filter(
        self,
        expression: str,
        *,
        entity: str,
        revealed: RevealedValues,
        shape: Shape,
        strict: bool = False,
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
        в 1С — двоичный поиск по названию, — хотя на известной сущности отклонялся.

        `shape` — строение сущностей по индексу (Ruling 35), см. `field_class`."""
        if self.mode == "off":
            _отказ_токена_без_гейта(expression)
            return expression
        return self._обратная_подмена(shape).filter(
            expression, entity=entity, strict=strict, revealed=revealed
        )

    def inbound_value(
        self,
        text: str,
        *,
        entity: str,
        field: str,
        revealed: RevealedValues,
        shape: Shape,
        strict: bool = False,
    ) -> str:
        """Одно значение от модели (параметр рецепта, элемент ключа). Раскрытие токена здесь
        возможно только при известном поле и совпадении класса — см. `unmasking._раскрыть`.
        `revealed` — набор вызова, `shape` — строение по индексу; см. `inbound_filter`."""
        if self.mode == "off":
            _отказ_токена_без_гейта(text)
            return text
        return self._обратная_подмена(shape).value(
            text, entity=entity, field=field, strict=strict, revealed=revealed
        )

    def inbound_key(
        self,
        key,
        *,
        entity: str,
        revealed: RevealedValues,
        shape: Shape,
        strict: bool = False,
    ):
        """Ключ записи от модели. `revealed` — набор вызова, `shape` — строение по индексу; см.
        `inbound_filter`."""
        if self.mode == "off":
            _отказ_токена_без_гейта(key)
            return key
        return self._обратная_подмена(shape).key(
            key, entity=entity, strict=strict, revealed=revealed
        )

    def inbound_write(
        self,
        data: dict,
        *,
        entity: str,
        shape: Shape,
        current: dict | None,
        revealed: RevealedValues,
        strict: bool = False,
        numbering: dict[str, int] | None = None,
        row: int | None = None,
    ) -> dict:
        """Тело PATCH/POST пишущего тула M2: токены → реальные значения (Б-1). `current` —
        текущее состояние объекта так, как его вернул `client.get(..., scrub=self.scrubber(
        revealed))` (update), `None` — для create. Правило — `Unmasker.write`: написание берётся
        из текущего значения поля, иначе единственное известное словарю, иначе отказ
        `token_ambiguous`; молчаливого выбора нет. Анти-оракульные и классовые правила — те же,
        что у `inbound_value`. `revealed`, `shape`, `strict` — как у `inbound_filter`.

        `numbering`/`row` — тело, раскрываемое несколькими вызовами (Ruling 56, `create`: строка
        табличной части — от имени своей сущности): общая нумерация токенов всего тела
        (`unmasking.number_tokens`) и номер строки; место для текста отказа гейт строит сам по
        индексу, строки от вызывающего там нет; см. `Unmasker.write`."""
        if self.mode == "off":
            _отказ_токена_без_гейта(data)
            return data
        return self._обратная_подмена(shape).write(
            data,
            entity=entity,
            current=current,
            revealed=revealed,
            strict=strict,
            numbering=numbering,
            row=row,
        )

    def check_open_literal(
        self, entity: str, field: str, value, *, shape: Shape, strict: bool = False
    ) -> None:
        """Отказывает `GateError`, если `value` — открытый литерал (не целый токен) для поля,
        которое на входе принимает только токен (`unmasking.open_literal_refusal`: `dob`,
        `contact`). Код и текст — те же, что у отбора на чтении.

        Нужна пишущему тулу ДО обращения к 1С (Ruling 45, находка I-1 ревью задачи 5 M2):
        `inbound_write` открытый литерал в тело пропускает — там пишется новое значение, — но
        подготовка `update` сравнивает его с текущим и отвечает «изменений нет», то есть задаёт
        тот же вопрос, что `ДатаРождения eq datetime'…'`, запрещённый на чтении. Класс — входной
        (`field_class`), как у отбора. Не строка — не литерал: тип проверяет вызывающий. Строка с
        токеном внутри или его обрезком получает `token_partial`, как на чтении, где этот код
        стоит раньше общего отказа (`unmasking.token_fragment_refusal`).

        Только `update` (Ruling 47): `create` литерал не отклоняет — у нового объекта сравнивать
        не с чем. Пустая дата 1С (`0001-01-01T00:00:00`) — очистка поля, а не догадка о дате, и
        литералом не считается (М-3 повторного ревью): иначе очистить дату рождения было бы
        нечем. Ответ «изменений нет» на неё говорит лишь, что дата пуста, — один признак, а не
        перебор пространства дат. Отбор на чтении это исключение не задевает."""
        if self.mode == "off" or not isinstance(value, str) or parse_token(value) is not None:
            return
        if value == ПУСТАЯ_ДАТА_1С:
            return
        отказ = open_literal_refusal(
            self.field_class(entity, field, shape=shape, strict=strict), field
        )
        if отказ is not None:
            raise token_fragment_refusal(field, value) or отказ

    def mask(
        self,
        data,
        *,
        entity: str,
        resolve: Resolve,
        hidden: Callable[[str], bool],
        revealed: RevealedValues | None,
        shape: Shape,
        strict: bool = False,
        persist: bool = True,
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
        `None` — только у вызова, который заведомо ничего не раскрывал.

        `shape` — строение сущностей по индексу (`contact_info.EntityShape`, Ruling 33): по нему
        узнаётся строка контактной информации, в которой нет поля `Тип` (модель выбрала одно
        `Представление`). Обязателен по той же причине, что `resolve`: без него такая строка
        тихо уходит по пути свободного текста, где адрес не ловит ни один детектор.

        `persist=False` — маска без записи в словарь (Ruling 58): для данных, которые не ответ 1С
        этого вызова, — журнал записи (`WriteService.journal`). См. `Masker.mask`."""
        return self._masker.mask(
            data,
            entity=entity,
            resolve=resolve,
            hidden=hidden,
            strict=strict,
            revealed=revealed,
            shape=shape,
            persist=persist,
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
        return self._guard.scrub_revealed(text, revealed, mode=self.mode)

    def scrub_revealed_json(self, text: str, revealed: RevealedValues | None):
        """Тот же ранний проход по сырому телу УСПЕШНОГО ответа — и его разбор JSON (Ruling 32):
        строки, которые проход переписал, приходят `revealed.ScrubbedText`, исходное значение
        своего места каждая оставляет в наборе вызова (Б-3), и маскировщик токенизирует поле с
        классом по нему. Без раскрытого — обычный `json.loads`."""
        if self.mode == "off" or not revealed:
            return json.loads(text)
        return self._guard.scrub_revealed_json(text, revealed, mode=self.mode)

    def scrubber(self, revealed: RevealedValues | None) -> Scrubber:
        """Ранний проход для клиента 1С (`Client1C.get(scrub=…)`): единственное, что смотрит на
        сырой ответ базы до разбора, маскировки и усечения."""
        return Scrubber(self, revealed)

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
        одного места — `ToolService._run`.

        Сторож Б-3 (находка M-6 ревью 7): строка, переписанная ранним проходом (`ScrubbedText`),
        в конверте означает, что тул положил ответ 1С в выдачу мимо маскировщика, — ответ
        заменяется ошибкой `internal`. См. `_отказ_мимо_маскировщика`."""
        if _есть_строка_раннего_прохода(envelope):
            return self._отказ_мимо_маскировщика(revealed)
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
        (`describe` и подобные), а не JSON-тулов. `revealed` — как у `finish`, сторож Б-3 — тоже."""
        if isinstance(text, ScrubbedText):
            return self._отказ_мимо_маскировщика(revealed)
        return self._guard.check(text, mode=self.mode, revealed=revealed).text

    def _отказ_мимо_маскировщика(self, revealed: RevealedValues | None) -> str:
        """Ответ вместо конверта, в котором нашлась строка раннего прохода (сторож Б-3).

        Почему отказ, а не «привести к обычной строке и отдать». Сама строка утечки не несёт:
        исходное значение хранит набор вызова (`RevealedValues.original_of`), в строке — токены
        раннего прохода. Но она не прошла маскировщик, а поле с классом закрывает только он:
        ранний проход меняет лишь раскрытое в этом вызове, а соседний реквизит той же строки
        (второй ИНН, телефон, название) остаётся открытым до стража — последнего рубежа, а не
        основного. Такой ответ — дефект тула (M2: ответ на запись, отданный как пришёл), и
        показать его громко дешевле, чем полагаться на то, что страж узнает всё.

        В журнал — только факт: ни текста ответа, ни пути к строке (в нём могут быть данные)."""
        _log.error("сторож Б-3: в ответ тула попала строка раннего прохода мимо маскировщика")
        конверт = {
            "error": {
                "code": "internal",
                "message": "внутренняя ошибка шлюза: ответ 1С не прошёл маскировку",
                "hint": "это дефект шлюза, а не запроса; сообщите владельцу",
            }
        }
        текст = json.dumps(конверт, ensure_ascii=False)
        return self._guard.check(текст, mode=self.mode, revealed=revealed).text

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
        сообщение = self.mask_message(message, revealed)
        подсказка = self.mask_message(hint, revealed) if hint else hint
        return self.finish(
            {"error": {"code": code, "message": сообщение, "hint": подсказка}}, revealed
        )

    def mask_message(self, text: str, revealed: RevealedValues | None = None) -> str:
        """Текст сообщения — ошибки 1С или отказа — так же, как его маскирует `error`: ранний
        проход раскрытого по сырому тексту, затем `mask_text` без записи в словарь (Ruling 48).
        Результат — ещё не ответ: его кладут в конверт, и конверт проходит `finish` со стражем.

        Отдельно от `error` — для текста ошибки, который лежит не в отказе, а в данных ответа:
        сохранённый журналом текст ошибки 1С (`WriteService.journal`, Ruling 58)."""
        return self._masker.mask_text(self.scrub_revealed(text, revealed), entity="", field="error")


def _есть_строка_раннего_прохода(значение) -> bool:
    """Есть ли в конверте строка `ScrubbedText` — в значениях, в ключах, на любой глубине.

    Обход итеративный (конверт `raw_get` бывает глубоким, рекурсия упёрлась бы в предел стека) и
    стоит один проход по конверту — дешевле сериализации, которая идёт следом. Проверяются и
    ключи: разбор с пометками ключей не помечает, но сторож держит правило «строка раннего
    прохода не выходит», а не знание о том, как устроен сегодняшний разбор."""
    стек = [значение]
    while стек:
        текущее = стек.pop()
        if isinstance(текущее, str):
            if isinstance(текущее, ScrubbedText):
                return True
        elif isinstance(текущее, dict):
            стек.extend(текущее.keys())
            стек.extend(текущее.values())
        elif isinstance(текущее, list | tuple):
            стек.extend(текущее)
    return False


def guard_only(guard: Guard, envelope: dict) -> str:
    """Сериализация и страж на строжайшем уровне — для ответов, у которых база не определена
    (запрос на неизвестную/недоступную базу): политики и классов полей ещё нет, но известные
    словарю значения всё равно не должны выйти наружу (инвариант 1).

    Набора раскрытого здесь нет и быть не может, и это не упущение: пока база не разрешена (или
    гейт этой базы не построился), обратная подмена не выполнялась ни разу — раскрывать токены
    некому и нечем (задача N1 M1d)."""
    текст = json.dumps(envelope, ensure_ascii=False)
    return guard.check(текст, mode=СТРОЖАЙШИЙ_УРОВЕНЬ).text


class Scrubber:
    """Ранний проход раскрытого (Ruling 25) в двух формах, которые нужны клиенту 1С: вызов — по
    тексту ошибки (`map_error` дальше режет его), `load` — по телу успешного ответа вместе с
    разбором JSON (Ruling 32: разбор помечает переписанные строки их исходным значением). Клиент о
    гейте ничего не знает — только вызывает эти два метода (`client1c.client.Scrub`)."""

    __slots__ = ("_gate", "_revealed")

    def __init__(self, gate: BaseGate, revealed: RevealedValues | None) -> None:
        self._gate = gate
        self._revealed = revealed

    def __call__(self, text: str) -> str:
        return self._gate.scrub_revealed(text, self._revealed)

    def load(self, text: str):
        return self._gate.scrub_revealed_json(text, self._revealed)
