#!/usr/bin/env python3
"""The cost of reading a store's SLOTS section in blocks of B slots, and the block size that minimises it.

A lookup reads the 8-byte slot of its key. The hash scatters keys, so which slot a lookup wants is
uniform over the N slots, with no locality. With the slots in M = N/B blocks of B slots, read when a lookup
needs one and kept, the expected number of blocks read by k lookups is M (1 − (1 − 1/M)^k), and each read
costs one request: the round trip R plus 8B bytes at bandwidth W. So

    T(k, B) = M (1 − (1 − 1/M)^k) (R + 8B / W),   M = ceil(N / B).

B = 1 is a slot at a time and B = N is the whole section at open (Limits::slots_at_open).

    python3 slots_model.py [N] [R in ms]
"""
import math
import sys

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1_028_992  # the slots of 10^6 keys
R = (float(sys.argv[2]) if len(sys.argv) > 2 else 20) / 1e3
KS = [1, 3, 10, 30, 100, 300, 1000, 10_000, 100_000]
WS = {"unlimited": math.inf, "100 MB/s": 100e6, "12.5 MB/s": 12.5e6}


def cost(k, B, W):
    M = math.ceil(N / B)
    return M * (-math.expm1(k * math.log1p(-1 / M))) * (R + 8 * B / W) if M > 1 else R + 8 * B / W


def human(slots):
    b = 8 * slots
    return f"{b} B" if b < 1024 else f"{b / 1024:.0f} KiB" if b < 1 << 20 else f"{b / (1 << 20):.1f} MiB"


sizes = sorted({max(1, round(2 ** (e / 4))) for e in range(0, 4 * math.ceil(math.log2(N)) + 1)} | {N})
print(f"N = {N:,} slots ({human(N)}), round trip {R * 1e3:g} ms\n")
for name, W in WS.items():
    print(f"### bandwidth {name}\n")
    print("| lookups k | one slot at a time (B = 1) | the whole section at open | the best block | its size | time vs B = 1 | vs whole |")
    print("|---|---|---|---|---|---|---|")
    for k in KS:
        best = min(sizes, key=lambda B: cost(k, B, W))
        one, whole, b = cost(k, 1, W), cost(k, N, W), cost(k, best, W)
        print(f"| {k:,} | {one * 1e3:,.0f} ms | {whole * 1e3:,.0f} ms | {b * 1e3:,.0f} ms | {human(best)} | {b / one:.2f}× | {b / whole:.2f}× |")
    # one block size for every k: the one whose time is within the smallest factor of the best for each k
    worst = lambda B: max(cost(k, B, W) / min(cost(k, c, W) for c in sizes) for k in KS)
    B = min(sizes, key=worst)
    print(f"\nOne size for all k in {KS[0]}..{KS[-1]:,}: {human(B)} ({B:,} slots), never more than {worst(B):.2f}× the best for that k.\n")
