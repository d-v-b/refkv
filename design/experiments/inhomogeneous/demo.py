# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "refkv[virtualizarr]>=0.1.0",
#     "obstore>=0.11.1",
#     "virtualizarr>=2.7.3",
#     "zarr>=3.4.0",
#     "cast-value-rs>=0.4.2",
#     "xarray>=2026.9.0",
#     "h5py>=3.16.0",
#     "h5netcdf>=1.8.1",
#     "matplotlib>=3.10",
# ]
# ///
"""The many-file cases of zarr-developers/zarr-specs#399 ("Varying codec parameters
per-chunk"), and a sketch of one way to express them as a single Zarr array.

Each case writes a few small netCDF4 files, each valid and consistent on its own,
holding one smooth field `v(time, x)` that continues across the files. Every file is
virtualized with VirtualiZarr, which parses all of them without trouble. The question
is the next step, one logical array along time:

- "one Zarr array today": `xr.concat` of the virtual datasets, as
  `open_virtual_mfdataset` does. A Zarr array has one codec chain, one dtype, one
  regular chunk grid and one set of attributes, so VirtualiZarr refuses combinations
  that would need more, and says what is missing. That refusal protects users; it is
  the gap the issue describes.
- "switch sketch": the same per-file manifests, combined by py into one array
  whose chunks each select their own codec chain (design 2.1 of the issue), saved as a
  vzip archive with refkv and read back through Zarr.

Both are compared with the files read one by one:

- the reference is each file decoded by xarray + h5netcdf (HDF5's own filters, CF
  scaling and _FillValue), concatenated;
- "identical" means equal bit for bit (NaN equal to NaN); otherwise the largest
  absolute difference is reported;
- figures/ gets one plot per case: the files and the combined arrays along time at
  x = 0, with the files' boundaries, and the sketch's whole field over (time, x).

    uv run demo.py           # the six cases, and figures/
    uv run demo.py scaling   # how switch scales when every chunk is encoded differently

The dependencies are declared inline (PEP 723), so `uv run` builds an environment for
this script alone; py, next to it, is the only other file it needs.

Cases (the use cases of the issue):

1. codec eras: the compression changes partway through the record (GOES-16)
2. rectilinear: files of different lengths along time (monthly files)
3. ragged inner chunks: each file is chunked internally and its last chunk is short
4. per-file scale_factor / add_offset (BOM BARPA)
5. varying dtype: the same variable stored as int16 in some files and float32 in others
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import textwrap
import time
import warnings
import zlib
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Self

import h5py
import matplotlib
import numpy as np
import virtualizarr as vz
import xarray as xr
import zarr
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import LocalStore
from virtualizarr.parsers import HDFParser
from zarr.abc.codec import ArrayArrayCodec, ArrayBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.core.metadata.v3 import parse_codecs
from zarr.registry import get_pipeline_class, register_codec

import refkv
import refkv.zarr
from refkv.pb import Concat, Range, Source

matplotlib.use("Agg")  # files only, no display
import matplotlib.pyplot as plt  # noqa: E402 - after choosing the backend

warnings.filterwarnings("ignore")
HERE = Path(__file__).parent
NX = 6  # a small spatial axis, so every case runs in a second


# --------------------------------------------------------------------------------------
# The switch sketch.
#
# Combining inhomogeneous files into one Zarr array with refkv, from independent codecs.
#
# Each file virtualizes on its own (VirtualiZarr does this well). What no single Zarr
# array can express is that the files' chunks decode differently. Here every way a
# chunk can differ is its own, independent codec:
#
# | what varies per file             | codec                                   |
# |----------------------------------|-----------------------------------------|
# | compression, shuffle             | the file's own codecs (`numcodecs.*`)   |
# | stored dtype                     | `cast_value` (zarr-extensions)          |
# | CF `scale_factor` / `add_offset` | `scale_offset` (zarr-extensions)        |
# | stored chunk shape (edge chunks) | `crop` (new, below)                     |
#
# and one codec, `switch`, chooses per chunk which composition of them applies:
#
# - `switch` is an array-to-bytes codec,
#   `{"cases": {key: {"codecs": chain, "parameters": [data_type, ...]}}}`. Each case is an
#   ordinary Zarr codec chain that may leave numbers open (`{"parameter": k}`), keyed by
#   the first 8 bytes of the SHA-256 of its canonical JSON. A chunk's bytes start with its
#   case's key and that case's parameters; `switch` fills them in and decodes the rest
#   with that chain, as `sharding_indexed` decodes its inner chunks with its inner chain.
# - Keys name cases by their content: arrays that use the same chain use the same key,
#   so concatenating arrays merges their case tables by union, and no chunk is rewritten
#   (`concatenate`).
# - `crop` is an array-to-array codec, `{"shape": [...]}`: the stored chunk's shape. It
#   decodes by cropping to the chunk's cell (HDF5 stores edge chunks whole) and encodes
#   by padding with the fill value.
#
# refkv's reference values carry each chunk's header: a chunk is
# `Concat(Range(data=<key + parameters>), Range(source=<file>, offset, length))`, literal
# bytes in the archive, then the file's own bytes read in place. No store injects
# anything at read time and no codec does I/O.
#
# Ragged and month-length files use the zarr-extensions `rectilinear` chunk grid, which
# zarr-python 3.4 reads with `zarr.config.set({"array.rectilinear_chunks": True})`.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CropCodec(ArrayArrayCodec):
    """`crop`: the stored chunk has `shape`, at least the chunk's cell in every
    dimension; decoding keeps its leading corner, the cell."""

    shape: tuple[int, ...] = ()
    is_fixed_size = True

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(shape=tuple(data["configuration"]["shape"]))

    def to_dict(self) -> dict:
        return {"name": "crop", "configuration": {"shape": list(self.shape)}}

    def resolve_metadata(self, chunk_spec: ArraySpec) -> ArraySpec:
        if len(self.shape) != len(chunk_spec.shape) or any(s < c for s, c in zip(self.shape, chunk_spec.shape)):
            raise ValueError(f"crop: stored shape {self.shape} is smaller than the chunk {chunk_spec.shape}")
        return replace(chunk_spec, shape=self.shape)

    async def _decode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> NDBuffer:
        return chunk_array[tuple(slice(0, n) for n in chunk_spec.shape)]

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> NDBuffer:
        a = chunk_array.as_numpy_array()
        out = np.full(self.shape, chunk_spec.fill_value, dtype=a.dtype)
        out[tuple(slice(0, n) for n in a.shape)] = a
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(out)

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        return input_byte_length // int(np.prod(chunk_spec.shape)) * int(np.prod(self.shape))


register_codec("crop", CropCodec)


KEY_BYTES = 8  # a case's key: the first 8 bytes of the SHA-256 of its canonical JSON


def canonical(case: dict) -> bytes:
    """A case's canonical JSON: sorted keys, no whitespace, UTF-8. (A standard would
    name RFC 8785, which also fixes how numbers are written.)"""
    return json.dumps(case, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def case_key(case: dict) -> bytes:
    return hashlib.sha256(canonical(case)).digest()[:KEY_BYTES]


@dataclass(frozen=True)
class SwitchCodec(ArrayBytesCodec):
    """`switch`: `{"cases": {key: {"codecs": chain, "parameters": [data_type, ...]}}}`.

    A case is a codec chain that may leave values open: `{"parameter": k}` anywhere in a
    codec's configuration stands for the chunk's k-th parameter. A case's key is the
    hex of the first 8 bytes of the SHA-256 of its canonical JSON, so it names the case
    by its content, not its position: arrays that share a case share its key, and two
    case tables merge by union. A chunk is the 8-byte key, then the case's parameters
    (each little-endian in its declared data type), then the bytes the completed chain
    decodes.
    """

    cases: tuple = ()  # (key hex, codecs as JSON, parameter dtypes)
    is_fixed_size = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_key", {k: (codecs, params) for k, codecs, params in self.cases})

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        cases = []
        for key, case in data["configuration"]["cases"].items():
            if case_key(case).hex() != key:
                raise ValueError(f"switch: case {key} is not the key of its contents ({case_key(case).hex()})")
            cases.append((key, json.dumps(case["codecs"]), tuple(case.get("parameters", ()))))
        return cls(cases=tuple(cases))

    def to_dict(self) -> dict:
        return {"name": "switch", "configuration": {"cases": {
            key: {"codecs": json.loads(codecs), "parameters": list(params)} for key, codecs, params in self.cases}}}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        raw = chunk_bytes.to_bytes()
        key, pos = raw[:KEY_BYTES].hex(), KEY_BYTES
        try:
            codecs, params = self._by_key[key]
        except KeyError:
            raise ValueError(f"switch: the chunk names case {key}, which the array does not list") from None
        values = []
        for dtype in params:
            dt = np.dtype(dtype).newbyteorder("<")
            values.append(np.frombuffer(raw, dtype=dt, count=1, offset=pos)[0].item())
            pos += dt.itemsize
        pipeline = get_pipeline_class().from_codecs(_completed(codecs, tuple(values))).evolve_from_array_spec(chunk_spec)
        (out,) = await pipeline.decode([(chunk_spec.prototype.buffer.from_bytes(raw[pos:]), chunk_spec)])
        return out

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        raise NotImplementedError("this sketch decodes virtual chunks only; a writer would choose a case")

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


def _json_number(v: float | int):
    """A parameter as JSON, as Zarr writes fill values: NaN and the infinities as strings."""
    if isinstance(v, float) and not np.isfinite(v):
        return "NaN" if np.isnan(v) else ("Infinity" if v > 0 else "-Infinity")
    return v


@lru_cache(maxsize=4096)
def _completed(codecs: str, values: tuple) -> tuple:
    """A case's codecs with its open values filled in, parsed (cached: chunks that
    share a case and its numbers share one parse)."""

    def fill(node):
        if isinstance(node, dict):
            if set(node) == {"parameter"}:
                return _json_number(values[node["parameter"]])
            return {k: fill(v) for k, v in node.items()}
        if isinstance(node, list):
            return [fill(v) for v in node]
        return node

    return tuple(parse_codecs(fill(json.loads(codecs))))


register_codec("switch", SwitchCodec)


def chain(ma, attrs, out_dtype: np.dtype) -> tuple[list[dict], list[str], list[float]]:
    """How one file's chunks decode to the combined array's dtype: a case (the
    independent transforms its file needs, then the file's own codecs, with the numbers
    that vary between files left open) and the values of those numbers."""
    m = ma.metadata
    stored = m.data_type.to_native_dtype()
    codecs: list[dict] = []
    params: list[str] = []
    values: list[float] = []
    if "scale_factor" in attrs or "add_offset" in attrs:  # CF: physical = packed * scale_factor + add_offset
        codecs.append({"name": "scale_offset", "configuration": {"offset": {"parameter": 0}, "scale": {"parameter": 1}}})
        params += ["float64", "float64"]
        values += [float(attrs.get("add_offset", 0)), 1 / float(attrs.get("scale_factor", 1))]
    if stored != out_dtype:
        config = {"data_type": stored.name}
        if stored.kind in "iu":
            # the array's fill value, NaN, has to round-trip through the integer type
            # (cast_value, "data_type"): CF's _FillValue is the integer that stands for it
            fill = attrs.get("_FillValue")
            if fill is None:
                raise ValueError(f"an {stored} file without _FillValue: no integer can stand for NaN")
            config["scalar_map"] = {"encode": [["NaN", int(fill)]], "decode": [[int(fill), "NaN"]]}
        codecs.append({"name": "cast_value", "configuration": config})
    codecs.append({"name": "crop", "configuration": {"shape": list(m.chunks)}})
    return codecs + [c.to_dict() for c in m.codecs], params, values


def header(case: dict, values: list[float]) -> bytes:
    """A chunk's switch header: its case's key, then its parameters."""
    return case_key(case) + b"".join(np.array([v], dtype=np.dtype(d).newbyteorder("<")).tobytes()
                                    for d, v in zip(case["parameters"], values))


def lengths(n: int, c: int) -> list[int]:
    return [c] * (n // c) + ([n % c] if n % c else [])


def combine(vdss: list[xr.Dataset], name: str = "v", dim: str = "time") -> refkv.ReferenceStore:
    """One reference store holding `name` concatenated along `dim` (axis 0) across the
    files' virtual datasets, with the concatenated coordinates."""
    return refkv.ReferenceStore(*build(vdss, name, dim))


def build(vdss: list[xr.Dataset], name: str = "v", dim: str = "time") -> tuple[dict, list[Source]]:
    """`combine`'s mapping and sources."""
    scaled = any("scale_factor" in v[name].attrs or "add_offset" in v[name].attrs for v in vdss)
    out_dtype = np.dtype(np.result_type(*[np.float32 if scaled else v[name].data.metadata.data_type.to_native_dtype()
                                          for v in vdss]))
    cases: dict[str, dict] = {}
    sources, source_of, mapping = [], {}, {}
    time_chunks: list[int] = []
    other = None
    for vds in vdss:
        ma = vds[name].data
        codecs, params, values = chain(ma, vds[name].attrs, out_dtype)
        case = {"codecs": codecs, "parameters": params}
        cases[case_key(case).hex()] = case
        head = header(case, values)
        c0, *crest = ma.metadata.chunks
        rest = [lengths(n, c) for n, c in zip(ma.shape[1:], crest)]
        if other is None:
            other = rest
        elif rest != other:
            raise ValueError("the files are chunked differently along the other axes")
        base = len(time_chunks)
        time_chunks += lengths(ma.shape[0], c0)
        for idx, e in ma.manifest.dict().items():
            i, *js = (int(x) for x in idx.split("."))
            s = source_of.setdefault(e["path"], len(source_of))
            if s == len(sources):
                sources.append(Source(url=e["path"]))
            mapping[f"{name}/c/" + "/".join(map(str, (base + i, *js)))] = Concat(
                (Range(data=head), Range(source=s, offset=e["offset"], length=e["length"])))
    mapping["zarr.json"] = json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {}}).encode()
    mapping[f"{name}/zarr.json"] = json.dumps({
        "zarr_format": 3, "node_type": "array",
        "shape": [sum(time_chunks), *map(sum, other)], "data_type": out_dtype.name,
        "chunk_grid": {"name": "rectilinear", "configuration": {"kind": "inline", "chunk_shapes": [time_chunks, *other]}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": "NaN", "codecs": [{"name": "switch", "configuration": {"cases": cases}}],
        "dimension_names": list(vdss[0][name].dims), "attributes": {},
    }).encode()
    for coord in vdss[0][name].dims:  # the coordinates, small: inline bytes
        values = np.concatenate([v[coord].values for v in vdss]) if coord == dim else vdss[0][coord].values
        attrs = {}
        if values.dtype.kind == "M":  # decoded CF times: back to numbers with their units
            values = ((values - np.datetime64("2000-01-01", "ns")) // np.timedelta64(1, "D")).astype("int64")
            attrs = {"units": "days since 2000-01-01"}
        values = np.ascontiguousarray(values).astype(values.dtype.newbyteorder("<"))
        mapping[f"{coord}/zarr.json"] = json.dumps({
            "zarr_format": 3, "node_type": "array", "shape": [len(values)], "data_type": values.dtype.name,
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [len(values)]}},
            "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
            "fill_value": 0, "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
            "dimension_names": [coord], "attributes": attrs,
        }).encode()
        mapping[f"{coord}/c/0"] = values.tobytes()
    return mapping, sources


def concatenate(a: tuple[dict, list[Source]], b: tuple[dict, list[Source]], name: str = "v",
                dim: str = "time") -> tuple[dict, list[Source]]:
    """`b` appended to `a` along `dim` (axis 0), as manifest operations do: the case
    tables merge by union of their keys, `b`'s chunks move along the axis and their
    references renumber the sources they name. Every chunk's header (its case key and
    parameters) is copied as it is."""
    (ma, sa), (mb, sb) = a, b
    meta_a, meta_b = json.loads(ma[f"{name}/zarr.json"]), json.loads(mb[f"{name}/zarr.json"])
    grid_a = meta_a["chunk_grid"]["configuration"]["chunk_shapes"]
    grid_b = meta_b["chunk_grid"]["configuration"]["chunk_shapes"]
    if meta_a["data_type"] != meta_b["data_type"] or grid_a[1:] != grid_b[1:]:
        raise ValueError("the arrays differ in data type or in their chunks along the other axes")
    cases = dict(meta_a["codecs"][0]["configuration"]["cases"])
    for key, case in meta_b["codecs"][0]["configuration"]["cases"].items():
        if cases.setdefault(key, case) != case:
            raise ValueError(f"case {key} names two different chains")  # a key collision
    shift, prefix = len(grid_a[0]), f"{name}/c/"
    mapping = {k: v for k, v in ma.items() if k.startswith(prefix)}
    for k, ref in mb.items():
        if k.startswith(prefix):
            i, *rest = k[len(prefix):].split("/")
            mapping[prefix + "/".join([str(int(i) + shift), *rest])] = Concat(tuple(
                p if p.data is not None else Range(source=p.source + len(sa), offset=p.offset, length=p.length, crc32c=p.crc32c)
                for p in ref.parts))
    meta = dict(meta_a, shape=[meta_a["shape"][0] + meta_b["shape"][0], *meta_a["shape"][1:]])
    meta["chunk_grid"] = {"name": "rectilinear", "configuration": {"kind": "inline",
                                                                   "chunk_shapes": [grid_a[0] + grid_b[0], *grid_a[1:]]}}
    meta["codecs"] = [{"name": "switch", "configuration": {"cases": cases}}]
    mapping["zarr.json"] = ma["zarr.json"]
    mapping[f"{name}/zarr.json"] = json.dumps(meta).encode()
    for coord in meta_a["dimension_names"]:
        cm = json.loads(ma[f"{coord}/zarr.json"])
        if coord == dim:
            dt = np.dtype(cm["data_type"]).newbyteorder("<")
            values = np.concatenate([np.frombuffer(ma[f"{coord}/c/0"], dt), np.frombuffer(mb[f"{coord}/c/0"], dt)])
            cm = dict(cm, shape=[len(values)], chunk_grid={"name": "regular", "configuration": {"chunk_shape": [len(values)]}})
            mapping[f"{coord}/c/0"] = values.tobytes()
        else:
            mapping[f"{coord}/c/0"] = ma[f"{coord}/c/0"]
        mapping[f"{coord}/zarr.json"] = json.dumps(cm).encode()
    return mapping, [*sa, *sb]


def read(store, name: str = "v") -> np.ndarray:
    with zarr.config.set({"array.rectilinear_chunks": True}):
        return xr.open_zarr(refkv.zarr.store(store), consolidated=False)[name].values


# --------------------------------------------------------------------------------------
# The demo: the files, the two ways to combine them, the checks and the plots.
# --------------------------------------------------------------------------------------


def smooth(t: np.ndarray, base: float = 285.0, amp: float = 4.0) -> np.ndarray:
    """One smooth field over (time, x), continuous across files: a misdecoded file
    shows as a jump at its boundary."""
    x = np.arange(NX)
    return base + amp * np.sin(2 * np.pi * t[:, None] / 37.0) + 0.6 * np.cos(2 * np.pi * x[None, :] / NX)


def write(path: Path, data: np.ndarray, *, t0: int, chunks=None, compression=None,
          compression_opts=None, shuffle=False, attrs=None) -> None:
    """One netCDF4 file: a variable `v(time, x)`, its `time` coordinate starting at t0."""
    with h5py.File(path, "w") as f:
        nt = data.shape[0]
        t = f.create_dataset("time", data=np.arange(t0, t0 + nt, dtype="int64"))
        t.make_scale("time")
        t.attrs["units"] = "days since 2000-01-01"
        x = f.create_dataset("x", data=np.arange(NX, dtype="int64"))
        x.make_scale("x")
        v = f.create_dataset("v", data=data, chunks=chunks, compression=compression,
                             compression_opts=compression_opts, shuffle=shuffle)
        v.dims[0].attach_scale(t)
        v.dims[1].attach_scale(x)
        for k, val in (attrs or {}).items():
            v.attrs[k] = val


def virtualize(paths: list[Path], root: Path) -> list[xr.Dataset]:
    """Each file as a virtual dataset (only its metadata is read)."""
    registry = ObjectStoreRegistry({f"file://{root}": LocalStore(prefix=root)})
    return [vz.open_virtual_dataset(f"file://{p}", registry=registry, parser=HDFParser(),
                                    loadable_variables=["time", "x"]) for p in paths]


def truth(paths: list[Path]) -> np.ndarray:
    """What a user expects: every file decoded on its own, then stacked."""
    return np.concatenate([xr.open_dataset(p, engine="h5netcdf")["v"].values for p in paths])


@dataclass
class Result:
    title: str
    files: list[str]
    boundaries: list[int]  # the time index where each file starts
    want: np.ndarray
    vz: np.ndarray | None = None
    vz_note: str = ""
    rk: np.ndarray | None = None
    rk_note: str = ""
    extra: dict = field(default_factory=dict)


def compare(got: np.ndarray, want: np.ndarray) -> str:
    if got.shape != want.shape:
        return f"shape {got.shape}, not {want.shape}"
    if np.array_equal(got, want, equal_nan=True):
        return "identical to the files (bit for bit)"
    d = np.abs(got.astype("float64") - want.astype("float64"))
    bad = int(np.sum(~np.isclose(got, want, equal_nan=True, rtol=0, atol=1e-3)))
    return (f"max |difference| {np.nanmax(d):.3g}; {bad} of {want.size} values off by more than 0.001"
            if bad else f"max |difference| {np.nanmax(d):.3g} (float32 rounding)")


def virtualizarr_result(r: Result, vdss: list[xr.Dataset]) -> None:
    """One Zarr array today: concatenate the virtual datasets (xarray's default
    combine_attrs="override", as open_virtual_mfdataset uses), then read through Zarr."""
    try:
        combined = xr.concat(vdss, dim="time", coords="minimal", compat="override", combine_attrs="override")
    except Exception as e:  # what one Zarr array can't express, as VirtualiZarr reports it
        r.vz_note = f"not one array: {type(e).__name__}: {str(e).splitlines()[0][:160]}"
        return
    store = refkv.ReferenceStore.from_virtualizarr(combined)
    r.vz = xr.open_zarr(refkv.zarr.store(store), consolidated=False)["v"].values
    r.vz_note = ("combined, with the first file's attributes for every file (combine_attrs='override'; "
                 "'no_conflicts' refuses instead): " + compare(r.vz, r.want))


def refkv_result(r: Result, vdss: list[xr.Dataset], root: Path) -> None:
    """refkv: one array from the same per-file manifests (the switch sketch above), saved as a vzip
    archive, reopened, and read through Zarr."""
    store = combine(vdss)
    archive = root / "combined.vzip"
    store.save(str(archive))
    r.rk = read(refkv.open(str(archive)))
    n = len(json.loads(store.get("v/zarr.json"))["codecs"][0]["configuration"]["cases"])
    data = sum(p.stat().st_size for p in root.glob("*.nc"))
    r.rk_note = f"one array, {n} switch case{'s' * (n != 1)}: " + compare(r.rk, r.want)
    r.extra["archive"] = (f"the archive is {archive.stat().st_size:,} bytes; the chunks stay in "
                          f"the {len(store.sources)} files ({data:,} bytes), read in place")


def run(title: str, make) -> Result:
    print(f"\n{'=' * 78}\n{title}\n{'-' * 78}")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        paths = make(root)
        files, starts, t = [], [], 0
        for p in paths:
            with h5py.File(p) as f:
                v = f["v"]
                filters = [s for s in (v.compression and f"{v.compression}", v.shuffle and "shuffle") if s]
                cf = ""
                if "scale_factor" in v.attrs:
                    cf = f"\n×{float(v.attrs['scale_factor']):g} +{float(v.attrs['add_offset']):g}"
                label = f"{v.dtype}\n" + ("+".join(filters) or "uncompressed") + cf
                print(f"  {p.name}: shape={v.shape} chunks={v.chunks} {label.replace(chr(10), ' ')}")
                files.append(label)
                starts.append(t)
                t += v.shape[0]
        r = Result(title, files, starts, truth(paths))
        vdss = virtualize(paths, root)  # each file alone virtualizes fine
        virtualizarr_result(r, vdss)
        refkv_result(r, vdss, root)
    print(f"  one Zarr array today: {r.vz_note}\n  switch sketch:        {r.rk_note}\n"
          f"                        {r.extra['archive']}")
    return r


def plot(r: Result, path: Path) -> None:
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(9, 6.5), height_ratios=[3, 2], constrained_layout=True)
    fig.suptitle(r.title, fontsize=10, x=0.01, ha="left")
    n = r.want.shape[0]
    t = np.arange(n)
    ax.plot(t, r.want[:, 0], color="0.75", lw=6, label="the files, each read on its own")
    if r.vz is not None:
        ax.plot(t, r.vz[:, 0], color="tab:orange", lw=1.5, marker="x", ms=4,
                label="one Zarr array today (first file's attributes)")
    if r.rk is not None:
        ax.plot(t, r.rk[:, 0], color="k", lw=1.2, ls="--", label="switch sketch")
    ends = r.boundaries[1:] + [n]
    for b in r.boundaries[1:]:
        ax.axvline(b - 0.5, color="0.5", lw=0.8, ls=":")
    top = ax.secondary_xaxis("top")  # each file's encoding, over its span
    top.set_xticks([(b + e - 1) / 2 for b, e in zip(r.boundaries, ends)], r.files, fontsize=7)
    top.tick_params(length=0)
    ax.set_xlim(-0.5, n - 0.5)
    ax.set_xlabel("time index")
    ax.set_ylabel("v at x = 0")
    vz = r.vz_note if r.vz is not None else "\n".join(textwrap.wrap(r.vz_note, 120))
    ax.set_title(vz if r.title.startswith("6.") else f"one Zarr array today: {vz}", fontsize=8, loc="left",
                 color="0.3")
    ax.legend(fontsize=8, loc="best")
    im = ax2.imshow(r.rk.T, aspect="auto", cmap="viridis", interpolation="nearest",
                    extent=(-0.5, n - 0.5, NX - 0.5, -0.5))
    for b in r.boundaries[1:]:
        ax2.axvline(b - 0.5, color="w", lw=0.8, ls=":")
    ax2.set_xlabel("time index")
    ax2.set_ylabel("x")
    ax2.set_title(f"switch sketch, all of v(time, x): {r.rk_note}", fontsize=8, loc="left")
    fig.colorbar(im, ax=ax2, label="v")
    fig.savefig(path, dpi=110)
    plt.close(fig)


rng = np.random.default_rng(0)


def era_files(root: Path) -> list[Path]:
    paths = []
    # gzip, gzip, then gzip behind a shuffle filter, then no compression at all
    for i, (comp, shuf) in enumerate([("gzip", False), ("gzip", False), ("gzip", True), (None, False)]):
        p = root / f"era_{i}.nc"
        write(p, smooth(np.arange(8 * i, 8 * i + 8)).astype("float32"), t0=8 * i, chunks=(4, NX),
              compression=comp, shuffle=shuf)
        paths.append(p)
    return paths


def rectilinear_files(root: Path) -> list[Path]:
    paths, t0 = [], 0
    for i, nt in enumerate([31, 28, 31]):  # January, February, March
        p = root / f"month_{i}.nc"
        write(p, smooth(np.arange(t0, t0 + nt)).astype("float32"), t0=t0, chunks=(nt, NX), compression="gzip")
        paths.append(p)
        t0 += nt
    return paths


def ragged_files(root: Path) -> list[Path]:
    paths = []
    for i in range(3):  # 10 steps per file, chunked by 4: chunks of 4, 4, 2
        p = root / f"ragged_{i}.nc"
        write(p, smooth(np.arange(10 * i, 10 * i + 10)).astype("float32"), t0=10 * i, chunks=(4, NX),
              compression="gzip")
        paths.append(p)
    return paths


def scaled_files(root: Path) -> list[Path]:
    paths = []
    for i, (scale, offset) in enumerate([(0.01, 280.0), (0.02, 270.0), (0.005, 290.0)]):
        p = root / f"scaled_{i}.nc"
        physical = smooth(np.arange(10 * i, 10 * i + 10))
        packed = np.round((physical - offset) / scale).astype("int16")
        write(p, packed, t0=10 * i, chunks=(5, NX), compression="gzip",
              attrs={"scale_factor": np.float32(scale), "add_offset": np.float32(offset),
                     "_FillValue": np.int16(-32767)})
        paths.append(p)
    return paths


def dtype_files(root: Path) -> list[Path]:
    paths = []
    for i, dtype in enumerate(["int16", "int16", "float32"]):
        p = root / f"dtype_{i}.nc"
        values = smooth(np.arange(10 * i, 10 * i + 10), base=50, amp=40)
        write(p, (np.round(values) if dtype == "int16" else values).astype(dtype), t0=10 * i, chunks=(5, NX),
              compression="gzip", attrs={"_FillValue": np.int16(-32767)} if dtype == "int16" else None)
        paths.append(p)
    return paths


CASES = [
    ("1. codec eras: gzip, gzip, shuffle + gzip, uncompressed (GOES-16)", era_files),
    ("2. rectilinear: monthly files of 31, 28 and 31 days, one chunk each", rectilinear_files),
    ("3. ragged inner chunks: 10 steps per file chunked by 4 (4, 4, 2)", ragged_files),
    ("4. per-file scale_factor / add_offset on int16 (BOM BARPA)", scaled_files),
    ("5. varying dtype: int16, int16, float32", dtype_files),
]

def concatenation(figures: Path) -> None:
    """Two arrays combined separately, then concatenated as manifests: the case tables
    merge by union of their content keys, and no chunk's header is rewritten."""
    title = "6. concatenating two combined arrays (gzip, shuffle + gzip | gzip, uncompressed)"
    print(f"\n{'=' * 78}\n{title}\n{'-' * 78}")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        paths = []
        for i, (comp, shuf) in enumerate([("gzip", False), ("gzip", True), ("gzip", False), (None, False)]):
            p = root / f"part_{i}.nc"
            write(p, smooth(np.arange(8 * i, 8 * i + 8)).astype("float32"), t0=8 * i, chunks=(4, NX),
                  compression=comp, shuffle=shuf)
            paths.append(p)
        vdss = virtualize(paths, root)
        a, b = build(vdss[:2]), build(vdss[2:])
        mapping, sources = concatenate(a, b)
        keys = [set(json.loads(m["v/zarr.json"])["codecs"][0]["configuration"]["cases"]) for m in (a[0], b[0], mapping)]
        moved = all(mapping[f"v/c/{int(k.split('/')[2]) + len(json.loads(a[0]['v/zarr.json'])['chunk_grid']['configuration']['chunk_shapes'][0])}/{k.split('/')[3]}"].parts[0].data
                    == ref.parts[0].data for k, ref in b[0].items() if k.startswith("v/c/"))
        direct = json.loads(build(vdss)[0]["v/zarr.json"])["codecs"]
        same = json.loads(mapping["v/zarr.json"])["codecs"] == direct
        archive = root / "concatenated.vzip"
        refkv.ReferenceStore(mapping, sources).save(str(archive))
        got, want = read(refkv.open(str(archive))), truth(paths)
    print(f"  array A: {len(keys[0])} cases {sorted(keys[0])}\n  array B: {len(keys[1])} cases {sorted(keys[1])}")
    print(f"  A + B:   {len(keys[2])} cases (the shared gzip case once); every chunk header copied unchanged: {moved};"
          f" codecs equal to combining all four files at once: {same}")
    note = "concatenated, saved and reopened: " + compare(got, want)
    print(f"  {note}")
    r = Result(title, ["float32\ngzip", "float32\ngzip+shuffle", "float32\ngzip", "float32\nuncompressed"],
               [0, 8, 16, 24], want, vz_note="array A = files 1-2 and array B = files 3-4, each combined with switch, then concatenated", rk=got,
               rk_note=f"A ({len(keys[0])} cases) + B ({len(keys[1])} cases) = {len(keys[2])} cases: " + compare(got, want))
    plot(r, figures / "case6.png")


# --------------------------------------------------------------------------------------
# Scaling: `uv run demo.py scaling`.
#
# How `switch` scales when every chunk is encoded differently.
#
# The worst case for a table of codec chains: N chunks, each packed with its own CF
# scale and offset (as GRIB packs each message). One source file holds the N chunks,
# each zlib-compressed int16. Two archives describe the same array:
#
# - **one case per chunk**: N cases in `zarr.json`, each a full chain with its numbers;
#   a chunk's header is its case's 8-byte key.
# - **one case, two parameters**: one chain in `zarr.json` with `scale_offset`'s
#   `offset` and `scale` left open; a chunk's header is the case index and its two
#   float64 numbers after the key, a literal in its reference in the archive's paged index.
#
# For each, the run reports `zarr.json`, the archive, the time to open the array and to
# read one chunk, and checks 200 chunks spread over the array against the physical
# values, bit for bit.
#
#     uv run demo.py scaling
# --------------------------------------------------------------------------------------


SCALE_CHUNK = (4, 6)
SCALE_FILL = -32767


def scale_packing(i: int) -> tuple[float, float]:
    """Chunk i's CF scale_factor and add_offset: a different pair for every chunk (the
    offset alone is unique; content keys would merge chunks that repeat a pair)."""
    return 0.01 * (1 + (i % 97) / 97), 280.0 + i * 1e-5


def scale_physical(i: int) -> np.ndarray:
    t = np.arange(SCALE_CHUNK[0] * i, SCALE_CHUNK[0] * (i + 1))[:, None]
    return 285 + 4 * np.sin(2 * np.pi * t / 37.0) + 0.6 * np.cos(2 * np.pi * np.arange(SCALE_CHUNK[1])[None, :] / SCALE_CHUNK[1])


def scale_source(path: Path, n: int) -> list[tuple[int, int]]:
    """The N packed chunks, back to back; each one's (offset, length)."""
    ranges, pos = [], 0
    with open(path, "wb") as f:
        for i in range(n):
            scale, offset = scale_packing(i)
            packed = np.round((scale_physical(i) - offset) / scale).astype("<i2")
            data = zlib.compress(packed.tobytes(), 4)
            f.write(data)
            ranges.append((pos, len(data)))
            pos += len(data)
    return ranges


def scale_chain(scale, offset) -> list[dict]:
    return [{"name": "scale_offset", "configuration": {"offset": offset, "scale": scale}},
            {"name": "cast_value", "configuration": {"data_type": "int16",
                                                       "scalar_map": {"encode": [["NaN", SCALE_FILL]], "decode": [[SCALE_FILL, "NaN"]]}}},
            {"name": "bytes", "configuration": {"endian": "little"}},
            {"name": "numcodecs.zlib", "configuration": {"level": 4}}]


def scale_archive(path: Path, url: str, ranges: list[tuple[int, int]], templated: bool) -> int:
    n = len(ranges)
    template = {"codecs": scale_chain({"parameter": 1}, {"parameter": 0}), "parameters": ["float64", "float64"]}
    per_chunk = [] if templated else [{"codecs": scale_chain(1 / scale_packing(i)[0], scale_packing(i)[1]), "parameters": []}
                                      for i in range(n)]
    cases = {case_key(c).hex(): c for c in ([template] if templated else per_chunk)}
    meta = {"zarr_format": 3, "node_type": "array", "shape": [SCALE_CHUNK[0] * n, SCALE_CHUNK[1]], "data_type": "float32",
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(SCALE_CHUNK)}},
            "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
            "fill_value": "NaN", "codecs": [{"name": "switch", "configuration": {"cases": cases}}],
            "attributes": {}}
    doc = json.dumps(meta).encode()
    mapping = {"zarr.json": json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {}}).encode(),
               "v/zarr.json": doc}
    for i, (off, length) in enumerate(ranges):
        scale, offset = scale_packing(i)
        head = header(template, [offset, 1 / scale]) if templated else header(per_chunk[i], [])
        mapping[f"v/c/{i}/0"] = Concat((Range(data=head), Range(source=0, offset=off, length=length)))
    refkv.ReferenceStore(mapping, [Source(url=url)]).save(str(path))
    return len(doc)


def scale_measure(path: Path, n: int) -> tuple[float, float, int]:
    _completed.cache_clear()
    t0 = time.perf_counter()
    a = zarr.open_array(refkv.zarr.store(refkv.open(str(path))), path="v", mode="r")
    t1 = time.perf_counter()
    a[SCALE_CHUNK[0] * (n - 1):SCALE_CHUNK[0] * n]
    t2 = time.perf_counter()
    wrong = 0
    for i in np.linspace(0, n - 1, 200).astype(int):
        scale, offset = scale_packing(i)
        packed = np.round((scale_physical(i) - offset) / scale).astype("int16")
        want = packed.astype("float32") / np.float32(1 / scale) + np.float32(offset)
        got = a[SCALE_CHUNK[0] * i:SCALE_CHUNK[0] * (i + 1)]
        wrong += not np.array_equal(got, want.astype("float32"))
    return t1 - t0, t2 - t1, wrong


def scaling() -> None:
    """The scaling run (`uv run demo.py scaling`)."""
    print(f"{'chunks':>8}  {'design':<26}{'zarr.json':>12}{'archive':>11}{'open':>9}{'read 1':>9}  checked")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for n in (1_000, 10_000, 100_000):
            src = root / f"source_{n}.bin"
            ranges = scale_source(src, n)
            for templated, label in ((False, "one case per chunk"), (True, "one case, two parameters")):
                path = root / f"{n}_{templated}.vzip"
                doc = scale_archive(path, src.as_uri(), ranges, templated)
                t_open, t_read, wrong = scale_measure(path, n)
                print(f"{n:>8,}  {label:<26}{doc / 1e3:>10,.1f}kB{path.stat().st_size / 1e6:>9.2f}MB"
                      f"{t_open:>8.2f}s{t_read * 1e3:>7.1f}ms  "
                      + ("200 chunks bit for bit" if not wrong else f"{wrong} of 200 chunks differ"))


def main() -> None:
    """The six cases and their figures (`uv run demo.py`)."""
    figures = HERE / "figures"
    figures.mkdir(exist_ok=True)
    for i, (title, make) in enumerate(CASES, 1):
        r = run(title, make)
        plot(r, figures / f"case{i}.png")
    concatenation(figures)
    print(f"\nfigures: {figures}")


if __name__ == "__main__":
    if sys.argv[1:] == ["scaling"]:
        scaling()
    elif sys.argv[1:]:
        sys.exit("usage: demo.py [scaling]")
    else:
        main()
