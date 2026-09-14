"""Чтение и запись индекса метаданных (SPEC §4.2–§4.4)."""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import sqlite3
from collections.abc import Iterable

from odata1c.index.edmx import PARSER_VERSION, ParsedMetadata
from odata1c.index.naming import normalize, stems
from odata1c.index.schema import connect

ПРЕДПРОСМОТР_ПОЛЕЙ = 12

# SPEC §4.4, ветка 3 (вхождение сжатого запроса в сжатое имя): запросы короче порога дают
# слишком много случайных совпадений на реальных именах (однобуквенный запрос "к" — почти
# в любом имени сущности; см. тест на короткий запрос) — для них остаются только точное
# совпадение и совпадение по основам слов.
МИНИМАЛЬНАЯ_ДЛИНА_ПОДСТРОКИ = 3

# SPEC §4.4, ветка 4 (триграммное сходство): доля общих триграмм ниже порога — случайное
# пересечение, а не осмысленный кандидат. Порог 0.2 подобран по образцу synthetic.edmx: он
# отсекает совпадение только по общему префиксу вида объекта (0.161 — «Catalog_Контрагенты»
# против «Catalog_БанковскиеСчета» по запросу полного имени первой) и случайные пересечения
# по несвязанным словам (0.024), но пропускает опечатку в одну букву у сущностей сравнимой
# длины (0.238–0.567 в образце) — см. тесты на пороге.
ПОРОГ_ТРИГРАММ = 0.2


@dataclasses.dataclass(slots=True)
class FoundEntity:
    name: str
    russian_kind: str
    key_fields: list[str]
    field_preview: list[str]
    score: float


@dataclasses.dataclass(slots=True)
class EntityDescription:
    name: str
    kind: str
    russian_kind: str
    parent_entity: str | None
    is_tabular_part: bool
    is_records: bool
    is_virtual: bool
    virtual_kind: str | None
    key_fields: list[str]
    description_field: str | None
    fields: list[dict]
    children: list[str]
    actions: list[dict]
    members: list[str]
    navigations: dict[str, str]
    is_independent_register: bool


class IndexCorruptError(Exception):
    """Файл индекса — не SQLite-база, повреждён, или построен прежней версией разбора (тот же
    код/атрибуты, что у OdataError и ConfigError: SPEC §5.2 — code, message, hint).

    `message`/`hint` — необязательные переопределения (раунд правок 1, задача 3 плана M1b-fix):
    `require_current_version()` ниже поднимает эту же ошибку с собственным текстом, не подходящим
    под «файл — не SQLite-база или повреждён». Вызовы без них (файл действительно повреждён)
    не меняются — подсказка по-прежнему про `reindex`.
    """

    def __init__(
        self,
        path: pathlib.Path,
        детали: str,
        *,
        message: str | None = None,
        hint: str | None = None,
    ) -> None:
        итоговое_сообщение = message or f"индекс метаданных повреждён или недоступен: {path}"
        super().__init__(итоговое_сообщение)
        self.code = "index_corrupt"
        self.message = итоговое_сообщение
        self.hint = hint or f"обновите индекс командой odata1c reindex <база> ({детали})"


class IndexRepository:
    def __init__(self, path: pathlib.Path, *, read_only: bool = False) -> None:
        """`read_only=True` (ревью раунда 1 задачи 2 M3, находка M-2/Ruling 75): открыть файл БЕЗ
        побочных записей — обычный `index.schema.connect` не только читает, а заводит `PRAGMA
        journal_mode=WAL` и выполняет `CREATE TABLE/INDEX IF NOT EXISTS` из `SCHEMA_SQL` — на
        индексе прежней версии разбора это молча дописывает недостающие таблицы/колонки в файл
        ВЛАДЕЛЬЦА. Диагностические пути (`odata1c doctor`), которым нужно только УЗНАТЬ состояние
        индекса, а не работать с ним, открывают файл через `_open_read_only` — см. её докстринг."""
        self.path = pathlib.Path(path)
        try:
            if read_only:
                self._connection = self._open_read_only(self.path)
            else:
                self._connection = connect(self.path)
        except sqlite3.DatabaseError as ошибка:
            raise IndexCorruptError(self.path, str(ошибка)) from ошибка

    @staticmethod
    def _open_read_only(path: pathlib.Path) -> sqlite3.Connection:
        """Соединение только на чтение, без побочных файлов.

        `mode=ro` в одиночку НЕ достаточен (проверено исполнением): файл в WAL-режиме (а он
        всегда в WAL — `index.schema.connect` включает его при создании) заводит `-wal`/`-shm`
        рядом с собой при ЛЮБОМ подключении, читающем через механизм WAL, даже без единой записи
        в саму базу — это тоже файлы на диске владельца, которых доктор оставлять не должен.
        `immutable=1` — штатный режим SQLite «файл не изменится, пока соединение открыто»: он
        выключает WAL-машинерию совсем и читает напрямую из основного файла. Соединение здесь
        живёт одно короткое чтение — риск (конкурентный reindex ровно в этот миг) даёт не худший
        исход, чем WAL: не свежий, а не изменённый на диске файл, и следующий вызов doctor
        увидит уже актуальное."""
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            # sqlite3.connect() ленив — файл не трогается, пока не выполнен первый оператор
            # (проверено исполнением: на мусорном файле голый connect() в режиме mode=ro не
            # бросает ничего). Явный зонд здесь — по тому же поводу, что и `PRAGMA
            # journal_mode=WAL` в ветке записи (`index.schema.connect`): повреждённый или
            # не-SQLite файл должен проявить себя сразу, а не позже, из какого-нибудь SELECT
            # внутри meta()/resolve_name(), куда конструктор уже не дотянется. `PRAGMA
            # schema_version` существует в любой настоящей базе SQLite (даже пустой) и ничего
            # не пишет.
            connection.execute("PRAGMA schema_version").fetchone()
        except sqlite3.DatabaseError:
            # Находка ревью раунда 1 (регресс своей же правки, найден прогоном): без явного
            # close() здесь соединение, уже открытое `sqlite3.connect()` выше, утекает —
            # `raise` в __init__ поднимает IndexCorruptError, но объект Connection остаётся
            # недостижимым и не закрытым, и sqlite3 на сборке мусора выдаёт ResourceWarning
            # (в тестах — падение сессии pytest). Тот же приём, что уже есть в
            # `index.schema.connect` для ветки записи.
            connection.close()
            raise
        return connection

    def close(self) -> None:
        self._connection.close()

    def require_current_version(self) -> None:
        """Понятная ошибка проекта вместо сырого `sqlite3.OperationalError`, если файл индекса
        построен прежней версией разбора (раунд правок 1, задача 3 плана M1b-fix, Important):
        без этой проверки `describe()`/`find()` на индексе без `is_records`,
        `actions.side_effecting`, таблиц `enums`/`navigations` падают необработанным
        `no such column`/`no such table` — слой MCP-тулов M1d читает индекс напрямую и получил бы
        внутреннюю ошибку вместо диагностики.

        Не вызывается из `__init__`: временный пустой файл при построении индекса и старый файл,
        который читает `reindex._прежнее_состояние`, не должны падать здесь — оба открываются до
        того, как в них вообще появляется `parser_version` текущей версии. Вызывают этот метод
        только читающие пути (слой тулов), которым нужны актуальные колонки/таблицы.
        """
        try:
            версия = self.meta("parser_version")
        except sqlite3.OperationalError:
            # Совсем старый файл — до появления самой таблицы meta или её колонок; тот же
            # диагноз, что и явное несовпадение версии.
            версия = None
        if версия != PARSER_VERSION:
            raise IndexCorruptError(
                self.path,
                "версия разбора устарела",
                message=f"индекс метаданных построен прежней версией разбора: {self.path}",
                hint="обновите индекс: odata1c reindex <база> или тул odata1c_reindex",
            )

    def write(self, parsed: ParsedMetadata) -> None:
        момент = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        действия_по_сущности: dict[str, list] = {}
        for действие in parsed.actions:
            действия_по_сущности.setdefault(действие.entity, []).append(действие)

        with self._connection:
            self._connection.execute("DELETE FROM entities")
            self._connection.execute("DELETE FROM entities_fts")
            self._connection.execute("DELETE FROM enums")
            self._connection.execute("DELETE FROM navigations")
            for сущность in parsed.entities:
                имя_норм = normalize(сущность.name)
                основы_имени = " ".join(stems(сущность.base_name))
                курсор = self._connection.execute(
                    "INSERT INTO entities (name, kind, russian_kind, base_name, parent_entity,"
                    " is_tabular_part, is_records, is_virtual, virtual_kind, key_fields_json,"
                    " description_field, has_posted, has_recorder, is_independent_register,"
                    " norm_name, stems, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        сущность.name,
                        сущность.kind,
                        сущность.russian_kind,
                        сущность.base_name,
                        сущность.parent_entity,
                        int(сущность.is_tabular_part),
                        int(сущность.is_records),
                        int(сущность.is_virtual),
                        сущность.virtual_kind,
                        json.dumps(сущность.key_fields, ensure_ascii=False),
                        сущность.description_field,
                        int(сущность.has_posted),
                        int(сущность.has_recorder),
                        int(сущность.is_independent_register),
                        имя_норм,
                        основы_имени,
                        момент,
                    ),
                )
                entity_id = курсор.lastrowid
                self._connection.executemany(
                    "INSERT INTO fields (entity_id, name, edm_type, nullable, is_key, is_ref,"
                    " ref_targets_json, is_composite) VALUES (?,?,?,?,?,?,?,?)",
                    [
                        (
                            entity_id,
                            поле.name,
                            поле.edm_type,
                            int(поле.nullable),
                            int(поле.is_key),
                            int(поле.is_ref),
                            json.dumps(поле.ref_targets, ensure_ascii=False),
                            int(поле.is_composite),
                        )
                        for поле in сущность.fields
                    ],
                )
                self._connection.executemany(
                    "INSERT INTO actions (entity_id, name, params_json, http_method, returns,"
                    " side_effecting) VALUES (?,?,?,?,?,?)",
                    [
                        (
                            entity_id,
                            действие.name,
                            json.dumps(действие.params, ensure_ascii=False),
                            действие.http_method,
                            действие.returns,
                            int(действие.side_effecting),
                        )
                        for действие in действия_по_сущности.get(сущность.name, [])
                    ],
                )
                self._connection.executemany(
                    "INSERT INTO navigations (entity_id, name, target) VALUES (?,?,?)",
                    [
                        (entity_id, имя_навигации, цель)
                        for имя_навигации, цель in сущность.navigations.items()
                    ],
                )
                if сущность.kind == "Enum":
                    self._connection.execute(
                        "INSERT INTO enums (entity_id, members_json) VALUES (?,?)",
                        (
                            entity_id,
                            json.dumps(
                                parsed.enums.get(сущность.base_name, []), ensure_ascii=False
                            ),
                        ),
                    )
                self._connection.execute(
                    "INSERT INTO entities_fts (name, norm_name, stems) VALUES (?,?,?)",
                    (сущность.name, имя_норм, основы_имени),
                )
            self._set_meta("edmx_sha256", parsed.edmx_sha256)
            self._set_meta("indexed_at", момент)
            self._set_meta("entity_count", str(len(parsed.entities)))
            self._set_meta("parser_version", PARSER_VERSION)
            if parsed.platform_hint:
                self._set_meta("platform_hint", parsed.platform_hint)
            if parsed.unresolved_entity_sets:
                # Наборы данных с испорченной ссылкой на тип (см. edmx.py). Ключ читает
                # следующая задача (реиндекс), чтобы сообщить о них, а не пропустить молча.
                self._set_meta(
                    "unresolved_entity_sets",
                    json.dumps(parsed.unresolved_entity_sets, ensure_ascii=False),
                )
            else:
                self._connection.execute(
                    "DELETE FROM meta WHERE key = ?", ("unresolved_entity_sets",)
                )

    def find(self, query: str, kind: str | None = None, limit: int = 10) -> list[FoundEntity]:
        """Ранжирование SPEC §4.4: точное совпадение → основы слов → вхождение подстроки →
        триграммы.

        Правка по итогам ревью задачи 5 (Important): раньше запрос выбирал все строки основной
        таблицы без учёта `kind` и заново пересчитывал `normalize()`/`stems()` по имени каждой
        сущности — притом что write() уже сохраняет оба значения (столбцы `norm_name`, `stems`
        таблицы `entities`, тот же расчёт продублирован в `entities_fts`). На 30 000 сущностей
        это давало 1.7 с на запрос. Теперь фильтр по виду уходит в SQL (использует
        `idx_entities_kind`), а норма и основы имени читаются готовыми — пересчитывается только
        сам запрос (один раз за вызов). Ранжирование по-прежнему в Python — на перенос в FTS5
        MATCH это решение не распространяется (SPEC §4.4 не описывает MATCH-ранжирование).
        """
        нормализованный = normalize(query)
        основы = " ".join(stems(query))
        sql = "SELECT id, name, russian_kind, key_fields_json, norm_name, stems FROM entities"
        параметры: tuple = ()
        if kind:
            sql += " WHERE kind = ?"
            параметры = (kind,)
        строки = self._connection.execute(sql, параметры).fetchall()

        результаты: list[FoundEntity] = []
        for строка in строки:
            оценка = self._оценить(нормализованный, основы, строка["norm_name"], строка["stems"])
            if оценка <= 0:
                continue
            результаты.append(
                FoundEntity(
                    name=строка["name"],
                    russian_kind=строка["russian_kind"],
                    key_fields=json.loads(строка["key_fields_json"]),
                    field_preview=self._предпросмотр_полей(строка["id"]),
                    score=оценка,
                )
            )
        результаты.sort(key=lambda найдено: (-найдено.score, найдено.name))
        return результаты[:limit]

    def side_effecting_actions(self) -> frozenset[str]:
        """Имена действий, которые `$metadata` базы помечает изменяющими данные (`IsSideEffecting`),
        по всем сущностям, без учёта регистра. Нужно `raw_get`: 1С выполняет такие действия и по
        запросу GET (проба P8), а у конфигурации могут быть свои действия сверх Post/Unpost."""
        return frozenset(
            строка[0].casefold()
            for строка in self._connection.execute(
                "SELECT DISTINCT name FROM actions WHERE side_effecting = 1"
            )
        )

    def describe(self, entity: str) -> EntityDescription | None:
        строка = self._connection.execute(
            "SELECT * FROM entities WHERE name = ?", (entity,)
        ).fetchone()
        if строка is None:
            return None
        поля = [
            dict(поле)
            for поле in self._connection.execute(
                "SELECT name, edm_type, nullable, is_key, is_ref, ref_targets_json, is_composite,"
                " sensitivity, sensitivity_source FROM fields WHERE entity_id = ?"
                " ORDER BY is_key DESC, name",
                (строка["id"],),
            ).fetchall()
        ]
        for поле in поля:
            поле["ref_targets"] = json.loads(поле.pop("ref_targets_json"))
        дети = [
            ребёнок["name"]
            for ребёнок in self._connection.execute(
                "SELECT name FROM entities WHERE parent_entity = ? ORDER BY name", (entity,)
            ).fetchall()
        ]
        действия = [
            dict(действие)
            for действие in self._connection.execute(
                "SELECT name, params_json, http_method, returns, side_effecting FROM actions"
                " WHERE entity_id = ?",
                (строка["id"],),
            ).fetchall()
        ]
        for действие in действия:
            действие["params"] = json.loads(действие.pop("params_json"))
            действие["side_effecting"] = bool(действие["side_effecting"])
        строка_перечисления = self._connection.execute(
            "SELECT members_json FROM enums WHERE entity_id = ?", (строка["id"],)
        ).fetchone()
        участники = json.loads(строка_перечисления["members_json"]) if строка_перечисления else []
        навигации = {
            строка_навигации["name"]: строка_навигации["target"]
            for строка_навигации in self._connection.execute(
                "SELECT name, target FROM navigations WHERE entity_id = ?", (строка["id"],)
            ).fetchall()
        }
        return EntityDescription(
            name=строка["name"],
            kind=строка["kind"],
            russian_kind=строка["russian_kind"],
            parent_entity=строка["parent_entity"],
            is_tabular_part=bool(строка["is_tabular_part"]),
            is_records=bool(строка["is_records"]),
            is_virtual=bool(строка["is_virtual"]),
            virtual_kind=строка["virtual_kind"],
            key_fields=json.loads(строка["key_fields_json"]),
            description_field=строка["description_field"],
            fields=поля,
            children=дети,
            actions=действия,
            members=участники,
            navigations=навигации,
            is_independent_register=bool(строка["is_independent_register"]),
        )

    def descendants(self, names: Iterable[str]) -> set[str]:
        """Всё поддерево дочерних сущностей под заданными именами: табличные части, наборы
        записей, виртуальные таблицы — и их собственные дети, до неподвижной точки.

        Обход именно транзитивный, а не в одно поколение (Ruling 30, итоговое ревью M1d, раунд 3):
        у среза регистра сведений родителем стоит не сам регистр, а его набор записей
        (`InformationRegister_X` → `…_RecordType` → `…_SliceLast`; проверено на настоящем
        `$metadata` УТ и на образце `tests/fixtures/edmx/ut-real.edmx`). Одного поколения хватило
        бы, чтобы закрыть набор записей и оставить открытым срез — те же данные под другим именем.

        Сами `names` в результат не входят: это вопрос «кто под ними», а не «они и всё под ними».
        Ответ по `parent_entity` — тому же столбцу, из которого `describe` собирает `children`.
        """
        найденное: set[str] = set()
        слой = {имя for имя in names if имя}
        while слой:
            вопросы = ",".join("?" * len(слой))
            следующий = {
                строка["name"]
                for строка in self._connection.execute(
                    f"SELECT name FROM entities WHERE parent_entity IN ({вопросы})",
                    tuple(слой),
                ).fetchall()
            }
            # Вычитание найденного — защита от цикла в данных (родитель-потомок кольцом): индекс
            # такого строить не должен, но цикл здесь означал бы вечный цикл в демоне.
            слой = следующий - найденное
            найденное |= следующий
        return найденное

    def entity_names(self) -> set[str]:
        return {
            строка["name"]
            for строка in self._connection.execute("SELECT name FROM entities").fetchall()
        }

    def resolve_name(self, entity: str) -> str | None:
        """Каноническое имя сущности индекса по имени, набранному в другом регистре, — или `None`,
        если такой сущности в индексе нет вовсе.

        Нужно `raw_get` (форма (д) ревью 2026-09-11): имя набора в чужом регистре
        (`catalog_Контрагенты`) не находится точным сравнением, а публикация 1С такой путь, по
        всей видимости, принимает. Последствие не только в маске (её закрывает строгий режим), но
        и в `entities.hide`: запрет, выписанный на `Catalog_Контрагенты`, обходился бы одной
        сменой регистра буквы. Приводим имя к каноническому ДО проверки скрытости.

        Сначала точное сравнение по индексу (обычный путь, одна выборка по первичному ключу);
        перебор имён — только при промахе, то есть на уже медленной ветке «сущность неизвестна».
        `str.lower()` вместо `COLLATE NOCASE`: SQLite сворачивает регистр только у латиницы, а
        имена наборов 1С наполовину кириллические.
        """
        строка = self._connection.execute(
            "SELECT name FROM entities WHERE name = ?", (entity,)
        ).fetchone()
        if строка:
            return строка["name"]
        искомое = entity.lower()
        for имя in self.entity_names():
            if имя.lower() == искомое:
                return имя
        return None

    def kind_counts(self) -> dict[str, int]:
        """Сколько сущностей каждого вида — сводка индекса для ресурса `odata1c://index/{base}`
        (план M1d, задача 7). Вид — русское название (`справочник`, `документ`, …), чтобы
        сводка читалась без обратного перевода префиксов имён; порядок — по убыванию числа,
        при равенстве по имени вида, чтобы ответ не плясал между вызовами.

        Табличные части, наборы записей регистров и виртуальные таблицы считаются наравне с
        остальными: это тоже адресуемые сущности индекса, и `entity_count` в `meta` считает их
        так же — расхождение суммы этой сводки с `entity_count` было бы непонятно читателю.
        """
        return {
            строка["russian_kind"]: строка["число"]
            for строка in self._connection.execute(
                "SELECT russian_kind, COUNT(*) AS число FROM entities"
                " GROUP BY russian_kind ORDER BY число DESC, russian_kind"
            ).fetchall()
        }

    def field_sensitivities(self) -> dict[tuple[str, str], str | None]:
        """(сущность, поле) → проставленный класс защиты (или None, если не проставлен).

        Читает прежнее состояние перед перестройкой индекса (SPEC §4.3 п. 4, задача 5:
        «новые поля под защитой» — именно новые, а не все на каждый прогон).
        """
        return {
            (строка["entity"], строка["field"]): строка["sensitivity"]
            for строка in self._connection.execute(
                "SELECT e.name AS entity, f.name AS field, f.sensitivity AS sensitivity"
                " FROM fields f JOIN entities e ON e.id = f.entity_id"
            ).fetchall()
        }

    def field_names(self, entity: str) -> set[str]:
        return {
            строка["name"]
            for строка in self._connection.execute(
                "SELECT f.name FROM fields f JOIN entities e ON e.id = f.entity_id"
                " WHERE e.name = ?",
                (entity,),
            ).fetchall()
        }

    def set_field_sensitivity(self, entity: str, field: str, value: str, source: str) -> bool:
        """Проставить класс защиты поля. Возвращает True, если строка действительно обновлена.

        Правка по итогам ревью задачи 5 (Important): промах условия (опечатка в правиле,
        рассинхронизация имён после обновления индекса) раньше не давал ни ошибки, ни признака —
        притом что это точка подключения гейта: непроставленный класс означает, что поле уйдёт
        модели в открытом виде, а «реальные значения защищаемых классов не выходят никогда» —
        инвариант продукта (AGENTS.md), а не рекомендация. Вызывающий обязан проверить результат.
        """
        with self._connection:
            курсор = self._connection.execute(
                "UPDATE fields SET sensitivity = ?, sensitivity_source = ?"
                " WHERE name = ? AND entity_id = (SELECT id FROM entities WHERE name = ?)",
                (value, source, field, entity),
            )
            return курсор.rowcount > 0

    def meta(self, key: str) -> str | None:
        строка = self._connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return строка["value"] if строка else None

    def _set_meta(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT INTO meta (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _предпросмотр_полей(self, entity_id: int) -> list[str]:
        return [
            строка["name"]
            for строка in self._connection.execute(
                "SELECT name FROM fields WHERE entity_id = ? ORDER BY is_key DESC, name LIMIT ?",
                (entity_id, ПРЕДПРОСМОТР_ПОЛЕЙ),
            ).fetchall()
        ]

    @staticmethod
    def _оценить(запрос_норм: str, запрос_основы: str, имя_норм: str, имя_основы: str) -> float:
        имя_без_вида = имя_норм.split(" ", 1)[-1]
        if запрос_норм in (имя_норм, имя_без_вида):
            return 100.0
        совпало_основ = len(set(запрос_основы.split()) & set(имя_основы.split()))
        if совпало_основ:
            return 50.0 + совпало_основ
        сжатый_запрос = запрос_норм.replace(" ", "")
        сжатое_имя = имя_норм.replace(" ", "")
        if len(сжатый_запрос) >= МИНИМАЛЬНАЯ_ДЛИНА_ПОДСТРОКИ and сжатый_запрос in сжатое_имя:
            return 30.0 + len(сжатый_запрос) / max(len(сжатое_имя), 1)
        сходство = _триграммы(сжатый_запрос, сжатое_имя)
        if сходство < ПОРОГ_ТРИГРАММ:
            return 0.0
        return сходство * 20.0


def _триграммы(левое: str, правое: str) -> float:
    if len(левое) < 3 or len(правое) < 3:
        return 0.0
    набор_левых = {левое[i : i + 3] for i in range(len(левое) - 2)}
    набор_правых = {правое[i : i + 3] for i in range(len(правое) - 2)}
    пересечение = набор_левых & набор_правых
    if not пересечение:
        return 0.0
    return len(пересечение) / len(набор_левых | набор_правых)
