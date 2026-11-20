"""Checks the browser ENVI zarrifier's output against GDAL and NumPy.

Each fixtures/envi/*.hdr (and *.HDR) is served over local HTTP, its data file beside it (probed by
the zarrifier: a name that does not exist is a 404), and zarrified by js/conformance/zarrify.ts
(the browser code, run under Node). The array, read through the reference reader and zarr-python,
must equal GDAL's ENVI driver's reading of the raster (through rasterio), and NumPy's reading of
the data file by the header's fields as GDAL reads them; its axes `c`, `y`, `x` are GDAL's bands,
lines and samples. Every field GDAL reads must be on the root, by its name with its spaces, the
same text (GDAL joins a braced value's lines). Both files of the input must be rebuilt from the
archive's IR mirror (the data file described in place, the header file held: spec/zarrify/envi.md
§5.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#52-the-ir-mirror>).
`envi_reject_*` inputs must be rejected.

Usage: uv run python js/test/envi/verify.py [<dir of ENVI inputs>]
"""

from __future__ import annotations

import gzip
import sys
import warnings
import zipfile
from pathlib import Path

import numpy as np
import rasterio
import zarr
from rasterio.errors import NotGeoreferencedWarning

HERE = Path(__file__).parent
ROOT = HERE.parents[2]

import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store)
from refkv import _core  # noqa: E402
from refkv._narrow import at, mapping, ndarray, string  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # js/test, for verify_common
from verify_common import LOOPBACK_SOURCES, metadata_v3, run, serve  # noqa: E402

# ENVI's data type codes, as NumPy names their types
TYPES = {1: "u1", 2: "i2", 3: "i4", 4: "f4", 5: "f8", 6: "c8", 9: "c16", 12: "u2", 13: "u4", 14: "i8", 15: "u8"}
# the data's axes, outermost first, by interleave, as indexes into (bands, lines, samples)
ORDER = {"bsq": (0, 1, 2), "bil": (1, 0, 2), "bip": (1, 2, 0)}


def gdal(data: Path) -> tuple[np.ndarray, dict[str, str]]:
    """GDAL's reading of the raster whose data file is `data`, and the header's fields it reads."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with rasterio.open(data) as r:
            return r.read(), r.tags(ns="ENVI")


def numpy_read(data: Path, fields: dict[str, str]) -> np.ndarray:
    """NumPy's reading of the data file by the header's fields, shaped (bands, lines, samples)."""
    raw = data.read_bytes()
    if fields.get("file_compression", "0") == "1":
        raw = gzip.decompress(raw)
    shape = [int(fields[k]) for k in ("bands", "lines", "samples")]
    order = ORDER[fields["interleave"].lower()]
    dtype = np.dtype(TYPES[int(fields["data_type"])]).newbyteorder(">" if fields.get("byte_order") == "1" else "<")
    stored = np.frombuffer(raw[int(fields.get("header_offset", "0")):], dtype).reshape([shape[i] for i in order])
    return stored.transpose(np.argsort(order))


def problems_of(path: Path, out: Path) -> list[str]:
    problems = []
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    own = mapping(at(root.attrs, "refkv_zarrified", "envi"))
    data = path.parent / string(own["data_file"])
    want, fields = gdal(data)
    if not np.array_equal(numpy_read(data, fields), want):
        problems.append("NumPy's reading differs from GDAL's")
    arr = root.get_array("0")
    names = [str(n) for n in metadata_v3(arr).dimension_names or []]
    if names != ["c", "y", "x"]:
        problems.append(f"axes {names} != ['c', 'y', 'x']")
    got = ndarray(arr[...])
    if got.dtype != want.dtype.newbyteorder("=") or got.shape != want.shape:
        problems.append(f"{got.dtype} {got.shape} != GDAL's {want.dtype} {want.shape}")
    elif not np.array_equal(got, want):
        problems.append("values differ from GDAL's")
    # GDAL names a field with `_` for its spaces, and joins a braced value's lines with a space
    mine = {k.replace(" ", "_"): " ".join(string(v).split("\n")) for k, v in mapping(own["fields"]).items()}
    for k, v in fields.items():
        if " ".join(mine.get(k, "").split()) != " ".join(v.split()):
            problems.append(f"field {k!r}: {mine.get(k)!r} != GDAL's {v!r}")
    with zipfile.ZipFile(out) as z:
        keys = set(z.namelist())
        rebuilt = _core.rebuild_store(lambda k: z.read(k) if k in keys else None)
    for key, size, held in rebuilt:
        f = path.parent / key
        if not f.is_file() or f.stat().st_size != size:
            problems.append(f"{key}: not a {size}-byte file of the input")
        elif key == path.name and held != f.read_bytes():
            problems.append(f"{key}: the header is not held, byte for byte")
        elif key != path.name and held is not None:
            problems.append(f"{key}: not described in place")
    return problems


def main(fixtures: Path) -> int:
    paths = sorted(p for p in fixtures.iterdir() if p.suffix in (".hdr", ".HDR"))
    return run(paths, lambda path, out, _: problems_of(path, out), url=serve(paths))


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "envi"))
