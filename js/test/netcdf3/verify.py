"""Checks the browser netCDF3 zarrifier's output against netCDF-C, through netCDF4-python.

Each fixtures/netcdf3/*.nc, or each netCDF3 file of a directory given (a corpus, such as the files
conformance/zarrify/corpus_netcdf3.txt lists), is served over local HTTP and zarrified by
js/conformance/zarrify.ts (the browser code, run under Node). Every variable must be presented with
the values, shape, data type, dimension names and attributes netCDF-C reads
(python/tests/ir/netcdf3_oracle.py), and the file must be rebuilt byte for byte from the archive's
IR mirror (refkv_source, spec/zarrify/netcdf3.md §6.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#63-the-ir-mirror>). A fixture
named `netcdf3_reject_*` must be rejected, and so must a corpus file netCDF-C does not open. The
fixtures netCDF-C cannot read (names that are not UTF-8, a streaming record count) are checked
against the mirror alone.

Usage: uv run python js/test/netcdf3/verify.py [<dir of .nc files>]
"""

from __future__ import annotations

import sys
from pathlib import Path

import netCDF4
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "python" / "tests" / "ir"))
from netcdf3_oracle import problems  # noqa: E402

import refkv.zarr  # noqa: E402
from refkv.profiles import REGISTRY  # noqa: E402

sys.path.insert(0, str(HERE.parent))
from ir_mirror import mirror_problems  # noqa: E402
from verify_common import LOOPBACK_SOURCES, rejected_by_name, run, serve  # noqa: E402

UNREAD = {"netcdf3_unpresented.nc", "netcdf3_streaming.nc"}


def netcdf3(path: Path) -> bool:
    with path.open("rb") as f:
        head = f.read(4)
    return head[:3] == b"CDF" and head[3:] in (b"\x01", b"\x02", b"\x05")


def opens(path: Path) -> bool:
    try:
        netCDF4.Dataset(path).close()
    except OSError:
        return False
    return True


def problems_of(path: Path, out: Path) -> tuple[list[str], str]:
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    found = []
    if path.name not in UNREAD:
        with netCDF4.Dataset(path) as ds:
            ds.set_auto_maskandscale(False)
            ds.set_auto_chartostring(False)
            found = problems(path, root, ds)
    arrays = sum(1 for _ in root.array_keys())
    return found + mirror_problems(out, path), f"{arrays} arrays"


def main(directory: Path, corpus: bool) -> int:
    assert "netcdf3" in {e["name"] for e in REGISTRY}
    paths = sorted(p for p in directory.glob("*.nc") if netcdf3(p))
    unopened = {p.name for p in paths if corpus and not opens(p)}
    rejected = (lambda name: name in unopened) if corpus else rejected_by_name
    return run(paths, lambda path, out, _: problems_of(path, out), url=serve(paths), rejected=rejected, width=48)


if __name__ == "__main__":
    given = len(sys.argv) > 1
    sys.exit(main(Path(sys.argv[1]) if given else ROOT / "fixtures" / "netcdf3", corpus=given))
