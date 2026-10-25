"""Hand-written protobuf wire-format encoding/decoding for the vzip schema (spec §5
<https://github.com/d-v-b/refkv/blob/main/spec/history/archive/SPEC.r7.md#5-messages>, Appendix
A)."""

from ._narrow import integer

MAX64 = (1 << 64) - 1
MAX32 = (1 << 32) - 1
MAX_FIELD = (1 << 29) - 1


class MalformedError(Exception):
    pass


# ---------------------------------------------------------------- decoding

def _varint(buf, pos, end):
    result = 0
    shift = 0
    for _ in range(10):
        if pos >= end:
            raise MalformedError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            if result > MAX64:
                raise MalformedError("varint exceeds 2^64-1")
            return result, pos
        shift += 7
    raise MalformedError("varint longer than 10 bytes")


def parse_fields(buf):
    """Return a list of (field_number, wire_type, value).

    value is an int for VARINT/I64/I32 and bytes for LEN.
    """
    pos = 0
    end = len(buf)
    out = []
    while pos < end:
        tag, pos = _varint(buf, pos, end)
        wt = tag & 7
        fn = tag >> 3
        if wt in (3, 4, 6, 7):
            raise MalformedError(f"invalid wire type {wt}")
        if fn == 0 or fn > MAX_FIELD:
            raise MalformedError(f"invalid field number {fn}")
        if wt == 0:
            v, pos = _varint(buf, pos, end)
        elif wt == 1:
            if end - pos < 8:
                raise MalformedError("truncated I64")
            v = int.from_bytes(buf[pos:pos + 8], "little")
            pos += 8
        elif wt == 5:
            if end - pos < 4:
                raise MalformedError("truncated I32")
            v = int.from_bytes(buf[pos:pos + 4], "little")
            pos += 4
        else:  # wt == 2
            ln, pos = _varint(buf, pos, end)
            if ln > end - pos:
                raise MalformedError("LEN field extends past end of message")
            v = bytes(buf[pos:pos + ln])
            pos += ln
        out.append((fn, wt, v))
    return out


def _bytes(v: object) -> bytes:
    if not isinstance(v, bytes):
        raise TypeError(f"expected bytes, got {type(v).__name__}")
    return v


# A schema maps each field number to (name, type, label). The type is one of 'uint32',
# 'uint64', 'int64', 'string' and 'bytes', or a dict (a message schema); the label is one
# of 'single', 'optional', 'repeated' and 'oneof:<group>'.

def decode(buf, schema) -> dict[str, object]:
    """The message in `buf`: each value as its schema types it (narrowed where it is read)."""
    result: dict[str, object] = {}
    repeated: dict[str, list[object]] = {}
    for fn, wt, raw in parse_fields(buf):
        spec = schema.get(fn)
        if spec is None:
            continue  # unknown (or reserved) field: skipped
        name, typ, label = spec
        is_len = isinstance(typ, dict) or typ in ("string", "bytes")
        if wt != (2 if is_len else 0):
            raise MalformedError(f"field {fn} has wrong wire type {wt}")
        # the wire type (checked above) gives raw's type: int for VARINT, bytes for LEN
        v: object = raw
        if typ == "uint32":
            if integer(raw) > MAX32:
                raise MalformedError(f"uint32 field {fn} overflows")
        elif typ == "int64":
            if integer(raw) >= 1 << 63:
                v = integer(raw) - (1 << 64)
        elif typ == "string":
            try:
                v = _bytes(raw).decode("utf-8", "strict")
            except UnicodeDecodeError:
                raise MalformedError(f"string field {fn} is not valid UTF-8") from None
        elif isinstance(typ, dict):
            v = decode(_bytes(raw), typ)
        if label == "repeated":
            if name not in repeated:
                repeated[name] = result[name] = []
            repeated[name].append(v)
        elif label.startswith("oneof:"):
            result[label[6:]] = (name, v)
        else:
            result[name] = v
    return result


RANGE = {
    1: ("source", "uint32", "single"),
    3: ("offset", "uint64", "single"),
    4: ("length", "uint64", "single"),
    5: ("data", "bytes", "optional"),
}
CONCAT = {1: ("parts", RANGE, "repeated")}
SOURCE = {
    1: ("url", "string", "oneof:kind"),
    2: ("key", "string", "oneof:kind"),
    3: ("data", "bytes", "oneof:kind"),
    4: ("size", "uint64", "optional"),
    5: ("etag", "string", "optional"),
    6: ("modified_not_after", "int64", "optional"),
}
SOURCE_TABLE = {1: ("sources", SOURCE, "repeated")}
PAGE = {
    1: ("first_key", "string", "single"),
    2: ("offset", "uint64", "single"),
    3: ("length", "uint64", "single"),
}
PINNED = {
    1: ("key", "string", "single"),
    2: ("data_offset", "uint64", "single"),
    3: ("size", "uint64", "single"),
    4: ("csize", "uint64", "single"),
    5: ("method", "uint32", "single"),
}
CD_INDEX = {1: ("pages", PAGE, "repeated"), 2: ("pinned", PINNED, "repeated")}


# ---------------------------------------------------------------- encoding

def enc_varint(n):
    if n < 0:
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _f_varint(fn, v):
    return enc_varint(fn << 3) + enc_varint(v)


def _f_len(fn, b):
    return enc_varint((fn << 3) | 2) + enc_varint(len(b)) + b


def encode_range(source=0, offset=0, length=0, data=None):
    out = b""
    if source:
        out += _f_varint(1, source)
    if offset:
        out += _f_varint(3, offset)
    if length:
        out += _f_varint(4, length)
    if data is not None:
        out += _f_len(5, bytes(data))
    return out


def encode_concat(encoded_parts):
    return b"".join(_f_len(1, p) for p in encoded_parts)


def encode_source(kind, value, size=None, etag=None, mnf=None):
    fn = {"url": 1, "key": 2, "data": 3}[kind]
    v = value.encode("utf-8") if kind != "data" else bytes(value)
    out = _f_len(fn, v)
    if size is not None:
        out += _f_varint(4, size)
    if etag is not None:
        out += _f_len(5, etag.encode("utf-8"))
    if mnf is not None:
        out += _f_varint(6, mnf)
    return out


def encode_source_table(encoded_sources):
    return b"".join(_f_len(1, s) for s in encoded_sources)


def encode_page(first_key, offset, length):
    out = b""
    if first_key:
        out += _f_len(1, first_key)
    if offset:
        out += _f_varint(2, offset)
    if length:
        out += _f_varint(3, length)
    return out


def encode_pinned(key, data_offset, size, csize, method):
    out = b""
    if key:
        out += _f_len(1, key)
    if data_offset:
        out += _f_varint(2, data_offset)
    if size:
        out += _f_varint(3, size)
    if csize:
        out += _f_varint(4, csize)
    if method:
        out += _f_varint(5, method)
    return out


def encode_cd_index(pages, pinned):
    return b"".join(_f_len(1, p) for p in pages) + b"".join(_f_len(2, p) for p in pinned)
