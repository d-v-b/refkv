"""refkv's intermediate representation (design/ARCHITECTURE.md §3
<https://github.com/d-v-b/refkv/blob/main/design/ARCHITECTURE.md#3-proposal-an-intermediate-representation>),
the path `refkv.virtualize`
takes for every format (a file here, a `.SAFE.zip` included; a store, a `.SAFE` directory
included, through `store.py`): the Rust core (rust/refkv-ir, the `refkv._core` module)
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


def mpeg_header(head: bytes) -> bool:
    """Whether `head` starts with an MPEG audio frame header (spec/virtualize.md §1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#12-input>): the 11-bit sync, a
    version and a layer that are not reserved, and a sample rate index from 0 to 2, of any layer
    and bitrate index (the MP3 profile rejects Layers I and II, and a free-format bitrate, by
    name)."""
    if len(head) < 4 or head[0] != 0xFF or head[1] & 0xE0 != 0xE0:
        return False
    version, layer, rate = (head[1] >> 3) & 3, (head[1] >> 1) & 3, (head[2] >> 2) & 3
    return version != 1 and layer != 0 and rate != 3


def sniff(head: bytes) -> str | None:
    """The IR format a file's first bytes give, if any (132 of them for DICOM, 348 for
    NIfTI-1, 212 for MRC, 4 for MP3; DICOM first: a dual-personality file's preamble may hold
    a TIFF header)."""
    if head[128:132] == b"DICM":
        return "dicom"
    if head[:4] == ND2_MAGIC:
        return "nd2"
    if head[:4] == b"II*\0" and head[8:12] == b"\x18\x00IS":
        return "vsi"  # a VSI file: a TIFF whose byte 8 starts the metadata tree
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
    if len(head) >= 212 and head[208:211] == b"MAP":
        return "mrc"
    if head[:3] == b"ID3" or mpeg_header(head):
        return "mp3"
    return None


def parse(fmt: str, transport, conc: int | None = None):
    """(IR, facts, planner counts) of the source the transport reads, or Rejected."""
    from refkv import _core

    from refkv.ir.planner import drive, new_run

    run = new_run(fmt, transport, conc)
    drive(run, transport, conc)
    try:
        ir, facts, stats = run.finish()
    except _core.Rejected as e:
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
    from refkv import _core

    from refkv.ir.output import from_rust
    from refkv.ir.planner import drive, new_run, open_transport
    from refkv.uri import path_reference

    transport = transport or open_transport(location, policy)
    want = min(552, transport.size)
    held = transport.head[1] or b""
    head = held[:want] if len(held) >= want else transport.get(0, want)
    fmt = sniff(head)
    if fmt is None:
        raise Rejected("not a file of any supported format")
    run = new_run(fmt, transport, connections, byte_cost)
    drive(run, transport, connections)
    fmt = run.format  # a TIFF the core found to be NDPI is "ndpi"
    try:
        t, ir = run.output(url or (location if transport.remote else path_reference(location)),
                           mirror)
    except _core.Rejected as e:
        raise Rejected(str(e)) from None
    out = from_rust(t)
    out.size = transport.size
    out.etag = transport.etag() if url in (None, location) else None
    return fmt, out, ir
