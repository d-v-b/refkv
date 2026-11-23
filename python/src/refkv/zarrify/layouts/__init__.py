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
import json
import math
import re
import struct
import types
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from refkv.entries import (
    CRC_FIELD,
    MAX_PAYLOAD,
    MIRROR,
    chunk_coords,
    chunk_key,
    dump,
)
from refkv.errors import Rejected
from refkv.pb import Concat, Range
from refkv.profiles import REGISTRY

if TYPE_CHECKING:
    from refkv.build import Parsed, Value, Written

__all__ = ["BASE_PROFILES", "MAX_PAYLOAD", "MIRROR", "Entries", "EntryValue", "Layout",
           "LayoutFailed", "LayoutRejected", "LayoutUsageError", "Plugin", "Problem",
           "chunk_coords", "chunk_key", "concat", "cut", "dump", "encode_entries", "fill_value", "payload_size",
           "plan", "without_checksums"]

type EntryValue = bytes | Range | Concat  # a value of an output's entry
type Entries = dict[str, EntryValue]  # an output's entries, keys from its root, as a plugin builds them

GROUP = "refkv.layouts"  # the entry points' group
# the profiles a plugin may read: every file and store profile (not a dataset layout's entry)
BASE_PROFILES = frozenset(str(e["name"]) for e in REGISTRY if e["kind"] in ("file", "store", "both"))



NAME = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")



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
    data source gets none)."""
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


def without_checksums(value: Range | Concat) -> Range | Concat:
    """`value` with no checksum on any range: as outputs are
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
