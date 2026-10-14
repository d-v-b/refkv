# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "refkv[virtualizarr]>=0.1.0",
#     "obstore>=0.11.1",
#     "zarr>=3.4.0",
#     "cast-value-rs>=0.4.2",
#     "xarray>=2026.9.0",
# ]
# ///
"""How `switch` scales when every chunk is encoded differently.

The worst case for a table of codec chains: N chunks, each packed with its own CF
scale and offset (as GRIB packs each message). One source file holds the N chunks,
each zlib-compressed int16. Two archives describe the same array:

- **one case per chunk**: N cases in `zarr.json`, each a full chain with its numbers;
  a chunk's header is its case's 8-byte key.
- **one case, two parameters**: one chain in `zarr.json` with `scale_offset`'s
  `offset` and `scale` left open; a chunk's header is the case index and its two
  float64 numbers after the key, a literal in its reference in the archive's paged index.

For each, the run reports `zarr.json`, the archive, the time to open the array and to
read one chunk, and checks 200 chunks spread over the array against the physical
values, bit for bit.

    uv run scaling.py
"""

from __future__ import annotations

import json
import tempfile
import time
import zlib
from pathlib import Path

import numpy as np
import zarr

import refkv
import refkv.zarr
import switch  # next to this script, which Python puts on sys.path
from refkv.pb import Concat, Range, Source

CHUNK = (4, 6)
FILL = -32767


def packing(i: int) -> tuple[float, float]:
    """Chunk i's CF scale_factor and add_offset: a different pair for every chunk (the
    offset alone is unique; content keys would merge chunks that repeat a pair)."""
    return 0.01 * (1 + (i % 97) / 97), 280.0 + i * 1e-5


def physical(i: int) -> np.ndarray:
    t = np.arange(CHUNK[0] * i, CHUNK[0] * (i + 1))[:, None]
    return 285 + 4 * np.sin(2 * np.pi * t / 37.0) + 0.6 * np.cos(2 * np.pi * np.arange(CHUNK[1])[None, :] / CHUNK[1])


def write_source(path: Path, n: int) -> list[tuple[int, int]]:
    """The N packed chunks, back to back; each one's (offset, length)."""
    ranges, pos = [], 0
    with open(path, "wb") as f:
        for i in range(n):
            scale, offset = packing(i)
            packed = np.round((physical(i) - offset) / scale).astype("<i2")
            data = zlib.compress(packed.tobytes(), 4)
            f.write(data)
            ranges.append((pos, len(data)))
            pos += len(data)
    return ranges


def chain(scale, offset) -> list[dict]:
    return [{"name": "scale_offset", "configuration": {"offset": offset, "scale": scale}},
            {"name": "cast_value", "configuration": {"data_type": "int16",
                                                       "scalar_map": {"encode": [["NaN", FILL]], "decode": [[FILL, "NaN"]]}}},
            {"name": "bytes", "configuration": {"endian": "little"}},
            {"name": "numcodecs.zlib", "configuration": {"level": 4}}]


def archive(path: Path, url: str, ranges: list[tuple[int, int]], templated: bool) -> int:
    n = len(ranges)
    template = {"codecs": chain({"parameter": 1}, {"parameter": 0}), "parameters": ["float64", "float64"]}
    per_chunk = [] if templated else [{"codecs": chain(1 / packing(i)[0], packing(i)[1]), "parameters": []}
                                      for i in range(n)]
    cases = {switch.case_key(c).hex(): c for c in ([template] if templated else per_chunk)}
    meta = {"zarr_format": 3, "node_type": "array", "shape": [CHUNK[0] * n, CHUNK[1]], "data_type": "float32",
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(CHUNK)}},
            "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
            "fill_value": "NaN", "codecs": [{"name": "switch", "configuration": {"cases": cases}}],
            "attributes": {}}
    doc = json.dumps(meta).encode()
    mapping = {"zarr.json": json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {}}).encode(),
               "v/zarr.json": doc}
    for i, (off, length) in enumerate(ranges):
        scale, offset = packing(i)
        head = switch.header(template, [offset, 1 / scale]) if templated else switch.header(per_chunk[i], [])
        mapping[f"v/c/{i}/0"] = Concat((Range(data=head), Range(source=0, offset=off, length=length)))
    refkv.ReferenceStore(mapping, [Source(url=url)]).save(str(path))
    return len(doc)


def measure(path: Path, n: int) -> tuple[float, float, int]:
    switch._completed.cache_clear()
    t0 = time.perf_counter()
    a = zarr.open_array(refkv.zarr.store(refkv.open(str(path))), path="v", mode="r")
    t1 = time.perf_counter()
    a[CHUNK[0] * (n - 1):CHUNK[0] * n]
    t2 = time.perf_counter()
    wrong = 0
    for i in np.linspace(0, n - 1, 200).astype(int):
        scale, offset = packing(i)
        packed = np.round((physical(i) - offset) / scale).astype("int16")
        want = packed.astype("float32") / np.float32(1 / scale) + np.float32(offset)
        got = a[CHUNK[0] * i:CHUNK[0] * (i + 1)]
        wrong += not np.array_equal(got, want.astype("float32"))
    return t1 - t0, t2 - t1, wrong


if __name__ == "__main__":
    print(f"{'chunks':>8}  {'design':<26}{'zarr.json':>12}{'archive':>11}{'open':>9}{'read 1':>9}  checked")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for n in (1_000, 10_000, 100_000):
            src = root / f"source_{n}.bin"
            ranges = write_source(src, n)
            for templated, label in ((False, "one case per chunk"), (True, "one case, two parameters")):
                path = root / f"{n}_{templated}.vzip"
                doc = archive(path, src.as_uri(), ranges, templated)
                t_open, t_read, wrong = measure(path, n)
                print(f"{n:>8,}  {label:<26}{doc / 1e3:>10,.1f}kB{path.stat().st_size / 1e6:>9.2f}MB"
                      f"{t_open:>8.2f}s{t_read * 1e3:>7.1f}ms  "
                      + ("200 chunks bit for bit" if not wrong else f"{wrong} of 200 chunks differ"))
