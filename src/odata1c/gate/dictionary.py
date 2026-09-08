"""Словарь гейта: кэш обратного отображения и источник номеров для названий (SPEC §6.6).

Словарь общий для всех баз: значение, классифицированное в одной базе, заменяется в любой другой
с уровнем выше off (SPEC §6.2).
"""

from __future__ import annotations

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

CREATE TABLE IF NOT EXISTS name_variants (
    token TEXT NOT NULL REFERENCES tokens(token) ON DELETE CASCADE,
    variant_norm TEXT NOT NULL,
    PRIMARY KEY (variant_norm)
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
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(СХЕМА)
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
        токен = (
            строка["token"]
            if строка
            else self._создать(type_, нормализованное, base, entity, field)
        )
        self._запомнить_вариант(токен, base, entity, field, raw_value)
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
        return {
            строка["variant_norm"]: строка["token"]
            for строка in self._connection.execute(
                "SELECT variant_norm, token FROM name_variants"
            ).fetchall()
        }

    def _создать(self, type_: str, нормализованное: str, base: str, entity: str, field: str) -> str:
        момент = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        if type_ in НУМЕРУЕМЫЕ:
            номер = self._следующий_номер(type_)
            токен = f"[[{type_}:{номер}]]"
        else:
            номер = None
            токен = self._свободный_токен(type_, нормализованное)

        with self._connection:
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
        self._revision += 1
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
        with self._connection:
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
