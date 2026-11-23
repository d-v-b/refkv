"""Layouts defined outside refkv (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>): a Python package writes the whole
hierarchy of a zarrify call's output itself, from the input as refkv parsed it, and refkv writes
exactly what it returns, once it has checked that every reference reads bytes of a source refkv
opened and that the archive can hold it. A plugin layout is a projection of the input's IR, as
refkv's own output is: it is given no output of refkv's, and builds on one only where it asks
for it (`refkv.build.project`).

A plugin is an object registered under the entry-point group `refkv.layouts`, its key the
layout's name, or given to `Layout` itself, with these attributes:

- `name`: the layout's name, `<distribution>.<layout>`, the entry point's key;
- `formats`: `"*"`, or the profiles it reads, file or store profiles, each with the versions
  of its convention it reads (`{"tiff": {0}, "hdf5": {0}, "zarr2": {0}}`); names this refkv has no
  profile of are ignored;
- `options(given)`: the options in canonical form, a JSON object, or ValueError;
- `write(inputs, options)`: the output's entries, keys from its root, built from `inputs`, the
  parsed inputs of the call (each a `refkv.build.Parsed`; a `zarrify` call gives one), as a
  mapping of keys to bytes or values or a `refkv.build.Written`; it raises
  `refkv.zarrify.Rejected` for an input it cannot present.

    from refkv.zarrify import zarrify
    from refkv.zarrify.layouts import Layout

    store = zarrify("image.ome.tif", layout=Layout("acme-ome.ome06"))
    store.mapping["zarr.json"]  # the plugin's root

`refkv.build` holds what a plugin builds with: the values that name the bytes they read,
`project` for refkv's own output of an input, and `seal`, the check refkv applies to what a
plugin returns (`refkv.build.apply`). The check guards against a plugin's mistakes, not against a
hostile plugin, which is code running in the caller's process. `refkv.zarrify.layouts.hierarchy`
(the `layouts` extra) builds and checks Zarr documents; `refkv.zarrify.coding` turns coding
parameters into codecs.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import io
import json
import re
import types
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, override

from refkv.pb import Concat, Range
from refkv.zarrify.common import MAX_PAYLOAD, Rejected
from refkv.zarrify.profiles import REGISTRY

if TYPE_CHECKING:
    from refkv.build import Parsed, Value, Written

__all__ = ["BASE_PROFILES", "MAX_PAYLOAD", "MIRROR", "Entries", "EntryValue", "Layout",
           "LayoutFailed", "LayoutRejected", "LayoutUsageError", "Plugin", "Problem",
           "chunk_coords", "chunk_key", "dump", "encode_entries", "plan"]

type EntryValue = bytes | Range | Concat  # a value of an output's entry
type Entries = dict[str, EntryValue]  # an output's entries, keys from its root, as a plugin builds them

GROUP = "refkv.layouts"  # the entry points' group
# the IR mirror's node (spec/conventions.md §8 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#8-the-ir-mirror>)
MIRROR = "refkv_source"
# the profiles a plugin may read: every file and store profile (not a dataset layout's entry)
BASE_PROFILES = frozenset(str(e["name"]) for e in REGISTRY if e["kind"] in ("file", "store", "both"))
REFKV_UUIDS = frozenset(str(e["uuid"]).lower() for e in REGISTRY)
REFKV_MEMBERS = ("refkv_zarrified", "refkv_provenance")  # what refkv declares on a node


def undeclared(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """A node's attributes without refkv's declarations: no `refkv_zarrified` or
    `refkv_provenance`, and no CMO of refkv's conventions in `zarr_conventions` (dropped when
    nothing is left in it); a copy of the top level."""
    attrs = dict(attributes)
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
NAME = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")

MAX_OPEN = 256 << 20  # bytes read at open, and of a format entry: the default max_format_entry
MERGE_GAP = 1 << 16  # ranges of one source this close are one read, as refkv's readers merge them by default
# a DEFLATE stream that could reach this is not written (spec/bindings/vzip.md §14.1
# <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#141-requirements>)
STORED = 0xFFFFFFFF
PAGE_ENTRY = 16  # the most a phkv page index's entry takes beside its first key: three varints, and room
CRC_FIELD = 6  # the most bytes a range's crc32c field takes: a tag, and a varint of 32 bits
# the documents a Zarr reader parses as JSON, by a key's last segment
DOCUMENTS = frozenset(["zarr.json", ".zgroup", ".zarray", ".zattrs", ".zmetadata"])


class LayoutUsageError(ValueError):
    """A plugin layout asked for wrongly: a name no plugin has, a plugin that fails to load or
    breaks the rules of spec/layout.md, options it refuses, or a plugin layout beside built-in ones."""


class LayoutRejected(Rejected):
    """The layout request is refused for this input: an input of a profile the plugin does not
    read, or a `Rejected` the plugin raised (refkv's own output's among them, where the plugin
    asked for it). The input is one the profile's parser accepts; no output is written."""


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

    def write(self, inputs: Sequence[Parsed], options: Mapping[str, Any]) -> Mapping[str, bytes | Value] | Written: ...


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
    """A plugin's `formats` checked: every file and store profile (`"*"`), or the versions of each
    profile it reads; or LayoutUsageError."""
    if formats == "*":
        return BASE_PROFILES
    why = 'formats is "*", or a mapping of profile names to iterables of convention versions (int)'
    if not isinstance(formats, Mapping):
        raise _usage(name, why)
    try:
        read = {k: frozenset(vs) for k, vs in formats.items()}
    except TypeError as e:
        raise _usage(name, why) from e
    if not all(isinstance(k, str) and all(type(v) is int and v >= 0 for v in vs) for k, vs in read.items()):
        raise _usage(name, why)
    if not set(read) & BASE_PROFILES:  # a later refkv's names are ignored, but one must be this one's
        raise _usage(name, f"formats names no profile of this refkv ({', '.join(sorted(read)) or 'none'}); "
                           f"they are {', '.join(sorted(BASE_PROFILES))}")
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
        """Whether the plugin reads an input of `profile`, its convention at `version`."""
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


def _without_checksum(value: Range | Concat) -> Range | Concat:
    """A reference value as spec/zarrify.md §1.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#11-output-and-equivalence> compares
    it: its ranges without checksums."""
    if isinstance(value, Range):
        return dataclasses.replace(value, crc32c=None)
    return Concat(tuple(dataclasses.replace(r, crc32c=None) for r in value.parts))


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


# ---- what refkv does with a plugin's output (spec/layout.md)

@dataclass
class Problem:
    """A problem `check` finds (`refkv.zarrify.layouts.hierarchy.check`), or a refusal of
    `zarrify`: the key it is at (None for the output as a whole), its code and a message."""

    key: str | None
    code: str
    message: str


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
