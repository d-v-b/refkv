"""Layouts defined outside refkv (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>): a Python package adds a layout of its
own to the hierarchy a built-in profile writes, built from that profile's output, and refkv checks
it, places it under `refkv_layouts/`, declares it and joins it to the output.

A plugin is an object registered under the entry-point group `refkv.layouts`, its key the
layout's name, with these attributes:

- `name`: the layout's name, `<distribution>.<layout>`, the entry point's key;
- `path`: its node's default path below `refkv_layouts/`, one key segment;
- `formats`: the base profiles it reads, each with the versions of its convention it reads
  (`{"tiff": {0}, "hdf5": {0}}`); names this refkv has no file profile of are ignored;
- `convention`: its convention, a `ConventionInfo`;
- `options(given)`: the options in canonical form, or ValueError;
- `write(base, options)`: the layout's entries, keys relative to its node, built from `base`
  (a `Base`), most often with `copy`; it raises `refkv.zarrify.Rejected` for an input it cannot
  present.

    from refkv.zarrify import zarrify
    from refkv.zarrify.layouts import Layout

    store = zarrify("image.ome.tif", plugins=[Layout("acme-ome.ome06")])
    store.mapping["refkv_layouts/ome06/zarr.json"]
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from refkv.pb import Concat, Range
from refkv.refstore import ReferenceStore
from refkv.zarrify.common import PROFILES, Rejected, reference_store
from refkv.zarrify.profiles import REGISTRY

if TYPE_CHECKING:
    from refkv.ir import Ir

__all__ = ["Base", "ConventionInfo", "Layout", "LayoutFailed", "LayoutRejected", "LayoutUsageError", "Plugin",
           "copy"]

GROUP = "refkv.layouts"  # the entry points' group
NODE = "refkv_layouts"  # the root's child that holds every plugin layout
KEY = "refkv_layout"  # the refkv_layout convention's member, and its CMO's name
UUID = "372e9a9f-06b1-4ea1-8829-e206c05e03e6"
VERSION = 0
# The refkv_layout convention's metadata object (spec/layout.md §1
# <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#1-declaration>): at version 0, which has
# no tag, its URLs name the development branch.
CMO = {
    "uuid": UUID,
    "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/layout/schema.json",
    "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/layout.md",
    "name": KEY,
    "description": "A layout of a zarrified source that a plugin wrote, and what it was written from",
}
# The conventions refkv defines, which no plugin layout's documents may declare.
REFKV_UUIDS = frozenset([UUID, *(str(e["uuid"]) for e in REGISTRY)])
FILE_PROFILES = frozenset(str(e["name"]) for e in REGISTRY if e["kind"] in ("file", "both"))

NAME = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")
PATH = re.compile(r"[a-z0-9_-]+")


class LayoutUsageError(ValueError):
    """A plugin layout asked for wrongly: a name no plugin has, a plugin that fails to load or
    breaks the rules of spec/layout.md §2, options it refuses, two layouts of one name, path or
    key, or a layout on a store input."""


class LayoutRejected(Rejected):
    """The layout request is refused for this input: a base the plugin does not read, or a
    `Rejected` the plugin raised. The input is one the profile accepts; no output is written."""


class LayoutFailed(Exception):  # noqa: N818 (public name, beside Rejected)
    """The plugin failed: its `write` raised, or its entries broke a rule of spec/layout.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#3-the-entries>."""


@dataclass(frozen=True)
class ConventionInfo:
    """A plugin's convention: its version, and the members of its convention metadata object
    (CMO) but `name`, which is the layout's key."""

    uuid: str
    version: int
    schema_url: str
    spec_url: str
    description: str


@dataclass(frozen=True)
class Layout:
    """A plugin layout to write: the plugin's `name`, its `options` (a JSON object, which the
    plugin puts in canonical form), and its node's `path` below `refkv_layouts/` (default: the
    plugin's)."""

    name: str
    options: Mapping[str, object] | None = None
    path: str | None = None


class Plugin(Protocol):
    """What a plugin provides (the module's docstring)."""

    name: str
    path: str
    formats: Mapping[str, Iterable[int]]
    convention: ConventionInfo

    def options(self, given: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def write(self, base: Base, options: Mapping[str, Any]) -> Mapping[str, bytes | Range | Concat]: ...


def key_of(name: str) -> str:
    """A layout's key, its CMO's `name`: its name with `.` and `-` as `_`."""
    return name.replace(".", "_").replace("-", "_")


def _normalized(distribution: str) -> str:
    """A distribution's name, normalized as PEP 503 says."""
    return re.sub(r"[-_.]+", "-", distribution).lower()


def _entry_points() -> Sequence[importlib.metadata.EntryPoint]:
    """The installed plugins' entry points."""
    return tuple(importlib.metadata.entry_points(group=GROUP))


def load(name: str) -> Plugin:
    """The plugin named `name`, loaded and checked (spec/layout.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#2-plugins>), or LayoutUsageError."""
    if NAME.fullmatch(name) is None:
        raise LayoutUsageError(f"{name!r} is not a layout's name, <distribution>.<layout> in [a-z0-9-]")
    first = name.split(".", 1)[0]
    if first.startswith("refkv"):
        raise LayoutUsageError(f"{name!r}: names whose first segment starts with refkv are refkv's")
    found = [e for e in _entry_points() if e.name == name]
    if not found:
        raise LayoutUsageError(f"no layout plugin is named {name!r} (the entry points of {GROUP!r})")
    if len(found) > 1:
        raise LayoutUsageError(f"{len(found)} entry points provide the layout {name!r}")
    entry = found[0]
    dist = entry.dist
    if dist is None or _normalized(dist.name) != first:
        given = "no distribution" if dist is None else f"the distribution {dist.name!r}"
        raise LayoutUsageError(f"{name!r} is provided by {given}: a layout's name starts with its distribution's")
    try:
        plugin = entry.load()
    except Exception as e:
        raise LayoutUsageError(f"the layout plugin {name!r} failed to load: {e}") from e
    _check_plugin(name, plugin)
    return plugin


def _check_plugin(name: str, plugin: Plugin) -> None:
    """Checks a loaded plugin's attributes against spec/layout.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#2-plugins>."""
    if getattr(plugin, "name", None) != name:
        raise LayoutUsageError(f"the layout plugin {name!r} names itself {getattr(plugin, 'name', None)!r}")
    path = getattr(plugin, "path", None)
    if not isinstance(path, str) or not _path_ok(path):
        raise LayoutUsageError(f"{name}: its path {path!r} is not one key segment of [a-z0-9_-]")
    convention = getattr(plugin, "convention", None)
    if not isinstance(convention, ConventionInfo):
        raise LayoutUsageError(f"{name}: its convention is not a ConventionInfo")
    if convention.uuid in REFKV_UUIDS:
        raise LayoutUsageError(f"{name}: its convention's UUID is a refkv convention's")
    if not isinstance(convention.version, int) or convention.version < 0:
        raise LayoutUsageError(f"{name}: its convention's version is not a natural number")
    formats = getattr(plugin, "formats", None)
    if not isinstance(formats, Mapping) or not all(
            isinstance(k, str) and all(isinstance(v, int) for v in vs) for k, vs in formats.items()):
        raise LayoutUsageError(f"{name}: formats is not a mapping of profile names to convention versions")
    if not callable(getattr(plugin, "options", None)) or not callable(getattr(plugin, "write", None)):
        raise LayoutUsageError(f"{name}: a layout plugin has options() and write()")


def _path_ok(path: str) -> bool:
    return PATH.fullmatch(path) is not None and not path.startswith("__")


@dataclass(frozen=True)
class Planned:
    """A layout asked for: its plugin loaded, its options canonical, and its node's path."""

    layout: Layout
    plugin: Plugin
    options: Mapping[str, object]
    path: str


def plan(layouts: Sequence[Layout]) -> list[Planned]:
    """The plugin layouts of a call, loaded and checked before the input is opened, or
    LayoutUsageError."""
    planned: list[Planned] = []
    for layout in layouts:
        if not isinstance(layout, Layout):
            raise LayoutUsageError(f"plugins holds Layout values, not {layout!r}")
        plugin = load(layout.name)
        path = plugin.path if layout.path is None else layout.path
        if not _path_ok(path):
            raise LayoutUsageError(f"{layout.name}: the path {path!r} is not one key segment of [a-z0-9_-]")
        try:
            options = dict(plugin.options(dict(layout.options or {})))
        except ValueError as e:
            raise LayoutUsageError(f"{layout.name}: its options are refused: {e}") from e
        try:  # canonical options are JSON as they are: no NaN, no tuples
            same = json.loads(json.dumps(options, allow_nan=False)) == options
        except (TypeError, ValueError) as e:
            raise LayoutUsageError(f"{layout.name}: its canonical options are not a JSON object: {e}") from e
        if not same:
            raise LayoutUsageError(f"{layout.name}: its canonical options change in a JSON round trip")
        planned.append(Planned(layout, plugin, options, path))
    for what, seen in (("name", [p.layout.name for p in planned]), ("path", [p.path for p in planned]),
                       ("key", [key_of(p.layout.name) for p in planned])):
        if len(seen) != len(set(seen)):
            raise LayoutUsageError(f"two plugin layouts have one {what}")
    return planned


def _without_checksum(value: Range | Concat) -> Range | Concat:
    """A reference value as spec/zarrify.md §1.1 compares it: its ranges without checksums."""
    if isinstance(value, Range):
        return dataclasses.replace(value, crc32c=None)
    return Concat(tuple(dataclasses.replace(r, crc32c=None) for r in value.parts))


def _is_document(key: str) -> bool:
    return key == "zarr.json" or key.endswith("/zarr.json")


def _root(store: ReferenceStore) -> dict[str, Any]:
    """The root document of a built-in output, parsed."""
    value = store.mapping["zarr.json"]
    if not isinstance(value, bytes):
        raise TypeError("an output's root document is bytes")
    return json.loads(value)


class Base:
    """The built-in profile's output as a plugin sees it (spec/layout.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#2-plugins>): its image layout and
    `refkv_source`, without a dataset layout, its root without `layouts`. Read only.

    - `profile`, `version`, `uuid`: the profile that read the input (a TIFF the parser found to
      be NDPI is `ndpi`), and its convention's version and UUID;
    - `ir`: the IR the output was projected from (`refkv.ir.Ir`);
    - `keys()`, `document(key)`, `array(path)`: its keys, a JSON document as a dict, and an
      array's document and chunks."""

    def __init__(self, store: ReferenceStore, ir: Ir | None) -> None:
        root = _root(store)
        prop = dict(root["attributes"]["refkv_zarrified"])
        layouts = prop.pop("layouts", {})
        root["attributes"]["refkv_zarrified"] = prop
        self.profile: str = prop["profile"]
        self.version: int = prop["version"]
        self.uuid: str = PROFILES[self.profile][0]
        self.ir = ir
        hidden = tuple(f"{p}/" for p in layouts.values())
        self._values = {k: v for k, v in store.mapping.items() if not k.startswith(hidden)}
        self._root = root
        self._references = frozenset(_without_checksum(v) for v in self._values.values()
                                     if isinstance(v, (Range, Concat)))

    def keys(self) -> list[str]:
        """The base's keys, sorted."""
        return sorted(self._values)

    def document(self, key: str) -> dict[str, Any]:
        """The JSON document at `key` (a `zarr.json`), parsed: a new dict on each call."""
        if key == "zarr.json":
            return json.loads(json.dumps(self._root))
        value = self._values.get(key)
        if not _is_document(key) or not isinstance(value, bytes):
            raise KeyError(key)
        return json.loads(value)

    def array(self, path: str) -> tuple[dict[str, Any], dict[str, bytes | Range | Concat]]:
        """The array at `path`: its document, and its chunks by key relative to the array."""
        prefix = f"{path}/" if path else ""
        doc = self.document(f"{prefix}zarr.json")
        if doc.get("node_type") != "array":
            raise KeyError(f"{path!r} is not an array")
        chunks = {k[len(prefix):]: v for k, v in self._values.items()
                  if k.startswith(prefix) and not _is_document(k[len(prefix):])}
        return doc, chunks

    def references(self) -> frozenset[Range | Concat]:
        """The base's reference values, without checksums: those a layout may copy."""
        return self._references


def _inverse(order: Sequence[int]) -> list[int]:
    out = [0] * len(order)
    for i, o in enumerate(order):
        out[o] = i
    return out


def _with_transpose(codecs: list[dict[str, object]], inverse: list[int]) -> list[dict[str, object]]:
    """`codecs` with a transpose of `inverse` first, composed with a transpose they start with:
    transposing by `inverse` and then by the base's order `t` is one transpose by
    `[inverse[t[i]] for each i]`; an identity transpose is left out."""
    if codecs and codecs[0].get("name") == "transpose":
        t = codecs[0]["configuration"]["order"]  # type: ignore[index]
        order, rest = [inverse[i] for i in t], codecs[1:]  # type: ignore[union-attr]
    else:
        order, rest = inverse, codecs
    if order == list(range(len(order))):
        return list(rest)
    return [{"name": "transpose", "configuration": {"order": order}}, *rest]


def _permuted_key(key: str, order: Sequence[int], doc: Mapping[str, object]) -> str:
    """A chunk's key with its indexes permuted, in the array's chunk key encoding."""
    encoding = doc.get("chunk_key_encoding") or {"name": "default"}
    name = encoding.get("name")  # type: ignore[union-attr]
    sep = (encoding.get("configuration") or {}).get("separator")  # type: ignore[union-attr]
    if name == "default":
        sep = sep or "/"
        head, _, rest = key.partition(sep)
        if head != "c" or not rest:
            raise ValueError(f"{key!r} is not a chunk key of the default encoding")
        idx = rest.split(sep)
        return sep.join(["c", *(idx[o] for o in order)])
    if name == "v2":
        sep = sep or "."
        idx = key.split(sep)
        return sep.join(idx[o] for o in order)
    raise ValueError(f"the chunk key encoding {name!r} is not one copy can permute")


def copy(base: Base, path: str, order: Sequence[int] | None = None, attributes: Mapping[str, object] | None = None,
         dimension_names: Sequence[str | None] | None = None) -> dict[str, bytes | Range | Concat]:
    """The entries of the array at `path` of `base`, keys relative to the array, by the rules of
    spec/layout.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#4-copying-an-array>:
    its document with `attributes` as given (`{}` by default) and `dimension_names` as given
    (else the base's), and its chunks' values as they are. With `order`, a permutation of its
    axes (new axis `i` is the base's axis `order[i]`), the array transposed: its shape, chunk
    grid, dimension names and chunk keys permuted, and a `transpose` codec of the inverse order
    first (for a sharded array, first in the shard's inner and index codecs), so that it decodes
    to the base's values, transposed."""
    doc, chunks = base.array(path)
    names = doc.get("dimension_names") if dimension_names is None else list(dimension_names)
    if order is not None:
        n = len(doc["shape"])  # type: ignore[arg-type]
        order = [int(o) for o in order]
        if sorted(order) != list(range(n)):
            raise ValueError(f"order {order} is not a permutation of the array's {n} axes")
        inverse = _inverse(order)
        doc["shape"] = [doc["shape"][o] for o in order]  # type: ignore[index]
        grid = doc["chunk_grid"]["configuration"]  # type: ignore[index]
        for member in ("chunk_shape", "chunk_shapes"):
            if member in grid:
                grid[member] = [grid[member][o] for o in order]  # type: ignore[index]
        codecs = list(doc["codecs"])  # type: ignore[call-overload]
        if codecs and codecs[0].get("name") == "sharding_indexed":
            config = codecs[0]["configuration"]
            config["chunk_shape"] = [config["chunk_shape"][o] for o in order]
            config["codecs"] = _with_transpose(list(config["codecs"]), inverse)
            config["index_codecs"] = _with_transpose(list(config["index_codecs"]), [*inverse, n])
        else:
            doc["codecs"] = _with_transpose(codecs, inverse)
        if names is not None and dimension_names is None:
            names = [names[o] for o in order]  # type: ignore[index]
        chunks = {_permuted_key(k, order, doc): v for k, v in chunks.items()}
    doc["attributes"] = dict(attributes or {})
    if names is not None:
        doc["dimension_names"] = list(names)  # type: ignore[call-overload]
    else:
        doc.pop("dimension_names", None)
    return {"zarr.json": dump(doc), **dict(sorted(chunks.items()))}


def dump(doc: object) -> bytes:
    """A document as zarrify writes one: compact JSON, in UTF-8."""
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()


def _segments_ok(key: str) -> bool:
    return bool(key) and all(s and s not in (".", "..") and not s.startswith("__") for s in key.split("/"))


def _check(name: str, entries: Mapping[str, object], base: Base, key: str) -> dict[str, dict[str, object]]:
    """Checks a layout's entries against spec/layout.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#3-the-entries>, and returns its
    documents, parsed, by key; LayoutFailed otherwise."""
    def fail(why: str) -> LayoutFailed:
        return LayoutFailed(f"{name}: {why}")

    docs: dict[str, dict[str, object]] = {}
    for k, v in entries.items():
        if not isinstance(k, str) or not _segments_ok(k):
            raise fail(f"{k!r} is not a key of a layout (relative, with no empty, '.', '..' or '__' segment)")
        if _is_document(k):
            if not isinstance(v, (bytes, bytearray)):
                raise fail(f"{k}: a document is bytes")
            try:
                doc = json.loads(v)
            except ValueError as e:
                raise fail(f"{k} is not JSON: {e}") from e
            if not isinstance(doc, dict) or doc.get("zarr_format") != 3 or doc.get("node_type") not in ("group",
                                                                                                         "array"):
                raise fail(f"{k} is not a Zarr v3 group or array document")
            docs[k] = doc
        elif isinstance(v, (Range, Concat)):
            if _without_checksum(v) not in base.references():
                raise fail(f"{k}: a reference that is not one of the base's (a layout copies references, makes none)")
        elif not isinstance(v, (bytes, bytearray)):
            raise fail(f"{k}: a value is bytes, a Range or a Concat")
    if docs.get("zarr.json", {}).get("node_type") != "group":
        raise fail("the layout's node, zarr.json, is not a group")
    nodes = {k[: -len("zarr.json")].rstrip("/"): str(d["node_type"]) for k, d in docs.items()}
    for node in nodes:
        if node:
            parent = node.rpartition("/")[0]
            if nodes.get(parent) != "group":
                raise fail(f"{node}/zarr.json: its parent {parent or '(the node)'} is not a group of the layout")
    for k in entries:
        if k in docs:
            continue
        holder = k.rpartition("/")[0]
        while holder and holder not in nodes:
            holder = holder.rpartition("/")[0]
        if nodes.get(holder) != "array":
            raise fail(f"{k} is not below an array of the layout")
    for k, doc in docs.items():
        attrs = doc.get("attributes", {})
        if "consolidated_metadata" in doc:
            raise fail(f"{k} holds consolidated_metadata")
        if not isinstance(attrs, dict):
            raise fail(f"{k}: attributes is not an object")
        for member in ("refkv_zarrified", KEY):
            if member in attrs:
                raise fail(f"{k} holds {member}, which refkv writes")
        if key in attrs and (not isinstance(attrs[key], dict) or "version" in attrs[key]):
            raise fail(f"{k}: the layout's key {key} holds an object without version, which refkv writes")
        for cmo in attrs.get("zarr_conventions", []):
            if isinstance(cmo, dict) and cmo.get("uuid") in REFKV_UUIDS:
                raise fail(f"{k} declares a refkv convention, {cmo.get('uuid')}")
    return docs


def _cmo(name: str, convention: ConventionInfo) -> dict[str, str]:
    """A plugin's convention metadata object: the five members the framework allows."""
    return {"uuid": convention.uuid, "schema_url": convention.schema_url, "spec_url": convention.spec_url,
            "name": key_of(name), "description": convention.description}


def _declared(name: str, p: Planned, node: dict[str, object], base: Base, source: object) -> dict[str, object]:
    """The layout node's document, declared (spec/layout.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#3-the-entries>): the refkv_layout CMO,
    then the plugin's, then the others it wrote; its key; and the layout property."""
    mine = _cmo(name, p.plugin.convention)
    attrs = dict(node.get("attributes", {}))  # type: ignore[call-overload]
    others = [c for c in attrs.pop("zarr_conventions", [])
              if not (isinstance(c, dict) and c.get("uuid") == mine["uuid"])]
    key = key_of(name)
    own = {"version": p.plugin.convention.version, **attrs.pop(key, {})}
    node["attributes"] = {"zarr_conventions": [CMO, mine, *others], **attrs, key: own, KEY: {
        "version": VERSION,
        "layout": {"name": name, "uuid": p.plugin.convention.uuid, "version": p.plugin.convention.version},
        "source": source,
        "base": {"uuid": base.uuid, "version": base.version},
        "options": dict(p.options),
    }}
    return node


def join(store: ReferenceStore, ir: Ir | None, planned: Sequence[Planned]) -> ReferenceStore:
    """`store`, a built-in profile's output, with the plugin layouts `planned` added
    (spec/layout.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#3-the-entries>):
    each plugin's `write` run, one at a time, its entries checked, placed under
    `refkv_layouts/<path>/`, declared and named in the root's `layouts`. Raises LayoutRejected or
    LayoutFailed."""
    if any(k.startswith(f"{NODE}/") for k in store.mapping):
        raise LayoutRejected(f"the base holds {NODE}/, which plugin layouts take")
    base = Base(store, ir)
    root = _root(store)
    prop = root["attributes"]["refkv_zarrified"]
    added: dict[str, bytes | Range | Concat] = {}
    names: dict[str, str] = {}
    for p in planned:
        name = p.layout.name
        versions = p.plugin.formats.get(base.profile) if base.profile in FILE_PROFILES else None
        if versions is None or base.version not in set(versions):
            reads = ", ".join(sorted(k for k in p.plugin.formats if k in FILE_PROFILES)) or "no profile"
            raise LayoutRejected(f"{name} does not read the {base.profile} profile's convention, version "
                                 f"{base.version} (it reads {reads})")
        try:
            entries = dict(p.plugin.write(base, p.options))
        except Rejected as e:
            raise LayoutRejected(f"{name}: {e}") from e
        except Exception as e:
            raise LayoutFailed(f"{name}: {type(e).__name__}: {e}") from e
        docs = _check(name, entries, base, key_of(name))
        mine = _cmo(name, p.plugin.convention)
        for k, doc in docs.items():
            for cmo in doc.get("attributes", {}).get("zarr_conventions", []):  # type: ignore[union-attr]
                if isinstance(cmo, dict) and cmo.get("uuid") == mine["uuid"] and cmo != mine:
                    raise LayoutFailed(f"{name}: {k} declares its convention otherwise than its ConventionInfo")
        entries["zarr.json"] = dump(_declared(name, p, docs["zarr.json"], base, prop["source"]))
        prefix = f"{NODE}/{p.path}"
        added |= {f"{prefix}/{k}": v for k, v in entries.items()}
        names[name] = prefix
    added[f"{NODE}/zarr.json"] = dump({"zarr_format": 3, "node_type": "group",
                                       "attributes": {"zarr_conventions": [CMO], KEY: {"version": VERSION}}})
    # the root's layouts: after source, its names in code point order
    layouts = dict(sorted({**prop.get("layouts", {}), **names}.items()))
    rewritten: dict[str, object] = {}
    for k, v in prop.items():
        if k != "layouts":
            rewritten[k] = v
        if k == "source":
            rewritten["layouts"] = layouts
    root["attributes"]["refkv_zarrified"] = rewritten
    mapping = {**store.mapping, "zarr.json": dump(root), **added}
    late = set(store.read_at_open) | {k for k in added if _is_document(k)}
    refs = [(k, [v] if isinstance(v, Range) else list(v.parts)) for k, v in sorted(mapping.items())
            if isinstance(v, (Range, Concat))]
    entries_ = [(k, bytes(v), k in late) for k, v in sorted(mapping.items()) if isinstance(v, (bytes, bytearray))]
    info = dict(store.info)
    info["layouts"] = sorted({*info.get("layouts", []), *names})  # type: ignore[misc]
    return reference_store(refs, entries_, list(store._sources), policy=store.policy,  # noqa: SLF001 (the base's table)
                           info=info)
