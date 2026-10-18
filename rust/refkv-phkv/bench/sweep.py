#!/usr/bin/env python3
"""Sweeps the phkv reader's parameters (Limits::tail and Limits::record_read) and the writer's
(Options::page_size and inline_max) over HTTP with a delay per request, on files bench.py wrote.

    uv run python rust/refkv-phkv/bench/sweep.py --data DIR [--rtt-ms 20]

Prints Markdown tables and writes DIR/sweep.json.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from bench import BENCH, Server, read_rust

DEFAULT_TAIL = (64 << 10) + 64


def human(b):
    for u in ("B", "KiB", "MiB", "GiB"):
        if b < 1024 or u == "GiB":
            return f"{b:.0f} {u}" if u == "B" else f"{b:.1f} {u}"
        b /= 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True, help="the directory bench.py wrote its files under")
    ap.add_argument("--rtt-ms", type=float, default=20)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--big", type=int, default=1_000_000, help="keys of the refs files the tail is swept over")
    ap.add_argument("--bytes-keys", type=int, default=100_000, help="keys of the bytes files the rest is swept over")
    args = ap.parse_args()
    server = Server(args.data)
    server.rtt = args.rtt_ms / 1e3
    results = []

    def run(workload, n, name, *limits, **tags):
        path = args.data / workload / name
        url = f"http://127.0.0.1:{server.port}/{workload}/{name}"
        r = read_rust(url, workload, n, args.reps, *limits)
        r |= {"workload": workload, "n": n, "file": name, "file_bytes": path.stat().st_size, "limits": limits, **tags}
        results.append(r)
        return r

    cold = lambda r: f"{r['first_get_requests']} req, {human(r['first_get_bytes'])}, {r['first_get_ms']:.0f} ms"
    print(f"All over HTTP with {args.rtt_ms:g} ms per request.\n")

    n = args.big
    print(f"### `tail`: the bytes read first, {n:,} references, open and one lookup\n")
    tails = [DEFAULT_TAIL, 128 << 10, 256 << 10, 512 << 10, 1 << 20]
    print("| tail | " + " | ".join(human(t) for t in tails) + " |\n|---|" + "---|" * len(tails))
    for label, name in (("standalone", f"{n}.phkv"), ("hybrid", f"{n}.hybrid")):
        print(f"| {label} | " + " | ".join(cold(run("refs", n, name, t, 512, sweep="tail", tail=t)) for t in tails) + " |")

    n = args.bytes_keys
    print(f"\n### `record_read`: the bytes read at a record first, {n:,} keys of 1 KiB inline values\n")
    rrs = [512, 1024, 2048, 4096]
    print("| record_read | " + " | ".join(human(t) for t in rrs) + " |\n|---|" + "---|" * len(rrs))
    cell = lambda r: f"{r['warm_requests_per_get']:.2f} req, {human(r['warm_bytes_per_get'])}, {r['warm_ms_median']:.0f} ms"
    print("| later lookup | " + " | ".join(cell(run("bytes", n, f"{n}.phkv", DEFAULT_TAIL, rr, sweep="record_read", record_read=rr)) for rr in rrs) + " |")

    print(f"\n### Writer: `page_size` and `inline_max`, {n:,} keys of 1 KiB values\n")
    variants = [("default", 4096, 4096), ("p16k", 16384, 4096), ("p64k", 65536, 4096), ("p64k_rr", 65536, 4096), ("noinline", 4096, 0)]
    print("| page_size, inline_max | file | later lookup (record_read 512) | later lookup (record_read 4096) | list of 100 keys |\n|---|---|---|---|---|")
    for tag, page, inline in variants:
        name = f"{n}.phkv" if tag == "default" else f"{n}.{tag}.phkv"
        if tag == "p64k_rr":
            continue
        if tag != "default" and not (args.data / "bytes" / name).exists():
            subprocess.run([str(BENCH), "build", str(args.data / "bytes"), str(n), "bytes", "1024", tag, str(page), str(inline)], check=True)
        a = run("bytes", n, name, DEFAULT_TAIL, 512, sweep="writer", page_size=page, inline_max=inline)
        b = run("bytes", n, name, DEFAULT_TAIL, 4096, sweep="writer", page_size=page, inline_max=inline)
        print(f"| {human(page)}, {human(inline) if inline else 'none'} | {human(a['file_bytes'])} | {cell(a)} | {cell(b)} | {a['list_requests']} req, {human(a['list_bytes'])}, {a['list_ms']:.0f} ms |")
    (args.data / "sweep.json").write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
