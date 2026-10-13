"""The NDPI access benchmark (research/ndpi-access) with the IR's read planner: the policy of
rust/refkv-ir/src/plan.rs (`Planner::policy`, calibrated, on a remote server: the gap threshold
and amplification cap from the latency, rate, connections and byte cost; `packs` for
multi-range servers) and its merge (`refkv._ir.plan_spans`, the Rust function itself).
Usage: bench_ir.py [--byte-cost S_PER_MB]"""
from __future__ import annotations
import dataclasses, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
import bench
import model as m
from refkv import _ir

MIN_GAP, MAX_GAP, WARM_GAP, PACKED_GAP = 4 << 10, 8 << 20, 4 << 10, 512
DOMINATE, MAX_RANGES = 0.5, 100


class IRPlanner(m.Planner):
    """A batch (a view's chunk reads, or one chunk's) through the IR planner's policy."""

    def __init__(self, byte_cost: float = 0.1e-6, per_chunk: bool = False):
        self.byte_cost, self.per_chunk = byte_cost, per_chunk
        self.name = f"IR planner, {'per chunk' if per_chunk else 'per view'}" + (
            f", byte cost {byte_cost * 1e6:g} s/MB" if byte_cost != 0.1e-6 else "")

    def policy(self, n: int, net: m.Net) -> tuple[int, float]:
        if net.multirange:
            return PACKED_GAP, 4.0
        L, c = net.rtt, net.conns
        B = net.bw / c  # a connection's rate when every connection is busy
        paying = L / (1 / B + c * self.byte_cost)
        if n * L / c >= DOMINATE:
            return int(min(max(paying, MIN_GAP), MAX_GAP)), 16.0
        return int(min(max(paying, MIN_GAP), WARM_GAP)), 4.0

    def batch(self, r: np.ndarray, net: m.Net) -> list[list[tuple[int, int]]]:
        ranges = [(int(s), int(e - s)) for s, e in r]
        gap, cap = self.policy(len(ranges), net)
        spans = _ir.plan_spans(ranges, gap, cap, net.conns)
        if net.multirange and len(spans) > 1:
            size = min(max(-(-len(spans) // net.conns), 1), MAX_RANGES)
            return [spans[k:k + size] for k in range(0, len(spans), size)]
        return [[s] for s in spans]

    def plan(self, ranges, net, lv):
        if not ranges:
            return m.Plan()
        groups = ranges if self.per_chunk else [np.concatenate(ranges)]
        return m.Plan([req for g in groups for req in self.batch(g, net)])


bc = float(sys.argv[sys.argv.index("--byte-cost") + 1]) * 1e-6 if "--byte-cost" in sys.argv else 0.1e-6
PL = {
    "today (per chunk, gap 64 KiB)": lambda: m.PerChunk(1 << 16),
    "IR planner, per chunk": lambda: IRPlanner(bc, per_chunk=True),
    "IR planner, per view": lambda: IRPlanner(bc),
    "IR planner, per view, + span cache": lambda: m.SpanCache(IRPlanner(bc)),
    "bound: one exact request per view": lambda: m.Ideal(),
}
NETS = ["h1-20ms-50M", "h1-80ms-500M", "h2-20ms-50M", "h2-80ms-500M", "openslide"]

# the baseline cases (results/baseline.md)
slide = m.load("CMU-1")
cases = {"CMU-1 level 1, all chunks": (1, None), "CMU-1 level 0, central 4×4 chunks": (0, (range(17, 21), range(10, 14)))}
print("| case | reader | requests | MB read | MB of intervals | " + " | ".join(NETS) + " |")
print("|---|---|---:|---:|---:|" + "---:|" * len(NETS))
for case, (L, region) in cases.items():
    lv = slide.levels[L]
    nu, nv = -(-lv.r // lv.b), -(-lv.q // lv.a)
    us, vs = region or (range(nu), range(nv))
    ranges = [m.chunk_ranges(lv, lv.a, lv.b, u, v) for u in us for v in vs]
    need = sum(int((r[:, 1] - r[:, 0]).sum()) for r in ranges)
    for pname, mk in PL.items():
        if "span" in pname:
            continue
        cells = []
        for nname in NETS:
            p = mk().plan(ranges, m.NETS[nname], lv)
            cells.append(f"{len(p.requests)} rq, {p.bytes() / 1e6:.1f} MB, {m.simulate(p.sizes(), m.NETS[nname]):.2f} s")
        p = mk().plan(ranges, m.NETS[NETS[0]], lv)
        print(f"| {case} ({len(ranges)} chunks) | {pname} | {len(p.requests)} | {p.bytes() / 1e6:.1f} | {need / 1e6:.2f} | "
              + " | ".join(cells) + " |")

# the traces: total s · requests · overfetch over the 56 views
for sname in ("CMU-1", "Hamamatsu-1"):
    slide = m.load(sname)
    tr = m.traces(slide)
    nviews = sum(len(v) for v in tr.values())
    print(f"\n**{sname}** ({nviews} views)\n")
    nets = [(n, m.NETS[n]) for n in NETS] + [(f"{n} + multi-range", dataclasses.replace(m.NETS[n], multirange=True))
                                             for n in ("h1-20ms-50M", "h1-80ms-500M")]
    print("| reader | " + " | ".join(n for n, _ in nets) + " |")
    print("|---|" + "---:|" * len(nets))
    for pname, mk in PL.items():
        cells = []
        for _, net in nets:
            rows = []
            for tname in ("overview", "zoom", "pan_L0", "pan_mid"):
                rows += bench.run_trace(slide, m.CHUNKINGS["spec"], tr[tname], mk(), net)
            s = bench.summarize(rows)
            cells.append(f"{s['total_s']:.1f} s · {s['requests']} · {s['overfetch_visible']:.1f}×")
        print(f"| {pname} | " + " | ".join(cells) + " |", flush=True)
