"""Writes synthetic safetensors inputs to fixtures/safetensors/, covering the rules of the
safetensors profile (spec/zarrify/safetensors.md §8
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#8-safetensors-profile>):
a tensor of every data type the library defines, `BF16`, `F8_*`, `F4` and `F6_*` included;
scalars, empty tensors and files with no tensors; tensors large enough to be cut into chunks,
packed ones among them; names with `/` and names that cannot be paths; a header past the first
read; and the inputs the profile rejects (`*_reject_*`).

The files are written by the safetensors library (`safetensors.serialize`), the reference
writer, but for what it cannot write: `F6_*` tensors, a header it would not write (unpadded,
its tensors out of order, an entry with another member) and the rejected inputs, which are
written by hand. Values come from ml_dtypes, packed with the least significant bit first, as
PyTorch's `float4_e2m1fn_x2` packs two `F4` elements a byte. js/test/safetensors/verify.py and
python/tests/ir/test_safetensors.py compare each accepted input's zarrification with the
library's reading.

Usage: uv run python fixtures/generators/safetensors/write_fixtures.py
"""

from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path

import ml_dtypes
import numpy as np
import safetensors

OUT = Path(__file__).parents[2] / "safetensors"

# a dtype's name in the header, its library name (None: the library cannot write it), its
# NumPy type and its bits
TYPES: dict[str, tuple[str | None, type, int]] = {
    "BOOL": ("bool", np.bool_, 8),
    "U8": ("uint8", np.uint8, 8),
    "I8": ("int8", np.int8, 8),
    "U16": ("uint16", np.uint16, 16),
    "I16": ("int16", np.int16, 16),
    "U32": ("uint32", np.uint32, 32),
    "I32": ("int32", np.int32, 32),
    "U64": ("uint64", np.uint64, 64),
    "I64": ("int64", np.int64, 64),
    "F16": ("float16", np.float16, 16),
    "F32": ("float32", np.float32, 32),
    "F64": ("float64", np.float64, 64),
    "C64": ("complex64", np.complex64, 64),
    "BF16": ("bfloat16", ml_dtypes.bfloat16, 16),
    "F8_E5M2": ("float8_e5m2", ml_dtypes.float8_e5m2, 8),
    "F8_E4M3": ("float8_e4m3fn", ml_dtypes.float8_e4m3fn, 8),
    "F8_E4M3FNUZ": ("float8_e4m3fnuz", ml_dtypes.float8_e4m3fnuz, 8),
    "F8_E5M2FNUZ": ("float8_e5m2fnuz", ml_dtypes.float8_e5m2fnuz, 8),
    "F8_E8M0": ("float8_e8m0fnu", ml_dtypes.float8_e8m0fnu, 8),
    "F4": ("float4_e2m1fn_x2", ml_dtypes.float4_e2m1fn, 4),
    "F6_E2M3": (None, ml_dtypes.float6_e2m3fn, 6),
    "F6_E3M2": (None, ml_dtypes.float6_e3m2fn, 6),
}


def values(dtype: str, shape: tuple[int, ...], seed: int = 0) -> np.ndarray:
    """Distinct values of every element of a tensor of `dtype`, each exact in it."""
    t = TYPES[dtype][1]
    count = int(np.prod(shape))
    base = (np.arange(count, dtype=np.int64) * 7 + seed) % 251
    if t is np.bool_:
        v = base % 3 == 0
    elif np.issubdtype(t, np.integer):
        v = base - (0 if np.issubdtype(t, np.unsignedinteger) else 100)
    elif t is np.complex64:
        v = (base / 4 - 20) + 1j * (base / 8)
    elif TYPES[dtype][2] <= 8:
        # a narrow float: its finite values, by their bits
        every = np.arange(1 << TYPES[dtype][2], dtype=np.uint8).view(t)
        finite = every[np.isfinite(every.astype(np.float64))]
        return finite[base % len(finite)].reshape(shape)
    else:
        v = base / 4 - 20
    return np.asarray(v).astype(t).reshape(shape)


def data(dtype: str, a: np.ndarray) -> bytes:
    """A tensor's data in the buffer: its elements little-endian, a packed type's bits least
    significant first."""
    k = TYPES[dtype][2]
    if k >= 8:
        return a.astype(np.dtype(TYPES[dtype][1]).newbyteorder("<")).tobytes()
    bits = 0
    for i, e in enumerate(a.reshape(-1).view(np.uint8)):
        bits |= (int(e) & ((1 << k) - 1)) << (i * k)
    return bits.to_bytes(a.size * k // 8, "little")


def library(tensors: dict[str, tuple[str, np.ndarray]], metadata: dict[str, str] | None = None) -> bytes:
    """The file the safetensors library writes of `tensors` (name: dtype and values)."""
    keep, specs = [], {}
    for name, (dtype, a) in tensors.items():
        b = np.frombuffer(data(dtype, a), dtype=np.uint8).copy()
        keep.append(b)
        shape = list(a.shape)
        if dtype == "F4":
            shape[-1] //= 2  # the library's shape of an F4 tensor is its bytes'
        library_name = TYPES[dtype][0]
        assert library_name is not None, dtype
        specs[name] = safetensors.TensorSpec(dtype=library_name, shape=shape,
                                             data_ptr=b.ctypes.data if b.size else 0, data_len=b.size)
    return safetensors.serialize(specs, metadata)


def hand(header: str | bytes, buffer: bytes = b"", pad: bool = True, length: int | None = None) -> bytes:
    """A file of `header` (padded with spaces to a multiple of 8, as the library pads it, when
    `pad`) and `buffer`, its length `length` when given."""
    h = header.encode() if isinstance(header, str) else header
    if pad:
        h += b" " * (-len(h) % 8)
    return struct.pack("<Q", len(h) if length is None else length) + h + buffer


def entries(tensors: list[tuple[str, str, tuple[int, ...], np.ndarray | None]], **extra: dict) -> tuple[str, bytes]:
    """A header's text (compact, as serde_json writes it) and buffer of `tensors`, in the order
    given, the data one after another."""
    members, buffer = {}, b""
    for name, dtype, shape, a in tensors:
        d = data(dtype, a if a is not None else values(dtype, shape))
        offsets = [len(buffer), len(buffer) + len(d)]
        members[name] = {"dtype": dtype, "shape": list(shape), "data_offsets": offsets, **extra.get(name, {})}
        buffer += d
    return json.dumps(members, separators=(",", ":")), buffer


def write(name: str, b: bytes) -> None:
    (OUT / name).write_bytes(b)


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    # the spec's example (spec/zarrify/safetensors.md §6
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#6-example>)
    two = {"weight": ("F32", values("F32", (2, 3))), "bias": ("BF16", values("BF16", (3,), 1))}
    write("st_two.safetensors", library(two, {"format": "pt"}))
    # a tensor of every data type the library writes
    shapes = {"F4": (2, 4)}
    writable = [d for d in TYPES if TYPES[d][0] is not None]
    every = {d.lower(): (d, values(d, shapes.get(d, (2, 3)), i)) for i, d in enumerate(writable)}
    write("st_dtypes.safetensors", library(every))
    # the F6 types, which the library defines and cannot write: 48 and 24 bits
    write("st_f6.safetensors", hand(*entries([("e2m3", "F6_E2M3", (2, 4), None), ("e3m2", "F6_E3M2", (4,), None)])))
    small = {"count": ("I64", values("I64", ())), "none": ("F32", values("F32", (0, 3))),
             "two": ("U8", values("U8", (2,)))}
    write("st_scalar_empty.safetensors", library(small))
    write("st_no_tensors.safetensors", library({}, {"format": "pt"}))
    write("st_empty.safetensors", library({}))
    # cut into two chunks of rows, the second padded (spec/zarrify/safetensors.md §8.3
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#83-chunks>)
    write("st_chunks.safetensors", library({"w": ("F32", values("F32", (301, 1000)))}))
    # packed: two chunks of 550 rows, each starting on a byte; and 551 rows of 999 elements,
    # whose second chunk would start mid-byte, one chunk
    write("st_packed_chunks.safetensors", library({"w": ("F4", values("F4", (1100, 1000)))}))
    write("st_packed_one.safetensors", hand(*entries([("w", "F4", (1102, 999), None)])))
    # names: groups, and names that are not paths (spec/zarrify/safetensors.md §4.1
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#41-paths>)
    names = ["a", "a/b", "enc/conv/kernel", "enc/conv/bias", "x//y", "__x", "refkv_source/w", "zarr.json",
             "model.layers.0.weight", "dir/.."]
    write("st_names.safetensors", library({n: ("U8", values("U8", (2,), i)) for i, n in enumerate(names)}))
    # a header the library would not write: unpadded (N = 8k + 3 or so), its tensors not in the
    # order of their data, __metadata__ between them, two metadata members, an entry with
    # another member, and spaces after the separators
    text, buffer = entries([("b", "I16", (2, 2), None), ("a", "F64", (3,), None)], b={"note": "kept"})
    header = json.loads(text)
    shuffled = {"a": header["a"], "__metadata__": {"format": "np", "source": "hand"}, "b": header["b"]}
    write("st_hand.safetensors", hand(json.dumps(shuffled), buffer, pad=False))
    # a header past the first read, 64 KiB
    write("st_long_header.safetensors", library({"w": ("F16", values("F16", (4,)))}, {"long": "x" * 70000}))

    stores()

    # rejected
    t = '{"a":{"dtype":"F32","shape":[2],"data_offsets":[0,8]}}'
    write("st_reject_short.safetensors", b"\x02\x00\x00\x00\x00")
    write("st_reject_header_limit.safetensors", struct.pack("<Q", 100_000_001) + b'{"')
    write("st_reject_header_outside.safetensors", struct.pack("<Q", 1000) + b'{"a":{"dtype":"F32"}}')
    write("st_reject_not_utf8.safetensors", hand(b'{"\xff":1}'))
    write("st_reject_start.safetensors", hand(" " + t, bytes(8)))
    write("st_reject_not_json.safetensors", hand('{"a":}'))
    again = ',"a":{"dtype":"F32","shape":[2],"data_offsets":[8,16]}}'
    write("st_reject_duplicate.safetensors", hand(t[:-1] + again, bytes(16)))
    write("st_reject_metadata.safetensors", hand('{"__metadata__":{"format":1}}'))
    write("st_reject_entry.safetensors", hand('{"a":{"dtype":"F32","shape":[-1],"data_offsets":[0,0]}}'))
    write("st_reject_dtype.safetensors", hand('{"a":{"dtype":"F128","shape":[1],"data_offsets":[0,16]}}', bytes(16)))
    backwards = '{"a":{"dtype":"U8","shape":[0],"data_offsets":[8,4]}}'
    write("st_reject_end_before_begin.safetensors", hand(backwards, bytes(8)))
    write("st_reject_overlap.safetensors", hand('{"a":{"dtype":"U8","shape":[8],"data_offsets":[0,8]},'
                                                '"b":{"dtype":"U8","shape":[8],"data_offsets":[4,12]}}', bytes(12)))
    write("st_reject_gap.safetensors", hand('{"a":{"dtype":"U8","shape":[4],"data_offsets":[0,4]},'
                                            '"b":{"dtype":"U8","shape":[4],"data_offsets":[8,12]}}', bytes(12)))
    write("st_reject_size.safetensors", hand('{"a":{"dtype":"F32","shape":[3],"data_offsets":[0,8]}}', bytes(8)))
    write("st_reject_misaligned.safetensors", hand('{"a":{"dtype":"F4","shape":[3],"data_offsets":[0,2]}}', bytes(2)))
    write("st_reject_trailing.safetensors", hand(t, bytes(9)))
    write("st_reject_beyond.safetensors", hand(t, bytes(4)))


def put(store: Path, key: str, b: bytes) -> None:
    (store / key).parent.mkdir(parents=True, exist_ok=True)
    (store / key).write_bytes(b)


def stores() -> None:
    """Stores of safetensors files (spec/zarrify/safetensors.md §9
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#9-the-store-form>): a
    model's two shards with their index and configuration and other files, as a Hub repository
    holds them; and a store of every case the store form records."""
    model = OUT / "st_store_model"
    first = {"embed.weight": ("BF16", values("BF16", (8, 4))), "layers.0.w": ("F8_E4M3", values("F8_E4M3", (4, 4), 1)),
             "layers.0.scale": ("F8_E8M0", values("F8_E8M0", (2, 2), 2))}
    second = {"layers.1.w": ("I8", values("I8", (4, 2), 3)), "norm": ("F32", values("F32", (4,), 4))}
    names = {"model-00001-of-00002.safetensors": first, "model-00002-of-00002.safetensors": second}
    weight_map = {}
    for file, tensors in names.items():
        put(model, file, library(tensors))
        weight_map |= dict.fromkeys(tensors, file)
    index = {"metadata": {"total_size": 0}, "weight_map": dict(sorted(weight_map.items()))}
    put(model, "model.safetensors.index.json", json.dumps(index, indent=2).encode())
    put(model, "config.json", json.dumps({"architectures": ["Tiny"], "quantization_config": {"expert_dtype": "fp4"},
                                          "hidden_size": 4}, indent=2).encode())
    put(model, "README.md", b"# A tiny model\n")
    put(model, "inference/generate.py", b'print("hello")\n')
    # every case: a nested model and its configuration, a shard with a bad header (not
    # presented), a shard whose key has a reserved segment, an index that disagrees with its
    # shards, a configuration that is not JSON, a shard whose header is past the first read, and
    # an empty object
    mixed = OUT / "st_store_mixed"
    conv = {"conv.weight": ("F16", values("F16", (2, 2, 3, 3)))}
    put(mixed, "unet/diffusion_pytorch_model.safetensors", library(conv))
    put(mixed, "unet/config.json", b'{"in_channels": 4}')
    short = '{"a":{"dtype":"F32","shape":[3],"data_offsets":[0,8]}}'
    put(mixed, "vae/diffusion_pytorch_model.safetensors", hand(short, bytes(8)))
    put(mixed, "vae/config.json", b'{"latent": ')
    put(mixed, "__hidden/x.safetensors", library({"x": ("U8", values("U8", (2,)))}))
    put(mixed, "text/model.safetensors", library({"w": ("F32", values("F32", (2,)))}))
    disagreeing = {"weight_map": {"w": "model.safetensors", "gone": "other.safetensors"}}
    put(mixed, "text/model.safetensors.index.json", json.dumps(disagreeing).encode())
    put(mixed, "long/model.safetensors", library({"w": ("F16", values("F16", (4,)))}, {"long": "x" * 70000}))
    put(mixed, "empty.txt", b"")


if __name__ == "__main__":
    main()
