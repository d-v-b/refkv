"""Reference stores as read-only zarr stores, for zarr-python and xarray (the `zarr` extra).

A reference store (`refkv.ReferenceStore` in memory, `refkv.vzip.VZipStore` for an archive)
needs no Zarr: its keys are strings, and its values bytes or references. This
module wraps one as a `zarr.abc.store.Store`:

    import xarray as xr
    import refkv

    ds = xr.open_zarr(refkv.zarr.open("data.vzip"))  # a saved archive
    ds = xr.open_zarr(refkv.zarr.store(store))      # a refkv.ReferenceStore

Importing it also registers the codecs that archives of virtualized files and
stores use (`refkv.zarr.codecs`): JPEG, JPEG 2000 and JPEG XR (whose decoders
need the `images` extra), `refkv.stream` (whose decoders need the `media` extra), zlib, and
`n5_default`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from refkv._extras import missing

try:
    import zarr  # noqa: F401
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

import refkv.zarr.codecs  # noqa: E402,F401  registers the codecs vzip archives use
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
    supports_partial_writes = False
    supports_listing = True

    def __init__(self, reader: ReferenceReader) -> None:
        if not isinstance(reader, ReferenceReader):
            raise TypeError(f"refkv.zarr wraps a reference store (refkv.ReferenceStore or "
                            f"refkv.vzip.VZipStore), not {type(reader).__name__}")
        super().__init__(read_only=True)
        self.reader = reader

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ZarrStore) and other.reader == self.reader

    __hash__ = object.__hash__

    def __repr__(self) -> str:
        return f"refkv.zarr.store({self.reader!r})"

    async def _open(self) -> None:
        await self.reader._ensure_open()
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
        import asyncio

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

    def _check_writable(self) -> None:
        raise ValueError(f"{type(self.reader).__name__} is read-only: a reference store cannot be "
                         f"changed; build a new one instead")

    def with_read_only(self, read_only: bool = False) -> ZarrStore:
        if not read_only:
            self._check_writable()
        return self

    async def set(self, key: str, value: Buffer) -> None:
        self._check_writable()

    async def set_if_not_exists(self, key: str, value: Buffer) -> None:
        self._check_writable()

    async def _set_many(self, values) -> None:
        self._check_writable()

    async def set_partial_values(self, key_start_values) -> None:
        self._check_writable()

    async def delete(self, key: str) -> None:
        self._check_writable()

    async def delete_dir(self, prefix: str) -> None:
        self._check_writable()

    async def clear(self) -> None:
        self._check_writable()


def store(reader: ReferenceReader) -> ZarrStore:
    """`reader`, a reference store (`refkv.ReferenceStore`, or a `refkv.vzip.VZipStore`), as a
    read-only zarr store: `xr.open_zarr(refkv.zarr.store(store))`."""
    return ZarrStore(reader)


def open(url: str, **kwargs) -> ZarrStore:  # noqa: A001 - like refkv.open, zarr.open
    """The store saved at `url` (a local path or an `http(s)` URL), opened by `refkv.open`
    (by default with the ZIP binding), as a read-only zarr store:
    `xr.open_zarr(refkv.zarr.open("data.vzip"))`.

    Keyword arguments go to `refkv.open`: `binding`, `policy`, `registry`, `stats`,
    `max_concurrency` (8 by default; None for no cap) and `resolve`."""
    import refkv

    return ZarrStore(refkv.open(url, **kwargs))


__all__ = ["ZarrStore", "open", "store"]
