"""Combining inhomogeneous files into one Zarr array with refkv, from independent codecs.

Each file virtualizes on its own (VirtualiZarr does this well). What no single Zarr
array can express is that the files' chunks decode differently. Here every way a
chunk can differ is its own, independent codec:

| what varies per file             | codec                                   |
|----------------------------------|-----------------------------------------|
| compression, shuffle             | the file's own codecs (`numcodecs.*`)   |
| stored dtype                     | `cast_value` (zarr-extensions)          |
| CF `scale_factor` / `add_offset` | `scale_offset` (zarr-extensions)        |
| stored chunk shape (edge chunks) | `crop` (new, below)                     |

and one codec, `switch`, chooses per chunk which composition of them applies:

- `switch` is an array-to-bytes codec, `{"cases": [chain, chain, ...]}`, each case an
  ordinary Zarr codec chain (array-to-array codecs, one array-to-bytes codec,
  bytes-to-bytes codecs). A chunk's bytes start with a varint, the index of its case;
  `switch` decodes the rest with that case's chain, as `sharding_indexed` decodes its
  inner chunks with its inner chain.
- `crop` is an array-to-array codec, `{"shape": [...]}`: the stored chunk's shape. It
  decodes by cropping to the chunk's cell (HDF5 stores edge chunks whole) and encodes
  by padding with the fill value.

refkv's reference values carry each chunk's case index: a chunk is
`Concat(Range(data=<varint>), Range(source=<file>, offset, length))`, a literal byte in
the archive, then the file's own bytes read in place. No store injects anything at read
time and no codec does I/O.

Ragged and month-length files use the zarr-extensions `rectilinear` chunk grid, which
zarr-python 3.4 reads with `zarr.config.set({"array.rectilinear_chunks": True})`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Self

import numpy as np
import xarray as xr
import zarr
from zarr.abc.codec import ArrayArrayCodec, ArrayBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.core.metadata.v3 import parse_codecs
from zarr.registry import get_pipeline_class, register_codec

import refkv
import refkv.zarr
from refkv.pb import Concat, Range, Source


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def read_varint(buf: bytes) -> tuple[int, int]:
    n = shift = pos = 0
    while True:
        b = buf[pos]
        pos += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, pos


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


@dataclass(frozen=True)
class SwitchCodec(ArrayBytesCodec):
    """`switch`: `{"cases": [{"codecs": chain, "parameters": [data_type, ...]}, ...]}`.

    A case is a codec chain that may leave values open: `{"parameter": k}` anywhere in a
    codec's configuration stands for the chunk's k-th parameter. A chunk is a varint
    case index, then the case's parameters (each in its declared data type,
    little-endian), then the bytes the completed chain decodes. Chunks that differ only
    in numbers share one case, so `zarr.json` grows with the kinds of encoding, and the
    numbers travel with each chunk.
    """

    cases: tuple = ()  # (codecs as JSON, parameter dtypes)
    is_fixed_size = False

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(cases=tuple((json.dumps(c["codecs"]), tuple(c.get("parameters", ())))
                               for c in data["configuration"]["cases"]))

    def to_dict(self) -> dict:
        return {"name": "switch", "configuration": {"cases": [
            {"codecs": json.loads(codecs), "parameters": list(params)} for codecs, params in self.cases]}}

    def _chain(self, index: int, values: tuple) -> tuple:
        return _completed(self.cases[index][0], values)

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        raw = chunk_bytes.to_bytes()
        index, pos = read_varint(raw)
        values = []
        for dtype in self.cases[index][1]:
            dt = np.dtype(dtype).newbyteorder("<")
            values.append(np.frombuffer(raw, dtype=dt, count=1, offset=pos)[0].item())
            pos += dt.itemsize
        pipeline = get_pipeline_class().from_codecs(self._chain(index, tuple(values))).evolve_from_array_spec(chunk_spec)
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


def header(index: int, params: list[str], values: list[float]) -> bytes:
    """A chunk's switch header: its case, then its parameters."""
    return varint(index) + b"".join(np.array([v], dtype=np.dtype(d).newbyteorder("<")).tobytes()
                                    for d, v in zip(params, values))


def lengths(n: int, c: int) -> list[int]:
    return [c] * (n // c) + ([n % c] if n % c else [])


def combine(vdss: list[xr.Dataset], name: str = "v", dim: str = "time") -> refkv.ReferenceStore:
    """One reference store holding `name` concatenated along `dim` (axis 0) across the
    files' virtual datasets, with the concatenated coordinates."""
    scaled = any("scale_factor" in v[name].attrs or "add_offset" in v[name].attrs for v in vdss)
    out_dtype = np.dtype(np.result_type(*[np.float32 if scaled else v[name].data.metadata.data_type.to_native_dtype()
                                          for v in vdss]))
    cases: list[dict] = []
    index: dict[str, int] = {}
    sources, source_of, mapping = [], {}, {}
    time_chunks: list[int] = []
    other = None
    for vds in vdss:
        ma = vds[name].data
        codecs, params, values = chain(ma, vds[name].attrs, out_dtype)
        case = {"codecs": codecs, "parameters": params}
        k = index.setdefault(json.dumps(case, sort_keys=True), len(cases))
        if k == len(cases):
            cases.append(case)
        head = header(k, params, values)
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
    return refkv.ReferenceStore(mapping, sources)


def read(store, name: str = "v") -> np.ndarray:
    with zarr.config.set({"array.rectilinear_chunks": True}):
        return xr.open_zarr(refkv.zarr.store(store), consolidated=False)[name].values
