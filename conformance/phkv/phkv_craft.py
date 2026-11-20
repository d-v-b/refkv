"""Stores made from a valid phkv store by changing one thing, for the runner's `crafted/` and `probe/`
vectors: take a store apart into its sections (decoding those it stored compressed), change a
section, the directory or the superblock, and put it together again with the CRC-32Cs right, so that
only the change is wrong.

Layout of what is written: the sections one after another, the section directory, the superblock
(spec/bindings/phkv.md <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#17-container>),
then `distance` zero bytes, which are not the store's.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace

from phkv_validate import PAGES, PTRHASH, RECORDS, SLOTS, SOURCES, _decode, crc32c

__all__ = ["PAGES", "PTRHASH", "RECORDS", "SLOTS", "SOURCES", "Section", "Store", "build", "parse", "varint"]

SUPERBLOCK = 64
ENTRY = 40


@dataclass(frozen=True)
class Section:
    type: int
    data: bytes  # decoded, as stored with `codec`
    codec: int = 0
    flags: int = 0
    stored: bytes | None = None  # the stored bytes, if not `data` (a codec other than 0, or corrupt)
    size: int | None = None  # the directory's `size`, if not len(data)
    crc: int | None = None  # the directory's `crc32c`, if not that of the stored bytes


@dataclass(frozen=True)
class Store:
    sections: tuple[Section, ...]
    key_count: int


def varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def parse(buf: bytes) -> Store:
    """The sections of a standalone store whose superblock is last (distance 0), each decoded and
    kept as codec 0, so that a change to its bytes is the only change."""
    sb = buf[len(buf) - SUPERBLOCK:]
    key_count, dir_off, dir_len = struct.unpack_from("<QQQ", sb, 16)
    sections = []
    for i in range(dir_off, dir_off + dir_len, ENTRY):
        typ, codec, flags, _, off, length, size, _, _ = struct.unpack_from("<IBBHQQQII", buf, i)
        stored = buf[off:off + length]
        data = _decode(stored, codec, size, [], f"section {typ}")
        assert data is not None, f"cannot decode section {typ}"
        sections.append(Section(typ, data, 0, flags))
    return Store(tuple(sections), key_count)


def build(store: Store, *, distance: int = 0, key_count: int | None = None, version: int = 0, flags: int = 0) -> bytes:
    out = bytearray()
    entries = []
    for s in store.sections:
        stored = s.data if s.stored is None else s.stored
        entries.append(struct.pack("<IBBHQQQII", s.type, s.codec, s.flags, 0, len(out), len(stored),
                                   len(s.data) if s.size is None else s.size,
                                   crc32c(stored) if s.crc is None else s.crc, 0))
        out += stored
    directory = b"".join(entries)
    dir_off = len(out)
    out += directory
    reg_off = 0
    reg_len = len(out) + SUPERBLOCK
    sb = bytearray(b"PHKV" + struct.pack("<HHQQQQQQII", version, flags, distance,
                                         store.key_count if key_count is None else key_count,
                                         dir_off, len(directory), reg_off, reg_len, crc32c(directory), 0))
    sb[60:64] = crc32c(bytes(sb[:60])).to_bytes(4, "little")
    return bytes(out) + bytes(sb) + bytes(distance)


def section(store: Store, typ: int) -> Section:
    return next(s for s in store.sections if s.type == typ)


def with_section(store: Store, typ: int, **changes) -> Store:
    """`store` with its section of type `typ` changed (`dataclasses.replace` of it)."""
    return Store(tuple(replace(s, **changes) if s.type == typ else s for s in store.sections), store.key_count)


def without(store: Store, typ: int) -> Store:
    return Store(tuple(s for s in store.sections if s.type != typ), store.key_count)


def with_extra(store: Store, extra: Section) -> Store:
    return Store((*store.sections, extra), store.key_count)


def pages(data: bytes) -> list[tuple[bytes, int, int]]:
    """The page index as (first_key, offset, length)."""
    from phkv_validate import _varint  # noqa: PLC0415

    out, q = [], 0
    while q < len(data):
        n, q = _varint(data, q)
        first = data[q:q + n]
        q += n
        off, q = _varint(data, q)
        length, q = _varint(data, q)
        out.append((first, off, length))
    return out


def encode_pages(ps: list[tuple[bytes, int, int]]) -> bytes:
    return b"".join(varint(len(k)) + k + varint(o) + varint(n) for k, o, n in ps)


def oversized_payload_store(base: bytes, payload_length: int) -> bytes:
    """A one-key store (`base` written from a description of one reference to a literal, key `k`)
    whose record is a kind-2 reference with a payload of `payload_length` zero bytes."""
    st = parse(base)
    slots = section(st, SLOTS).data
    record = (varint(1) + bytes([2, 0]) + varint(0) + varint(payload_length) + b"k" + bytes(payload_length))
    new = bytearray(slots)
    for i in range(0, len(new), 12):
        if int.from_bytes(new[i + 8:i + 12], "little"):
            new[i:i + 8] = (0).to_bytes(8, "little")
            new[i + 8:i + 12] = len(record).to_bytes(4, "little")
    st = with_section(st, SLOTS, data=bytes(new))
    st = with_section(st, RECORDS, data=record)
    st = with_section(st, PAGES, data=encode_pages([(b"k", 0, len(record))]))
    return build(st)
