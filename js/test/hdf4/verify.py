"""Checks the browser HDF4 zarrifier's output against pyhdf, the HDF4 library's binding.

Each fixtures/hdf4/*.hdf, or each `.hdf` file of a directory given (a corpus, such as GDAL's HDF4
samples, https://download.osgeo.org/gdal/data/hdf4/, or the HDF4 library's test files, `*.hdf` in
https://github.com/HDFGroup/hdf4), is served over local HTTP and zarrified by
js/conformance/zarrify.ts (the browser code, run under Node). Every dataset the SD interface lists
must be presented with the values, shape, dimension names and attributes pyhdf reads, or listed with
a reason (python/tests/ir/hdf4_oracle.py; of the fixtures, the reasons their generator plants), and
the file must be rebuilt byte for byte from the archive's IR mirror (refkv_source,
spec/zarrify/hdf4.md §7.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#73-the-ir-mirror>).
`hdf4_reject_*` files must be rejected. A note gives what a corpus file leaves out, by reason.

Usage: uv run python js/test/hdf4/verify.py [<dir of .hdf files>]
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "python" / "tests" / "ir"))
from hdf4_oracle import UNPRESENTED, nodes, own, problems  # noqa: E402

import refkv.zarr  # noqa: E402

sys.path.insert(0, str(HERE.parent))
from ir_mirror import mirror_problems  # noqa: E402
from verify_common import LOOPBACK_SOURCES, run, serve  # noqa: E402


def problems_of(path: Path, out: Path, corpus: bool) -> tuple[list[str], str]:
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    found = problems(path, root, None if corpus else UNPRESENTED.get(path.name, {}))
    left = Counter(u["reason"] for _, n in nodes(root) for u in own(n).get("unpresented", []))
    arrays = sum(isinstance(n, zarr.Array) for _, n in nodes(root))
    note = f"{arrays} arrays" + (f", not presented: {dict(sorted(left.items()))}" if left else "")
    return found + mirror_problems(out, path), note


def main(directory: Path, corpus: bool) -> int:
    paths = sorted(directory.glob("*.hdf"))
    return run(paths, lambda path, out, _: problems_of(path, out, corpus), url=serve(paths), width=60)


if __name__ == "__main__":
    given = len(sys.argv) > 1
    sys.exit(main(Path(sys.argv[1]) if given else ROOT / "fixtures" / "hdf4", corpus=given))
