"""Conformance harness CLI (HARNESS.md): `read` and `write` commands."""

import contextlib
import json
import os
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "vzip_impl"

from vzip_impl import reader, writer
from vzip_impl.errors import InvalidInputError, VzError


class BadJSONError(Exception):
    pass


def _no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise BadJSONError(f"duplicate member {k!r}")
        d[k] = v
    return d


def _bad_constant(name):
    raise BadJSONError(f"invalid JSON constant {name}")


def load_json(path):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BadJSONError("not UTF-8") from None
    try:
        return json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_bad_constant)
    except json.JSONDecodeError as e:
        raise BadJSONError(str(e)) from e


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# ------------------------------------------------------------------ read

def parse_query(q):
    if not isinstance(q, dict):
        raise BadJSONError("query is not an object")
    op = q.get("op")
    if op not in ("classify", "get", "get_raw", "list"):
        raise BadJSONError(f"unknown op {op!r}")
    if op == "list":
        if "prefix" not in q or not isinstance(q["prefix"], str):
            raise BadJSONError("list without a string prefix")
    elif "key" not in q or not isinstance(q["key"], str):
        raise BadJSONError(f"{op} without a string key")
    if "range" in q and op != "get":
        raise BadJSONError(f"range on {op}")
    req = ("whole",)
    if op == "get" and "range" in q:
        r = q["range"]
        if not isinstance(r, dict):
            raise BadJSONError("range is not an object")
        forms = [f for f in (("start", "end"), ("offset",), ("suffix",)) if any(m in r for m in f)]
        if len(forms) != 1 or not all(m in r for m in forms[0]):
            raise BadJSONError("range must have exactly one complete form")
        vals = [r[m] for m in forms[0]]
        for v in vals:
            if not _is_int(v) or v < 0:
                raise BadJSONError("range numbers must be non-negative integers")
        name = {"start": "range", "offset": "offset", "suffix": "suffix"}[forms[0][0]]
        req = (name, *vals)
    return op, q.get("key", q.get("prefix")), req


def run_query(ar, op, key, req):
    try:
        if op == "classify":
            return {"ok": True, "kind": ar.classify(key)}
        if op == "get":
            v = ar.get(key, req)
            return {"ok": True, "value": None if v is None else v.hex()}
        if op == "get_raw":
            v = ar.raw(key)
            return {"ok": True, "value": None if v is None else bytes(v).hex()}
        return {"ok": True, "keys": ar.list(key)}
    except VzError as e:
        return {"ok": False, "class": e.cls, "error": str(e)}
    except MemoryError:
        return {"ok": False, "class": "request", "error": "out of memory"}
    except Exception as e:  # noqa: BLE001 - a bug: report it rather than lose the other results
        return {"ok": False, "class": "internal", "error": f"{type(e).__name__}: {e}"}


def _load_queries(path):
    qs = load_json(path)
    if not isinstance(qs, list):
        raise BadJSONError("queries file is not an array")
    return [parse_query(q) for q in qs]


def cmd_read(archive_path, queries_path):
    try:
        parsed = _load_queries(queries_path)
    except (BadJSONError, OSError) as e:
        print(f"invalid queries file: {e}", file=sys.stderr)
        return 2
    try:
        ar = reader.Archive(archive_path)
    except VzError as e:
        out = {"open": {"ok": False, "class": "archive", "error": str(e)}, "results": []}
        sys.stdout.write(json.dumps(out) + "\n")
        return 0
    with ar:
        results = [run_query(ar, *p) for p in parsed]
    sys.stdout.write(json.dumps({"open": {"ok": True}, "results": results}) + "\n")
    return 0


# ------------------------------------------------------------------ write

_HEX_RE = re.compile(r"(?:[0-9a-f]{2})*")


def _hex(v, what):
    if not isinstance(v, str) or not _HEX_RE.fullmatch(v):
        raise InvalidInputError(f"{what} must be a lowercase even-length hex string")
    return bytes.fromhex(v)


def _get(obj, name, default, check, what):
    if name not in obj:
        return default
    v = obj[name]
    if v is None:
        raise InvalidInputError(f"{what} must not be null")
    return check(v, what)


def _bool(v, what):
    if not isinstance(v, bool):
        raise InvalidInputError(f"{what} must be a boolean")
    return v


def _uint(v, what):
    if not _is_int(v) or v < 0:
        raise InvalidInputError(f"{what} must be a non-negative integer")
    return v


def _int(v, what):
    if not _is_int(v):
        raise InvalidInputError(f"{what} must be an integer")
    return v


def _str(v, what):
    if not isinstance(v, str):
        raise InvalidInputError(f"{what} must be a string")
    return v


def _list(v, what):
    if not isinstance(v, list):
        raise InvalidInputError(f"{what} must be an array")
    return v


def parse_description(d):
    if not isinstance(d, dict):
        raise InvalidInputError("description must be an object")
    page_size = d.get("page_size")
    if page_size is not None and (not _is_int(page_size) or page_size < 1):
        raise InvalidInputError("page_size must be null or an integer >= 1")
    mirror = _get(d, "mirror", True, _bool, "mirror")
    srcs = _get(d, "sources", [], _list, "sources")
    ents = _get(d, "entries", [], _list, "entries")

    sources = []
    for i, s in enumerate(srcs):
        w = f"sources[{i}]"
        if not isinstance(s, dict):
            raise InvalidInputError(f"{w} must be an object")
        kinds = [k for k in ("url", "key", "data") if k in s]
        if len(kinds) != 1:
            raise InvalidInputError(f"{w} must have exactly one of url, key, data")
        kind = kinds[0]
        if s[kind] is None:
            raise InvalidInputError(f"{w}.{kind} must not be null")
        value = _hex(s[kind], w + ".data") if kind == "data" else _str(s[kind], f"{w}.{kind}")
        size = _get(s, "size", None, _uint, w + ".size")
        etag = _get(s, "etag", None, _str, w + ".etag")
        mnf = _get(s, "modified_not_after", None, _int, w + ".modified_not_after")
        if kind != "url" and (size is not None or etag is not None or mnf is not None):
            raise InvalidInputError(f"{w}: pins are only allowed on url sources")
        sources.append(writer.WSource(kind, value, size, etag, mnf))

    entries = []
    for i, e in enumerate(ents):
        w = f"entries[{i}]"
        if not isinstance(e, dict):
            raise InvalidInputError(f"{w} must be an object")
        if "key" not in e:
            raise InvalidInputError(f"{w} has no key")
        key = _get(e, "key", None, _str, w + ".key")
        has_b, has_r = "bytes" in e, "ranges" in e
        if has_b == has_r:
            raise InvalidInputError(f"{w} must have exactly one of bytes and ranges")
        compress = _get(e, "compress", False, _bool, w + ".compress")
        pinned = _get(e, "pinned", False, _bool, w + ".pinned")
        if has_b:
            data = _get(e, "bytes", None, _hex, w + ".bytes")
            entries.append(writer.WEntry(key, data=data, compress=compress, pinned=pinned))
        else:
            rs = _get(e, "ranges", None, _list, w + ".ranges")
            ranges = []
            for j, r in enumerate(rs):
                wr = f"{w}.ranges[{j}]"
                if not isinstance(r, dict):
                    raise InvalidInputError(f"{wr} must be an object")
                if "data" in r:
                    if any(m in r for m in ("source", "offset", "length")):
                        raise InvalidInputError(f"{wr} mixes a literal and a source range")
                    ranges.append(writer.WRange(data=_get(r, "data", None, _hex, wr + ".data")))
                else:
                    ranges.append(writer.WRange(
                        source=_get(r, "source", 0, _uint, wr + ".source"),
                        offset=_get(r, "offset", 0, _uint, wr + ".offset"),
                        length=_get(r, "length", 0, _uint, wr + ".length")))
            entries.append(writer.WEntry(key, ranges=ranges, compress=compress, pinned=pinned))
    return entries, sources, page_size, mirror


def cmd_write(desc_path, out_path):
    try:
        d = load_json(desc_path)
        entries, sources, page_size, mirror = parse_description(d)
        data = writer.build(entries, sources, page_size, mirror)
    except (BadJSONError, InvalidInputError, OSError) as e:
        print(f"invalid description: {e}", file=sys.stderr)
        return 1
    tmp = Path(out_path + f".tmp-vzip-{os.getpid()}")
    try:
        with open(tmp, "xb") as f:
            f.write(data)
        tmp.replace(out_path)
    except OSError as e:
        with contextlib.suppress(OSError):
            tmp.unlink()
        print(f"cannot write {out_path}: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv):
    if len(argv) == 3 and argv[0] == "read":
        return cmd_read(argv[1], argv[2])
    if len(argv) == 3 and argv[0] == "write":
        return cmd_write(argv[1], argv[2])
    print("usage: vzip read <archive> <queries.json> | vzip write <description.json> <out.vzip>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
