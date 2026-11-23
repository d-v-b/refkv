"""No parser writes Zarr output metadata into its IR's elements or its description (d-v-b/refkv#378,
Acceptance item 2): the IR and the description say what the source holds, in the source's own
terms, and every output's decisions are its own (spec/stored-array.md §1
<https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#1-stored-arrays>).

**Zarr output metadata**, for this test, is any of these, found anywhere in the forms of the IR's
elements (spec/conventions.md §8.5
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes>), in the IR's
element types, or in the description:

1. A **Zarr data type** used as a type: the value of a key `data_type`, `dtype` or `type` (or an
   element's type) that zarr reads as a Zarr v3 data type (`zarr.core.dtype.
   get_data_type_from_json`), such as `"uint16"` or `{"name": "struct", "configuration": ...}`, or
   an object there whose `name` is a data type zarr has registered, such as `"struct"`.
2. A **Zarr codec**: an object whose `name` is a codec zarr or numcodecs resolves (`bytes`,
   `zstd`, `imagecodecs_jpeg`, `numcodecs.shuffle`, ...) and that has a `configuration`, or is
   the value or an item of a key `codec`, `codecs` or `compressor`; or such a codec's name as an
   item of a list there (a codec chain such as `["zlib", "bytes"]`).
3. A **Zarr or convention metadata key**: one of Zarr's array and group metadata
   (`zarr_format`, `node_type`, `chunk_grid`, `chunk_key_encoding`, `codecs`, `compressor`,
   `fill_value`, `dimension_names`, `storage_transformers`, `_ARRAY_DIMENSIONS`) or OME-Zarr's
   (`multiscales`, `omero`, `coordinateTransformations`).

What the source declares itself is never output metadata: a string the bytes of one of the IR's
value elements hold verbatim (a Zarr store's `zarr.json` naming its codecs, an NRRD header's
`type: int16`, OME-XML's `Type="uint16"`) is allowed wherever it appears. So are the source's
declarations in its own terms, which are none of the above: an NPY `descr` such as `<u2`, a
safetensors `dtype` such as `F32`, an HDF5 datatype's class and size, a TIFF compression number.

This finds the presentation decisions that are Zarr metadata. It does not find one that is
spelled in no output's terms, such as a parser leaving out an element because an output would
not present it; those are for review to find.

No list of the profiles is kept here, nor of the names above: the test reads the registry, the
fixtures, and zarr's and numcodecs' registries. A profile that still writes output metadata is a
strict xfail in `OFFENDERS` below, which the PR that fixes it removes."""

import json
import re
from collections import defaultdict
from collections.abc import Iterator
from functools import cache
from typing import Any

import numcodecs.registry
import pytest
from helpers import FIXTURES, accepted_fixtures
from zarr.core.dtype import data_type_registry, get_data_type_from_json
from zarr.registry import get_codec_class

from refkv import _core

PROFILES = sorted(e["name"] for e in json.loads(_core.registry()))
TYPE_KEYS = frozenset({"data_type", "dtype", "type"})
CODEC_KEYS = frozenset({"codec", "codecs", "compressor"})
METADATA_KEYS = frozenset({
    "zarr_format", "node_type", "chunk_grid", "chunk_key_encoding", "codecs", "compressor", "fill_value",
    "dimension_names", "storage_transformers", "_ARRAY_DIMENSIONS", "multiscales", "omero",
    "coordinateTransformations",
})

# Each profile whose parser still writes Zarr output metadata, with what it writes. The PR that
# fixes a profile removes its entry; a blank line keeps each entry apart from the next, so that
# PRs removing different entries do not conflict.
OFFENDERS: dict[str, str] = {
    "czi": "CZI's subblock forms give a Zarr data type (`geometry.dtype`) and a Zarr codec chain (`codec`)",

    "dicom": "DICOM's frame forms give a Zarr codec (`codec.name` `bytes`, with its byte order)",




    "mp3": "MP3's frame forms give a Zarr data type (`geometry.data_type`)",

    "mrc": "MRC's data forms give a Zarr data type (`geometry.data_type`)",

    "nd2": "ND2's frame forms give a Zarr data type (`geometry.dtype`) and a Zarr codec chain (`codec`)",


    "npy": "NPY's data forms give a Zarr data type (`geometry.data_type`)",


}


def data_type_names() -> frozenset[str]:
    # private, as the registry has no public listing of its names
    data_type_registry._lazy_load()
    return frozenset(data_type_registry.contents)


DATA_TYPES = data_type_names()


def is_data_type(v: object) -> bool:
    """Whether zarr reads `v` as a Zarr v3 data type, or `v` is an object naming one."""
    if isinstance(v, dict) and v.get("name") in DATA_TYPES:
        return True
    if not isinstance(v, str | dict):
        return False
    try:
        get_data_type_from_json(v, zarr_format=3)
    except (ValueError, TypeError, KeyError):
        return False
    return True


def is_codec(name: object) -> bool:
    """Whether `name` is a codec zarr or numcodecs resolves."""
    return isinstance(name, str) and resolves(name)


@cache
def resolves(name: str) -> bool:
    if name in numcodecs.registry.codec_registry:
        return True
    try:
        get_codec_class(name)
    except Exception:  # noqa: BLE001 (zarr raises its own error, or a plugin's import error)
        return False
    return True


def found(v: Any, where: str, under: str | None = None) -> Iterator[tuple[str, Any]]:
    """Each `(path, value)` of `v` (found at `where`, the value of key `under`) that is Zarr output
    metadata, by the rules of the module's docstring."""
    if under in TYPE_KEYS and is_data_type(v):
        yield where, v
        return
    if isinstance(v, dict):
        if is_codec(v.get("name")) and ("configuration" in v or under in CODEC_KEYS):
            yield where, v
            return
        for k, x in v.items():
            if k in METADATA_KEYS:
                yield f"{where}.{k}", k
            else:
                yield from found(x, f"{where}.{k}", k)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            if under in CODEC_KEYS and is_codec(x):
                yield f"{where}[{i}]", x
            else:
                yield from found(x, f"{where}[{i}]", under)


def declared(text: str, source: bytes) -> bool:
    """Whether the source's value bytes hold `text` verbatim, as a token of its own: not within
    a longer name (`int16` within `uint16`)."""
    edge = rb"[A-Za-z0-9_.]"
    return re.search(rb"(?<!" + edge + rb")" + re.escape(text.encode()) + rb"(?!" + edge + rb")", source) is not None


def problems(p: Any) -> list[str]:
    """The Zarr output metadata in an input's IR and description, none the source declares."""
    ir = p.ir
    source = b"\0".join(r for i in range(len(ir)) if (r := ir.raw(i)) is not None)
    out: list[tuple[str, Any]] = []
    for k, text in enumerate(ir.forms()):
        out.extend(found(json.loads(text), f"form {k}"))
    out.extend((f"type of {ir.path(i)!r}", t) for i in range(len(ir)) if is_data_type(t := ir.type(i)))
    out.extend(found(p.description, "description"))

    def own(v: Any) -> bool:
        if isinstance(v, str):
            return declared(v, source)
        name = v.get("name") if isinstance(v, dict) else None
        return isinstance(name, str) and declared(name, source)

    return [f"{where}: {json.dumps(v)[:120]}" for where, v in out if not own(v)]


@cache
def offending() -> dict[str, dict[str, list[str]]]:
    """By the profile that read it, each accepted fixture with what output metadata it has (an
    empty list for none)."""
    out: dict[str, dict[str, list[str]]] = defaultdict(dict)
    for path, p in accepted_fixtures():
        out[p.profile][str(path.relative_to(FIXTURES))] = problems(p)
    return out


def test_rules_find_output_metadata():
    """So that a rule that finds nothing cannot pass every profile unnoticed."""
    form = {
        "geometry": {"data_type": {"name": "struct", "configuration": {"fields": []}}, "dtype": "uint16"},
        "codec": [{"name": "bytes", "configuration": {"endian": "little"}}],
        "chain": {"codecs": ["zlib"]},
        "attributes": {"multiscales": []},
        "own": {"type": "uchar", "descr": "<u2", "dtype": "F32", "codec": {"name": "avc1"}},
    }
    assert [w for w, _ in found(form, "f")] == [
        "f.geometry.data_type", "f.geometry.dtype", "f.codec[0]", "f.chain.codecs", "f.attributes.multiscales"]


def test_declared_is_a_whole_token():
    source = b'<Pixels Type="uint16"/>\0type: int8'
    assert [t for t in ("uint16", "int16", "int8", "uint8") if declared(t, source)] == ["uint16", "int8"]


def test_some_fixture_is_read():
    assert sum(len(v) for v in offending().values()) > 0


@pytest.mark.parametrize("profile", [
    pytest.param(p, marks=pytest.mark.xfail(strict=True, reason=OFFENDERS[p])) if p in OFFENDERS else p
    for p in PROFILES
])
def test_ir_has_no_zarr(profile):
    fixtures = offending().get(profile)
    if not fixtures:
        pytest.skip(f"no fixture is read by {profile}")
    bad = {f: why for f, why in fixtures.items() if why}
    assert not bad, f"{profile}'s parser writes Zarr output metadata:\n" + "\n".join(
        f"  {f}: {'; '.join(why[:3])}" for f, why in sorted(bad.items())[:10])
