"""refkv's intermediate representation (design/ARCHITECTURE.md §3
<https://github.com/d-v-b/refkv/blob/main/design/ARCHITECTURE.md#3-proposal-an-intermediate-representation>),
the path `refkv.zarrify`
takes for every format (a file here, a `.SAFE.zip` included; a store, a `.SAFE` directory
included, through `store.py`): the Rust core (rust/refkv-ir, the `refkv._core` module)
parses, checking each format's source model schema, plans the reads, projects the
convention's hierarchy and mirrors the IR under `refkv_source`; this package is the
host: the I/O (`planner.py`), the archive writer (`output.py` into `Output`), and
the rebuild from an archive (`cmirror.py`)."""

from __future__ import annotations

import json

from refkv import _core
from refkv.ir.output import from_rust
from refkv.ir.planner import Transport, drive, new_run, open_transport
from refkv.policy import Policy
from refkv.uri import path_reference
from refkv.zarrify.common import Output, Rejected


def sniff(head: bytes) -> str | None:
    """The IR format a file's first bytes (up to 552 of them) give, if any: the Rust core's
    `Format::sniff`, which tests each file profile's signature in the registry's `sniff` order
    (spec/zarrify.md §1.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>)."""
    return _core.sniff(head)


def parse(fmt: str, transport: Transport, conc: int | None = None) -> tuple[_core.Ir, dict, dict]:
    """(IR, facts, planner counts) of the source the transport reads, or Rejected."""
    run = new_run(fmt, transport, conc)
    drive(run, transport, conc)
    try:
        ir, facts, stats = run.finish()
    except _core.Rejected as e:
        raise Rejected(str(e)) from None
    return ir, json.loads(facts), json.loads(stats)


def zarrify(location: str, url: str | None = None, mirror: bool = True, policy: Policy | None = None,
            transport: Transport | None = None, connections: int | None = None,
            byte_cost: float | None = None) -> tuple[str, Output, _core.Ir]:
    """(format, output, IR): the source read through the read planner, parsed by the
    Rust parser of its format, projected and mirrored under refkv_source by the Rust
    core. The output pins source 0's size, and its ETag when every response gave the
    same strong one. `connections`: requests at a time to a remote source (default 8);
    `byte_cost`: seconds of wall time the planner charges a byte fetched (default 0.1 s
    per MB)."""
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
