"""Проверка, что пакет собран и импортируется."""

import odata1c


def test_версия_пакета_объявлена():
    assert isinstance(odata1c.__version__, str)
    assert odata1c.__version__.count(".") >= 2
