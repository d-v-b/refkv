"""Survey HDF4 files with pyhdf: SDS, their attributes, Vdatas and their fields, Vgroups."""

import sys
from pathlib import Path

from pyhdf.HDF import HC, HDF
from pyhdf.SD import SD, SDC
import pyhdf.V  # noqa: F401
import pyhdf.VS  # noqa: F401

TYPES = {getattr(SDC, n): n for n in dir(SDC) if n.isupper() and isinstance(getattr(SDC, n), int)
         and n in ("CHAR", "CHAR8", "UCHAR", "UCHAR8", "INT8", "UINT8", "INT16", "UINT16", "INT32",
                   "UINT32", "FLOAT32", "FLOAT64")}


def short(v, n=80):
    s = repr(v)
    return s if len(s) <= n else s[:n] + "..."


def survey(path: Path) -> None:
    print(f"\n######## {path.name} ({path.stat().st_size} bytes)")
    sd = SD(str(path), SDC.READ)
    nds, natt = sd.info()
    print(f"SD: {nds} datasets, {natt} file attributes")
    for name, (val) in sd.attributes(full=1).items():
        v, idx, typ, n = val
        print(f"  file attr {name!r}: {TYPES.get(typ, typ)}[{n}] {short(v)}")
    for i in range(nds):
        s = sd.select(i)
        name, rank, dims, typ, nattr = s.info()
        comp = None
        try:
            comp = s.getcompress()
        except Exception as e:  # noqa: BLE001
            comp = f"none ({e})"
        try:
            chunk = s.getchunkinfo() if hasattr(s, "getchunkinfo") else None
        except Exception as e:  # noqa: BLE001
            chunk = f"? ({e})"
        dims_list = dims if isinstance(dims, list) else [dims]
        print(f"  SDS {i} {name!r}: rank {rank} dims {dims_list} {TYPES.get(typ, typ)} "
              f"attrs {nattr} comp {comp} chunk {chunk} ref {sd.reftoindex if False else s.ref()}")
        for an, (av, aidx, atyp, an_) in s.attributes(full=1).items():
            print(f"      attr {an!r}: {TYPES.get(atyp, atyp)}[{an_}] {short(av, 60)}")
        for d in range(rank):
            dim = s.dim(d)
            dn, dlen, dtyp, dnatt = dim.info()
            print(f"      dim {d} {dn!r} len {dlen} scale-type {TYPES.get(dtyp, dtyp)} nattr {dnatt}")
        if typ in (SDC.CHAR8, SDC.UCHAR8) and rank >= 1:
            a = s.get()
            print(f"      CHAR data shape {getattr(a, 'shape', None)} first {short(a if isinstance(a, str) else a[:2], 120)}")
        s.endaccess()
    sd.end()

    hdf = HDF(str(path), HC.READ)
    vs = hdf.vstart()
    ref = -1
    count = 0
    while True:
        try:
            ref = vs.next(ref)
        except Exception:  # noqa: BLE001
            break
        vd = vs.attach(ref)
        nrec, intmode, fields, size, vname = vd.inquire()
        cls = vd._class
        finfo = vd.fieldinfo()
        count += 1
        if count <= 60:
            print(f"  VDATA ref {ref} {vname!r} class {cls!r} nrec {nrec} size {size} "
                  f"fields {[(f[0], TYPES.get(f[1], f[1]), f[2]) for f in finfo]}")
            if nrec and any(f[1] in (SDC.CHAR8, SDC.UCHAR8) for f in finfo):
                try:
                    recs = vd.read(min(nrec, 2))
                    print(f"      first records {short(recs, 140)}")
                except Exception as e:  # noqa: BLE001
                    print(f"      read failed {e}")
        vd.detach()
    print(f"  {count} vdatas")
    vs.end()
    v = hdf.vgstart()
    ref = -1
    vcount = 0
    while True:
        try:
            ref = v.getid(ref)
        except Exception:  # noqa: BLE001
            break
        vg = v.attach(ref)
        vcount += 1
        if vcount <= 40:
            print(f"  VGROUP ref {ref} {vg._name!r} class {vg._class!r} entries {vg._nmembers}")
        vg.detach()
    print(f"  {vcount} vgroups")
    v.end()
    hdf.close()


for p in sys.argv[1:]:
    try:
        survey(Path(p))
    except Exception as e:  # noqa: BLE001
        print(f"!! {p}: {type(e).__name__}: {e}")
