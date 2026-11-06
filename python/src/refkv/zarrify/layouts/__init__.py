"""Layouts defined outside refkv (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>): a Python package writes the whole
hierarchy of a zarrify call's output itself, built from what a built-in profile read, its output
(the base) and its IR, and refkv writes exactly what it returns, once it has checked that every
reference reads bytes of a source refkv opened and that the archive can hold it.

A plugin is an object registered under the entry-point group `refkv.layouts`, its key the
layout's name, or given to `Layout` itself, with these attributes:

- `name`: the layout's name, `<distribution>.<layout>`, the entry point's key;
- `formats`: `"*"`, or the base profiles it reads, each with the versions of its convention it
  reads (`{"tiff": {0}, "hdf5": {0}}`); names this refkv has no file profile of are ignored;
- `options(given)`: the options in canonical form, a JSON object, or ValueError;
- `write(base, options)`: the output's entries, keys from its root, built from `base` (a
  `Base`), as a mapping or a `Written`; it raises `refkv.zarrify.Rejected` for an input it
  cannot present.

    from refkv.zarrify import zarrify
    from refkv.zarrify.layouts import Layout

    store = zarrify("image.ome.tif", layout=Layout("acme-ome.ome06"))
    store.mapping["zarr.json"]  # the plugin's root

The checks guard against a plugin's mistakes, not against a hostile plugin, which is code
running in the caller's process. `refkv.zarrify.layouts.hierarchy` (the `layouts` extra) builds
and checks Zarr documents; `refkv.zarrify.coding` turns coding parameters into codecs.
"""

from __future__ import annotations

import bisect
import dataclasses
import importlib.metadata
import io
import json
import math
import os
import re
import struct
import types
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol, cast, override

import refkv.ir  # its Ir, read at call time: refkv.ir imports this module's package
import refkv.zarrify as _zarrify  # the zarrify call `base` makes: read at call time (a cycle)
from refkv.ir.kind import Kind
from refkv.ir.store import StoreIrOutput
from refkv.pb import Concat, Range, Source
from refkv.policy import Policy
from refkv.reader import Registry, Resolver
from refkv.refstore import ReferenceStore
from refkv.vzip.names import INDEX_KEY, RESERVED_PREFIX, SOURCES_KEY
from refkv.zarrify.common import MAX_PAYLOAD, PAGE_SIZE, PROFILES, Output, Rejected
from refkv.zarrify.profiles import REGISTRY

if TYPE_CHECKING:
    from refkv.ir import Ir

__all__ = ["FILE_PROFILES", "MAX_PAYLOAD", "MAX_READS", "MIRROR", "Base", "Entries", "EntryValue", "Layout",
           "LayoutFailed", "LayoutRejected", "LayoutUsageError", "Plugin", "Written", "base", "base_async",
           "chunk_coords", "chunk_key", "concat", "cut", "dump", "encode_entries", "fill_value", "payload_size",
           "without_checksums"]

type EntryValue = bytes | Range | Concat  # a value of an output's entry
type Entries = dict[str, EntryValue]  # an output's entries, keys from its root, as a plugin builds them

GROUP = "refkv.layouts"  # the entry points' group
# the base's IR mirror's node (spec/conventions.md §8 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#8-the-ir-mirror>)
MIRROR = "refkv_source"
FILE_PROFILES = frozenset(str(e["name"]) for e in REGISTRY if e["kind"] in ("file", "both"))
REFKV_UUIDS = frozenset(str(e["uuid"]).lower() for e in REGISTRY)
REFKV_MEMBERS = ("refkv_zarrified", "refkv_provenance")  # what refkv declares on a node
NAME = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")

# bytes of a key's UTF-8 the ZIP binding holds (spec/bindings/vzip.md §10.3 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#103-file-names>)
MAX_KEY = 65535
MAX_OPEN = 256 << 20  # bytes read at open, and of a format entry: the default max_format_entry
# url ranges of one value: the default reads per `get` (spec/refkv.md §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>
# rule 5)
MAX_READS = 1024
MERGE_GAP = 1 << 16  # ranges of one source this close are one read, as refkv's readers merge them
# a DEFLATE stream that could reach this is not written (spec/bindings/vzip.md §14.1
# <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#141-requirements>)
STORED = 0xFFFFFFFF
MAX_OFFSET = (1 << 64) - 1
PAGE_ENTRY = 16  # the most a phkv page index's entry takes beside its first key: three varints, and room
CRC_FIELD = 6  # the most bytes a range's crc32c field takes: a tag, and a varint of 32 bits
# the documents a Zarr reader parses as JSON, by a key's last segment
DOCUMENTS = frozenset(["zarr.json", ".zgroup", ".zarray", ".zattrs", ".zmetadata"])
STRUCT, VALUE, DATA, DERIVED, ALIAS, OBJECT = Kind.STRUCT, Kind.VALUE, Kind.DATA, Kind.DERIVED, Kind.ALIAS, Kind.OBJECT


class LayoutUsageError(ValueError):
    """A plugin layout asked for wrongly: a name no plugin has, a plugin that fails to load or
    breaks the rules of spec/layout.md, options it refuses, or a layout on a store input."""


class LayoutRejected(Rejected):
    """The layout request is refused for this input: a base the plugin does not read, or a
    `Rejected` the plugin raised. The input is one the profile accepts; no output is written."""


class LayoutFailed(Exception):  # noqa: N818 (public name, beside Rejected)
    """The plugin failed: its `write` raised, or its output broke a rule of spec/layout.md."""


@dataclass(frozen=True)
class Layout:
    """A plugin layout to write: the plugin, by its entry point's name or as the plugin object
    itself (an instance, or a class refkv instantiates with no arguments), and its `options` (a
    mapping, which the plugin puts in canonical form)."""

    plugin: object
    options: Mapping[str, object] | None = None


class Plugin(Protocol):
    """What a plugin provides (the module's docstring)."""

    name: str
    formats: Mapping[str, Iterable[int]] | str

    def options(self, given: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def write(self, base: Base, options: Mapping[str, Any]) -> Mapping[str, bytes | Range | Concat] | Written: ...


@dataclass(frozen=True)
class Written:
    """What `write` may return in place of its entries: `entries`, keys from the output's root;
    `read_at_open`, the keys of bytes entries a vzip reader is given when it opens the archive
    (None: refkv's default, spec/layout.md); `stored`, the keys of bytes entries kept
    uncompressed. A phkv store ignores both."""

    entries: Mapping[str, EntryValue]
    read_at_open: Iterable[str] | None = None
    stored: Iterable[str] = ()


def _text(e: BaseException) -> str:
    """An exception's message, or `described(e)` when its `str()` raises."""
    try:
        return str(e)
    except BaseException:  # noqa: BLE001 (a plugin's exception, whatever its __str__ does)
        return described(e)


def _type_name(value: object) -> str:
    """The name of `value`'s type, even one whose metaclass's `__name__` raises."""
    try:
        return str(type(value).__name__)
    except BaseException:  # noqa: BLE001 (a plugin's metaclass, whatever it does)
        return str(type.__dict__["__name__"].__get__(type(value)))


def _shown_value(value: object) -> str:
    """`repr(value)` of a str, or the name of `value`'s type: a plugin's object's repr is its code."""
    return repr(value) if type(value) is str else f"a {_type_name(value)}"


def described(e: BaseException) -> str:
    """An exception as a message names it, `Type: text`, even one whose `str()` raises."""
    try:
        text = str(e)
    except BaseException:  # noqa: BLE001 (a plugin's exception, whatever its __str__ does)
        text = "(its message cannot be read)"
    return f"{_type_name(e)}: {text}"


def dump(doc: object, *, allow_nan: bool = False) -> bytes:
    """A document as zarrify writes one: compact, strict JSON in UTF-8 (ValueError for NaN, an
    infinity, or anything else strict JSON cannot hold). `allow_nan` writes `NaN` and
    `Infinity` bare, as zarr-python writes them in a Zarr v2 `.zattrs`, which `check` then
    reports under `json`."""
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False, allow_nan=allow_nan).encode()


def encode_entries(entries: Mapping[str, EntryValue], *, checksums: bool = False) -> bytes:
    """An output's entries as bytes, one way only (keys sorted, each value tagged and encoded): for
    comparing two outputs entry for entry, as a plugin's determinism test does across processes
    (spec/layout.md §7 <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#7-determinism>).
    Checksums are left out, as outputs are compared, unless `checksums`."""
    out = bytearray()
    for k in sorted(entries):
        v = entries[k]
        if isinstance(v, (bytes, bytearray, memoryview)):
            tag, body = b"b", bytes(v)
        else:
            tag, body = b"c" if isinstance(v, Concat) else b"r", (v if checksums else _without_checksum(v)).encode()
        key = k.encode()
        out += tag + len(key).to_bytes(4, "little") + key + len(body).to_bytes(8, "little") + body
    return bytes(out)


# ---- loading a plugin (spec/layout.md, names and formats)

def _normalized(distribution: str) -> str:
    """A distribution's name, normalized as PEP 503 says."""
    return re.sub(r"[-_.]+", "-", distribution).lower()


def _entry_points() -> Sequence[importlib.metadata.EntryPoint]:
    """The installed plugins' entry points."""
    return tuple(importlib.metadata.entry_points(group=GROUP))


def _usage(name: object, why: str) -> LayoutUsageError:
    return LayoutUsageError(f"{name}: {why}")


def load(name: str) -> Plugin:
    """The plugin whose entry point is named `name`, loaded; or LayoutUsageError."""
    if NAME.fullmatch(name) is None:
        raise LayoutUsageError(f"{name!r} is not a layout's name, <distribution>.<layout> in [a-z0-9-]")
    try:  # importlib.metadata reads every distribution's entry_points.txt, and one malformed raises
        points = list(_entry_points())
    except Exception as e:
        raise LayoutUsageError(f"the installed entry points of {GROUP!r} cannot be read: {described(e)}") from e
    found = [e for e in points if e.name == name]
    if not found:
        installed = ", ".join(sorted(str(e.name) for e in points)) or "none"
        raise LayoutUsageError(f"no layout plugin is named {name!r} (the entry points of {GROUP!r}: {installed})")
    if len(found) > 1:
        raise _usage(name, f"{len(found)} entry points provide it")
    entry = found[0]
    dist_name = getattr(getattr(entry, "dist", None), "name", None)
    if not isinstance(dist_name, str) or _normalized(dist_name) != name.split(".", 1)[0]:
        given = "no distribution" if not isinstance(dist_name, str) else f"the distribution {dist_name!r}"
        raise _usage(name, f"it is provided by {given}: a layout's name starts with its distribution's")
    try:  # SystemExit too (an argparse at import, say): it would stop refkv's event loop thread
        plugin = entry.load()
    except BaseException as e:  # any of a plugin's, made a usage error; an ImportError names its extra
        raise _usage(name, f"the plugin failed to load: {described(e)}") from e
    if isinstance(plugin, types.ModuleType):
        raise _usage(name, f"the entry point names the module {plugin.__name__}: name the plugin's class")
    return plugin  # type: ignore[no-any-return]


def _instance(name: object, plugin: object) -> Plugin:
    """`plugin`, or an instance of it when it is a class."""
    if not isinstance(plugin, type):
        return plugin  # type: ignore[return-value]
    try:
        return plugin()  # type: ignore[no-any-return]
    except BaseException as e:
        raise _usage(name, f"the plugin's class failed to instantiate: {described(e)}") from e


def _formats(name: object, formats: object) -> frozenset[str] | dict[str, frozenset[int]]:
    """A plugin's `formats` checked: every file profile (`"*"`), or the versions of each
    profile it reads; or LayoutUsageError."""
    if formats == "*":
        return FILE_PROFILES
    why = 'formats is "*", or a mapping of profile names to iterables of convention versions (int)'
    if not isinstance(formats, Mapping):
        raise _usage(name, why)
    try:
        read = {k: frozenset(vs) for k, vs in formats.items()}
    except TypeError as e:
        raise _usage(name, why) from e
    if not all(isinstance(k, str) and all(type(v) is int and v >= 0 for v in vs) for k, vs in read.items()):
        raise _usage(name, why)
    if not set(read) & FILE_PROFILES:  # a later refkv's names are ignored, but one must be this one's
        raise _usage(name, f"formats names no file profile of this refkv ({', '.join(sorted(read)) or 'none'}); "
                           f"they are {', '.join(sorted(FILE_PROFILES))}")
    return read


@dataclass(frozen=True)
class Planned:
    """A layout asked for: its plugin loaded and checked, its name, the formats it reads, and its
    options in canonical form (as JSON text, so that each `write` gets a copy of its own)."""

    plugin: Plugin
    name: str
    formats: frozenset[str] | dict[str, frozenset[int]]
    options: str

    def fresh_options(self) -> dict[str, Any]:
        """The canonical options, a new copy."""
        return json.loads(self.options)

    def reads(self, profile: str, version: int) -> bool:
        """Whether the plugin reads a base of `profile`'s convention at `version`."""
        if isinstance(self.formats, frozenset):
            return profile in self.formats
        return version in self.formats.get(profile, frozenset())


def _str_keys(value: object) -> bool:
    """Whether every mapping in `value` names its members by strings, walked without recursion."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            if not all(isinstance(k, str) for k in item):
                return False
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return True


def _strings_ok(value: object) -> bool:
    """Whether every string in a parsed JSON value encodes as UTF-8 (no lone surrogate)."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return False
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return True


def plan(layout: object) -> Planned:
    """The plugin layout of a call, loaded and checked before the input is opened, or
    LayoutUsageError (LayoutFailed for an `options()` that raises other than ValueError)."""
    if not isinstance(layout, Layout):
        raise LayoutUsageError(f"layout is one Layout, not {type(layout).__name__}")
    if layout.options is not None and not isinstance(layout.options, Mapping):
        raise LayoutUsageError(f"a layout's options are a mapping (a JSON object), not "
                               f"{type(layout.options).__name__}")
    given_plugin = layout.plugin
    if isinstance(given_plugin, str):
        shown = given_plugin
        plugin = _instance(shown, load(given_plugin))
        from_entry = True
    else:  # named by its class in messages: its own str() is the plugin's code
        shown = f"the plugin object of {_type_name(given_plugin)}"
        plugin = _instance(shown, given_plugin)
        from_entry = False
    try:
        name = getattr(plugin, "name", None)
        formats = getattr(plugin, "formats", None)
        options_of, write = getattr(plugin, "options", None), getattr(plugin, "write", None)
    except BaseException as e:  # an attribute that raises when read
        raise _usage(shown, f"the plugin's attributes cannot be read: {described(e)}") from e
    if type(name) is not str or NAME.fullmatch(name) is None:  # a str subclass's methods are the plugin's code
        raise _usage(shown, f"its name {_shown_value(name)} is not <distribution>.<layout> in [a-z0-9-]")
    if from_entry and name != given_plugin:
        raise _usage(shown, f"the plugin names itself {_shown_value(name)}: set its name to the entry point's key")
    try:  # a plugin's own mapping, or iterables, may raise anything as they are read
        read = _formats(name, formats)
    except LayoutUsageError:
        raise
    except BaseException as e:
        raise _usage(name, f"its formats cannot be read: {described(e)}") from e
    if not callable(options_of) or not callable(write):
        raise _usage(name, "a layout plugin has options() and write()")
    try:  # first, as it refuses a cycle, which a walk of the members would follow for ever
        given = json.loads(json.dumps(dict(layout.options or {}), allow_nan=False))
        named = _str_keys(layout.options or {})
    except Exception as e:  # the caller's mapping, whatever it raises as it is read
        raise _usage(name, f"its options are not a JSON object: {described(e)}") from e
    if not named:
        raise _usage(name, "its options' members are named by strings, at every depth")
    try:
        options = options_of(given)
    except ValueError as e:  # the plugin's refusal of the caller's options
        raise _usage(name, f"its options are refused: {described(e)}") from e
    except BaseException as e:  # any other of a plugin's (a bug in options(), not the caller's)
        raise LayoutFailed(f"{name}: options() raised {described(e)}") from e
    if not isinstance(options, Mapping):
        raise _usage(name, f"its options() returned {type(options).__name__}, not a mapping")
    try:  # canonical options are JSON as they are: no NaN, no tuples, no lone surrogate
        text = json.dumps(dict(options), allow_nan=False)
        same = json.loads(text) == dict(options) and _strings_ok(dict(options))
    except Exception as e:
        raise _usage(name, f"its canonical options are not a JSON object: {described(e)}") from e
    if not same:
        raise _usage(name, "its canonical options are not JSON as they are (a tuple, say, or a lone surrogate): "
                           "options() returns lists, dicts, strings, numbers and booleans")
    return Planned(plugin, name, read, text)


# ---- the base (spec/layout.md, what a plugin is given)

def _without_checksum(value: Range | Concat) -> Range | Concat:
    """A reference value as spec/zarrify.md §1.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#11-output-and-equivalence> compares
    it: its ranges without checksums."""
    if isinstance(value, Range):
        return dataclasses.replace(value, crc32c=None)
    return Concat(tuple(dataclasses.replace(r, crc32c=None) for r in value.parts))


def _is_document(key: str) -> bool:
    return key.rpartition("/")[2] == "zarr.json"


def _prefix(path: str) -> str:
    return f"{path}/" if path else ""


class Base:
    """The built-in profile's output as a plugin sees it (spec/layout.md
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md>): every layout the format has, its
    root as refkv writes it, no checksums, and the IR it was projected from, with the bridges
    between them. Each accessor returns new objects; one that is given a key, path or element the
    base does not have raises KeyError naming it.

    - `profile`, `version`, `uuid`: the profile that read the input (a TIFF the parser found to
      be NDPI is `ndpi`), and its convention's version and UUID; `checksums`, whether the call
      asks for them;
    - `ir`: the IR (`refkv.ir.Ir`); `reference`, `form`, `element_at` and `elements` read it;
    - `sources`, `zero_source`: the source table, and its zero data source;
    - `keys()`, `document(key)`, `attributes(path)`, `source_metadata(path)`, `array(path)`,
      `entries(prefix)`: the output's entries."""

    def __init__(self, store: ReferenceStore, ir: Ir | None, *, checksums: bool = False,
                 objects: Sequence[str] = (), sizes: Sequence[int | None] | None = None) -> None:
        self._values: dict[str, bytes | Range | Concat] = {
            k: _without_checksum(v) if isinstance(v, (Range, Concat)) else v for k, v in store.mapping.items()}
        # each reference value without its checksum, to the base's own value, which refkv writes
        self._references = {self._values[k]: v for k, v in store.mapping.items() if isinstance(v, (Range, Concat))}
        self._sorted = sorted(self._values)  # for an array's keys, found by prefix
        self._read_at_open = store.read_at_open
        self._sources = tuple(store._sources)  # noqa: SLF001 (the base's table)
        # each url source's size, as refkv found it: given, else its pin
        self._sizes = tuple(sizes) if sizes is not None else tuple(s.size for s in self._sources)
        root = json.loads(self._values["zarr.json"])  # type: ignore[arg-type]
        prop = root["attributes"]["refkv_zarrified"]
        self._profile: str = prop["profile"]
        self._version: int = prop["version"]
        self._checksums = checksums
        self._ir = ir
        self._objects = list(objects)  # a store IR's object keys, by source index
        self._index: dict[int, tuple[list[int], list[tuple[int, int, int, int | None]]]] | None = None
        self._objects_of: dict[int, int] | None = None
        self._holders: dict[Range | Concat, list[str]] | None = None

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
    def checksums(self) -> bool:
        return self._checksums

    @property
    def ir(self) -> Ir:
        if self._ir is None:  # a base built by hand, in a test
            raise ValueError("this base has no IR")
        return self._ir

    @property
    def sources(self) -> tuple[Source, ...]:
        return self._sources

    @property
    def zero_source(self) -> tuple[int, int] | None:
        """(index, length) of the base's zero data source, or None (spec/zarrify.md §1.2
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>)."""
        for i, s in enumerate(self._sources):
            if s.data and not s.data.strip(b"\0"):
                return i, len(s.data)
        return None

    def keys(self) -> list[str]:
        """The base's keys, sorted."""
        return list(self._sorted)

    def __contains__(self, key: object) -> bool:
        return key in self._values

    @property
    def datasets(self) -> dict[str, str]:
        """The base's dataset layouts (spec/conventions.md §9
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>), by the
        name the root gives each, each its node's path; `{}` for a format that has none."""
        prop = json.loads(self._values["zarr.json"])["attributes"]["refkv_zarrified"]  # type: ignore[arg-type]
        named = prop.get("layouts") if isinstance(prop.get("layouts"), dict) else {}
        return {str(k): str(v) for k, v in named.items()}

    @property
    def mirror(self) -> str | None:
        """The path of the base's IR mirror (spec/conventions.md §8
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#8-the-ir-mirror>), `MIRROR`, or None."""
        return MIRROR if f"{MIRROR}/zarr.json" in self._values else None

    def _left_out(self, mirror: bool, datasets: bool) -> list[str]:
        out = [] if mirror or self.mirror is None else [MIRROR]
        return out if datasets else out + list(self.datasets.values())

    def nodes(self, *, mirror: bool = False, datasets: bool = False) -> list[str]:
        """The paths of the base's nodes (the root is `""`), sorted: its image layout's, and the
        IR mirror's with `mirror`, the dataset layouts' with `datasets`."""
        left = self._left_out(mirror, datasets)
        out = []
        for k in self._sorted:
            if not _is_document(k):
                continue
            path = k[: -len("zarr.json")].rstrip("/")
            if not any(path == p or path.startswith(f"{p}/") for p in left):
                out.append(path)
        return sorted(out)

    def node_type(self, path: str) -> str | None:
        """`"group"` or `"array"` for the base's node at `path`, None for a path it has no node at."""
        value = self._values.get(f"{_prefix(path)}zarr.json")
        return json.loads(value).get("node_type") if isinstance(value, bytes) else None

    def document(self, key: str) -> dict[str, Any]:
        """The JSON document at `key` (a `zarr.json`), parsed."""
        value = self._values.get(key)
        if not _is_document(key) or not isinstance(value, bytes):
            raise KeyError(f"no document {key!r} in the base (a document's key ends in zarr.json)")
        return json.loads(value)  # type: ignore[no-any-return]

    def _node(self, path: str) -> dict[str, Any]:
        try:
            return self.document(f"{_prefix(path)}zarr.json")
        except KeyError:
            raise KeyError(f"no node {path!r} in the base") from None

    def attributes(self, path: str) -> dict[str, Any]:
        """The attributes of the node at `path` without refkv's declarations: no
        `refkv_zarrified` or `refkv_provenance`, and no CMO of refkv's conventions in
        `zarr_conventions` (dropped when nothing is left in it)."""
        attrs = dict(self._node(path).get("attributes", {}))
        for member in REFKV_MEMBERS:
            attrs.pop(member, None)
        cmos = attrs.get("zarr_conventions")
        if isinstance(cmos, list):
            kept = [c for c in cmos if not (isinstance(c, dict) and (
                str(c.get("uuid", "")).lower() in REFKV_UUIDS or c.get("name") in REFKV_MEMBERS))]
            if kept:
                attrs["zarr_conventions"] = kept
            else:
                del attrs["zarr_conventions"]
        return attrs

    def source_metadata(self, path: str) -> Any:  # noqa: ANN401 (JSON the format's convention specifies)
        """The source metadata the node at `path` declares: the member named after the profile its
        `refkv_zarrified` names (the base's, or a dataset layout's own) (spec/conventions.md §2
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>), or None."""
        prop = self._node(path).get("attributes", {}).get("refkv_zarrified")
        if not isinstance(prop, dict):
            return None
        # a node of a dataset layout declares its own profile (`mrc-dataset`), and so names its member
        return prop.get(prop.get("profile", self._profile))

    def array(self, path: str) -> tuple[dict[str, Any], dict[str, bytes | Range | Concat]]:
        """The array at `path`: its document, and its chunks by key relative to the array."""
        doc = self._node(path)
        if doc.get("node_type") != "array":
            raise KeyError(f"{path!r} is a group of the base, not an array")
        prefix = _prefix(path)
        chunks: dict[str, bytes | Range | Concat] = {}
        for k in self._sorted[bisect.bisect_left(self._sorted, prefix):]:
            if not k.startswith(prefix):
                break
            if not _is_document(k[len(prefix):]):
                chunks[k[len(prefix):]] = self._values[k]
        return doc, chunks

    def entries(self, prefix: str = "", *, declarations: bool = True, mirror: bool = True,
                datasets: bool = True) -> Entries:
        """The base's entries whose keys start with `prefix`, keys as the base holds them:
        `out |= base.entries()` keeps the whole of refkv's output, `base.entries("refkv_source/")`
        the IR mirror. Without `declarations`, each `zarr.json` is written with its node's
        `attributes`, refkv's declarations left out, as `dump` writes it; without `mirror` or
        `datasets`, the IR mirror's or the dataset layouts' entries are left out."""
        keys = [k for k in self._sorted[bisect.bisect_left(self._sorted, prefix):] if k.startswith(prefix)] \
            if prefix else self._sorted
        left = tuple(f"{p}/" for p in self._left_out(mirror, datasets))
        out: Entries = {}
        for k in keys:
            if left and k.startswith(left):
                continue
            v = self._values[k]
            if not declarations and _is_document(k):
                doc = json.loads(v)  # type: ignore[arg-type]
                doc["attributes"] = self.attributes(k[: -len("zarr.json")].rstrip("/"))
                v = dump(doc)
            out[k] = v
        return out

    def _arrays_holding(self, value: Range | Concat) -> list[str]:
        """The paths of the base's arrays that hold the reference `value` (without checksums)."""
        if self._holders is None:
            arrays = {k[: -len("zarr.json")].rstrip("/") for k in self._sorted if _is_document(k)
                      and json.loads(self._values[k]).get("node_type") == "array"}  # type: ignore[arg-type]
            holders: dict[Range | Concat, list[str]] = {}
            for k, v in self._values.items():
                if isinstance(v, (Range, Concat)):
                    holder = k.rpartition("/")[0]
                    while holder and holder not in arrays:
                        holder = holder.rpartition("/")[0]
                    paths = holders.setdefault(v, [])
                    if holder in arrays and holder not in paths:  # a reference outside every array is no chunk
                        paths.append(holder)
            self._holders = holders
        return self._holders.get(_without_checksum(value), [])

    def base_value(self, value: object) -> Range | Concat | None:
        """The base's reference value equal to `value` but for checksums, or None."""
        if type(value) is not Range and type(value) is not Concat:
            return None
        try:
            return self._references.get(_without_checksum(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):  # a Range of unhashable members
            return None

    # ---- the IR

    def _element(self, i: object) -> int:
        if type(i) is not int or not 0 <= i < len(self.ir):
            raise KeyError(f"no element {i!r} in the IR")
        return i

    def members(self, i: int) -> list[tuple[int, int, int]]:
        """Element `i`'s data members (an alias's are its target's), each `(number, start,
        length)`: its name index (a TIFF tile's number), and its bytes' extent, in its object for a
        store IR. A member that is missing (a tile of byte count 0, or one outside the file) has
        none: `numbers` the tags give and those listed differ."""
        numbers, starts, lengths, _ = self.ir.members(self._target(self._element(i)))
        return list(zip(numbers, starts, lengths, strict=True))

    def _member(self, i: int, member: object) -> tuple[int, int, int]:
        """(start, length, form id) of element `i`'s data member `member`."""
        _, starts, lengths, forms = self.ir.members(self._target(i))
        if type(member) is not int or not 0 <= member < len(starts):
            raise KeyError(f"element {i} ({self.ir.path(i)!r}) has no data member {member!r}")
        return starts[member], lengths[member], forms[member]

    def form(self, i: int, member: int | None = None) -> dict[str, Any] | None:
        """The form of element `i` (or of its data member `member`), parsed (its `geometry`,
        `codec` and `recipe`), or None for one without a form (spec/conventions.md §8.5
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes>)."""
        i = self._element(i)
        text = self.ir.form(i) if member is None else self.ir.forms()[self._member(i, member)[2]]
        return None if text is None else json.loads(text)

    def _target(self, i: int) -> int:
        """`i`, or what it is an alias of."""
        seen = set()
        while self.ir.kind(i) == ALIAS and i not in seen:
            seen.add(i)
            t = self.ir.target(i)
            if t < 0:
                break
            i = t
        return i

    def _object(self, i: int) -> int | None:
        """The object element (a file of a file read as a store, spec/conventions.md §8.9
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>) that holds
        element `i`, or None for a file IR."""
        if not self.ir.object_bases():
            return None
        if self._objects_of is None:
            self._objects_of = {}
        at = i
        while at >= 0:
            if self.ir.kind(at) == OBJECT:
                return at
            at = self.ir.parent(at)
        return None

    def source_of(self, i: int) -> int:
        """The index in the base's table of the source that holds element `i`'s bytes: 0 for a file
        IR, the source of its object for a store IR. ValueError for an object the base
        references no byte of, which spec/zarrify.md §1.9
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>
        leaves out of the table."""
        o = self._object(self._target(self._element(i)))
        if o is None:
            if self.ir.object_bases():
                raise ValueError(f"{self.ir.path(i)!r} is in no object of the store")
            return 0
        key = self.ir.path(o)
        if key not in self._objects:
            raise ValueError(f"the object {key!r} is not in the base's table, which references none of its bytes "
                             "(spec/zarrify.md §1.9 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>)")
        return self._objects.index(key)

    def _data_part(self, data: bytes) -> Range:
        """A range of the base's data source of `data`, or `data` as a literal."""
        for k, s in enumerate(self._sources):
            if s.data == data:
                return Range(k, 0, len(data))
        return Range(data=data)

    def reference(self, i: int, member: int | None = None) -> Range | Concat:
        """The reference value that reads element `i`'s bytes (or its data member `member`'s) as
        the profile reads them: a Range of the source that holds them, or a Concat of its form's
        recipe (spec/layout.md). ValueError for an element that holds no bytes of a source."""
        i = self._element(i)
        path = self.ir.path(i)
        if member is not None:  # an alias's members are its target's
            self._source_space(self._target(i))
            start, length, form_id = self._member(self._target(i), member)
            text: str | None = self.ir.forms()[form_id]
        elif self.ir.kind(i) == ALIAS:  # its own extent, read by its own form
            start, length = self.ir.extent(i)
            t = self._target(i)
            ts, tn = self.ir.extent(t)
            if t == i or not (ts <= start and start + length <= ts + tn):
                raise ValueError(f"{path!r} is an alias whose extent is not within its target's")
            text = self.ir.form(i) or self.ir.form(t)
        else:
            kind = self.ir.kind(i)
            if kind == DERIVED or self.ir.space(i) != 0:
                raise ValueError(f"{path!r} is in a derived space (the inflated bytes of another element): "
                                 "no range of a source reads it")
            if kind not in (DATA, VALUE):
                raise ValueError(f"{path!r} is not data or a value (its kind is {kind}): it holds no bytes of its own")
            start, length = self.ir.extent(i)
            text = self.ir.form(i)
        form = None if text is None else json.loads(text)
        codec = form.get("codec") if isinstance(form, dict) else None
        if isinstance(codec, dict) and codec.get("mask"):
            raise ValueError(f"{path!r}: its form skips filters (mask {codec['mask']}), so it decodes otherwise than "
                             "its siblings: copy its array from the base")
        source = self.source_of(i)  # a store IR's extents are its object's own offsets
        recipe = form.get("recipe") if isinstance(form, dict) else None
        if not recipe:
            return Range(source, start, length)
        shared = self.ir.shared()
        parts: list[Range] = []
        # the parts of spec/conventions.md §8.5 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes>
        for step in recipe:
            if step[0] == "src":
                o, n = int(step[1]), step[2]
                parts.append(Range(source, start + o, length - o if n is None else int(n)))
            elif step[0] == "rows":  # c runs of n bytes, the first at o, each s after the one before
                o, stride, n, c = (int(x) for x in step[1:5])
                parts += [Range(source, start + o + k * stride, n) for k in range(c)]
            elif step[0] == "shared":
                parts.append(self._data_part(bytes(shared[int(step[1])])))
            else:
                raise ValueError(f"{path!r}: its recipe has a part {step[0]!r} this refkv does not read")
        return parts[0] if len(parts) == 1 else Concat(tuple(parts))

    def _space_index(self, source: int) -> tuple[list[int], list[tuple[int, int, int, int | None]]]:
        """The data elements of source `source` and the data members no element covers, by start:
        (starts, entries of (start, end, element, member))."""
        if self._index is None:
            ir = self.ir
            by: dict[int, list[tuple[int, int, int, int | None]]] = {}
            covered: set[tuple[int, int, int]] = set()
            kinds = ir.columns()["kind"]

            def source_or_none(i: int) -> int | None:
                try:
                    return self.source_of(i)
                except ValueError:
                    return None
            for i in range(len(ir)):
                if kinds[i] == DATA and ir.space(i) == 0 and (s := source_or_none(i)) is not None:
                    a, n = ir.extent(i)
                    by.setdefault(s, []).append((a, a + n, i, None))
                    covered.add((s, a, n))
            for i in range(len(ir)):
                if kinds[i] == STRUCT and ir.space(i) == 0 and (s := source_or_none(i)) is not None:
                    _, starts, lengths, _forms = ir.members(i)
                    for k, (a, n) in enumerate(zip(starts, lengths, strict=True)):
                        if (s, a, n) not in covered:
                            by.setdefault(s, []).append((a, a + n, i, k))
            self._index = {}
            for s, spans in by.items():
                spans.sort(key=lambda t: (t[0], t[1]))
                self._index[s] = ([t[0] for t in spans], spans)
        return self._index.get(source, ([], []))

    def element_at(self, source: int, offset: int) -> tuple[int, int | None] | None:
        """(element, member) of the IR holding byte `offset` of the base's source `source`: the
        innermost data element (member None), else a struct's data member no element covers;
        None for a byte no data holds, and KeyError for a source the table does not have."""
        if type(source) is not int or not 0 <= source < len(self._sources):
            raise KeyError(f"no source {source!r} in the base's table")
        if self._sources[source].url is None:
            return None
        starts, spans = self._space_index(source)
        k = bisect.bisect_right(starts, offset) - 1
        while k >= 0:
            s, e, i, member = spans[k]
            if s <= offset < e:
                return i, member
            if e <= offset and (k == 0 or spans[k - 1][1] <= offset):
                break
            k -= 1
        return None

    def span(self, i: int, member: int | None = None) -> Range:
        """The bytes element `i` (or its data member `member`) spans in its source, as one Range,
        no recipe applied: what `elements`' offsets are offsets into, so that `cut(base.span(i,
        m), start, stop)` reads a part's bytes whatever the element's form (a JPEG tile's span
        is its own bytes, without the tables its `reference` splices in). ValueError for an
        element that holds no bytes of a source."""
        i = self._element(i)
        if member is not None:  # an alias's members are its target's
            t = self._target(i)
            self._source_space(t)
            start, length, _ = self._member(t, member)
            return Range(self.source_of(i), start, length)
        self._source_space(i)
        if self.ir.kind(i) not in (DATA, VALUE, ALIAS):
            raise ValueError(f"{self.ir.path(i)!r} is not data or a value: it holds no bytes of its own")
        start, end = self._span_end(i, None)
        return Range(self.source_of(i), start, end - start)

    def _source_space(self, i: int) -> None:
        """ValueError for an element in a derived space (its extent in the inflated bytes of
        another), or a derived one, whose bytes and members no range of a source reads."""
        if self.ir.kind(i) == DERIVED or self.ir.space(i) != 0:
            raise ValueError(f"{self.ir.path(i)!r} is in a derived space (the inflated bytes of another element): "
                             "no range of a source reads it")

    def _span_end(self, i: int, member: int | None) -> tuple[int, int]:
        """(start, end) of element `i`'s bytes, or of its data member `member`'s."""
        if member is not None:
            _, starts, lengths, _ = self.ir.members(i)
            return starts[member], starts[member] + lengths[member]
        a, n = self.ir.extent(i)
        return a, a + n

    def elements(self, path: str) -> dict[str, tuple[tuple[int, int | None, int, int] | Range | None, ...]]:
        """For every chunk of the base's array at `path` (keys relative to the array), the bytes its
        value reads, in order, by the element that holds them: (element, member, start, stop), the
        bytes of it read, half open, offsets within the element's (or member's) own bytes, its
        `span` (not its `reference`, which a recipe may change), one item for each
        element a part runs across (a chunk of strips the profile joined is several); None for a
        literal part or a part of a data source; a Range, of the part, for bytes of a url source
        that no data element holds (an HDF5 dataset's external storage)."""
        _, chunks = self.array(path)
        out: dict[str, tuple[tuple[int, int | None, int, int] | Range | None, ...]] = {}
        for key, value in chunks.items():
            items: list[tuple[int, int | None, int, int] | Range | None] = []
            for r in (() if isinstance(value, bytes) else _parts(value)):
                if r.data is not None or self._sources[r.source].url is None:
                    items.append(None)
                    continue
                at, end = r.offset, r.offset + r.length
                while True:
                    held = self.element_at(r.source, at)
                    if held is None:  # bytes no data element holds, up to the next one that does
                        starts, _ = self._space_index(r.source)
                        k = bisect.bisect_right(starts, at)
                        stop = min(end, starts[k]) if k < len(starts) else end
                        items.append(Range(r.source, at, max(stop - at, 0)))
                    else:
                        i, member = held
                        begin, finish = self._span_end(i, member)
                        stop = min(end, finish)
                        items.append((i, member, at - begin, stop - begin))
                    if stop >= end:
                        break
                    at = stop
            out[key] = tuple(items)
        return out

    def zeros(self, n: int) -> Range:
        """A range of `n` zero bytes of a data source: the base's zero data source when it is at
        least that long, else of source `len(base.sources)`, the zero data source refkv adds
        after the table's sources when the output references it, as long as the most bytes the
        output reads of it (spec/layout.md §4
        <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#4-writing-the-output>), for
        padding a chunk at an array's edge. A range of that source is the same whatever `Base`
        made it."""
        if type(n) is not int or n < 0:
            raise ValueError(f"{n!r} zero bytes: a natural number of them")
        zero = self.zero_source
        if zero is not None and zero[1] >= n:
            return Range(zero[0], 0, n)
        return Range(len(self._sources), 0, n)  # the source after the table's: refkv adds it

    def fits(self, value: object) -> str | None:
        """Why refkv would not write `value` in this base's output (spec/layout.md §4
        <https://github.com/d-v-b/refkv/blob/main/spec/layout.md#4-writing-the-output>), or None:
        a reference value whose ranges lie outside the base's sources, whose payload could pass
        65519 bytes once checksummed, or that a reader would read in more than 1024 reads."""
        why = _value_problem(value, self)
        if why is None and type(value) in (Range, Concat) and self.base_value(value) is None:
            v: Range | Concat = value  # type: ignore[assignment]
            reads = _reads(v, self)
            if reads > MAX_READS:
                why = f"it reads {reads} ranges, more than the {MAX_READS} a reader makes for one value by default"
            elif _payload(v, self, self._checksums) > MAX_PAYLOAD:
                why = f"a payload of up to {_payload(v, self, self._checksums)} bytes, more than {MAX_PAYLOAD}"
        return why


# ---- chunk keys (Zarr v3's chunk key encodings, and Zarr v2's dimension separator)

def _encoding(doc: Mapping[str, Any]) -> tuple[str, str]:
    """An array document's chunk key encoding: its name and separator (a Zarr v2 `.zarray`'s
    `dimension_separator` as the `v2` encoding's)."""
    if doc.get("zarr_format") == 2:
        return "v2", str(doc.get("dimension_separator") or ".")
    encoding = doc.get("chunk_key_encoding") or {"name": "default"}
    config = encoding.get("configuration") or {} if isinstance(encoding, dict) else None
    if not isinstance(encoding, dict) or not isinstance(config, dict):
        raise ValueError(f"the chunk key encoding {encoding!r} is not an object with a configuration object")  # noqa: TRY004 (a document's, as chunk_coords' other refusals)
    name = encoding.get("name")
    sep = config.get("separator")
    if name == "default":
        return name, sep or "/"
    if name == "v2":
        return name, sep or "."
    raise ValueError(f"the chunk key encoding {name!r} is not one refkv reads (default or v2)")


def _index(text: str, key: str) -> int:
    """A chunk key's index, written as Zarr writes one: digits, with no leading zero."""
    if not text.isascii() or not text.isdigit() or (len(text) > 1 and text[0] == "0"):
        raise ValueError(f"{key!r} is not a chunk key: {text!r} is no index as Zarr writes one")
    return int(text)


def chunk_coords(key: str, doc: Mapping[str, Any]) -> list[int]:
    """A chunk key's coordinates, relative to the array of document `doc` (Zarr v3, or a v2
    `.zarray`), in its chunk key encoding: `default` or `v2`, with its separator (a scalar's one
    chunk is `c`, or `0` in `v2`). ValueError for a key that is no chunk key of it."""
    name, sep = _encoding(doc)
    if name == "default":
        head, _, rest = key.partition(sep)
        if head != "c":
            raise ValueError(f"{key!r} is not a chunk key of the default encoding")
        return [_index(i, key) for i in rest.split(sep)] if rest else []
    if key == "0" and doc.get("shape") == []:
        return []
    return [_index(i, key) for i in key.split(sep)]


def chunk_key(coords: Sequence[int], doc: Mapping[str, Any]) -> str:
    """The key of the chunk at `coords`, relative to the array of document `doc`, in its chunk
    key encoding."""
    name, sep = _encoding(doc)
    text = [str(i) for i in coords]
    return sep.join(["c", *text]) if name == "default" else (sep.join(text) or "0")


# ---- references, spec/layout.md

def _parts(value: Range | Concat) -> tuple[Range, ...]:
    return (value,) if isinstance(value, Range) else value.parts


def cut(value: Range | Concat, start: int, stop: int) -> Range | Concat:
    """The reference value that reads bytes `start` to `stop` of what `value` reads: a Range when
    they fall in one part, else a Concat of the parts they span, the first and last cut (a part
    of a data source stays a range of it). Reads nothing; ValueError for bounds outside `value`."""
    size = value.size
    if not (type(start) is int and type(stop) is int and 0 <= start <= stop <= size):
        raise ValueError(f"bytes {start} to {stop} are not within the value's {size}")
    out: list[Range] = []
    at = 0
    for r in _parts(value):
        n = r.size
        lo, hi = max(start, at), min(stop, at + n)
        if lo < hi or (lo == hi == start == stop and at <= start < at + n and not out):
            a, b = lo - at, hi - at
            if r.data is not None:
                out.append(Range(data=r.data[a:b]))
            else:
                out.append(Range(r.source, r.offset + a, b - a))
        at += n
    if not out:
        return Range(data=b"")
    return out[0] if len(out) == 1 else Concat(tuple(out))


def concat(*values: Range | Concat, merge: bool = False) -> Range | Concat:
    """One flat value of the parts of `values`, in order: a Range for one part. With `merge`,
    a range that starts where the one before it ends, in the same source, joins it, as do two
    literals in a row."""
    parts = [r for v in values for r in _parts(v)]
    if not parts:
        raise ValueError("concat of no values")
    if merge:
        joined: list[Range] = []
        for r in parts:
            last = joined[-1] if joined else None
            if last is not None and r.data is None and last.data is None and last.source == r.source \
                    and last.offset + last.length == r.offset:
                joined[-1] = Range(r.source, last.offset, last.length + r.length)
            elif last is not None and r.data is not None and last.data is not None:
                joined[-1] = Range(data=last.data + r.data)
            else:
                joined.append(dataclasses.replace(r, crc32c=None))
        parts = joined
    return parts[0] if len(parts) == 1 else Concat(tuple(parts))


def payload_size(value: Range | Concat, *, checksums: bool = False) -> int:
    """A value's payload in bytes, as refkv measures it against the limit of 65519 (spec/zarrify.md
    §1.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>): with `checksums`,
    the most they can add, a 6-byte `crc32c` field a source range (an upper bound: a range of a
    data source gets none, which `Base.fits` knows)."""
    extra = CRC_FIELD * sum(1 for r in _parts(value) if r.data is None and r.crc32c is None) if checksums else 0
    bare = value if isinstance(value, Range) or len(value.parts) != 1 else value.parts[0]
    return len(bare.encode()) + extra


def fill_value(doc: Mapping[str, Any]) -> object:
    """A Zarr v3 array document's `fill_value` as a Python value: a float data type's `"NaN"`,
    `"Infinity"`, `"-Infinity"` or `"0x…"` bits as a float (the bits of a float type Python has no
    float of, such as `float8_e4m3fn`, stay as they are), a complex one's pair as a complex, other
    values as JSON has them (Zarr v3 data types, permitted fill values)."""
    value, data_type = doc.get("fill_value"), doc.get("data_type")
    if isinstance(data_type, str) and data_type.startswith("complex") and isinstance(value, list) and len(value) == 2:
        size = int(data_type[len("complex"):]) // 16
        return complex(_float(value[0], size), _float(value[1], size))
    if not isinstance(value, str) or not isinstance(data_type, str):
        return value
    if value in _NONFINITE and data_type.startswith(("float", "bfloat")):  # float8_*, float6_* and float4_* too
        return _NONFINITE[value]
    if data_type == "bfloat16" and value.startswith("0x") and len(value) == 6:
        return struct.unpack(">f", (int(value, 16) << 16).to_bytes(4, "big"))[0]  # a float32's high half
    if data_type in ("float16", "float32", "float64"):
        return _float(value, int(data_type[len("float"):]) // 8)
    return value  # the bits of a type Python has no float of (float8_*, ...), as Zarr writes them


_NONFINITE = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}


def _float(value: object, size: int) -> float:
    if not isinstance(value, str):
        return float(value)  # type: ignore[arg-type]
    if value in _NONFINITE:
        return _NONFINITE[value]
    if value.startswith("0x") and len(value) == 2 + 2 * size and size in (2, 4, 8):
        return struct.unpack(">" + {2: "e", 4: "f", 8: "d"}[size], int(value, 16).to_bytes(size, "big"))[0]
    raise ValueError(f"{value!r} is not a fill value of a {8 * size}-bit float")


def _url(r: Range, base: Base) -> bool:
    """Whether `r` is a range of a url source of `base`'s table."""
    return r.data is None and r.source < len(base.sources) and base.sources[r.source].url is not None


def _payload(value: Range | Concat, base: Base, checksums: bool) -> int:
    """A value's payload, with the most checksums can add to its ranges of url sources."""
    extra = CRC_FIELD * sum(1 for r in _parts(value) if _url(r, base) and r.crc32c is None) if checksums else 0
    bare = value if isinstance(value, Range) or len(value.parts) != 1 else value.parts[0]
    return len(bare.encode()) + extra


def without_checksums(value: Range | Concat) -> Range | Concat:
    """`value` with no checksum on any range: as `Base` gives values, and as outputs are
    compared (spec/zarrify.md §1.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#11-output-and-equivalence>)."""
    return _without_checksum(value)


# ---- what refkv does with a plugin's output (spec/layout.md)

@dataclass
class Problem:
    """A problem `check` finds (`refkv.zarrify.layouts.hierarchy.check`), or a refusal of
    `zarrify`: the key it is at (None for the output as a whole), its code and a message."""

    key: str | None
    code: str
    message: str


@dataclass
class Prepared:
    """A plugin's output as refkv writes it: its values, with references swapped for the base's
    or stripped of checksums, and which bytes entries are read at open and which compressed."""

    values: dict[str, bytes | Range | Concat]
    read_at_open: set[str]
    compressed: set[str]
    fresh: set[str]  # the keys of references that are not the base's, which get checksums


def _key_problem(key: object) -> str | None:
    if type(key) is not str:
        return f"keys are str, not {type(key).__name__}"
    if not key:
        return "a key is not empty"
    try:
        n = len(key.encode("utf-8"))
    except UnicodeEncodeError:
        return "a key is valid UTF-8 (it holds a lone surrogate)"
    if n > MAX_KEY:
        return f"a key of {n} bytes, more than an archive's {MAX_KEY}"
    if key.startswith(RESERVED_PREFIX):
        return f"keys beginning {RESERVED_PREFIX!r} are the ZIP binding's own"
    return None


def _int(v: object) -> bool:
    return type(v) is int and v >= 0


def _range_problem(r: object, base: Base) -> str | None:
    """Why a part of a plugin's reference is not one refkv writes (spec/layout.md), or None."""
    if type(r) is not Range:
        return f"a part is a Range, not {type(r).__name__}"
    if r.data is not None:
        if type(r.data) is not bytes:
            return f"a literal's data is bytes, not {type(r.data).__name__}"
        if r.source != 0 or r.offset != 0 or r.length != 0 or type(r.source) is not int \
                or type(r.offset) is not int or type(r.length) is not int:
            return "a literal has source, offset and length 0"
        return None
    if not (_int(r.source) and _int(r.offset) and _int(r.length)):
        return "a range's source, offset and length are natural numbers (int, not bool)"
    if r.crc32c is not None and not (_int(r.crc32c) and r.crc32c < 1 << 32):
        return "a range's crc32c is a natural number below 2^32"
    end = r.offset + r.length
    if end > MAX_OFFSET:
        return "a range ends past 2^64 - 1"
    if r.source == len(base.sources):  # the zero source refkv adds (`Base.zeros`), as long as the output reads
        if end > MAX_OPEN:
            return f"bytes {r.offset} to {end} of the zero source, past the {MAX_OPEN} of a source table"
        return None
    if r.source >= len(base.sources):
        return f"source {r.source} is not in the base's table ({len(base.sources)} sources)"
    s = base.sources[r.source]
    if s.url is not None:
        size = base._sizes[r.source]  # noqa: SLF001 (the module's own Base)
        if size is None:
            return f"source {r.source}'s size is not known, so a range of it cannot be checked"
        if end > size:
            return f"bytes {r.offset} to {end} of source {r.source}, past its {size}"
        return None
    if s.data is not None:
        if end > len(s.data):
            return f"bytes {r.offset} to {end} of data source {r.source}, past its {len(s.data)}"
        return None
    return f"source {r.source} is a key source, which a plugin's output does not reference"


def _value_problem(v: object, base: Base) -> str | None:
    """Why a value is neither bytes nor a reference value refkv writes for `base`, or None."""
    if type(v) in (bytes, bytearray, memoryview):
        return None
    if type(v) is not Range and type(v) is not Concat:
        return f"a value is bytes, a Range or a Concat, not {type(v).__name__}"
    parts = (v,) if type(v) is Range else v.parts  # type: ignore[union-attr]
    if type(v) is Concat and (type(parts) is not tuple or not parts):
        return "a Concat's parts are a non-empty tuple of Ranges"
    why = next((w for w in (_range_problem(r, base) for r in parts) if w), None)
    if why is None and sum(r.size for r in parts) > MAX_OFFSET:
        why = "its ranges total more than 2^64 - 1 bytes"
    return why


def _reads(value: Range | Concat, base: Base) -> int:
    """The reads of url sources a value makes, ranges of one source at most `MERGE_GAP` apart
    counted once, as refkv's readers merge them (`refkv.reader`); a data source needs none."""
    spans: dict[int, list[tuple[int, int]]] = {}
    for r in _parts(value):
        if _url(r, base) and r.length:
            spans.setdefault(r.source, []).append((r.offset, r.offset + r.length))
    n = 0
    for ranges in spans.values():
        ranges.sort()
        end = -MERGE_GAP - 1
        for a, b in ranges:
            if a > end + MERGE_GAP:
                n += 1
            end = max(end, b)
    return n


def _unpacked(written: object) -> tuple[Mapping[Any, Any], object, object] | str:
    """(entries, read_at_open, stored) of what `write` returned, or why it is neither a mapping
    nor a Written."""
    if isinstance(written, Written):
        if not isinstance(written.entries, Mapping):
            return f"Written's entries are a mapping, not {type(written.entries).__name__}"
        return written.entries, written.read_at_open, written.stored
    if isinstance(written, Mapping):
        return written, None, ()
    return f"write() returned {type(written).__name__}, not a mapping of keys to entries or a Written"


def _key_set(name: str, keys: object) -> set[str] | str:
    if isinstance(keys, (str, bytes)) or not isinstance(keys, Iterable):
        return f"{name} is an iterable of keys, not {type(keys).__name__}"
    out = set()
    for k in keys:
        if type(k) is not str:
            return f"{name} holds keys (str), not {type(k).__name__}"
        out.add(k)
    return out


def prepare(base: Base, written: object) -> tuple[Prepared, list[Problem]]:
    """A plugin's output as refkv writes it, and the problems that keep `zarrify` from writing it
    (codes `key`, `reference` and `open`; spec/layout.md)."""
    problems: list[Problem] = []
    unpacked = _unpacked(written)
    if isinstance(unpacked, str):
        return Prepared({}, set(), set(), set()), [Problem(None, "reference", unpacked)]
    entries, read_at_open, stored = unpacked
    values: dict[str, bytes | Range | Concat] = {}
    fresh: set[str] = set()
    for k, v in list(entries.items()):
        why = _key_problem(k)
        if why:
            problems.append(Problem(k if isinstance(k, str) else repr(k), "key", why))
            continue
        if type(v) in (bytes, bytearray, memoryview):
            values[k] = bytes(v)
            continue
        why = _value_problem(v, base)
        if why:
            problems.append(Problem(k, "reference", why))
            continue
        known = base.base_value(v)
        if known is not None:
            values[k] = known
            continue
        bare = tuple(dataclasses.replace(r, crc32c=None) for r in _parts(v))
        value: Range | Concat = bare[0] if len(bare) == 1 else Concat(bare)
        values[k] = value
        fresh.add(k)
        if _reads(value, base) > MAX_READS:
            problems.append(Problem(k, "reference", f"it reads {_reads(value, base)} ranges, more than the "
                                                    f"{MAX_READS} a reader makes for one value by default "
                                                    "(spec/refkv.md §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)"))
        if payload_size(value) > MAX_PAYLOAD and not base.checksums:
            problems.append(Problem(k, "reference", f"a payload of {payload_size(value)} bytes, more than "
                                                    f"{MAX_PAYLOAD}"))
    sets: dict[str, set[str]] = {}
    for name, keys in (("read_at_open", read_at_open), ("stored", stored)):
        if keys is None:
            continue
        got = _key_set(name, keys)
        if isinstance(got, str):
            problems.append(Problem(None, "open", got))
            continue
        problems.extend(Problem(k, "open", f"{name} names {k!r}, which is not a bytes entry of the output")
                        for k in sorted(got) if not isinstance(values.get(k), bytes))
        sets[name] = got & {k for k, v in values.items() if isinstance(v, bytes)}
    if "read_at_open" in sets:
        at_open = sets["read_at_open"]
    else:
        at_open = set()
        for k, v in values.items():
            if not isinstance(v, bytes):
                continue
            if base._values.get(k) == v:  # noqa: SLF001 (the module's own Base): the base's placement
                if k in base._read_at_open:  # noqa: SLF001
                    at_open.add(k)
            elif k.rpartition("/")[2] in DOCUMENTS:
                at_open.add(k)
    kept = sets.get("stored", set())
    compressed = {k for k, v in values.items() if isinstance(v, bytes) and k not in at_open and k not in kept
                  and _deflate_bound(len(v)) < STORED}
    opened = sum(len(values[k]) for k in at_open)  # type: ignore[arg-type]
    if opened > MAX_OPEN:
        problems.append(Problem(None, "open", f"{opened} bytes read at open, more than {MAX_OPEN}"))
    return Prepared(values, at_open, compressed, fresh), problems


def _deflate_bound(n: int) -> int:
    """The most bytes a DEFLATE stream of `n` bytes takes (zlib's `deflateBound`)."""
    return n + (n >> 12) + (n >> 14) + (n >> 25) + 13


class _Counting(io.RawIOBase):
    """A file that keeps nothing, for building an archive's structure only."""

    @override
    def writable(self) -> bool:
        return True

    @override
    def write(self, b: Any) -> int:
        return len(b)


def store_of(base: Base, prepared: Prepared, *, policy: Policy | None, info: Mapping[str, object],
             registry: Registry | None = None,
             checksum: Callable[[str, int, int], int] | None = None) -> tuple[ReferenceStore, list[Problem]]:
    """The output as a `refkv.ReferenceStore`, references first and then bytes entries, each by
    key, the plugin's own ranges checksummed with `checksum(url, offset, length)` when given; and
    the problems of the archive it makes (code `reference`, a payload past the limit with its
    checksums; `archive`, what the writer refuses or a reader would)."""
    problems: list[Problem] = []
    sources = list(base.sources)
    added = len(sources)  # the index of the zero data source `Base.zeros` names
    zeros = max((r.offset + r.length for v in prepared.values.values() if not isinstance(v, bytes)
                 for r in _parts(v) if r.data is None and r.source == added), default=None)
    zero_at = added
    if zeros is not None:  # as long as the output reads, or the base's own when that is long enough
        own = base.zero_source
        if own is not None and own[1] >= zeros:
            zero_at = own[0]
        else:
            sources.append(Source(data=bytes(zeros)))

    def checked(r: Range) -> Range:
        if checksum is None or r.data is not None:
            return r
        url = sources[r.source].url
        return r if url is None else dataclasses.replace(r, crc32c=checksum(url, r.offset, r.length))

    mapping: dict[str, bytes | Range | Concat] = {}
    for k in sorted(k for k, v in prepared.values.items() if not isinstance(v, bytes)):
        v = prepared.values[k]
        if k in prepared.fresh:
            done = tuple(checked(dataclasses.replace(r, source=zero_at) if r.data is None and r.source == added
                                 else r) for r in _parts(v))  # type: ignore[arg-type]
            v = done[0] if len(done) == 1 else Concat(done)
            if payload_size(v) > MAX_PAYLOAD:
                problems.append(Problem(k, "reference", f"a payload of {payload_size(v)} bytes with its checksums, "
                                                        f"more than {MAX_PAYLOAD}"))
        mapping[k] = v
    for k in sorted(k for k, v in prepared.values.items() if isinstance(v, bytes)):
        mapping[k] = prepared.values[k]
    store = ReferenceStore(mapping, sources, read_at_open=sorted(prepared.read_at_open),
                           compressed=sorted(prepared.compressed), page_size=PAGE_SIZE, policy=policy,
                           registry=registry, info=info)
    if not problems:
        try:
            # the structure only: compression makes it no larger
            sink = cast("BinaryIO", _Counting())
            _, _, sizes = store._write_vzip(sink, sources, mirror_refs=True, page_size=PAGE_SIZE,  # noqa: SLF001
                                            compress=False)
        except ValueError as e:
            problems.append(Problem(None, "archive", f"the archive cannot be written: {e}"))
        else:
            problems.extend(Problem(None, "archive", f"its {key} would be {sizes[key]} bytes, more than the "
                                                     f"{MAX_OPEN} a reader reads by default")
                            for key in (SOURCES_KEY, INDEX_KEY) if sizes.get(key, 0) > MAX_OPEN)
            # a phkv store's page index can hold every key, each with its page's three varints
            keys = sum(len(k.encode()) + PAGE_ENTRY for k in mapping)
            if keys > MAX_OPEN:
                problems.append(Problem(None, "archive", f"its keys and their page entries total {keys} bytes, "
                                                         f"more than the {MAX_OPEN} a phkv reader reads at open"))
    return store, problems


def base_of(out: Output | StoreIrOutput, fmt: str, policy: Policy | None,
            registry: Registry | None = None) -> tuple[Base, ReferenceStore]:
    """The `Base` of a file input's output, and the output as a store (reading its sources under
    `policy`, with the object stores of `registry`)."""
    store = out.to_store(policy=policy, registry=registry, info={"format": fmt, **out.summary})
    if out.ir is None:
        raise TypeError("an output without an IR takes no layout")
    if isinstance(out, StoreIrOutput):
        objects, _, _ = out._numbered()  # noqa: SLF001 (its table's url sources, in order)
        sizes: list[int | None] = [out.sizes.get(k) for k in objects] + [None] * (len(store._sources) - len(objects))  # noqa: SLF001
    else:
        objects: list[str] = []
        sizes = [out.size] + [None] * (len(store._sources) - 1)  # noqa: SLF001
    return Base(store, refkv.ir.Ir(out.ir), checksums=out.checksum is not None, objects=objects, sizes=sizes), store


def apply(out: Output | StoreIrOutput, fmt: str, policy: Policy | None, planned: Planned,
          registry: Registry | None = None) -> ReferenceStore:
    """The output of a call that names a plugin layout (spec/layout.md): the plugin's `write`
    run on its base, checked and written. Raises LayoutRejected or LayoutFailed."""
    base, store = base_of(out, fmt, policy, registry)
    name = planned.name
    if not planned.reads(base.profile, base.version):
        reads = "every file profile" if isinstance(planned.formats, frozenset) else ", ".join(
            f"{k} v{', v'.join(str(v) for v in sorted(vs))}" for k, vs in sorted(planned.formats.items())
            if k in FILE_PROFILES and vs) or "no version of any"
        raise LayoutRejected(f"{name} does not read the {base.profile} profile's convention at version "
                             f"{base.version} (it reads {reads})")
    try:  # prepare reads what write returned, a plugin's mapping or generators among it: its code too
        written = planned.plugin.write(base, planned.fresh_options())
        prepared, problems = prepare(base, written)
    except Rejected as e:
        raise LayoutRejected(f"{name}: {_text(e)}") from e
    except BaseException as e:  # any of a plugin's, made a failure
        raise LayoutFailed(f"{name}: {described(e)}") from e
    if problems:
        raise LayoutFailed(_failure(name, problems))
    info = {**store.info, "layouts": [name]}
    # the base store's policy, which reads the input's own files as well as `policy` allows
    result, problems = store_of(base, prepared, policy=store.policy, info=info, checksum=out.checksum,
                                registry=registry)
    if problems:
        raise LayoutFailed(_failure(name, problems))
    return result


def _failure(name: str, problems: Sequence[Problem]) -> str:
    p = problems[0]
    more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
    return f"{name}: {p.key + ': ' if p.key else ''}{p.message}{more}"


# ---- a base for tests and notebooks

async def base_async(location: str | os.PathLike[str], url: str | None = None, *, checksums: bool = False,
                     policy: Policy | None = None, registry: Registry | None = None) -> Base:
    """The `Base` a plugin is given for the file at `location`, read as `zarrify` reads it: for
    a plugin's tests, and a notebook. Raises what `zarrify` raises for the input, and
    LayoutUsageError for a store input. `registry` is the application's object stores by URL prefix,
    as `zarrify` takes them."""
    location = os.fspath(location)
    if _zarrify.is_store(location):
        raise LayoutUsageError(f"{location} is a store: a store input takes no layout")
    resolver = Resolver(registry)
    fmt, out = await _zarrify._zarrify_async(location, url, checksums=checksums, policy=policy,  # noqa: SLF001
                                             registry=resolver)
    if not isinstance(out, (Output, StoreIrOutput)) or out.ir is None:
        raise LayoutUsageError(f"{location}: its output has no IR, so it takes no layout")
    return base_of(out, fmt, policy, resolver)[0]


def base(location: str | os.PathLike[str], url: str | None = None, *, checksums: bool = False,
         policy: Policy | None = None, registry: Registry | None = None) -> Base:
    """`base_async`, the caller's thread waiting for it."""
    return _zarrify._sync.run(base_async(location, url, checksums=checksums, policy=policy,  # noqa: SLF001
                                         registry=registry))
