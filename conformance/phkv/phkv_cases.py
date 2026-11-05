"""Conformance cases for phkv: the vzip suite's descriptions, adapted, and cases of phkv's own.

A description is the one of conformance/vzip/HARNESS.md less `mirror` and `pinned`, which are
vzip's, and with `inline_max`: a phkv store has no reserved names, so a key such as `__vz__/hdr`
is an ordinary key, and `page_size` has the meaning of spec/bindings/phkv.md §19.4.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vzip"))

import cases as vzip_cases
from model import standard_queries

H = vzip_cases.H
write_data_files = vzip_cases.write_data_files


def adapt(desc: dict) -> dict:
    """The phkv description of a vzip one: `mirror` and `pinned` dropped, a null `page_size` too."""
    out = {k: v for k, v in desc.items() if k != "mirror" and not (k == "page_size" and v is None)}
    out["entries"] = [{k: v for k, v in e.items() if k != "pinned"} for e in desc.get("entries", [])]
    return out


def _self_contained() -> dict[str, dict]:
    """Descriptions that read nothing but the store: their sources are `key` and `data` sources, and
    an HTTP source the runner serves. Every implementation can read them, whatever it resolves."""
    sources = [{"key": "__vz__/hdr"}, {"data": H(b"SHARED")}, {"key": "meta/zarr.json"}]
    entries = [
        {"key": "meta/zarr.json", "bytes": H(b'{"zarr_format":3}')},
        {"key": "meta/big", "bytes": H(bytes(range(200)) * 3), "compress": True},
        {"key": "meta/compressible", "bytes": H(b"a" * 5000), "compress": True},
        {"key": "empty", "bytes": ""},
        {"key": "empty_compressed", "bytes": "", "compress": True},
        {"key": "unicode/ключ/é", "bytes": H("é")},
        # phkv reserves no names: these are ordinary keys, listed like any other
        {"key": "__vz__/hdr", "bytes": H(b"HDR!"), "compress": True},
        {"key": "__vz__/raw", "bytes": H(b"raw")},
        {"key": "r/hdr", "ranges": [{"source": 0, "offset": 0, "length": 4}, {"data": "aa"}]},
        {"key": "r/concat", "ranges": [{"data": "aa"}, {"source": 1, "offset": 1, "length": 2},
                                       {"source": 2, "offset": 2, "length": 5}]},
        {"key": "r/meta", "ranges": [{"source": 2, "offset": 2, "length": 5}]},
        {"key": "r/empty_concat", "ranges": []},
        {"key": "r/empty_literal", "ranges": [{"data": ""}]},
        {"key": "r/zero_len", "ranges": [{"source": 1, "offset": 5, "length": 0}]},
        # keys whose UTF-8 and UTF-16 orders differ, and a leading byte order mark
        {"key": "o/\ufeffbom", "bytes": H(b"bom")},
        {"key": "o/\U0001F600", "bytes": H(b"grin")},
        {"key": "o/\uff5e", "bytes": H(b"tilde")},
        {"key": "o/\uffff", "bytes": H(b"max-bmp")},
    ]
    basic = {"sources": sources, "entries": entries}
    paged_entries = entries + [
        {"key": f"p/c/{i}", "ranges": [{"source": 2, "offset": i % 8, "length": 3}]} for i in range(300)
    ] + [{"key": f"p/b/{i}", "bytes": H(bytes([i % 256]) * (1 + i % 40))} for i in range(200)]
    paged = {**basic, "entries": paged_entries, "page_size": 256}
    many = {"sources": [{"key": "m/zarr.json"}], "entries": [{"key": "m/zarr.json", "bytes": H(b"{}")}] + [
        {"key": f"m/c/{i}", "ranges": [{"source": 0, "offset": i % 2, "length": 1}]} for i in range(70_000)]}
    return {
        "selfcontained": basic,
        "selfcontained_paged": paged,
        "selfcontained_pages_of_one_key": {**paged, "page_size": 1, "entries": paged_entries[:80]},
        "selfcontained_inline_none": {**paged, "inline_max": 0},
        "selfcontained_inline_all": {**paged, "inline_max": 1 << 20},
        # the names vzip reserves are ordinary keys in a phkv store (spec/bindings/phkv.md §16)
        "reserved_names_are_ordinary": {"sources": [{"key": "__vz__/sources"}], "entries": [
            {"key": "__vz__/sources", "bytes": H(b"not a source table")},
            {"key": "__vz__/index", "bytes": H(b"not an index"), "compress": True},
            {"key": "__vz__/phkv", "bytes": H(b"not a container")},
            {"key": "r", "ranges": [{"source": 0, "offset": 4, "length": 5}]}]},
        "empty": {"sources": [], "entries": []},
        "empty_paged": {"sources": [], "entries": [], "page_size": 100},
        # payloads past vzip's 65,519-byte limit (a ZIP extra field's), up to phkv's 2^24 (§19.3)
        "large_payloads": {"sources": [], "entries": [
            {"key": "many_ranges", "ranges": [{"data": H(bytes([i % 256]) * 8)} for i in range(7000)]},
            {"key": "one_big_literal", "ranges": [{"data": "ab" * 65_516}]},
            {"key": "small", "bytes": "00"}]},
        # the perfect hash at a size that needs several buckets
        "many_keys": many,
    }


def descriptions(root: Path, http_base: str | None = None) -> dict[str, dict]:
    """The self-contained descriptions, then the vzip suite's adapted (they read `file:` sources, which
    an implementation that resolves only HTTP cannot: see run.py `--no-file-sources`)."""
    out = _self_contained()
    base = vzip_cases.descriptions(root, http_base)
    for name, d in base.items():
        if name in ("nomirror", "paged_tiny_pages", "zip64_paged", "empty", "empty_paged", "max_payload"):
            continue
        out[f"files_{name}" if not name.startswith("http") else name] = adapt(d)
    basic = out["files_basic"]
    # where each value's stored bytes live, and how a lookup finds them
    out["files_inline_none"] = {**basic, "inline_max": 0}
    return out


def queries_for(name: str, desc: dict) -> list[dict]:
    if name == "many_keys":
        sub = {**desc, "entries": [desc["entries"][i] for i in (0, 1, 2, 35_000, 69_999, 70_000)]}
        qs = standard_queries(sub)
        return [q for q in qs if q["op"] != "list"] + [{"op": "list", "prefix": "m/zarr"}]
    return standard_queries(desc)


def invalid_descriptions(root: Path) -> dict[str, dict]:
    """Descriptions a writer must reject: the vzip suite's less those about vzip's own limits."""
    vzip_only = {"reserved_sources_key", "reserved_index_key", "key_source_names_format_entry",
                 "pinned_reference", "pinned_without_index", "payload_too_large", "payload_over_limit"}
    # (the last two are valid here: see `large_payloads`)
    out = {name: adapt(d) for name, d in vzip_cases.invalid_descriptions(root).items()
           if name not in vzip_only}
    base = {"sources": [], "entries": []}
    out["inline_max_negative"] = {**base, "inline_max": -1}
    out["inline_max_not_an_integer"] = {**base, "inline_max": 1.5}
    out["page_size_zero_with_keys"] = {**base, "page_size": 0, "entries": [{"key": "a", "bytes": "00"}]}
    return out
