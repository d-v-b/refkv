"""Combining inhomogeneous files into one Zarr array with refkv.

Each file still virtualizes on its own (VirtualiZarr does this well). What no single
Zarr array can express is that the files' chunks decode differently: another codec
chain, another dtype, another CF scaling, another stored chunk shape. Two pieces fix
that without copying a byte of data:

1. `chunk_variant`, an array-to-bytes codec whose configuration lists the ways a chunk
   can be stored (its "variants": codec chain, stored dtype and chunk shape, CF scale
   and offset). A chunk's bytes start with a varint, the index of its variant; the codec
   decodes the rest with that variant, crops it to the chunk's cell and casts it to the
   array's dtype. This is design 2.1 of zarr-specs#399 ("inject context into the chunk
   bytes").
2. refkv's reference values carry that varint. A chunk is
   `Concat(Range(data=<varint>), Range(source=<file>, offset, length))`: a few literal
   bytes stored in the archive, followed by the file's own bytes, read in place. The
   context travels with the reference, so no store has to inject it at read time,
   no codec does I/O, and the result is an ordinary archive any refkv reader opens.

Ragged and month-length files use the zarr-extensions `rectilinear` chunk grid, which
zarr-python 3.4 reads with `zarr.config.set({"array.rectilinear_chunks": True})`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Self

import numcodecs
import numpy as np
import xarray as xr
import zarr
from zarr.abc.codec import ArrayBytesCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.registry import register_codec

import refkv
import refkv.zarr
from refkv.pb import Concat, Range, Source


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _read_varint(buf: bytes) -> tuple[int, int]:
    n = shift = pos = 0
    while True:
        b = buf[pos]
        pos += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, pos


@dataclass(frozen=True)
class ChunkVariantCodec(ArrayBytesCodec):
    """`chunk_variant`: `{"variants": [{"data_type", "endian", "shape", "codecs",
    "scale_factor", "add_offset"}, ...]}`; `codecs` are numcodecs configurations in
    encode order (the decoder applies them in reverse)."""

    variants: tuple = ()
    is_fixed_size = False

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(variants=tuple(json.dumps(v, sort_keys=True) for v in data["configuration"]["variants"]))

    def to_dict(self) -> dict:
        return {"name": "chunk_variant", "configuration": {"variants": [json.loads(v) for v in self.variants]}}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        raw = chunk_bytes.to_bytes()
        index, pos = _read_varint(raw)
        v = json.loads(self.variants[index])
        data = raw[pos:]
        for config in reversed(v["codecs"]):
            data = numcodecs.get_codec(dict(config)).decode(data)
        stored = np.dtype(v["data_type"]).newbyteorder("<" if v["endian"] == "little" else ">")
        arr = np.frombuffer(bytes(data), dtype=stored).reshape(v["shape"])
        arr = arr[tuple(slice(0, n) for n in chunk_spec.shape)]  # an edge chunk stored whole
        out_dtype = chunk_spec.dtype.to_native_dtype()
        if "scale_factor" in v or "add_offset" in v:
            arr = arr * np.asarray(v.get("scale_factor", 1), out_dtype) + np.asarray(v.get("add_offset", 0), out_dtype)
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(np.ascontiguousarray(arr, dtype=out_dtype))

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        raise NotImplementedError("chunk_variant decodes virtual chunks only")

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("chunk_variant", ChunkVariantCodec)


def _variant(ma, attrs) -> dict:
    """How one file's chunks are stored, from its VirtualiZarr ManifestArray."""
    m = ma.metadata
    native = m.data_type.to_native_dtype()
    endian = "little"
    codecs = []
    for c in m.codecs:
        d = c.to_dict()
        if d["name"] == "bytes":
            endian = (d.get("configuration") or {}).get("endian", "little")
        else:  # a numcodecs codec, named "numcodecs.<id>"
            codecs.append({"id": d["name"].split(".", 1)[1], **(d.get("configuration") or {})})
    v = {"data_type": native.str.lstrip("<>|="), "endian": endian, "shape": list(m.chunks), "codecs": codecs}
    for k in ("scale_factor", "add_offset"):
        if k in attrs:
            v[k] = float(attrs[k])
    return v


def _lengths(n: int, c: int) -> list[int]:
    return [c] * (n // c) + ([n % c] if n % c else [])


def combine(vdss: list[xr.Dataset], name: str = "v", dim: str = "time") -> refkv.ReferenceStore:
    """One reference store holding `name` concatenated along `dim` (axis 0) across the
    files' virtual datasets, and the concatenated `dim` and other coordinates."""
    variants, keys = [], {}
    sources, source_of = [], {}
    mapping: dict = {}
    time_chunks: list[int] = []
    other_chunks = None
    scaled = any("scale_factor" in v[name].attrs or "add_offset" in v[name].attrs for v in vdss)
    out_dtype = np.result_type(*[np.float32 if scaled else v[name].data.metadata.data_type.to_native_dtype()
                                 for v in vdss])
    for vds in vdss:
        ma = vds[name].data
        variant = _variant(ma, vds[name].attrs)
        key = json.dumps(variant, sort_keys=True)
        if key not in keys:
            keys[key] = len(variants)
            variants.append(variant)
        prefix = Range(data=_varint(keys[key]))
        c0, *crest = ma.metadata.chunks
        rest = [_lengths(n, c) for n, c in zip(ma.shape[1:], crest)]
        if other_chunks is None:
            other_chunks = rest
        elif rest != other_chunks:
            raise ValueError("the files are chunked differently along the other axes")
        base = len(time_chunks)
        time_chunks += _lengths(ma.shape[0], c0)
        for idx, entry in ma.manifest.dict().items():
            i, *js = (int(x) for x in idx.split("."))
            s = source_of.setdefault(entry["path"], len(source_of))
            if s == len(sources):
                sources.append(Source(url=entry["path"]))
            mapping[f"{name}/c/" + "/".join(str(x) for x in (base + i, *js))] = Concat(
                (prefix, Range(source=s, offset=entry["offset"], length=entry["length"])))
    shape = [sum(time_chunks), *[sum(r) for r in other_chunks]]
    mapping["zarr.json"] = json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {}}).encode()
    mapping[f"{name}/zarr.json"] = json.dumps({
        "zarr_format": 3, "node_type": "array", "shape": shape, "data_type": np.dtype(out_dtype).name,
        "chunk_grid": {"name": "rectilinear", "configuration": {"kind": "inline",
                                                                "chunk_shapes": [time_chunks, *other_chunks]}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": "NaN", "codecs": [{"name": "chunk_variant", "configuration": {"variants": variants}}],
        "dimension_names": list(vdss[0][name].dims), "attributes": {},
    }).encode()
    for coord in vdss[0][name].dims:  # the coordinates, small: inline bytes
        values = (np.concatenate([v[coord].values for v in vdss]) if coord == dim else vdss[0][coord].values)
        attrs = {}
        if values.dtype.kind == "M":  # decoded CF times: back to numbers with their units
            epoch = np.datetime64("2000-01-01", "ns")
            values = ((values - epoch) // np.timedelta64(1, "D")).astype("int64")
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


def read(store: refkv.ReferenceStore, name: str = "v") -> np.ndarray:
    with zarr.config.set({"array.rectilinear_chunks": True}):
        return xr.open_zarr(refkv.zarr.store(store), consolidated=False)[name].values
