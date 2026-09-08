"""Общие фикстуры юнит-тестов."""

import pathlib

import pytest

ОБРАЗЦЫ = pathlib.Path(__file__).parent.parent / "fixtures" / "edmx"


@pytest.fixture
def edmx_synthetic() -> bytes:
    """Синтетический $metadata: по одному представителю каждого разбираемого случая."""
    return (ОБРАЗЦЫ / "synthetic.edmx").read_bytes()
