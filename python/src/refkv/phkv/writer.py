"""`PhkvWriter`: write a standalone phkv store (spec/bindings/phkv.md §23
<https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#23-writing-a-phkv-store>) to a
binary file, with the Rust core's writer, `refkv._core.PhkvWriter`. The same input gives the
same file."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, BinaryIO, Literal, Self

from refkv import _core
from refkv.pb import Range, Source

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from types import TracebackType

type CodecName = Literal["none", "deflate", "zstd"]

_CODECS: dict[str, int] = {"none": 0, "deflate": 1, "zstd": 2}

# the types of the registered properties (spec/bindings/phkv.md §20.2
# <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#202-property-names>)
_REGISTERED: dict[str, str] = {"mtime": "i64", "mode": "u64", "media_type": "str"}


def _codec(name: CodecName) -> int:
    try:
        return _CODECS[name]
    except KeyError:
        raise ValueError(f"no codec {name!r}: one of {', '.join(map(repr, _CODECS))}") from None


class PhkvWriter:
    """Write a standalone phkv store to a binary file object.

    - `codec`: how bytes values are stored, `"none"` (the default), `"deflate"` or `"zstd"`;
      a compressed value can only be read whole.
    - `inline_max`: a value whose stored bytes are at most this long is kept in its record, which
      a lookup reads with the key (4096 by default).
    - `crc32c`: whether each bytes record carries the CRC-32C of its value (the default).
    - `section_codec`: the codec of the store's own sections, `"zstd"` by default.
    - `page_size`: about how many bytes of records each page of the page index holds.
    - `checksum(url, offset, length)`: if set, the CRC-32C of those bytes of a url source,
      recorded on every range of a url source that has none (spec §4.2
      <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>), as `VZipWriter` does.
    - `mirror_refs`: not supported. vzip's option writes a reference's payload as its entry's body
      too, for ZIP tools; a phkv record is the payload, so there is nothing to mirror, and any
      value other than None is a ValueError.

    The writer checks what spec/refkv.md §7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing> and §23.1 of the binding
    require, when a key is added and at `close`: a violation is a ValueError, and no store is
    written. Use it as a context manager to close it.
    """

    def __init__(
        self,
        f: BinaryIO,
        *,
        codec: CodecName = "none",
        inline_max: int = 4096,
        crc32c: bool = True,
        section_codec: CodecName = "zstd",
        page_size: int = 4096,
        checksum: Callable[[str, int, int], int] | None = None,
        mirror_refs: None = None,
    ) -> None:
        if mirror_refs is not None:
            raise ValueError("a phkv store has no mirror_refs: a reference's record is its payload")
        self._checksum = checksum
        self._urls: dict[int, str] = {}  # the url sources, by index
        self._w = _core.PhkvWriter(f, _codec(codec), inline_max, crc32c, _codec(section_codec), page_size)
        self._sources: dict[Source, int] = {}

    @property
    def n_sources(self) -> int:
        """The number of sources in the table so far."""
        return len(self._sources)

    def source(self, src: Source) -> int:
        """Intern `src` into the source table and return its index for `Range.source`.

        Index 0 is free on the wire, so register the most common source first.
        """
        i = self._sources.get(src)
        if i is None:
            i = self._sources[src] = self._w.source(src.encode())
            if src.url is not None:
                self._urls[i] = src.url
        return i

    def url(self, url: str) -> int:
        """Source index of an external object (absolute, or relative to the store)."""
        return self.source(Source(url=url))

    def internal(self, key: str) -> int:
        """Source index of another key of this store (checked at close)."""
        return self.source(Source(key=key))

    def blob(self, data: bytes) -> int:
        """Source index of literal bytes stored in the table, e.g. a shared header."""
        return self.source(Source(data=bytes(data)))

    def add_bytes(self, key: str, data: bytes, *, codec: CodecName | None = None) -> None:
        """Add a bytes value, stored with `codec` (the writer's by default)."""
        self._w.add_bytes(key, bytes(data), None if codec is None else _codec(codec))

    def add_file(self, key: str, path: str | os.PathLike[str]) -> None:
        """Add the bytes of the file at `path`, uncompressed, streaming it."""
        self._w.add_file(key, os.fspath(path))

    def add_ref(self, key: str, url: str, offset: int, length: int) -> None:
        self.add_ranges(key, [Range(source=self.url(url), offset=offset, length=length)])

    def add_ranges(self, key: str, ranges: Iterable[Range]) -> None:
        """Add a reference whose value is the concatenation of `ranges`.

        `Range.source` must come from `source`/`url`/`internal`/`blob`.
        """
        if self._checksum is not None:
            ranges = [self._with_checksum(r) for r in ranges]
        self._w.add_ranges(key, [bytes(r.data) if r.data is not None else (r.source, r.offset, r.length, r.crc32c)
                                 for r in ranges])

    def _with_checksum(self, r: Range) -> Range:
        """`r`, with the CRC-32C of its bytes if it is a range of a url source."""
        if self._checksum is None:
            raise AssertionError("_with_checksum without a checksum: the caller checks")
        if r.data is not None or r.crc32c is not None or (url := self._urls.get(r.source)) is None:
            return r
        return Range(r.source, r.offset, r.length, None, self._checksum(url, r.offset, r.length))

    def set_property(self, key: str, name: str, value: int | str | bytes) -> None:
        """Set a property of `key` (spec/bindings/phkv.md §20
        <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#20-properties>): `mtime` (an
        integer), `mode` (a non-negative integer), `media_type` (a string), or a name with a `:`."""
        if isinstance(value, bool):
            raise TypeError("a property is an int, a str or bytes, not a bool")
        match value:
            case int():
                kind = _REGISTERED.get(name) or ("u64" if value >= 0 else "i64")
            case str():
                kind = "str"
            case bytes():
                kind = "bytes"
            case _:
                raise TypeError(f"a property is an int, a str or bytes, not {value!r}")
        self._w.set_property(key, name, kind, value)

    def set_metadata(self, name: str, value: bytes) -> None:
        """Set an entry of the store's METADATA section (`writer`, or a name with a `:`)."""
        self._w.set_metadata(name, bytes(value))

    def close(self) -> None:
        """Write the records and the other sections."""
        self._w.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.close()


__all__ = ["CodecName", "PhkvWriter"]
