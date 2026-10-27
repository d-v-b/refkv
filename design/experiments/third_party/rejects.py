"""What third-party readers do with the inputs refkv's profiles reject (design/THIRD_PARTY_READERS.md).

For each fixture that `conformance/zarrify/rejects/<format>.json` lists, the format's package
opens it and lists its chunks as a package-backed path would:

- `tiff`: tifffile, every series and level written as references with `write_fsspec` (a level
  it cannot write is reported, and the others still listed); a reference past the end of the
  file is counted;
- `nd2`: nd2, every frame's offset (`ND2File._rdr._frame_offsets`), and the sizes and data type
  the array needs;
- `czi`: czifile, every subblock's data offset and size;
- `ims`: h5py, `get_chunk_info` of every chunk of every chunked dataset.

Prints a Markdown table row per fixture (`OK` with what the package gave, or `FAIL` with its
exception) and a count per format. Usage: `uv run python
design/experiments/third_party/rejects.py [tiff nd2 czi ims]`.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import warnings
from collections.abc import Callable
from pathlib import Path

import czifile
import h5py
import nd2
import tifffile

ROOT = Path(__file__).resolve().parents[3]


def tiff(path: Path) -> str:
    size = path.stat().st_size
    levels, outside = [], 0
    with tifffile.TiffFile(path) as tif:
        for series in tif.series:
            for level in series.levels:
                try:
                    with level.aszarr() as z:
                        out = io.StringIO()
                        z.write_fsspec(out, "https://example.test/" + path.name)
                except ValueError as e:  # this level only, as measure.py counts it
                    levels.append(f"{level.shape} {level.dtype} (write_fsspec fails: {str(e)[:60]})")
                    continue
                refs = json.loads(out.getvalue())
                outside += sum(1 for v in refs.values() if isinstance(v, list) and len(v) == 3 and v[1] + v[2] > size)
                zarrays = [json.loads(v) for k, v in refs.items() if k.endswith(".zarray")]
                codecs = sorted({c["id"] for a in zarrays
                                 for c in [a.get("compressor"), *(a.get("filters") or [])] if c})
                levels.append(f"{level.shape} {level.dtype}{' ' + ','.join(codecs) if codecs else ''}")
    return "; ".join(levels) + (f"; {outside} references past the end of the file" if outside else "")


def nd2_frames(path: Path) -> str:
    with nd2.ND2File(path) as f:
        frames = len(f._rdr._frame_offsets)  # pyrefly: ignore[missing-attribute]
        # what the array needs besides the offsets: its sizes and data type
        return f"{frames} frames, sizes {dict(f.sizes)}, {f.dtype}"


def czi(path: Path) -> str:
    with czifile.CziFile(path) as f:
        return f"{sum(1 for _ in f.subblocks())} subblocks"


def ims(path: Path) -> str:
    datasets: list[h5py.Dataset] = []
    with h5py.File(path, "r") as f:
        f.visititems(lambda _name, obj: datasets.append(obj) if isinstance(obj, h5py.Dataset) else None)
        n = 0
        for ds in datasets:
            if ds.chunks is not None:
                for i in range(ds.id.get_num_chunks()):
                    ds.id.get_chunk_info(i)
                    n += 1
    return f"{n} chunks"


READERS: dict[str, Callable[[Path], str]] = {"tiff": tiff, "nd2": nd2_frames, "czi": czi, "ims": ims}


def main() -> None:
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    for fmt in sys.argv[1:] or list(READERS):
        cases = json.loads((ROOT / "conformance/zarrify/rejects" / f"{fmt}.json").read_text())["cases"]
        ok = 0
        print(f"\n| {fmt} fixture | the profile's rejection | the package |\n|---|---|---|")
        for case in cases:
            try:
                result = "OK " + READERS[fmt](ROOT / "fixtures" / fmt / case["input"])
                ok += 1
            except Exception as e:  # noqa: BLE001 (reported in the table)
                result = f"FAIL {type(e).__name__}: {str(e)[:80]}"
            print(f"| {case['input']} | {case['message'][:60]} | {result} |")
        print(f"\n{fmt}: the package opened {ok} of {len(cases)} rejected fixtures")


if __name__ == "__main__":
    main()
