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

import asyncio
import json

from refkv import _core, _sync
from refkv.ir.kind import Kind as Kind  # re-exported: refkv.ir.Kind
from refkv.ir.output import from_rust
from refkv.ir.planner import Transport, drive, drive_async, new_run, open_transport
from refkv.ir.stored import Stored
from refkv.ir.stored import read as _read_stored
from refkv.policy import Policy
from refkv.uri import path_reference
from refkv.zarrify.common import Output, Rejected
from refkv.zarrify.profiles import REGISTRY

# the TIFF variants, by name and title: the profiles the TIFF profile's reading chooses (their
# registry entries' `chosen_by`; spec/zarrify.md §1.2
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>)
TIFF_VARIANTS = {e["name"]: e["title"] for e in REGISTRY if e["chosen_by"] == "tiff"}


# A decoded IR value (`Ir.value`), as `refkv._core` gives it.
type Value = int | float | str | bytes | list[Value] | dict[str, Value]


class Ir:
    """A source's IR (spec/conventions.md §8
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#8-the-ir-mirror>): the elements of
    `refkv._core.Ir`, each with its path, kind, type, extent, value and children, and `at(path)`,
    an element's index by its path. The IR a plugin's layout is given (`refkv.zarrify.layouts`)."""

    def __init__(self, core: _core.Ir) -> None:
        self._core = core
        self._paths: dict[str, int] | None = None

    def __len__(self) -> int:
        return len(self._core)

    def __getattr__(self, name: str) -> object:
        return getattr(self._core, name)

    # the per-element reads, typed (refkv/_core.pyi says what each gives); a ValueError for no element `i`
    def kind(self, i: int) -> int:
        """The element's kind, a `Kind`."""
        return self._core.kind(i)

    def parent(self, i: int) -> int:
        return self._core.parent(i)

    def name(self, i: int) -> str:
        return self._core.name(i)

    def path(self, i: int) -> str:
        return self._core.path(i)

    def type(self, i: int) -> str:
        return self._core.type(i)

    def space(self, i: int) -> int:
        return self._core.space(i)

    def extent(self, i: int) -> tuple[int, int]:
        return self._core.extent(i)

    def form(self, i: int) -> str | None:
        return self._core.form(i)

    def forms(self) -> list[str]:
        return self._core.forms()

    def target(self, i: int) -> int:
        """What an alias names, or -1."""
        return self._core.target(i)

    def raw(self, i: int) -> bytes | None:
        return self._core.raw(i)

    def value(self, i: int) -> Value | None:
        return self._core.value(i)

    def children(self, i: int) -> list[int]:
        return self._core.children(i)

    def child(self, i: int, name: str) -> int | None:
        return self._core.child(i, name)

    def members(self, i: int) -> tuple[list[int], list[int], list[int], list[int]]:
        """The data members of element `i`, runs expanded: (name index, start, length, form id)."""
        return self._core.members(i)

    def columns(self) -> dict[str, bytes]:
        """The dense columns as little-endian bytes (refkv/_core.pyi)."""
        return self._core.columns()

    def shared(self) -> list[bytes]:
        return self._core.shared()

    def object_bases(self) -> list[tuple[str, int, int]]:
        return self._core.object_bases()

    def stored_arrays(self) -> Stored:
        """The arrays the source stores, as the IR describes them (spec/stored-array.md
        <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md>): the chain table and the
        descriptions."""
        return _read_stored(self._core.stored_arrays())

    def chunk_map(self) -> _core.ChunkMap:
        """The chunk map, a row for each unit the source holds (spec/stored-array.md §4
        <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#4-the-chunk-map>): an object
        of the Arrow PyCapsule interface, which pyarrow, polars or nanoarrow read."""
        return self._core.chunk_map()

    def at(self, path: str) -> int | None:
        """The index of the element at `path`, or None."""
        if self._paths is None:
            self._paths = {self._core.path(i): i for i in range(len(self._core))}
        return self._paths.get(path)

    def index(self, path: str) -> int:
        """The index of the element at `path`; KeyError for a path the IR has no element at."""
        i = self.at(path)
        if i is None:
            raise KeyError(f"no element at {path!r} in the IR")
        return i


def sniff(head: bytes) -> str | None:
    """The IR format a file's first bytes (up to 65,600 of them) give, if any: the Rust core's
    `Format::sniff`, which tests each file profile's signature in the registry's `sniff` order
    (spec/zarrify.md §1.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>)."""
    return _core.sniff(head)


async def sniffed(transport: Transport) -> str | None:
    """The IR format of the file `transport` opened, by its first bytes: 552 of them and, only
    when those give none, 65,600 (`sniff`)."""
    async def head(want: int) -> bytes:
        held = transport.head[1] or b""
        return held[:want] if len(held) >= want else await asyncio.to_thread(transport.get, 0, want)

    want = min(552, transport.size)
    fmt = sniff(await head(want))
    if fmt is None and want < transport.size:
        fmt = sniff(await head(min(65600, transport.size)))
    return fmt


def parse(fmt: str, transport: Transport,
          conc: int | None = None) -> tuple[_core.Ir, dict[str, object], dict[str, object]]:
    """(IR, facts, planner counts) of the source the transport reads, or Rejected."""
    run = new_run(fmt, transport, conc)
    drive(run, transport, conc)
    try:
        ir, facts, stats = run.finish()
    except _core.Rejected as e:
        raise Rejected(str(e)) from None
    return ir, json.loads(facts), json.loads(stats)


class NamesSiblingsError(Exception):
    """A file that names siblings (spec/zarrify.md §1.9
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>): the
    caller starts over with a store run of the format, `args[0]`."""


async def zarrify_async(location: str, url: str | None = None, mirror: bool = True, policy: Policy | None = None,
                        transport: Transport | None = None, connections: int | None = None,
                        byte_cost: float | None = None, fmt: str | None = None,
                        layouts: list[str] | None = None, checksums: bool = False,
                        written: bool = True) -> tuple[str, Output, _core.Ir]:
    """(format, output, IR): the source read through the read planner, parsed by the
    Rust parser of its format (`fmt`, else the one its first bytes give; a TIFF variant's
    file, rejected unless the TIFF parser finds that variant), projected and
    mirrored under refkv_source by the Rust core. The output pins source 0's size, and its
    ETag when every response gave the same strong one. `connections`: requests at a time to
    a remote source (default 8); `byte_cost`: seconds of wall time the planner charges a
    byte fetched (default 0.1 s per MB); `layouts`: the layouts the projection writes beside
    the image ("dataset"; default: every one the format has); `checksums`: the host will record
    the ranges' checksums, so that a sharded array's ranges are not joined across inner chunks
    (spec/zarrify.md §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>),
    and, when `written` (the output is written as it is, not only built from), a payload over the
    limit only with them is `refkv._core.Failed`."""
    transport = transport or await asyncio.to_thread(open_transport, location, policy)
    if fmt is None:
        fmt = await sniffed(transport)
        if fmt is None:
            raise Rejected("not a file of any supported format")
    named = fmt
    run = new_run("tiff" if fmt in TIFF_VARIANTS else fmt, transport, connections, byte_cost)
    await drive_async(run, transport, connections)
    fmt = run.format  # a TIFF the core found to be NDPI is "ndpi"
    if run.beside:
        raise NamesSiblingsError(fmt)
    if named in TIFF_VARIANTS and fmt != named:  # before the projection reads more
        raise Rejected(f"not {TIFF_VARIANTS[named]}: the TIFF parser read it as {fmt}")

    def output() -> tuple[Output, _core.Ir]:
        try:
            t, ir = run.output(url or (location if transport.remote else path_reference(location)),
                               mirror, layouts, bool(checksums), written)
        except _core.Rejected as e:
            raise Rejected(str(e)) from None
        return from_rust(t), ir

    # the projection, and its output's conversion, on a thread: both grow with the chunks
    out, ir = await asyncio.to_thread(output)
    out.ir = ir
    out.size = transport.size
    out.etag = transport.etag() if url in (None, location) else None
    return fmt, out, ir


def zarrify(location: str, url: str | None = None, mirror: bool = True, policy: Policy | None = None,
            transport: Transport | None = None, connections: int | None = None,
            byte_cost: float | None = None, fmt: str | None = None,
            layouts: list[str] | None = None, checksums: bool = False) -> tuple[str, Output, _core.Ir]:
    """`zarrify_async`, the caller's thread waiting for it (`refkv._sync.run`)."""
    return _sync.run(zarrify_async(location, url, mirror, policy, transport, connections, byte_cost, fmt,
                                   layouts, checksums))
