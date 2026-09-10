"""Одновременный первый старт нескольких процессов на чистом `daemon.yaml` (план M1d, задача 6,
раунд правок 2, находка Б.3): `ensure_gate_secret` — «прочитать, дописать строку, `os.replace`»
без взаимного исключения; читает секрет ДВАЖДЫ (проверка «уже есть?», потом текст для дописывания)
с зазором между чтениями, в который умещались несколько процессов, каждый не видевший чужого
секрета — `daemon.yaml` получал 2-3 строки `gate_secret`. Цена не косметическая: токены гейта
детерминированы от секрета (инвариант 5), расхождение после следующего перезапуска демона рвёт
совпадение старых токенов с новыми.

Настоящие ОС-процессы (`multiprocessing`), не потоки — гонка на файловых syscall (`os.replace`,
`open`) воспроизводится по-настоящему только через них, GIL здесь ни при чём. `multiprocessing.
Barrier` синхронизирует старт всех воркеров, чтобы бить точно в узкое окно гонки, а не полагаться
на случайное совпадение времени запуска процессов пула — без барьера гонка не гарантирована
(проверено: `multiprocessing.Pool.map` без барьера гонку не поймал ни разу за 5 попыток, тот же
код без барьера же и с ним — при отключённом замке падал `PermissionError`/`WinError 32` или
дописывал вторую строку секрета)."""

from __future__ import annotations

import multiprocessing
import pathlib
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="находка воспроизведена и чинится для Windows-пути; на POSIX os.replace атомарен иначе",
)


def _воркер(args: tuple[str, object]) -> str:
    from odata1c.config.writer import ensure_gate_secret

    path_str, барьер = args
    барьер.wait()
    return ensure_gate_secret(pathlib.Path(path_str))


def test_одновременный_первый_старт_даёт_ровно_один_gate_secret(tmp_path):
    path = tmp_path / "daemon.yaml"
    path.write_text("port: 7171\n", encoding="utf-8")

    n = 6
    барьер = multiprocessing.Manager().Barrier(n)
    with multiprocessing.Pool(n) as пул:
        секреты = пул.map(_воркер, [(str(path), барьер)] * n)

    текст = path.read_text(encoding="utf-8")
    assert текст.count("gate_secret:") == 1, f"ожидалась ровно одна строка секрета: {текст!r}"
    assert len(set(секреты)) == 1, "все процессы должны сойтись на одном и том же секрете"
