"""Схема metadata.sqlite (SPEC §4.2)."""

from __future__ import annotations

import pathlib
import sqlite3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    russian_kind TEXT NOT NULL,
    base_name TEXT NOT NULL,
    parent_entity TEXT,
    is_tabular_part INTEGER NOT NULL DEFAULT 0,
    is_virtual INTEGER NOT NULL DEFAULT 0,
    virtual_kind TEXT,
    key_fields_json TEXT NOT NULL,
    description_field TEXT,
    has_posted INTEGER NOT NULL DEFAULT 0,
    has_recorder INTEGER NOT NULL DEFAULT 0,
    is_independent_register INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fields (
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    edm_type TEXT NOT NULL,
    nullable INTEGER NOT NULL DEFAULT 1,
    is_key INTEGER NOT NULL DEFAULT 0,
    is_ref INTEGER NOT NULL DEFAULT 0,
    ref_targets_json TEXT NOT NULL DEFAULT '[]',
    is_composite INTEGER NOT NULL DEFAULT 0,
    sensitivity TEXT,
    sensitivity_source TEXT,
    PRIMARY KEY (entity_id, name)
);

CREATE TABLE IF NOT EXISTS actions (
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    http_method TEXT NOT NULL DEFAULT 'POST',
    returns TEXT,
    PRIMARY KEY (entity_id, name)
);

CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts
    USING fts5(name, norm_name, stems, tokenize='trigram');

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);
CREATE INDEX IF NOT EXISTS idx_entities_parent ON entities(parent_entity);
"""


def connect(path: pathlib.Path) -> sqlite3.Connection:
    """Соединение в режиме WAL: читающие сессии не блокируют пишущую (SPEC §2.2).

    Если файл повреждён (не SQLite-база), sqlite3 узнаёт об этом не на sqlite3.connect(), а
    только на первой операции — здесь на PRAGMA. В этом случае соединение уже открыто и держит
    файловый дескриптор; не закрыв его перед тем, как передать исключение выше, оставляем
    объект sqlite3.Connection недостижимым, но не закрытым — на сборке мусора интерпретатор
    выдаёт ResourceWarning (в тестах он превращается в ошибку сессии pytest).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SCHEMA_SQL)
    except sqlite3.DatabaseError:
        connection.close()
        raise
    return connection
