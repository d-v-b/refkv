"""Checks the browser's precomputed zarrifier against an independent reader, tensorstore.

Each store in fixtures/precomputed/ is served by the harness proxy (with its S3 listing) and
zarrified by js/conformance/zarrify.ts (the browser code, run under Node). Every array of the
archive is read through refkv.zarr and zarr-python, with the codec `refkv.precomputed`, and must
equal tensorstore's reading of the same scale, where tensorstore can read the store: a coded object
is its HTTP layer's business there, and a local directory has none. `precomputed_reject_*` stores
must be rejected. The mirror must rebuild the listing and `info`, byte for byte.

With `--remote <store URL> <level> <z0:z1,y0:y1,x0:x1>`, a public store is zarrified by the Python
package and that region is compared with tensorstore's reading over HTTP.

Usage: uv run python js/test/precomputed/verify.py [<store dir> ...]
       uv run python js/test/precomputed/verify.py --remote <store URL> <level> <region>
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import tensorstore
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
FIXTURES = ROOT / "fixtures"
sys.path.insert(0, str(ROOT / "conformance" / "zarrify"))
from proxy import Proxy  # noqa: E402

import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store; registers the codec)
from refkv import _core  # noqa: E402
from refkv._narrow import at, integer, ndarray  # noqa: E402
from refkv.ir.cmirror import Archive  # noqa: E402
from refkv.zarrify import zarrify  # noqa: E402

sys.path.insert(0, str(HERE.parent))  # js/test, for verify_common
from verify_common import LOOPBACK_SOURCES, run  # noqa: E402

# a chunk's coding is the HTTP response's for tensorstore, so these read only here
CODED = {"precomputed_codings", "precomputed_codings_bare"}
# its other objects and its empty chunk follow this convention's rules, not tensorstore's
OWN_RULES = {"precomputed_not_presented"}


def tensorstore_level(kvstore, index: int):
    return tensorstore.open({"driver": "neuroglancer_precomputed", "kvstore": kvstore, "scale_index": index}).result()


def check_remote(url: str, level: str, region: str) -> int:
    slices = tuple(slice(*map(int, r.split(":"))) for r in region.split(","))
    root = zarr.open_group(refkv.zarr.store(zarrify(url)), mode="r")
    array = root.get_array(level)
    mine = ndarray(array[slices])
    v = tensorstore_level(url, integer(at(array.attrs.asdict(), "refkv_zarrified", "precomputed", "index")))
    o = v.domain.inclusive_min
    z, y, x = slices
    region_of = v[x.start + o[0]:x.stop + o[0], y.start + o[1]:y.stop + o[1], z.start + o[2]:z.stop + o[2]]
    theirs = np.asarray(region_of.read().result())
    if not np.array_equal(mine, theirs[..., 0].T):
        print(f"{url} {level} {region}: differs from tensorstore")
        return 1
    print(f"{url} {level} {region}: {mine.shape}, equal to tensorstore")
    return 0


def problems_of(store: Path, out: Path) -> list[str]:
    def never(_o: int, _n: int) -> bytes:
        raise AssertionError("a store's mirror reads no source")

    problems = []
    rebuilt = _core.rebuild_store(Archive(str(out), never).get)
    stored = {f.relative_to(store).as_posix(): f for f in store.rglob("*") if f.is_file()}
    if {k: n for k, n, _ in rebuilt} != {k: f.stat().st_size for k, f in stored.items()}:
        problems.append("the mirror's listing is not the store's")
    for k, _, data in rebuilt:
        if data is not None and (k != "info" or stored[k].read_bytes() != data):
            problems.append(f"the mirror's {k} is not the store's")
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r")
    for path in sorted(root.array_keys(), key=int):
        array = root.get_array(path)
        got = ndarray(array[...])
        if store.name in CODED | OWN_RULES:
            continue
        index = integer(at(array.attrs.asdict(), "refkv_zarrified", "precomputed", "index"))
        v = tensorstore_level({"driver": "file", "path": f"{store}/"}, index)
        theirs = np.asarray(v.translate_to[0, 0, 0, 0].read().result()).transpose(3, 2, 1, 0)
        if not np.array_equal(got, theirs[0] if theirs.shape[0] == 1 else theirs):
            problems.append(f"{path}: differs from tensorstore")
    return problems


def main(argv: list[str]) -> int:
    if argv[:1] == ["--remote"]:
        return check_remote(*argv[1:4])
    stores = [Path(a) for a in argv] or sorted(d for d in (FIXTURES / "precomputed").iterdir() if d.is_dir())
    proxy = Proxy(FIXTURES, Path(tempfile.mkdtemp()))
    return run(stores, lambda store, out, _: problems_of(store, out)[:3],
               url=lambda d: proxy.local_store(d.resolve().relative_to(FIXTURES.resolve()).as_posix()), width=44)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
