"""Structural validation of a written phkv store against spec/bindings/phkv.md and its description.

Has its own parser (the superblock, the section directory, the sections, the records and the page
index) and uses the official protobuf runtime (pbref) for the source table, so it shares no code
with any implementation. It does not rebuild the perfect hash: the reads of the suite do that, for
every key, through every implementation. Returns a list of problems (empty = valid).
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vzip"))

import pbref

MAGIC = b"PHKV"
SOURCES, RECORDS, PAGES, PTRHASH, SLOTS, PROPERTY, METADATA = range(1, 8)
ONE_PER_STORE = (SOURCES, RECORDS, PAGES, PTRHASH, SLOTS)
STORED_ONLY = (RECORDS, SLOTS)  # codec 0 only (§17.4)

_CRC_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ (0x82F63B78 if _c & 1 else 0)
    _CRC_TABLE.append(_c)


def crc32c(data: bytes) -> int:
    c = 0xFFFFFFFF
    for b in data:
        c = _CRC_TABLE[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


def _varint(b: bytes, p: int) -> tuple[int, int]:
    v = shift = 0
    while True:
        if p >= len(b):
            raise ValueError("truncated varint")
        x = b[p]
        p += 1
        v |= (x & 0x7F) << shift
        if not x & 0x80:
            return v, p
        shift += 7
        if shift > 63:
            raise ValueError("overlong varint")


def _decode(stored: bytes, codec: int, size: int, problems: list, what: str) -> bytes | None:
    if codec == 0:
        return stored
    try:
        if codec == 1:
            d = zlib.decompressobj(-15)
            out = d.decompress(stored)
            if not d.eof or d.unused_data:
                problems.append(f"{what}: DEFLATE stream does not end exactly at the end of the stored bytes")
        elif codec == 2:
            import imagecodecs  # noqa: PLC0415

            out = bytes(imagecodecs.zstd_decode(stored))
        else:
            problems.append(f"{what}: codec {codec} is reserved")
            return None
    except Exception as e:  # noqa: BLE001
        problems.append(f"{what}: does not decode: {e}")
        return None
    if len(out) != size:
        problems.append(f"{what}: decodes to {len(out)} bytes, not size {size}")
    return out


def _check_sources(table_bytes: bytes, desc: dict, problems: list) -> None:
    table = pbref.SourceTable.FromString(table_bytes)
    want_all = desc.get("sources", [])
    if len(table.sources) != len(want_all):
        problems.append(f"source table holds {len(table.sources)} sources, the description {len(want_all)}")
    else:
        for i, (got, want) in enumerate(zip(table.sources, want_all, strict=True)):
            kind = got.WhichOneof("kind")
            expected_kind = next(k for k in ("url", "key", "data") if k in want)
            value = getattr(got, kind) if kind else None
            value = value.hex() if kind == "data" else value
            if kind != expected_kind or value != want[expected_kind]:
                problems.append(f"source {i}: {kind} {value!r}, expected {expected_kind} {want[expected_kind]!r}")
            problems.extend(
                f"source {i}: pin {pin} differs from the description"
                for pin in ("size", "etag", "modified_not_after")
                if got.HasField(pin) != (pin in want) or (pin in want and getattr(got, pin) != want[pin]))
    if not table.HasField("revision"):
        problems.append("source table records no revision")


def _check_records(records: bytes, desc: dict, key_count: int, problems: list) -> None:
    expected = {e["key"]: e for e in desc["entries"]}
    seen: list[str] = []
    last: bytes | None = None
    p = rank = 0
    while p < len(records):
        start = p
        data = payload = None
        try:
            klen, p = _varint(records, p)
            kind, rflags = records[p], records[p + 1]
            p += 2
            r, p = _varint(records, p)
            codec = size = stored_len = crc = None
            if kind == 0:
                codec = records[p]
                p += 1
                if not rflags & 1:
                    _, p = _varint(records, p)  # offset of the stored bytes
                stored_len, p = _varint(records, p)
                size, p = _varint(records, p)
                if rflags & 2:
                    (crc,) = struct.unpack_from("<I", records, p)
                    p += 4
            elif kind in (1, 2):
                plen, p = _varint(records, p)
            else:
                problems.append(f"record at {start}: kind {kind} is not 0, 1 or 2")
                return
            key = records[p:p + klen].decode("utf-8")
            p += klen
            if kind == 0 and rflags & 1:
                data = records[p:p + stored_len]
                p += stored_len
            elif kind != 0:
                payload = records[p:p + plen]
                p += plen
        except (ValueError, IndexError, struct.error) as e:
            problems.append(f"record at {start}: malformed: {e}")
            return
        if p > len(records):
            problems.append(f"record at {start}: extends past the end of RECORDS")
            return
        if klen < 1 or rflags & ~7 or rflags & 4 or (kind != 0 and rflags & 3) or r != rank:
            problems.append(f"record {key!r}: bad key length, flags or rank")
        if last is not None and key.encode() <= last:
            problems.append(f"record {key!r}: keys are not in strictly increasing UTF-8 order")
        last = key.encode()
        seen.append(key)
        rank += 1
        e = expected.get(key)
        if e is None:
            problems.append(f"record {key!r}: not in the description")
        elif (kind == 0) != ("bytes" in e):
            problems.append(f"record {key!r}: kind {kind} does not match the description")
        elif kind != 0:
            range_id, canonical = pbref.payload(e["ranges"])
            if kind != (1 if range_id == 0x7A76 else 2) or payload != canonical:
                problems.append(f"record {key!r}: payload differs from the canonical encoding")
        else:
            want = bytes.fromhex(e["bytes"])
            if size != len(want):
                problems.append(f"record {key!r}: size {size}, expected {len(want)}")
            if crc is not None and crc != crc32c(want):
                problems.append(f"record {key!r}: crc32c is not that of the value")
            if data is not None:
                value = _decode(data, codec, size, problems, f"value of {key!r}")
                if value is not None and value != want:
                    problems.append(f"record {key!r}: value differs from the description")
    if sorted(seen) != sorted(expected):
        problems.append("the records' keys differ from the description's")
    if len(seen) != key_count:
        problems.append(f"{len(seen)} records, key_count {key_count}")


def _check_pages(pages: bytes, records_length: int, problems: list) -> None:
    q = at = 0
    prev = None
    try:
        while q < len(pages):
            first_length, q = _varint(pages, q)
            first = pages[q:q + first_length]
            q += first_length
            _, q = _varint(pages, q)  # the page's offset: absolute (§19.4)
            length, q = _varint(pages, q)
            if first_length < 1 or length < 1:
                problems.append("a page with an empty first key or length")
            if prev is not None and first <= prev:
                problems.append("page first keys are not strictly increasing")
            prev = first
            at += length
    except ValueError as e:
        problems.append(f"PAGES does not decode: {e}")
        return
    if at != records_length:
        problems.append(f"pages cover {at} bytes of RECORDS, which is {records_length} long")


def validate(path: Path, desc: dict) -> list[str]:
    problems: list[str] = []
    buf = Path(path).read_bytes()
    if len(buf) < 64:
        return ["file shorter than a superblock"]
    sb = buf[len(buf) - 64:]  # these writers write the superblock last (distance 0)
    if sb[:4] != MAGIC:
        return ["no superblock at the end of the file"]
    version, flags, distance, key_count, dir_off, dir_len, reg_off, reg_len, dir_crc, sb_crc = (
        struct.unpack_from("<HHQQQQQQII", sb, 4))
    if crc32c(sb[:60]) != sb_crc:
        problems.append("superblock crc32c is wrong")
    if version != 0:
        problems.append(f"version is {version}, not 0")
    if flags != 0:
        problems.append(f"flags are {flags:#x}: a standalone store has none")
    if distance != 0:
        problems.append(f"distance is {distance}, not 0 (the writer wrote the superblock last)")
    if key_count != len(desc["entries"]):
        problems.append(f"key_count is {key_count}, the description has {len(desc['entries'])} entries")
    if dir_len % 40 or dir_off + dir_len > len(buf):
        return [*problems, "the section directory is outside the file or has a partial entry"]
    directory = buf[dir_off:dir_off + dir_len]
    if crc32c(directory) != dir_crc:
        problems.append("directory crc32c is wrong")
    if reg_off + reg_len != len(buf):
        problems.append(f"container region [{reg_off}, +{reg_len}) does not end at the end of the superblock")
    sections: dict[int, list[tuple[int, bytes, int]]] = {}
    for i in range(0, dir_len, 40):
        typ, codec, sflags, res1, off, length, size, crc, res2 = struct.unpack_from("<IBBHQQQII", directory, i)
        what = f"section type {typ}"
        if res1 or res2 or sflags & ~1:
            problems.append(f"{what}: reserved bits are set")
        if typ == 0 or 8 <= typ < 0x80000000:
            problems.append(f"{what}: reserved type")
        if off + length > len(buf):
            problems.append(f"{what}: lies outside the file")
            continue
        stored = buf[off:off + length]
        if crc32c(stored) != crc:
            problems.append(f"{what}: crc32c is wrong")
        if codec == 0 and length != size:
            problems.append(f"{what}: codec 0 with length {length} != size {size}")
        if typ in STORED_ONLY and codec != 0:
            problems.append(f"{what}: must be stored (codec 0)")
        sections.setdefault(typ, []).append((codec, stored, size))
    for typ in ONE_PER_STORE:
        want = 1 if typ == SOURCES or key_count >= 1 else 0
        if len(sections.get(typ, [])) != want:
            problems.append(f"section type {typ}: {len(sections.get(typ, []))} of them, expected {want}")
    if len(sections.get(METADATA, [])) > 1:
        problems.append("more than one METADATA section")
    if SOURCES not in sections or (key_count and (RECORDS not in sections or PAGES not in sections)):
        return problems

    codec, stored, size = sections[SOURCES][0]
    raw = _decode(stored, codec, size, problems, "SOURCES")
    if raw is not None:
        _check_sources(raw, desc, problems)
    if key_count:
        records = sections[RECORDS][0][1]
        _check_records(records, desc, key_count, problems)
        codec, stored, size = sections[PAGES][0]
        pages = _decode(stored, codec, size, problems, "PAGES")
        if pages is not None:
            _check_pages(pages, len(records), problems)
    return problems
