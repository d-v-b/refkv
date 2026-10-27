"""Prints the requests that paged vzip and phkv make to open a store and look up two keys, and what
each request reads. `trace.py FILE_DIR N [WORKLOAD]` uses FILE_DIR/WORKLOAD/N.paged.vzip and N.phkv,
which bench.py wrote (WORKLOAD is `bytes` or `refs`, `bytes` by default).
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

from bench import BENCH, Server, key

from refkv.policy import Policy
from refkv.reader import Stats
from refkv.vzip.store import VZipStore

SECTIONS = {1: "SOURCES", 2: "RECORDS", 3: "PAGES", 4: "PTRHASH", 5: "SLOTS", 6: "PROPERTY", 7: "METADATA"}


def phkv_regions(path: Path):
    """(start, end, name) of the parts of a standalone phkv file."""
    b = path.read_bytes()
    size = len(b)
    sb = b[size - 64:]
    assert sb[:4] == b"PHKV"
    dir_off, dir_len = struct.unpack_from("<QQ", sb, 24)
    regions = [(dir_off, dir_off + dir_len, "directory"), (size - 64, size, "superblock")]
    for i in range(dir_len // 40):
        kind, = struct.unpack_from("<I", b, dir_off + 40 * i)
        off, length = struct.unpack_from("<QQ", b, dir_off + 40 * i + 8)
        regions.append((off, off + length, SECTIONS[kind]))
    return size, sorted(regions)


def vzip_regions(path: Path):
    b = path.read_bytes()
    size = len(b)
    at = size - 60 if b[size - 60:size - 56] == b"PK\x05\x06" and struct.unpack_from("<H", b,
                                                                                     size - 40)[0] == 38 else size - 44
    comment = b[at + 22:]
    so, ss = struct.unpack_from("<QQ", comment, 6)
    regions = [(so, so + ss, "__vz__/sources")]
    if len(comment) == 38:
        io, isz = struct.unpack_from("<QQ", comment, 22)
        regions.append((io, io + isz, "__vz__/index"))
    cd = struct.unpack_from("<I", b, at + 16)[0]
    if cd == 0xFFFFFFFF:
        loc = at - 20
        cd = struct.unpack_from("<Q", b, struct.unpack_from("<Q", b, loc + 8)[0] + 48)[0]
    regions.append((cd, size, "central directory (pages) and end records"))
    return size, sorted(regions)


def label(off: int, n: int, size: int, regions) -> str:
    end = off + n
    names = []
    for s, e, name in regions:
        if off < e and end > s:
            part = "all" if off <= s and end >= e else "part"
            names.append(f"{name} ({part})")
    if end == size and n >= 1 << 16:
        names.insert(0, "the last 64 KiB")
    return "; ".join(names) or "entry data"


def human(n):
    return f"{n:,} B" if n < 1024 else f"{n / 1024:.1f} KiB"


def main():
    d, n = Path(sys.argv[1]), int(sys.argv[2])
    workload = sys.argv[3] if len(sys.argv) > 3 else "bytes"
    keys = [key(34567 % n), key(81020 % n)]
    server = Server(d)
    out = {}

    f = d / workload / f"{n}.phkv"
    size, regions = phkv_regions(f)
    url = f"http://127.0.0.1:{server.port}/{workload}/{n}.phkv"
    t = json.loads(subprocess.run([str(BENCH), "trace", url, workload, *keys], check=True, capture_output=True,
                                  text=True).stdout)
    out["phkv"] = (size, regions, [(name, [tuple(r) for r in reads]) for name, reads in t["steps"]])

    f = d / workload / f"{n}.paged.vzip"
    size, regions = vzip_regions(f)
    stats = Stats()
    s = VZipStore(f"http://127.0.0.1:{server.port}/{workload}/{n}.paged.vzip", resolve=False, stats=stats,
                  policy=Policy(allow_private_hosts=True))
    steps, seen = [], 0
    for k in keys:
        (s.raw if workload == "refs" else s.get)(k)
        steps.append((k + " (opens the store first)" if not steps else k, [(st, ln) for _, st, ln in stats.log[seen:]]))
        seen = len(stats.log)
    out["paged vzip"] = (size, regions, steps)

    for name, (size, regions, steps) in out.items():
        print(f"### {name}, {n:,} keys of the `{workload}` workload ({human(size)})\n")
        print("| step | request | offset | length | reads |\n|---|---|---|---|---|")
        for step, reads in steps:
            for i, (off, ln) in enumerate(reads, 1):
                print(f"| {step if i == 1 else ''} | {i} | {off:,} | {human(ln)} | {label(off, ln, size, regions)} |")
            print(f"| | **{len(reads)} requests, {human(sum(r[1] for r in reads))}** | | | |")
        print()


if __name__ == "__main__":
    main()
