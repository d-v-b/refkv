"""The layout `refkv.virtualizarr` (python/src/refkv/zarrify/layouts/virtualizarr.py) against
VirtualiZarr's own parsers: over HDF5, netCDF-4, FITS and zipped Zarr v3 files, every document is
the one VirtualiZarr's `ManifestStore` serves but for the differences the module's docstring keeps,
every chunk reference is refkv's default layout's, and every array reads as VirtualiZarr's does;
a tile-compressed FITS image reads as astropy reads it; and each way its options or an input are
refused."""

import bz2
import gzip
import json
import math
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, cast
from unittest import mock

import h5py
import imagecodecs.numcodecs
import numpy as np
import pytest
import virtualizarr.parsers
import zarr
import zarr.codecs
import zarr.storage
from astropy.io import fits
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import LocalStore
from zarr.core.buffer import default_buffer_prototype
from zarr.core.metadata import ArrayV3Metadata
from zarr.core.sync import sync

import refkv.zarr
from refkv.pb import Range
from refkv.zarrify import zarrify
from refkv.zarrify.layouts import Layout, LayoutRejected, LayoutUsageError, base, chunk_coords, encode_entries
from refkv.zarrify.layouts.hierarchy import check
from refkv.zarrify.layouts.virtualizarr import VirtualiZarr

# the FITS oracle's helpers are VirtualiZarr's private ones, of 2.8.0 on: refkv's floor, 2.7.3, which
# CI's minimum-versions job installs, has neither
pytest.importorskip("virtualizarr", minversion="2.8.0")
from virtualizarr.parsers.fits import _attributes, _hdu_names

imagecodecs.numcodecs.register_codecs(verbose=False)  # VirtualiZarr's lzf arrays
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
NAME = "refkv.virtualizarr"
PHONY = re.compile(r"phony_dim_[0-9]+")
ALLOWED = frozenset({"json", "copy"})  # the check codes the module's docstring names

Json = dict[str, Any]


# ---- inputs the fixtures lack

def attributes_h5(path: Path) -> Path:
    """An HDF5 file with an attribute of every kind HDFParser reads, and the datasets whose
    `_FillValue` it encodes."""
    with h5py.File(path, "w") as f:
        a = f.attrs
        a["int"], a["big"], a["float"] = np.int32(7), np.uint64(2**64 - 1), 1.5
        a["nan"], a["inf"], a["negzero"] = np.nan, -np.inf, -0.0
        a["ints"], a["one"] = np.array([1, 2, 3], "i2"), np.array([4.5])
        a["floats"] = np.array([1.0, np.nan, np.inf])
        a["matrix"], a["one_by_one"] = np.arange(6, dtype="i4").reshape(2, 3), np.array([[9]], "i8")
        a["vlen"], a["vlen_empty"] = "text", ""
        a["fixed"], a["fixed_array"] = np.bytes_(b"abc"), np.array([b"ab", b"c"], "S2")
        a["fixed_one"], a["row"] = np.array([b"abc"], "S3"), np.array([[1, 2, 3]], "i4")  # lists to HDFParser
        a["empty"], a["flag"] = h5py.Empty("f4"), np.bool_(True)
        a["scale"], a["_NCProperties"] = "DIMENSION_SCALE", "hidden"
        t = f.create_dataset("t", data=np.arange(4, dtype="f4"), chunks=(2,), compression="gzip",
                             fillvalue=np.float32(-9))
        t.attrs["_FillValue"], t.attrs["units"] = np.float32(-9), "K"
        f.create_dataset("n", data=np.arange(3.0)).attrs["_FillValue"] = np.nan
        f.create_dataset("i", data=np.arange(3, dtype=">i4"), fillvalue=-1).attrs["_FillValue"] = np.int32(-1)
        f.create_dataset("s", data=np.array([[b"a", b"bb"], [b"c", b"d"]], "S2"), chunks=(1, 2))
        g = f.create_group("g")
        g.attrs["note"] = "sub"
        g.create_dataset("scalar", data=np.float64(2.5))
    return path


def fits_file(path: Path, hdus: list[tuple[list[tuple[str, object]], bytes]]) -> Path:
    """A FITS file of HDUs, each its cards (keyword, value) and its data's bytes."""
    out = bytearray()
    for cards, data in hdus:
        text = ""
        for keyword, value in [*cards, ("END", None)]:
            if keyword == "END":
                card = "END"
            elif isinstance(value, bool):
                card = f"{keyword:<8}= {'T' if value else 'F':>20}"
            elif isinstance(value, str):
                card = f"{keyword:<8}= '{value:<8}'"
            else:
                card = f"{keyword:<8}= {value!r:>20}"
            text += f"{card:<80}"
        out += text.ljust(-(-len(text) // 2880) * 2880).encode()
        out += data + b"\0" * (-len(data) % 2880)
    path.write_bytes(bytes(out))
    return path


Hdu = tuple[list[tuple[str, object]], bytes]  # an HDU's cards (keyword, value) and its data's bytes


def image(xtension: bool, bitpix: int, data: np.ndarray, *extra: tuple[str, object]) -> Hdu:
    head: list[tuple[str, object]] = [("XTENSION", "IMAGE")] if xtension else [("SIMPLE", True)]
    head += [("BITPIX", bitpix), ("NAXIS", data.ndim)]
    head += [(f"NAXIS{i + 1}", n) for i, n in enumerate(reversed(data.shape))]
    head += [("PCOUNT", 0), ("GCOUNT", 1)] if xtension else []
    return [*head, *extra], data.astype(data.dtype.newbyteorder(">")).tobytes()


def blank_fits(path: Path) -> Path:
    """Integer images with BLANK, one scaled by BSCALE and BZERO."""
    return fits_file(path, [
        ([("SIMPLE", True), ("BITPIX", 8), ("NAXIS", 0), ("EXTEND", True), ("ORIGIN", "test")], b""),
        image(True, 16, np.array([[1, -32768], [3, 4]], "i2"), ("BSCALE", 2.0), ("BZERO", 10.0),
              ("BLANK", -32768), ("EXTNAME", "SCALED")),
        image(True, 32, np.array([5, -1, 7], "i4"), ("BLANK", -1), ("EXTNAME", "MASKED"),
              ("DP1", "EXTVER: 1"), ("DP1", "NAXES: 2"), ("DP1", "AXIS.1: 1.5"), ("NOTREC", "a: b"),
              ("PADDED", "AB: 1")),  # padded to 8 within its quotes: no record to astropy
    ])


def zipped_zarr(path: Path) -> Path:
    """A Zarr v3 store, zipped without compression, as ZippedZarrParser reads one."""
    store = zarr.storage.ZipStore(path, mode="w", compression=zipfile.ZIP_STORED)
    g = zarr.open_group(store, mode="w", attributes={"title": "zipped"})
    a = g.create_array("a", shape=(4, 6), chunks=(2, 3), dtype="f4", dimension_names=["y", "x"],
                       attributes={"units": "m"}, fill_value=float("nan"))
    a[:] = np.arange(24).reshape(4, 6)
    b = g.create_group("sub").create_array("b", shape=(5,), chunks=(2,), dtype="i2",
                                           compressors=zarr.codecs.ZstdCodec())
    b[:] = np.arange(5)
    store.close()
    return path


GENERATED = {"attributes.h5": attributes_h5, "blank.fits": blank_fits, "store.zarr.zip": zipped_zarr}
PARSERS = {"hdf5": "HDFParser", "fits": "FITSParser", "zarr3": "ZippedZarrParser"}

# each input, and its arrays whose chunk grid (and so chunk references) VirtualiZarr's differs from
# refkv's: a dataset or HDU refkv splits into chunks within its payload limit
CASES: list[tuple[str, frozenset[str]]] = [
    ("hdf5/hdf5_netcdf4.h5", frozenset()),
    ("hdf5/hdf5_numbers.h5", frozenset({"bands"})),
    ("hdf5/hdf5_phony_dims.h5", frozenset()),
    ("hdf5/hdf5_phony_dims_tracked.h5", frozenset()),
    ("hdf5/hdf5_strings.h5", frozenset()),
    ("hdf5/hdf5_latest.h5", frozenset()),
    ("hdf5/hdf5_big_fills.h5", frozenset()),
    ("hdf5/hdf5_fill_out_of_range.h5", frozenset()),
    ("attributes.h5", frozenset()),
    ("fits/fits_primary_scaled.fits", frozenset()),
    ("fits/fits_primary_uint16.fits", frozenset()),
    ("fits/fits_primary_1d.fits", frozenset()),
    ("fits/fits_primary_cube.fits", frozenset()),
    ("fits/fits_mef_naxis0.fits", frozenset()),
    ("fits/fits_mef_same_name.fits", frozenset()),
    ("fits/fits_mef_unnamed.fits", frozenset()),
    ("fits/fits_mef_sci_err_dq.fits", frozenset()),
    ("fits/fits_many_hdus.fits", frozenset()),
    ("fits/fits_asciitable.fits", frozenset()),
    ("fits/fits_header_cards.fits", frozenset()),
    ("fits/fits_header_huge.fits", frozenset()),
    ("fits/fits_chunk_rows.fits", frozenset({"PRIMARY"})),
    ("blank.fits", frozenset()),
    ("store.zarr.zip", frozenset()),
]


def _input(name: str, tmp_path: Path) -> str:
    return str(GENERATED[name](tmp_path / name) if name in GENERATED else FIXTURES / name)


# ---- VirtualiZarr's presentation

def _manifest_store(parser: str, path: str):
    url = Path(path).resolve().as_uri()
    # HDFParser joins a nested group's path with pathlib.Path, which on Windows gives `g\h`, a group h5py
    # does not find; a POSIX path is what it means
    with mock.patch("virtualizarr.parsers.hdf.hdf.Path", PurePosixPath):
        return getattr(virtualizarr.parsers, parser)()(url, ObjectStoreRegistry({"file:///": LocalStore()}))


References = dict[str, dict[tuple[int, ...], tuple[int, int]]]  # each array's chunks' (offset, length)


def virtualizarr_s(parser: str, path: str) -> tuple[dict[str, Json], References, Any]:
    """The documents a parser's ManifestStore serves, by key, each array's chunk references by
    coordinates as (offset, length), and the store."""
    store = _manifest_store(parser, path)
    keys, chunks = ["zarr.json"], {}

    def walk(group, prefix: str) -> None:
        for name, array in group.arrays.items():
            keys.append(f"{prefix}{name}/zarr.json")
            chunks[prefix + name] = {tuple(int(i) for i in k.split(".")) if k else (): (e["offset"], e["length"])
                                     for k, e in array.manifest.dict().items()}
        for name, sub in group.groups.items():
            keys.append(f"{prefix}{name}/zarr.json")
            walk(sub, f"{prefix}{name}/")

    walk(store._group, "")
    docs = {k: json.loads(sync(store.get(k, default_buffer_prototype())).to_bytes()) for k in keys}
    return docs, chunks, store


def _arrays(group) -> dict[str, Any]:
    return {p: a for p, a in group.members(max_depth=None) if isinstance(a, zarr.Array)}


def _chunks(out, doc: Json, path: str) -> dict[tuple[int, ...], Any]:
    """An array's chunk values in an output, by coordinates (an array has no nodes below it)."""
    return {tuple(chunk_coords(k[len(path) + 1:], doc)): v for k, v in out.items()
            if k.startswith(f"{path}/") and not k.endswith("/zarr.json")}


def _base_chunks(b) -> list[list[str]]:
    """Each base array's chunk values (refkv's default layout's references), sorted, the mirror's
    left out."""
    return [sorted(map(repr, b.array(p)[1].values())) for p in b.nodes() if b.node_type(p) == "array"]


def _text(value: object) -> object:
    """A document's non-finite floats as the text the `text` option writes."""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, list):
        return [_text(v) for v in value]
    if isinstance(value, dict):
        return {k: _text(v) for k, v in value.items()}
    return value


def _kept(doc: Json, profile: str, names: list[str] | None = None) -> Json:
    """A document with the differences the layout keeps made alike: the zlib level, a zipped
    store's chunk key encoding, an empty storage_transformers, and, given the base's dimension
    `names`, those of the axes VirtualiZarr names `phony_dim_<axis>`, which netCDF-4 names."""
    doc = dict(doc)
    if doc.get("storage_transformers") == []:
        del doc["storage_transformers"]
    if names is not None and doc.get("dimension_names") is not None:
        doc["dimension_names"] = [names[i] if PHONY.fullmatch(str(n)) else n
                                  for i, n in enumerate(doc["dimension_names"])]
    if "codecs" in doc:
        doc["codecs"] = [{**c, "configuration": {}} if c.get("name") == "numcodecs.zlib" else c for c in doc["codecs"]]
    if profile == "zarr3":
        doc.pop("chunk_key_encoding", None)
    return doc


@pytest.mark.parametrize("nonfinite", ["token", "text"])
@pytest.mark.parametrize(("name", "regridded"), CASES, ids=[c for c, _ in CASES])
def test_the_layout_presents_a_file_as_virtualizarr_s_parser_does(tmp_path, name, regridded, nonfinite):
    """Every document is VirtualiZarr's but for what the layout keeps of refkv's (above, and a
    variable-length string's or a regridded array's chunk grid, and a value-less FITS card's
    `null`), every chunk reference is refkv's default layout's, and every array reads as
    VirtualiZarr's does; `check` finds nothing but the non-finite tokens zarr-python writes."""
    path = _input(name, tmp_path)
    b = base(path)
    plugin = VirtualiZarr()
    out = plugin.write(b, plugin.options({"nonfinite": nonfinite}))
    problems = check(b, out)
    assert [p for p in problems if p.code not in ALLOWED] == []
    assert nonfinite == "token" or not [p for p in problems if p.code == "json"]

    theirs, references, manifest_store = virtualizarr_s(PARSERS[b.profile], path)
    ours = {k: json.loads(v) for k, v in out.items() if k.rpartition("/")[2] == "zarr.json" and isinstance(v, bytes)}
    defaults = _base_chunks(b)
    assert sorted(ours) == sorted(theirs)
    for key, doc in ours.items():
        node = key.removesuffix("zarr.json").rstrip("/")
        names = b.document(key).get("dimension_names") if b.profile == "hdf5" and key in b else None
        mine, expected = _kept(doc, b.profile), _kept(theirs[key], b.profile, names)
        if nonfinite == "text":
            expected = cast("Json", _text(expected))
        if doc.get("data_type") == "string" or node in regridded:
            del mine["chunk_grid"], expected["chunk_grid"]
        if name == "fits/fits_header_cards.fits" and node == "PRIMARY":
            assert (mine["attributes"].pop("NOVALUE"), expected["attributes"].pop("NOVALUE")) == (None, "None")
        assert json.dumps(mine, sort_keys=True) == json.dumps(expected, sort_keys=True), key
        if doc["node_type"] != "array":
            continue

        chunks = _chunks(out, doc, node)
        assert sorted(map(repr, chunks.values())) in defaults, key
        if doc.get("data_type") != "string" and node not in regridded:
            assert {c: (v.offset, v.length) for c, v in chunks.items() if isinstance(v, Range)} == \
                references[node], key

    store = zarrify(path, layout=Layout(NAME, {"nonfinite": nonfinite}))
    mine_arrays = _arrays(zarr.open_group(refkv.zarr.store(store), mode="r"))
    for path_, array in _arrays(zarr.open_group(manifest_store, mode="r", zarr_format=3)).items():
        x, y = np.asarray(array[...]), np.asarray(mine_arrays[path_][...])
        assert (x.dtype, x.shape) == (y.dtype, y.shape), path_
        assert x.tolist() == y.tolist() if x.dtype.kind in "OT" else x.tobytes() == y.tobytes(), path_


def zero_scaled_fits(path: Path) -> Path:
    """A tile-compressed image whose table has BSCALE = 1 and BZERO = 0, which astropy's image
    header leaves out."""
    hdu = fits.CompImageHDU(np.arange(12, dtype="i2").reshape(3, 4), compression_type="RICE_1")
    hdu.header["BSCALE"], hdu.header["BZERO"] = 1.0, 0.0
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(path)
    return path


COMPRESSED = sorted(p.name for p in (FIXTURES / "fits").glob("fits_comp_*.fits")
                    if p.name not in ("fits_comp_hand_no_ztile.fits", "fits_comp_hand_absent_tile.fits"))


@pytest.mark.parametrize("name", [*COMPRESSED, "zero_scaled.fits"])
def test_a_tile_compressed_fits_image_reads_as_astropy_reads_it(tmp_path, name):
    """FITSParser points a compressed HDU's table bytes at an uncompressed image; the layout
    presents refkv's decoded image under FITSParser's name, with the image header astropy
    reconstructs as its attributes and its axes named as FITSParser names them, and astropy's
    values (BSCALE and BZERO applied, in float64)."""
    path = zero_scaled_fits(tmp_path / name) if name == "zero_scaled.fits" else FIXTURES / "fits" / name
    store = zarrify(str(path), layout=Layout(NAME))
    group = zarr.open_group(refkv.zarr.store(store), mode="r")
    with fits.open(path) as hdus:
        names = _hdu_names(hdus)
        for hdu_name, hdu in zip(names[1:], hdus[1:], strict=True):
            array = group[hdu_name]
            assert isinstance(array, zarr.Array)
            dims = array.metadata.dimension_names if isinstance(array.metadata, ArrayV3Metadata) else None
            assert dims is not None
            str(hdu.header)
            assert dict(array.attrs) == {k: v for k, v in _attributes(hdu.header).items() if k != "BLANK"}, hdu_name
            assert len(dims) == hdu.header["NAXIS"]
            assert all(str(n).startswith(f"{hdu_name}_") for n in dims)
            data, scaled = np.asarray(array[...]), "BSCALE" in hdu.header or "BZERO" in hdu.header
            assert data.dtype == (np.dtype("f8") if scaled else hdu.data.dtype.newbyteorder("="))
            # refkv's float64 dequantization may differ from astropy's in the last bit (in its default layout too)
            np.testing.assert_allclose(data, hdu.data, rtol=0, atol=1e-12)


@pytest.mark.parametrize("name", ["fits_asciitable.fits", "fits_mef_sci_err_dq.fits"])
@pytest.mark.parametrize("compression", ["gz", "bz2"])
def test_a_fits_file_compressed_whole_reads_as_the_file_does(tmp_path, name, compression):
    """FITSParser does not read a gzip or bzip2 file; the layout presents it as the file itself,
    each array behind the base's decompressing codecs, with the file's values."""
    plain = FIXTURES / "fits" / name
    packed = tmp_path / f"{name}.{compression}"
    opener = gzip.open if compression == "gz" else bz2.open
    with opener(packed, "wb") as f:
        f.write(plain.read_bytes())
    stores = [zarrify(str(p), layout=Layout(NAME)) for p in (plain, packed)]
    expected, found = (_arrays(zarr.open_group(refkv.zarr.store(s), mode="r")) for s in stores)
    assert sorted(found) == sorted(expected)
    for path, array in expected.items():
        assert dict(found[path].attrs) == dict(array.attrs), path
        assert np.asarray(found[path][...]).tobytes() == np.asarray(array[...]).tobytes(), path


def test_a_binary_table_is_refkv_s_group_under_its_name():
    """FITSParser refuses a binary table; the layout keeps refkv's group for it, under the HDU's
    name, so that the root reads as FITSParser's with the table skipped."""
    path = str(FIXTURES / "fits/fits_bintable.fits")
    b = base(path)
    out = VirtualiZarr().write(b, {"nonfinite": "token"})
    table = {k for k in out if k.startswith("TAB/")}
    assert table == {f"TAB/{k[2:]}" for k in b.entries("1/", declarations=False, mirror=False)}
    skipped = virtualizarr.parsers.FITSParser(skip_variables=["TAB"])(
        Path(path).resolve().as_uri(), ObjectStoreRegistry({"file:///": LocalStore()}))
    root = out["zarr.json"]
    assert isinstance(root, bytes)
    assert json.loads(root)["attributes"] == skipped._group.metadata.attributes
    assert check(b, out) == []


OUTPUT = """
import sys
from refkv.zarrify.layouts import base, encode_entries
from refkv.zarrify.layouts.virtualizarr import VirtualiZarr
sys.stdout.buffer.write(encode_entries(VirtualiZarr().write(base(sys.argv[1]), {"nonfinite": "token"})))
"""


@pytest.mark.parametrize("name", ["hdf5/hdf5_netcdf4.h5", "fits/fits_mef_sci_err_dq.fits"])
def test_the_layout_is_deterministic(name):
    """Another process, with another hash seed, writes the same output."""
    path = str(FIXTURES / name)
    env = {**os.environ, "PYTHONHASHSEED": "12345"}
    run = subprocess.run([sys.executable, "-c", OUTPUT, path], env=env, capture_output=True, check=True)
    assert run.stdout == encode_entries(VirtualiZarr().write(base(path), {"nonfinite": "token"}))


# ---- refusals

def test_an_unknown_option_is_a_usage_error():
    with pytest.raises(LayoutUsageError, match="unknown options"):
        zarrify(str(FIXTURES / "hdf5/hdf5_netcdf4.h5"), layout=Layout(NAME, {"mirror": True}))


def test_a_nonfinite_option_not_token_or_text_is_a_usage_error():
    with pytest.raises(LayoutUsageError, match="nonfinite is token or text"):
        zarrify(str(FIXTURES / "hdf5/hdf5_netcdf4.h5"), layout=Layout(NAME, {"nonfinite": "null"}))


def test_a_format_virtualizarr_does_not_parse_is_rejected():
    with pytest.raises(LayoutRejected):
        zarrify(str(FIXTURES / "tiff/strips_uint8.tif"), layout=Layout(NAME))


def test_an_image_with_bscale_zero_is_rejected(tmp_path):
    path = fits_file(tmp_path / "zero.fits", [image(False, 16, np.array([1, 2], "i2"), ("BSCALE", 0.0))])
    with pytest.raises(LayoutRejected, match="BSCALE = 0"):
        zarrify(str(path), layout=Layout(NAME))
