"""The benchmark's interval tables (cache/<slide>.npz, model.load's form) from the IR: each
level's facts (the parser's grid: q, r, a, b, the MCU and the restart interval) and its McuStarts
(with McuStartsHighBytes) read where the IR's elements say, from the sparse local copies.
Usage: prepare_ir.py <sparse dir>"""
import sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
import model as m
from vzip.ir import parse
from vzip.ir.planner import open_transport

sparse = Path(sys.argv[1])
m.CACHE.mkdir(exist_ok=True)
for name in ("CMU-1", "Hamamatsu-1", "Hamamatsu-2"):
    path = sparse / f"{name}.ndpi"
    ir, facts, _ = parse("tiff", open_transport(str(path), None))
    data = path.open("rb")

    def values(f):
        if not f:
            return None
        o, n = ir.extent(f["value"])
        data.seek(o)
        b = data.read(n)
        dt = {1: "<u1", 3: "<u2", 4: "<u4", 13: "<u4", 16: "<u8", 18: "<u8"}[f["type"]]
        v = np.frombuffer(b, dt).astype(np.int64)
        if f.get("high") is not None and len(v) == 1:
            ho, hn = ir.extent(f["high"]); data.seek(ho)
            v = v + (int(np.frombuffer(data.read(4), "<u4")[0]) << 32)
        return v

    arrays = {}
    for L, lv in enumerate(facts["levels"]):
        g = lv["grid"]
        if g is None:
            arrays[f"{L}_single"] = np.array([lv["strip"], lv["length"], lv["w"], lv["h"]], np.int64)
            continue
        starts = values(lv["starts"])
        high = values(lv["high"])
        if high is not None:
            starts = starts + (high << 32)
        ends = np.r_[starts[1:] - 2, lv["length"] - 2]
        q, r = g["q"], g["r"]
        arrays[f"{L}_meta"] = np.array([lv["w"], lv["h"], g["mw"], g["mh"], g["restart"] * g["mw"], g["a"], g["b"], q, r],
                                       np.int64)
        arrays[f"{L}_off"] = (lv["strip"] + starts).reshape(r, q)
        arrays[f"{L}_len"] = (ends - starts).reshape(r, q)
    np.savez(m.CACHE / f"{name}.npz", **arrays)
    print(name, [(k, v.shape) for k, v in arrays.items() if k.endswith(("_meta", "_single"))])
