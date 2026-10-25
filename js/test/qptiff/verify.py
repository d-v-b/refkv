"""Checks the browser QPTIFF zarrifier against tifffile.

Each fixture in fixtures/qptiff/ is zarrified by js/conformance/zarrify_file.ts (the
browser code, run under Node). The archive is then read by the reference reader
(python/src/refkv) and zarr-python (rectilinear chunk grids enabled), and every level must
equal the channels' pages as tifffile decodes them, grouped by the ImageType of their
descriptions (spec/zarrify/qptiff.md §3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#3-images-and-channels>); the
file is rebuilt from the archive's IR mirror. The rejected fixtures must be refused.

Usage: uv run python js/test/qptiff/verify.py
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import tifffile
import zarr

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("tiff_verify", HERE.parent / "tiff" / "verify.py")
assert spec is not None
assert spec.loader is not None
tiff_verify = sys.modules["tiff_verify"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tiff_verify)

from ir_mirror import is_mirror, mirror_problems  # noqa: E402  (on sys.path through tiff_verify)
from verify_common import tiff_page  # noqa: E402  (on sys.path through tiff_verify)

import refkv.zarr  # noqa: E402
from refkv._narrow import at, ndarray, sequence, string  # noqa: E402

zarr.config.set({"array.rectilinear_chunks": True})


def expected_levels(path: Path) -> list[np.ndarray]:
    """Each level as `[c, y, x]`: the FullResolution pages, then each run of as many
    ReducedResolution pages (but a level in strips whose last strip is short and whose
    compression is neither none nor LZW, which the convention leaves out, §4.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#42-tiles-and-strips>); an
    RGB page's samples become the channel axis."""
    with tifffile.TiffFile(path) as tf:
        kinds = [(re.search(r"<ImageType>([^<]*)</ImageType>", p.description or "") or [None, None])[1]
                 for p in map(tiff_page, tf.pages)]
        full = [i for i, k in enumerate(kinds) if k == "FullResolution"]
        reduced = [i for i, k in enumerate(kinds) if k == "ReducedResolution"]
        n = len(full)
        out = []
        for ks in [full] + [reduced[i:i + n] for i in range(0, len(reduced), n)]:
            page = tiff_page(tf.pages[ks[0]])
            if not page.is_tiled and page.compression not in (1, 5):
                rows = min(page.tags[278].value if 278 in page.tags else page.imagelength, page.imagelength)
                if page.imagelength > rows and page.imagelength % rows:
                    continue  # a level the convention leaves out (§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#42-tiles-and-strips>)
            a = np.stack([tf.pages[k].asarray() for k in ks])
            # an RGB image's samples first; one gray channel has no channel axis
            out.append(np.moveaxis(a[0], -1, 0) if a.ndim == 4 else a[0] if n == 1 else a)
        return out


def main(fixtures: Path = HERE.parents[2] / "fixtures" / "qptiff") -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for path in sorted(fixtures.glob("*.qptiff")):
            out = Path(tmp) / (path.stem + ".vzip")
            p = subprocess.run([*tiff_verify.CLI, str(path), str(out), path.resolve().as_uri()],
                               capture_output=True, text=True, check=False)
            if path.name.startswith("edge_reject"):
                ok = p.returncode == 1 and not out.exists()
                failures += not ok
                print(f"{path.name:46s} {'refused: ' + p.stderr.strip() if ok else 'NOT REFUSED'}")
                continue
            if p.returncode:
                print(f"{path.name:46s} FAILED: {p.stderr.strip()[-300:]}")
                failures += 1
                continue
            problems = []
            if not is_mirror(out):
                problems.append("no IR mirror")
            else:
                problems += mirror_problems(out, path)
            summary = json.loads(p.stdout)
            group = zarr.open_group(refkv.zarr.open(str(out), policy=tiff_verify.LOOPBACK_SOURCES), mode="r",
                                    zarr_format=3)
            datasets = sequence(at(group.attrs, "ome", "multiscales", 0, "datasets"))
            want = expected_levels(path)
            if len(want) != len(datasets):
                problems.append(f"{len(datasets)} levels, tifffile has {len(want)}")
            for level, d in zip(want, datasets, strict=False):
                got = ndarray(group.get_array(string(at(d, "path")))[...])
                if got.dtype != level.dtype or got.shape != level.shape or not np.array_equal(got, level):
                    problems.append(f"level {at(d, 'path')} differs "
                                    f"({got.dtype} {got.shape} vs {level.dtype} {level.shape})")
            failures += bool(problems)
            print(f"{path.name:46s} {problems or 'ok'} format={summary.get('format')} "
                  f"levels={summary['levels']} channels={summary.get('channels')}")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
