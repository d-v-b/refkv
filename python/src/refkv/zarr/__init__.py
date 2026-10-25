"""Reference stores as read-only zarr stores, for zarr-python and xarray (the `zarr` extra).

A reference store (`refkv.ReferenceStore` in memory, `refkv.vzip.VZipStore` for an archive)
needs no Zarr: its keys are strings, and its values bytes or references. This
module wraps one as a `zarr.abc.store.Store`:

    import xarray as xr
    import refkv

    ds = xr.open_zarr(refkv.zarr.open("data.vzip"))  # a saved archive
    ds = xr.open_zarr(refkv.zarr.store(store))      # a refkv.ReferenceStore

Importing it also registers the codecs that archives of zarrified files and
stores use (`refkv.zarr.codecs`, one module per codec; the image codecs' decoders
need the `images` extra, the `refkv.stream` codec's the `media` extra), and turns on
zarr-python's `array.rectilinear_chunks`, which the zarr-extensions `rectilinear` chunk
grid of some archives' arrays needs (an MP4 file's video planes, a clipped CZI level).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING, Unpack, override

from refkv._extras import missing

if TYPE_CHECKING:
    from refkv import OpenOptions

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

zarr.config.set({"array.rectilinear_chunks": True})
from refkv.reader import (  # noqa: E402
    OffsetRequest,
    RangeRequest,
    ReferenceReader,
    Request,
    SuffixRequest,
)


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
