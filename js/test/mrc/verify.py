"""Checks the browser MRC zarrifier's output against mrcfile.

Each fixtures/mrc/*.mrc is served over local HTTP and zarrified by
js/conformance/zarrify.ts (the browser code, run under Node). The array is read
through the reference reader and zarr-python and must equal mrcfile's data, in its
shape (an image, a stack of images, a volume, or a stack of volumes) and type, and
every header field must be mrcfile's reading of the header (spec/zarrify/mrc.md §5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#5-source-metadata>). The
file must be rebuilt byte for byte from the archive's IR mirror (refkv_source, §5.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#52-the-ir-mirror>).
`mrc_reject_*` files must be rejected.

Usage: uv run python js/test/mrc/verify.py [<dir of .mrc files>]
"""

from __future__ import annotations

import base64
import math
import struct
import sys
import warnings
from pathlib import Path

import mrcfile
import numpy as np
import xarray as xr
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "vzip"))

import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store)
from refkv._narrow import at, mapping, ndarray  # noqa: E402

sys.path.insert(0, str(HERE.parent))
from ir_mirror import mirror_problems  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # js/test, for verify_common
from verify_common import LOOPBACK_SOURCES, run, serve  # noqa: E402

CANONICAL_NAN = 0x7FC00000


def as_json(v):
    """A header value as spec/conventions.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json> and
    spec/zarrify/mrc.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#5-source-metadata> write it,
    from mrcfile's numpy value."""
    if isinstance(v, np.ndarray) and v.ndim == 0:
        return as_json(v[()])
    if isinstance(v, np.void) and v.dtype.names:
        return [as_json(v[name]) for name in v.dtype.names]
    if isinstance(v, np.void):
        return base64.b64encode(bytes(v)).decode()
    if isinstance(v, np.ndarray):
        return [as_json(x) for x in v]
    if isinstance(v, np.floating):
        bits = int(np.asarray(v).astype(v.dtype.newbyteorder("=")).view("=u4"))
        if bits == 1 << 31 or (np.isnan(v) and bits != CANONICAL_NAN):
            return {"bits": f"{bits:08x}"}
    if isinstance(v, (bytes, np.bytes_)):
        b = bytes(v).split(b"\0", 1)[0]
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return {"latin1": b.decode("latin-1")}
    v = v.item() if hasattr(v, "item") else v
    if isinstance(v, float):
        return "NaN" if math.isnan(v) else "Infinity" if v == float("inf") else "-Infinity" if v == float("-inf") else v
    return v


IMOD = 1146047817


def load(path: Path):
    """mrcfile's header and data (its warnings, such as a zero grid size, are not failures).
    mrcfile does not read IMOD's unsigned-byte flag (spec/zarrify/mrc.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#3-data-types>): a mode 0
    file with IMOD's stamp and bit 0 of its flags is its bytes as uint8."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with mrcfile.open(path, permissive=False) as f:
            assert f.header is not None, path  # (None only before a header is read or created)
            header, data = f.header.copy(), np.array(f.data)
    raw = path.read_bytes()
    order = "<" if header.mode.dtype.byteorder in "<=" else ">"
    stamp, flags = struct.unpack(order + "ii", raw[152:160])
    if int(header.mode) == 0 and stamp == IMOD and flags & 1:
        data = data.view(np.uint8)
    return header, data


def problems_of(path: Path, out: Path) -> list[str]:
    header, want = load(path)
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    got = ndarray(root.get_array("0")[...])
    problems = []
    if got.dtype != want.dtype.newbyteorder("="):
        problems.append(f"dtype {got.dtype} != {want.dtype}")
    if got.shape != want.shape or not np.array_equal(got, want, equal_nan=got.dtype.kind in "fc"):
        problems.append(f"values differ ({got.shape} vs {want.shape})")
    zarrified = mapping(at(root.attrs, "refkv_zarrified"))
    meta = mapping(zarrified["mrc"])
    fields = mapping(meta["header"], "header")
    if meta["data_type"] != str(want.dtype.newbyteorder("=")):
        problems.append(f"data_type {meta['data_type']} != {want.dtype}")
    if list(fields) != list(header.dtype.names):
        problems.append(f"header fields {list(fields)} != mrcfile's {list(header.dtype.names)}")
    problems += [f"header {name} {fields.get(name)!r} != mrcfile's {as_json(header[name])!r}"
                 for name in header.dtype.names if fields.get(name) != as_json(header[name])]
    kind = {2: "image", 4: "volume_stack"}.get(want.ndim, "image_stack" if header.ispg == 0 else "volume")
    if meta["kind"] != kind:
        problems.append(f"kind {meta['kind']} != {kind}")
    # the dataset layout (spec/zarrify/mrc-dataset.md): xarray opens it with the file's values
    # and coordinates in angstroms
    if zarrified.get("layouts") != {"dataset": "dataset"}:
        problems.append("the root does not name the dataset layout")
    ds = xr.open_zarr(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), group="dataset", consolidated=False)
    if ds.attrs["refkv_zarrified"]["mrc-dataset"] != meta:
        problems.append("the dataset's source metadata is not the root's")
    if not np.array_equal(ds["data"].values, want, equal_nan=want.dtype.kind in "fc"):
        problems.append("the dataset's values differ")
    for axis in "xyz":
        if axis not in ds.coords:
            continue
        cella, grid = float(header.cella[axis]), int(header[f"m{axis}"])
        origin = [float(header.origin[a]) for a in "xyz"]
        placed = int(header.ispg) != 0 and all(np.isfinite(origin)) and any(o != 0 for o in origin)
        o = origin["xyz".index(axis)] if placed else 0.0
        expected = o + np.arange(ds.sizes[axis], dtype=np.float64) * (cella / grid)
        if not np.array_equal(ds[axis].values, expected) or ds[axis].attrs != {"units": "angstrom"}:
            problems.append(f"coordinate {axis} is not {expected[:3]}...")
        if (ds[axis].encoding.get("dtype") != np.dtype("uint32")
                or ds[axis].encoding.get("scale_factor") != cella / grid):
            problems.append(f"coordinate {axis} is not the index with a scale factor: {ds[axis].encoding}")
    return problems + mirror_problems(out, path)


def main(fixtures: Path) -> int:
    paths = sorted(fixtures.glob("*.mrc"))
    return run(paths, lambda path, out, _: problems_of(path, out), url=serve(paths), width=40)


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "mrc"))
