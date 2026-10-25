"""Checks the browser VSI zarrifier's output against the synthetic slides' known pixels, and
against Bio-Formats when it is at hand.

Each fixtures/vsi/*.vsi is served over local HTTP, with the tile files beside it (probed by the
zarrifier: a file that does not exist is a 404), and zarrified by
js/conformance/zarrify.ts (the browser code, run under Node). Every stack's level 0, read
through the reference reader and zarr-python, must equal the array the generator encoded
(fixtures/vsi/expected/<name>/<stack>.npy, in TCZYX order, the samples last; a missing tile the
background), and, when bioio-bioformats can start a JVM, every level of every stack and the
macro image must be what Bio-Formats reads (within 1 for JPEG 2000, whose decoders round
differently). Every file of the input must be rebuilt from the archive's IR mirror (the files are
described in place: their leaves partition them, spec/zarrify/vsi.md §6.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#62-the-ir-mirror>).
`vsi_reject_*` slides must be rejected.

Usage: uv run python js/test/vsi/verify.py [<dir of .vsi files>] [--no-bioformats]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "vzip"))

from refkv import _core  # noqa: E402
import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # js/test, for verify_common
from verify_common import LOOPBACK_SOURCES, run, serve  # noqa: E402


def as_tczyx(arr: np.ndarray, axes: list[str], samples: int) -> np.ndarray:
    axes = list(axes)
    if samples > 1:
        arr = np.moveaxis(arr, axes.index("c"), -1)
        axes.remove("c")
    for ax in ("t", "c", "z"):
        if ax not in axes:
            arr = np.expand_dims(arr, {"t": 0, "c": 1, "z": 2}[ax])
    return arr


def bioformats():
    try:
        from bioio_bioformats import Reader

        Reader(str(ROOT / "fixtures" / "vsi" / "vsi_no_stacks.vsi"))
        return Reader
    except Exception:  # noqa: BLE001
        return None


def problems_of(path: Path, out: Path, summary: dict, Reader) -> list[str]:
    problems = []
    archive = refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES)
    root = zarr.open_group(archive, mode="r", zarr_format=3)
    images = summary["images"]
    expected = path.parent / "expected" / path.stem
    for k, im in enumerate(images):
        if im["kind"] != "stack" or im.get("id") is None or not (expected / f"{im['id']}.npy").exists():
            continue
        want = np.load(expected / f"{im['id']}.npy")
        got = as_tczyx(root[f"{k}/0"][...], im["axes"], im["samples"])
        if got.shape != want.shape or got.dtype != want.dtype:
            problems.append(f"{im['name']}: {got.shape} {got.dtype} != expected {want.shape} {want.dtype}")
        elif not np.array_equal(got, want):
            problems.append(f"{im['name']}: values differ from the generator's")
    if Reader is not None:
        try:
            r = Reader(str(path))
        except Exception:  # noqa: BLE001 - a slide Bio-Formats does not open (odd metadata): known pixels only
            print(f"{path.name:44s} (Bio-Formats does not open it; known pixels only)")
            return problems
        ours = [(k, im) for k, im in enumerate(images) if im["source"] == "tiles" or im["kind"] == "macro"]
        if len(r.scenes) != len(ours):
            problems.append(f"Bio-Formats reads {len(r.scenes)} scenes, we have {len(ours)} images")
        for (k, im), scene in zip(ours, r.scenes):
            r.set_scene(scene)
            r.set_resolution_level(0)  # the level stays set across scenes
            rgb = "S" in str(r.dims)
            tolerance = 1 if im.get("codec") == "jpeg2000" else 0
            bgr = root[str(k)].attrs["ome"]["omero"]["channels"][0]["label"] == "B"
            for level in r.resolution_levels:
                r.set_resolution_level(level)
                want = r.get_image_data("TCZYXS" if rgb else "TCZYX")
                got = as_tczyx(root[f"{k}/{level}"][...], im["axes"], im["samples"])
                if bgr:
                    got = got[..., ::-1]  # stored blue first; Bio-Formats reorders to RGB
                if got.shape != want.shape:
                    problems.append(f"{scene} level {level}: {got.shape} != Bio-Formats' {want.shape}")
                elif np.abs(got.astype(np.int64) - want.astype(np.int64)).max() > tolerance:
                    problems.append(f"{scene} level {level}: values differ from Bio-Formats'")
    # the rebuild: every file of the input from the mirror's table and its own bytes
    import zipfile

    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())

        def get(key: str):
            return z.read(key) if key in names else None

        rebuilt = _core.rebuild_store(get)
    for key, size, held in rebuilt:
        f = path.parent / key
        if held is not None or not f.is_file() or f.stat().st_size != size:
            problems.append(f"{key}: not described in place as a {size}-byte file")
    return problems


def main(fixtures: Path, use_bioformats: bool) -> int:
    Reader = bioformats() if use_bioformats else None
    print("Bio-Formats:", "yes" if Reader else "no (known pixels only)")

    def check(path: Path, out: Path, p) -> list:
        summary = json.loads(p.stdout.strip().splitlines()[-1])
        return problems_of(path, out, summary, Reader)

    paths = sorted(fixtures.glob("*.vsi"))
    return run(paths, check, url=serve(paths))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(Path(args[0]) if args else ROOT / "fixtures" / "vsi", "--no-bioformats" not in sys.argv))
