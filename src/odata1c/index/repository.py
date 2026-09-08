"""Чтение и запись индекса метаданных (SPEC §4.2–§4.4)."""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import sqlite3

from odata1c.index.edmx import ParsedMetadata
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
    russian_kind: str
    key_fields: list[str]
    description_field: str | None
    fields: list[dict]
    children: list[str]
    actions: list[dict]
    is_independent_register: bool


class IndexCorruptError(Exception):
    """Файл индекса — не SQLite-база или повреждён (тот же код/атрибуты, что у OdataError и
    ConfigError: SPEC §5.2 — code, message, hint)."""

    def __init__(self, path: pathlib.Path, детали: str) -> None:
        message = f"индекс метаданных повреждён или недоступен: {path}"
        super().__init__(message)
        self.code = "index_corrupt"
        self.message = message
        self.hint = f"обновите индекс командой odata1c reindex <база> ({детали})"


class IndexRepository:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        try:
            self._connection = connect(self.path)
        except sqlite3.DatabaseError as ошибка:
            raise IndexCorruptError(self.path, str(ошибка)) from ошибка

    def close(self) -> None:
        self._connection.close()

    def write(self, parsed: ParsedMetadata) -> None:
        момент = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        действия_по_сущности: dict[str, list] = {}
        for действие in parsed.actions:
            действия_по_сущности.setdefault(действие.entity, []).append(действие)

        with self._connection:
            self._connection.execute("DELETE FROM entities")
            self._connection.execute("DELETE FROM entities_fts")
            for сущность in parsed.entities:
                курсор = self._connection.execute(
                    "INSERT INTO entities (name, kind, russian_kind, base_name, parent_entity,"
                    " is_tabular_part, is_virtual, virtual_kind, key_fields_json,"
                    " description_field, has_posted, has_recorder, is_independent_register,"
                    " indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        сущность.name,
                        сущность.kind,
                        сущность.russian_kind,
                        сущность.base_name,
                        сущность.parent_entity,
                        int(сущность.is_tabular_part),
                        int(сущность.is_virtual),
                        сущность.virtual_kind,
                        json.dumps(сущность.key_fields, ensure_ascii=False),
                        сущность.description_field,
                        int(сущность.has_posted),
                        int(сущность.has_recorder),
                        int(сущность.is_independent_register),
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
                    "INSERT INTO actions (entity_id, name, params_json, http_method, returns)"
                    " VALUES (?,?,?,?,?)",
                    [
                        (
                            entity_id,
                            действие.name,
                            json.dumps(действие.params, ensure_ascii=False),
                            действие.http_method,
                            действие.returns,
                        )
                        for действие in действия_по_сущности.get(сущность.name, [])
                    ],
                )
                self._connection.execute(
                    "INSERT INTO entities_fts (name, norm_name, stems) VALUES (?,?,?)",
                    (сущность.name, normalize(сущность.name), " ".join(stems(сущность.base_name))),
                )
            self._set_meta("edmx_sha256", parsed.edmx_sha256)
            self._set_meta("indexed_at", момент)
            self._set_meta("entity_count", str(len(parsed.entities)))
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
        """Ранжирование SPEC §4.4: точное совпадение → основы слов → триграммы."""
        нормализованный = normalize(query)
        основы = " ".join(stems(query))
        строки = self._connection.execute(
            "SELECT id, name, russian_kind, key_fields_json, base_name, kind FROM entities"
        ).fetchall()

        результаты: list[FoundEntity] = []
        for строка in строки:
            if kind and строка["kind"] != kind:
                continue
            имя_норм = normalize(строка["name"])
            имя_основы = " ".join(stems(строка["base_name"]))
            оценка = self._оценить(нормализованный, основы, имя_норм, имя_основы)
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

    def describe(self, entity: str) -> EntityDescription | None:
        строка = self._connection.execute(
            "SELECT * FROM entities WHERE name = ?", (entity,)
        ).fetchone()
        if строка is None:
            return None
        поля = [
            dict(поле)
            for поле in self._connection.execute(
                "SELECT name, edm_type, nullable, is_key, is_ref, is_composite, sensitivity,"
                " sensitivity_source FROM fields WHERE entity_id = ? ORDER BY is_key DESC, name",
                (строка["id"],),
            ).fetchall()
        ]
        дети = [
            ребёнок["name"]
            for ребёнок in self._connection.execute(
                "SELECT name FROM entities WHERE parent_entity = ? ORDER BY name", (entity,)
            ).fetchall()
        ]
        действия = [
            dict(действие)
            for действие in self._connection.execute(
                "SELECT name, params_json, http_method, returns FROM actions WHERE entity_id = ?",
                (строка["id"],),
            ).fetchall()
        ]
        for действие in действия:
            действие["params"] = json.loads(действие.pop("params_json"))
        return EntityDescription(
            name=строка["name"],
            russian_kind=строка["russian_kind"],
            key_fields=json.loads(строка["key_fields_json"]),
            description_field=строка["description_field"],
            fields=поля,
            children=дети,
            actions=действия,
            is_independent_register=bool(строка["is_independent_register"]),
        )

    def entity_names(self) -> set[str]:
        return {
            строка["name"]
            for строка in self._connection.execute("SELECT name FROM entities").fetchall()
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

    def set_field_sensitivity(self, entity: str, field: str, value: str, source: str) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE fields SET sensitivity = ?, sensitivity_source = ?"
                " WHERE name = ? AND entity_id = (SELECT id FROM entities WHERE name = ?)",
                (value, source, field, entity),
            )

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
