"""Writes synthetic Zarr v2 hierarchies to fixtures/zarr2/<name>/,
covering the rules of the Zarr v2 profile (spec/zarrify/zarr2.md §7
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#7-zarr-v2-profile>): C and F
order, both dimension separators, every supported data type and byte order,
each compressor (none, zlib, gzip, zstd, blosc with each shuffle), fill
values (numbers, "NaN", "Infinity", "-Infinity", null), 0-d arrays, missing
and empty chunks, implicit groups, nodes inside arrays, stale consolidated
metadata, OME-NGFF attributes below a root that does not declare
OME-NGFF 0.4 (copied unchanged), the data types of NPY's table, the filters and
the other compressors, arrays that are left out, and the inputs the profile
rejects (`zarr2_reject_*`, one per rejection rule).

Most arrays are one chunk, so that each store is a handful of objects; only
the arrays that test the chunk grid have two or three chunks (a partial edge
chunk, a missing chunk, an empty chunk, each separator, F order). Each
rejection is the smallest store that breaks its rule: one `.zarray` at the
root, with no chunk.

Documents and chunks are written here as zarr-python 2 writes them (each
chunk the full chunk shape, padded with the fill value, in the array's
order, then compressed with numcodecs); js/test/zarr2/verify.py reads them
back with zarr-python's own Zarr v2 reader.

Usage: uv run python fixtures/generators/zarr2/write_fixtures.py
"""

from __future__ import annotations

import base64
import gzip
import json
import shutil
from pathlib import Path
from typing import Literal

import numcodecs
import numpy as np
from numcodecs.compat import ensure_bytes


def pinned_gzip(data: bytes, level: int = 9) -> bytes:
    """gzip.compress with modification time 0 and the header's OS byte pinned to 255
    ("unknown"): zlib writes its build's OS code there (3 on Linux, 19 on macOS), so
    unpinned fixtures differ between platforms."""
    out = bytearray(gzip.compress(data, compresslevel=level, mtime=0))
    out[9] = 255
    return bytes(out)

OUT = Path(__file__).parents[2] / "zarr2"
RNG = np.random.default_rng(2)


def put(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_json(path: Path, doc) -> None:
    put(path, json.dumps(doc, indent=2, allow_nan=True).encode())


class _Gzip:
    """numcodecs' gzip, with the header's modification time 0 (numcodecs writes the
    current time): reproducible files. The members are what numcodecs writes otherwise."""

    def __init__(self, level: int):
        self.level = level

    def encode(self, data: bytes) -> bytes:
        return pinned_gzip(data, self.level)


def codec(compressor: dict | None):
    if compressor is None:
        return None
    c = dict(compressor)
    if c.get("id") == "gzip" and isinstance(c.get("level"), int):
        return _Gzip(c["level"])
    return numcodecs.get_codec(c)


def array(root: Path, path: str, data: np.ndarray, chunks: list[int], *, compressor=None,
          order: Literal["C", "F"] = "C", sep=None, fill=0, skip=frozenset(), attrs=None, dtype: str | None = None,
          zarray_extra=None, write_chunks: bool = True) -> None:
    d = root / path if path else root
    doc = {"zarr_format": 2, "shape": list(data.shape), "chunks": chunks, "dtype": dtype or data.dtype.str,
           "compressor": compressor, "fill_value": fill, "order": order, "filters": None}
    if sep is not None:
        doc["dimension_separator"] = sep
    doc.update(zarray_extra or {})
    write_json(d / ".zarray", doc)
    if attrs is not None:
        write_json(d / ".zattrs", attrs)
    if not write_chunks:
        return
    c = codec(compressor)
    if data.ndim == 0:
        put(d / "0", c.encode(data.tobytes()) if c else data.tobytes())
        return
    grid = [-(-s // n) for s, n in zip(data.shape, chunks, strict=True)]
    padding = 0 if fill is None or isinstance(fill, str) else fill
    if isinstance(fill, str):
        padding = float(fill.replace("Infinity", "inf"))
    for idx in np.ndindex(*grid):
        if idx in skip:
            continue
        sl = tuple(slice(i * n, min((i + 1) * n, s)) for i, n, s in zip(idx, chunks, data.shape, strict=True))
        block = np.full(chunks, padding, dtype=data.dtype)
        block[tuple(slice(0, e.stop - e.start) for e in sl)] = data[sl]
        raw = block.tobytes(order=order)
        key = (sep or ".").join(map(str, idx))
        put(d / key, c.encode(raw) if c else raw)


def group(root: Path, path: str, attrs=None) -> None:
    d = root / path if path else root
    write_json(d / ".zgroup", {"zarr_format": 2})
    if attrs is not None:
        write_json(d / ".zattrs", attrs)


def arr(shape, dtype) -> np.ndarray:
    dt = np.dtype(dtype)
    if dt.kind == "f":
        return (RNG.standard_normal(shape) * 100).astype(dt)
    if dt.kind == "b":
        return RNG.integers(0, 2, size=shape).astype(dt)
    info = np.iinfo(dt)
    return RNG.integers(info.min, info.max, size=shape, endpoint=True, dtype=dt.newbyteorder("=")).astype(dt)


def store(name: str) -> Path:
    d = OUT / name
    d.mkdir(parents=True)
    return d


LZMA_CHUNK = base64.b64decode("/Td6WFoAAATm1rRGAgAhARYAAAB0L+WjAQALAAABAAIAAwAEAAUAANUEJ4Ym"
                              "oKqbAAEkDKYY2NgftvN9AQAAAAAEWVo=")
ZLIB = {"id": "zlib", "level": 1}
GZIP = {"id": "gzip", "level": 5}
ZSTD = {"id": "zstd", "level": 1, "checksum": False}


def blosc(cname="lz4", clevel=5, shuffle=1, blocksize=0):
    return {"id": "blosc", "cname": cname, "clevel": clevel, "shuffle": shuffle, "blocksize": blocksize}


def stored(root: Path, path: str, doc: dict, chunks: dict[str, bytes], attrs=None) -> None:
    """An array whose `.zarray` is the defaults below with `doc` set over them, and whose chunk
    objects are given, for what `array` does not encode: strings, filters and other types."""
    d = root / path if path else root
    write_json(d / ".zarray", {"zarr_format": 2, "compressor": None, "fill_value": None, "order": "C", "filters": None,
                               **doc})
    if attrs is not None:
        write_json(d / ".zattrs", attrs)
    for key, data in chunks.items():
        put(d / key, data)


def types() -> None:
    """The data types of NPY's table (spec/zarrify/zarr2.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays>), each with what
    its fill value may be. The NOAA NWM retrospective stores' two forms come first: a scalar
    `|S1` with no chunk encoding, and a blosc `|S15`."""
    d = store("zarr2_types")
    group(d, "")
    stored(d, "crs", {"shape": [], "chunks": [], "dtype": "|S1"}, {"0": b"x"}, attrs={"grid_mapping_name": "lcc"})
    ids = np.array([b"01010000", b"02010000", b"14316700"], dtype="|S15")
    stored(d, "gage_id", {"shape": [3], "chunks": [3], "dtype": "|S15", "compressor": blosc()},
           {"0": codec(blosc()).encode(ids.tobytes())})
    grid = np.array([[b"ab", b"cd", b"ef"], [b"gh", b"ij", b"kl"]], dtype="|S2")
    stored(d, "bytes_f_nested", {"shape": [2, 3], "chunks": [2, 3], "dtype": "|S2", "order": "F",
                                 "dimension_separator": "/"}, {"0/0": grid.tobytes(order="F")})
    stored(d, "bytes_fill", {"shape": [2], "chunks": [1], "dtype": "|S2",
                             "fill_value": base64.b64encode(b"  ").decode()},
           {"1": b"zz"})
    stored(d, "bytes_empty_fill", {"shape": [2], "chunks": [2], "dtype": "|S2", "fill_value": ""}, {})
    stored(d, "void", {"shape": [2], "chunks": [2], "dtype": "|V2"}, {"0": b"\x01\x02\x03\x04"})
    stored(d, "unicode_le", {"shape": [2], "chunks": [2], "dtype": "<U3", "fill_value": "n/a"},
           {"0": np.array(["ab", "é"], dtype="<U3").tobytes()})
    stored(d, "unicode_be", {"shape": [2], "chunks": [2], "dtype": ">U2", "fill_value": ""},
           {"0": np.array(["x", "yz"], dtype=">U2").tobytes()})
    stored(d, "complex64", {"shape": [2], "chunks": [2], "dtype": "<c8", "fill_value": [1.5, -0.0]},
           {"0": np.array([1 + 2j, 3j], dtype="<c8").tobytes()})
    stored(d, "complex128_be", {"shape": [2], "chunks": [1], "dtype": ">c16", "fill_value": ["NaN", "Infinity"]},
           {"0": np.array([1 + 2j], dtype=">c16").tobytes()})
    stored(d, "complex_number_fill", {"shape": [1], "chunks": [1], "dtype": "<c16", "fill_value": 0}, {})
    stored(d, "complex_null_fill", {"shape": [1], "chunks": [1], "dtype": "<c8"}, {})
    stored(d, "datetime_nat", {"shape": [2], "chunks": [2], "dtype": "<M8[ns]", "fill_value": -9223372036854775808},
           {"0": np.array(["2020-01-01", "NaT"], dtype="<M8[ns]").tobytes()})
    stored(d, "timedelta_be", {"shape": [1], "chunks": [1], "dtype": ">m8[10s]", "fill_value": 7},
           {"0": np.array([5], dtype=">m8[10s]").tobytes()})
    stored(d, "datetime_word", {"shape": [1], "chunks": [1], "dtype": "<M8[D]", "fill_value": "NaT"}, {})
    fields = [["a", "<u2"], ["b", "<f4"]]
    stored(d, "struct", {"shape": [2], "chunks": [2], "dtype": fields, "fill_value": "AAAAAAAA"},
           {"0": np.array([(1, 0.5), (2, 1.5)], dtype=[("a", "<u2"), ("b", "<f4")]).tobytes()})
    stored(d, "struct_nested", {"shape": [1], "chunks": [1], "dtype": [["p", [["x", ">i2"]]], ["q", "|u1"]]},
           {"0": b"\x00\x01\x02"})


def codecs() -> None:
    """The filters and the other compressors (spec/zarrify/zarr2.md §3.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#31-compressors>, §3.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#33-filters>), with an
    AnnData store's strings: `|O`, `vlen-utf8`, blosc and a `fill_value` of 0."""
    d = store("zarr2_codecs")
    group(d, "")
    plain = np.arange(6, dtype="<u2").reshape(2, 3)
    for name, comp in (("lz4", {"id": "lz4", "acceleration": 1}), ("lz4_default", {"id": "lz4"}),
                       ("bz2", {"id": "bz2", "level": 1}), ("bz2_default", {"id": "bz2"}),
                       ("lz4_extra", {"id": "lz4", "acceleration": 1, "writer": "x"})):
        known = {k: v for k, v in comp.items() if k != "writer"}
        stored(d, name, {"shape": [2, 3], "chunks": [2, 3], "dtype": "<u2", "compressor": comp},
               {"0.0": ensure_bytes(numcodecs.get_codec(known).encode(plain.tobytes()))})
    # liblzma's output is not the same in all its versions: the bytes of numcodecs.LZMA().encode
    stored(d, "lzma", {"shape": [2, 3], "chunks": [2, 3], "dtype": "<u2", "compressor": {
        "id": "lzma", "format": 1, "check": -1, "preset": None, "filters": None}}, {"0.0": LZMA_CHUNK})
    stored(d, "lzma_default", {"shape": [2, 3], "chunks": [2, 3], "dtype": "<u2", "compressor": {"id": "lzma"}},
           {"0.0": LZMA_CHUNK})

    x = np.arange(12, dtype="<f8").reshape(3, 4) / 4
    fso = {"id": "fixedscaleoffset", "scale": 4, "offset": 1, "dtype": "<f8", "astype": "<i2"}
    enc = numcodecs.get_codec(fso)
    stored(d, "fixedscaleoffset", {"shape": [3, 4], "chunks": [3, 4], "dtype": "<f8", "filters": [fso]},
           {"0.0": ensure_bytes(enc.encode(x))})
    stored(d, "fixedscaleoffset_f_zlib", {"shape": [3, 4], "chunks": [3, 4], "dtype": "<f8", "order": "F",
                                          "filters": [fso],
                                          "compressor": ZLIB},
           {"0.0": codec(ZLIB).encode(ensure_bytes(enc.encode(np.asfortranarray(x))))})
    # the blosc shuffle of what the filter gives: u1 is a bitshuffle, as numcodecs' automatic one
    to_u1 = {"id": "fixedscaleoffset", "scale": 1, "offset": 0, "dtype": "<f8", "astype": "|u1"}
    stored(d, "fixedscaleoffset_u1_blosc", {"shape": [3, 4], "chunks": [3, 4], "dtype": "<f8", "filters": [to_u1],
                                            "compressor": blosc("lz4", 5, -1)},
           {"0.0": codec(blosc("lz4", 5, -1)).encode(ensure_bytes(numcodecs.get_codec(to_u1).encode(x)))})
    be = {"id": "fixedscaleoffset", "scale": 10, "offset": 0, "dtype": ">f4", "astype": ">i4"}
    stored(d, "fixedscaleoffset_be", {"shape": [2], "chunks": [2], "dtype": ">f4", "filters": [be]},
           {"0": ensure_bytes(numcodecs.get_codec(be).encode(np.array([0.5, 1.5], dtype=">f4")))})
    q = {"id": "quantize", "digits": 2, "dtype": "<f8", "astype": "<f8"}
    stored(d, "quantize", {"shape": [3, 4], "chunks": [3, 4], "dtype": "<f8", "filters": [q], "compressor": ZSTD},
           {"0.0": codec(ZSTD).encode(ensure_bytes(numcodecs.get_codec(q).encode(x)))})
    q_then_fso = {"id": "fixedscaleoffset", "scale": 8, "offset": 0, "dtype": "<f8", "astype": "<i4"}
    stored(d, "quantize_then_fixedscaleoffset", {"shape": [3, 4], "chunks": [3, 4], "dtype": "<f8", "filters": [q,
                                                                                                                q_then_fso]},
           {"0.0": ensure_bytes(numcodecs.get_codec(q_then_fso).encode(numcodecs.get_codec(q).encode(x)))})

    vlen = numcodecs.VLenUTF8()
    names = np.array(["CD4", "é", "", "B cell"], dtype=object)
    stored(d, "strings_anndata", {"shape": [4], "chunks": [4], "dtype": "|O", "filters": [{"id": "vlen-utf8"}],
                                  "fill_value": 0, "compressor": blosc()},
           {"0": codec(blosc()).encode(vlen.encode(names))}, attrs={"encoding-type": "string-array"})
    stored(d, "strings_null_fill", {"shape": [4], "chunks": [2], "dtype": "|O", "filters": [{"id": "vlen-utf8"}]},
           {"0": vlen.encode(names[:2])})
    stored(d, "strings_f_fill", {"shape": [2, 2], "chunks": [2, 2], "dtype": "|O", "order": "F",
                                 "filters": [{"id": "vlen-utf8"}],
                                 "fill_value": "none", "compressor": ZLIB},
           {"0.0": codec(ZLIB).encode(vlen.encode(np.asfortranarray(names.reshape(2, 2))))})


def left_out() -> None:
    """A store with one array of each kind that is left out (spec/zarrify/zarr2.md §3.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#34-arrays-that-are-not-presented>),
    among arrays that are presented, its chunks and nested documents other objects."""
    d = store("zarr2_left_out")
    group(d, "", {"title": "left out"})
    level = np.arange(4, dtype="|u1").reshape(2, 2)  # no RNG: the stores after these keep their bytes
    array(d, "kept", level, [2, 2])
    stored(d, "pickled", {"shape": [2], "chunks": [2], "dtype": "|O", "filters": [{"id": "pickle"}]}, {"0": b"pickle"},
           attrs={"units": "kept in the mirror"})
    stored(d, "a/filtered", {"shape": [2], "chunks": [2], "dtype": "<u2", "filters": [{"id": "delta", "dtype": "<u2"}]},
           {"0": b"\x01\x00\x01\x00"})  # a/ is an implicit group, with nothing in it
    stored(d, "shapeless", {"shape": "2", "chunks": [2], "dtype": "<u2"}, {"0": b"\x00\x00"})
    stored(d, "fill", {"shape": [1], "chunks": [1], "dtype": "|S2", "fill_value": "AAE="}, {})
    put(d / "not_json/.zarray", b"{not json")
    put(d / "not_json/0", b"x")
    put(d / "empty_doc/.zarray", b"")
    put(d / "empty_doc/0", b"x")
    array(d, "bad_attrs", level, [2, 2], attrs=["not", "an", "object"])
    group(d, "pickled/inside")  # inside an array that is left out: not a node either
    put(d / "pickled/empty", b"")


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # Compressors, orders and separators. Two arrays have a grid of two chunks with a
    # partial edge chunk: raw_c (C order, "." separator, along the first axis) and
    # zstd_f_nested (F order, "/" separator, along the second axis); the others are one
    # chunk, padded where the chunk shape is larger than the shape.
    d = store("zarr2_compressors")
    group(d, "", {"title": "compressors"})
    array(d, "raw_c", arr((7, 5), "<i2"), [4, 5])
    array(d, "zlib_f", arr((6, 5, 4), "<u2"), [6, 6, 4], compressor=ZLIB, order="F")
    array(d, "gzip_nested", arr((9, 4), "<f4"), [9, 4], compressor=GZIP, sep="/")
    array(d, "zstd_f_nested", arr((5, 5, 3), "<f8"), [5, 3, 4], compressor=ZSTD, order="F", sep="/")
    array(d, "blosc_shuffle", arr((10, 6), "<i4"), [10, 6], compressor=blosc())
    array(d, "blosc_bitshuffle", arr((6, 6), "<u8"), [6, 6], compressor=blosc("zstd", 3, 2))
    array(d, "blosc_noshuffle", arr((6, 3), "<i8"), [6, 4], compressor=blosc("zlib", 1, 0, 256))
    array(d, "blosc_autoshuffle_u1", arr((9, 9), "|u1"), [9, 9], compressor=blosc("blosclz", 9, -1))
    array(d, "blosc_autoshuffle_i2", arr((5, 9), "<i2"), [5, 9], compressor=blosc("lz4hc", 2, -1))
    array(d, "f_order_1d", arr((11,), "<u4"), [12], compressor=ZLIB, order="F")

    # Data types and byte orders.
    d = store("zarr2_dtypes")
    group(d, "")
    for dt in ("|b1", "|i1", "|u1", ">i2", "<u2", ">u4", "<i4", ">i8", "<u8", "<f2", ">f2", ">f4",
               "<f4", ">f8"):
        name = dt.replace("<", "le_").replace(">", "be_").replace("|", "na_")
        array(d, name, arr((5, 3), dt), [5, 3], dtype=dt, fill=False if dt.endswith("b1") else 0)

    # Fill values, missing and empty chunks: each array is two chunks along the first
    # axis, the first missing (read as the fill value) and the second a partial edge
    # chunk, padded with the fill value.
    d = store("zarr2_fill_values")
    group(d, "")
    sparse = {(0, 0)}
    array(d, "nan", arr((5, 6), "<f4"), [3, 6], fill="NaN", skip=sparse)
    array(d, "inf", arr((5, 6), "<f8"), [3, 6], fill="Infinity", skip=sparse, compressor=ZLIB)
    array(d, "neg_inf", arr((5, 6), ">f4"), [3, 6], fill="-Infinity", skip=sparse)
    array(d, "null_int", arr((5, 6), "<i2"), [3, 6], fill=None, skip=sparse)
    array(d, "null_bool", arr((5, 6), "|b1"), [3, 6], fill=None, skip=sparse)
    array(d, "true_bool", arr((5, 6), "|b1"), [3, 6], fill=True, skip=sparse)
    array(d, "neg_i1", arr((5, 6), "|i1"), [3, 6], fill=-5, skip=sparse)
    array(d, "float_fill", arr((5, 6), "<f8"), [3, 6], fill=0.5, skip=sparse)
    array(d, "float_as_int_fill", arr((5, 6), "<u2"), [3, 6], fill=7.0, skip=sparse)
    array(d, "f2_max", arr((3, 4), "<f2"), [2, 4], fill=65504, skip=sparse)
    put(d / "nan/0.0", b"")  # an empty chunk: no entry

    # 0-d arrays, a root array, extra objects.
    d = store("zarr2_scalar_root")
    array(d, "", np.array(42.5, dtype="<f8"), [], compressor=ZLIB, attrs={"units": "K"})

    d = store("zarr2_hierarchy")
    group(d, "", {"root": True})
    array(d, "a/b/zero_d", np.array(7, dtype="<i4"), [])  # a/ and a/b/ are implicit groups
    array(d, "a/c", arr((4, 4), "<u2"), [2, 4], sep="/", attrs={"_ARRAY_DIMENSIONS": ["y", "x"]})
    group(d, "a/c/inside")  # inside an array: not a node
    put(d / "a/c/0/9", b"x")  # outside the grid
    put(d / "a/c/00/1", b"x")  # leading zero
    put(d / "a/c/0.0", b"x")  # the other separator
    group(d, "g", {"empty": {}})
    array(d, "g/h", arr((3,), "<f4"), [4])
    put(d / "g/h/0.0", b"x")  # too many indices for a 1-d array
    put(d / "notes/README.md", b"not part of the hierarchy\n")
    write_json(d / "orphan/.zattrs", {"no": "node"})  # .zattrs without .zarray or .zgroup
    group(d, "sp ace/é")
    array(d, "sp ace/é/x y", arr((2, 2), "|u1"), [2, 2])
    array(d, "empty_shape", np.zeros((0, 3), "<i2"), [2, 2])
    # Stale consolidated metadata: never read.
    write_json(d / ".zmetadata", {"zarr_consolidated_format": 1, "metadata": {
        ".zgroup": {"zarr_format": 2}, "ghost/.zarray": {"shape": [1]}}})

    # OME-NGFF 0.4 attributes below a root that does not declare OME-NGFF 0.4
    # (spec/zarrify.md §1.4
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): the store is
    # read by §7
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#7-zarr-v2-profile>, and
    # every attribute is
    # copied unchanged (OME-Zarr 0.4 stores are read by spec/zarrify/ome-zarr.md).
    d = store("zarr2_ome_attrs")
    img = arr((2, 8, 10), "<u2")
    axes = [{"name": "c", "type": "channel"}, {"name": "y", "type": "space", "unit": "micrometer"},
            {"name": "x", "type": "space", "unit": "micrometer"}]
    group(d, "", {"note": "a plain group"})
    group(d, "0", {
        "multiscales": [{"version": "0.4", "name": "image", "axes": axes, "datasets": [
            {"path": str(i), "coordinateTransformations": [{"type": "scale", "scale": [1, 0.5 * 2**i, 0.5 * 2**i]}]}
            for i in range(2)], "type": "mean", "metadata": {"method": "stride"}}],
        "omero": {"channels": [{"label": "a", "color": "FF0000", "window": {"start": 0, "end": 1000, "min": 0,
                                                                            "max": 65535}, "active": True}] * 2,
                  "rdefs": {"model": "color"}},
        "other": "kept"})
    for i in range(2):
        array(d, f"0/{i}", img[:, :: 2**i, :: 2**i], [2, 8, 10], compressor=blosc(), sep="/")
    group(d, "0/labels", {"labels": ["cells"]})
    group(d, "0/labels/cells", {"multiscales": [{"version": "0.4", "axes": axes, "datasets": [
        {"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [1, 0.5, 0.5]}]}]}],
        "image-label": {"version": "0.4", "colors": [{"label-value": 1, "rgba": [255, 0, 0, 255]}]}})
    array(d, "0/labels/cells/0", arr((2, 8, 10), "<u4"), [2, 8, 10], compressor=ZSTD, sep="/")
    # Other versions, a group that already has `ome`, and mixed versions.
    group(d, "v03", {"multiscales": [{"version": "0.3", "axes": ["y", "x"], "datasets": [{"path": "0"}]}]})
    array(d, "v03/0", arr((3, 3), "|u1"), [3, 3])
    group(d, "has_ome", {"ome": {"version": "0.5"}, "multiscales": [{"version": "0.4", "datasets": []}]})
    group(d, "mixed", {"multiscales": [{"version": "0.4"}, {"version": "0.5"}]})
    # Axes that are not valid OME-NGFF (duplicates).
    group(d, "dup_axes", {"multiscales": [{"version": "0.4", "axes": [{"name": "x"}, {"name": "x"}],
                                           "datasets": [{"path": "0"}]}]})
    array(d, "dup_axes/0", arr((3, 3), "|u1"), [3, 3])

    # Rejections, one per rule: an array at the root, with no chunk.
    def reject(name: str, **kw):
        d = store(f"zarr2_reject_{name}")
        doc = {"zarr_format": 2, "shape": [4, 4], "chunks": [2, 2], "dtype": "<u2", "compressor": None,
               "fill_value": 0, "order": "C", "filters": None}
        raw = kw.pop("raw", None)
        doc.update(kw)
        if raw is not None:
            put(d / ".zarray", raw)
        else:
            write_json(d / ".zarray", {k: v for k, v in doc.items() if v != "DROP"})

    def accepted(name: str, **kw):
        """Inputs the frozen reference rejects and the profile presents since it froze
        (conformance/zarrify/revised/zarr2.txt), with no chunk."""
        d = store(f"zarr2_accepted_{name}")
        write_json(d / ".zarray", {"zarr_format": 2, "shape": [4, 4], "chunks": [2, 2], "dtype": "<u2",
                                   "compressor": None,
                                   "fill_value": 0, "order": "C", "filters": None, **kw})

    reject("filters", filters=[{"id": "delta", "dtype": "<u2"}])
    reject("filters_not_list", filters={"id": "delta"})
    reject("filter_no_id", filters=[{"dtype": "<u2"}])
    reject("filter_member", filters=[{"id": "fixedscaleoffset", "scale": 1, "offset": 0, "dtype": "<u2",
                                      "astype": "|u1",
                                      "writer": "x"}])
    reject("filter_numbers", filters=[{"id": "fixedscaleoffset", "scale": "1", "offset": 0, "dtype": "<u2",
                                       "astype": "|u1"}])
    reject("filter_dtype", filters=[{"id": "fixedscaleoffset", "scale": 1, "offset": 0, "dtype": "<f8",
                                     "astype": "|u1"}])
    reject("filter_dtype_kind", filters=[{"id": "fixedscaleoffset", "scale": 1, "offset": 0, "dtype": "<M8[s]",
                                          "astype": "|u1"}])
    reject("filter_astype", filters=[{"id": "fixedscaleoffset", "scale": 1, "offset": 0, "dtype": "<u2",
                                      "astype": "<c8"}])
    reject("quantize_integer", filters=[{"id": "quantize", "digits": 2, "dtype": "<u2", "astype": "<u2"}])
    reject("quantize_digits", dtype="<f8", filters=[{"id": "quantize", "digits": "2", "dtype": "<f8", "astype": "<f8"}])
    # zarr-python's numcodecs.quantize does not give the bytes codec its astype
    reject("quantize_astype", dtype="<f8", filters=[{"id": "quantize", "digits": 2, "dtype": "<f8", "astype": "<f4"}])
    reject("vlen_not_object", filters=[{"id": "vlen-utf8"}])
    reject("vlen_not_alone", dtype="|O", filters=[{"id": "vlen-utf8"}, {"id": "zlib"}])
    reject("vlen_bytes", dtype="|O", filters=[{"id": "vlen-bytes"}])
    accepted("compressor_lz4", compressor={"id": "lz4", "acceleration": 1})
    accepted("compressor_bz2", compressor={"id": "bz2", "level": 1})
    reject("compressor_no_id", compressor={"level": 1})
    reject("lz4_acceleration", compressor={"id": "lz4", "acceleration": "1"})
    reject("bz2_level", compressor={"id": "bz2", "level": 0})
    reject("lzma_format", compressor={"id": "lzma", "format": 4})
    reject("lzma_check", compressor={"id": "lzma", "check": "crc"})
    reject("lzma_preset", compressor={"id": "lzma", "preset": "6"})
    reject("lzma_filters", compressor={"id": "lzma", "filters": {"id": 3}})
    reject("blosc_cname", compressor=blosc("snappy2"))
    reject("blosc_shuffle", compressor=blosc(shuffle=3))
    reject("blosc_clevel", compressor={"id": "blosc", "cname": "lz4", "shuffle": 1})
    accepted("dtype_unicode", dtype="<U4", fill_value="")
    accepted("dtype_bytes", dtype="|S8", fill_value=None)
    reject("dtype_object", dtype="|O")
    accepted("dtype_complex", dtype="<c8", fill_value=[0, 0])
    accepted("dtype_datetime", dtype="<M8[ns]")
    accepted("dtype_structured", dtype=[["a", "<u2"], ["b", "<f4"]], fill_value=None)
    reject("dtype_no_byteorder", dtype="|u2")
    reject("dtype_long_double", dtype="<f16")
    reject("dtype_field_of_shape", dtype=[["a", "<u2", [2]]])
    reject("dtype_two_byte_orders", dtype=[["a", "<u2"], ["b", ">u2"]])
    reject("dtype_no_unit", dtype="<M8")
    reject("fill_bytes_mixed", dtype="|S2", fill_value="AAE=")
    reject("fill_bytes_base64", dtype="|S2", fill_value="not base64")
    reject("fill_bytes_long", dtype="|S1", fill_value="AAA=")
    reject("fill_bytes_number", dtype="|S2", fill_value=0)
    reject("fill_complex_word", dtype="<c8", fill_value="x")
    reject("fill_complex_overflow", dtype="<c8", fill_value=[1e39, 0])
    reject("fill_complex_length", dtype="<c8", fill_value=[0, 0, 0])
    reject("fill_time_fraction", dtype="<M8[s]", fill_value=1.5)
    reject("fill_time_word", dtype="<M8[s]", fill_value="never")
    reject("fill_text_long", dtype="<U1", fill_value="ab")
    reject("fill_text_number", dtype="<U1", fill_value=0)
    reject("fill_struct_nonzero", dtype=[["a", "|u1"]], fill_value="AQ==")
    reject("fill_string_number", dtype="|O", filters=[{"id": "vlen-utf8"}], fill_value=1)
    reject("order", order="A")
    reject("order_missing", order="DROP")
    reject("fill_out_of_range", dtype="|u1", fill_value=300)
    reject("fill_bool_int", dtype="|b1", fill_value=1)
    reject("fill_fraction_int", fill_value=0.5)
    reject("fill_string_int", fill_value="NaN")
    reject("fill_list_int", fill_value=[1])
    reject("fill_uint64_max", dtype="<u8", fill_value=18446744073709551615)
    reject("fill_f4_overflow", dtype="<f4", fill_value=1e39)
    reject("fill_base64", dtype="<f8", fill_value="AAAAAAAA+H8=")
    reject("separator", dimension_separator="-")
    reject("zarr_format_3", zarr_format=3)
    reject("shape_chunks_mismatch", chunks=[2])
    reject("chunks_zero", chunks=[2, 0])
    reject("shape_negative", shape=[4, -4])
    reject("json_nan_literal", raw=b'{"zarr_format": 2, "shape": [4, 4], "chunks": [2, 2], "dtype": "<f4", '
                                   b'"compressor": null, "fill_value": NaN, "order": "C", "filters": null}')
    reject("json_duplicate_last_wins", raw=b'{"zarr_format": 2, "shape": [4, 4], "chunks": [2, 2], "dtype": "<u2", '
                                           b'"compressor": null, "fill_value": 0, "order": "C", "filters": null, '
                                           b'"order": "K"}')
    d = store("zarr2_reject_zarray_and_zgroup")
    array(d, "", arr((2, 2), "|u1"), [2, 2], write_chunks=False)
    group(d, "")
    d = store("zarr2_reject_zattrs_not_object")  # below the root, so that the message names the node
    group(d, "")
    group(d, "x", ["not", "an", "object"])  # a group's: an array's leaves that array out
    arr((2, 2), "|u1")  # the array this store had: the stores after it keep their bytes
    d = store("zarr2_reject_zgroup_format")
    write_json(d / ".zgroup", {"zarr_format": "2"})
    d = store("zarr2_reject_no_root_metadata")
    array(d, "x", arr((2, 2), "|u1"), [2, 2], write_chunks=False)

    # The zarrification convention (spec/zarrify.md conventions §2
    # <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>): every copied
    # attribute, another convention's included, goes under refkv_zarrified.zarr2 on its node.
    # `v` was itself zarrified (it carries a refkv declaration and the key), and
    # `bad` a zarr_conventions that is not an array: both are copied as they are.
    proj = {"uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f", "name": "proj:"}
    d = store("zarr2_conventions")
    group(d, "", {"zarr_conventions": [proj], "proj:code": "EPSG:4326"})
    array(d, "x", arr((2, 2), "|u1"), [2, 2], attrs={"zarr_conventions": [proj], "proj:code": "EPSG:4326"})
    group(d, "v", {"zarr_conventions": [{"uuid": "48e9ac4e-1156-4a62-955e-20467d9c2700"}],
                   "refkv_zarrified": {"profile": "tiff", "version": 1}})
    group(d, "bad", {"zarr_conventions": proj})

    # Source metadata (spec/zarrify/zarr2.md §4
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>): the
    # members of .zarray and .zgroup that
    # the Zarr v3 metadata does not reproduce (an unread member, a null fill value, a compressor's
    # members its codec does not carry), the compressors' levels, which the codecs carry,
    # integers beyond 2^53 kept exact, and -0.0 fill values, written as Zarr v3 hex fills. These
    # are written after the stores above, so that the random data of those does not change.
    d = store("zarr2_source_metadata")
    write_json(d / ".zgroup", {"zarr_format": 2, "creator": {"name": "a writer"}})
    put(d / ".zattrs", b'{"id": 18446744073709551615, "neg": -9007199254740993, "safe": 9007199254740991}')
    array(d, "null_fill", arr((4,), "<f4"), [4], fill=None)
    array(d, "neg_zero_f2", arr((4,), ">f2"), [4], fill=-0.0)
    array(d, "neg_zero_f4", arr((4,), "<f4"), [4], fill=-0.0)
    array(d, "neg_zero_f8", arr((4,), "<f8"), [4], fill=-0.0)
    array(d, "big_fill_f8", arr((4,), "<f8"), [4], fill=2**64)
    array(d, "zlib_9", arr((4,), "<u2"), [4], compressor={"id": "zlib", "level": 9})
    array(d, "zlib_no_level", arr((4,), "<u2"), [4], compressor={"id": "zlib"})
    array(d, "gzip_default", arr((4,), "<u2"), [4], compressor={"id": "gzip", "level": -1})
    array(d, "zstd_checksum", arr((4,), "<u2"), [4], compressor={"id": "zstd", "level": -3, "checksum": True})
    array(d, "extra_members", arr((4,), "<u2"), [4], compressor=blosc(),
          zarray_extra={"compressor": {**blosc(), "nthreads": 2}, "custom": {"x": 1}},
          attrs={"_ARRAY_DIMENSIONS": ["x"]})

    # Objects that are neither documents of a node nor chunks (spec/zarrify/zarr2.md §5
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>):
    # each is kept whole under refkv_source/objects/, except .zmetadata and empty objects.
    d = store("zarr2_objects")
    group(d, "")
    array(d, "a", arr((4,), "<u2"), [2])
    put(d / "a/notes.txt", b"inside an array\n")
    group(d, "a/inner")  # inside an array: not a node, its document kept as an object
    put(d / "README.md", b"# provenance\n")
    write_json(d / "sub/.zattrs", {"no": "group"})  # .zattrs without .zgroup
    array(d, "sub/b", arr((2,), "|u1"), [2])
    put(d / "OME/METADATA.ome.xml", b"<OME/>")  # not a bioformats2raw collection
    put(d / "empty.txt", b"")  # empty: no entry
    write_json(d / ".zmetadata", {"zarr_consolidated_format": 1, "metadata": {}})  # never kept

    for name, comp in (("zlib_level", {"id": "zlib", "level": 10}), ("gzip_level", {"id": "gzip", "level": "1"}),
                       ("zstd_level", {"id": "zstd", "level": 23}), ("zstd_checksum", {"id": "zstd", "checksum": 1})):
        reject(name, compressor=comp)
    d = store("zarr2_reject_objects_collision")  # a node where the other objects go
    group(d, "")
    group(d, "refkv_source")
    put(d / "README.md", b"x")

    types()
    codecs()
    left_out()

    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"{sum(1 for _ in OUT.iterdir())} stores, {total} bytes")


def exact_objects() -> None:
    """Objects whose last segment a Zarr reader takes for a node's document, escaped with
    `~` under refkv_source/objects/; empty objects, whose keys are refkv_source's source
    metadata, or refkv_source/empty.json under a root array (spec/zarrify/zarr2.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>);
    and float fill values written as integers beyond 2^53 - 1 (§4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>). Fixed
    data, no RNG."""
    d = store("zarr2_node_names")
    group(d, "")
    array(d, "a", np.arange(2, dtype="|u1"), [2])
    put(d / "zarr.json", b'{"zarr_format": 3, "node_type": "group", "attributes": {}}')
    put(d / "zarr.json~", b"one tilde")
    put(d / "g/zarr.json", b'{"zarr_format": 3, "node_type": "group"}')
    put(d / "a/zarr.json", b'{"zarr_format": 3, "node_type": "group"}')  # under an array, not a chunk
    group(d, "a/inner")  # inside an array: not a node, its .zgroup kept as an object
    put(d / "x/.zarray~~", b"two tildes")
    put(d / "x/zarr.jsonx", b"not escaped")
    put(d / "e", b"")
    put(d / "g/e2", b"")
    d = store("zarr2_root_array_empty")
    array(d, "", np.arange(2, dtype="|u1"), [2])
    put(d / "README.md", b"# a root array\n")
    put(d / "empty", b"")
    put(d / "\u00e9mpty", b"")
    d = store("zarr2_fill_float_int")
    group(d, "")
    array(d, "h", np.zeros(2, dtype="<f8"), [2], fill=9007199254740993)  # binary64: 9007199254740992
    array(d, "k", np.zeros(2, dtype="<f4"), [2], fill=-18446744073709551616)
    array(d, "s", np.zeros(2, dtype="<f8"), [2], fill=9007199254740991)  # within 2^53 - 1: not kept




def empty_chunks() -> None:
    """A root array with an empty chunk object, whose key is listed in
    refkv_source/empty.json (spec/zarrify/zarr2.md §3.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#32-chunks>, §5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>). Fixed data,
    no RNG."""
    d = store("zarr2_root_array_empty_chunk")
    array(d, "", np.array([1, 2, 3, 4], dtype="|u1"), [2], fill=7)
    put(d / "1", b"")


def root_objects() -> None:
    """A root array with other objects beside its chunk (a README, and `7`, outside the
    chunk grid), which the root keeps without a refkv_source group (spec/zarrify/zarr2.md
    §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>). Written
    byte for byte as the first, hand-made copy was: compact JSON with a newline, no RNG."""
    d = store("zarr2_root_array_objects")
    put(d / ".zarray", b'{"zarr_format":2,"shape":[4],"chunks":[4],"dtype":"<u2","order":"C",'
                       b'"compressor":null,"filters":null,"fill_value":0}\n')
    put(d / "0", np.array([1, 2, 3, 4], dtype="<u2").tobytes())
    put(d / "7", b"x\n")
    put(d / "README.md", b"notes\n")


if __name__ == "__main__":
    main()
    exact_objects()
    empty_chunks()
    root_objects()
