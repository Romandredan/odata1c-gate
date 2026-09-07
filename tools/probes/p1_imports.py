"""Проба P1: импорт установленных зависимостей и минимальная проверка работоспособности."""
import platform

import ahocorasick_rs
import httpx
import lxml.etree
import mcp
import pydantic
import snowballstemmer
import uvicorn
import yaml

print("python", platform.python_version(), platform.machine())
print("lxml", lxml.etree.__version__)
automaton = ahocorasick_rs.AhoCorasick(["ромашка"])
print("ahocorasick_rs:", automaton.find_matches_as_strings("оплата от ооо ромашка по счёту"))
print("snowball:", snowballstemmer.stemmer("russian").stemWord("контрагенты"))
print("mcp", getattr(mcp, "__version__", "версия не экспортирована"))
print("httpx", httpx.__version__, "| pydantic", pydantic.VERSION, "| uvicorn", uvicorn.__version__)
print("yaml", yaml.__version__)
