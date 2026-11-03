"""Reference stores as read-only zarr stores, for zarr-python and xarray (the `zarr` extra).

A reference store (`refkv.ReferenceStore` in memory, `refkv.vzip.VZipStore` for an archive)
needs no Zarr: its keys are strings, and its values bytes or references. This
module wraps one as a `zarr.abc.store.Store`:

    import xarray as xr
    import refkv

    ds = xr.open_zarr(refkv.zarr.open("data.vzip"))  # a saved archive
    ds = xr.open_zarr(refkv.zarr.store(store))      # a refkv.ReferenceStore

Importing it also registers the data types that archives of zarrified safetensors files use and
zarr-python lacks (`refkv.zarr.dtypes`: bfloat16, the float8, float6 and float4 types, and
`refkv.float8_e4m3fn`, each an `ml_dtypes` type, which zarr also finds through refkv's
`zarr.data_type` entry points), and the codecs that archives of zarrified files and
stores use and zarr-python lacks (`refkv.zarr.codecs`, one module per codec, which
zarr also finds through refkv's `zarr.codecs` entry points, without this import;
the `refkv.tiff_lzw` codec's decoder needs the `images` extra, which also installs
imagecodecs-zarr's JPEG, JPEG 2000 and JPEG XR codecs, the `refkv.stream` codec's
the `media` extra), and turns on
zarr-python's `array.rectilinear_chunks`, which the zarr-extensions `rectilinear` chunk
grid of some archives' arrays needs (an MP4 file's video planes, a clipped CZI level).

A zarrified HDF5 file's float attribute is a float value, Zarr v3's fill value encoding,
which a reader decodes by the datatype the node's source metadata records
(spec/zarrify/hdf5.md §6
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>): a NaN, an
infinity or −0 is a string in the archive, such as `"NaN"`. The store decodes them, and
serves each as the float zarr-python and hdmf-zarr write (the literal `NaN`), so that pynwb's
`NWBZarrIO` reads a `rate` of NaN as a number; a string attribute `"NaN"` stays a string.
Every other value is the archive's, as it is; `reader` gives the archive's own bytes.
"""

from __future__ import annotations

import asyncio
import json
import math
import string
import struct
from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING, Unpack, override

from refkv._extras import missing

if TYPE_CHECKING:
    from refkv import OpenOptions
    from refkv._narrow import JSON

try:
    import zarr
    from zarr.abc.store import (
        ByteRequest,
        OffsetByteRequest,
        RangeByteRequest,
        Store,
        SuffixByteRequest,
    )
    from zarr.core.buffer import Buffer, BufferPrototype
except ImportError as e:
    raise missing("zarr", "refkv.zarr", e) from e

import refkv
import refkv.zarr.codecs  # registers the codecs vzip archives use
import refkv.zarr.dtypes  # and the data types (bfloat16, float8, ...) zarr-python lacks

zarr.config.set({"array.rectilinear_chunks": True})
from refkv.reader import (  # noqa: E402
    OffsetRequest,
    RangeRequest,
    ReferenceReader,
    Request,
    SuffixRequest,
)

_NONFINITE = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}
_FORMATS = {2: ">e", 4: ">f", 8: ">d"}  # a float of each size, from its bits


def _float_value(value: str, size: JSON) -> JSON:
    """A float value of `size` bytes that is a string, decoded: `"NaN"`, `"Infinity"`,
    `"-Infinity"`, or `"0x"` and the float's bits in hex (−0, or a NaN with other bits). Any
    other string is returned as it is. A NaN's bits do not reach zarr-python: the JSON it
    parses has the one literal `NaN`."""
    if value in _NONFINITE:
        return _NONFINITE[value]
    digits = value[2:]
    if (value.startswith("0x") and isinstance(size, int) and size in _FORMATS and len(digits) == 2 * size
            and all(c in string.hexdigits for c in digits)):
        return struct.unpack(_FORMATS[size], bytes.fromhex(digits))[0]
    return value


def _floats(value: JSON, datatype: JSON) -> JSON:
    """An HDF5 attribute's JSON value with each float of `datatype` (its described datatype)
    that is a string decoded, in nested arrays and compounds' members."""
    if not isinstance(datatype, dict):
        return value
    if isinstance(value, list):
        return [_floats(v, datatype) for v in value]
    if datatype.get("class") == "float" and isinstance(value, str):
        return _float_value(value, datatype.get("size"))
    if datatype.get("class") == "compound" and isinstance(value, dict):
        listed = datatype.get("members")
        members = {name: m.get("datatype") for m in listed if isinstance(m, dict)
                   and isinstance(name := m.get("name"), str)} if isinstance(listed, list) else {}
        return {k: _floats(v, members.get(k)) for k, v in value.items()}
    return value


def _hdf5_floats(document: bytes) -> bytes:
    """A node's zarr.json with the HDF5 profile's float attributes that are strings in the
    archive (spec/zarrify/hdf5.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>) decoded,
    written as Python's `json` writes a float: `NaN`, `Infinity`, `-Infinity` and `-0.0`,
    which zarr-python reads as floats. The node's `refkv_zarrified.hdf5.attributes`, each
    attribute's datatype, tells a float's `"NaN"` from a string's. Any other document is
    returned as it is."""
    if b"refkv_zarrified" not in document or not any(s in document for s in (b'"NaN"', b'Infinity"', b'"0x')):
        return document
    try:
        meta = json.loads(document)
        attributes = meta["attributes"]
        datatypes = attributes["refkv_zarrified"]["hdf5"]["attributes"]
    except (ValueError, KeyError, TypeError):
        return document
    if not isinstance(attributes, dict) or not isinstance(datatypes, dict):
        return document
    meta["attributes"] = {k: _floats(v, datatypes.get(k)) for k, v in attributes.items()}
    return json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode()


def _slice(data: bytes, byte_range: ByteRequest | None) -> bytes:
    """The bytes of `data` that `byte_range` asks for."""
    if byte_range is None:
        return data
    if isinstance(byte_range, RangeByteRequest):
        return data[byte_range.start:byte_range.end]
    if isinstance(byte_range, OffsetByteRequest):
        return data[byte_range.offset:]
    if isinstance(byte_range, SuffixByteRequest):
        return data[len(data) - min(byte_range.suffix, len(data)):]
    raise TypeError(f"unexpected byte request {byte_range!r}")


def _request(byte_range: ByteRequest | None) -> Request | None:
    """zarr's byte request as refkv's."""
    if byte_range is None:
        return None
    if isinstance(byte_range, RangeByteRequest):
        return RangeRequest(byte_range.start, byte_range.end)
    if isinstance(byte_range, OffsetByteRequest):
        return OffsetRequest(byte_range.offset)
    if isinstance(byte_range, SuffixByteRequest):
        return SuffixRequest(byte_range.suffix)
    raise TypeError(f"unexpected byte request {byte_range!r}")


class ZarrStore(Store):
    """A reference store, read through zarr: a read-only `zarr.abc.store.Store` whose
    values are those of `reader` (a `refkv.ReferenceStore` or a `refkv.vzip.VZipStore`).

    `store(reader)` and `open(url)` make one. Reads go to the reference store, under
    its policy, request cap and `Stats`; the reference store stays reachable as
    `reader`. A reference store is immutable, so every write or delete raises the
    ValueError that zarr raises for a read-only store: `to_zarr(store)` or
    `zarr.open(store, mode="w")` fails at once. A changed store is a new store.
    """

    supports_writes = False
    supports_deletes = False
    supports_listing = True

    def __init__(self, reader: ReferenceReader) -> None:
        if not isinstance(reader, ReferenceReader):
            raise TypeError(f"refkv.zarr wraps a reference store (refkv.ReferenceStore or "
                            f"refkv.vzip.VZipStore), not {type(reader).__name__}")
        super().__init__(read_only=True)
        self.reader = reader

    def __eq__(self, value: object) -> bool:
        return isinstance(value, ZarrStore) and value.reader == self.reader

    __hash__ = object.__hash__

    def __repr__(self) -> str:
        return f"refkv.zarr.store({self.reader!r})"

    @override
    async def _open(self) -> None:
        await self.reader._ensure_open()  # noqa: SLF001 (the reference store's open)
        self._is_open = True

    # ------------------------------------------------------------ reads

    async def get(
        self, key: str, prototype: BufferPrototype, byte_range: ByteRequest | None = None
    ) -> Buffer | None:
        if key == "zarr.json" or key.endswith("/zarr.json"):
            # rewritten whole, then cut: a range of the rewritten document
            data = await self.reader.get_async(key)
            data = None if data is None else _slice(_hdf5_floats(bytes(data)), byte_range)
        else:
            data = await self.reader.get_async(key, _request(byte_range))
        return None if data is None else prototype.buffer.from_bytes(data)

    async def get_partial_values(
        self,
        prototype: BufferPrototype,
        key_ranges: Iterable[tuple[str, ByteRequest | None]],
    ) -> list[Buffer | None]:
        return await asyncio.gather(*(self.get(k, prototype, br) for k, br in key_ranges))

    async def exists(self, key: str) -> bool:
        return await self.reader.classify_async(key) != "missing"

    async def list(self) -> AsyncIterator[str]:
        async for k in self.reader.list_async(""):
            yield k

    async def list_prefix(self, prefix: str) -> AsyncIterator[str]:
        async for k in self.reader.list_async(prefix):
            yield k

    async def list_dir(self, prefix: str) -> AsyncIterator[str]:
        async for k in self.reader.list_dir_async(prefix):
            yield k

    # ------------------------------------------------------------ no mutations

    @override
    def _check_writable(self) -> None:
        raise ValueError(f"{type(self.reader).__name__} is read-only: a reference store cannot be "
                         f"changed; build a new one instead")

    @override
    def with_read_only(self, read_only: bool = False) -> ZarrStore:
        if not read_only:
            self._check_writable()
        return self

    @override
    async def set(self, key: str, value: Buffer) -> None:
        self._check_writable()

    @override
    async def set_if_not_exists(self, key: str, value: Buffer) -> None:
        self._check_writable()

    @override
    async def _set_many(self, values: Iterable[tuple[str, Buffer]]) -> None:
        self._check_writable()

    async def set_partial_values(
        self, key_start_values: Iterable[tuple[str, int, bytes]]  # noqa: ARG002 (zarr's Store signature)
    ) -> None:
        self._check_writable()

    @override
    async def delete(self, key: str) -> None:
        self._check_writable()

    @override
    async def delete_dir(self, prefix: str) -> None:
        self._check_writable()

    @override
    async def clear(self) -> None:
        self._check_writable()


def store(reader: ReferenceReader) -> ZarrStore:
    """`reader`, a reference store (`refkv.ReferenceStore`, or a `refkv.vzip.VZipStore`), as a
    read-only zarr store: `xr.open_zarr(refkv.zarr.store(store))`."""
    return ZarrStore(reader)


def open(url: str, **kwargs: Unpack[OpenOptions]) -> ZarrStore:  # noqa: A001 (refkv.zarr.open, the public name)
    """The store saved at `url` (a local path or an `http(s)` URL), opened by `refkv.open`
    (by default with the ZIP binding), as a read-only zarr store:
    `xr.open_zarr(refkv.zarr.open("data.vzip"))`.

    Keyword arguments go to `refkv.open`: `binding`, `policy`, `registry`, `stats`,
    `max_concurrency` (8 by default; None for no cap) and `resolve`."""
    return ZarrStore(refkv.open(url, **kwargs))


__all__ = ["ZarrStore", "open", "store"]
