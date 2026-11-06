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
- `options(given)`: the options in canonical form, a JSON object, or ValueError;
- `write(base, options)`: the layout's entries, keys relative to its node, built from `base`
  (a `Base`), most often with `copy`; it raises `refkv.zarrify.Rejected` for an input it cannot
  present.

    from refkv.zarrify import zarrify
    from refkv.zarrify.layouts import Layout

    store = zarrify("image.ome.tif", plugins=[Layout("acme-ome.ome06")])
    store.mapping["refkv_layouts/ome06/zarr.json"]

The checks guard against a plugin's mistakes, not against a hostile plugin, which is code
running in the caller's process.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import json
import math
import re
import urllib.parse
import uuid as uuids
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from refkv.pb import Concat, Range
from refkv.zarrify.common import PROFILES, Rejected, reference_store
from refkv.zarrify.profiles import REGISTRY

if TYPE_CHECKING:
    from refkv.ir import Ir
    from refkv.refstore import ReferenceStore

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
# What only refkv declares: its conventions' UUIDs, CMO names and URLs, and the members it writes.
PROVENANCE_UUID = "7ffd0ef7-df19-408c-a4df-c91623b4c3db"  # spec/provenance.md's convention
REFKV_UUIDS = frozenset([UUID, PROVENANCE_UUID, *(str(e["uuid"]).lower() for e in REGISTRY)])
REFKV_NAMES = frozenset(["refkv_zarrified", KEY, "refkv_provenance"])
REFKV_HOSTS = frozenset(["github.com", "raw.githubusercontent.com"])
RESERVED_KEYS = frozenset(["zarr_conventions", *REFKV_NAMES])
FILE_PROFILES = frozenset(str(e["name"]) for e in REGISTRY if e["kind"] in ("file", "both"))
ARRAY_MEMBERS = ("shape", "data_type", "chunk_grid", "chunk_key_encoding", "codecs", "fill_value")

MAX_KEY = 65535  # bytes of a key's UTF-8 the ZIP binding holds (spec/bindings/vzip.md)

NAME = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")
PATH = re.compile(r"[a-z0-9_-]+")


class LayoutUsageError(ValueError):
    """A plugin layout asked for wrongly: a name no plugin has, a plugin that fails to load or
    breaks the rules of spec/layout.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#2-plugins>, options it refuses, two
    layouts of one name, path, key or UUID, or a layout on a store input."""


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


def _is_refkv_url(url: object) -> bool:
    """Whether `url` is under refkv's repository, `d-v-b/refkv` on GitHub, whatever its scheme,
    case, `www.`, port or trailing slash."""
    if not isinstance(url, str):
        return False
    parts = urllib.parse.urlsplit(url.strip().lower() if "://" in url else "//" + url.strip().lower())
    host = (parts.hostname or "").removeprefix("www.")
    return host in REFKV_HOSTS and (parts.path.rstrip("/") + "/").startswith("/d-v-b/refkv/")


def _utf8(value: object) -> bool:
    """Whether every string in `value` encodes as UTF-8 (no lone surrogate)."""
    return _strings_ok(value)


def _usage(name: str, why: str) -> LayoutUsageError:
    return LayoutUsageError(f"{name}: {why}")


def load(name: str) -> tuple[Plugin, dict[str, frozenset[int]]]:
    """The plugin named `name`, loaded and checked (spec/layout.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#2-plugins>), and the formats it reads;
    or LayoutUsageError."""
    if not isinstance(name, str) or NAME.fullmatch(name) is None:
        raise LayoutUsageError(f"{name!r} is not a layout's name, <distribution>.<layout> in [a-z0-9-]")
    first = name.split(".", 1)[0]
    if first.startswith("refkv"):
        raise _usage(name, "names whose first segment starts with refkv are refkv's")
    if key_of(name) in RESERVED_KEYS:
        raise _usage(name, f"its key {key_of(name)} is a member refkv writes")
    points = list(_entry_points())
    found = [e for e in points if e.name == name]
    if not found:
        installed = ", ".join(sorted(str(e.name) for e in points)) or "none"
        raise LayoutUsageError(f"no layout plugin is named {name!r} (the entry points of {GROUP!r}: {installed})")
    if len(found) > 1:
        raise _usage(name, f"{len(found)} entry points provide it")
    entry = found[0]
    dist = getattr(entry, "dist", None)
    dist_name = getattr(dist, "name", None)
    if not isinstance(dist_name, str) or _normalized(dist_name) != first:
        given = "no distribution" if not isinstance(dist_name, str) else f"the distribution {dist_name!r}"
        raise _usage(name, f"it is provided by {given}: a layout's name starts with its distribution's")
    try:
        plugin = entry.load()
        if isinstance(plugin, type):  # the entry point names the plugin's class: refkv makes the instance
            plugin = plugin()
    except Exception as e:
        raise _usage(name, f"the plugin failed to load: {type(e).__name__}: {e}") from e
    try:
        return plugin, _check_plugin(name, plugin)
    except LayoutUsageError:
        raise
    except Exception as e:  # an attribute that raises when read
        raise _usage(name, f"the plugin's attributes cannot be read: {type(e).__name__}: {e}") from e


def _check_plugin(name: str, plugin: Plugin) -> dict[str, frozenset[int]]:
    """Checks a loaded plugin's attributes against spec/layout.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#2-plugins>, and returns the formats
    it reads."""
    if getattr(plugin, "name", None) != name:
        raise LayoutUsageError(f"the layout plugin {name!r} names itself {getattr(plugin, 'name', None)!r}: "
                               "set its name to the entry point's key")
    path = getattr(plugin, "path", None)
    if not isinstance(path, str) or not _path_ok(path):
        raise _usage(name, f"its path {path!r} is not one key segment of [a-z0-9_-]")
    convention = getattr(plugin, "convention", None)
    if not isinstance(convention, ConventionInfo):
        raise _usage(name, "its convention is not a ConventionInfo")
    if not _utf8([convention.uuid, convention.schema_url, convention.spec_url, convention.description]):
        raise _usage(name, "its convention holds a string that is not valid Unicode")
    try:
        uuid = str(uuids.UUID(convention.uuid))
    except (TypeError, ValueError, AttributeError) as e:
        raise _usage(name, f"its convention's UUID {convention.uuid!r} is not a UUID") from e
    if uuid != convention.uuid:
        raise _usage(name, f"its convention's UUID is written {uuid}, in lower case with hyphens")
    if uuid in REFKV_UUIDS:
        raise _usage(name, "its convention's UUID is a refkv convention's")
    if type(convention.version) is not int or convention.version < 0:
        raise _usage(name, "its convention's version is not a natural number")
    for member in ("schema_url", "spec_url", "description"):
        value = getattr(convention, member)
        if not isinstance(value, str) or not value:
            raise _usage(name, f"its convention's {member} is not a string")
        if member != "description" and _is_refkv_url(value):
            raise _usage(name, f"its convention's {member} is refkv's")
    formats = getattr(plugin, "formats", None)
    try:
        read = {str(k): frozenset(vs) for k, vs in formats.items()}  # type: ignore[union-attr]
    except (AttributeError, TypeError) as e:
        raise _usage(name, "formats is not a mapping of profile names to convention versions") from e
    if not all(isinstance(k, str) and all(type(v) is int and v >= 0 for v in vs) for k, vs in read.items()):
        raise _usage(name, "formats is not a mapping of profile names to convention versions")
    if not set(read) & FILE_PROFILES:  # a later refkv's names are ignored, but one must be this one's
        raise _usage(name, f"formats names no file profile of this refkv ({', '.join(sorted(read)) or 'none'}); "
                           f"they are {', '.join(sorted(FILE_PROFILES))}")
    if not callable(getattr(plugin, "options", None)) or not callable(getattr(plugin, "write", None)):
        raise _usage(name, "a layout plugin has options() and write()")
    return read


def _path_ok(path: str) -> bool:
    return PATH.fullmatch(path) is not None and not path.startswith("__")


@dataclass(frozen=True)
class Planned:
    """A layout asked for: its plugin loaded, the formats it reads, its options in canonical form
    (as JSON text, so that nothing a plugin does changes what is recorded), and its node's path."""

    layout: Layout
    plugin: Plugin
    formats: Mapping[str, frozenset[int]]
    options: str
    path: str
    convention: ConventionInfo  # as checked, whatever the plugin does to its own after

    def fresh_options(self) -> dict[str, Any]:
        """The canonical options, a new copy."""
        return json.loads(self.options)


def plan(layouts: Sequence[Layout]) -> list[Planned]:
    """The plugin layouts of a call, loaded and checked before the input is opened, or
    LayoutUsageError."""
    if isinstance(layouts, Layout) or not isinstance(layouts, Sequence):
        raise LayoutUsageError("plugins is a list of Layout values")
    planned: list[Planned] = []
    for layout in layouts:
        if not isinstance(layout, Layout):
            raise LayoutUsageError(f"plugins holds Layout values, not {layout!r}")
        if layout.options is not None and not isinstance(layout.options, Mapping):
            raise _usage(layout.name, f"its options are a mapping (a JSON object), not {type(layout.options).__name__}")
        if layout.path is not None and not isinstance(layout.path, str):
            raise _usage(layout.name, f"its path is a string, not {type(layout.path).__name__}")
        plugin, formats = load(layout.name)
        convention: ConventionInfo = plugin.convention
        path = str(plugin.path) if layout.path is None else layout.path
        if not _path_ok(path):
            raise _usage(layout.name, f"the path {path!r} is not one key segment of [a-z0-9_-]")
        try:
            given = json.loads(json.dumps(dict(layout.options or {}), allow_nan=False))
        except (TypeError, ValueError, RecursionError) as e:
            raise _usage(layout.name, f"its options are not a JSON object: {type(e).__name__}: {e}") from e
        try:
            options = plugin.options(given)
        except KeyError as e:
            raise _usage(layout.name, f"its options are refused: a missing member {e}") from e
        except (Exception, SystemExit) as e:
            raise _usage(layout.name, f"its options are refused: {e}") from e
        if not isinstance(options, Mapping):
            raise _usage(layout.name, f"its options() returned {type(options).__name__}, not a mapping")
        try:  # canonical options are JSON as they are: no NaN, no tuples, no lone surrogate
            text = json.dumps(dict(options), allow_nan=False, sort_keys=False)
            same = json.loads(text) == dict(options) and _utf8(dict(options))
        except Exception as e:
            raise _usage(layout.name, f"its canonical options are not a JSON object: {type(e).__name__}: {e}") from e
        if not same:
            raise _usage(layout.name, "its canonical options are not JSON as they are (a tuple, say, or a lone "
                                      "surrogate): options() returns lists, dicts, strings, numbers and booleans")
        planned.append(Planned(layout, plugin, formats, text, path, convention))
    for what, seen in (("name", [p.layout.name for p in planned]), ("path", [p.path for p in planned]),
                       ("key", [key_of(p.layout.name) for p in planned]),
                       ("UUID", [p.convention.uuid for p in planned])):
        twice = sorted({v for v in seen if seen.count(v) > 1})
        if twice:
            raise LayoutUsageError(f"two plugin layouts have one {what}, {twice[0]!r}")
    return planned


def _without_checksum(value: Range | Concat) -> Range | Concat:
    """A reference value as spec/zarrify.md §1.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#11-output-and-equivalence> compares
    it: its ranges without checksums."""
    if isinstance(value, Range):
        return dataclasses.replace(value, crc32c=None)
    return Concat(tuple(dataclasses.replace(r, crc32c=None) for r in value.parts))


def _is_reference(value: object) -> bool:
    """Whether `value` is a reference value: a Range, or a Concat of Ranges, of those very types."""
    if type(value) is Range:
        return True
    parts = getattr(value, "parts", None)
    return type(value) is Concat and isinstance(parts, tuple) and all(type(r) is Range for r in parts)


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
    `refkv_source`, without a dataset layout, its root without `layouts`. Read only: each
    plugin is given one of its own.

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
        self._profile: str = prop["profile"]
        self._version: int = prop["version"]
        self._ir = ir
        hidden = tuple(f"{p}/" for p in layouts.values())
        kept = {k: v for k, v in store.mapping.items() if not k.startswith(hidden)}
        # each reference value without its checksum (a plugin sees none: they are of the source's
        # bytes), to the base's own value, which refkv writes
        self._references = {_without_checksum(v): v for v in kept.values() if isinstance(v, (Range, Concat))}
        self._values = {k: _without_checksum(v) if isinstance(v, (Range, Concat)) else v for k, v in kept.items()}
        self._root = json.dumps(root)

    @property
    def profile(self) -> str:
        return self._profile

    @property
    def version(self) -> int:
        return self._version

    @property
    def uuid(self) -> str:
        return PROFILES[self._profile][0]

    @property
    def ir(self) -> Ir | None:
        return self._ir

    def keys(self) -> list[str]:
        """The base's keys, sorted."""
        return sorted(self._values)

    def document(self, key: str) -> dict[str, Any]:
        """The JSON document at `key` (a `zarr.json`), parsed: a new dict on each call."""
        if key == "zarr.json":
            return json.loads(self._root)
        value = self._values.get(key)
        if not _is_document(key) or not isinstance(value, bytes):
            raise KeyError(f"no document {key!r} in the base (a document's key ends in zarr.json)")
        return json.loads(value)

    def array(self, path: str) -> tuple[dict[str, Any], dict[str, bytes | Range | Concat]]:
        """The array at `path`: its document, and its chunks by key relative to the array; a
        KeyError naming `path` if there is none."""
        prefix = f"{path}/" if path else ""
        try:
            doc = self.document(f"{prefix}zarr.json")
        except KeyError:
            raise KeyError(f"no array {path!r} in the base") from None
        if doc.get("node_type") != "array":
            raise KeyError(f"{path!r} is a group of the base, not an array")
        chunks = {k[len(prefix):]: v for k, v in self._values.items()
                  if k.startswith(prefix) and not _is_document(k[len(prefix):])}
        return doc, chunks

    def base_value(self, value: object) -> Range | Concat | None:
        """The base's reference value equal to `value` but for checksums, or None."""
        if not _is_reference(value):
            return None
        try:
            return self._references.get(_without_checksum(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):  # a Range of unhashable members
            return None


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
        if head != "c":
            raise ValueError(f"{key!r} is not a chunk key of the default encoding")
        idx = rest.split(sep) if rest else []
        return sep.join(["c", *(idx[o] for o in order)])
    if name == "v2":
        sep = sep or "."
        idx = key.split(sep)
        return sep.join(idx[o] for o in order)
    raise ValueError(f"the chunk key encoding {name!r} is not one copy can permute")


def copy(base: Base, path: str, order: Sequence[int] | None = None, attributes: Mapping[str, object] | None = None,
         dimension_names: Sequence[str | None] | None = None) -> dict[str, bytes | Range | Concat]:
    """The entries of the array at `path` of `base`, keys relative to the array, its `zarr.json`
    among them, by the rules of spec/layout.md §4
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#4-copying-an-array>: its document with
    `attributes` as given (`{}` by default) and `dimension_names` as given, in the copy's axis
    order (else the base's), and its chunks' values as they are. With `order`, a permutation of
    its axes as `numpy.transpose` takes one (the copy's axis `i` is the base's axis `order[i]`),
    the array transposed: its shape, chunk grid, dimension names and chunk keys permuted, and a
    `transpose` codec of the inverse order first (for a sharded array, first in the shard's inner
    and index codecs), so that it decodes to the base's values, transposed."""
    doc, chunks = base.array(path)
    n = len(doc["shape"])
    names = doc.get("dimension_names") if dimension_names is None else list(dimension_names)
    if dimension_names is not None and len(names) != n:  # type: ignore[arg-type]
        raise ValueError(f"{len(names)} dimension names for an array of {n} dimensions")  # type: ignore[arg-type]
    if order is not None:
        if not all(type(o) is int for o in order):
            raise ValueError(f"order {list(order)} holds the base's axis indexes, integers (as numpy.transpose takes)")
        order = list(order)
        if sorted(order) != list(range(n)):
            raise ValueError(f"order {order} is not a permutation of the array's {n} axes")
    if order is not None and order != list(range(n)):
        inverse = _inverse(order)
        doc["shape"] = [doc["shape"][o] for o in order]
        grid = doc["chunk_grid"]["configuration"]
        for member in ("chunk_shape", "chunk_shapes"):
            if member in grid:
                grid[member] = [grid[member][o] for o in order]
        codecs = list(doc["codecs"])
        if codecs and codecs[0].get("name") == "sharding_indexed":
            config = codecs[0]["configuration"]
            config["chunk_shape"] = [config["chunk_shape"][o] for o in order]
            config["codecs"] = _with_transpose(list(config["codecs"]), inverse)
            config["index_codecs"] = _with_transpose(list(config["index_codecs"]), [*inverse, n])
        else:
            doc["codecs"] = _with_transpose(codecs, inverse)
        if names is not None and dimension_names is None:
            names = [names[o] for o in order]
        chunks = {_permuted_key(k, order, doc): v for k, v in chunks.items()}
    doc["attributes"] = dict(attributes or {})
    if names is not None:
        doc["dimension_names"] = list(names)
    else:
        doc.pop("dimension_names", None)
    return {"zarr.json": dump(doc), **dict(sorted(chunks.items()))}


def dump(doc: object) -> bytes:
    """A document as zarrify writes one: compact JSON, in UTF-8."""
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _key_ok(key: object) -> bool:
    """Whether `key` is a key a layout may hold: relative, valid UTF-8, with no empty, `.` or `..`
    segment, none that starts with `__`, and `zarr.json` only as its last."""
    if type(key) is not str or not key:
        return False
    try:
        key.encode("utf-8")
    except UnicodeEncodeError:
        return False
    segments = key.split("/")
    return all(s and s not in (".", "..") and not s.startswith("__") for s in segments) and \
        "zarr.json" not in segments[:-1]


def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError(f"an object holds a member twice: {min(k for k in keys if keys.count(k) > 1)!r}")
    return dict(pairs)


def _strings_ok(value: object) -> bool:
    """Whether every string in a parsed document encodes as UTF-8 (no lone surrogate)."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True
    if isinstance(value, dict):
        return all(_strings_ok(k) and _strings_ok(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_strings_ok(v) for v in value)
    return True


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is beyond binary64")
    return value


def _parsed(value: bytes) -> object:
    """A document, parsed as strict JSON in UTF-8: no byte order mark, no NaN or infinities, no
    member twice, no lone surrogate, nesting within Python's recursion limit."""
    text = value.decode("utf-8")  # strict: a UnicodeDecodeError, a ValueError, for other bytes
    if text.startswith("\ufeff"):
        raise ValueError("a byte order mark")
    try:
        doc = json.loads(text, parse_constant=_no_constant, parse_float=_finite, object_pairs_hook=_no_duplicates)
        if not _strings_ok(doc):
            raise ValueError("a string that is not valid Unicode")
    except RecursionError:
        raise ValueError("nested too deep") from None
    return doc


def _check_cmos(where: str, cmos: object, plugin: dict[str, str]) -> None:
    """Checks a document's `zarr_conventions`: a list of objects, none declaring a refkv
    convention, and the plugin's equal to the one its ConventionInfo makes."""
    if not isinstance(cmos, list) or not all(isinstance(c, dict) for c in cmos):
        raise TypeError(f"{where}: zarr_conventions is not a list of objects")
    for cmo in cmos:
        try:
            uuid = str(uuids.UUID(str(cmo.get("uuid")).strip()))
        except ValueError:
            uuid = str(cmo.get("uuid", "")).strip().lower()
        name = cmo.get("name")
        if name is not None and not isinstance(name, str):
            raise TypeError(f"{where}: a CMO's name is not a string")
        if uuid in REFKV_UUIDS or name in REFKV_NAMES or any(_is_refkv_url(cmo.get(m))
                                                             for m in ("schema_url", "spec_url")):
            raise ValueError(f"{where} declares a refkv convention, which refkv alone writes")
        if (uuid == plugin["uuid"] or name == plugin["name"]) and cmo != plugin:
            raise ValueError(f"{where} declares the layout's convention otherwise than its ConventionInfo")


def _check(name: str, entries: dict[Any, Any], base: Base, plugin: dict[str, str], prefix: str = "",
           ) -> dict[str, dict[str, Any]]:
    """Checks a layout's entries against spec/layout.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#3-the-entries>, and returns its
    documents, parsed, by key; LayoutFailed otherwise."""
    def fail(why: str) -> LayoutFailed:
        return LayoutFailed(f"{name}: {why}")

    key = key_of(name)
    docs: dict[str, dict[str, Any]] = {}
    for k, given in list(entries.items()):
        if not _key_ok(k):
            raise fail(f"{k!r} is not a key of a layout (relative, valid UTF-8, with no empty, '.', '..' or "
                       "'__' segment, and zarr.json only last)")
        if len(f"{prefix}{k}".encode()) > MAX_KEY:
            raise fail(f"a key of {len(f'{prefix}{k}'.encode())} bytes, more than an archive's {MAX_KEY}")
        v = bytes(given) if type(given) in (bytes, bytearray) else given  # the very bytes checked are written
        entries[k] = v
        if _is_document(k):
            if type(v) is not bytes:
                raise fail(f"{k}: a document is bytes")
            try:
                doc = _parsed(v)
            except ValueError as e:
                raise fail(f"{k} is not strict JSON in UTF-8: {e}") from e
            if not isinstance(doc, dict) or type(doc.get("zarr_format")) is not int or doc["zarr_format"] != 3 \
                    or doc.get("node_type") not in ("group", "array"):
                raise fail(f"{k} is not a Zarr v3 group or array document")
            if doc["node_type"] == "array" and (any(m not in doc for m in ARRAY_MEMBERS)
                                                 or not isinstance(doc["shape"], list)):
                raise fail(f"{k}: an array's document holds {', '.join(ARRAY_MEMBERS)}, its shape a list")
            docs[k] = doc
        elif type(v) is not bytes and base.base_value(v) is None:
            raise fail(f"{k}: neither bytes nor one of the base's references (a layout copies references, makes none)")
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
        holder = k.rpartition("/")[0]  # type: ignore[union-attr]
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
        for member in REFKV_NAMES:
            if member in attrs:
                raise fail(f"{k} holds {member}, which refkv writes")
        if key in attrs and not isinstance(attrs[key], dict):
            raise fail(f"{k}: the plugin's key {key} does not hold an object")
        if key in attrs and "version" in attrs[key]:
            raise fail(f"{k}: the plugin's key {key} must not hold version: refkv writes it")
        try:
            _check_cmos(k, attrs.get("zarr_conventions", []), plugin)
        except (TypeError, ValueError) as e:
            raise fail(str(e)) from e
    return docs


def _cmo(name: str, convention: ConventionInfo) -> dict[str, str]:
    """A plugin's convention metadata object: the five members the framework allows."""
    return {"uuid": convention.uuid, "schema_url": convention.schema_url, "spec_url": convention.spec_url,
            "name": key_of(name), "description": convention.description}


def _declared(p: Planned, node: dict[str, Any], base: tuple[str, int], source: object) -> dict[str, Any]:
    """The layout node's document, declared (spec/layout.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#3-the-entries>): the refkv_layout CMO,
    then the plugin's, then the others it wrote; its key, holding the plugin's version; and the
    layout property, with the canonical options taken before the plugin ran."""
    name, convention = p.layout.name, p.convention
    mine = _cmo(name, convention)
    attrs = dict(node.get("attributes", {}))
    others = [c for c in attrs.pop("zarr_conventions", []) if c.get("uuid") != mine["uuid"]]
    key = key_of(name)
    own = {"version": convention.version, **attrs.pop(key, {})}
    node["attributes"] = {"zarr_conventions": [CMO, mine, *others], **attrs, key: own, KEY: {
        "version": VERSION,
        "layout": {"name": name, "uuid": convention.uuid, "version": convention.version},
        "source": source,
        "base": {"uuid": base[0], "version": base[1]},
        "options": p.fresh_options(),
    }}
    return node


def join(store: ReferenceStore, ir: Ir | None, planned: Sequence[Planned]) -> ReferenceStore:
    """`store`, a built-in profile's output, with the plugin layouts `planned` added
    (spec/layout.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#3-the-entries>):
    each plugin's `write` run, one at a time on a `Base` of its own, its entries checked, its
    references replaced by the base's own values (checksums included), placed under
    `refkv_layouts/<path>/`, declared and named in the root's `layouts`. Raises LayoutRejected or
    LayoutFailed."""
    if any(k.startswith(f"{NODE}/") for k in store.mapping):
        raise LayoutRejected(f"the base holds {NODE}/, which plugin layouts take")
    root = _root(store)
    prop = root["attributes"]["refkv_zarrified"]
    profile, version = str(prop["profile"]), int(prop["version"])
    declared_base = (PROFILES[profile][0], version)
    added: dict[str, bytes | Range | Concat] = {}
    names: dict[str, str] = {}
    for p in planned:
        name = p.layout.name
        versions = p.formats.get(profile) if profile in FILE_PROFILES else None
        if versions is None or version not in versions:
            reads = ", ".join(f"{k} v{', v'.join(str(v) for v in sorted(vs))}" for k, vs in sorted(p.formats.items())
                              if k in FILE_PROFILES and vs)
            raise LayoutRejected(f"{name} does not read the {profile} profile's convention at version {version} "
                                 f"(it reads {reads or 'no version of any'})")
        base = Base(store, ir)
        try:
            written = p.plugin.write(base, p.fresh_options())
            entries: dict[Any, Any] = dict(written) if isinstance(written, Mapping) else {}
        except Rejected as e:
            raise LayoutRejected(f"{name}: {e}") from e
        except (Exception, SystemExit) as e:  # SystemExit too: it would stop refkv's event loop thread
            raise LayoutFailed(f"{name}: {type(e).__name__}: {e}") from e
        if not isinstance(written, Mapping):
            raise LayoutFailed(f"{name}: write() returned {type(written).__name__}, not a mapping of keys to entries")
        docs = _check(name, entries, base, _cmo(name, p.convention), f"{NODE}/{p.path}/")
        try:
            node = dump(_declared(p, docs["zarr.json"], declared_base, prop["source"]))
        except (TypeError, ValueError) as e:
            raise LayoutFailed(f"{name}: its node's document cannot be written: {e}") from e
        prefix = f"{NODE}/{p.path}"
        for k, v in entries.items():
            # a reference is written as the base holds it, checksums and all
            added[f"{prefix}/{k}"] = base.base_value(v) or v  # type: ignore[assignment]
        added[f"{prefix}/zarr.json"] = node
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
