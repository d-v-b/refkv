"""phkv's record-block cache (Limits::record_block) against paged vzip, over HTTP: random later lookups and
clustered lookups (3 runs of 100 neighbouring keys), with records read one at a time and in blocks, and with
the whole SLOTS section read at open as well.

    uv run python rust/refkv-phkv/bench/blockcache.py --data DIR

DIR is where bench.py wrote its files. Times are at --rtt-ms per request; requests and bytes are counted per lookup.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bench import Server, read_python, read_rust

TAIL = (512 << 10) + 64  # the reader's defaults (Limits)
RECORD_READ = 4608
SLOT_BLOCK = 4096
WHOLE = 1 << 30  # slots_at_open: read SLOTS whole at open, whatever its size


def human(b):
    for u in ("B", "KiB", "MiB"):
        if b < 1024 or u == "MiB":
            return f"{b:.0f} {u}" if u == "B" else f"{b:.1f} {u}"
        b /= 1024
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--rtt-ms", type=float, default=20)
    ap.add_argument("--lookups", type=int, default=200)
    ap.add_argument("--reps", type=int, default=3)
    args = ap.parse_args()
    server = Server(Path(args.data))
    server.rtt = args.rtt_ms / 1e3
    cases = [("refs", 100_000), ("refs", 1_000_000), ("bytes", 100_000)]
    # (label, file suffix, (slots_at_open, record_block) or None for the vzip reader)
    readers = [("vzip, paged", "paged.vzip", None)]
    def name(slots, block):
        what = "records alone" if block == 0 else f"record blocks of {human(block)}"
        return f"phkv, {what}" + (", SLOTS read at open" if slots else "")

    readers += [(name(s, b), "phkv", (s, b)) for s in (0, WHOLE) for b in (0, 4096, 16384, 65536)]
    rows = []
    for workload, n in cases:
        print(f"\n### {n:,} keys, `{workload}`: over HTTP, {args.rtt_ms:g} ms per request\n")
        print("| reader | open + first lookup | random later lookup | clustered lookup |\n|---|---|---|---|")
        for label, suffix, limits in readers:
            url = f"http://127.0.0.1:{server.port}/{workload}/{n}.{suffix}"
            if limits is None:
                r = read_python(url, workload, n, args.reps, args.lookups)
            else:
                slots, block = limits
                r = read_rust(url, workload, n, args.reps, TAIL, RECORD_READ, slots, SLOT_BLOCK, args.lookups, block)
            rows.append({"workload": workload, "n": n, "reader": label, **r})
            cold = f"{r['first_get_requests']} req, {human(r['first_get_bytes'])}, {r['first_get_ms']:.0f} ms"
            warm = f"{r['warm_requests_per_get']:.2f} req, {human(r['warm_bytes_per_get'])}, {r['warm_ms_mean']:.0f} ms"
            clus = (f"{r['clustered_requests_per_get']:.2f} req, {human(r['clustered_bytes_per_get'])}, "
                    f"{r['clustered_ms_mean']:.0f} ms")
            print(f"| {label} | {cold} | {warm} | {clus} |", flush=True)
    with open(f"{args.data}/blockcache.json", "w") as f:
        json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
