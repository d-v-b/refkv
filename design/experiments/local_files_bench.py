"""Time 64 KiB reads of one local file through refkv.reader.LocalFiles and through
os.pread on one open descriptor. `get_range_async` is what the reader calls; it runs
`_range` on a worker thread, and the `_range` row times that alone.

The file is read once first, to warm the page cache, so both columns time the
per-read cost (open, stat, seek, read and close, or the read alone) rather than the
disk. On NFS every open is a round trip to the server.

Usage: uv run python design/experiments/local_files_bench.py PATH
"""

from __future__ import annotations

import os
import sys
import time

from refkv._sync import run
from refkv.reader import LocalFiles

N = 2000
CHUNK = 64 * 1024


def main(path: str) -> None:
    size = os.path.getsize(path)
    offsets = [i * (max(0, size - CHUNK) // N) for i in range(N)]  # evenly spread
    with open(path, "rb") as fh:
        while fh.read(1 << 20):  # warm the page cache
            pass

    files = LocalFiles()
    t = time.perf_counter()
    for off in offsets:
        run(files.get_range_async(path, start=off, end=off + CHUNK))
    local = (time.perf_counter() - t) / N * 1e6
    t = time.perf_counter()
    for off in offsets:
        files._range(path, off, off + CHUNK)  # the same read, without the hop to a thread and back
    direct = (time.perf_counter() - t) / N * 1e6

    fd = os.open(path, os.O_RDONLY)
    try:
        t = time.perf_counter()
        for off in offsets:
            os.pread(fd, CHUNK, off)
        pread = (time.perf_counter() - t) / N * 1e6
    finally:
        os.close(fd)

    print(f"{path} ({size} bytes), {N} reads of {CHUNK} bytes")
    print(f"{'LocalFiles.get_range_async':<28}{local:>10.1f} us/read")
    print(f"{'LocalFiles._range':<28}{direct:>10.1f} us/read")
    print(f"{'os.pread, one fd':<28}{pread:>10.1f} us/read")
    print(f"{'ratio, async / pread':<28}{local / pread:>10.1f}x")
    print(f"{'ratio, _range / pread':<28}{direct / pread:>10.1f}x")


if __name__ == "__main__":
    main(sys.argv[1])
