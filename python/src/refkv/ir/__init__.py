"""refkv's intermediate representation (design/ARCHITECTURE.md §3
<https://github.com/d-v-b/refkv/blob/main/design/ARCHITECTURE.md#3-proposal-an-intermediate-representation>),
the path `refkv.virtualize`
takes for TIFF (NDPI included), ND2, CZI, DICOM, NIfTI, IMS and SAFE (a `.SAFE.zip` here; a `.SAFE` directory through `store.py`) files, and Zarr v2, N5 and OME-Zarr stores (`store.py`): the Rust core (rust/refkv-ir, the `refkv._ir` module)
parses, checking each format's source model schema, plans the reads, projects the
convention's hierarchy and mirrors the IR under `refkv_source`; this package is the
host: the I/O (`planner.py`), the archive writer (`output.py` into `Output`), and
the rebuild from an archive (`cmirror.py`)."""

from __future__ import annotations

import json

from refkv.virtualize.common import Rejected

TIFF_MAGIC = (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+")
CZI_MAGIC = b"ZISRAWFILE" + bytes(6)
ND2_MAGIC = bytes.fromhex("DACEBE0A")
HDF5 = b"\x89HDF\r\n\x1a\n"
ZIP_MAGIC = b"PK\x03\x04"


def nifti_version(head: bytes) -> int | None:
    """The NIfTI version a file's first bytes (at least 348 of them, when the file has
    them) select (spec/virtualize.md §1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#12-input>), or None."""
    if len(head) < 12:
        return None
    sizes = (int.from_bytes(head[:4], "little", signed=True), int.from_bytes(head[:4], "big", signed=True))
    if 348 in sizes and len(head) >= 348 and head[344:348] == b"n+1\0":
        return 1
    if 540 in sizes and head[4:12] == b"n+2\0\r\n\x1a\n":
        return 2
    return None


def sniff(head: bytes) -> str | None:
    """The IR format a file's first bytes give, if any (132 of them for DICOM, 348 for
    NIfTI-1; DICOM first: a dual-personality file's preamble may hold a TIFF header)."""
    if head[128:132] == b"DICM":
        return "dicom"
    if head[:4] == ND2_MAGIC:
        return "nd2"
    if head[:4] in TIFF_MAGIC:
        return "tiff"
    if head[:16] == CZI_MAGIC:
        return "czi"
    if nifti_version(head) is not None:
        return "nifti"
    if head[:8] == HDF5:
        return "ims"
    if head[:4] == ZIP_MAGIC:
        return "safe"
    return None


def parse(fmt: str, transport, conc: int | None = None):
    """(IR, facts, planner counts) of the source the transport reads, or Rejected."""
    from refkv import _ir

    from refkv.ir.planner import drive, new_run

    run = new_run(fmt, transport, conc)
    drive(run, transport, conc)
    try:
        ir, facts, stats = run.finish()
    except _ir.Rejected as e:
        raise Rejected(str(e)) from None
    return ir, json.loads(facts), json.loads(stats)


def virtualize(location: str, url: str | None = None, mirror: bool = True, policy=None, transport=None,
               connections: int | None = None, byte_cost: float | None = None):
    """(format, output, IR): the source read through the read planner, parsed by the
    Rust parser of its format, projected and mirrored under refkv_source by the Rust
    core. The output pins source 0's size, and its ETag when every response gave the
    same strong one. `connections`: requests at a time to a remote source (default 8);
    `byte_cost`: seconds of wall time the planner charges a byte fetched (default 0.1 s
    per MB)."""
    from refkv import _ir

    from refkv.ir.output import from_rust
    from refkv.ir.planner import drive, new_run, open_transport

    transport = transport or open_transport(location, policy)
    want = min(552, transport.size)
    held = transport.head[1] or b""
    head = held[:want] if len(held) >= want else transport.get(0, want)
    fmt = sniff(head)
    if fmt is None:
        raise Rejected("not a TIFF, CZI, ND2, DICOM, NIfTI or IMS file or SAFE zip file")
    run = new_run(fmt, transport, connections, byte_cost)
    drive(run, transport, connections)
    fmt = run.format  # a TIFF the core found to be NDPI is "ndpi"
    try:
        t, ir = run.output(url or location, mirror)
    except _ir.Rejected as e:
        raise Rejected(str(e)) from None
    out = from_rust(t)
    out.size = transport.size
    out.etag = transport.etag() if url in (None, location) else None
    return fmt, out, ir
