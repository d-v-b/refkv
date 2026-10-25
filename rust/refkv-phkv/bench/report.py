"""Writes the tables of RESULTS.md from a results.json that bench.py wrote: `report.py DIR`."""
import json
import sys
from pathlib import Path

rows = json.loads((Path(sys.argv[1]) / "results.json").read_text())
def sizes(w):
    return sorted({r["n"] for r in rows if r["workload"] == w})
formats = list(dict.fromkeys(r["format"] for r in rows))
transports = list(dict.fromkeys(r["transport"] for r in rows))


def human(b):
    for u in ("B", "KiB", "MiB", "GiB"):
        if b < 1024 or u == "GiB":
            return f"{b:.0f} {u}" if u == "B" else f"{b:.1f} {u}"
        b /= 1024
    return None


def ms(x):
    return f"{x:.2f}" if x < 10 else f"{x:.0f}"


def table(workload, transport, cell):
    ns = sizes(workload)
    out = ["| format (reader) | " + " | ".join(f"{n:,}" for n in ns) + " |", "|---|" + "---|" * len(ns)]
    for f in formats:
        rs = {r["n"]: r for r in rows if r["workload"] == workload and r["transport"] == transport and r["format"] == f}
        reader = next(iter(rs.values()))["reader"]
        out.append(f"| {f} ({reader}) | " + " | ".join(cell(rs[n]) if n in rs else "" for n in ns) + " |")
    return "\n".join(out)


def cold(r):
    return f"{r['first_get_requests']} req, {human(r['first_get_bytes'])}, {ms(r['first_get_ms'])} ms"
def warm(r):
    return f"{r['warm_requests_per_get']:.2f} req, {human(r['warm_bytes_per_get'])}, {ms(r['warm_ms_median'])} ms"
def lst(r):
    return f"{r['list_requests']} req, {human(r['list_bytes'])}, {ms(r['list_ms'])} ms"
def size(r):
    return human(r["file_bytes"])

for workload in ("refs", "bytes"):
    print(f"## Workload `{workload}`\n")
    print("### File size\n")
    print(table(workload, "local", size) + "\n")
    for title, cell in (("First lookup of a store opened cold (open and one `%s`)"
                         % ("raw" if workload == "refs" else "get"), cold),
                        ("Later lookups of random keys in an opened store (per lookup, median)", warm),
                        ("`list` of the 100 keys under one prefix, on a store opened cold", lst)):
        print(f"### {title}\n")
        for t in transports:
            print(f"**{t}**\n")
            print(table(workload, t, cell) + "\n")
