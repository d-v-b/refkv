"""The vzip archive's writer and the reading of its ZIP structure.

`VZipWriter` is rust/refkv-vzip's writer (`refkv._core.VzipWriter`), the one the CLI writes
with; the pure-Python writer it replaced is conformance/vzip/python_writer.py, which writes the
same bytes (python/tests/test_vzip_writer.py). The layout it writes:

    [local header + body]*      one per entry, STORED unless compress=True; no local
                                extra field, except a 20-byte ZIP64 field on an entry
                                of 4 GiB or more (spec §10.1
                                <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#101-structure>
                                rules 4 and 7)
    [SourceTable]               reserved entry "__vz__/sources" (deflated)
    [late entries]              entries added with late=True (e.g. zarr.json), so a
                                reader's open request covers them
    [CdIndex]                   only with page_size: sparse index over the CD
    [central directory]         sorted; __vz__/sources and __vz__/index records last.
                                Reference entries carry extra field 0x7a76 with a
                                serialized Range, or 0x7a77 with a serialized Concat
    [zip64 EOCD + locator]      always (spec §10.2
                                <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#102-zip64>);
                                the EOCD's counts, size and offset are all ones
    [EOCD]                      archive comment = b"vzip/0" + u64 offset + u64 size
                                of the (deflated) SourceTable body [+ the same for
                                the CdIndex]

The comment promises that every local header has an empty extra field (or, for
an entry of 4 GiB or more, exactly the 20-byte ZIP64 one), so a reader can
compute the offset of any body from the central directory alone,
and lets a reader fetch the central directory and URL table in one request.
Archives without the comment are still readable; the reader then reads each
local header before trusting the body offset.
"""

from __future__ import annotations

import itertools
import os
import struct
import zlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import BinaryIO, Self

from refkv.pb import (
    Concat,
    Range,
    Reference,
    Source,
    decode_source_table,
    decode_table,
)
from refkv.vzip.names import INDEX_KEY as INDEX_KEY  # re-exported: refkv.vzip.store imports them here
from refkv.vzip.names import RESERVED_PREFIX as RESERVED_PREFIX
from refkv.vzip.names import SOURCES_KEY as SOURCES_KEY

MAX_PAYLOAD = 65519
RANGE_EXTRA_ID = 0x7A76  # "vz": payload is a Range
CONCAT_EXTRA_ID = 0x7A77  # payload is a Concat
ZIP64_EXTRA_ID = 0x0001
FORMAT_VERSION = 0
# The revision of spec/refkv.md the writer follows, recorded in every archive's source table (§1.3
# <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#13-versioning>, §5
# <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>)
SPEC_REVISION = 11
MAGIC_COMMENT = f"vzip/{FORMAT_VERSION}".encode()

_LFH = struct.Struct("<IHHHHHIIIHH")  # 30 bytes
_CDH = struct.Struct("<IHHHHHHIIIHHHHHII")  # 46 bytes
_EOCD = struct.Struct("<IHHHHIIH")  # 22 bytes
_EOCD64 = struct.Struct("<IQHHIIQQQQ")  # 56 bytes
_LOC64 = struct.Struct("<IIQI")  # 20 bytes

_SIG_LFH = 0x04034B50
_SIG_CDH = 0x02014B50
_SIG_EOCD = 0x06054B50
_SIG_EOCD64 = 0x06064B50
_SIG_LOC64 = 0x07064B50

_U32 = 0xFFFFFFFF
_U16 = 0xFFFF
_FLAG_UTF8 = 0x0800
_DOS_DATE = (0 << 9) | (1 << 5) | 1  # 1980-01-01, for reproducible archives
_MADE_BY = 20  # spec §14.2 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#142-recommended-layout-informative>
_EXT_ATTR = 0


@dataclass
class Entry:
    name: str
    header_offset: int
    size: int  # uncompressed
    csize: int
    method: int
    # (extra field header ID, payload) for reference entries; decoded lazily
    payload: tuple[int, bytes] | None = None
    # offset of the body, if it is known without reading the local header
    data_offset: int | None = None
    # spec §6.4 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#64-errors> entry error:
    # every operation on this key fails with it
    error: str | None = None
    _ref: Reference | None = None

    @property
    def is_ref(self) -> bool:
        return self.payload is not None

    @property
    def ref(self) -> Reference | None:
        """The decoded payload; raises ValueError if it is malformed (a payload error)."""
        if self.payload is None:
            return None
        if self._ref is None:
            hid, raw = self.payload
            # spec §11.3 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#113-reference-entries>:
            # a payload error, even if it fits
            if len(raw) > MAX_PAYLOAD:
                raise ValueError(f"reference payload of {len(raw)} bytes exceeds {MAX_PAYLOAD}")
            self._ref = (Range if hid == RANGE_EXTRA_ID else Concat).decode(raw)
        return self._ref


class VZipWriter:
    """Stream a vzip archive to a binary file object: rust/refkv-vzip's writer
    (`refkv._core.VzipWriter`), the one the CLI writes with.

    Each `add_*` call writes its entry to `f` at once; `close` writes the source table, the
    late entries, the page index and the central directory. As a context manager, the
    writer closes when the block ends without an exception; when one escapes the block,
    `close` is skipped and `f` holds entries without a central directory, which no reader
    opens.
    """

    def __init__(
        self, f: BinaryIO, *, mirror_refs: bool = True, page_size: int | None = None,
        checksum: Callable[[str, int, int], int] | None = None,
    ) -> None:
        """`mirror_refs`: also write each reference's payload as its entry's body, for ZIP
        tools that ignore extra fields (spec §11.3
        <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#113-reference-entries>);
        otherwise a reference entry's body is empty.

        `page_size`: if set, also write a CdIndex with pages of ~page_size bytes.

        `checksum(url, offset, length)`: if set, the CRC-32C of those bytes of a url
        source, recorded on every range of a url source that has none (spec §4.2
        <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>)."""
        from refkv import _core  # noqa: PLC0415 (`import refkv` imports no ZIP code)

        self._w = _core.VzipWriter(f, mirror_refs, page_size)
        self._checksum = checksum
        self._urls: list[str | None] = []  # each source's url (None for another kind), for checksums
        self._count: int | None = None  # the table's sources, once closed

    def source(self, src: Source) -> int:
        """Intern `src` into the source table and return its index for `Range.source`.

        Index 0 is free on the wire, so register the most common source first.
        """
        i = self._w.source(src.encode())
        if i == len(self._urls):
            self._urls.append(src.url)
        return i

    @property
    def n_sources(self) -> int:
        """The number of sources in the table so far (after `close`, its final count)."""
        return self._w.source_count() if self._count is None else self._count

    def url(self, url: str) -> int:
        """Source index of an external object (absolute, or relative to the archive)."""
        return self.source(Source(url=url))

    def internal(self, key: str) -> int:
        """Source index of another entry of this archive (checked at close)."""
        return self.source(Source(key=key))

    def blob(self, data: bytes) -> int:
        """Source index of literal bytes stored in the table, e.g. a shared header."""
        return self.source(Source(data=bytes(data)))

    def add_file(self, key: str, path: str | os.PathLike[str], *, block: int = 1 << 24) -> None:
        """Add the bytes of the file at `path` as a STORED entry, streaming it
        (twice: once for its CRC-32, once to copy it), so entries of any size,
        such as Zarr shards of 4 GiB or more, never sit in memory. `block`, the size of
        the pure-Python writer's reads, is kept for its callers; the Rust writer picks its own."""
        del block
        self._w.add_file(key, os.fspath(path))

    def add_bytes(self, key: str, data: bytes, *, compress: bool = False, late: bool = False) -> None:
        """Add a concrete entry. Compressed entries can only be read whole.

        `late=True` defers the entry to just before the central directory, where
        the reader's open request will fetch it (use for small metadata documents).
        """
        self._w.add_bytes(key, bytes(data), compress, late)

    def add_hidden(self, name: str, data: bytes, *, compress: bool = True, late: bool = False) -> str:
        """Add an entry outside the zarr key space; returns its key (see `internal`)."""
        return self._w.add_hidden(name, bytes(data), compress, late)

    def add_ref(self, key: str, url: str, offset: int, length: int) -> None:
        """Add a reference to bytes [offset, offset+length) of the object at `url` (interned with
        `url`): `add_ranges` with one range."""
        self.add_ranges(key, [Range(source=self.url(url), offset=offset, length=length)])

    def _crc(self, r: Range) -> int | None:
        """The CRC-32C a range of a url source carries: its own, else the checksum's."""
        if r.crc32c is not None or self._checksum is None or r.data is not None:
            return r.crc32c
        url = self._urls[r.source] if r.source < len(self._urls) else None
        return None if url is None else self._checksum(url, r.offset, r.length)

    def add_ranges(self, key: str, ranges: list[Range]) -> None:
        """Add a reference whose value is the concatenation of `ranges`.

        One range is stored as a bare Range (extra field 0x7a76); several as a
        Concat (0x7a77). `Range.source` must come from `source`/`url`/`internal`/`blob`.
        """
        parts: list[bytes | tuple[int, int, int, int | None]] = []
        for r in ranges:
            if r.data is None:
                parts.append((r.source, r.offset, r.length, self._crc(r)))
            elif r.crc32c is not None:
                raise ValueError(
                    f"literal range of {key!r} with a crc32c (spec §7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing>)")
            else:
                parts.append(bytes(r.data))
        self._w.add_ranges(key, parts)

    def add_url_refs(self, items: Iterable[tuple[str, str, int]], *, pin_size: bool = False) -> None:
        """Bulk path for many objects referenced whole: for each (key, url, size),
        a new url source (not interned) and a reference entry `(source, 0, size)`.
        With `pin_size`, each source also pins the object's size (spec §5.1
        <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#51-pins>).

        Taken 65536 at a time, so that millions of entries are never all held."""
        checksum = self._checksum
        for batch in itertools.batched(items, 1 << 16):
            self._urls.extend(url for _, url, _ in batch)
            self._w.add_url_refs([(k, u, n, None if checksum is None else checksum(u, 0, n)) for k, u, n in batch],
                                 pin_size)

    @property
    def format_sizes(self) -> dict[str, int]:
        """The sizes, uncompressed, of the format entries written (the source table and the
        page index, once `close` has written them), which a reader bounds by its
        `max_format_entry` (spec §13.1
        <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#131-opening>)."""
        return dict(self._w.format_sizes())

    def close(self) -> None:
        """Check the archive, then write its source table, its late entries, its page index
        (with `page_size`) and its central directory. `f` stays open: the caller closes it.

        Raises ValueError, before anything more is written, when the source table breaks a
        writer requirement (spec §7
        <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing>): an empty or invalid
        url source, an empty key source, a key source naming a format entry, a missing entry or
        a reference entry, or a range past the end of an internal or data source."""
        count = self._w.source_count()
        self._w.close()
        self._count = count

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        if exc[0] is None:
            self.close()


# ---------------------------------------------------------------- reading


TAIL_GUESS = 1 << 16  # first speculative read from the end of the archive


@dataclass(slots=True)
class Directory:
    """Locations of the central directory, decoded from the archive tail."""

    cd_offset: int
    cd_size: int
    n_entries: int
    is_vzip: bool
    sources_offset: int  # body of the deflated SourceTable
    sources_size: int = 0
    index_offset: int | None = None  # body of the deflated CdIndex, if paged
    index_size: int = 0


def parse_tail(tail: bytes, file_size: int) -> Directory:
    """Decode the end records of a vzip archive from its last bytes (spec §10.4
    <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#104-end-of-central-directory-record-and-archive-comment>)."""
    for clen in (38, 22):
        i = len(tail) - _EOCD.size - clen
        if i >= 0 and tail[i : i + 4] == struct.pack("<I", _SIG_EOCD) and _EOCD.unpack_from(tail, i)[7] == clen:
            break
    else:
        raise ValueError("not a vzip archive (no end of central directory record "
                         "with a 22- or 38-byte comment)")
    comment = tail[i + _EOCD.size :]
    if not comment.startswith(b"vzip/"):
        raise ValueError(f"not a vzip archive (comment starts {comment[:6]!r})")
    if not comment.startswith(MAGIC_COMMENT):
        raise ValueError(f"unsupported vzip format version {comment[5:6]!r}; this reader "
                         f"implements version {FORMAT_VERSION}")
    # spec §10.2 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#102-zip64>: the
    # directory's size and offset always come from the zip64 record; the EOCD's own counts, size and
    # offset are ignored
    loc = i - _LOC64.size
    if loc < 0 or tail[loc : loc + 4] != struct.pack("<I", _SIG_LOC64):
        raise ValueError("no zip64 end of central directory locator before the end of "
                         "central directory record")
    _, _, eocd64_off, _ = _LOC64.unpack_from(tail, loc)
    j = eocd64_off - (file_size - len(tail))
    if j < 0 or eocd64_off + _EOCD64.size > file_size:
        raise ValueError("zip64 EOCD outside the file or the tail buffer")
    (sig, rec_size, _, _, _, _, _, n, cd_size, cd_off) = _EOCD64.unpack_from(tail, j)
    if sig != _SIG_EOCD64 or rec_size != 44:
        raise ValueError("bad zip64 EOCD")
    soff, ssize = struct.unpack_from("<QQ", comment, len(MAGIC_COMMENT))
    ioff, isize = (None, 0)
    if clen == 38:
        ioff, isize = struct.unpack_from("<QQ", comment, len(MAGIC_COMMENT) + 16)
    return Directory(cd_off, cd_size, n, True, soff, ssize, ioff, isize)


def _is_large(size: int, csize: int) -> bool:
    """Whether an entry needs ZIP64 sizes (spec §10.1
    <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#101-structure> rule 7)."""
    return size >= _U32 or csize >= _U32


def _iter_extra(extra: bytes) -> Iterator[tuple[int, bytes]]:
    pos = 0
    while pos < len(extra):
        if pos + 4 > len(extra):
            raise ValueError("truncated extra field block header")
        hid, n = struct.unpack_from("<HH", extra, pos)
        if pos + 4 + n > len(extra):
            raise ValueError("extra field block runs past the end of the extra field")
        yield hid, extra[pos + 4 : pos + 4 + n]
        pos += 4 + n


def parse_central_directory(cd: bytes, *, trust_offsets: bool) -> dict[str, Entry]:
    """Decode the central directory into {key: Entry}.

    This is the classifier for the overlay (spec §11.1
    <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#111-classification>): an entry with one
    0x7a76 or 0x7a77 extra field block belongs to the reference key space, any
    other entry to the bytes key space, and a key with no entry to neither.
    Problems confined to one record become that entry's `error` (spec §13.3
    <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#133-errors>). """
    entries: dict[str, Entry] = {}
    mv = memoryview(cd)
    pos = 0
    while pos < len(cd):
        if pos + _CDH.size > len(cd):
            raise ValueError("truncated central directory record")
        (sig, _, _, flags, method, _, _, _, csize, size, nlen, xlen, clen, _, _, _, off) = (
            _CDH.unpack_from(mv, pos)
        )
        if sig != _SIG_CDH:
            raise ValueError(f"bad central directory record at {pos}")
        pos += _CDH.size
        if pos + nlen + xlen + clen > len(cd):
            raise ValueError("central directory record runs past the directory")
        try:
            name = bytes(mv[pos : pos + nlen]).decode("utf-8")
        except UnicodeDecodeError:
            name = ""
        pos += nlen
        extra = bytes(mv[pos : pos + xlen])
        pos += xlen + clen
        # spec §10.3 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#103-file-names>: such
        # records name no key
        if not name:
            continue
        if name in entries:
            raise ValueError(f"duplicate central directory record for {name!r}")
        payload: tuple[int, bytes] | None = None
        error: str | None = None
        ref_blocks = 0
        z64: list[bytes] = []
        try:
            for hid, data in _iter_extra(extra):
                if hid in (RANGE_EXTRA_ID, CONCAT_EXTRA_ID):
                    ref_blocks += 1
                    payload = (hid, data)
                elif hid == ZIP64_EXTRA_ID:
                    z64.append(data)
        except (ValueError, IndexError, struct.error) as e:
            error = f"unparseable extra field: {e}"
        # spec §10.2 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#102-zip64>: a large
        # entry's sizes, then an offset of 4 GiB or more, come from the one ZIP64 block, in that order
        large = size == _U32 and csize == _U32
        need = (16 if large else 0) + (8 if off == _U32 else 0)
        if error is None and len(z64) > 1:
            error = "more than one ZIP64 extra block"
        elif error is None and (size == _U32) != (csize == _U32):
            error = "exactly one size field is 0xFFFFFFFF"
        elif error is None and need:
            if not z64 or len(z64[0]) < need:
                error = "ZIP64 extra block missing or too short"
            else:
                block = z64[0]
                if large:
                    size, csize = struct.unpack_from("<QQ", block)
                if off == _U32:
                    (off,) = struct.unpack_from("<Q", block, 16 if large else 0)
        if error is None:
            if ref_blocks > 1:
                error = "more than one reference extra field block"
            elif method not in (0, 8):
                error = f"unsupported compression method {method}"
            elif flags & 1:
                error = "encrypted entry"
            elif large and method != 0:
                error = "large entry is not STORED"
            elif payload is not None and method != 0:
                error = "reference entry is not STORED"
            elif payload is not None and large:
                error = "reference entry is large"
        data_offset = off + _LFH.size + nlen + (20 if large else 0) if trust_offsets else None
        entries[name] = Entry(name, off, size, csize, method, payload, data_offset, error)
    return entries


class InflateLimitError(ValueError):
    """A DEFLATE stream inflates to more than the reader allows (spec §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 6)."""


def inflate_clean(raw: bytes, limit: int | None = None) -> bytes:
    """Inflate one complete raw DEFLATE stream that fills `raw` exactly (spec §13.1
    <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#131-opening>).

    With `limit`, inflating stops as soon as the output passes `limit` bytes,
    which raises InflateLimitError (spec §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 6)."""
    d = zlib.decompressobj(-15)
    if limit is None:
        out = d.decompress(raw)
    else:
        out = d.decompress(raw, limit + 1)
        if len(out) > limit:
            raise InflateLimitError(f"inflates to more than {limit} bytes")
    if not d.eof:
        raise ValueError("DEFLATE stream is incomplete")
    if d.unused_data:
        raise ValueError(f"{len(d.unused_data)} bytes follow the DEFLATE stream")
    return out


def parse_local_header(buf: bytes) -> int:
    """Return the length of a local file header (i.e. offset of the body)."""
    sig, *_, nlen, xlen = _LFH.unpack_from(buf, 0)
    if sig != _SIG_LFH:
        raise ValueError("bad local file header")
    return _LFH.size + nlen + xlen


LFH_SIZE = _LFH.size


def read_source_table(body: bytes) -> list[Source]:
    return decode_source_table(body)


def read_table(body: bytes) -> tuple[list[Source], int | None]:
    """The sources and the recorded spec revision of a SourceTable (spec §5
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>)."""
    return decode_table(body)
