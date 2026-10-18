#!/usr/bin/env python3
"""Paged vzip against phkv, over HTTP: the open and first lookup, and later lookups, with phkv's
slots read one at a time (the default) or in blocks the size of a vzip page (Limits::slot_block).

    uv run python rust/refkv-phkv/bench/versus.py --data DIR

DIR is where bench.py wrote its files. Later lookups are timed over --short of them at --rtt-ms per
request, and counted (requests and bytes, no delay) over --long of them, where the caches have filled.
"""
from __future__ import annotations

import argparse
import json

from bench import Server, read_python, read_rust

DEFAULT_TAIL = (64 << 10) + 64
PAGE = 4096  # the page size of the benchmark's vzip archives


def human(b):
    for u in ("B", "KiB", "MiB"):
        if b < 1024 or u == "MiB":
            return f"{b:.0f} {u}" if u == "B" else f"{b:.1f} {u}"
        b /= 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--rtt-ms", type=float, default=20)
    ap.add_argument("--short", type=int, default=200)
    ap.add_argument("--long", type=int, default=5000)
    ap.add_argument("--reps", type=int, default=3)
    args = ap.parse_args()
    server = Server(__import__("pathlib").Path(args.data))
    cases = [("refs", 100_000), ("refs", 1_000_000), ("bytes", 100_000)]
    rows = []
    for workload, n in cases:
        readers = [("vzip, paged", "paged.vzip", None)]
        readers += [("phkv, slots one at a time", "phkv", (512, 0)), ("phkv, 4 KiB slot blocks", "phkv", (512, PAGE))]
        if workload == "bytes":
            readers += [("phkv, 4 KiB slot blocks, record_read 2 KiB", "phkv", (2048, PAGE))]
        print(f"\n### {n:,} keys, `{workload}`: over HTTP, {args.rtt_ms:g} ms per request\n")
        print(f"| reader | open + first lookup | later lookup ({args.short} lookups) | later lookup ({args.long:,} lookups, counted) |\n|---|---|---|---|")
        for label, suffix, limits in readers:
            url = f"http://127.0.0.1:{server.port}/{workload}/{n}.{suffix}"
            res = []
            for rtt, lookups in ((args.rtt_ms, args.short), (0, args.long)):
                server.rtt = rtt / 1e3
                if limits is None:
                    res.append(read_python(url, workload, n, args.reps, lookups))
                else:
                    record_read, block = limits
                    res.append(read_rust(url, workload, n, args.reps, DEFAULT_TAIL, record_read, 0, block, lookups))
            a, b = res
            rows.append({"workload": workload, "n": n, "reader": label, "short": a, "long": b})
            print(f"| {label} | {a['first_get_requests']} req, {human(a['first_get_bytes'])}, {a['first_get_ms']:.0f} ms "
                  f"| {a['warm_requests_per_get']:.2f} req, {human(a['warm_bytes_per_get'])}, {a['warm_ms_mean']:.0f} ms "
                  f"| {b['warm_requests_per_get']:.2f} req, {human(b['warm_bytes_per_get'])} |", flush=True)
    with open(f"{args.data}/versus.json", "w") as f:
        json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
