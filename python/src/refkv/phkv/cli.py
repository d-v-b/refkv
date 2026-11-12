"""Conformance-harness CLI (conformance/phkv/HARNESS.md) for the reference implementation.

    python -m refkv.phkv.cli [--allow-private-hosts] [--allow-scheme <scheme>]... read <store> <queries.json>
    python -m refkv.phkv.cli [--section-codec none|deflate|zstd] write <description.json> <out>

`read` uses the reader's default policy (spec §6.7
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>). --allow-private-hosts
is UNSAFE: it lets the store's sources reach loopback, private, link-local and other special
addresses. The harness needs it, since the runner serves its HTTP sources on 127.0.0.1.
--allow-scheme names a scheme the policy otherwise refuses (`file`, for the harness's `files_*` stores).
`--section-codec` is how `write` stores the store's own sections (the writer's default, zstd, if not
given), for a run against a reader that decodes fewer codecs.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping

from refkv._files import replacing
from refkv._narrow import mapping, sequence, string
from refkv._sync import run
from refkv.errors import ArchiveError
from refkv.phkv.store import PhkvStore
from refkv.phkv.writer import CodecName, PhkvWriter
from refkv.policy import Policy
from refkv.vzip.cli import (
    _check_query,
    _hex,
    _int,
    _no_duplicates,
    _range,
    _reject_nulls,
    _request,
    _source,
)


def _answer(store: PhkvStore, q: Mapping[str, object]) -> dict[str, object]:
    if q["op"] == "classify":
        return {"ok": True, "kind": store.classify(string(q["key"], "key"))}
    if q["op"] in ("get", "get_raw"):
        key = string(q["key"], "key")
        value = store.get(key, _request(q.get("range"))) if q["op"] == "get" else store.raw(key)  # type: ignore[arg-type]
        return {"ok": True, "value": None if value is None else value.hex()}
    if q["op"] == "list":
        return {"ok": True, "keys": store.list(string(q["prefix"], "prefix"))}
    raise ValueError(f"unknown op {q['op']!r}")


def _run_query(store: PhkvStore, q: Mapping[str, object]) -> dict[str, object]:
    try:
        return _answer(store, q)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "class": getattr(e, "cls", "error"), "error": f"{type(e).__name__}: {e}"}


def read(path: str, queries_path: str, policy: Policy | None = None) -> dict[str, object]:
    with open(queries_path, encoding="utf-8") as f:
        queries = json.load(f, object_pairs_hook=_no_duplicates)
    if not isinstance(queries, list):
        raise ValueError("the queries file must hold a JSON array")
    for q in queries:
        _check_query(q)
    store = PhkvStore(path, policy=policy)
    try:
        run(store._ensure_open())  # noqa: SLF001 (open now, to report a bad store apart from the queries)
    except ArchiveError as e:  # anything else is a crash, which the run reports as one
        return {"open": {"ok": False, "class": "archive", "error": f"{type(e).__name__}: {e}"}, "results": []}
    return {"open": {"ok": True}, "results": [_run_query(store, q) for q in queries]}


def write(desc_path: str, out: str, section_codec: CodecName = "zstd") -> None:
    with open(desc_path, encoding="utf-8") as f:
        desc = json.load(f, object_pairs_hook=_no_duplicates)
    _reject_nulls(desc)
    page_size = _int(desc["page_size"], "page_size") if "page_size" in desc else 4096
    if page_size < 1:
        raise ValueError(f"page_size must be an integer >= 1, not {page_size!r}")
    inline_max = _int(desc["inline_max"], "inline_max") if "inline_max" in desc else 4096
    with replacing(out) as f, PhkvWriter(
            f, section_codec=section_codec, page_size=page_size, inline_max=inline_max) as w:
        for src in desc.get("sources", []):
            w.source(_source(src))
        for e in desc["entries"]:
            _add_entry(w, e)


def _add_entry(w: PhkvWriter, e: Mapping[str, object]) -> None:
    key = string(e["key"], "key")
    if ("bytes" in e) == ("ranges" in e):
        raise ValueError(f"{key!r}: an entry has exactly one of bytes, ranges")
    compress = e.get("compress", False)
    if not isinstance(compress, bool):
        raise ValueError("compress must be a JSON boolean")
    if "ranges" in e:
        if compress:
            raise ValueError(f"{key!r}: reference entries are never compressed")
        w.add_ranges(key, [_range(mapping(r, "range")) for r in sequence(e["ranges"], "ranges")])
        return
    w.add_bytes(key, _hex(e["bytes"]), codec="deflate" if compress else "none")


def main(argv: list[str]) -> int:
    private = False
    schemes: set[str] = set()
    section_codec: CodecName = "zstd"
    while argv[:1] and argv[0].startswith("--"):
        if argv[0] == "--allow-private-hosts":
            private, argv = True, argv[1:]  # unsafe: asked for by name
        elif argv[0] == "--allow-scheme" and len(argv) > 1:
            schemes.add(argv[1])
            argv = argv[2:]
        elif argv[0] == "--section-codec" and len(argv) > 1 and argv[1] in ("none", "deflate", "zstd"):
            section_codec, argv = argv[1], argv[2:]  # type: ignore[assignment]
        else:
            print(__doc__, file=sys.stderr)
            return 2
    policy = Policy(allow_private_hosts=private, schemes=frozenset(schemes))
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
            write(argv[1], argv[2], section_codec)
        except Exception as e:  # noqa: BLE001
            print(f"{type(e).__name__}: {e}", file=sys.stderr)
            return 1
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
