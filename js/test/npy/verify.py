"""Checks the browser NumPy zarrifier's output against `np.load`.

Each fixtures/npy/*.npy file and *.npz archive is served over local HTTP and zarrified by
js/conformance/zarrify.ts (the browser code, run under Node). Every presented array is read
through the reference reader and zarr-python and must equal `np.load`'s, in its shape, data
type and values (fixed-length bytes and voids as `uint8` with a trailing axis,
spec/zarrify/npy.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#3-data-types>),
and an archive's arrays must be its presented members, by name (§4.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#42-an-npz-archive>). The file
must be rebuilt byte for byte from the archive's IR mirror (refkv_source, §5.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#52-the-ir-mirror>). `*_reject_*`
files must be rejected.

Usage: uv run python js/test/npy/verify.py [<dir of .npy and .npz files>]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]

import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store)
from refkv._narrow import at, mapping, ndarray, sequence, string  # noqa: E402

sys.path.insert(0, str(HERE.parent))
from ir_mirror import mirror_problems  # noqa: E402
from verify_common import LOOPBACK_SOURCES, run, serve  # noqa: E402


def as_stored(a: np.ndarray) -> np.ndarray:
    """`np.load`'s array as the layout presents it: fixed-length bytes and voids as `uint8` with
    a trailing axis of their size."""
    if a.dtype.kind in "SV" and not a.dtype.names:
        return np.frombuffer(np.ascontiguousarray(a).tobytes(), dtype=np.uint8).reshape((*a.shape, a.dtype.itemsize))
    return a


def differences(got: np.ndarray, want: np.ndarray, where: str) -> list[str]:
    """How `got` differs from `want` in shape, type or values (a structure's field by field),
    whatever their byte order."""
    if got.shape != want.shape:
        return [f"{where}: shape {got.shape} != {want.shape}"]
    if want.dtype.names:
        if got.dtype.names != want.dtype.names:
            return [f"{where}: fields {got.dtype.names} != {want.dtype.names}"]
        return [p for name in want.dtype.names for p in differences(got[name], want[name], f"{where}.{name}")]
    if got.dtype.newbyteorder("=") != want.dtype.newbyteorder("="):
        return [f"{where}: dtype {got.dtype} != {want.dtype}"]
    return [] if np.array_equal(got, want) else [f"{where}: values differ"]


def problems_of(path: Path, out: Path) -> list[str]:
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    meta = mapping(mapping(at(root.attrs, "refkv_zarrified"))["npy"])
    problems: list[str] = []
    if path.suffix == ".npy":
        problems += differences(ndarray(root.get_array("array")[...]), as_stored(np.load(path)), "array")
    else:
        paths = [string(mapping(m)["path"]) for m in sequence(meta["members"]) if "path" in mapping(m)]
        if sorted(root.array_keys()) != sorted(paths):
            problems.append(f"arrays {sorted(root.array_keys())} != the presented members {sorted(paths)}")
        with np.load(path, allow_pickle=False) as loaded:
            for p in paths:
                problems += differences(ndarray(root.get_array(p)[...]), as_stored(loaded[p]), p)
    return problems + mirror_problems(out, path)


def main(fixtures: Path) -> int:
    paths = sorted([*fixtures.glob("*.npy"), *fixtures.glob("*.npz")])
    return run(paths, lambda path, out, _: problems_of(path, out), url=serve(paths), width=40)


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "npy"))
