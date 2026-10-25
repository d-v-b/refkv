"""Checks the browser LIF zarrifier's output against liffile.

Each fixtures/lif/*.lif is served over local HTTP and zarrified by js/conformance/zarrify.ts
(the browser code, run under Node). Each image of the file is read by liffile, and each of its
fields (a mosaic's tiles) is a series of the archive, read through the reference reader and
zarr-python, which must equal liffile's data, in its type, with the image's axes in the order
t, c, z, y, x (spec/zarrify/lif.md §4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#4-output>); its source metadata
must hold the image's dimensions and channels as the XML writes them (§5.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#52-the-images>). The file must be
rebuilt byte for byte from the archive's IR mirror (refkv_source, §5.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#53-the-ir-mirror>).
`edge_reject_lif_*` files must be rejected.

Usage: uv run python js/test/lif/verify.py [<dir of .lif files>]
"""

from __future__ import annotations

import sys
from pathlib import Path

import liffile
import numpy as np
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "vzip"))

import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store)
from refkv._narrow import at, mapping, ndarray, sequence, string  # noqa: E402

sys.path.insert(0, str(HERE.parent))
from ir_mirror import mirror_problems  # noqa: E402
from verify_common import LOOPBACK_SOURCES, run, serve  # noqa: E402

# liffile's dimension codes as axes; any other is a field dimension
AXIS = {"T": "t", "C": "c", "S": "c", "Z": "z", "Y": "y", "X": "x"}


def expected_fields(im) -> np.ndarray:
    """liffile's array of an image, one row per field (the first field dimension fastest), each
    in the axis order t, c, z, y, x; BGR samples as stored."""
    sizes = dict(im.sizes)  # before asarray, which reads padded rows only once sizes has found them
    data = np.asarray(im.asarray())
    if im._is_bgr:  # liffile gives BGR samples as RGB; the convention keeps the bytes' order
        data = data[..., ::-1]
    dims = list(sizes)
    fields = [d for d in dims if d not in AXIS]
    order = sorted((d for d in dims if d in AXIS), key=lambda d: "tczyx".index(AXIS[d]))
    data = np.transpose(data, [dims.index(d) for d in reversed(fields)] + [dims.index(d) for d in order])
    return data.reshape(-1, *data.shape[len(fields):])


def problems_of(path: Path, out: Path) -> list[str]:
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    series = [string(s) for s in sequence(at(root.get_group("OME").attrs, "ome", "series"))] if "OME" in root else []
    problems = []
    s = 0
    with liffile.LifFile(path) as f:
        for k, im in enumerate(f.images):
            want = expected_fields(im)
            dims = [dict(e.attrib) for e in
                    im.xml_element.findall("./Data/Image/ImageDescription/Dimensions/DimensionDescription")]
            for field, values in enumerate(want):
                if s >= len(series):
                    return [*problems, f"image {k} field {field}: no series"]
                group = root.get_group(series[s])
                own = mapping(at(group.attrs, "refkv_zarrified", "lif"))
                got = ndarray(group.get_array("0")[...])
                if own["image"] != k or own["dimensions"] != dims:
                    problems.append(f"series {series[s]}: not image {k}'s metadata")
                if got.dtype != values.dtype or got.size != values.size or not np.array_equal(
                        got.reshape(values.shape), values, equal_nan=True):
                    problems.append(f"series {series[s]}: values differ ({got.shape} {got.dtype} vs {values.shape})")
                s += 1
    if s != len(series):
        problems.append(f"{len(series)} series, not {s}")
    return problems + mirror_problems(out, path)


def main(fixtures: Path) -> int:
    paths = sorted(fixtures.glob("*.lif"))
    return run(paths, lambda path, out, _: problems_of(path, out), url=serve(paths), width=44)


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "lif"))
