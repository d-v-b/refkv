"""Hostile IMS probes for the IR (spec/virtualize/ims.md §5.3
<https://github.com/d-v-b/vzip/blob/main/spec/virtualize/ims.md#53-the-walk>): amplification
through hard links, loops in B-trees and object headers, deep and wide groups. Each is a
synthetic Imaris file (fixtures/generators/ims/write_fixtures.py), some patched after h5py
wrote them.
Usage: uv run python python/tests/ir/gen_ims_probes.py <out dir> [--small]
(--small writes the tests' sizes: no file over 1 MB.)"""
import struct
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "fixtures" / "generators" / "ims"))
sys.path.insert(0, str(ROOT / "conformance" / "virtualize" / "reference"))
import write_fixtures as wf  # noqa: E402
from vzip_reference.ims.hdf5 import Hdf5, le  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
small = "--small" in sys.argv
wf.OUT = out
LEVEL = [{"size": (2, 6, 7), "dims": (2, 8, 8), "chunks": (1, 4, 4)}]


def h5(path: Path) -> Hdf5:
    data = path.read_bytes()

    def read(o: int, n: int) -> bytes:
        return data[o:o + n]

    return Hdf5(read, len(data))


def patch(path: Path, at: int, value: bytes) -> None:
    data = bytearray(path.read_bytes())
    data[at:at + len(value)] = value
    path.write_bytes(bytes(data))


def symtab_btree(f: Hdf5, at: int) -> int:
    """The address of the version 1 B-tree of the old-style group at `at`."""
    m = f.message(f.header(at), 0x11)
    return le(m, 0, 8)


# Time points that are one group by hard links: every chunk referenced once per time point.
for name, n, size in (("ims_amp_times", 4, (2, 64, 64)), *(() if small else (("ims_amp_times_big", 128, (16, 512, 512)),))):
    with wf.open_file(name, "latest") as f:
        wf.imaris(f, [{"size": size, "dims": size, "chunks": (1, 16, 16)}], dtype="u1")
        tp = f["DataSet/ResolutionLevel 0"]
        for t in range(1, n):
            tp[f"TimePoint {t}"] = tp["TimePoint 0"]

# A version 1 B-tree leaf whose child is the node itself, in a group the layout does not
# read (accepted: the group is not described) and in the root group (rejected); and an
# internal node (two entries per node: a deep tree) whose child is the root node.
for name, where in (("ims_btree_loop_extra", "Extra"), ("ims_btree_loop_root", "/")):
    with wf.open_file(name) as f:
        wf.imaris(f, LEVEL, dtype="u1")
        for k in range(3):
            f.create_group(f"Extra/g{k}")
    p = out / f"{name}.ims"
    f = h5(p)
    at = f.root if where == "/" else f.links(f.root)[b"Extra"]
    tree = symtab_btree(f, at)
    patch(p, tree + 24 + 8, struct.pack("<Q", tree))  # child 0 is the node itself
with wf.open_file("ims_btree_cycle_extra", small_k=True) as f:
    wf.imaris(f, LEVEL, dtype="u1")
    for k in range(40):
        f.create_group(f"Extra/g{k:02d}")
p = out / "ims_btree_cycle_extra.ims"
f = h5(p)
tree = symtab_btree(f, f.links(f.root)[b"Extra"])
data = p.read_bytes()
if data[tree + 5] == 0:
    raise SystemExit("ims_btree_cycle_extra: the tree has one level")
patch(p, tree + 24 + 8 + 8 + 8, struct.pack("<Q", tree))  # the root's child 1 is the root

# A dataset the image does not use, its chunks allocated early: an implicit chunk index, over
# a maximum grid larger than its grid.
with wf.open_file("ims_implicit_extra", "latest") as f:
    wf.imaris(f, LEVEL, dtype="u1")
    import h5py
    space = h5py.h5s.create_simple((40, 6), (64, 6))
    dcpl = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
    dcpl.set_chunk((4, 6))
    dcpl.set_alloc_time(h5py.h5d.ALLOC_TIME_EARLY)
    h5py.h5d.create(f.id, b"Implicit", h5py.h5t.STD_U16LE, space, dcpl=dcpl)
    f["Implicit"][...] = np.arange(240, dtype="u2").reshape(40, 6)

# An object header whose continuation names its own first block (rejected where the layout
# reads it, not described elsewhere).
for name, where in (("ims_continuation_loop_extra", "Extra"), ("ims_continuation_loop_root", "/")):
    with wf.open_file(name) as f:
        wf.imaris(f, LEVEL, dtype="u1")
        g = f.create_group("Extra")
        for k in range(40):
            g.attrs[f"a{k:02d}"] = wf.text("x" * 40)
            f.attrs[f"r{k:02d}"] = wf.text("y" * 40)
    p = out / f"{name}.ims"
    f = h5(p)
    at = f.root if where == "/" else f.links(f.root)[b"Extra"]
    data = p.read_bytes()
    pos, n = at + 16, le(data, at + 8, 4)
    end = pos + n
    while pos + 8 <= end:
        typ, size = le(data, pos, 2), le(data, pos + 2, 2)
        if typ == 0x10:
            patch(p, pos + 8, struct.pack("<Q", at + 16))
            break
        pos += 8 + size
    else:
        raise SystemExit(f"{name}: no continuation message")

# Groups nested deeper than the walk goes, and a group of many links.
with wf.open_file("ims_deep_groups", "latest") as f:
    wf.imaris(f, LEVEL, dtype="u1")
    f.create_group("/".join(["Deep"] + [f"d{k}" for k in range(80 if small else 300)]))
with wf.open_file("ims_wide_group", "latest") as f:
    wf.imaris(f, LEVEL, dtype="u1")
    g = f.create_group("Wide")
    for k in range(2000 if small else 50000):
        g[f"link{k:06d}"] = f["DataSet"]  # hard links to one group: walked once
    g.create_dataset("table", data=np.arange(10, dtype="u4"))
