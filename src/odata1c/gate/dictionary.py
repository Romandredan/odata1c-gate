"""Словарь гейта: кэш обратного отображения и источник номеров для названий (SPEC §6.6).

Словарь общий для всех баз: значение, классифицированное в одной базе, заменяется в любой другой
с уровнем выше off (SPEC §6.2).
"""

from __future__ import annotations

import bisect
import datetime
import pathlib
import re
import sqlite3

from odata1c.gate.tokens import make_token, normalize_value

СХЕМА = """
CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    normalized TEXT NOT NULL,
    seq INTEGER,
    first_seen_at TEXT NOT NULL,
    first_base TEXT,
    first_entity TEXT,
    first_field TEXT,
    UNIQUE (type, normalized)
);

CREATE TABLE IF NOT EXISTS variants (
    token TEXT NOT NULL REFERENCES tokens(token) ON DELETE CASCADE,
    base TEXT NOT NULL,
    entity TEXT NOT NULL,
    field TEXT NOT NULL,
    raw_value TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (token, base, field, raw_value)
);

-- Ключ — пара (token, variant_norm), а не один variant_norm: короткий вариант вида "ромашка"
-- законно принадлежит разным организациям (ООО "Ромашка" и АО "Ромашка" — два разных токена).
-- Оба варианта должны быть записаны, а не молчаливо потеряны через INSERT OR IGNORE по одному
-- variant_norm — иначе один общий вариант остаётся закреплён за первым встреченным токеном, и
-- страж заменит им упоминание второй организации. Различение однозначных и неоднозначных
-- вариантов при чтении — в Dictionary.name_variants()/ambiguous_name_variants()
-- (поправка ревью, 2026-09-09).
CREATE TABLE IF NOT EXISTS name_variants (
    token TEXT NOT NULL REFERENCES tokens(token) ON DELETE CASCADE,
    variant_norm TEXT NOT NULL,
    PRIMARY KEY (token, variant_norm)
);

CREATE INDEX IF NOT EXISTS idx_variants_lookup ON variants(token, base, field);
"""

НУМЕРУЕМЫЕ = ("org", "person")
МИНИМАЛЬНАЯ_ДЛИНА_ВАРИАНТА = 4
ФОРМЫ = re.compile(r'^(ооо|оао|зао|пао|ао|ип|нко|ано|фгуп|гуп|муп|тсж|снт)[\s"«]+', re.IGNORECASE)
КАВЫЧКИ = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"'})


def name_variants_of(value: str) -> list[str]:
    """Варианты названия для сканера: как есть и без организационно-правовой формы."""
    очищенное = " ".join(value.translate(КАВЫЧКИ).split())
    варианты = [очищенное.lower()]
    без_формы = ФОРМЫ.sub("", очищенное).strip(' "').strip()
    if без_формы and без_формы.lower() != варианты[0]:
        варианты.append(без_формы.lower())
    return [вариант for вариант in варианты if len(вариант) >= МИНИМАЛЬНАЯ_ДЛИНА_ВАРИАНТА]


_ПРОБЕЛЬНЫЙ_ПОВТОР = re.compile(r"\s+")


class _КартаПозиций:
    """Ленивое отображение индекса нормализованной строки в диапазон `[start, end)` исходной —
    без материализации отдельной записи `(start, end)` на каждый символ текста, как раньше
    (замер ревью 2026-09-09: на ответе в сто тысяч символов список из ста тысяч кортежей и
    столько же вызовов `str.lower()`/`str.isspace()` по одному символу — главная статья времени
    прохода стража, около 95% от общего). Достаточно запомнить точки, где нормализация меняет
    длину строки — схлопнутые повторы пробелов, — а обычный (не схлопнутый) символ пересчитывается
    арифметикой по накопленному сдвигу перед ближайшей предыдущей такой точкой, без явного
    перебора: количество точек равно числу пробельных пропусков в тексте, а не числу символов."""

    __slots__ = ("_позиции", "_начала", "_концы", "_длина")

    def __init__(self, события: list[tuple[int, int, int]], длина: int) -> None:
        # события — по одной записи (норм_позиция, ориг_начало, ориг_конец) на каждый схлопнутый
        # повтор пробелов, по возрастанию норм_позиции (порядок гарантирован построением —
        # события находятся проходом слева направо по regex-совпадениям).
        self._позиции = [норм_позиция for норм_позиция, _, _ in события]
        self._начала = [ориг_начало for _, ориг_начало, _ in события]
        self._концы = [ориг_конец for _, _, ориг_конец in события]
        self._длина = длина

    def __len__(self) -> int:
        return self._длина

    def __getitem__(self, норм_индекс: int) -> tuple[int, int]:
        i = bisect.bisect_right(self._позиции, норм_индекс) - 1
        if i < 0:
            return (норм_индекс, норм_индекс + 1)
        событие_поз = self._позиции[i]
        if норм_индекс == событие_поз:
            return (self._начала[i], self._концы[i])
        сдвиг = self._концы[i] - событие_поз - 1
        return (норм_индекс + сдвиг, норм_индекс + сдвиг + 1)


def normalize_text_with_map(текст: str) -> tuple[str, _КартаПозиций | list[tuple[int, int]]]:
    """Нормализация произвольного текста для поиска автоматом Aho-Corasick по известным
    названиям (SPEC §6.5, слой 3; §6.8 — страж), с картой позиций к исходной строке.

    Ключи автомата (варианты названий, `name_variants_of` выше) нормализованы: кавычки любого
    вида приведены к прямой, пробелы любого вида схлопнуты в один, регистр — к нижнему. Текст
    ответа до правки (ревью 2026-09-09, дыры 1 и 2) сверялся с этими ключами КАК ЕСТЬ — двойной
    пробел, неразрывный пробел (U+00A0) или кавычки-ёлочки внутри названия не совпадали ни с
    одним ключом, хотя написание то же самое; полное `текст.lower()` для регистра само по себе
    другая дыра — символ, у которого нижний регистр разворачивается в несколько знаков (например,
    `İ`, U+0130 → «i» плюс комбинирующая точка), сдвигает длину строки и вместе с ней — индексы
    совпадений автомата относительно исходной строки.

    Возвращает нормализованную строку и объект, отвечающий на `карта[индекс]` парой
    `(начало, конец)` в исходной строке — для каждого индекса нормализованной строки диапазон
    символов исходной, из которых он получен (интерфейс — как у списка кортежей, вызывающему
    коду, guard.py/masking.py, не важно, список это или ленивая карта). Схлопнутый повтор
    пробелов отображается в диапазон, охватывающий весь повтор целиком, а не один из его
    символов, — поэтому совпадение автомата, зацепившее такую позицию, при обратном отображении
    в исходную строку забирает весь повтор, а не часть его.

    Быстрый путь (подавляющее большинство текста: не содержит символов, у которых `str.lower()`
    разворачивается в несколько знаков) строит карту через `_КартаПозиций` — регулярным
    выражением по пробельным повторам (реализация на C), без цикла по символам на Python.
    Медленный путь — посимвольный, как раньше, с откатом к исходному символу там, где нижний
    регистр разворачивается в несколько знаков; используется только когда такой символ в тексте
    действительно есть (проверяется одним сравнением длин, тоже без цикла на Python) — для
    защищаемых в проекте алфавитов (кириллица, латиница ASCII) это не потеря, а компромисс ценой
    того, что такие редкие символы не могут быть частью названия в словаре.
    """
    приведённый = текст.translate(КАВЫЧКИ)
    нижний = приведённый.lower()
    if len(нижний) == len(приведённый):
        события: list[tuple[int, int, int]] = []
        куски: list[str] = []
        позиция = 0
        сдвиг = 0
        for совпадение in _ПРОБЕЛЬНЫЙ_ПОВТОР.finditer(нижний):
            начало, конец = совпадение.span()
            куски.append(нижний[позиция:начало])
            события.append((начало - сдвиг, начало, конец))
            сдвиг += (конец - начало) - 1
            куски.append(" ")
            позиция = конец
        куски.append(нижний[позиция:])
        нормализовано = "".join(куски)
        return нормализовано, _КартаПозиций(события, len(нормализовано))

    нормализовано_части: list[str] = []
    карта_список: list[tuple[int, int]] = []
    n = len(приведённый)
    i = 0
    while i < n:
        символ = приведённый[i]
        if символ.isspace():
            начало = i
            i += 1
            while i < n and приведённый[i].isspace():
                i += 1
            нормализовано_части.append(" ")
            карта_список.append((начало, i))
            continue
        ниж = символ.lower()
        if len(ниж) != 1:
            ниж = символ
        нормализовано_части.append(ниж)
        карта_список.append((i, i + 1))
        i += 1
    return "".join(нормализовано_части), карта_список


class DictionaryCorruptError(Exception):
    """Файл словаря — не SQLite-база или повреждён (тот же набор атрибутов, что у OdataError,
    ConfigError и IndexCorruptError: SPEC §5.2 — code, message, hint; см.
    index/repository.py::IndexCorruptError — тот же дефект и то же решение, перенесённое сюда
    по итогам ревью, 2026-09-09)."""

    def __init__(self, path: pathlib.Path, детали: str) -> None:
        message = f"словарь гейта повреждён или недоступен: {path}"
        super().__init__(message)
        self.code = "dictionary_corrupt"
        self.message = message
        self.hint = (
            f"словарь — единственное место, где хранится связь токена со значением: "
            f"восстановить его нельзя, только начать заново. Переместите повреждённый файл "
            f"{path} в сторону и запустите ещё раз — новый словарь начнёт накапливаться с нуля, "
            f"но токены, выданные до сих пор, перестанут раскрываться ({детали})"
        )


class Dictionary:
    def __init__(self, path: pathlib.Path, secret: bytes) -> None:
        self.path = pathlib.Path(path)
        self._secret = secret
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Управление транзакциями — штатное (isolation_level по умолчанию, без ручного
        # autocommit), как в index/schema.py: connect(). В отключённом режиме
        # (isolation_level=None) `with соединение:` по всему хранилищу выглядит как транзакция,
        # но ею не является — каждая запись фиксируется отдельно и при WAL это обращение к диску
        # на каждую вставку (см. пояснение в schema.py: ~40 мс на запись вместо долей мс).
        # Для словаря это критично вдвойне: он пишется на каждом ответе базы и единственный
        # хранит связь токена со значением — потеря половины записи при сбое посреди неё
        # означает токены, которые невозможно раскрыть. При штатном управлении первая же
        # DML-команда открывает транзакцию неявно, и `with self._connection:` в _создать()
        # и _запомнить_вариант() действительно фиксирует или откатывает её целиком.
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        # Если файл существует, но не является SQLite-базой (или повреждён), sqlite3 узнаёт об
        # этом не на connect(), а только на первой операции — здесь на PRAGMA/executescript.
        # Соединение к этому моменту уже открыто и держит файловый дескриптор; не закрыв его
        # перед тем, как исключение уйдёт наверх, получаем недостижимый, но не закрытый
        # sqlite3.Connection — на сборке мусора ResourceWarning (в тестах — ошибка сессии
        # pytest, см. filterwarnings=["error"]). Решение и формулировка — по образцу
        # index/schema.py::connect() + index/repository.py::IndexRepository.__init__ (тот же
        # дефект уже находили и чинили там; перенесено сюда по итогам ревью, 2026-09-09).
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            # synchronous=NORMAL при журнале WAL — штатная и безопасная комбинация (документация
            # SQLite): повреждения базы она не допускает, checkpoint всё равно синхронизируется
            # полностью. Риск ограничен потерей нескольких последних зафиксированных транзакций
            # при внезапном отказе ОС или питания (не при обычном сбое процесса — commit
            # остаётся atomic). Для словаря этот риск приемлем, а не просто дешевле: токены
            # реквизитов (ИНН, счета, телефоны) вычисляются из значения и секретного ключа, а не
            # читаются из словаря — потерянная запись сама восстановится при следующей встрече
            # того же значения. Токены названий и ФИО при потере получат новые порядковые
            # номера — старые перестанут раскрываться в уже закрытых чатах, но это неудобство
            # истории переписки, а не потеря данных базы. Против этого — цена полной
            # синхронизации на каждом ответе 1С, которую иначе платил бы каждый запрос модели
            # (решение координатора, 2026-09-09).
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.executescript(СХЕМА)
        except sqlite3.DatabaseError as ошибка:
            self._connection.close()
            raise DictionaryCorruptError(self.path, str(ошибка)) from ошибка
        self._revision = self._count()

    def close(self) -> None:
        self._connection.close()

    def revision(self) -> int:
        return self._revision

    def token_for(self, type_: str, raw_value: str, *, base: str, entity: str, field: str) -> str:
        нормализованное = normalize_value(type_, raw_value)
        if not нормализованное:
            return raw_value

        строка = self._connection.execute(
            "SELECT token FROM tokens WHERE type = ? AND normalized = ?",
            (type_, нормализованное),
        ).fetchone()
        новое_значение = строка is None
        # Одна транзакция на всю выдачу токена: запись о токене (_создать) и вариант написания
        # (_запомнить_вариант) фиксируются вместе одним commit'ом, а не двумя раздельными. Вариант
        # написания без самой записи о токене бессмыслен, а половинчатое состояние между двумя
        # отдельными фиксациями — ровно то, от чего уходили штатным управлением транзакциями
        # (см. комментарий в __init__): при сбое посреди пары commit'ов возможен был токен без
        # варианта или (при повторном INSERT) конфликт уникальности. При исключении внутри блока
        # `with` откатывается вся пара разом. Побочный эффект — тот же прирост скорости, что и
        # у корректности: один fsync на новое значение вместо двух (решение координатора,
        # 2026-09-09, по итогам замера в задаче 4).
        with self._connection:
            токен = (
                строка["token"]
                if строка
                else self._создать(type_, нормализованное, base, entity, field)
            )
            self._запомнить_вариант(токен, base, entity, field, raw_value)
        if новое_значение:
            self._revision += 1
        return токен

    def reveal(
        self, token: str, *, base: str | None = None, field: str | None = None
    ) -> str | None:
        строка = self._connection.execute(
            "SELECT normalized FROM tokens WHERE token = ?", (token,)
        ).fetchone()
        if строка is None:
            return None
        if base and field:
            вариант = self._connection.execute(
                "SELECT raw_value FROM variants WHERE token = ? AND base = ? AND field = ?"
                " ORDER BY seen_at DESC LIMIT 1",
                (token, base, field),
            ).fetchone()
            if вариант:
                return вариант["raw_value"]
        return строка["normalized"]

    def number_tokens(self) -> dict[str, str]:
        """Цифровые значения словаря и их токены: с этим множеством сверяется страж (SPEC §6.8)."""
        return {
            строка["normalized"]: строка["token"]
            for строка in self._connection.execute(
                "SELECT normalized, token FROM tokens WHERE normalized GLOB '[0-9]*'"
            ).fetchall()
            if строка["normalized"].isdigit()
        }

    def name_variants(self) -> dict[str, str]:
        """Однозначные варианты названий: с этим множеством сверяется страж (SPEC §6.5, слой 3).

        Вариант, закреплённый более чем за одним токеном (короткая форма совпала у разных
        организаций/физлиц — например, «Ромашка» у ООО «Ромашка» и АО «Ромашка»), сюда не
        попадает. Заменить такой вариант правильно невозможно — неизвестно, к какому из токенов
        он относится в конкретном тексте, — а неправильная подмена подставит одно юридическое
        лицо вместо другого, что хуже отсутствия подмены. Полное название каждой организации
        (со своей организационно-правовой формой) при этом по-прежнему однозначно и остаётся
        здесь — коллизия задевает только общую сокращённую часть (поправка ревью, 2026-09-09).
        """
        return {
            строка["variant_norm"]: строка["token"]
            for строка in self._connection.execute(
                "SELECT variant_norm, token FROM name_variants"
                " WHERE variant_norm IN ("
                "   SELECT variant_norm FROM name_variants"
                "   GROUP BY variant_norm HAVING COUNT(*) = 1"
                " )"
            ).fetchall()
        }

    def ambiguous_name_variants(self) -> dict[str, list[str]]:
        """Варианты названий, закреплённые более чем за одним токеном (см. name_variants()).

        Не участвуют в подмене — нужны, чтобы предупредить пользователя: короткая форма
        встретилась у нескольких организаций/физлиц, и страж сознательно не подменяет её ни
        одним из токенов (поправка ревью, 2026-09-09).
        """
        результат: dict[str, list[str]] = {}
        for строка in self._connection.execute(
            "SELECT variant_norm, token FROM name_variants"
            " WHERE variant_norm IN ("
            "   SELECT variant_norm FROM name_variants GROUP BY variant_norm HAVING COUNT(*) > 1"
            " )"
            " ORDER BY variant_norm, token"
        ).fetchall():
            результат.setdefault(строка["variant_norm"], []).append(строка["token"])
        return результат

    def _создать(self, type_: str, нормализованное: str, base: str, entity: str, field: str) -> str:
        """Вставки без собственной транзакции — вызывающий (token_for) держит одну на всё."""
        момент = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        if type_ in НУМЕРУЕМЫЕ:
            номер = self._следующий_номер(type_)
            токен = f"[[{type_}:{номер}]]"
        else:
            номер = None
            токен = self._свободный_токен(type_, нормализованное)

        self._connection.execute(
            "INSERT INTO tokens (token, type, normalized, seq, first_seen_at, first_base,"
            " first_entity, first_field) VALUES (?,?,?,?,?,?,?,?)",
            (токен, type_, нормализованное, номер, момент, base, entity, field),
        )
        if type_ in НУМЕРУЕМЫЕ:
            self._connection.executemany(
                "INSERT OR IGNORE INTO name_variants (token, variant_norm) VALUES (?,?)",
                [(токен, вариант) for вариант in name_variants_of(нормализованное)],
            )
        return токен

    def _свободный_токен(self, type_: str, нормализованное: str) -> str:
        """Коллизия — другое значение с тем же токеном: хвост удлиняется до 16 (SPEC §6.3)."""
        for длина in (10, 16):
            кандидат = make_token(self._secret, type_, нормализованное, tail_length=длина)
            занято = self._connection.execute(
                "SELECT normalized FROM tokens WHERE token = ?", (кандидат,)
            ).fetchone()
            if занято is None or занято["normalized"] == нормализованное:
                return кандидат
        raise RuntimeError(f"не удалось выдать токен класса {type_}: коллизия и на 16 символах")

    def _следующий_номер(self, type_: str) -> int:
        строка = self._connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS следующий FROM tokens WHERE type = ?", (type_,)
        ).fetchone()
        return int(строка["следующий"])

    def _запомнить_вариант(
        self, токен: str, base: str, entity: str, field: str, raw_value: str
    ) -> None:
        """Вставка без собственной транзакции — вызывающий (token_for) держит одну на всё."""
        self._connection.execute(
            "INSERT OR IGNORE INTO variants (token, base, entity, field, raw_value, seen_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                токен,
                base,
                entity,
                field,
                raw_value,
                datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            ),
        )

    def _count(self) -> int:
        return int(
            self._connection.execute("SELECT COUNT(*) AS всего FROM tokens").fetchone()["всего"]
        )
