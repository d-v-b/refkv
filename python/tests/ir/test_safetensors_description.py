"""The description of every accepted safetensors fixture, file and store
(spec/zarrify/safetensors.md §8.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#85-the-description>, §9.7
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#97-the-description>), compared
with the safetensors library, the reference reader: the header's length, its `__metadata__`, and
each tensor's name, dtype, shape and bytes (its `data_offsets` in the buffer) are what
`safetensors.deserialize` and `safe_open` read, its other members what Python's `json` reads of the
header, in the header's order; a shard the description gives an error is one the library refuses;
and a document is what `json` reads of it. A second test asserts that the description holds none
of the output's decisions."""

import json
import struct
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import safetensors
from ir_helpers import FIXTURES, ROOT
from safetensors import safe_open

from refkv.build import read

FILES = sorted(p for p in (FIXTURES / "safetensors").iterdir() if p.is_file() and "reject" not in p.name)
STORES = sorted(p for p in (FIXTURES / "safetensors").iterdir() if p.is_dir())
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "safetensors" / "description.json").read_text())
STORE_SCHEMA = {**SCHEMA, "$ref": "#/$defs/store"}


def assert_header_is_the_librarys(header: dict[str, Any], path: Path, at: str) -> None:
    """`header`, a header's description, against the library's reading of the file `path`."""
    raw = path.read_bytes()
    (n,) = struct.unpack("<Q", raw[:8])
    assert header["header_length"] == n, at
    with safe_open(path, "numpy") as f:
        assert header["metadata"] == f.metadata(), at
    theirs = dict(safetensors.deserialize(raw))
    written = json.loads(raw[8:8 + n])
    written.pop("__metadata__", None)
    assert [t["name"] for t in header["tensors"]] == list(written), at
    assert {t["name"] for t in header["tensors"]} == set(theirs), at
    buffer = raw[8 + n:]
    for t in header["tensors"]:
        lib = theirs[t["name"]]
        assert t["dtype"] == lib["dtype"], f"{at}: {t['name']}"
        assert t["shape"] == list(lib["shape"]), f"{at}: {t['name']}"
        begin, end = t["data_offsets"]
        assert buffer[begin:end] == bytes(lib["data"]), f"{at}: {t['name']}"
        extra = {k: v for k, v in written[t["name"]].items() if k not in ("dtype", "shape", "data_offsets")}
        assert t.get("extra", {}) == extra, f"{at}: {t['name']}"


def test_a_safetensors_files_description_is_what_the_library_reads():
    """Every accepted safetensors file: the description is valid by its schema, is the library's
    reading of the header and the tensors, and names the IR's elements of the length, the header
    and each tensor's data, none for a tensor of no bytes."""
    seen = dict.fromkeys(("tensors", "metadata", "extra", "empty"), 0)
    for path in FILES:
        with read(path) as parsed:
            assert parsed.profile == "safetensors", path.name
            d: Any = parsed.description
            assert not list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(d)), path.name
            assert_header_is_the_librarys(d, path, path.name)
            ir, elements = parsed.ir, d["elements"]
            assert ir.path(elements["length"]) == "length", path.name
            assert ir.path(elements["header"]) == "header", path.name
            for i, (t, e) in enumerate(zip(d["tensors"], elements["tensors"], strict=True)):
                begin, end = t["data_offsets"]
                if begin == end:
                    assert e is None, path.name
                    seen["empty"] += 1
                else:
                    assert ir.path(e) == f"tensors/{i}", path.name
                seen["tensors"] += 1
                seen["extra"] += "extra" in t
        seen["metadata"] += d["metadata"] is not None
    assert all(seen.values()), seen


def test_a_safetensors_stores_description_is_what_the_library_and_json_read():
    """Every safetensors store fixture: the description is valid by its schema; each shard's
    header is the library's reading of it, and a shard given an error is one the library refuses;
    and each index or configuration is what Python's `json` reads of it, or, given an error, one
    it does not read."""
    seen = dict.fromkeys(("shards", "refused", "documents", "not json"), 0)
    for store in STORES:
        with read(store) as parsed:
            assert parsed.profile == "safetensors", store.name
            d: Any = parsed.description
        assert not list(jsonschema.Draft202012Validator(STORE_SCHEMA).iter_errors(d)), store.name
        keys = sorted(p.relative_to(store).as_posix() for p in store.rglob("*.safetensors"))
        assert [s["key"] for s in d["shards"]] == keys, store.name
        for s in d["shards"]:
            at = f"{store.name}: {s['key']}"
            if "error" in s:
                with pytest.raises(safetensors.SafetensorError):
                    safetensors.deserialize((store / s["key"]).read_bytes())
                seen["refused"] += 1
                continue
            assert_header_is_the_librarys(s["header"], store / s["key"], at)
            seen["shards"] += 1
        for doc in d["documents"]:
            raw = (store / doc["key"]).read_bytes()
            if "error" in doc:
                with pytest.raises((json.JSONDecodeError, UnicodeDecodeError)):
                    json.loads(raw)
                seen["not json"] += 1
                continue
            assert json.loads(doc["json"]) == json.loads(raw), doc["key"]
            seen["documents"] += 1
    assert all(seen.values()), seen


def test_a_parsed_safetensors_file_describes_every_tensor_and_no_decision():
    """`Parsed.description` of a safetensors file holds every tensor of the header, those the
    hierarchy does not present among them, and none of the hierarchy's decisions: no path, reason,
    Zarr data type, fill value, group or source metadata."""
    with read(FIXTURES / "safetensors" / "st_names.safetensors") as parsed:
        d: Any = parsed.description
    assert set(d) == {"header_length", "metadata", "tensors", "elements"}
    decisions = {"path", "reason", "data_type", "fill", "groups", "own"}
    assert not any(decisions & set(t) for t in d["tensors"])
    raw = (FIXTURES / "safetensors" / "st_names.safetensors").read_bytes()
    written = json.loads(raw[8:8 + struct.unpack("<Q", raw[:8])[0]])
    assert len(d["tensors"]) == len(written) - ("__metadata__" in written)
