"""Conformance-harness CLI (conformance/vzip/HARNESS.md) for the reference implementation.

    python -m refkv.vzip.cli [--allow-private-hosts] read <archive> <queries.json>
    python -m refkv.vzip.cli write <description.json> <out>

`read` uses the reader's default policy (spec §6.7
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>). --allow-private-hosts
is UNSAFE: it lets the archive's sources reach loopback, private, link-local and
other special addresses. The harness needs it, since the runner serves its
HTTP sources on 127.0.0.1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from refkv._sync import run
from refkv.pb import Range, Source
from refkv.policy import Policy
from refkv.reader import OffsetRequest, RangeRequest, Request, SuffixRequest
from refkv.vzip.archive import RESERVED_PREFIX, VZipWriter
from refkv.vzip.store import VZipStore


def _request(r: dict | None) -> Request | None:
    if r is None:
        return None
    if "suffix" in r:
        return SuffixRequest(r["suffix"])
    if "offset" in r:
        return OffsetRequest(r["offset"])
    return RangeRequest(r["start"], r["end"])


def _answer(store: VZipStore, q: dict) -> dict:
    if q["op"] == "classify":
        return {"ok": True, "kind": store.classify(q["key"])}
    if q["op"] in ("get", "get_raw"):
        value = store.get(q["key"], _request(q.get("range"))) if q["op"] == "get" else store.raw(q["key"])
        return {"ok": True, "value": None if value is None else value.hex()}
    if q["op"] == "list":
        return {"ok": True, "keys": store.list(q["prefix"])}
    raise ValueError(f"unknown op {q['op']!r}")


def _run_query(store: VZipStore, q: dict) -> dict:
    try:
        return _answer(store, q)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "class": getattr(e, "cls", "error"),
                "error": f"{type(e).__name__}: {e}"}


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate JSON member names: {keys}")
    return dict(pairs)


def _well_formed(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate from a JSON "\udXXX" escape
        return False
    return True


def _check_query(q: object) -> None:
    """HARNESS: a malformed query makes the whole queries file invalid."""
    if not isinstance(q, dict) or q.get("op") not in ("classify", "get", "get_raw", "list"):
        raise ValueError(f"malformed query {q!r}")
    if q["op"] == "list":
        if not isinstance(q.get("prefix"), str) or not _well_formed(q["prefix"]):
            raise ValueError(f"malformed query {q!r}")
        return
    if not isinstance(q.get("key"), str) or not _well_formed(q["key"]):
        raise ValueError(f"malformed query {q!r}")
    if "range" in q:
        r = q["range"]
        if q["op"] != "get":
            raise ValueError(f"range on a {q['op']} query: {q!r}")
        forms = set(r) & {"start", "end", "offset", "suffix"} if isinstance(r, dict) else None
        if forms not in ({"start", "end"}, {"offset"}, {"suffix"}):
            raise ValueError(f"malformed range in {q!r}")
        r = {k: v for k, v in r.items() if k in ("start", "end", "offset", "suffix")}
        if any(not isinstance(v, int) or isinstance(v, bool) or not 0 <= v < 2**53
               for v in r.values()):
            raise ValueError(f"range values must be non-negative integers: {q!r}")


def read(archive: str, queries_path: str, policy: Policy | None = None) -> dict:
    with open(queries_path, encoding="utf-8") as f:
        queries = json.load(f, object_pairs_hook=_no_duplicates)
    if not isinstance(queries, list):
        raise ValueError("the queries file must hold a JSON array")
    for q in queries:
        _check_query(q)
    store = VZipStore(archive, policy=policy)
    try:
        run(store._ensure_open())  # noqa: SLF001 (open now, to report a bad archive apart from the queries)
    except Exception as e:  # noqa: BLE001
        return {"open": {"ok": False, "class": "archive", "error": f"{type(e).__name__}: {e}"},
                "results": []}
    return {"open": {"ok": True}, "results": [_run_query(store, q) for q in queries]}


_PINS = ("size", "etag", "modified_not_after")


def _hex(v: object) -> bytes:
    if not isinstance(v, str) or v != v.lower() or len(v) % 2:
        raise ValueError(f"not lowercase even-length hex: {v!r}")
    return bytes.fromhex(v)


def _int(v: object, what: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise ValueError(f"{what} must be a non-negative JSON integer, not {v!r}")
    return v


def _source(d: dict) -> Source:
    kinds = [k for k in ("url", "key", "data") if k in d]
    if len(kinds) != 1:
        raise ValueError(f"a source has exactly one of url, key, data: {d}")
    pins = {k: d[k] for k in _PINS if k in d}
    for k in ("size", "modified_not_after"):
        if k in pins:
            _int(pins[k], k)
    k = kinds[0]
    if k == "url":
        return Source(url=d["url"], **pins)
    if pins:
        raise ValueError("pins are only allowed on url sources")
    if k == "key":
        return Source(key=d["key"])
    return Source(data=_hex(d["data"]))


def _range(r: dict) -> Range:
    if "data" in r:
        if set(r) & {"source", "offset", "length"}:
            raise ValueError(f"range mixes data with source fields: {r}")
        return Range(data=_hex(r["data"]))
    return Range(source=_int(r.get("source", 0), "source"), offset=_int(r.get("offset", 0), "offset"),
                 length=_int(r.get("length", 0), "length"))


def _reject_nulls(desc: dict) -> None:
    """HARNESS: null is allowed only for page_size."""
    def walk(v: object, path: str) -> None:
        if v is None and path != "page_size":
            raise ValueError(f"null not allowed at {path}")
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k if path == "" else f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")
    walk(desc, "")


def write(desc_path: str, out: str) -> None:
    with open(desc_path, encoding="utf-8") as f:
        desc = json.load(f, object_pairs_hook=_no_duplicates)
    _reject_nulls(desc)
    page_size = desc.get("page_size")
    if not isinstance(desc.get("mirror", True), bool):
        raise ValueError("mirror must be a JSON boolean")
    if page_size is not None and (not isinstance(page_size, int) or isinstance(page_size, bool)
                                  or page_size < 1):
        raise ValueError(f"page_size must be null or an integer >= 1, not {page_size!r}")
    try:
        with open(out, "wb") as f:
            w = VZipWriter(f, mirror_refs=desc.get("mirror", True), page_size=page_size)
            for src in desc.get("sources", []):
                w.source(_source(src))
            for e in desc["entries"]:
                _add_entry(w, e, page_size)
            w.close()
    except Exception:
        Path(out).unlink(missing_ok=True)
        raise


def _add_entry(w: VZipWriter, e: dict, page_size: int | None) -> None:
    key = e["key"]
    if ("bytes" in e) == ("ranges" in e):
        raise ValueError(f"{key!r}: an entry has exactly one of bytes, ranges")
    if "ranges" in e:
        if e.get("pinned"):
            raise ValueError(f"{key!r}: only bytes entries can be pinned")
        if e.get("compress"):
            raise ValueError(f"{key!r}: reference entries are never compressed")
        w.add_ranges(key, [_range(r) for r in e["ranges"]])
        return
    data = _hex(e["bytes"])
    compress = e.get("compress", False)
    for flag in ("compress", "pinned"):
        if not isinstance(e.get(flag, False), bool):
            raise ValueError(f"{flag} must be a JSON boolean")
    if e.get("pinned") and page_size is None:
        raise ValueError(f"{key!r}: pinned requires page_size")
    if key.startswith(RESERVED_PREFIX):
        w.add_hidden(key[len(RESERVED_PREFIX):], data, compress=compress, late=e.get("pinned", False))
    else:
        w.add_bytes(key, data, compress=compress, late=e.get("pinned", False))


def main(argv: list[str]) -> int:
    policy = None
    if argv[:1] == ["--allow-private-hosts"]:
        policy, argv = Policy(allow_private_hosts=True), argv[1:]  # unsafe: asked for by name
    if len(argv) == 3 and argv[0] == "read":
        try:
            result = read(argv[1], argv[2], policy)
        except (OSError, ValueError) as e:
            print(f"invalid queries file: {e}", file=sys.stderr)
            return 2
        print(json.dumps(result))
        return 0
    if len(argv) == 3 and argv[0] == "write":
        try:
            write(argv[1], argv[2])
        except Exception as e:  # noqa: BLE001
            print(f"{type(e).__name__}: {e}", file=sys.stderr)
            return 1
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
