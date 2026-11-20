"""Structural validation of a written phkv store against spec/bindings/phkv.md and its description.

Has its own parser (the superblock, the section directory, the sections, the records and the page
index) and uses the official protobuf runtime (pbref) for the source table, so it shares no code
with any implementation. It checks the PTRHASH section's header and the SLOTS section's length and
slots (each record has exactly one slot, with its exact length), but does not rebuild the perfect
hash: the reads of the suite look up every key of the stores that need several buckets (`many_keys`),
through every implementation. Returns a list of problems (empty = valid).
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
STORED_ONLY = (RECORDS, SLOTS)  # codec 0 only (<https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#174-sections>)

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
            raw_value = getattr(got, kind) if kind else None
            value = raw_value.hex() if kind == "data" and raw_value is not None else raw_value
            if kind != expected_kind or value != want[expected_kind]:
                problems.append(f"source {i}: {kind} {value!r}, expected {expected_kind} {want[expected_kind]!r}")
            problems.extend(
                f"source {i}: pin {pin} differs from the description"
                for pin in ("size", "etag", "modified_not_after")
                if got.HasField(pin) != (pin in want) or (pin in want and getattr(got, pin) != want[pin]))
    if not table.HasField("revision"):
        problems.append("source table records no revision")


def _check_records(
    records: bytes, desc: dict, key_count: int, problems: list, whole: bytes = b"",
) -> list[tuple[str, int, int]]:
    """Checks the records; returns each record's (key, start, end) in RECORDS."""
    expected = {e["key"]: e for e in desc["entries"]}
    seen: list[str] = []
    spans: list[tuple[str, int, int]] = []
    last: bytes | None = None
    p = rank = 0
    while p < len(records):
        start = p
        data = payload = None
        offset = 0
        try:
            klen, p = _varint(records, p)
            kind, rflags = records[p], records[p + 1]
            p += 2
            r, p = _varint(records, p)
            codec = size = stored_len = plen = 0
            crc = None
            if kind == 0:
                codec = records[p]
                p += 1
                if not rflags & 1:
                    offset, p = _varint(records, p)  # offset of the stored bytes, in the file
                stored_len, p = _varint(records, p)
                size, p = _varint(records, p)
                if rflags & 2:
                    (crc,) = struct.unpack_from("<I", records, p)
                    p += 4
            elif kind in (1, 2):
                plen, p = _varint(records, p)
            else:
                problems.append(f"record at {start}: kind {kind} is not 0, 1 or 2")
                return spans
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
            return spans
        if p > len(records):
            problems.append(f"record at {start}: extends past the end of RECORDS")
            return spans
        if klen < 1 or rflags & ~7 or rflags & 4 or (kind != 0 and rflags & 3) or r != rank:
            problems.append(f"record {key!r}: bad key length, flags or rank")
        if last is not None and key.encode() <= last:
            problems.append(f"record {key!r}: keys are not in strictly increasing UTF-8 order")
        last = key.encode()
        seen.append(key)
        spans.append((key, start, p))
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
            if data is None:  # stored out of line: [offset, offset + stored_len) of the file
                if offset + stored_len > len(whole):
                    problems.append(f"record {key!r}: stored bytes [{offset}, +{stored_len}) lie outside the file")
                else:
                    data = whole[offset:offset + stored_len]
            if data is not None:
                value = _decode(data, codec, size, problems, f"value of {key!r}")
                if value is not None and value != want:
                    problems.append(f"record {key!r}: value differs from the description")
    if sorted(seen) != sorted(expected):
        problems.append("the records' keys differ from the description's")
    if len(seen) != key_count:
        problems.append(f"{len(seen)} records, key_count {key_count}")
    return spans


def _check_pages(pages: bytes, spans: list[tuple[str, int, int]], base: int, problems: list) -> None:
    """The pages partition RECORDS, which starts at `base` in the file: each page starts where the previous
    ended, at the record of its first key."""
    starts = {start: key for key, start, _ in spans}
    records_length = spans[-1][2] if spans else 0
    q = at = 0
    prev = None
    try:
        while q < len(pages):
            first_length, q = _varint(pages, q)
            first = pages[q:q + first_length]
            q += first_length
            offset, q = _varint(pages, q)  # the page's offset in the file (<https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#194-page-index>)
            length, q = _varint(pages, q)
            if first_length < 1 or length < 1:
                problems.append("a page with an empty first key or length")
            if prev is not None and first <= prev:
                problems.append("page first keys are not strictly increasing")
            if offset != base + at:
                problems.append(f"page at {at}: its offset is {offset}, not {base + at}")
            if starts.get(offset - base) != first.decode("utf-8", "replace"):
                problems.append(f"page at {offset}: first_key {first!r} is not the key of the record there")
            prev = first
            at += length
    except ValueError as e:
        problems.append(f"PAGES does not decode: {e}")
        return
    if at != records_length:
        problems.append(f"pages cover {at} bytes of RECORDS, which is {records_length} long")


def _check_hash(ptrhash: bytes, slots: bytes, spans: list[tuple[str, int, int]], problems: list) -> None:
    """PTRHASH's header (spec 18.1) and SLOTS: 12 * P * S bytes, a slot per record with its exact length."""
    if len(ptrhash) < 40:
        problems.append("PTRHASH is shorter than its header")
        return
    hash_id, seed, parts, per_part, buckets = struct.unpack_from("<B7xQQQQ", ptrhash)
    del seed
    if hash_id != 1 or ptrhash[1:8] != bytes(7):
        problems.append("PTRHASH: hash_id is not 1, or a reserved byte is not 0")
    if not (parts and per_part and buckets):
        problems.append("PTRHASH: P, S or B is 0")
        return
    if parts * per_part < len(spans):
        problems.append("PTRHASH: P * S is less than the number of keys")
    if len(ptrhash) != 40 + parts * buckets:
        problems.append(f"PTRHASH: size {len(ptrhash)} is not 40 + P * B = {40 + parts * buckets}")
    if len(slots) != 12 * parts * per_part:
        problems.append(f"SLOTS is {len(slots)} bytes, not 12 * P * S = {12 * parts * per_part}")
        return
    by_start = {start: (key, end - start) for key, start, end in spans}
    used: dict[int, int] = {}
    for i in range(0, len(slots), 12):
        offset, length = struct.unpack_from("<QI", slots, i)
        if length == 0:
            if offset != 0:
                problems.append(f"slot {i // 12}: empty with offset {offset}")
            continue
        found = by_start.get(offset)
        if found is None:
            problems.append(f"slot {i // 12}: offset {offset} is not the start of a record")
        elif length != found[1] and length != 0xFFFFFFFF:
            problems.append(f"slot {i // 12}: length {length}, the record of {found[0]!r} is {found[1]} bytes")
        else:
            used[offset] = used.get(offset, 0) + 1
    if len(used) != len(spans) or any(n != 1 for n in used.values()):
        problems.append(f"{len(used)} records have a slot, {len(spans)} keys: each needs exactly one")


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
    sections: dict[int, list[tuple[int, bytes, int, int]]] = {}
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
        sections.setdefault(typ, []).append((codec, stored, size, off))
    for typ in ONE_PER_STORE:
        want = 1 if typ == SOURCES or key_count >= 1 else 0
        if len(sections.get(typ, [])) != want:
            problems.append(f"section type {typ}: {len(sections.get(typ, []))} of them, expected {want}")
    if len(sections.get(METADATA, [])) > 1:
        problems.append("more than one METADATA section")
    if SOURCES not in sections or (key_count and (RECORDS not in sections or PAGES not in sections)):
        return problems

    codec, stored, size, _ = sections[SOURCES][0]
    raw = _decode(stored, codec, size, problems, "SOURCES")
    if raw is not None:
        _check_sources(raw, desc, problems)
    if key_count:
        records = sections[RECORDS][0][1]
        spans = _check_records(records, desc, key_count, problems, buf)
        codec, stored, size, _ = sections[PAGES][0]
        pages = _decode(stored, codec, size, problems, "PAGES")
        if pages is not None:
            _check_pages(pages, spans, sections[RECORDS][0][3], problems)
        if PTRHASH in sections and SLOTS in sections:
            codec, stored, size, _ = sections[PTRHASH][0]
            ptrhash = _decode(stored, codec, size, problems, "PTRHASH")
            if ptrhash is not None:
                _check_hash(ptrhash, sections[SLOTS][0][1], spans, problems)
    return problems
