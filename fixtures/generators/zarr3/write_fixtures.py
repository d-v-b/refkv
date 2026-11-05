"""Writes synthetic Zarr v3 hierarchies to fixtures/zarr3/<name>/, and zipped ones to
fixtures/zarr3/<name>.zip, covering the rules of the Zarr v3 profile (spec/zarrify/zarr3.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#7-zarr-v3-profile>): both chunk
key encodings and separators, 0-d and empty arrays, missing, empty and out-of-grid chunks, a
sharded array, the rectilinear grid, codecs and data types passed through, implicit groups, a
node inside an array, consolidated metadata, attributes kept in place and moved, nodes left
out, and, in the zip form, a rewritten `zarr.json` (superseded), deflated documents, deflated
chunks, directory entries and the inputs the profile rejects (`zarr3_reject_*`, one per
rejection rule).

Documents and chunks are written by zarr-python, so that js/test/zarr3/verify.py reads them back
with zarr-python itself; documents the profile must refuse are written by hand. The zip files are
written here, not by zarr-python's ZipStore, which stamps each entry with the time it was written:
every entry is dated 1980-01-01, and a deflated entry is deflate's stored blocks, which no
platform's zlib changes. Codecs are zarr-python's but `gzip`, whose zlib output differs between
builds.

Usage: uv run python fixtures/generators/zarr3/write_fixtures.py
"""

from __future__ import annotations

import json
import shutil
import struct
import zlib
from pathlib import Path

import numpy as np
import zarr
from zarr.codecs import BloscCodec, BytesCodec, Crc32cCodec, TransposeCodec, ZstdCodec

OUT = Path(__file__).parents[2] / "zarr3"


def put(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_json(path: Path, doc) -> None:
    put(path, json.dumps(doc, indent=2).encode())


def store(name: str) -> Path:
    d = OUT / name
    d.mkdir(parents=True)
    return d


def array_doc(shape, chunks, **over) -> dict:
    """An array's zarr.json, as zarr-python writes one of uint8 without compression."""
    doc = {
        "zarr_format": 3, "node_type": "array", "shape": shape, "data_type": "uint8",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunks}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": 0, "codecs": [{"name": "bytes"}], "attributes": {},
    }
    doc.update(over)
    return {k: v for k, v in doc.items() if v is not None}


def group_doc(attributes=None) -> dict:
    return {"zarr_format": 3, "node_type": "group", "attributes": attributes or {}}


# ---- zip files


def stored_deflate(data: bytes) -> bytes:
    """A raw deflate stream of stored blocks: the same bytes on every platform."""
    out = bytearray()
    blocks = [data[i:i + 65535] for i in range(0, len(data), 65535)] or [b""]
    for k, b in enumerate(blocks):
        out += bytes([1 if k == len(blocks) - 1 else 0]) + struct.pack("<HH", len(b), len(b) ^ 0xFFFF) + b
    return bytes(out)


def zip_bytes(entries: list[tuple[str, bytes, int]]) -> bytes:
    """A zip file of (name, bytes, method) entries in that order: method 0 stored, 8 deflated
    (stored blocks), any other written as stored bytes under that method's number. Every entry
    is dated 1980-01-01 00:00, with no extra field."""
    body, central = bytearray(), bytearray()
    for name, data, method in entries:
        raw = name.encode()
        payload = stored_deflate(data) if method == 8 else data
        crc = zlib.crc32(data)
        head = struct.pack("<4sHHHHHIIIHH", b"PK\x03\x04", 20, 0x800, method, 0, 0x21, crc, len(payload), len(data),
                           len(raw), 0)
        central += struct.pack("<4sHHHHHHIIIHHHHHII", b"PK\x01\x02", 20, 20, 0x800, method, 0, 0x21, crc,
                               len(payload), len(data), len(raw), 0, 0, 0, 0, 0, len(body)) + raw
        body += head + raw + payload
    end = struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, len(entries), len(entries), len(central), len(body), 0)
    return bytes(body + central + end)


def tree(d: Path) -> list[tuple[str, bytes]]:
    """A directory's files, the root's zarr.json first, then by key."""
    files = sorted((p.relative_to(d).as_posix(), p.read_bytes()) for p in d.rglob("*") if p.is_file())
    return sorted(files, key=lambda f: (f[0] != "zarr.json", f[0]))


def zipped(name: str, d: Path, *, first: list[tuple[str, bytes, int]] = (), method=lambda key: 0) -> None:
    """The directory `d` zipped as fixtures/zarr3/<name>.zip, after the entries `first` (earlier
    versions of its documents, as zarr-python's ZipStore leaves them), each entry's method
    `method(key)`."""
    put(OUT / f"{name}.zip", zip_bytes([*first, *((k, b, method(k)) for k, b in tree(d))]))


# ---- the stores


def hierarchy() -> Path:
    """A root group with groups, an implicit group and arrays of both encodings and separators,
    a 0-d and an empty array, a sharded one, missing, empty and out-of-grid chunks, a node
    inside an array, consolidated metadata and other objects."""
    d = store("zarr3_hierarchy")
    root = zarr.open_group(d, mode="w", zarr_format=3, attributes={"title": "a hierarchy", "n": 18446744073709551615})
    a = root.create_array("a", shape=(4, 6), chunks=(2, 3), dtype="<i2", fill_value=-1, dimension_names=["y", "x"],
                          attributes={"units": "m"})
    a[:2, :] = np.arange(12, dtype="<i2").reshape(2, 6)
    a[2:, :3] = 7  # chunk c/1/1 is missing
    dot = root.create_array("g/dot", shape=(3,), chunks=(2,), dtype="<f4", fill_value=float("nan"),
                            chunk_key_encoding={"name": "default", "separator": "."})
    dot[:] = [1.5, 2.5, 3.5]
    v2 = root.create_array("g/v2", shape=(2, 2), chunks=(1, 2), dtype=">u4", fill_value=0,
                           chunk_key_encoding={"name": "v2", "separator": "."})
    v2[:] = [[1, 2], [3, 4]]
    v2s = root.create_array("g/v2_slash", shape=(2, 2), chunks=(1, 1), dtype="|u1", fill_value=9,
                            chunk_key_encoding={"name": "v2", "separator": "/"})
    v2s[0, :] = [5, 6]
    scalar = root.create_array("scalar", shape=(), dtype="<f8", fill_value=0.0)
    scalar[()] = 2.25
    root.create_array("empty", shape=(0, 3), chunks=(2, 3), dtype="u1")
    sh = root.create_array("sharded", shape=(8, 4), chunks=(2, 2), shards=(4, 4), dtype="<u2",
                           compressors=ZstdCodec(level=3))
    sh[:] = np.arange(32, dtype="<u2").reshape(8, 4)
    zarr.open_group(d / "deep/er", mode="w", zarr_format=3)  # deep/ is an implicit group
    zarr.consolidate_metadata(d)
    write_json(d / "a/inner/zarr.json", group_doc())  # inside an array: not a node
    put(d / "a/c/9/9", b"outside the grid")
    put(d / "g/v2_slash/1/1", b"")  # an empty chunk object: absent
    put(d / "README.md", b"# a hierarchy\n")
    put(d / "g/.zgroup", b'{"zarr_format": 2}')
    put(d / "g/zarr.json~", b"one tilde")
    return d


def scalar_root() -> Path:
    d = store("zarr3_scalar_root")
    a = zarr.create_array(d, shape=(), dtype="<i8", fill_value=0, zarr_format=3)
    a[()] = -5
    put(d / "notes.txt", b"a root array's other object\n")
    return d


def codecs() -> Path:
    """Codecs and data types passed through, a codec no reader knows among them."""
    d = store("zarr3_codecs")
    root = zarr.open_group(d, mode="w", zarr_format=3)
    t = root.create_array("transposed", shape=(2, 3), chunks=(2, 3), dtype="<u2")
    t[:] = np.arange(6, dtype="<u2").reshape(2, 3)
    b = root.create_array("blosc", shape=(4,), chunks=(4,), dtype="<f8",
                          compressors=BloscCodec(cname="lz4", clevel=5, shuffle="shuffle"))
    b[:] = [0.5, 1.5, 2.5, 3.5]
    c = root.create_array("crc", shape=(3,), chunks=(3,), dtype="<i4", serializer=BytesCodec(endian="big"),
                          compressors=[Crc32cCodec()])
    c[:] = [1, -2, 3]
    s = root.create_array("strings", shape=(2,), chunks=(2,), dtype=str)
    s[:] = ["ab", "é"]
    x = root.create_array("transpose_codec", shape=(2, 2), chunks=(2, 2), dtype="u1",
                          filters=[TransposeCodec(order=(1, 0))])
    x[:] = [[1, 2], [3, 4]]
    write_json(d / "custom/zarr.json", array_doc([2], [2], codecs=[{"name": "bytes"}, {"name": "someone.custom"}]))
    put(d / "custom/c/0", b"\x01\x02")
    return d


def rectilinear() -> Path:
    d = store("zarr3_rectilinear")
    write_json(d / "zarr.json", group_doc())
    grid = {"name": "rectilinear", "configuration": {"kind": "inline", "chunk_shapes": [[2, [3, 2]], 4]}}
    write_json(d / "r/zarr.json", array_doc([8, 6], None, chunk_grid=grid))
    for i, rows in enumerate([2, 3, 3]):
        for j in range(2):
            put(d / f"r/c/{i}/{j}", bytes([i * 2 + j] * (rows * 4)))
    put(d / "r/c/3/0", b"beyond the grid's three chunks")
    return d


def conventions() -> Path:
    """Attributes kept in place and moved: other conventions' CMOs follow the convention's own
    in `zarr_conventions`; a `refkv_zarrified`, a `zarr_conventions` that is not a nonempty
    array, and one that already lists the convention, move into the source metadata."""
    d = store("zarr3_conventions")
    proj = {"uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f", "name": "proj:", "description": "a GeoZarr convention"}
    write_json(d / "zarr.json", group_doc({"zarr_conventions": [proj], "proj:code": "EPSG:4326",
                                           "ome": {"version": "0.5"}}))
    write_json(d / "kept/zarr.json", group_doc({"zarr_conventions": [proj], "proj:code": "EPSG:32633"}))
    write_json(d / "nested/zarr.json", group_doc({"refkv_zarrified": {"profile": "tiff"}, "keep": 1}))
    write_json(d / "object/zarr.json", group_doc({"zarr_conventions": {"not": "a list"}}))
    write_json(d / "empty/zarr.json", group_doc({"zarr_conventions": []}))
    ours = {"uuid": "95203d0a-6586-426a-b920-3db2f4f1e97e", "name": "refkv_zarrified"}
    write_json(d / "ours/zarr.json", group_doc({"zarr_conventions": [ours]}))
    write_json(d / "bare/zarr.json", {"zarr_format": 3, "node_type": "group"})
    return d


def left_out() -> Path:
    """One array presented, and below the root every node a rule leaves out, its objects kept."""
    d = store("zarr3_left_out")
    write_json(d / "zarr.json", group_doc())
    write_json(d / "ok/zarr.json", array_doc([2], [2]))
    put(d / "ok/c/0", b"\x01\x02")
    cases = {
        "grid": array_doc([2], None, chunk_grid={"name": "irregular", "configuration": {}}),
        "grid_dims": array_doc([2, 2], [2]),
        "encoding": array_doc([2], [2], chunk_key_encoding={"name": "hashed"}),
        "separator": array_doc([2], [2], chunk_key_encoding={"name": "default", "configuration": {"separator": "-"}}),
        "transformers": array_doc([2], [2], storage_transformers=[{"name": "foo"}]),
        "no_codecs": array_doc([2], [2], codecs=[]),
        "no_fill": array_doc([2], [2], fill_value=None),
        "shape": array_doc([-1], [2]),
        "format": {**group_doc(), "zarr_format": 2},
        "node_type": {**group_doc(), "node_type": "folder"},
        "attributes": {**group_doc(), "attributes": [1]},
    }
    for name, doc in cases.items():
        write_json(d / f"{name}/zarr.json", doc)
        put(d / f"{name}/c/0", b"\x01\x02")
    put(d / "not_json/zarr.json", b"{\"zarr_format\": 3,")
    put(d / "not_json/c/0", b"\x01\x02")
    put(d / "empty_doc/zarr.json", b"")
    write_json(d / "grid/inner/zarr.json", group_doc())  # below a node left out: not a node
    return d


def zips(h: Path) -> None:
    # zarr-python's ZipStore rewrites a document by adding an entry of the same name: the last
    # entry of a name is the object, the earlier ones superseded
    old = json.dumps(group_doc({"title": "an earlier version"}), indent=2).encode()
    zipped("zarr3_hierarchy", h, first=[("zarr.json", old, 0), ("g/zarr.json", old, 0)])
    # documents deflated, chunks stored; directory entries and keys a listing ignores
    d = store("_zip_mixed")
    write_json(d / "zarr.json", group_doc({"x": 1}))
    a = zarr.create_array(d / "a", shape=(4,), chunks=(2,), dtype="<u2", fill_value=0, zarr_format=3)
    a[:] = [1, 2, 3, 4]
    entries = [("a/", b"", 0), *((k, b, 8 if k.endswith("zarr.json") else 0) for k, b in tree(d)),
               ("b/./c", b"ignored", 0), ("notes/", b"a directory entry with bytes", 0)]
    put(OUT / "zarr3_deflated_documents.zip", zip_bytes(entries))
    # an array whose chunks are deflated is left out; a stored one is presented
    write_json(d / "z/zarr.json", array_doc([2], [2]))
    put(d / "z/c/0", b"\x01\x02")
    zipped("zarr3_compressed_chunks", d, method=lambda k: 8 if k == "z/c/0" else 0)
    shutil.rmtree(d)


def rejects() -> None:
    """Each the smallest store that breaks its rule: a root zarr.json and no chunk."""
    for name, doc in {
        "zarr3_reject_root_zarr_format": {**group_doc(), "zarr_format": 2},
        "zarr3_reject_root_node_type": {**group_doc(), "node_type": "folder"},
        "zarr3_reject_root_not_object": [1, 2],
        "zarr3_reject_root_attributes": {**group_doc(), "attributes": "none"},
        "zarr3_reject_root_shape": array_doc([2, 1.5], [2, 1]),
        "zarr3_reject_root_grid": array_doc([2], None, chunk_grid={"name": "irregular"}),
        "zarr3_reject_root_grid_dims": array_doc([2], [2, 2]),
        "zarr3_reject_root_rectilinear": array_doc([2], None, chunk_grid={"name": "rectilinear", "configuration": {
            "kind": "inline", "chunk_shapes": [[[2, 0]]]}}),
        "zarr3_reject_root_encoding": array_doc([2], [2], chunk_key_encoding={"name": "hashed"}),
        "zarr3_reject_root_separator": array_doc([2], [2], chunk_key_encoding={"name": "v2", "configuration": {
            "separator": "|"}}),
        "zarr3_reject_root_transformers": array_doc([2], [2], storage_transformers=[{"name": "foo"}]),
        "zarr3_reject_root_data_type": array_doc([2], [2], data_type=None),
        "zarr3_reject_root_fill_value": array_doc([2], [2], fill_value=None),
        "zarr3_reject_root_codecs": array_doc([2], [2], codecs={"name": "bytes"}),
    }.items():
        write_json(store(name) / "zarr.json", doc)
    put(store("zarr3_reject_root_empty") / "zarr.json", b"")
    put(store("zarr3_reject_root_not_json") / "zarr.json", b"{\"zarr_format\": 3, }")
    d = store("zarr3_reject_no_root")
    write_json(d / "a/zarr.json", group_doc())
    d = store("zarr3_reject_objects_collision")
    write_json(d / "zarr.json", group_doc())
    write_json(d / "refkv_source/x/zarr.json", group_doc())
    root = json.dumps(group_doc()).encode()
    put(OUT / "zarr3_reject_zip_no_root.zip", zip_bytes([("a/zarr.json", root, 0)]))
    put(OUT / "zarr3_reject_zip_root_method.zip", zip_bytes([("zarr.json", root, 12)]))
    scalar = json.dumps(array_doc([], [])).encode()
    put(OUT / "zarr3_reject_zip_root_chunk.zip", zip_bytes([("zarr.json", scalar, 0), ("c", b"\x05", 8)]))
    put(OUT / "zarr3_reject_zip_no_eocd.zip", zip_bytes([("zarr.json", root, 0)])[:-22])


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    h = hierarchy()
    scalar_root()
    codecs()
    rectilinear()
    conventions()
    left_out()
    zips(h)
    rejects()
    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"{sum(1 for _ in OUT.iterdir())} inputs, {total} bytes")


if __name__ == "__main__":
    main()
