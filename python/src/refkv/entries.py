"""What every output's entries obey, whoever builds them (refkv's projections, a plugin's layout,
`refkv.build`): the archive's limits, the IR mirror's node, the documents a Zarr reader parses
and how zarrify writes them, chunk keys, and refkv's declarations on a node. A module of its own
below `refkv.zarrify` (d-v-b/refkv#459), so that `refkv.build` reads them without importing
it; `refkv.zarrify.layouts` re-exports the public ones."""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from typing import Any, override

from refkv.profiles import REGISTRY

__all__ = ["CRC_FIELD", "DOCUMENTS", "MAX_OPEN", "MAX_PAYLOAD", "MERGE_GAP", "MIRROR", "PAGE_ENTRY",
           "REFKV_MEMBERS", "REFKV_UUIDS", "STORED", "chunk_coords", "chunk_key", "dump", "undeclared"]

# the most bytes of a reference's payload (spec/zarrify.md §1.2
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>)
MAX_PAYLOAD = 65519
# the IR mirror's node (spec/conventions.md §8 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#8-the-ir-mirror>)
MIRROR = "refkv_source"
MAX_OPEN = 256 << 20  # bytes read at open, and of a format entry: the default max_format_entry
MERGE_GAP = 1 << 16  # ranges of one source this close are one read, as refkv's readers merge them by default
# a DEFLATE stream that could reach this is not written (spec/bindings/vzip.md §14.1
# <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#141-requirements>)
STORED = 0xFFFFFFFF
PAGE_ENTRY = 16  # the most a phkv page index's entry takes beside its first key: three varints, and room
CRC_FIELD = 6  # the most bytes a range's crc32c field takes: a tag, and a varint of 32 bits
# the documents a Zarr reader parses as JSON, by a key's last segment
DOCUMENTS = frozenset(["zarr.json", ".zgroup", ".zarray", ".zattrs", ".zmetadata"])
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


def dump(doc: object, *, allow_nan: bool = False) -> bytes:
    """A document as zarrify writes one: compact, strict JSON in UTF-8 (ValueError for NaN, an
    infinity, or anything else strict JSON cannot hold). `allow_nan` writes `NaN` and
    `Infinity` bare, as zarr-python writes them in a Zarr v2 `.zattrs`, which `check` then
    reports under `json`."""
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False, allow_nan=allow_nan).encode()


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
