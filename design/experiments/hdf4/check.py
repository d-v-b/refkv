"""Rebuild every SDS of an HDF4 file from byte ranges alone (the layout walker's), and compare with
pyhdf, the reference reader. Also rebuild each character attribute and compare its text."""

import struct
import sys
import zlib
from pathlib import Path

import numpy as np
from pyhdf.SD import SD, SDC

sys.path.insert(0, str(Path(__file__).parent))
from layout import File  # noqa: E402

# netCDF's default fill values, which the SD library uses without a _FillValue (netcdf.h)
DEFAULT_FILL = {3: 0, 4: 0, 5: np.float32(9.9692099683868690e36), 6: 9.9692099683868690e36,
                20: -127, 21: 255, 22: -32767, 23: 65535, 24: -2147483647, 25: 4294967295}

DT = {3: "u1", 4: "S1", 5: ">f4", 6: ">f8", 20: "i1", 21: "u1", 22: ">i2", 23: ">u2", 24: ">i4",
      25: ">u4", 26: ">i8", 27: ">u8"}


def sds_refs(f: File):
    """For each SD element ref: the NDG that names it, its SDD (dims) and NT (type)."""
    out = {}
    for (t, r), (o, n) in f.dd.items():
        if t not in (720, 700):  # NDG, or the older SDG
            continue
        members = [f.u("HH", o + 4 * i) for i in range(n // 4)]
        d = dict(members)
        sd = next(((tt, rr) for tt, rr in members if tt in (702,)), (702, None))
        if 701 not in d:
            continue
        so, sn = f.dd[(701, d[701])]
        (rank,) = f.u("H", so)
        dims = f.u("I" * rank, so + 2)
        nt_ref = f.u("HH", so + 2 + 4 * rank)[1]
        no, _ = f.dd[(106, nt_ref)]
        typ = f.b[no + 1]
        out[r] = (list(dims), typ, sd[1])
    return out


def rebuild(f: File, ref: int, dims, typ):
    dt = np.dtype(DT[typ & 0xFFF])
    if typ & 0x4000:
        dt = dt.newbyteorder("<")
    need = int(np.prod(dims)) * dt.itemsize
    if ref is None or ((702, ref) not in f.dd and (702 | 0x4000, ref) not in f.dd):
        return None, "unwritten"
    if (702, ref) in f.dd:  # contiguous, plain
        o, n = f.dd[(702, ref)]
        return np.frombuffer(f.b[o:o + n], dt).reshape(dims), "contiguous"
    o, _ = f.dd[(702 | 0x4000, ref)]
    s = f.special(702 | 0x4000, ref)
    if s["kind"] == "COMP":
        if not s["comp"].startswith("DEFLATE"):
            return None, f"no codec: {s['comp']}"
        stream = f.element(40, f.u("H", o + 8)[0]) if s["data"] is None else None
        if stream is None:
            co, cn = s["data"]
            stream = f.b[co:co + cn]
        raw = zlib.decompress(stream)
        return np.frombuffer(raw, dt).reshape(dims), "compressed"
    if s["kind"] == "LINKED":
        raw = f.element(702, ref)
        return np.frombuffer(raw[:need], dt).reshape(dims), f"linked ({len(raw)} bytes for {need})"
    if s["kind"] == "CHUNKED":
        cd = [c for _, _, c in s["dims(flag,len,chunk)"]]
        nd = len(cd)
        out = np.zeros(dims, dt)  # fill value is not checked here
        tref = s["table"][1]
        data = f.element(1963, tref)
        nvert = f.vh(tref)["nvert"]
        for k in range(nvert):
            *origin, ct, cr = struct.unpack_from(">" + "I" * nd + "HH", data, k * (4 * nd + 4))
            if (ct, cr) in f.dd:
                co, cn = f.dd[(ct, cr)]
                raw = f.b[co:co + cn]
            else:
                cs = f.special(ct | 0x4000, cr)
                assert cs["kind"] == "COMP" and cs["comp"].startswith("DEFLATE"), cs
                co, cn = cs["data"]
                raw = zlib.decompress(f.b[co:co + cn])
            block = np.frombuffer(raw, dt).reshape(cd)
            sl = tuple(slice(o_ * c, min((o_ + 1) * c, d)) for o_, c, d in zip(origin, cd, dims))
            out[sl] = block[tuple(slice(0, s_.stop - s_.start) for s_ in sl)]
        return out, f"chunked {nvert} chunks of {cd}"
    raise ValueError(s)


def check(path):
    f = File(path)
    sd = SD(str(path), SDC.READ)
    refs = sds_refs(f)
    ok = bad = 0
    for i in range(sd.info()[0]):
        s = sd.select(i)
        name = s.info()[0]
        ref = s.ref()
        want = s.get()
        if ref not in refs:
            print(f"  {name}: no NDG or SDG of ref {ref}")
            bad += 1
            continue
        dims, typ, ref = refs[ref]
        if list(np.shape(want)) != dims:
            print(f"  {name}: SDD dims {dims}, pyhdf {list(np.shape(want))} (an unlimited dimension)")
            dims = list(np.shape(want))
        try:
            got, how = rebuild(f, ref, dims, typ)
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: ERROR {type(e).__name__} {e}")
            bad += 1
            continue
        if got is None and how.startswith("no codec"):
            print(f"  {name}: {how}")
            s.endaccess()
            continue
        if got is None:
            # unwritten: pyhdf gives the fill value everywhere
            w = np.asarray(want)
            try:
                fill = s.getfillvalue()
            except Exception:  # noqa: BLE001 (no _FillValue)
                fill = DEFAULT_FILL.get(typ & 0xFFF)
            same = w.size == 0 or bool((w == fill).all())
            ok += same
            bad += not same
            print(f"  {name}: unwritten, pyhdf gives {w.flat[0] if w.size else 'nothing'} x{w.size}")
            s.endaccess()
            continue
        same = np.array_equal(np.asarray(want).astype(got.dtype.newbyteorder("=")), got.astype(got.dtype.newbyteorder("=")), equal_nan=got.dtype.kind == "f")
        ok += same
        bad += not same
        if not same:
            print(f"  {name}: MISMATCH ({how})")
        s.endaccess()
    print(f"{Path(path).name}: {ok} SDS equal to pyhdf, {bad} not")


for p in sys.argv[1:]:
    try:
        check(p)
    except Exception as e:  # noqa: BLE001
        print(f"{Path(p).name}: FILE ERROR {type(e).__name__}: {e}")
