"""vzip writer (spec §9
<https://github.com/d-v-b/refkv/blob/main/spec/history/archive/SPEC.r7.md#9-writing>)."""

import struct
import zlib

from . import pb
from . import uri as urimod
from .errors import InvalidInputError
from .reader import ETAG_RE, FORMAT_KEYS, ID_CONCAT, ID_RANGE, MAX64, MAX_PAYLOAD

DOS_TIME = 0
DOS_DATE = (0 << 9) | (1 << 5) | 1  # 1980-01-01
LH_SIG = 0x04034B50
CD_SIG = 0x02014B50
EOCD_SIG = 0x06054B50
Z64_EOCD_SIG = 0x06064B50
Z64_LOC_SIG = 0x07064B50
FLAG_UTF8 = 0x0800
INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1


class WSource:
    def __init__(self, kind, value, size=None, etag=None, modified_not_after=None):
        self.kind = kind  # 'url' | 'key' | 'data'
        self.value = value  # str for url/key, bytes for data
        self.size = size
        self.etag = etag
        self.mnf = modified_not_after


class WRange:
    def __init__(self, source=0, offset=0, length=0, data=None):
        self.source = source
        self.offset = offset
        self.length = length
        self.data = data  # bytes for a literal range

    @property
    def size(self):
        return len(self.data) if self.data is not None else self.length


class WEntry:
    def __init__(self, key, data=None, ranges=None, compress=False, pinned=False):
        self.key = key
        self.data = data
        self.ranges = ranges
        self.compress = compress
        self.pinned = pinned

    @property
    def is_ref(self):
        return self.ranges is not None


def _deflate(data):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def _encode_payload(ranges):
    enc = [pb.encode_range(r.source, r.offset, r.length, r.data) if r.data is None
           else pb.encode_range(data=r.data) for r in ranges]
    if len(ranges) == 1:
        return ID_RANGE, enc[0]
    return ID_CONCAT, pb.encode_concat(enc)


def validate(entries, sources, page_size):
    """Check writer input against spec §9.1
    <https://github.com/d-v-b/refkv/blob/main/spec/history/archive/SPEC.r7.md#91-requirements>.
    Returns {key_bytes: entry}. Raises InvalidInputError."""
    by_key = {}
    for e in entries:
        if not isinstance(e.key, str):
            raise InvalidInputError("key must be a string")
        try:
            kb = e.key.encode("utf-8")
        except UnicodeEncodeError:
            raise InvalidInputError(f"key {e.key!r} is not valid UTF-8") from None
        if not kb:
            raise InvalidInputError("empty key")
        if len(kb) > 65535:
            raise InvalidInputError("key longer than 65535 bytes")
        if kb in FORMAT_KEYS:
            raise InvalidInputError(f"key {e.key!r} is reserved for a format entry")
        if kb in by_key:
            raise InvalidInputError(f"duplicate key {e.key!r}")
        by_key[kb] = e
        if (e.data is None) == (e.ranges is None):
            raise InvalidInputError(f"entry {e.key!r} must have exactly one of bytes and ranges")
        if e.is_ref and e.compress:
            raise InvalidInputError(f"reference entry {e.key!r} cannot be compressed")
        if e.pinned:
            if page_size is None:
                raise InvalidInputError(f"pinned entry {e.key!r} requires a page index")
            if e.is_ref:
                raise InvalidInputError(f"pinned entry {e.key!r} is not a bytes entry")
        if not e.is_ref and len(e.data) >= 0xFFFFFFFF:
            raise InvalidInputError(f"entry {e.key!r} is 4 GiB or larger")
    if page_size is not None and (isinstance(page_size, bool) or not isinstance(page_size, int)
                                  or page_size < 1):
        raise InvalidInputError("page_size must be a positive integer")

    for i, s in enumerate(sources):
        if s.kind == "url":
            if not s.value:
                raise InvalidInputError(f"source {i}: empty url")
            if not urimod.is_uri_reference(s.value):
                raise InvalidInputError(f"source {i}: url {s.value!r} is not an RFC 3986 URI-reference")
            if s.etag is not None and not (
                    isinstance(s.etag, str) and ETAG_RE.fullmatch(s.etag.encode("utf-8", "surrogatepass"))):
                raise InvalidInputError(f"source {i}: etag {s.etag!r} is not a strong entity tag")
            if s.size is not None and not 0 <= s.size <= MAX64:
                raise InvalidInputError(f"source {i}: size pin out of range")
            if s.mnf is not None and not INT64_MIN <= s.mnf <= INT64_MAX:
                raise InvalidInputError(f"source {i}: modified_not_after out of range")
        else:
            if s.size is not None or s.etag is not None or s.mnf is not None:
                raise InvalidInputError(f"source {i}: pins are only allowed on url sources")
            if s.kind == "key":
                try:
                    kb = s.value.encode("utf-8")
                except UnicodeEncodeError:
                    raise InvalidInputError(f"source {i}: key is not valid UTF-8") from None
                if not kb:
                    raise InvalidInputError(f"source {i}: empty key")
                if kb in FORMAT_KEYS:
                    raise InvalidInputError(f"source {i}: key names a format entry")
                tgt = by_key.get(kb)
                if tgt is None:
                    raise InvalidInputError(f"source {i}: key {s.value!r} is absent")
                if tgt.is_ref:
                    raise InvalidInputError(f"source {i}: key {s.value!r} is a reference entry")
            elif s.kind != "data":
                raise InvalidInputError(f"source {i}: unknown kind")

    for e in entries:
        if not e.is_ref:
            continue
        total = 0
        for r in e.ranges:
            if r.data is None:
                if not 0 <= r.source < len(sources):
                    raise InvalidInputError(f"entry {e.key!r}: range source {r.source} out of bounds")
                if r.offset < 0 or r.length < 0:
                    raise InvalidInputError(f"entry {e.key!r}: negative offset/length")
                end = r.offset + r.length
                if end > MAX64:
                    raise InvalidInputError(f"entry {e.key!r}: range offset + length exceeds 2^64-1")
                s = sources[r.source]
                if s.kind == "data":
                    n = len(s.value)
                elif s.kind == "key":
                    n = len(by_key[s.value.encode("utf-8")].data)
                else:
                    n = None
                if n is not None and end > n:
                    raise InvalidInputError(f"entry {e.key!r}: range extends past the end of source {r.source}")
            total += r.size
        if total > MAX64:
            raise InvalidInputError(f"entry {e.key!r}: reference size exceeds 2^64-1")
        _id, payload = _encode_payload(e.ranges)
        if len(payload) > MAX_PAYLOAD:
            raise InvalidInputError(f"entry {e.key!r}: reference payload is {len(payload)} bytes (max {MAX_PAYLOAD})")
    return by_key


def _local_header(name, method, crc, csize, usize):
    return struct.pack("<IHHHHHIIIHH", LH_SIG, 20, FLAG_UTF8, method, DOS_TIME, DOS_DATE,
                       crc, csize, usize, len(name), 0) + name


def _cd_record(name, method, crc, csize, usize, offset, ref=None):
    extra = b""
    ver = 20
    off32 = offset
    if offset >= 0xFFFFFFFF:
        extra += struct.pack("<HHQ", 0x0001, 8, offset)
        off32 = 0xFFFFFFFF
        ver = 45
    if ref is not None:
        rid, payload = ref
        extra += struct.pack("<HH", rid, len(payload)) + payload
    if len(extra) > 0xFFFF:
        raise InvalidInputError("extra field too large")
    return struct.pack("<IHHHHHHIIIHHHHHII", CD_SIG, 20, ver, FLAG_UTF8, method, DOS_TIME, DOS_DATE,
                       crc, csize, usize, len(name), len(extra), 0, 0, 0, 0, off32) + name + extra


def build(entries, sources, page_size=None, mirror=True):
    """Build a vzip archive and return it as bytes. Raises InvalidInputError."""
    validate(entries, sources, page_size)
    out = bytearray()
    cd = []  # (name, method, crc, csize, usize, offset, ref)
    pinned_info = []  # (name, body_offset, size, csize, method)

    def add(name, method, body, usize, crc, ref=None):
        if len(body) >= 0xFFFFFFFF or usize >= 0xFFFFFFFF:
            raise InvalidInputError(f"entry {name!r} is 4 GiB or larger")
        off = len(out)
        out.extend(_local_header(name, method, crc, len(body), usize))
        out.extend(body)
        cd.append((name, method, crc, len(body), usize, off, ref))
        return off + 30 + len(name)

    def add_entry(e):
        name = e.key.encode("utf-8")
        if e.is_ref:
            ref = _encode_payload(e.ranges)
            body = ref[1] if mirror else b""
            add(name, 0, body, len(body), zlib.crc32(body), ref)
        else:
            data = bytes(e.data)
            if e.compress:
                body, method = _deflate(data), 8
            else:
                body, method = data, 0
            boff = add(name, method, body, len(data), zlib.crc32(data))
            if e.pinned:
                pinned_info.append((name, boff, len(data), len(body), method))

    for e in entries:
        if not e.pinned:
            add_entry(e)

    st = pb.encode_source_table([
        pb.encode_source(s.kind, s.value, s.size, s.etag, s.mnf) for s in sources])
    st_body = _deflate(st)
    sources_off = add(b"__vz__/sources", 8, st_body, len(st), zlib.crc32(st))
    sources_size = len(st_body)

    for e in entries:
        if e.pinned:
            add_entry(e)

    comment_extra = b""
    if page_size is not None:
        body_recs = [r for r in cd if r[0] not in FORMAT_KEYS]
        body_recs.sort(key=lambda r: r[0])
        encoded = [(r[0], _cd_record(*r)) for r in body_recs]
        pages = []
        cur_first, cur_off, cur_len = None, 0, 0
        pos = 0
        for name, rec in encoded:
            if cur_first is not None and cur_len + len(rec) > page_size:
                pages.append(pb.encode_page(cur_first, cur_off, cur_len))
                cur_first = None
            if cur_first is None:
                cur_first, cur_off, cur_len = name, pos, 0
            cur_len += len(rec)
            pos += len(rec)
        if cur_first is not None:
            pages.append(pb.encode_page(cur_first, cur_off, cur_len))
        pinned_info.sort(key=lambda p: p[0])
        idx = pb.encode_cd_index(pages, [pb.encode_pinned(*p) for p in pinned_info])
        idx_body = _deflate(idx)
        index_off = add(b"__vz__/index", 8, idx_body, len(idx), zlib.crc32(idx))
        comment_extra = struct.pack("<QQ", index_off, len(idx_body))
        fmt_recs = [r for r in cd if r[0] in FORMAT_KEYS]
        cd_bytes = b"".join(rec for _n, rec in encoded) + b"".join(_cd_record(*r) for r in fmt_recs)
    else:
        cd_bytes = b"".join(_cd_record(*r) for r in cd)

    cd_off = len(out)
    out.extend(cd_bytes)
    cd_size = len(cd_bytes)
    n = len(cd)
    comment = b"vzip/0" + struct.pack("<QQ", sources_off, sources_size) + comment_extra
    if n >= 0xFFFF or cd_size >= 0xFFFFFFFF or cd_off >= 0xFFFFFFFF:
        z64off = len(out)
        out.extend(struct.pack("<IQHHIIQQQQ", Z64_EOCD_SIG, 44, 45, 45, 0, 0, n, n, cd_size, cd_off))
        out.extend(struct.pack("<IIQI", Z64_LOC_SIG, 0, z64off, 1))
    out.extend(struct.pack("<IHHHHIIH", EOCD_SIG, 0, 0,
                           min(n, 0xFFFF), min(n, 0xFFFF),
                           min(cd_size, 0xFFFFFFFF), min(cd_off, 0xFFFFFFFF), len(comment)))
    out.extend(comment)
    return bytes(out)
