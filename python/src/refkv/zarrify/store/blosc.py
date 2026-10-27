"""The Zarr v3 blosc codec of a Zarr v2 or N5 store's blosc compressor.
"""

from __future__ import annotations

from collections.abc import Mapping

from refkv._narrow import JSON
from refkv.zarrify.common import Rejected
from refkv.zarrify.store.documents import as_int

BLOSC_NAMES = ("blosclz", "lz4", "lz4hc", "snappy", "zlib", "zstd")
MAX_BLOCK = 2**31 - 1


def blosc_codec(c: Mapping[str, JSON], typesize: int, shuffles: Mapping[int, str]) -> dict[str, object]:
    """The Zarr v3 blosc codec of an n5-blosc or numcodecs Blosc configuration
    (spec/zarrify/n5.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#3-datasets>,
    spec/zarrify/zarr2.md §3.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#31-compressors>)."""
    cname, clevel, shuffle = c.get("cname"), as_int(c.get("clevel"), 0, 9), as_int(c.get("shuffle"))
    blocksize = as_int(c.get("blocksize", 0), 0, MAX_BLOCK)
    if cname not in BLOSC_NAMES:
        raise Rejected(f"blosc cname {cname!r} is not supported")
    if clevel is None:
        raise Rejected(f"blosc clevel {c.get('clevel')!r} is not an integer from 0 to 9")
    if shuffle is None or shuffle not in shuffles:
        raise Rejected(f"blosc shuffle {c.get('shuffle')!r} is not supported")
    if blocksize is None:
        raise Rejected(f"blosc blocksize {c.get('blocksize')!r} is not supported")
    s = shuffles[shuffle]
    if s == "auto":
        s = "bitshuffle" if typesize == 1 else "shuffle"
    return {"name": "blosc", "configuration": {"cname": cname, "clevel": clevel, "shuffle": s,
                                               "typesize": typesize, "blocksize": blocksize}}
