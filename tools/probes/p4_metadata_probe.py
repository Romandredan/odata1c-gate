"""Проба P4 (SPEC §15.3): что на самом деле отдаёт OData-интерфейс базы 1С.

Адрес и учётные данные берутся из рабочего bases.yaml по имени базы — в репозиторий не попадают:
    uv run python tools/probes/p4_metadata_probe.py trade_dev

Представители сущностей выбираются из самого $metadata: публикация OData выборочная, заранее
зашитые имена дали бы 404 без ответа на вопрос. Проверка DELETE идёт по заведомо несуществующему
ключу (период 2099 года, случайные GUID измерений) — формат ключа и метод проверяются без удаления.
"""

import collections
import pathlib
import re
import sys
import time
import uuid

import httpx
from lxml import etree

from odata1c.config.home import resolve_home
from odata1c.config.loader import load_config
from odata1c.index.naming import VIRTUAL_SUFFIXES

OUT = pathlib.Path("tests/fixtures/edmx")
REGISTER_KINDS = ("AccumulationRegister", "InformationRegister", "AccountingRegister",
                  "CalculationRegister")


def local(element) -> str:
    return etree.QName(element).localname


def timed_get(client: httpx.Client, path: str, **kwargs) -> tuple[httpx.Response | None, float, str]:
    started = time.monotonic()
    try:
        response = client.get(path, **kwargs)
        return response, time.monotonic() - started, ""
    except httpx.HTTPError as exc:
        return None, time.monotonic() - started, f"{type(exc).__name__}: {exc}"


def show(label: str, response: httpx.Response | None, seconds: float, error: str) -> None:
    if response is None:
        print(f"  {label}: ошибка за {seconds:.1f} с — {error}")
        return
    body = response.text[:400].replace("\n", " ")
    print(f"  {label}: HTTP {response.status_code} за {seconds:.1f} с; {body}")


class Metadata:
    def __init__(self, edmx: bytes) -> None:
        self.root = etree.fromstring(edmx)
        self.types: dict[str, etree._Element] = {}
        self.sets: dict[str, str] = {}  # имя набора → имя типа без пространства имён
        self.imports: list[etree._Element] = []
        for element in self.root.iter():
            if not isinstance(element.tag, str):
                continue
            name = local(element)
            if name == "EntityType":
                self.types[element.get("Name")] = element
            elif name == "EntitySet":
                self.sets[element.get("Name")] = element.get("EntityType").rsplit(".", 1)[-1]
            elif name == "FunctionImport":
                self.imports.append(element)

    def keys(self, set_name: str) -> list[tuple[str, str]]:
        entity_type = self.types[self.sets[set_name]]
        props = {p.get("Name"): p.get("Type") for p in entity_type if local(p) == "Property"}
        return [(k.get("Name"), props.get(k.get("Name"), "?"))
                for k in entity_type.iter() if isinstance(k.tag, str) and local(k) == "PropertyRef"]

    def props(self, set_name: str) -> list[str]:
        return [p.get("Name") for p in self.types[self.sets[set_name]] if local(p) == "Property"]

    def navigations(self, set_name: str) -> list[str]:
        return [p.get("Name") for p in self.types[self.sets[set_name]]
                if local(p) == "NavigationProperty"]


def report_structure(md: Metadata, edmx: bytes) -> None:
    versions = {k: v for k, v in md.root.attrib.items()}
    for element in md.root.iter():
        if isinstance(element.tag, str) and local(element) == "DataServices":
            versions.update(element.attrib)
            break
    print(f"размер $metadata: {len(edmx) / 1024 / 1024:.2f} МБ; версии: {versions}")
    print(f"EntityType: {len(md.types)}; EntitySet: {len(md.sets)}; FunctionImport: {len(md.imports)}")

    kinds = collections.Counter(name.split("_", 1)[0] for name in md.sets)
    print("виды по префиксу:", dict(kinds.most_common()))

    known = collections.Counter()
    for name in md.sets:
        for suffix in VIRTUAL_SUFFIXES:
            if name.endswith(suffix):
                known[suffix] += 1
                break
    print("известные суффиксы среди EntitySet:", dict(known))

    # Хвосты латиницей после имени объекта — их порождает платформа, а не разработчик.
    tails = collections.Counter()
    for name in md.sets:
        parts = name.split("_")
        if len(parts) > 2 and re.fullmatch(r"[A-Za-z]+", parts[-1]):
            tails[f"{parts[0]}…_{parts[-1]}"] += 1
    print("латинские хвосты имён наборов:", dict(tails.most_common()))

    signatures = collections.Counter()
    examples: dict[str, str] = {}
    for fi in md.imports:
        params = [(p.get("Name"), p.get("Type")) for p in fi if local(p) == "Parameter"]
        attrs = {k: v for k, v in fi.attrib.items() if k != "Name"}
        signature = f"{fi.get('Name')} {sorted(attrs)} параметры={[n for n, _ in params]}"
        signatures[fi.get("Name")] += 1
        examples.setdefault(fi.get("Name"), f"{signature} пример={fi.attrib} {params}")
    print("FunctionImport по именам:", dict(signatures.most_common()))
    for name, example in examples.items():
        print(f"  {name}: {example[:500]}")

    shown: set[str] = set()
    for set_name in md.sets:
        kind = set_name.split("_", 1)[0]
        keys = md.keys(set_name)
        label = f"{kind} ключей={len(keys)}"
        if label not in shown:
            shown.add(label)
            print(f"ключ {label}: {set_name} → {keys}")

    for kind in ("Catalog", "Document"):
        name = next(n for n in md.sets if n.startswith(kind + "_") and n.count("_") == 1)
        service = [p for p in md.props(name) if re.fullmatch(r"[A-Za-z_]+", p)]
        print(f"служебные поля {name}: {service}")


def pick_independent_register(md: Metadata) -> str | None:
    for name in md.sets:
        if not name.startswith("InformationRegister_") or name.count("_") != 1:
            continue
        key_names = [k for k, _ in md.keys(name)]
        if "Recorder" in key_names or "Recorder_Key" in key_names:
            continue
        if key_names and all(t in ("Edm.Guid", "Edm.DateTime") for _, t in md.keys(name)):
            return name
    return None


def key_literal(md: Metadata, set_name: str) -> str:
    parts = []
    for name, edm_type in md.keys(set_name):
        if edm_type == "Edm.DateTime":
            parts.append(f"{name}=datetime'2099-01-01T00:00:00'")
        else:
            parts.append(f"{name}=guid'{uuid.uuid4()}'")
    return ",".join(parts)


def probe_requests(client: httpx.Client, md: Metadata) -> None:
    catalog = next(n for n in md.sets if n.startswith("Catalog_") and n.count("_") == 1)
    document = next(n for n in md.sets if n.startswith("Document_") and n.count("_") == 1
                    and any(s.startswith(n + "_") for s in md.sets))
    print(f"представители: справочник {catalog}, документ с табличной частью {document}")

    checks = {
        "$inlinecount": f"{catalog}?$format=json&$top=1&$inlinecount=allpages&$select=Ref_Key",
        "allowedOnly": f"{catalog}?$format=json&$top=1&allowedOnly=true&$select=Ref_Key",
        "$top без $format (формат по умолчанию)": f"{catalog}?$top=1&$select=Ref_Key",
        "фильтр по подстроке": f"{catalog}?$format=json&$top=1&$filter=substringof('а', Description)"
                               "&$select=Ref_Key,Description",
    }
    for label, path in checks.items():
        show(label, *timed_get(client, path))

    # Цепочка $expand по навигационным свойствам документа.
    chain: list[str] = []
    current = document
    for _ in range(4):
        navs = md.navigations(current)
        if not navs:
            break
        chain.append(navs[0])
        show(f"$expand глубина {len(chain)} ({'/'.join(chain)})",
             *timed_get(client, f"{document}?$format=json&$top=1&$select=Ref_Key"
                                f"&$expand={'/'.join(chain)}"))
        target = next((fi for fi in md.types[md.sets[current]] if local(fi) == "NavigationProperty"
                       and fi.get("Name") == navs[0]), None)
        # Тип конца связи без знания Association: ищем набор по префиксу имени свойства.
        guess = next((s for s in md.sets if s.count("_") == 1 and s.endswith("_" + navs[0])), None)
        if target is None or guess is None:
            break
        current = guess

    register = next((n for n in md.sets if n.startswith("AccumulationRegister_") and n.count("_") == 1),
                    None)
    if register:
        for call in ("Balance()", "Turnovers()", "BalanceAndTurnovers()"):
            show(f"виртуальная таблица {register}/{call}",
                 *timed_get(client, f"{register}/{call}?$format=json&$top=1"))

    independent = pick_independent_register(md)
    print(f"независимый регистр сведений: {independent}; ключ {md.keys(independent) if independent else '—'}")
    if independent:
        literal = key_literal(md, independent)
        show("GET записи по составному ключу (не существует)",
             *timed_get(client, f"{independent}({literal})?$format=json"))
        started = time.monotonic()
        try:
            response = client.delete(f"{independent}({literal})?$format=json")
            show(f"DELETE {independent}({literal})", response, time.monotonic() - started, "")
        except httpx.HTTPError as exc:
            show("DELETE", None, time.monotonic() - started, str(exc))


def main(base_name: str) -> None:
    config = load_config(resolve_home(None))
    base = config.bases[base_name]
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=base.url, auth=(base.user, base.password), timeout=300,
                      headers={"IBSession": "start"}) as client:
        response, seconds, error = timed_get(client, "$metadata", headers={"Accept": "application/xml"})
        if response is None or response.status_code != 200:
            show("$metadata", response, seconds, error)
            sys.exit(1)
        print(f"$metadata получен за {seconds:.1f} с; сеанс: {response.headers.get('IBSession')}")
        (OUT / "probe.full.edmx").write_bytes(response.content)
        md = Metadata(response.content)
        report_structure(md, response.content)
        probe_requests(client, md)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "trade_dev")
