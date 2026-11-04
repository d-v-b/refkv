"""Checks the browser safetensors zarrifier's output against the safetensors library.

Each fixtures/safetensors/*.safetensors file is served over local HTTP and zarrified by
js/conformance/zarrify.ts (the browser code, run under Node). Every presented tensor is read
through the reference reader and zarr-python (with refkv's ml_dtypes data types and `packbits`
codec) and must equal the library's reading (`safetensors.deserialize`), in its shape, data type
and bits (a packed type's elements unpacked here by shifts and masks,
spec/zarrify/safetensors.md §3.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#32-packed-data-types>), and
the root's source metadata must list every tensor the library reads (§5.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#51-the-root>). The file must
be rebuilt byte for byte from the archive's IR mirror (refkv_source, §5.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#52-the-ir-mirror>).
`*_reject_*` files must be rejected.

Usage: uv run python js/test/safetensors/verify.py [<dir of .safetensors files>]
"""

from __future__ import annotations

import sys
from pathlib import Path

import ml_dtypes
import numpy as np
import safetensors
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]

import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store, and the data types)
from refkv._narrow import at, mapping, ndarray, sequence, string  # noqa: E402

sys.path.insert(0, str(HERE.parent))
from ir_mirror import mirror_problems  # noqa: E402
from verify_common import LOOPBACK_SOURCES, run, serve  # noqa: E402

# a dtype's NumPy type and bits (spec/zarrify/safetensors.md §3
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#3-data-types>)
TYPES = {
    "BOOL": (np.bool_, 8), "U8": (np.uint8, 8), "I8": (np.int8, 8), "U16": (np.uint16, 16), "I16": (np.int16, 16),
    "U32": (np.uint32, 32), "I32": (np.int32, 32), "U64": (np.uint64, 64), "I64": (np.int64, 64),
    "F16": (np.float16, 16), "F32": (np.float32, 32), "F64": (np.float64, 64), "C64": (np.complex64, 64),
    "BF16": (ml_dtypes.bfloat16, 16), "F8_E5M2": (ml_dtypes.float8_e5m2, 8), "F8_E4M3": (ml_dtypes.float8_e4m3fn, 8),
    "F8_E4M3FNUZ": (ml_dtypes.float8_e4m3fnuz, 8), "F8_E5M2FNUZ": (ml_dtypes.float8_e5m2fnuz, 8),
    "F8_E8M0": (ml_dtypes.float8_e8m0fnu, 8), "F4": (ml_dtypes.float4_e2m1fn, 4),
    "F6_E2M3": (ml_dtypes.float6_e2m3fn, 6), "F6_E3M2": (ml_dtypes.float6_e3m2fn, 6),
}


def expected(dtype: str, shape: list[int], data: bytes) -> np.ndarray:
    """The library's tensor as an array: its bytes little-endian, or a packed type's elements,
    two 4-bit ones a byte, low first, or four 6-bit ones in each 3 bytes."""
    t, k = TYPES[dtype]
    b = np.frombuffer(data, dtype=np.uint8)
    if k >= 8:
        return b.view(np.dtype(t).newbyteorder("<")).reshape(shape)
    if k == 4:
        elements = np.stack([b & 0xF, b >> 4], axis=-1).reshape(-1)
    else:
        word = b.reshape(-1, 3).astype(np.uint32) @ np.array([1, 1 << 8, 1 << 16], dtype=np.uint32)
        elements = np.stack([(word >> (6 * i)) & 0x3F for i in range(4)], axis=-1).reshape(-1)
    return elements.astype(np.uint8)[: int(np.prod(shape))].view(t).reshape(shape)


def differences(got: np.ndarray, want: np.ndarray, where: str) -> list[str]:
    """How `got` differs from `want` in shape, type or bits."""
    if got.shape != want.shape:
        return [f"{where}: shape {got.shape} != {want.shape}"]
    if got.dtype.newbyteorder("=") != want.dtype.newbyteorder("="):
        return [f"{where}: dtype {got.dtype} != {want.dtype}"]
    got_bits, want_bits = (np.ascontiguousarray(a).view(np.uint8) for a in (got, want.astype(got.dtype)))
    same = np.array_equal(got_bits, want_bits)
    return [] if same else [f"{where}: values differ"]


def problems_of(path: Path, out: Path) -> list[str]:
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    meta = mapping(mapping(at(root.attrs, "refkv_zarrified"))["safetensors"])
    tensors = [mapping(t) for t in sequence(meta["tensors"])]
    library = dict(safetensors.deserialize(path.read_bytes()))
    problems: list[str] = []
    if sorted(string(t["name"]) for t in tensors) != sorted(library):
        problems.append(f"tensors {sorted(string(t['name']) for t in tensors)} != the library's {sorted(library)}")
    for t in tensors:
        lib = library.get(string(t["name"]))
        if lib is None or "path" not in t:
            continue
        got = ndarray(root.get_array(string(t["path"]))[...])
        problems += differences(got, expected(lib["dtype"], lib["shape"], lib["data"]), string(t["name"]))
    return problems + mirror_problems(out, path)


def main(fixtures: Path) -> int:
    paths = sorted(fixtures.glob("*.safetensors"))
    return run(paths, lambda path, out, _: problems_of(path, out), url=serve(paths), width=40)


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "safetensors"))
